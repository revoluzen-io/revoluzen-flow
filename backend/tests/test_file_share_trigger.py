"""File-arrival triggers on a Windows file share (SMB).

The share is ``FakeSmbClient`` from test_file_share.py: the smbclient
functions Flow calls, over folders under pytest's tmp_path, where each folder
entry carries its size and last-write time as a real server's listing does.
No SMB server, account or network is used.

Checked here: the listing (files only, sizes and times from the listing
itself, the folder read like a File step path, full ``\\\\server\\share``
paths, plain errors without the password); the watcher on a share folder
(files already there never run, each new file runs once with a path the File
step reads with the same connection, names match in any case, a replaced file
runs again, a file still open in the program writing it waits); the check
behind "Check the folder now"; and one run end to end through the API and the
real scheduler, where the pipeline's File step reads the file the trigger
passes.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from revoluzen_flow.connections import file_share
from revoluzen_flow.ir.schema import PipelineStatus, Step, StepType, Workflow, WorkflowParameter
from revoluzen_flow.ir.versioning import WorkflowStore
from revoluzen_flow.monitoring.store import ExecutionStore
from revoluzen_flow.scheduling.models import Schedule, ScheduleType
from revoluzen_flow.scheduling.store import ScheduleStore
from revoluzen_flow.storage.database import Database
from revoluzen_flow.triggers.file_listing import (
    ListingError,
    list_folder,
    list_smb,
    still_being_written,
)
from revoluzen_flow.triggers.file_watch import FileTriggerWatcher, preview_folder
from revoluzen_flow.triggers.store import TriggerStore
from tests.test_file_share import CONFIG, PASSWORD, FakeSmbClient

T0 = datetime(2026, 11, 11, 9, 0, 0, tzinfo=timezone.utc)
SHARE = "\\\\fileserver01\\mainframe"
DAILY = f"{SHARE}\\outbound\\daily"  # the connection's folder is outbound


class Connections:
    """The saved connections: one Windows file share and one S3 bucket."""

    def __init__(self, config: dict) -> None:
        self.config = config

    def get(self, connection_id, workspace_id=None):
        if workspace_id not in (None, "default"):
            return None
        if connection_id == "share1":
            return SimpleNamespace(config=dict(self.config), type="smb", credential_id=None)
        if connection_id == "bucket1":
            return SimpleNamespace(config={"bucket": "landing"}, type="s3", credential_id=None)
        return None


class FakeScheduler:
    """Records the runs the watcher asks for; a run 'ends' when the test says."""

    def __init__(self) -> None:
        self.started: list[dict] = []
        self.busy: set[str] = set()

    def is_trigger_busy(self, trigger_id):
        return trigger_id in self.busy

    def start_trigger_run(self, row, **options):
        self.busy.add(row["id"])
        self.started.append({"row": row, **options})
        return True

    def finish(self, status="success", execution_id="exe"):
        run = self.started[-1]
        self.busy.discard(run["row"]["id"])
        run["on_finish"](status, execution_id)


class Clock:
    def __init__(self, start: datetime = T0) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


@pytest.fixture
def fake(tmp_path, monkeypatch):
    client = FakeSmbClient(tmp_path / "servers")
    client.share()
    monkeypatch.setattr(file_share, "_client", lambda: client)
    return client


def _drop(fake: FakeSmbClient, name: str, data: bytes = b"x", *, at: datetime = T0,
          folder: tuple[str, ...] = ("outbound", "daily")):
    """A file on the share, with the last-write time the listing will show."""
    target = fake.share().joinpath(*folder)
    target.mkdir(parents=True, exist_ok=True)
    path = target / name
    path.write_bytes(data)
    os.utime(path, (at.timestamp(), at.timestamp()))
    return path


@pytest.fixture
def env(tmp_path, fake):
    db = Database(str(tmp_path / "triggers.db"))
    state = {
        "db": db,
        "store": WorkflowStore(db=db),
        "schedule_store": ScheduleStore(db=db),
        "execution_store": ExecutionStore(db=db),
        "connection_store": Connections(CONFIG),
        "data_dir": str(tmp_path),
    }
    scheduler = FakeScheduler()
    clock = Clock()
    # The real folder listing and in-use check, over the fake share.
    watcher = FileTriggerWatcher(scheduler, clock=clock, app_state=state)
    return SimpleNamespace(db=db, state=state, scheduler=scheduler, clock=clock, watcher=watcher,
                           fake=fake, files=TriggerStore(db))


def _pipeline(env) -> str:
    wf = Workflow(
        name="Load mainframe extracts",
        steps=[Step(id="read", type=StepType.SOURCE, label="Read the extract",
                    params={"connector_type": "file", "connection_id": "share1", "file_path": "${param.file_path}"})],
        parameters=[WorkflowParameter(name="file_path", type="string")],
    )
    saved = env.state["store"].save(wf)
    env.state["store"].update_status(saved.workflow.id, PipelineStatus.PUBLISHED)
    return saved.workflow.id


def _trigger(env, workflow_id: str, **fields) -> dict:
    defaults = dict(
        workflow_id=workflow_id, schedule_type=ScheduleType.FILE_ARRIVAL, file_location="smb",
        file_connection_id="share1", file_folder="daily", file_pattern="*.dat", file_parameter="file_path",
        file_settle_seconds=60, created_at=T0, updated_at=T0,
    )
    defaults.update(fields)
    return env.state["schedule_store"].create(Schedule(**defaults)).model_dump(mode="json")


def _look(env, row: dict, seconds: float = 60) -> str:
    """One poll, after ``seconds`` have passed."""
    env.clock.advance(seconds)
    return env.watcher.check(row)


def _paths(env) -> list[str]:
    return [run["parameter_overrides"].get("file_path") for run in env.scheduler.started]


# ── The listing ──────────────────────────────────────────────────────────


def test_the_listing_has_names_sizes_times_and_full_share_paths(fake):
    _drop(fake, "CUSTMAST.DAT", b"12345", at=T0)
    _drop(fake, "orders.csv", b"ab", at=T0 + timedelta(minutes=5))
    (fake.share() / "outbound" / "daily" / "archive").mkdir()
    found = sorted(list_smb("daily", CONFIG), key=lambda f: f.name)
    assert [(f.path, f.name, f.size, f.modified) for f in found] == [
        (f"{DAILY}\\CUSTMAST.DAT", "CUSTMAST.DAT", 5, T0),
        (f"{DAILY}\\orders.csv", "orders.csv", 2, T0 + timedelta(minutes=5)),
    ]
    assert found[0].version != found[1].version
    assert fake.entry_stats == 0, "sizes and times come with the listing, not one request per file"
    assert fake.sign_ins[-1]["username"] == "CORP\\svc_flow"
    assert fake.closed == 1, "the connection is closed after each look"


def test_the_folder_is_read_like_a_file_step_path(fake):
    _drop(fake, "a.dat", folder=("outbound",))
    _drop(fake, "b.dat", folder=("inbound",))
    _drop(fake, "c.dat", folder=())
    assert [f.path for f in list_smb("", CONFIG)] == [f"{SHARE}\\outbound\\a.dat"]  # the connection's folder
    assert [f.path for f in list_smb("\\inbound", CONFIG)] == [f"{SHARE}\\inbound\\b.dat"]  # from the top
    assert [f.path for f in list_smb("../inbound", CONFIG)] == [f"{SHARE}\\inbound\\b.dat"]
    assert [f.path for f in list_smb("\\\\FILESERVER01\\MainFrame\\inbound", CONFIG)] == [f"{SHARE}\\inbound\\b.dat"]
    assert [f.path for f in list_smb("\\", CONFIG)] == [f"{SHARE}\\c.dat"], "the top of the share, files only"

    signed_in = len(fake.sign_ins)
    for folder, message in [
        ("\\\\collector.example\\loot\\in", "is not on"),
        ("C:\\data\\in", "is a path on a computer"),
        ("..\\..\\x", "goes above the top of the share"),
        ("da*", "file names only"),
    ]:
        with pytest.raises(ListingError, match=message):
            list_smb(folder, CONFIG)
    assert len(fake.sign_ins) == signed_in, "refused before the password is sent anywhere"
    with pytest.raises(ListingError, match=r"\\\\fileserver01\\mainframe\\outbound\\missing was not found"):
        list_smb("missing", CONFIG)


def test_listing_problems_are_said_plainly_without_the_password(fake, caplog):
    caplog.set_level("DEBUG")
    with pytest.raises(ListingError, match="fileserver01 refused the sign-in") as refused:
        list_smb("daily", {**CONFIG, "password": "wrong-" + PASSWORD})
    with pytest.raises(ListingError, match="Could not reach nowhere01 on port 445"):
        list_smb("daily", {**CONFIG, "host": "nowhere01"})
    assert PASSWORD not in str(refused.value) and PASSWORD not in caplog.text


def test_the_trigger_uses_the_saved_share_connection(env):
    _drop(env.fake, "a.dat")
    found = list_folder("smb", "daily", app_state=env.state, workspace_id="default", connection_id="share1")
    assert [f.path for f in found] == [f"{DAILY}\\a.dat"]
    with pytest.raises(ListingError, match="not found in this workspace"):
        list_folder("smb", "daily", app_state=env.state, workspace_id="default", connection_id="gone")
    with pytest.raises(ListingError, match="cannot be used to watch a Windows file share"):
        list_folder("smb", "daily", app_state=env.state, workspace_id="default", connection_id="bucket1")
    with pytest.raises(ListingError, match="cannot be used to watch SFTP"):
        list_folder("sftp", "daily", app_state=env.state, workspace_id="default", connection_id="share1")


def test_only_a_share_is_asked_whether_a_file_is_still_being_written(env):
    _drop(env.fake, "a.dat")
    where = dict(app_state=env.state, workspace_id="default", connection_id="share1")
    assert still_being_written("smb", f"{DAILY}\\a.dat", **where) is False
    env.fake.writing.add("a.dat")
    assert still_being_written("smb", f"{DAILY}\\a.dat", **where) is True
    # Opened exactly as the File step opens it: others may read, not write.
    assert env.fake.calls[-1] == ("open", f"{DAILY}\\a.dat", "rb", "r")
    assert still_being_written("smb", f"{DAILY}\\gone.dat", **where) is False, "the run reports a missing file"
    calls = len(env.fake.calls)
    assert still_being_written("local", "/in/a.dat", **where) is False
    assert len(env.fake.calls) == calls


# ── The watcher on a share folder ────────────────────────────────────────


class TestShareFolder:
    def test_files_already_there_never_run_and_each_new_file_runs_once(self, env):
        _drop(env.fake, "OLD.DAT", at=T0 - timedelta(days=1))
        row = _trigger(env, _pipeline(env))
        _look(env, row)  # notes what is already there
        _drop(env.fake, "CUSTMAST.DAT", b"0123456789", at=T0 + timedelta(minutes=2))
        _drop(env.fake, "notes.txt", at=T0 + timedelta(minutes=2))  # not *.dat
        assert "1 file still arriving" in _look(env, row)
        message = _look(env, row)
        assert message.startswith("Started a run for CUSTMAST.DAT"), message
        assert _paths(env) == [f"{DAILY}\\CUSTMAST.DAT"], "*.dat matches CUSTMAST.DAT: case does not matter"
        run = env.scheduler.started[0]
        assert run["triggered_by"] == "file_arrival"
        assert run["run_metadata"]["file"] == {
            "path": f"{DAILY}\\CUSTMAST.DAT", "name": "CUSTMAST.DAT", "size": 10,
            "modified": (T0 + timedelta(minutes=2)).isoformat(), "location": "smb", "folder": "daily",
        }
        env.scheduler.finish("success", "exe-1")
        for _ in range(3):
            _look(env, row)
        assert len(env.scheduler.started) == 1, "no second run for the same file, none for the old one"
        assert [(f["path"], f["status"], f["run_status"]) for f in env.files.recent_files(row["id"])] == [
            (f"{DAILY}\\CUSTMAST.DAT", "done", "success"),
        ]

    def test_the_path_passed_is_read_by_the_file_step_with_the_same_connection(self, env, tmp_path):
        import duckdb

        from revoluzen_flow.nodes.base import ExecutionContext
        from revoluzen_flow.nodes.file_node import FileSourceNode

        row = _trigger(env, _pipeline(env), file_pattern="*.csv")
        _look(env, row)
        _drop(env.fake, "orders_1.csv", b"id,amount\n1,10\n2,20\n", at=T0 + timedelta(minutes=2))
        _look(env, row)
        _look(env, row)
        [path] = _paths(env)
        ctx = ExecutionContext(conn=duckdb.connect(), data_dir=str(tmp_path / "data"), full_run=True,
                               app_state={"connection_store": Connections(CONFIG)})
        rel = FileSourceNode({"connection_id": "share1", "file_path": path}).execute(ctx)
        assert sorted(rel.fetchall()) == [(1, 10), (2, 20)]
        # A connection to another share refuses the path instead of reading a file of the same name there.
        other = {**CONFIG, "share": "payroll"}
        ctx = ExecutionContext(conn=duckdb.connect(), data_dir=str(tmp_path / "data"), full_run=True,
                               app_state={"connection_store": Connections(other)})
        with pytest.raises(ValueError, match="is not on"):
            FileSourceNode({"connection_id": "share1", "file_path": path}).execute(ctx)

    def test_a_replaced_file_runs_again(self, env):
        row = _trigger(env, _pipeline(env))
        _look(env, row)
        _drop(env.fake, "CUSTMAST.DAT", b"night 1", at=T0 + timedelta(minutes=2))
        _look(env, row)
        _look(env, row)
        env.scheduler.finish()
        # The next night's extract: same name, same size, a new time.
        _drop(env.fake, "CUSTMAST.DAT", b"night 2", at=T0 + timedelta(days=1))
        _look(env, row)
        _look(env, row)
        assert _paths(env) == [f"{DAILY}\\CUSTMAST.DAT", f"{DAILY}\\CUSTMAST.DAT"]

    def test_a_file_still_open_in_the_program_writing_it_waits(self, env):
        row = _trigger(env, _pipeline(env))
        _look(env, row)
        # A Windows server may list the final size and time while the copy is still going on.
        _drop(env.fake, "BIG.DAT", b"x" * 100, at=T0 + timedelta(minutes=2))
        env.fake.writing.add("BIG.DAT")
        _look(env, row)
        message = _look(env, row)
        assert env.scheduler.started == []
        assert message == "BIG.DAT is still open in the program writing it, so its run waits."
        assert env.files.known_versions(row["id"]) == set(), "nothing is used up"
        env.fake.writing.discard("BIG.DAT")
        _look(env, row)
        assert _paths(env) == [f"{DAILY}\\BIG.DAT"]

    def test_a_finished_file_goes_ahead_of_one_still_being_written(self, env):
        row = _trigger(env, _pipeline(env))
        _look(env, row)
        _drop(env.fake, "A.DAT", at=T0 + timedelta(minutes=1))
        _drop(env.fake, "B.DAT", at=T0 + timedelta(minutes=2))
        env.fake.writing.add("A.DAT")
        _look(env, row)
        assert _look(env, row).startswith("Started a run for B.DAT. 1 more file waiting.")
        env.scheduler.finish()
        env.fake.writing.discard("A.DAT")
        _look(env, row)
        assert _paths(env) == [f"{DAILY}\\B.DAT", f"{DAILY}\\A.DAT"]

    def test_a_listing_that_fails_says_why(self, env):
        row = _trigger(env, _pipeline(env), file_folder="nowhere")
        message = _look(env, row)
        assert message == f"Cannot check the folder: {SHARE}\\outbound\\nowhere was not found."
        assert env.state["schedule_store"].get(row["id"]).status_message == message


def test_check_the_folder_now_lists_the_share_files(env):
    _drop(env.fake, "CUSTMAST.DAT", b"12345", at=T0)
    _drop(env.fake, "ORDERS.DAT", b"1", at=T0 + timedelta(hours=1))
    _drop(env.fake, "readme.txt", at=T0)
    preview = preview_folder(env.state, "default", {
        "file_location": "smb", "file_connection_id": "share1", "file_folder": "daily", "file_pattern": "*.dat",
    })
    assert (preview["total"], preview["matching"]) == (3, 2)
    assert [(f["name"], f["size"], f["path"], f["modified"]) for f in preview["files"]] == [
        ("ORDERS.DAT", 1, f"{DAILY}\\ORDERS.DAT", (T0 + timedelta(hours=1)).isoformat()),
        ("CUSTMAST.DAT", 5, f"{DAILY}\\CUSTMAST.DAT", T0.isoformat()),
    ]
    assert env.scheduler.started == [] and env.files.known_versions("any") == set()


# ── End to end through the API and the real scheduler ────────────────────

from tests.conftest_fixtures_v2 import (  # noqa: E402,F401
    admin_token, app_v2, authed_client, client, data_dir, db_fixture,
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


def _wait_for_files_done(trigger_id: str, count: int, timeout: float = 60) -> list[dict]:
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
    def test_a_file_landing_on_the_share_runs_the_published_pipeline_on_it(self, authed_client, fake):
        from revoluzen_flow.main import app_state

        saved = authed_client.post("/api/connections", json={
            "name": "Mainframe share", "type": "smb",
            "config": {"host": "fileserver01", "share": "mainframe", "base_folder": "outbound",
                       "domain": "CORP", "username": "svc_flow", "password": PASSWORD},
        })
        assert saved.status_code in (200, 201), saved.text
        assert PASSWORD not in saved.text
        connection_id = saved.json()["id"]

        now = datetime.now(timezone.utc)
        _drop(fake, "orders_old.csv", b"id,amount\n1,500\n", at=now - timedelta(days=1))

        r = authed_client.post("/api/workflows", json={
            "name": "Load orders from the share",
            "steps": [
                {"id": "read", "type": "source", "label": "Read the file",
                 "params": {"connector_type": "file", "connection_id": connection_id,
                            "file_path": "${param.file_path}"}},
                {"id": "big", "type": "filter", "label": "Big orders", "params": {"condition": "amount >= 100"}},
            ],
            "connections": [{"from_step": "read", "to_step": "big"}],
            "parameters": [{"name": "file_path", "type": "string", "required": True}],
        })
        assert r.status_code in (200, 201), r.text
        wid = r.json().get("id") or r.json()["workflow"]["id"]
        app_state["store"].update_status(wid, PipelineStatus.PUBLISHED)

        body = {
            "workflow_id": wid, "schedule_type": "file_arrival", "file_location": "smb",
            "file_connection_id": connection_id, "file_folder": "daily", "file_pattern": "ORDERS_*.CSV",
            "file_parameter": "file_path", "file_settle_seconds": 0,
        }
        created = authed_client.post("/api/schedules", json=body)
        assert created.status_code == 200, created.text
        row = created.json()
        assert row["file_location"] == "smb"

        preview = authed_client.post("/api/schedules/check-folder", json=body)
        assert preview.status_code == 200, preview.text
        assert [(f["name"], f["path"]) for f in preview.json()["files"]] == [
            ("orders_old.csv", f"{DAILY}\\orders_old.csv"),
        ]

        watcher = app_state["scheduler"].file_watcher
        watcher.check(row)  # notes the old file
        _drop(fake, "orders_new.csv", b"id,amount\n1,150\n2,90\n3,400\n", at=datetime.now(timezone.utc))
        watcher.check(row)  # first sight
        message = watcher.check(row)  # unchanged and closed: it runs
        assert message.startswith("Started a run for orders_new.csv"), message

        done = _wait_for_files_done(row["id"], 1)
        run = app_state["execution_store"].get(done[0]["execution_id"])
        assert run.status == "success", run.error_message
        assert run.triggered_by == "file_arrival"
        assert run.metadata["file"]["path"] == f"{DAILY}\\orders_new.csv"
        assert run.metadata["parameter_values"]["file_path"] == f"{DAILY}\\orders_new.csv", "a replay reads the same file"
        big = next(s for s in run.step_logs if s.step_id == "big")
        assert big.rows_processed == 2, "the run read the new file from the share, which has two big orders"

        for _ in range(3):
            watcher.check(row)
        time.sleep(0.5)
        runs = [r for r in app_state["execution_store"].list_by_workflow(wid) if r.get("triggered_by") == "file_arrival"]
        assert len(runs) == 1, "each file runs once, and the old one never"
        assert fake.sign_ins and all(s["username"] == "CORP\\svc_flow" for s in fake.sign_ins)

    def test_a_share_connection_is_needed(self, authed_client):
        from revoluzen_flow.main import app_state

        r = authed_client.post("/api/workflows", json={"name": "No share", "steps": [
            {"id": "s", "type": "source", "label": "S", "params": {"connector_type": "csv", "file_path": "orders.csv"}},
        ]})
        wid = r.json().get("id") or r.json()["workflow"]["id"]
        app_state["store"].update_status(wid, PipelineStatus.PUBLISHED)
        r = authed_client.post("/api/schedules", json={
            "workflow_id": wid, "schedule_type": "file_arrival", "file_location": "smb",
            "file_connection_id": "no-such-connection", "file_folder": "daily",
        })
        assert r.status_code == 400 and "not found" in r.text
