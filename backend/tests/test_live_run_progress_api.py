"""Live run progress on the editor's socket.

The editor keeps a WebSocket on /ws/execution/{pipeline}. Since 2026-10-25 a
run started by the Run button (POST /api/execute/workflow/{id}) or by a
published endpoint reports each step to every socket on that pipeline
(api/execution.py _canvas_progress_sink). A socket for a pipeline outside its
workspace is refused, since it would hear that pipeline's runs: step ids and
types, row counts, error text.
"""

from __future__ import annotations

import json
import os
import uuid

import anyio
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from tests.conftest_fixtures_v2 import (  # noqa: F401
    data_dir, db_fixture, app_v2, client, admin_token, authed_client,
    _ensure_user, _login,
)


def _receive(ws, timeout: float = 30.0) -> dict:
    """The socket's next message; fails after ``timeout`` instead of hanging."""
    async def next_message():
        with anyio.fail_after(timeout):
            return await ws._send_rx.receive()

    message = ws.portal.call(next_message)
    ws._raise_on_close(message)
    return json.loads(message["text"])


def _until_completed(ws) -> list[dict]:
    events: list[dict] = []
    while True:
        event = _receive(ws)
        events.append(event)
        if event.get("type") == "workflow_completed":
            return events


def _url(pipeline_id: str, token: str, workspace: str = "default") -> str:
    return f"/ws/execution/{pipeline_id}?token={token}&workspace_id={workspace}"


def _pipeline(http, token: str, name: str, folder: str, workspace: str = "default") -> str:
    """A saved pipeline that reads the seeded orders.csv into a managed table."""
    created = http.post(
        "/api/workflows",
        headers={"Authorization": f"Bearer {token}", "X-Workspace-Id": workspace},
        json={
            "name": name,
            "steps": [
                {"id": "orders", "type": "csv_source", "label": "Orders",
                 "params": {"file_path": os.path.join(folder, "orders.csv")},
                 "position": {"x": 0, "y": 0}},
                {"id": "save", "type": "local_table_sink", "label": "Save",
                 "params": {"schema_name": "default", "table_name": name.replace("-", "_"),
                            "mode": "replace"},
                 "position": {"x": 1, "y": 0}},
            ],
            "connections": [{"from_step": "orders", "to_step": "save"}],
        },
    )
    assert created.status_code in (200, 201), created.text
    return created.json()["id"]


@pytest.fixture(scope="module")
def outsider(app_v2, authed_client) -> tuple[str, str]:
    """A non-admin member of one other workspace: (its id, a sign-in token).

    An admin may open any workspace, so only a non-admin shows the boundary
    a signed-in user meets.
    """
    from revoluzen_flow.main import app_state
    from revoluzen_flow.workspaces.models import Workspace

    run = uuid.uuid4().hex[:8]
    email = f"outsider-{run}@tenant.test"
    workspaces = app_state["workspace_store"]
    ws = workspaces.create(Workspace(name=f"Elsewhere {run}", slug=f"elsewhere-{run}"))
    assert _ensure_user(authed_client, email, "Password123!", "data_engineer")
    user = app_state["user_store"].get_user_by_email(email)
    assert user is not None and user.role == "data_engineer"
    workspaces.add_member(ws.id, user.id, role="developer")

    login = TestClient(app_v2, base_url="http://localhost")
    try:
        token = _login(login, email, "Password123!")
    finally:
        login.close()
    assert token, f"could not sign in as {email}"
    return ws.id, token


def test_the_run_button_reports_each_step_to_the_editor(client, admin_token, data_dir):
    pipeline_id = _pipeline(client, admin_token, "progress-run-button", data_dir)
    # The socket and the run share the client's event loop, as they share the
    # server's: the run hands its progress to the loop that took the request.
    with client.websocket_connect(_url(pipeline_id, admin_token)) as ws:
        ws.send_json({"action": "ping"})  # answered once the socket is registered
        assert _receive(ws) == {"type": "pong", "workflow_id": pipeline_id}
        response = client.post(
            f"/api/execute/workflow/{pipeline_id}?full_run=true",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "success", response.text
        events = _until_completed(ws)
    assert [(e["type"], e.get("step_id")) for e in events] == [
        ("workflow_started", None),
        ("step_started", "orders"), ("step_completed", "orders"),
        ("step_started", "save"), ("step_completed", "save"),
        ("workflow_completed", None),
    ]
    assert {e["workflow_id"] for e in events} == {pipeline_id}
    assert events[-1]["status"] == "success"


def test_a_pipeline_outside_the_sockets_workspace_is_refused(client, admin_token, data_dir, outsider):
    pipeline_id = _pipeline(client, admin_token, "progress-other-workspace", data_dir)
    other_workspace, outsider_token = outsider
    for url in (
        # A member of another workspace who knows the pipeline id.
        _url(pipeline_id, outsider_token, workspace=other_workspace),
        # The admin may open another workspace, where this pipeline does not exist.
        _url(pipeline_id, admin_token, workspace="ws-elsewhere"),
        _url("no-such-pipeline", admin_token),
    ):
        with client.websocket_connect(url) as ws:
            assert _receive(ws) == {"type": "error", "error": "Workflow not found"}
            with pytest.raises(WebSocketDisconnect) as closed:
                _receive(ws)
            assert closed.value.code == 1008


def test_a_member_follows_a_pipeline_of_their_own_workspace(client, data_dir, outsider):
    other_workspace, outsider_token = outsider
    pipeline_id = _pipeline(client, outsider_token, "progress-own-workspace", data_dir,
                            workspace=other_workspace)
    with client.websocket_connect(_url(pipeline_id, outsider_token, workspace=other_workspace)) as ws:
        ws.send_json({"action": "ping"})
        assert _receive(ws) == {"type": "pong", "workflow_id": pipeline_id}


def test_the_socket_takes_action_messages(client, admin_token, data_dir):
    pipeline_id = _pipeline(client, admin_token, "progress-actions", data_dir)
    with client.websocket_connect(_url(pipeline_id, admin_token)) as ws:
        ws.send_json({"action": "ping"})
        assert _receive(ws) == {"type": "pong", "workflow_id": pipeline_id}
        # What the editor used to send.
        ws.send_json({"command": "ping"})
        answer = _receive(ws)
        assert answer["type"] == "error" and answer["error"] == "Unknown action: "
