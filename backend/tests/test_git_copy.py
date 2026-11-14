"""Each publish is copied to the customer's Git repository, and no secret goes with it.

These never run the real ``git``. A repository stands in for it, keeping each
commit's files, because what matters here is what would land there: which files,
who the commit says did it, and that no password or token is anywhere in it.
The commands themselves are checked against a recording of ``subprocess.run``.

Do not point these at a real repository on disk. Git for Windows reaches a local
path through ``sh -c``, and one hung fetch here grew to thousands of bash.exe.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
import uuid

import pytest

from revoluzen_flow.gitcopy import client as git_client
from revoluzen_flow.gitcopy import files as git_files
from revoluzen_flow.gitcopy import service
from revoluzen_flow.gitcopy.client import GitFailed, GitRepo, WillNotRetry, commit_page, default_username, explain
from revoluzen_flow.gitcopy.settings import (
    GitSettings,
    GitSettingsError,
    check_branch,
    check_folder,
    check_repo_url,
)
from revoluzen_flow.ir.schema import PipelineStatus
from revoluzen_flow.ir.versioning import definition_hash
from tests.conftest_fixtures_v2 import (  # noqa: F401
    admin_token, app_v2, authed_client, client, data_dir, db_fixture, role_clients,
)

pytestmark = pytest.mark.usefixtures("db_fixture")

REPO = "https://git.example.com/acme/flow-pipelines.git"
BROKEN = "https://git.example.com/acme/broken.git"
STEP_SECRET = "hunter2-step-password-7Qz"
HEADER_SECRET = "Bearer header-token-9Xy"
URL_SECRET = "url-password-4Kd"
CONNECTION_SECRET = "connection-password-2Fg"
CREDENTIAL_SECRET = "credential-password-8Lm"
ACCESS_TOKEN = "ghp_access-token-for-the-repository-1Ab"


class FakeServer:
    """What the Git server holds: every commit, oldest first."""

    def __init__(self):
        self.commits: list[dict] = []

    @property
    def files(self) -> dict[str, str]:
        return self.commits[-1]["files"] if self.commits else {}


class FakeRepo:
    """Stands in for GitRepo, on the same terms: start from what the server
    has, let the caller write, commit only if something changed."""

    server = FakeServer()

    def __init__(self, directory, settings):
        self.settings = settings

    def check_access(self) -> bool:
        if self.settings.repo_url == BROKEN:
            raise GitFailed("The Git server refused the access token.")
        return self.settings.branch == "main"

    def save(self, write_files, message, author):
        if self.settings.repo_url == BROKEN:
            raise GitFailed("The Git server refused the access token.")
        with tempfile.TemporaryDirectory() as work:
            for path, text in self.server.files.items():
                target = os.path.join(work, *path.split("/"))
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with open(target, "w", encoding="utf-8", newline="\n") as handle:
                    handle.write(text)
            write_files(work)
            after: dict[str, str] = {}
            for current, _dirs, names in os.walk(work):
                for name in names:
                    full = os.path.join(current, name)
                    with open(full, encoding="utf-8", newline="") as handle:
                        after[os.path.relpath(full, work).replace(os.sep, "/")] = handle.read()
        if after == self.server.files:
            return None
        sha = uuid.uuid4().hex
        self.server.commits.append({"sha": sha, "files": after, "message": message, "author": author})
        return sha


@pytest.fixture()
def server(monkeypatch) -> FakeServer:
    fake = FakeServer()
    monkeypatch.setattr(FakeRepo, "server", fake)
    monkeypatch.setattr(service, "GitRepo", FakeRepo)
    return fake


@pytest.fixture()
def connected(authed_client, server):
    r = authed_client.put("/api/git-copy/settings", json={
        "enabled": True, "repo_url": REPO, "branch": "main", "folder": "flow-pipelines", "token": ACCESS_TOKEN,
    })
    assert r.status_code == 200, r.text
    yield server
    authed_client.put("/api/git-copy/settings", json={"enabled": False, "repo_url": "", "clear_token": True})


def _store():
    from revoluzen_flow.state import get_workflow_store
    return get_workflow_store()


def _make(client, name: str, **extra) -> str:
    body = {
        "name": name,
        "business_purpose": "Load orders for finance",
        "steps": [
            {"id": "src", "type": "csv_source", "label": "Read orders", "params": {"file_path": "orders.csv"}},
        ],
        **extra,
    }
    r = client.post("/api/workflows", json=body)
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


def _publish(client, wid: str, note: str = "") -> dict:
    store = _store()
    store.update_status(
        wid, PipelineStatus.TESTING,
        test_results={"status": "success", "definition_hash": definition_hash(store.get(wid).workflow)},
    )
    r = client.post(f"/api/workflows/{wid}/publish", json={"note": note})
    assert r.status_code == 200, r.text
    return r.json()


def _finished(client, wid: str, after: str | None = None) -> dict:
    """Wait for the background copy started by the last action."""
    deadline = time.time() + 20
    latest = None
    while time.time() < deadline:
        latest = client.get(f"/api/git-copy/pipelines/{wid}").json()["latest"]
        if latest and latest["id"] != after and latest["status"] not in ("pending", "working"):
            return latest
        time.sleep(0.05)
    raise AssertionError(f"the copy did not finish: {latest}")


def _folder_of(server: FakeServer, pipeline_id: str) -> str:
    return next(path.rsplit("/", 1)[0] for path in server.files if f"--{pipeline_id}/" in path)


class TestWhatLandsInTheRepository:
    def test_a_publish_makes_a_commit_saying_who_and_why(self, authed_client, connected):
        wid = _make(authed_client, f"Daily Sales {uuid.uuid4().hex[:5]}")

        result = _publish(authed_client, wid, note="First go-live for finance")
        latest = _finished(authed_client, wid)

        assert result["git"]["status"] == "queued"
        assert latest["status"] == "saved" and latest["commit_sha"]
        folder = _folder_of(connected, wid)
        assert folder.startswith("flow-pipelines/")
        assert {f"{folder}/pipeline.json", f"{folder}/depends-on.json", f"{folder}/README.md"} <= set(connected.files)
        commit = connected.commits[-1]
        assert commit["message"].startswith('Publish "Daily Sales')
        assert "First go-live for finance" in commit["message"]
        assert commit["author"][1] == "admin@revoluzen.local"
        readme = connected.files[f"{folder}/README.md"]
        assert "First go-live for finance" in readme and "Load orders for finance" in readme

    def test_the_file_is_the_published_version_not_a_draft(self, authed_client, connected):
        wid = _make(authed_client, f"Draft Stays Out {uuid.uuid4().hex[:5]}")
        _publish(authed_client, wid)
        first = _finished(authed_client, wid)
        wf = authed_client.get(f"/api/workflows/{wid}").json()["workflow"]
        wf["steps"][0]["params"]["file_path"] = "draft-only.csv"
        assert authed_client.put(f"/api/workflows/{wid}", json={"workflow": wf}).status_code == 200

        authed_client.post("/api/git-copy/save-all")
        _finished(authed_client, wid, after=first["id"])

        text = connected.files[f"{_folder_of(connected, wid)}/pipeline.json"]
        assert "orders.csv" in text and "draft-only.csv" not in text

    def test_it_can_be_imported_again(self, authed_client, connected):
        wid = _make(authed_client, f"Round Trip {uuid.uuid4().hex[:5]}")
        _publish(authed_client, wid)
        _finished(authed_client, wid)
        payload = json.loads(connected.files[f"{_folder_of(connected, wid)}/pipeline.json"])

        imported = authed_client.post("/api/workflows/import", json={
            "pipeline": payload["pipeline"], "rename": f"Restored {uuid.uuid4().hex[:5]}",
        })

        assert imported.status_code == 200, imported.text
        restored = authed_client.get(f"/api/workflows/{imported.json()['id']}").json()["workflow"]
        assert restored["status"] == "draft" and restored["steps"][0]["params"]["file_path"] == "orders.csv"

    def test_the_same_files_make_no_commit(self, authed_client, connected, monkeypatch):
        class Frozen(git_files.datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 11, 11, 8, 0, tzinfo=tz)

        monkeypatch.setattr(git_files, "datetime", Frozen)
        wid = _make(authed_client, f"No Change {uuid.uuid4().hex[:5]}")
        _publish(authed_client, wid)
        first = _finished(authed_client, wid)
        count = len(connected.commits)

        authed_client.post("/api/workflows/" + wid + "/publish", json={"note": ""})
        repeat = _finished(authed_client, wid, after=first["id"])

        assert repeat["status"] == "unchanged" and len(connected.commits) == count

    def test_a_rename_moves_the_folder(self, authed_client, connected):
        wid = _make(authed_client, f"Before {uuid.uuid4().hex[:5]}")
        _publish(authed_client, wid)
        first = _finished(authed_client, wid)
        before = _folder_of(connected, wid)
        wf = authed_client.get(f"/api/workflows/{wid}").json()["workflow"]
        wf["name"] = f"After {uuid.uuid4().hex[:5]}"
        assert authed_client.put(f"/api/workflows/{wid}", json={"workflow": wf}).status_code == 200
        _publish(authed_client, wid)
        _finished(authed_client, wid, after=first["id"])

        after = _folder_of(connected, wid)
        assert after != before and "after-" in after
        assert not [path for path in connected.files if path.startswith(f"{before}/")]

    def test_unpublishing_is_on_the_record_and_keeps_the_files(self, authed_client, connected):
        wid = _make(authed_client, f"Going Away {uuid.uuid4().hex[:5]}")
        _publish(authed_client, wid)
        first = _finished(authed_client, wid)

        authed_client.post(f"/api/workflows/{wid}/revoke", json={"note": "Replaced"})
        second = _finished(authed_client, wid, after=first["id"])

        assert second["status"] == "saved"
        folder = _folder_of(connected, wid)
        assert "Unpublished" in connected.files[f"{folder}/README.md"]
        assert f"{folder}/pipeline.json" in connected.files
        assert connected.commits[-1]["message"].startswith('Unpublish "Going Away')

    def test_two_pipelines_keep_each_others_files(self, authed_client, connected):
        one = _make(authed_client, f"One {uuid.uuid4().hex[:5]}")
        two = _make(authed_client, f"Two {uuid.uuid4().hex[:5]}")
        _publish(authed_client, one)
        _finished(authed_client, one)
        _publish(authed_client, two)
        _finished(authed_client, two)
        assert _folder_of(connected, one) and _folder_of(connected, two)


class TestNoSecretLeavesFlow:
    def test_nothing_secret_is_in_any_file_or_commit(self, authed_client, connected):
        credential = authed_client.post("/api/credentials", json={
            "name": f"Warehouse login {uuid.uuid4().hex[:5]}", "type": "postgresql",
            "config": {"password": CREDENTIAL_SECRET},
        }).json()
        connection = authed_client.post("/api/connections", json={
            "name": f"Warehouse {uuid.uuid4().hex[:5]}", "type": "postgresql",
            "credential_id": credential["id"],
            "config": {"host": "db.example.com", "port": 5432, "password": CONNECTION_SECRET},
        }).json()
        wid = _make(
            authed_client, f"Has Secrets {uuid.uuid4().hex[:5]}",
            steps=[
                {"id": "db", "type": "db_source", "label": "Read", "params": {
                    "connection_id": connection["id"], "query": "SELECT 1",
                }},
                {"id": "mail", "type": "send_email", "label": "Mail", "params": {
                    "smtp_pass": STEP_SECRET, "to": "ops@example.com",
                }},
                {"id": "call", "type": "http_request", "label": "Call", "params": {
                    "url": f"https://svc:{URL_SECRET}@api.example.com/x",
                    "headers": [{"key": "Authorization", "value": HEADER_SECRET}],
                }},
            ],
            parameters=[{"name": "api_key", "type": "string", "default": "parameter-secret-5Tr"}],
        )

        _publish(authed_client, wid, note="Has secrets in it")
        assert _finished(authed_client, wid)["status"] == "saved"

        folder = _folder_of(connected, wid)
        everything = "\n".join(text for path, text in connected.files.items() if path.startswith(folder))
        everything += connected.commits[-1]["message"]
        for secret in (STEP_SECRET, HEADER_SECRET, URL_SECRET, CONNECTION_SECRET, CREDENTIAL_SECRET, "parameter-secret-5Tr"):
            assert secret not in everything, secret
        assert "ENC:v1:" not in everything
        needs = json.loads(connected.files[f"{folder}/depends-on.json"])
        assert needs["connections"][0]["settings"]["host"] == "db.example.com"
        assert needs["credentials"][0]["name"].startswith("Warehouse login")
        assert "config" not in needs["credentials"][0]

    @pytest.mark.parametrize("text", [
        "x ENC:v1:abcdef y",
        "-----BEGIN RSA PRIVATE KEY-----\nabc",
        "postgres://me:real-password@db/x",
    ])
    def test_the_last_check_refuses_the_whole_copy(self, text):
        with pytest.raises(WillNotRetry):
            git_files.assert_no_secrets({"pipeline.json": text}, "message")

    @pytest.mark.parametrize("text", [
        "postgres://me:${param.pw}@db/x",
        "https://example.com:8443/a@b",
        "a plain sentence with an @ in it",
    ])
    def test_the_last_check_leaves_ordinary_text(self, text):
        git_files.assert_no_secrets({"pipeline.json": text}, "message")


class TestTheAccessToken:
    def test_the_api_never_returns_it_and_a_blank_keeps_it(self, authed_client):
        r = authed_client.put("/api/git-copy/settings", json={
            "enabled": True, "repo_url": REPO, "token": ACCESS_TOKEN,
        })
        assert r.status_code == 200, r.text
        assert ACCESS_TOKEN not in r.text and r.json()["token_set"] is True
        assert ACCESS_TOKEN not in authed_client.get("/api/git-copy/settings").text

        kept = authed_client.put("/api/git-copy/settings", json={
            "enabled": True, "repo_url": REPO, "branch": "release",
        })
        assert kept.status_code == 200 and kept.json()["token_set"] is True
        assert service.current_settings().token == ACCESS_TOKEN

        from revoluzen_flow.state import get_db
        stored = get_db().fetchone("SELECT data FROM settings WHERE id = 'git_copy'")["data"]
        assert ACCESS_TOKEN not in stored and "ENC:v1:" in stored
        authed_client.put("/api/git-copy/settings", json={"enabled": False, "repo_url": "", "clear_token": True})

    def test_it_is_scrubbed_from_what_git_prints(self, tmp_path):
        repo = GitRepo(str(tmp_path), GitSettings(repo_url=REPO, token=ACCESS_TOKEN))
        assert ACCESS_TOKEN not in repo._scrub(f"fatal: could not read {ACCESS_TOKEN}")


class Recorder:
    """Stands in for subprocess.run and answers as git would."""

    def __init__(self, answers: dict[str, list] | None = None):
        self.calls: list[dict] = []
        self.answers = answers or {}

    def __call__(self, args, **kwargs):
        self.calls.append({"args": args, "env": kwargs["env"], "cwd": kwargs.get("cwd")})
        command = args[1]
        queue = self.answers.get(command)
        code, stdout, stderr = queue.pop(0) if queue else (0, "", "")
        return subprocess.CompletedProcess(args, code, stdout=stdout, stderr=stderr)

    def commands(self) -> list[str]:
        return [call["args"][1] for call in self.calls]


class TestTheGitCommands:
    @pytest.fixture()
    def repo(self, tmp_path):
        return GitRepo(str(tmp_path / "work"), GitSettings(repo_url=REPO, token=ACCESS_TOKEN, branch="main"))

    def _save(self, repo, tmp_path):
        return repo.save(lambda work: None, "A message", ("Priya", "priya@example.com"))

    def test_the_token_is_never_on_the_command_line(self, repo, monkeypatch):
        recorder = Recorder()
        monkeypatch.setattr(git_client.subprocess, "run", recorder)
        repo.check_access()
        assert not [a for call in recorder.calls for a in call["args"] if ACCESS_TOKEN in a]
        env = recorder.calls[0]["env"]
        assert "http.extraHeader" in env.values()
        assert ACCESS_TOKEN not in " ".join(env.values())  # only inside the base64 header

    def test_nothing_on_the_machine_can_wait_for_a_person_or_start_a_shell(self, repo, monkeypatch):
        recorder = Recorder()
        monkeypatch.setattr(git_client.subprocess, "run", recorder)
        repo.check_access()
        env = recorder.calls[0]["env"]
        helper = next(k for k, v in env.items() if v == "credential.helper")
        assert env[helper.replace("KEY", "VALUE")] == ""
        assert env["GIT_TERMINAL_PROMPT"] == "0"
        assert env["GIT_ALLOW_PROTOCOL"] == "https", "git reaches file: through a shell"
        redirect = next(k for k, v in env.items() if v == "http.followRedirects")
        assert env[redirect.replace("KEY", "VALUE")] == "false", "a redirect would carry the token"

    def test_a_local_path_cannot_reach_git_at_all(self, tmp_path):
        repo = GitRepo(str(tmp_path), GitSettings(repo_url="file:///srv/x.git"))
        with pytest.raises(WillNotRetry):
            repo.check_access()

    def test_a_normal_save(self, repo, tmp_path, monkeypatch):
        recorder = Recorder({"diff": [(1, "", "")], "rev-parse": [(0, "abc123\n", "")]})
        monkeypatch.setattr(git_client.subprocess, "run", recorder)
        assert self._save(repo, tmp_path) == "abc123"
        assert recorder.commands() == [
            "init", "config", "fetch", "checkout", "clean", "add", "diff", "commit", "rev-parse", "push",
        ]
        commit = next(call for call in recorder.calls if call["args"][1] == "commit")
        assert commit["env"]["GIT_AUTHOR_NAME"] == "Priya" and commit["env"]["GIT_AUTHOR_EMAIL"] == "priya@example.com"
        assert "--no-verify" in commit["args"]

    def test_an_empty_repository_starts_the_branch(self, repo, tmp_path, monkeypatch):
        recorder = Recorder({
            "fetch": [(128, "", "fatal: couldn't find remote ref refs/heads/main")],
            "diff": [(1, "", "")], "rev-parse": [(0, "abc123\n", "")],
        })
        monkeypatch.setattr(git_client.subprocess, "run", recorder)
        assert self._save(repo, tmp_path) == "abc123"
        assert "symbolic-ref" in recorder.commands() and "checkout" not in recorder.commands()

    def test_nothing_changed_means_no_commit(self, repo, tmp_path, monkeypatch):
        recorder = Recorder({"diff": [(0, "", "")]})
        monkeypatch.setattr(git_client.subprocess, "run", recorder)
        assert self._save(repo, tmp_path) is None
        assert "commit" not in recorder.commands() and "push" not in recorder.commands()

    def test_someone_else_pushed_first_so_it_starts_again(self, repo, tmp_path, monkeypatch):
        recorder = Recorder({
            "diff": [(1, "", ""), (1, "", "")],
            "rev-parse": [(0, "first\n", ""), (0, "second\n", "")],
            "push": [(1, "", "! [rejected] HEAD -> main (fetch first)"), (0, "", "")],
        })
        monkeypatch.setattr(git_client.subprocess, "run", recorder)
        assert self._save(repo, tmp_path) == "second"
        assert recorder.commands().count("fetch") == 2

    def test_a_refused_push_says_why_without_the_token(self, repo, tmp_path, monkeypatch):
        recorder = Recorder({
            "diff": [(1, "", "")], "rev-parse": [(0, "abc\n", "")],
            "push": [(128, "", f"fatal: Authentication failed for token {ACCESS_TOKEN}")],
        })
        monkeypatch.setattr(git_client.subprocess, "run", recorder)
        with pytest.raises(GitFailed) as failed:
            self._save(repo, tmp_path)
        assert "access token" in str(failed.value) and ACCESS_TOKEN not in str(failed.value)

    def test_a_server_that_does_not_answer_is_given_up_on(self, repo, monkeypatch):
        def hang(args, **kwargs):
            raise subprocess.TimeoutExpired(args, kwargs["timeout"])

        monkeypatch.setattr(git_client.subprocess, "run", hang)
        with pytest.raises(GitFailed):
            repo.check_access()


class TestWhenItCannotBeSaved:
    def _point_at(self, client, url: str):
        r = client.put("/api/git-copy/settings", json={
            "enabled": True, "repo_url": url, "branch": "main", "folder": "flow-pipelines",
        })
        assert r.status_code == 200, r.text

    def test_publishing_still_works_and_the_reason_is_shown(self, authed_client, connected):
        self._point_at(authed_client, BROKEN)
        wid = _make(authed_client, f"Git Down {uuid.uuid4().hex[:5]}")

        result = _publish(authed_client, wid)
        latest = _finished(authed_client, wid)

        assert result["status"] == "published"
        assert latest["status"] == "failed" and "access token" in latest["error"]
        assert latest["will_retry_at"], "a failure that can pass is tried again"
        assert authed_client.get(f"/api/workflows/{wid}").json()["workflow"]["status"] == "published"

    def test_try_again_works_once_it_is_fixed(self, authed_client, connected):
        self._point_at(authed_client, BROKEN)
        wid = _make(authed_client, f"Fixed Later {uuid.uuid4().hex[:5]}")
        _publish(authed_client, wid)
        assert _finished(authed_client, wid)["status"] == "failed"
        self._point_at(authed_client, REPO)

        assert authed_client.post(f"/api/git-copy/pipelines/{wid}/retry").status_code == 200

        deadline = time.time() + 20
        while time.time() < deadline:
            latest = authed_client.get(f"/api/git-copy/pipelines/{wid}").json()["latest"]
            if latest["status"] not in ("pending", "working", "failed"):
                break
            time.sleep(0.05)
        assert latest["status"] == "saved", latest

    def test_failures_stop_being_retried_eventually(self, authed_client, connected):
        store = service.queue()
        row = store.add(workflow_id="x", version=1, workspace_id="default", event="published")
        for _ in range(9):
            store.db.execute_write("UPDATE pipeline_git_copies SET attempts = attempts + 1 WHERE id = ?", (row["id"],))
        store.fail(row["id"], "still down")
        assert store.get(row["id"])["next_attempt_at"] is None

    def test_when_it_is_off_nothing_is_queued(self, authed_client):
        authed_client.put("/api/git-copy/settings", json={"enabled": False, "repo_url": ""})
        wid = _make(authed_client, f"Off {uuid.uuid4().hex[:5]}")
        assert _publish(authed_client, wid)["git"] == {"status": "off"}
        assert authed_client.get(f"/api/git-copy/pipelines/{wid}").json() == {"enabled": False, "latest": None}

    def test_the_test_button_reports_in_words(self, authed_client, connected, monkeypatch):
        import revoluzen_flow.api.git_copy as api

        monkeypatch.setattr(api, "GitRepo", FakeRepo)
        good = authed_client.post("/api/git-copy/test", json={
            "enabled": True, "repo_url": REPO, "branch": "a-new-branch",
        }).json()
        assert good["ok"] is True and good["branch_exists"] is False and "does not exist yet" in good["message"]
        bad = authed_client.post("/api/git-copy/test", json={"enabled": True, "repo_url": BROKEN, "branch": "main"}).json()
        assert bad["ok"] is False and "access token" in bad["message"]

    @pytest.mark.parametrize("stderr,expected", [
        ("fatal: Authentication failed for 'https://h/x.git/'", "access token"),
        ("fatal: could not read Username for 'https://h': terminal prompts disabled", "access token"),
        ("remote: Repository not found.", "no repository"),
        ("fatal: unable to access: Could not resolve host: h", "Could not find"),
        ("fatal: unable to access: Failed to connect to h port 443", "Could not reach"),
        ("SSL certificate problem: self-signed certificate", "secure connection"),
        ("remote: error: GH006: Protected branch update failed", "protected"),
    ])
    def test_git_errors_become_plain_words(self, stderr, expected):
        assert expected in explain(stderr)


class TestSettingsAreChecked:
    @pytest.mark.parametrize("url", [
        "https://user:pass@github.com/acme/flow.git",
        "ext::sh -c touch%20/tmp/x",
        "--upload-pack=touch",
        "ssh://git@github.com/acme/flow.git",
        "file:///srv/repo.git",
        "C:/repos/flow.git",
        "https://github.com/acme flow.git",
        "https://169.254.169.254/repo.git",
    ])
    def test_bad_addresses_are_refused(self, url):
        with pytest.raises(GitSettingsError):
            check_repo_url(url)

    def test_plain_http_only_on_a_private_network(self):
        assert check_repo_url("http://127.0.0.1:3000/acme/flow.git")
        with pytest.raises(GitSettingsError):
            check_repo_url("http://93.184.216.34/acme/flow.git")

    @pytest.mark.parametrize("branch", ["-x", "a..b", "a b", "x.lock", "a//b", "a/"])
    def test_bad_branches(self, branch):
        with pytest.raises(GitSettingsError):
            check_branch(branch)

    def test_good_branches(self):
        assert check_branch("") == "main" and check_branch("release/flow-1.2") == "release/flow-1.2"

    @pytest.mark.parametrize("folder", ["../up", "a/../b", ".git/hooks", "a;b"])
    def test_bad_folders(self, folder):
        with pytest.raises(GitSettingsError):
            check_folder(folder)

    def test_good_folders(self):
        assert check_folder("") == "" and check_folder("/data/flow pipelines/") == "data/flow pipelines"

    def test_a_token_is_needed(self, authed_client):
        authed_client.put("/api/git-copy/settings", json={"enabled": False, "repo_url": "", "clear_token": True})
        r = authed_client.put("/api/git-copy/settings", json={"enabled": True, "repo_url": REPO})
        assert r.status_code == 400 and "access token" in r.text

    @pytest.mark.parametrize("role", ["viewer", "analyst", "data_engineer"])
    def test_only_admins_set_it_up(self, role_clients, role):
        assert role_clients[role].get("/api/git-copy/settings").status_code == 403
        assert role_clients[role].put("/api/git-copy/settings", json={"enabled": False}).status_code == 403
        assert role_clients[role].post("/api/git-copy/test", json={}).status_code == 403

    def test_the_usual_names_for_a_token(self):
        assert default_username("https://github.com/a/b.git") == "x-access-token"
        assert default_username("https://gitlab.example.com/a/b.git") == "oauth2"
        assert default_username("https://git.acme.local/a/b.git") == "git"

    def test_a_link_to_the_commit_where_there_is_a_known_page(self):
        assert commit_page("https://github.com/a/b.git", "abc") == "https://github.com/a/b/commit/abc"
        assert commit_page("https://gitlab.com/a/b.git", "abc") == "https://gitlab.com/a/b/-/commit/abc"
        assert commit_page("https://git.acme.local/a/b.git", "abc") == ""
