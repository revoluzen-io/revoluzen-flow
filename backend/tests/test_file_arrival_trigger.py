"""File-arrival triggers: a new file in a folder starts one run.

The watcher is driven here with a fake folder listing and a fake clock, against
a real SQLite database, so every rule can be checked without a real server:
files already there never run, a file still being copied waits, each file runs
once (also across restarts and across two servers), a replaced file runs again,
names are filtered, runs go one at a time oldest first, and a pipeline that
cannot run yet keeps its files waiting instead of using them up.

The listings for SFTP, S3 and Azure are checked against recorded replies, with
no network: a fake SSH client, botocore's Stubber, and an httpx transport that
answers with an Azure "List Blobs" document.

The last class runs the real scheduler path end to end: a published pipeline
reads the file whose path the trigger passes, and the run is recorded as
started by a file arriving.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from revoluzen_flow.ir.schema import PipelineStatus, Step, StepType, Workflow, WorkflowParameter
from revoluzen_flow.ir.versioning import WorkflowStore
from revoluzen_flow.monitoring.store import ExecutionRecord, ExecutionStore
from revoluzen_flow.scheduling.models import Schedule, ScheduleType
from revoluzen_flow.scheduling.store import ScheduleStore
from revoluzen_flow.storage.database import Database
from revoluzen_flow.triggers.file_listing import (
    FoundFile,
    ListingError,
    list_azure_blob,
    list_local,
    list_s3,
    list_sftp,
    shared_key_authorization,
)
from revoluzen_flow.triggers.file_watch import FileTriggerWatcher, name_matches
from revoluzen_flow.triggers.store import TriggerStore

T0 = datetime(2026, 11, 11, 9, 0, 0, tzinfo=timezone.utc)


# ── Fixtures ─────────────────────────────────────────────────────────────


class FakeScheduler:
    """Records the runs the watcher asks for; a run 'ends' when the test says."""

    def __init__(self):
        self.started: list[dict] = []
        self.busy: set[str] = set()
        self.refuse = False

    def is_trigger_busy(self, trigger_id):
        return trigger_id in self.busy

    def start_trigger_run(self, row, **options):
        if self.refuse:
            return False
        self.busy.add(row["id"])
        self.started.append({"row": row, **options})
        return True

    def finish(self, status="success", execution_id="exe"):
        run = self.started[-1]
        self.busy.discard(run["row"]["id"])
        run["on_finish"](status, execution_id)


class Folder:
    """A folder whose listing the test controls."""

    def __init__(self):
        self.files: dict[str, FoundFile] = {}
        self.error: Exception | None = None
        self.calls = 0

    def put(self, name, size=10, modified=None, tag=""):
        self.files[name] = FoundFile(path=f"/in/{name}", name=name, size=size, modified=modified or T0, tag=tag)

    def remove(self, name):
        self.files.pop(name, None)

    def __call__(self, location, folder, *, app_state, workspace_id, connection_id=""):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return list(self.files.values())


class Clock:
    def __init__(self, start=T0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now = self.now + timedelta(seconds=seconds)


@pytest.fixture
def env(tmp_path):
    db = Database(str(tmp_path / "triggers.db"))
    state = {
        "db": db,
        "store": WorkflowStore(db=db),
        "schedule_store": ScheduleStore(db=db),
        "execution_store": ExecutionStore(db=db),
        "data_dir": str(tmp_path),
    }
    scheduler = FakeScheduler()
    folder = Folder()
    clock = Clock()
    watcher = FileTriggerWatcher(scheduler, lister=folder, clock=clock, app_state=state)
    return SimpleNamespace(
        db=db, state=state, scheduler=scheduler, folder=folder, clock=clock, watcher=watcher,
        files=TriggerStore(db),
    )


def _pipeline(env, *, published=True, parameters=("file_path",), settings=None, name="Load orders"):
    wf = Workflow(
        name=name,
        steps=[Step(id="read", type=StepType.SOURCE, label="Read the file",
                    params={"connector_type": "csv", "file_path": "${param.file_path}"})],
        parameters=[WorkflowParameter(name=p, type="string") for p in parameters],
        metadata={"execution_settings": settings} if settings else {},
    )
    saved = env.state["store"].save(wf)
    if published:
        env.state["store"].update_status(saved.workflow.id, PipelineStatus.PUBLISHED)
    return saved.workflow.id


def _trigger(env, workflow_id, **fields):
    defaults = dict(
        workflow_id=workflow_id, schedule_type=ScheduleType.FILE_ARRIVAL, file_location="local",
        file_folder="in", file_pattern="*.csv", file_parameter="file_path", file_settle_seconds=60,
        created_at=T0, updated_at=T0,
    )
    defaults.update(fields)
    schedule = env.state["schedule_store"].create(Schedule(**defaults))
    return schedule.model_dump(mode="json")


def _look(env, row, seconds=60):
    """One poll, after `seconds` have passed."""
    env.clock.advance(seconds)
    return env.watcher.check(row)


def _paths(env):
    return [run["parameter_overrides"].get("file_path") for run in env.scheduler.started]


# ── The rules ────────────────────────────────────────────────────────────


class TestOneRunPerNewFile:
    def test_files_already_there_never_run(self, env):
        env.folder.put("old.csv", modified=T0 - timedelta(days=1))
        row = _trigger(env, _pipeline(env))
        for _ in range(4):
            _look(env, row)
        assert env.scheduler.started == []

    def test_a_new_file_runs_once_with_its_path(self, env):
        env.folder.put("old.csv", modified=T0 - timedelta(days=1))
        row = _trigger(env, _pipeline(env))
        _look(env, row)  # notes what is already there
        env.folder.put("new.csv", modified=T0 + timedelta(minutes=2))
        first = _look(env, row)
        assert env.scheduler.started == [], "a file seen once may still be arriving"
        assert "still arriving" in first
        message = _look(env, row)
        assert _paths(env) == ["/in/new.csv"]
        assert message.startswith("Started a run for new.csv")
        run = env.scheduler.started[0]
        assert run["triggered_by"] == "file_arrival"
        assert run["run_metadata"]["file"]["name"] == "new.csv"
        env.scheduler.finish("success", "exe-1")
        for _ in range(3):
            _look(env, row)
        assert _paths(env) == ["/in/new.csv"], "the same file never starts a second run"
        done = env.files.recent_files(row["id"])
        assert [(f["path"], f["status"], f["run_status"], f["execution_id"]) for f in done] == [
            ("/in/new.csv", "done", "success", "exe-1"),
        ]

    def test_remembered_across_a_restart(self, env):
        row = _trigger(env, _pipeline(env))
        _look(env, row)
        env.folder.put("a.csv", modified=T0 + timedelta(minutes=2))
        _look(env, row)
        _look(env, row)
        env.scheduler.finish()
        restarted = FileTriggerWatcher(env.scheduler, lister=env.folder, clock=env.clock, app_state=env.state)
        for _ in range(3):
            env.clock.advance(60)
            restarted.check(row)
        assert _paths(env) == ["/in/a.csv"]

    def test_two_servers_never_start_the_same_file(self, env):
        row = _trigger(env, _pipeline(env))
        other_scheduler = FakeScheduler()
        other = FileTriggerWatcher(other_scheduler, lister=env.folder, clock=env.clock, app_state=env.state)
        _look(env, row)
        other.check(row)
        env.folder.put("a.csv", modified=T0 + timedelta(minutes=2))
        _look(env, row)
        other.check(row)
        _look(env, row)
        other.check(row)
        assert len(env.scheduler.started) + len(other_scheduler.started) == 1

    def test_a_replaced_file_is_a_new_file(self, env):
        row = _trigger(env, _pipeline(env))
        _look(env, row)
        env.folder.put("daily.csv", size=10, modified=T0 + timedelta(minutes=2))
        _look(env, row)
        _look(env, row)
        env.scheduler.finish()
        env.folder.put("daily.csv", size=25, modified=T0 + timedelta(days=1))
        _look(env, row)
        _look(env, row)
        assert _paths(env) == ["/in/daily.csv", "/in/daily.csv"]

    def test_a_content_tag_tells_versions_apart(self, env):
        row = _trigger(env, _pipeline(env))
        _look(env, row)
        env.folder.put("a.csv", modified=T0 + timedelta(minutes=2), tag="etag-1")
        _look(env, row)
        _look(env, row)
        env.scheduler.finish()
        # Same size and time, new content: an object store's tag says so.
        env.folder.put("a.csv", modified=T0 + timedelta(minutes=2), tag="etag-2")
        _look(env, row)
        _look(env, row)
        assert len(env.scheduler.started) == 2

    def test_also_run_for_files_already_there(self, env):
        env.folder.put("old.csv", modified=T0 - timedelta(days=1))
        row = _trigger(env, _pipeline(env), file_include_existing=True)
        _look(env, row)
        _look(env, row)
        assert _paths(env) == ["/in/old.csv"]

    def test_a_file_changed_after_saving_counts_as_new(self, env):
        """It was not there, as it is now, when the trigger was saved."""
        env.folder.put("a.csv", modified=T0 + timedelta(seconds=5))
        row = _trigger(env, _pipeline(env))
        _look(env, row)
        _look(env, row)
        assert _paths(env) == ["/in/a.csv"]

    def test_a_new_folder_takes_a_new_record_of_what_is_there(self, env):
        env.folder.put("old.csv", modified=T0 - timedelta(days=1))
        row = _trigger(env, _pipeline(env))
        _look(env, row)
        env.folder.put("elsewhere.csv", modified=T0 - timedelta(days=2))
        moved = env.state["schedule_store"].update(row["id"], {"file_folder": "other"}).model_dump(mode="json")
        _look(env, moved)
        _look(env, moved)
        assert env.scheduler.started == []


class TestStillArriving:
    def test_a_growing_file_waits_until_it_stops(self, env):
        row = _trigger(env, _pipeline(env), file_settle_seconds=120)
        _look(env, row)
        for size in (100, 200, 300):
            env.folder.put("big.csv", size=size, modified=T0 + timedelta(minutes=size))
            _look(env, row)
        assert env.scheduler.started == []
        _look(env, row, seconds=60)
        assert env.scheduler.started == [], "unchanged for 60 of the 120 seconds"
        _look(env, row, seconds=60)
        assert _paths(env) == ["/in/big.csv"]

    def test_no_settle_time_still_needs_two_identical_looks(self, env):
        row = _trigger(env, _pipeline(env), file_settle_seconds=0)
        _look(env, row)
        env.folder.put("a.csv", modified=T0 + timedelta(minutes=2))
        _look(env, row)
        assert env.scheduler.started == []
        _look(env, row)
        assert len(env.scheduler.started) == 1


class TestNames:
    @pytest.mark.parametrize("name,pattern,expected", [
        ("orders_2026.csv", "orders_*.csv", True),
        ("ORDERS_2026.CSV", "orders_*.csv", True),
        ("orders_2026.csv.tmp", "orders_*.csv", False),
        ("report.txt", "*.csv;*.txt", True),
        ("report.json", "*.csv; *.txt", False),
        ("anything", "", True),
    ])
    def test_pattern(self, name, pattern, expected):
        assert name_matches(name, pattern) is expected

    def test_only_matching_files_run(self, env):
        row = _trigger(env, _pipeline(env), file_pattern="orders_*.csv")
        _look(env, row)
        env.folder.put("orders_1.csv", modified=T0 + timedelta(minutes=2))
        env.folder.put("orders_1.csv.part", modified=T0 + timedelta(minutes=2))
        env.folder.put("notes.txt", modified=T0 + timedelta(minutes=2))
        _look(env, row)
        _look(env, row)
        assert _paths(env) == ["/in/orders_1.csv"]


class TestOneAtATime:
    def test_waiting_files_run_oldest_first_after_the_current_run(self, env):
        row = _trigger(env, _pipeline(env))
        _look(env, row)
        env.folder.put("b.csv", modified=T0 + timedelta(minutes=3))
        env.folder.put("a.csv", modified=T0 + timedelta(minutes=2))
        _look(env, row)
        message = _look(env, row)
        assert _paths(env) == ["/in/a.csv"]
        assert "1 more file waiting" in message
        waiting = _look(env, row)
        assert _paths(env) == ["/in/a.csv"]
        assert "waiting for the current run to finish" in waiting
        env.scheduler.finish()
        _look(env, row)
        assert _paths(env) == ["/in/a.csv", "/in/b.csv"]

    def test_a_run_turned_away_at_capacity_gives_its_file_back(self, env):
        row = _trigger(env, _pipeline(env))
        _look(env, row)
        env.folder.put("a.csv", modified=T0 + timedelta(minutes=2))
        _look(env, row)
        env.scheduler.refuse = True
        message = _look(env, row)
        assert "run limit" in message
        env.scheduler.refuse = False
        _look(env, row)
        assert _paths(env) == ["/in/a.csv"]


class TestNothingIsLost:
    def _arrive(self, env, row):
        _look(env, row)
        env.folder.put("a.csv", modified=T0 + timedelta(minutes=2))
        _look(env, row)

    def test_an_unpublished_pipeline_keeps_its_files_waiting(self, env):
        wid = _pipeline(env, published=False)
        row = _trigger(env, wid)
        self._arrive(env, row)
        message = _look(env, row)
        assert env.scheduler.started == []
        assert "not published" in message and "1 file waiting" in message
        env.state["store"].update_status(wid, PipelineStatus.PUBLISHED)
        _look(env, row)
        assert _paths(env) == ["/in/a.csv"]

    def test_a_paused_pipeline_keeps_its_files_waiting(self, env):
        wid = _pipeline(env)
        store = env.state["store"]
        latest = store.get(wid)
        latest.workflow.is_active_dev = False
        store.save(latest.workflow)
        row = _trigger(env, wid)
        self._arrive(env, row)
        assert "paused" in _look(env, row)
        assert env.scheduler.started == []

    def test_a_missing_parameter_keeps_the_files_waiting(self, env):
        row = _trigger(env, _pipeline(env, parameters=("path",)))
        self._arrive(env, row)
        message = _look(env, row)
        assert env.scheduler.started == []
        assert 'no parameter named "file_path"' in message

    def test_no_parameter_still_runs(self, env):
        row = _trigger(env, _pipeline(env, parameters=()), file_parameter="")
        self._arrive(env, row)
        _look(env, row)
        assert env.scheduler.started[0]["parameter_overrides"] == {}

    def test_a_listing_that_fails_says_why(self, env):
        row = _trigger(env, _pipeline(env))
        env.folder.error = ListingError("Cannot list /in on sftp.example.com: Authentication failed.")
        message = _look(env, row)
        assert message == "Cannot check the folder: Cannot list /in on sftp.example.com: Authentication failed."
        saved = env.state["schedule_store"].get(row["id"])
        assert saved.status_message == message and saved.last_checked_at is not None

    def test_the_overlap_setting_makes_files_wait(self, env):
        wid = _pipeline(env, settings={"enable_overlap_detection": True, "overlap_policy": "skip"})
        env.state["execution_store"].record(ExecutionRecord(workflow_id=wid, workflow_name="x", status="running"))
        row = _trigger(env, wid)
        self._arrive(env, row)
        message = _look(env, row)
        assert env.scheduler.started == []
        assert "overlap setting is skip" in message

    def test_cancel_previous_stops_the_other_run_and_goes_ahead(self, env):
        wid = _pipeline(env, settings={"enable_overlap_detection": True, "overlap_policy": "cancel_previous"})
        other = env.state["execution_store"].record(ExecutionRecord(workflow_id=wid, workflow_name="x", status="running"))
        row = _trigger(env, wid)
        self._arrive(env, row)
        _look(env, row)
        assert _paths(env) == ["/in/a.csv"]
        assert env.state["execution_store"].get(other.id).status == "cancelled"


class TestPollAndHousekeeping:
    def test_due_follows_the_poll_interval(self, env):
        row = _trigger(env, _pipeline(env), file_poll_seconds=300)
        assert env.watcher.due(row, env.clock.now)
        env.watcher.check(row)
        assert not env.watcher.due(row, env.clock.now + timedelta(seconds=299))
        assert env.watcher.due(row, env.clock.now + timedelta(seconds=300))

    def test_a_check_under_way_is_not_started_twice(self, env):
        row = _trigger(env, _pipeline(env))
        assert env.watcher.begin(row["id"])
        assert not env.watcher.due(row, env.clock.now)
        assert not env.watcher.begin(row["id"])
        env.watcher.end(row["id"])
        assert env.watcher.begin(row["id"])

    def test_a_deleted_trigger_leaves_nothing_behind(self, env):
        row = _trigger(env, _pipeline(env))
        _look(env, row)
        env.folder.put("a.csv", modified=T0 + timedelta(minutes=2))
        _look(env, row)
        _look(env, row)
        env.state["schedule_store"].delete(row["id"])
        env.files.forget_deleted_triggers()
        assert env.files.known_versions(row["id"]) == set()

    def test_a_file_gone_for_a_month_is_forgotten(self, env):
        row = _trigger(env, _pipeline(env))
        _look(env, row)
        env.folder.put("a.csv", modified=T0 + timedelta(minutes=2))
        _look(env, row)
        _look(env, row)
        env.scheduler.finish()
        env.folder.remove("a.csv")
        _look(env, row)
        assert env.files.known_versions(row["id"]), "remembered while it may come back"
        env.db.execute_write("UPDATE trigger_files SET gone_since = '2020-01-01T00:00:00+00:00'")
        _look(env, row)
        assert env.files.known_versions(row["id"]) == set()


# ── Listings, with recorded replies and no network ───────────────────────


class TestLocalListing:
    def test_lists_files_only_with_full_paths(self, tmp_path):
        folder = tmp_path / "in"
        folder.mkdir()
        (folder / "a.csv").write_bytes(b"x\n1\n")
        (folder / "sub").mkdir()
        found = list_local("in", str(tmp_path))
        assert [(f.name, f.size) for f in found] == [("a.csv", 4)]
        assert found[0].path == str(folder / "a.csv")
        assert found[0].modified is not None

    def test_a_missing_folder_is_said_plainly(self, tmp_path):
        with pytest.raises(ListingError, match="does not exist"):
            list_local("nope", str(tmp_path))


class TestSftpListing:
    class _Attr(SimpleNamespace):
        pass

    def _client(self, entries, seen):
        outer = self

        class _Sftp:
            def listdir_attr(self, path):
                seen["path"] = path
                return entries

            def close(self):
                seen["sftp_closed"] = True

        class _Ssh:
            def connect(self, **kwargs):
                seen["connect"] = kwargs

            def open_sftp(self):
                return _Sftp()

            def close(self):
                seen["ssh_closed"] = True

        return lambda config: (seen.setdefault("config", config), _Ssh())[1]

    def test_lists_regular_files_and_checks_the_host_key(self):
        import stat as stat_mod

        seen: dict = {}
        entries = [
            self._Attr(filename="orders.csv", st_mode=stat_mod.S_IFREG | 0o644, st_size=42, st_mtime=1_762_851_600),
            self._Attr(filename="archive", st_mode=stat_mod.S_IFDIR | 0o755, st_size=0, st_mtime=1_762_851_600),
        ]
        config = {"host": "sftp.example.com", "port": 22, "username": "flow", "password": "pw",
                  "host_key_fingerprint": "SHA256:abc"}
        found = list_sftp("/upload", config, client_factory=self._client(entries, seen))
        assert [(f.path, f.size) for f in found] == [("/upload/orders.csv", 42)]
        assert seen["path"] == "/upload"
        assert seen["connect"]["hostname"] == "sftp.example.com" and seen["connect"]["password"] == "pw"
        assert seen["config"]["host_key_fingerprint"] == "SHA256:abc", "the host key settings reach the client"
        assert seen["ssh_closed"]

    def test_a_failure_names_the_server_not_the_password(self):
        class _Ssh:
            def connect(self, **kwargs):
                raise OSError("Authentication failed.")

            def close(self):
                pass

        config = {"host": "sftp.example.com", "username": "flow", "password": "hunter2-secret"}
        with pytest.raises(ListingError) as caught:
            list_sftp("/upload", config, client_factory=lambda cfg: _Ssh())
        assert "sftp.example.com" in str(caught.value) and "hunter2" not in str(caught.value)


class TestS3Listing:
    def test_lists_the_folder_from_a_recorded_reply(self):
        boto3 = pytest.importorskip("boto3")
        from botocore.stub import Stubber

        client = boto3.client("s3", region_name="us-east-1", aws_access_key_id="x", aws_secret_access_key="y")
        stub = Stubber(client)
        stub.add_response(
            "list_objects_v2",
            {
                "Contents": [
                    {"Key": "incoming/orders_1.csv", "Size": 120, "ETag": '"abc123"',
                     "LastModified": datetime(2026, 11, 11, 8, 0, tzinfo=timezone.utc)},
                    {"Key": "incoming/", "Size": 0, "ETag": '"d41d8"',
                     "LastModified": datetime(2026, 11, 11, 8, 0, tzinfo=timezone.utc)},
                ],
                "IsTruncated": False,
                "KeyCount": 2,
            },
            {"Bucket": "landing", "Prefix": "incoming/", "Delimiter": "/"},
        )
        with stub:
            found = list_s3("/incoming", {"bucket": "landing"}, client=client)
        assert [(f.path, f.name, f.size, f.tag) for f in found] == [("incoming/orders_1.csv", "orders_1.csv", 120, "abc123")]
        assert found[0].version == "tag:abc123"


_AZURE_REPLY = b"""<?xml version="1.0" encoding="utf-8"?>
<EnumerationResults ServiceEndpoint="https://acct.blob.core.windows.net/" ContainerName="landing">
  <Prefix>incoming/</Prefix>
  <Delimiter>/</Delimiter>
  <Blobs>
    <Blob>
      <Name>incoming/orders_1.csv</Name>
      <Properties>
        <Last-Modified>Tue, 11 Nov 2026 08:00:00 GMT</Last-Modified>
        <Etag>0x8DDF0000000001</Etag>
        <Content-Length>2048</Content-Length>
        <BlobType>BlockBlob</BlobType>
      </Properties>
    </Blob>
    <BlobPrefix><Name>incoming/archive/</Name></BlobPrefix>
  </Blobs>
  <NextMarker />
</EnumerationResults>"""


class TestAzureListing:
    def _client(self, seen):
        import httpx

        def answer(request):
            seen.append(request)
            return httpx.Response(200, content=_AZURE_REPLY, headers={"Content-Type": "application/xml"})

        return httpx.Client(transport=httpx.MockTransport(answer))

    def test_signs_with_the_account_key_and_reads_the_reply(self):
        seen = []
        key = base64.b64encode(b"k" * 32).decode()
        config = {"account_name": "acct", "container": "landing", "auth_method": "account_key", "account_key": key}
        found = list_azure_blob("incoming", config, client=self._client(seen))
        assert [(f.path, f.name, f.size, f.tag) for f in found] == [
            ("incoming/orders_1.csv", "orders_1.csv", 2048, "0x8DDF0000000001"),
        ]
        request = seen[0]
        assert request.url.host == "acct.blob.core.windows.net" and request.url.path == "/landing"
        assert dict(request.url.params)["prefix"] == "incoming/"
        auth = request.headers["Authorization"]
        assert auth.startswith("SharedKey acct:")
        # The signature is Azure's string to sign, keyed with the account key.
        query = dict(request.url.params)
        to_sign = (
            "GET" + "\n" * 12
            + f"x-ms-date:{request.headers['x-ms-date']}\nx-ms-version:{request.headers['x-ms-version']}\n"
            + "/acct/landing" + "".join(f"\n{k}:{query[k]}" for k in sorted(query))
        )
        expected = base64.b64encode(hmac.new(b"k" * 32, to_sign.encode(), hashlib.sha256).digest()).decode()
        assert auth == f"SharedKey acct:{expected}"
        assert key not in str(request.url) and key not in auth

    def test_a_sas_token_goes_in_the_query_and_no_key_is_sent(self):
        seen = []
        config = {"account_name": "acct", "container": "landing", "auth_method": "sas_token",
                  "sas_token": "?sv=2022-11-02&sp=rl&sig=abc%3D"}
        list_azure_blob("incoming", config, client=self._client(seen))
        request = seen[0]
        assert "Authorization" not in request.headers
        params = dict(request.url.params)
        assert params["sig"] == "abc=" and params["comp"] == "list"

    def test_an_emulator_endpoint_signs_its_own_path(self):
        header = shared_key_authorization(
            account="devstoreaccount1", key=base64.b64encode(b"z" * 32).decode(), method="GET",
            container="c", query={"comp": "list", "restype": "container"},
            headers={"x-ms-date": "d", "x-ms-version": "v"}, endpoint_path="/devstoreaccount1",
        )
        to_sign = "GET" + "\n" * 12 + "x-ms-date:d\nx-ms-version:v\n/devstoreaccount1/devstoreaccount1/c\ncomp:list\nrestype:container"
        expected = base64.b64encode(hmac.new(b"z" * 32, to_sign.encode(), hashlib.sha256).digest()).decode()
        assert header == f"SharedKey devstoreaccount1:{expected}"

    def test_an_error_reply_is_said_plainly(self):
        import httpx

        client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404, content=b"<Error/>")))
        config = {"account_name": "acct", "container": "nope", "sas_token": "sv=1&sig=x"}
        with pytest.raises(ListingError, match="does not exist"):
            list_azure_blob("", config, client=client)


# ── End to end through the real scheduler ────────────────────────────────

from tests.conftest_fixtures_v2 import (  # noqa: E402,F401
    data_dir, db_fixture, app_v2, client, admin_token, authed_client,
)


@pytest.fixture(scope="module", autouse=True)
def _no_scheduler_loop():
    """The test drives each look itself; the background loop would race it."""
    old = os.environ.get("REVOLUZEN_FLOW_ENABLE_SCHEDULER")
    os.environ["REVOLUZEN_FLOW_ENABLE_SCHEDULER"] = "0"
    yield
    if old is None:
        os.environ.pop("REVOLUZEN_FLOW_ENABLE_SCHEDULER", None)
    else:
        os.environ["REVOLUZEN_FLOW_ENABLE_SCHEDULER"] = old


def _wait_for_files_done(trigger_id, count, timeout=60):
    """Until the trigger has noted `count` finished runs. The note is the last
    thing a run does, after its record and the trigger's lock."""
    from revoluzen_flow.main import app_state
    from revoluzen_flow.triggers.store import trigger_store

    store = trigger_store(app_state)
    deadline = time.time() + timeout
    while time.time() < deadline:
        done = [f for f in store.recent_files(trigger_id) if f["status"] == "done"]
        if len(done) >= count:
            return done
        time.sleep(0.2)
    raise AssertionError(f"trigger {trigger_id} did not finish {count} runs within {timeout}s")


class TestEndToEnd:
    def test_a_file_dropped_in_a_folder_runs_the_published_pipeline_on_it(self, authed_client, data_dir):
        from revoluzen_flow.main import app_state

        incoming = Path(data_dir) / "incoming"
        incoming.mkdir(exist_ok=True)
        old = incoming / "orders_old.csv"
        old.write_text("id,amount\n1,10\n", encoding="utf-8")
        long_ago = time.time() - 86_400
        os.utime(old, (long_ago, long_ago))

        r = authed_client.post("/api/workflows", json={
            "name": "Load arriving orders",
            "steps": [
                {"id": "read", "type": "source", "label": "Read the file",
                 "params": {"connector_type": "csv", "file_path": "${param.file_path}", "header": True}},
                {"id": "big", "type": "filter", "label": "Big orders", "params": {"condition": "amount >= 100"}},
            ],
            "connections": [{"from_step": "read", "to_step": "big"}],
            "parameters": [{"name": "file_path", "type": "string", "required": True}],
        })
        assert r.status_code in (200, 201), r.text
        wid = r.json().get("id") or r.json()["workflow"]["id"]

        body = {
            "workflow_id": wid, "schedule_type": "file_arrival", "file_location": "local",
            "file_folder": "incoming", "file_pattern": "orders_*.csv", "file_parameter": "file_path",
            "file_settle_seconds": 0,
        }
        refused = authed_client.post("/api/schedules", json=body)
        assert refused.status_code == 400 and "Publish" in refused.text, refused.text
        app_state["store"].update_status(wid, PipelineStatus.PUBLISHED)
        created = authed_client.post("/api/schedules", json=body)
        assert created.status_code == 200, created.text
        row = created.json()
        assert row["schedule_type"] == "file_arrival"

        preview = authed_client.post("/api/schedules/check-folder", json=body)
        assert preview.status_code == 200, preview.text
        assert preview.json()["matching"] == 1

        watcher = app_state["scheduler"].file_watcher
        watcher.check(row)  # notes the old file
        new = incoming / "orders_new.csv"
        new.write_text("id,amount\n1,150\n2,90\n3,400\n", encoding="utf-8")
        watcher.check(row)  # first sight
        message = watcher.check(row)  # unchanged: it runs
        assert message.startswith("Started a run for orders_new.csv"), message

        done = _wait_for_files_done(row["id"], 1)
        run = app_state["execution_store"].get(done[0]["execution_id"])
        assert run.status == "success", run.error_message
        assert run.triggered_by == "file_arrival"
        assert run.metadata["file"]["path"] == os.path.abspath(new)
        assert run.metadata["parameter_values"]["file_path"] == os.path.abspath(new), "a replay reads the same file"
        big = next(s for s in run.step_logs if s.step_id == "big")
        assert big.rows_processed == 2, "the run read the new file, which has two big orders"

        files = authed_client.get(f"/api/schedules/{row['id']}/files")
        assert files.status_code == 200
        assert [(f["path"], f["status"], f["run_status"]) for f in files.json()] == [
            (os.path.abspath(new), "done", "success"),
        ]

        for _ in range(3):
            watcher.check(row)
        time.sleep(0.5)
        again = [r for r in app_state["execution_store"].list_by_workflow(wid) if r.get("triggered_by") == "file_arrival"]
        assert len(again) == 1, "the same file never starts a second run"

        schedule = authed_client.get(f"/api/schedules/{row['id']}").json()
        assert schedule["last_run_status"] == "success"

    def test_a_connection_that_does_not_exist_is_refused(self, authed_client):
        from revoluzen_flow.main import app_state

        r = authed_client.post("/api/workflows", json={"name": "Lonely", "steps": [
            {"id": "s", "type": "source", "label": "S", "params": {"connector_type": "csv", "file_path": "orders.csv"}},
        ]})
        wid = r.json().get("id") or r.json()["workflow"]["id"]
        app_state["store"].update_status(wid, PipelineStatus.PUBLISHED)
        r = authed_client.post("/api/schedules", json={
            "workflow_id": wid, "schedule_type": "file_arrival", "file_location": "sftp",
            "file_connection_id": "no-such-connection", "file_folder": "/upload",
        })
        assert r.status_code == 400 and "not found" in r.text
