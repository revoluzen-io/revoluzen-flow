"""Signed calls to a published endpoint (Standard Webhooks format).

The signature check is tested on its own first, including the example the open
specification publishes, so a sender using one of its libraries is known to
agree with Flow. Then real HTTP goes through the app: an endpoint that needs
signed calls starts its pipeline only for a call signed with its secret, sent
within the time window, with a message id it has not accepted before. The
secret is shown once, kept encrypted, and replaced on request.
"""

from __future__ import annotations

import json
import time
import uuid

import pytest

from revoluzen_flow.triggers.webhook_signing import (
    SignatureError,
    new_secret,
    secret_key,
    sign,
    verify,
)

from tests.conftest_fixtures_v2 import (  # noqa: F401
    data_dir, db_fixture, app_v2, client, admin_token, authed_client,
)


# ── The signature ────────────────────────────────────────────────────────


def test_matches_the_example_the_specification_publishes():
    assert sign(
        "whsec_MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaSw", "msg_p5jXN8AQM9LWM0D4loKWxJek", 1614265330,
        b'{"test": 2432232314}',
    ) == "v1,g0hM9SsE+OTPJTGt/tmIKtSyZlE3uFJELVlNIOLJ1OE="


def _headers(secret, body, *, message_id="msg_1", timestamp=None, signature=None):
    ts = int(time.time()) if timestamp is None else timestamp
    return {
        "webhook-id": message_id,
        "webhook-timestamp": str(ts),
        "webhook-signature": signature or sign(secret, message_id, ts, body),
    }


class TestVerify:
    def test_a_good_call_passes(self):
        secret = new_secret()
        call = verify(secret, _headers(secret, b'{"a": 1}'), b'{"a": 1}')
        assert call.message_id == "msg_1"

    def test_a_changed_body_fails(self):
        secret = new_secret()
        with pytest.raises(SignatureError, match="does not match"):
            verify(secret, _headers(secret, b'{"a": 1}'), b'{"a": 2}')

    def test_another_secret_fails(self):
        with pytest.raises(SignatureError, match="does not match"):
            verify(new_secret(), _headers(new_secret(), b"{}"), b"{}")

    @pytest.mark.parametrize("skew", [-301, 301])
    def test_a_call_outside_the_window_fails(self, skew):
        secret = new_secret()
        now = 1_800_000_000
        headers = _headers(secret, b"{}", timestamp=now + skew)
        with pytest.raises(SignatureError, match="more than 300 seconds"):
            verify(secret, headers, b"{}", now=now)
        assert verify(secret, headers, b"{}", now=now + skew).message_id == "msg_1"

    def test_missing_headers_are_named(self):
        with pytest.raises(SignatureError, match="webhook-id, webhook-signature"):
            verify(new_secret(), {"webhook-timestamp": "1"}, b"")

    def test_one_matching_signature_among_several_is_enough(self):
        secret = new_secret()
        good = _headers(secret, b"{}")
        good["webhook-signature"] = "v1,bm90LWl0 " + good["webhook-signature"]
        assert verify(secret, good, b"{}").message_id == "msg_1"

    def test_a_timestamp_that_is_not_seconds_fails(self):
        secret = new_secret()
        headers = _headers(secret, b"{}")
        headers["webhook-timestamp"] = "2026-11-11T09:00:00Z"
        with pytest.raises(SignatureError, match="whole seconds"):
            verify(secret, headers, b"{}")

    def test_secrets_are_long_random_and_prefixed(self):
        a, b = new_secret(), new_secret()
        assert a.startswith("whsec_") and a != b and len(secret_key(a)) == 32


# ── Through the app ──────────────────────────────────────────────────────


def _pipeline(authed_client, *, sql_param=False):
    from revoluzen_flow.ir.schema import PipelineStatus
    from revoluzen_flow.main import app_state

    condition = "region = '${param.region}'"
    r = authed_client.post("/api/workflows", json={
        "name": f"Signed {uuid.uuid4().hex[:6]}",
        "steps": [
            {"id": "src", "type": "source", "label": "Orders",
             "params": {"connector_type": "csv", "file_path": "orders.csv", "header": True}},
            {"id": "flt", "type": "filter", "label": "One region", "params": {"condition": condition}},
        ],
        "connections": [{"from_step": "src", "to_step": "flt"}],
        "parameters": [{"name": "region", "type": "string", "required": True}],
    })
    assert r.status_code in (200, 201), r.text
    wid = r.json().get("id") or r.json()["workflow"]["id"]
    app_state["store"].update_status(wid, PipelineStatus.PUBLISHED)
    return wid


def _endpoint(authed_client, workflow_id, **fields):
    body = {"workflow_id": workflow_id, "path": f"hooks/{uuid.uuid4().hex[:8]}", "name": "hook",
            "require_api_key": False, "require_signature": True, **fields}
    r = authed_client.post("/api/gateway/endpoints", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def _send(client, endpoint, secret, payload, *, message_id=None, timestamp=None, extra_headers=None, sign_body=None):
    body = json.dumps(payload).encode()
    message_id = message_id or f"msg_{uuid.uuid4().hex}"
    headers = _headers(secret, sign_body if sign_body is not None else body, message_id=message_id, timestamp=timestamp)
    headers["Content-Type"] = "application/json"
    headers.update(extra_headers or {})
    return client.post(f"/api/published/{endpoint['path']}", content=body, headers=headers)


def _runs(workflow_id):
    from revoluzen_flow.main import app_state
    return app_state["execution_store"].list_by_workflow(workflow_id)


class TestSignedEndpoint:
    def test_a_signed_call_starts_the_pipeline_with_its_values(self, authed_client, client):
        wid = _pipeline(authed_client)
        endpoint = _endpoint(authed_client, wid)
        secret = endpoint["signing_secret"]
        assert secret.startswith("whsec_")
        r = _send(client, endpoint, secret, {"region": "EU"}, message_id="msg_first")
        assert r.status_code == 200, r.text
        result = r.json()
        assert result["status"] == "success"
        assert next(s for s in result["steps"] if s["step_id"] == "flt")["row_count"] == 2
        run = next(x for x in _runs(wid) if x["id"] == result["execution_id"])
        assert run["triggered_by"] == "webhook"
        assert run["metadata"]["webhook_id"] == "msg_first"

    def test_the_secret_is_shown_once_and_kept_encrypted(self, authed_client):
        from revoluzen_flow.main import app_state

        endpoint = _endpoint(authed_client, _pipeline(authed_client))
        secret = endpoint["signing_secret"]
        listed = authed_client.get("/api/gateway/endpoints").json()
        mine = next(e for e in listed if e["id"] == endpoint["id"])
        assert mine["require_signature"] == 1 and "signing_secret" not in mine
        assert secret not in json.dumps(listed)
        stored = app_state["gateway_store"].get_signing_secret(endpoint["id"])
        assert stored and stored != secret
        if app_state.get("encryptor") is not None:
            assert stored.startswith("ENC:v1:")
        raw = app_state["db"].fetchone("SELECT * FROM published_endpoints WHERE id = ?", (endpoint["id"],))
        assert secret not in json.dumps(raw, default=str)

    def test_unsigned_wrongly_signed_or_changed_calls_start_nothing(self, authed_client, client):
        wid = _pipeline(authed_client)
        endpoint = _endpoint(authed_client, wid)
        secret = endpoint["signing_secret"]
        unsigned = client.post(f"/api/published/{endpoint['path']}", json={"region": "EU"})
        assert unsigned.status_code == 401 and "webhook-id" in unsigned.text
        wrong = _send(client, endpoint, new_secret(), {"region": "EU"})
        assert wrong.status_code == 401 and "does not match" in wrong.text
        changed = _send(client, endpoint, secret, {"region": "APAC"}, sign_body=b'{"region": "EU"}')
        assert changed.status_code == 401
        stale = _send(client, endpoint, secret, {"region": "EU"}, timestamp=int(time.time()) - 3600)
        assert stale.status_code == 401 and "more than 300 seconds" in stale.text
        assert _runs(wid) == []

    def test_a_message_is_accepted_once(self, authed_client, client):
        wid = _pipeline(authed_client)
        endpoint = _endpoint(authed_client, wid)
        secret = endpoint["signing_secret"]
        assert _send(client, endpoint, secret, {"region": "EU"}, message_id="msg_once").status_code == 200
        # A recorded call, or the same message signed again, does not run twice.
        again = _send(client, endpoint, secret, {"region": "EU"}, message_id="msg_once")
        assert again.status_code == 409 and "already received" in again.text
        assert len(_runs(wid)) == 1

    def test_a_call_turned_away_before_running_keeps_its_id(self, authed_client, client):
        from revoluzen_flow.main import app_state

        wid = _pipeline(authed_client)
        endpoint = _endpoint(authed_client, wid)
        secret = endpoint["signing_secret"]
        store = app_state["store"]
        latest = store.get(wid)
        latest.workflow.is_active_dev = False
        store.save(latest.workflow)
        paused = _send(client, endpoint, secret, {"region": "EU"}, message_id="msg_retry")
        assert paused.status_code == 423
        latest = store.get(wid)
        latest.workflow.is_active_dev = True
        store.save(latest.workflow)
        assert _send(client, endpoint, secret, {"region": "EU"}, message_id="msg_retry").status_code == 200

    def test_a_new_secret_replaces_the_old_one(self, authed_client, client):
        endpoint = _endpoint(authed_client, _pipeline(authed_client))
        old = endpoint["signing_secret"]
        r = authed_client.post(f"/api/gateway/endpoints/{endpoint['id']}/signing-secret")
        assert r.status_code == 200, r.text
        new = r.json()["signing_secret"]
        assert new != old
        assert _send(client, endpoint, old, {"region": "EU"}).status_code == 401
        assert _send(client, endpoint, new, {"region": "EU"}).status_code == 200

    def test_key_and_signature_together(self, authed_client, client):
        endpoint = _endpoint(authed_client, _pipeline(authed_client), require_api_key=True)
        secret = endpoint["signing_secret"]
        key = authed_client.post("/api/gateway/keys", json={"name": "both"}).json()["api_key"]
        assert _send(client, endpoint, secret, {"region": "EU"}).status_code == 401
        both = _send(client, endpoint, secret, {"region": "EU"}, extra_headers={"X-API-Key": key})
        assert both.status_code == 200, both.text

    def test_turning_signing_on_and_off(self, authed_client, client):
        wid = _pipeline(authed_client)
        r = authed_client.post("/api/gateway/endpoints", json={
            "workflow_id": wid, "path": f"hooks/{uuid.uuid4().hex[:8]}", "name": "plain",
        })
        endpoint = r.json()
        assert "signing_secret" not in endpoint and not endpoint["require_signature"]
        on = authed_client.put(f"/api/gateway/endpoints/{endpoint['id']}", json={
            "require_signature": True, "require_api_key": False,
        })
        assert on.status_code == 200, on.text
        secret = on.json()["signing_secret"]
        assert _send(client, endpoint, secret, {"region": "EU"}).status_code == 200
        off = authed_client.put(f"/api/gateway/endpoints/{endpoint['id']}", json={
            "require_signature": False, "require_api_key": True,
        })
        assert off.status_code == 200
        assert "signing_secret" not in off.json() and not off.json()["require_signature"]
        from revoluzen_flow.main import app_state
        assert app_state["gateway_store"].get_signing_secret(endpoint["id"]) == ""
        assert client.post(f"/api/published/{endpoint['path']}", json={"region": "EU"}).status_code == 401

    def test_rotating_needs_signing_on(self, authed_client):
        r = authed_client.post("/api/gateway/endpoints", json={
            "workflow_id": _pipeline(authed_client), "path": f"hooks/{uuid.uuid4().hex[:8]}", "name": "x",
        })
        assert authed_client.post(f"/api/gateway/endpoints/{r.json()['id']}/signing-secret").status_code == 400

    def test_a_get_endpoint_cannot_be_signed(self, authed_client):
        r = authed_client.post("/api/gateway/endpoints", json={
            "workflow_id": _pipeline(authed_client), "path": f"hooks/{uuid.uuid4().hex[:8]}", "name": "g",
            "method": "GET", "require_signature": True,
        })
        assert r.status_code == 400 and "POST or PUT" in r.text

    def test_a_signed_endpoint_is_not_a_public_one(self, authed_client):
        """Request values in SQL are refused on an endpoint anyone can call,
        not on one whose calls are signed."""
        from revoluzen_flow.ir.schema import PipelineStatus
        from revoluzen_flow.main import app_state

        r = authed_client.post("/api/workflows", json={
            "name": f"SQL {uuid.uuid4().hex[:6]}",
            "steps": [{"id": "q", "type": "db_source", "label": "Q",
                       "params": {"query": "SELECT * FROM t WHERE region = '${param.region}'"}}],
            "parameters": [{"name": "region", "type": "string"}],
        })
        wid = r.json().get("id") or r.json()["workflow"]["id"]
        app_state["store"].update_status(wid, PipelineStatus.PUBLISHED)
        public = authed_client.post("/api/gateway/endpoints", json={
            "workflow_id": wid, "path": f"hooks/{uuid.uuid4().hex[:8]}", "require_api_key": False,
        })
        assert public.status_code == 400
        signed = authed_client.post("/api/gateway/endpoints", json={
            "workflow_id": wid, "path": f"hooks/{uuid.uuid4().hex[:8]}", "require_api_key": False,
            "require_signature": True,
        })
        assert signed.status_code == 200, signed.text

    def test_the_window_can_be_set_within_bounds(self, authed_client):
        wid = _pipeline(authed_client)
        assert _endpoint(authed_client, wid, signature_tolerance_seconds=600)["signature_tolerance_seconds"] == 600
        r = authed_client.post("/api/gateway/endpoints", json={
            "workflow_id": wid, "path": f"hooks/{uuid.uuid4().hex[:8]}", "require_signature": True,
            "signature_tolerance_seconds": 5,
        })
        assert r.status_code == 400
