"""A generated connector can use a saved credential, and the API Explorer can talk to its API.

The pipeline stores which credential to use, never the key itself. The test server
closes each connection after replying, as many small APIs do: the API Explorer used
to report that as a failed request.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace

import pytest

from revoluzen_flow.connectors.rest_framework import RestConnectorManifest, _apply_saved_credential

KEY = "synthetic-key"


class _Api(BaseHTTPRequestHandler):
    def do_GET(self):
        ok = self.headers.get("X-API-Key") == KEY
        body = json.dumps({"items": [{"sku": "A"}]} if ok else {"error": "key required"}).encode()
        self.send_response(200 if ok else 401)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def api_url():
    server = HTTPServer(("127.0.0.1", 0), _Api)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}/prices"
    server.shutdown()


def _manifest():
    return RestConnectorManifest.from_dict({
        "id": "partner", "name": "Partner", "base_url": "https://api.example.test", "streams": [],
        "params": [
            {"name": "auth_abc_key", "secret": True},
            {"name": "region", "secret": False},
        ],
    })


def _ctx(config, workspace="default", credential_workspace="default"):
    credential = SimpleNamespace(config=config, workspace_id=credential_workspace)
    store = SimpleNamespace(get_raw=lambda *a, **k: credential, mark_used=lambda *a, **k: None)
    return SimpleNamespace(app_state={"credential_store": store}, _workflow_ref=SimpleNamespace(workspace_id=workspace))


def test_fills_a_generated_key_field_from_the_credential():
    env = {"credential_id": "c1"}
    _apply_saved_credential(_manifest(), env, _ctx({"api_key": KEY}))
    assert env == {"auth_abc_key": KEY}


def test_only_secret_fields_are_filled():
    env = {"credential_id": "c1"}
    _apply_saved_credential(_manifest(), env, _ctx({"api_key": KEY, "region": "EMEA"}))
    assert "region" not in env


def test_a_value_on_the_node_is_left_alone():
    env = {"credential_id": "c1", "auth_abc_key": "typed"}
    _apply_saved_credential(_manifest(), env, _ctx({"api_key": KEY}))
    assert env["auth_abc_key"] == "typed"


def test_without_a_credential_nothing_changes():
    env = {"auth_abc_key": "typed"}
    _apply_saved_credential(_manifest(), env, _ctx({"api_key": KEY}))
    assert env == {"auth_abc_key": "typed"}


def test_refuses_a_credential_from_another_workspace():
    with pytest.raises(ValueError, match="no longer exists"):
        _apply_saved_credential(_manifest(), {"credential_id": "c1"}, _ctx({"api_key": KEY}, credential_workspace="other"))


def test_explorer_reads_a_reply_from_a_server_that_closes_the_connection(api_url, monkeypatch):
    monkeypatch.setenv("REVOLUZEN_FLOW_API_SOURCE_ALLOW_PRIVATE", "1")
    from revoluzen_flow.api.api_explorer import InspectRequest, inspect_request

    refused = inspect_request(InspectRequest(url=api_url, method="GET", auth_type="none"))
    assert refused["status"] == 401
    allowed = inspect_request(InspectRequest(url=api_url, method="GET", auth_type="api_key", token=KEY, key_name="X-API-Key"))
    assert allowed["status"] == 200
    assert allowed["data"] == {"items": [{"sku": "A"}]}
