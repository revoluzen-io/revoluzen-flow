"""Regression tests for the 2026-11-13 penetration test.

Each test pins one verified finding (F-numbers refer to the sprint report) so
the hole cannot silently reopen. They run against the shared v2 app and
per-role clients; server-mode cases flip runtime_config for the test only.
"""

from __future__ import annotations

import io
import os
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from tests.conftest_fixtures_v2 import (  # noqa: F401
    data_dir, db_fixture, app_v2, client, admin_token, authed_client, role_clients,
    assert_allowed, assert_forbidden,
)

from revoluzen_flow import runtime_config
from revoluzen_flow.connections.inline_secrets import SECRET_PLACEHOLDER

REPO = Path(__file__).resolve().parents[2]


def _role(role_clients, name):
    c = role_clients.get(name)
    if c is None:
        pytest.skip(f"{name} client not provisioned")
    return c


@pytest.fixture
def server_mode(monkeypatch):
    monkeypatch.setattr(runtime_config, "IS_SERVER_MODE", True)
    monkeypatch.setattr(runtime_config, "IS_LOCAL_MODE", False)
    monkeypatch.setattr(runtime_config, "SECURITY_MODE", "server")


@pytest.fixture
def anon(app_v2, authed_client):
    # No context manager: the module's app is already started by the shared
    # fixtures, and re-entering the lifespan would re-initialise app state.
    c = TestClient(app_v2, base_url="http://localhost")
    yield c
    c.close()


def _workflow(authed_client) -> str:
    r = authed_client.post("/api/workflows/", json={
        "name": f"sec-sprint {uuid.uuid4().hex[:6]}", "steps": [], "connections": [],
    })
    assert r.status_code == 200, r.text
    return r.json()["id"]


_EPHEMERAL = {
    "workflow": {"id": "wf_sec", "name": "sec", "steps": [], "connections": [], "parameters": []},
    "preview_limit": 5, "full_run": False, "safety_mode": "validate_only",
    "environment": "dev", "parameter_values": {},
}


# ── F4: running pipelines is an authoring capability ──────────────────────

class TestRunningNeedsAuthorRank:
    @pytest.mark.parametrize("role", ["viewer", "analyst"])
    def test_low_roles_cannot_run_ephemeral_pipelines(self, role_clients, role):
        r = _role(role_clients, role).post("/api/execute/workflow/ephemeral", json=_EPHEMERAL)
        assert r.status_code == 403, r.text

    def test_author_can_still_run_ephemeral_pipelines(self, role_clients):
        assert_allowed(_role(role_clients, "data_engineer").post(
            "/api/execute/workflow/ephemeral", json=_EPHEMERAL))

    def test_viewer_cannot_run_test_or_cancel_a_saved_pipeline(self, authed_client, role_clients):
        wid = _workflow(authed_client)
        viewer = _role(role_clients, "viewer")
        assert viewer.post(f"/api/execute/workflow/{wid}").status_code == 403
        assert viewer.post(f"/api/workflows/{wid}/test").status_code == 403
        assert viewer.post(f"/api/workflows/{wid}/cancel").status_code == 403


# ── F9: approvals need rank and record the real approver ──────────────────

class TestApprovals:
    def test_viewer_and_author_cannot_approve(self, authed_client, role_clients):
        wid = _workflow(authed_client)
        for role in ("viewer", "data_engineer"):
            r = _role(role_clients, role).post(f"/api/workflows/{wid}/approve?approved_by=cfo%40corp.test")
            assert r.status_code == 403, (role, r.text)

    def test_only_admins_manage_approval_gates(self, role_clients):
        r = _role(role_clients, "data_engineer").post(
            "/api/workflows/approval-gates", json={"scope": "global", "enabled": False})
        assert r.status_code == 403, r.text

    def test_actor_comes_from_the_session(self):
        from revoluzen_flow.api.workflows import _session_actor

        class U:
            email = "real@corp.test"
            id = "u1"
        assert _session_actor(U()) == "real@corp.test"

    def test_approver_is_the_session_user_and_cannot_be_the_submitter(self, authed_client, monkeypatch):
        """End to end through the real handlers: a forged approved_by is
        ignored, and the person who submitted cannot approve."""
        from revoluzen_flow.state import get_workflow_store
        wid = _workflow(authed_client)
        store = get_workflow_store()
        v = store.get(wid)
        wf = v.workflow
        wf.test_results = {"status": "success"}
        store.save(wf, change_summary="sprint test", created_by="t", kind="review")

        assert authed_client.post(f"/api/workflows/{wid}/submit-for-review?submitted_by=someone").status_code == 200
        me = authed_client.post(f"/api/workflows/{wid}/approve?approved_by=cfo%40corp.test")
        assert me.status_code == 403, me.text  # same person submitted

        wf = store.get(wid).workflow
        wf.submitted_by = "colleague@corp.test"
        store.save(wf, change_summary="sprint test", created_by="t", kind="review")
        ok = authed_client.post(f"/api/workflows/{wid}/approve?approved_by=cfo%40corp.test")
        assert ok.status_code == 200, ok.text
        assert ok.json()["approved_by"] != "cfo@corp.test"


# ── F2 / F3 / F5: notification config and inbox ───────────────────────────

class TestNotifications:
    def test_anonymous_cannot_read_config_in_server_mode(self, anon, server_mode):
        assert anon.get("/api/notifications/config").status_code == 401

    def test_config_never_returns_the_smtp_password(self, authed_client):
        pw = "sprint-smtp-" + uuid.uuid4().hex
        r = authed_client.put("/api/notifications/config", json={
            "smtp": {"host": "127.0.0.2", "port": 2526, "password": pw, "tls": False},
            "slack_webhook": "https://hooks.example/T/B/secret",
        })
        assert r.status_code == 200, r.text
        assert pw not in r.text and "hooks.example" not in r.text
        got = authed_client.get("/api/notifications/config").json()
        assert got["smtp"]["password"] == SECRET_PLACEHOLDER
        assert got["slack_webhook"] == SECRET_PLACEHOLDER

        # Sending the placeholder back keeps the stored value.
        authed_client.put("/api/notifications/config", json={
            "smtp": {**got["smtp"], "port": 2527}, "slack_webhook": SECRET_PLACEHOLDER,
        })
        from revoluzen_flow.alerts.notifier import NotificationService
        assert NotificationService._load_smtp_config()["password"] == pw

    @pytest.mark.parametrize("role", ["viewer", "data_engineer"])
    def test_non_admins_cannot_change_smtp(self, role_clients, role):
        r = _role(role_clients, role).put("/api/notifications/config",
                                          json={"smtp": {"host": "attacker.test"}})
        assert r.status_code == 403, r.text

    def test_inbox_has_no_anonymous_fallback_in_server_mode(self, anon, server_mode):
        assert anon.get("/api/notifications/").status_code == 401
        assert anon.delete("/api/notifications/").status_code == 401

    def test_execution_history_needs_identity_in_server_mode(self, anon, server_mode):
        assert anon.get("/api/executions/").status_code == 401

    def test_system_internals_need_identity_in_server_mode(self, anon, server_mode):
        for path in ("/api/system/metrics", "/api/system/resource-alerts",
                     "/api/system/update-readiness", "/api/scheduler/status",
                     "/api/files", "/api/health/memory", "/api/metrics/ai",
                     "/api/ws/connections"):
            assert anon.get(path).status_code == 401, path


# ── F7: secret variables ──────────────────────────────────────────────────

class TestSecretVariables:
    def test_secret_value_is_masked_and_resolve_needs_author(self, authed_client, role_clients):
        key = "SPRINT_" + uuid.uuid4().hex[:6].upper()
        secret = "sprint-var-" + uuid.uuid4().hex
        r = authed_client.post("/api/variables/", json={"key": key, "value": secret, "type": "secret"})
        assert r.status_code == 200, r.text
        vid = r.json()["id"]
        assert secret not in r.text

        viewer = _role(role_clients, "viewer")
        got = viewer.get(f"/api/variables/{vid}")
        if got.status_code == 200:
            assert got.json()["value"] == SECRET_PLACEHOLDER
        assert viewer.post(f"/api/variables/resolve?key={key}").status_code == 403
        res = authed_client.post(f"/api/variables/resolve?key={key}")
        assert secret not in res.text

        # Editing other fields with the placeholder does not wipe the value.
        authed_client.put(f"/api/variables/{vid}", json={"value": SECRET_PLACEHOLDER, "description": "x"})
        from revoluzen_flow.main import app_state
        assert app_state["variable_store"].get(vid).value == secret


# ── F6 / F12: connections ─────────────────────────────────────────────────

class TestConnections:
    def test_viewer_cannot_test_or_create_connections(self, role_clients):
        viewer = _role(role_clients, "viewer")
        assert viewer.post("/api/connections/test-inline",
                           json={"type": "postgresql", "config": {"host": "127.0.0.1"}}).status_code == 403
        assert viewer.post("/api/connections/", json={
            "name": "x", "type": "postgresql", "config": {"host": "h"}}).status_code == 403

    def test_viewer_cannot_repoint_a_stored_connection(self, authed_client, role_clients):
        r = authed_client.post("/api/connections/", json={
            "name": f"sprint-{uuid.uuid4().hex[:6]}", "type": "postgresql",
            "config": {"host": "10.1.2.3", "port": 5432, "database": "d", "user": "u",
                       "password": "sprint-db-password"},
        })
        assert r.status_code == 200, r.text
        cid = r.json()["id"]
        viewer = _role(role_clients, "viewer")
        moved = viewer.put(f"/api/connections/{cid}", json={"config": {
            "host": "attacker.test", "port": 5432, "database": "d", "user": "u",
            "password": SECRET_PLACEHOLDER}})
        assert moved.status_code == 403, moved.text
        assert viewer.post(f"/api/connections/{cid}/test").status_code == 403


# ── F8: /api/upload?replaces= ─────────────────────────────────────────────

class TestUploadReplaces:
    def _upload(self, c, replaces):
        return c.post(f"/api/upload?replaces={replaces}",
                      files={"file": ("sprint.csv", io.BytesIO(b"a\n1\n"), "text/csv")})

    def test_replaces_cannot_reach_internal_files(self, authed_client, data_dir):
        from revoluzen_flow.main import app_state
        root = app_state["data_dir"]
        Path(root, "keep-me.db").write_text("x")
        targets = ["keep-me.db"]
        # The app's own master key, if it lives here: never overwrite it.
        if Path(root, "secret.key").exists():
            targets.append("secret.key")
        before = {n: Path(root, n).read_bytes() for n in targets}
        for name in targets:
            r = self._upload(authed_client, name)
            assert r.status_code == 200, r.text
            assert r.json()["replaced_previous"] is False
            assert Path(root, name).read_bytes() == before[name], name
        os.makedirs(Path(root, "sub"), exist_ok=True)
        Path(root, "sub", "a.csv").write_text("x")
        self._upload(authed_client, "sub/a.csv")
        assert Path(root, "sub", "a.csv").exists()

    def test_viewer_cannot_upload(self, role_clients):
        assert self._upload(_role(role_clients, "viewer"), "x.csv").status_code == 403


# ── F17 / F18 / F20: accounts and workspaces ──────────────────────────────

class TestAccounts:
    def test_workspace_admin_cannot_address_a_foreign_workspace(self, authed_client, role_clients):
        ws = authed_client.post("/api/workspaces/", json={"name": f"Sprint {uuid.uuid4().hex[:5]}"})
        assert ws.status_code == 200, ws.text
        r = _role(role_clients, "workspace_admin").get(
            "/api/workflows/", headers={"X-Workspace-Id": ws.json()["id"]})
        assert r.status_code == 403, r.text

    def test_admin_cannot_promote_itself_or_mint_super_admins(self, authed_client, app_v2):
        email = f"sprint-admin-{uuid.uuid4().hex[:6]}@revoluzen.local"
        inv = authed_client.post("/api/auth/invite", json={"email": email, "role": "admin"})
        assert inv.status_code == 200, inv.text
        admin = TestClient(app_v2, base_url="http://localhost")
        tok = admin.post("/api/auth/login", json={"email": email, "password": inv.json()["temp_password"]}).json()["token"]
        admin.headers["Authorization"] = f"Bearer {tok}"

        uid = inv.json()["user_id"]
        assert admin.put(f"/api/auth/users/{uid}", json={"role": "super_admin"}).status_code == 403
        assert admin.put(f"/api/auth/users/{uid}", json={"id": "admin"}).status_code == 400
        assert admin.post("/api/auth/invite", json={
            "email": f"x-{uuid.uuid4().hex[:6]}@revoluzen.local", "role": "super_admin"}).status_code == 403

    def test_register_does_not_reveal_which_emails_exist(self, anon, authed_client):
        me = authed_client.get("/api/auth/me").json()
        email = me.get("email") or me.get("user", {}).get("email")
        known = anon.post("/api/auth/register", json={"email": email, "password": "x"})
        unknown = anon.post("/api/auth/register", json={
            "email": f"nobody-{uuid.uuid4().hex[:6]}@revoluzen.local", "password": "x"})
        assert known.status_code == unknown.status_code == 403


# ── F19: Copilot tool tiers cover every real role ─────────────────────────

def test_copilot_rbac_has_every_role():
    from revoluzen_flow.ai import rbac
    from revoluzen_flow.auth.deps import _ROLE_RANK
    for role in _ROLE_RANK:
        assert role in rbac._RBAC_MATRIX, role
    assert rbac.allowed_tiers_for("analyst", "dev") == rbac.allowed_tiers_for("viewer", "dev")
    assert rbac.allowed_tiers_for("data_engineer", "prod") == rbac.allowed_tiers_for("developer", "prod")


# ── F10 / F1: origin guard and local mode on the network ──────────────────

def test_origin_null_cannot_post_in_loopback_mode(authed_client):
    r = authed_client.post("/api/templates/import", headers={"Origin": "null"},
                           json={"name": "x", "steps": [], "connections": []})
    assert r.status_code == 403
    assert r.json().get("error") == "cross_origin_blocked"


@pytest.mark.parametrize("server, network", [
    (("127.0.0.1", 8001), False), (("::1", 8001), False), (("::ffff:127.0.0.1", 8001), False),
    (("10.0.0.5", 8001), True), (("172.17.0.2", 8001), True), (("testserver", 80), False), (None, False),
])
def test_network_arrival_is_detected_from_the_socket(server, network):
    from revoluzen_flow.api.local_hardening import _arrived_on_network_address
    scope = {"type": "http", "method": "GET", "path": "/", "headers": [], "query_string": b""}
    if server is not None:
        scope["server"] = server
    assert _arrived_on_network_address(Request(scope)) is network


def test_docker_defaults_require_sign_in():
    dockerfile = (REPO / "Dockerfile").read_text(encoding="utf-8")
    compose = (REPO / "docker-compose.yml").read_text(encoding="utf-8")
    assert "REVOLUZEN_FLOW_SECURITY_MODE=server" in dockerfile
    assert "REVOLUZEN_FLOW_SECURITY_MODE: server" in compose


# ── F11: reset links never trust the request's origin ─────────────────────

def test_reset_link_ignores_the_origin_header(monkeypatch):
    from revoluzen_flow.api.auth import _request_origin
    monkeypatch.delenv("REVOLUZEN_FLOW_PUBLIC_URL", raising=False)
    scope = {"type": "http", "method": "POST", "path": "/api/auth/forgot-password",
             "headers": [(b"origin", b"https://attacker.example"), (b"host", b"localhost:8001")],
             "query_string": b"", "scheme": "http", "server": ("127.0.0.1", 8001)}
    req = Request(scope)
    monkeypatch.setattr(runtime_config, "IS_SERVER_MODE", False)
    assert "attacker.example" not in _request_origin(req)
    monkeypatch.setattr(runtime_config, "IS_SERVER_MODE", True)
    assert _request_origin(req) == ""
    monkeypatch.setenv("REVOLUZEN_FLOW_PUBLIC_URL", "https://flow.corp.test/")
    assert _request_origin(req) == "https://flow.corp.test"
