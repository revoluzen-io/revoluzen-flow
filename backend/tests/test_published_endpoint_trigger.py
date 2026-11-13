"""POST /api/published/{path}: another application starts a pipeline.

This route returned 500 on every call with a valid key, because it called a
method the executor does not have. Nothing noticed: the only tests on the
surface checked that an anonymous caller is refused, and that refusal happens
before the broken line. So everything here sends real HTTP through the app and
runs a real saved pipeline, then reads the run back the way the Executions page
does.

The fixture pipeline reads a CSV and filters it with `${param.region}` and
`${param.min_amount}`. The row count coming out of the filter is what proves a
value really reached the engine: a substitution that silently failed would give
a different number or a failed step.
"""

from __future__ import annotations

import json
import os
import pathlib

import pytest

from tests.conftest_fixtures_v2 import (  # noqa: F401
    data_dir, db_fixture, app_v2, client, admin_token, authed_client, role_clients,
)

SYNTHETIC_BAD_KEY = "fpk_this-is-not-a-real-key"


def _body(resp):
    try:
        return resp.json()
    except Exception:  # noqa: BLE001
        return {"_raw": resp.text[:400]}


def _pipeline(name, *, condition="region = '${param.region}' AND amount >= ${param.min_amount}"):
    return {
        "name": name,
        "steps": [
            {
                "id": "src",
                "type": "source",
                "label": "Orders file",
                "params": {
                    "connector_type": "csv",
                    "file_path": "orders.csv",
                    "format": "csv",
                    "delimiter": ",",
                    "header": True,
                },
                "position": {"x": 0, "y": 0},
            },
            {
                "id": "flt",
                "type": "filter",
                "label": "Keep one region",
                "params": {"condition": condition},
                "position": {"x": 300, "y": 0},
            },
        ],
        "connections": [{"from_step": "src", "to_step": "flt"}],
        "parameters": [
            {"name": "region", "type": "string", "required": True, "description": "Which region"},
            {"name": "min_amount", "type": "int", "default": 0, "description": "Smallest order"},
        ],
    }


_counter = {"n": 0}


def _unique(prefix):
    _counter["n"] += 1
    return f"{prefix}-{_counter['n']}"


def _save_pipeline(authed_client, **kwargs):
    r = authed_client.post("/api/workflows", json=_pipeline(_unique("trigger-probe"), **kwargs))
    assert r.status_code in (200, 201), _body(r)
    wf = _body(r)
    return wf.get("id") or wf["workflow"]["id"]


def _publish(authed_client, workflow_id, **kwargs):
    path = kwargs.pop("path", None) or _unique("orders/run")
    r = authed_client.post("/api/gateway/endpoints", json={
        "workflow_id": workflow_id, "path": path, "name": "probe", **kwargs,
    })
    assert r.status_code == 200, _body(r)
    return _body(r)


@pytest.fixture
def api_key(authed_client):
    r = authed_client.post("/api/gateway/keys", json={"name": _unique("probe-key")})
    assert r.status_code == 200, _body(r)
    return _body(r)["api_key"]


@pytest.fixture
def endpoint(authed_client):
    return _publish(authed_client, _save_pipeline(authed_client))


def _call(client, endpoint, key=None, body=None, **kwargs):
    headers = {"X-API-Key": key} if key else {}
    if body is not None:
        kwargs["json"] = body
    return client.post(f"/api/published/{endpoint['path']}", headers=headers, **kwargs)


def _runs_for(authed_client, workflow_id):
    r = authed_client.get("/api/monitor/executions", params={"workflow_id": workflow_id})
    assert r.status_code == 200, _body(r)
    return _body(r)


def _step(response_body, step_id):
    return next(s for s in response_body["steps"] if s["step_id"] == step_id)


# ── The key ──────────────────────────────────────────────────────────

def test_no_key_is_401(client, endpoint):
    r = _call(client, endpoint, body={"region": "EU"})
    assert r.status_code == 401, _body(r)


def test_wrong_key_is_403(client, endpoint):
    r = _call(client, endpoint, key=SYNTHETIC_BAD_KEY, body={"region": "EU"})
    assert r.status_code == 403, _body(r)


def test_revoked_key_is_403(client, authed_client, endpoint):
    created = _body(authed_client.post("/api/gateway/keys", json={"name": _unique("gone")}))
    assert authed_client.delete(f"/api/gateway/keys/{created['id']}").status_code == 200
    r = _call(client, endpoint, key=created["api_key"], body={"region": "EU"})
    assert r.status_code == 403, _body(r)


def test_key_from_another_workspace_is_403(client, endpoint):
    """The route used to check that a key was valid, not whose it was."""
    from revoluzen_flow.main import app_state
    foreign = app_state["gateway_store"].create_api_key(name="foreign", workspace_id="another-workspace")
    r = _call(client, endpoint, key=foreign["api_key"], body={"region": "EU"})
    assert r.status_code == 403, _body(r)


def test_key_limited_to_other_addresses_is_403(client, authed_client, endpoint):
    created = _body(authed_client.post("/api/gateway/keys", json={
        "name": _unique("narrow"), "ip_allowlist": ["203.0.113.0/24"],
    }))
    r = _call(client, endpoint, key=created["api_key"], body={"region": "EU"})
    assert r.status_code == 403, _body(r)


# ── Parameters ───────────────────────────────────────────────────────

def test_value_really_reaches_the_pipeline(client, authed_client, api_key, endpoint):
    """The fixture file has two EU orders, one APAC, and a different row count
    for each proves the value changed what the engine did."""
    r = _call(client, endpoint, key=api_key, body={"region": "EU"})
    assert r.status_code == 200, _body(r)
    assert _body(r)["status"] == "success", _body(r)
    assert _step(_body(r), "flt")["row_count"] == 2

    r = _call(client, endpoint, key=api_key, body={"region": "APAC"})
    assert _step(_body(r), "flt")["row_count"] == 1


def test_a_typed_value_is_used_and_coerced(client, api_key, endpoint):
    # EU orders are 200 and 250. "220" arrives as text and must act as a number.
    r = _call(client, endpoint, key=api_key, body={"region": "EU", "min_amount": "220"})
    assert _step(_body(r), "flt")["row_count"] == 1
    assert _body(r)["parameter_values"] == {"region": "EU", "min_amount": 220}


def test_unknown_parameter_is_400_naming_it(client, authed_client, api_key, endpoint):
    r = _call(client, endpoint, key=api_key, body={"region": "EU", "regoin": "EU"})
    assert r.status_code == 400, _body(r)
    assert "regoin" in _body(r)["detail"]
    assert _runs_for(authed_client, endpoint["workflow_id"]) == [], "a refused call is not a run"


def test_missing_required_parameter_is_400_naming_it(client, authed_client, api_key, endpoint):
    r = _call(client, endpoint, key=api_key, body={"min_amount": 5})
    assert r.status_code == 400, _body(r)
    assert "region" in _body(r)["detail"]
    assert _runs_for(authed_client, endpoint["workflow_id"]) == []


def test_null_does_not_satisfy_a_required_parameter(client, api_key, endpoint):
    r = _call(client, endpoint, key=api_key, body={"region": None})
    assert r.status_code == 400, _body(r)
    assert "region" in _body(r)["detail"]


def test_value_of_the_wrong_type_is_400_naming_it(client, api_key, endpoint):
    r = _call(client, endpoint, key=api_key, body={"region": "EU", "min_amount": "lots"})
    assert r.status_code == 400, _body(r)
    assert "min_amount" in _body(r)["detail"]


@pytest.mark.parametrize("raw", [b"{not json", b"[1, 2]", b'"EU"'])
def test_a_body_that_is_not_a_json_object_is_400(client, api_key, endpoint, raw):
    """Ignoring it would run the pipeline on its defaults while the caller
    believes it passed something."""
    r = client.post(
        f"/api/published/{endpoint['path']}",
        headers={"X-API-Key": api_key, "Content-Type": "application/json"},
        content=raw,
    )
    assert r.status_code == 400, _body(r)


def test_default_with_a_system_placeholder_is_resolved(client, authed_client, api_key):
    """The page leaves such a default out of the command, relying on this. It
    used to reach the step as the literal text `${utcnow:%Y}`."""
    from datetime import datetime, timezone
    year = datetime.now(timezone.utc).strftime("%Y")
    pipeline = _pipeline(_unique("dated"), condition="'${param.year}' = '" + year + "'")
    pipeline["parameters"] = [{"name": "year", "type": "string", "default": "${utcnow:%Y}"}]
    wf = _body(authed_client.post("/api/workflows", json=pipeline))
    endpoint = _publish(authed_client, wf.get("id") or wf["workflow"]["id"])
    body = _body(_call(client, endpoint, key=api_key))
    assert body["parameter_values"] == {"year": year}
    assert _step(body, "flt")["row_count"] == 5, body


def test_no_parameters_declared_and_none_sent_still_runs(client, authed_client, api_key):
    pipeline = _pipeline(_unique("no-params"), condition="region = 'US'")
    pipeline["parameters"] = []
    wf = _body(authed_client.post("/api/workflows", json=pipeline))
    endpoint = _publish(authed_client, wf.get("id") or wf["workflow"]["id"])
    r = _call(client, endpoint, key=api_key)
    assert r.status_code == 200, _body(r)
    assert _step(_body(r), "flt")["row_count"] == 2
    assert _body(r)["parameter_values"] == {}


def test_a_secret_default_never_comes_back_to_the_caller(client, authed_client, api_key):
    """Every default is resolved up front so a bad call can be refused, and the
    response echoes what was resolved. A secret default must not be in it."""
    from revoluzen_flow.connections.inline_secrets import SECRET_PLACEHOLDER

    secret = "synthetic-default-token-5K"
    pipeline = _pipeline(_unique("secret-default"))
    pipeline["parameters"].append({"name": "api_token", "type": "string", "default": secret})
    wf = _body(authed_client.post("/api/workflows", json=pipeline))
    endpoint = _publish(authed_client, wf.get("id") or wf["workflow"]["id"])

    r = _call(client, endpoint, key=api_key, body={"region": "EU"})
    assert r.status_code == 200, _body(r)
    assert secret not in r.text
    assert _body(r)["parameter_values"]["api_token"] == SECRET_PLACEHOLDER
    assert _body(r)["parameter_values"]["region"] == "EU"

    run = authed_client.get(f"/api/monitor/executions/{_body(r)['execution_id']}")
    assert secret not in run.text
    assert _body(run)["metadata"]["parameter_values"]["api_token"] == SECRET_PLACEHOLDER


# ── The run it leaves behind ─────────────────────────────────────────

def test_run_is_recorded_with_the_trigger_and_values(client, authed_client, api_key, endpoint):
    body = _body(_call(client, endpoint, key=api_key, body={"region": "EU"}))
    run = _body(authed_client.get(f"/api/monitor/executions/{body['execution_id']}"))
    assert run["workflow_id"] == endpoint["workflow_id"]
    assert run["triggered_by"] == "api"
    assert run["status"] == "success"
    # Recorded with the default filled in, so the run shows what was really used.
    assert run["metadata"]["parameter_values"] == {"region": "EU", "min_amount": 0}
    assert run["metadata"]["published_endpoint"] == f"/{endpoint['path']}"
    assert run["workflow_version"] is not None
    assert [s["step_id"] for s in run["step_logs"]] == ["src", "flt"]
    # Keeps the "no run recorded" assertions elsewhere in this file honest.
    assert [r["id"] for r in _runs_for(authed_client, endpoint["workflow_id"])] == [body["execution_id"]]


def test_it_counts_as_a_production_trigger(client, authed_client, api_key, endpoint):
    """The Dashboard's headline success rate reads this sub-dict."""
    before = _body(authed_client.get("/api/monitor/stats"))["scheduled"]["total"]
    _call(client, endpoint, key=api_key, body={"region": "EU"})
    after = _body(authed_client.get("/api/monitor/stats"))["scheduled"]["total"]
    assert after == before + 1


def test_a_manual_run_is_still_manual(authed_client, endpoint):
    """The shared run path takes the trigger as an argument. Its default must
    keep meaning what it meant before."""
    r = authed_client.post(
        f"/api/execute/workflow/{endpoint['workflow_id']}",
        json={"parameter_values": {"region": "EU"}},
    )
    assert r.status_code == 200, _body(r)
    run = _body(authed_client.get(f"/api/monitor/executions/{_body(r)['execution_id']}"))
    assert run["triggered_by"] == "manual"


def test_the_run_goes_through_the_pool(client, authed_client, api_key, endpoint):
    before = _body(authed_client.get("/api/pool/status")).get("counters", {})
    _call(client, endpoint, key=api_key, body={"region": "EU"})
    after = _body(authed_client.get("/api/pool/status")).get("counters", {})
    assert after.get("total_submitted", 0) > before.get("total_submitted", 0), (
        "the run did not pass through pool admission"
    )


def test_calls_are_counted_on_the_endpoint(client, authed_client, api_key, endpoint):
    _call(client, endpoint, key=api_key, body={"region": "EU"})
    _call(client, endpoint, key=api_key, body={"region": "US"})
    listed = _body(authed_client.get("/api/gateway/endpoints"))
    row = next(e for e in listed if e["id"] == endpoint["id"])
    assert row["request_count"] == 2
    assert row["last_called_at"] > 0


def test_reads_all_the_data_not_the_dev_sample(client, authed_client, api_key, data_dir):
    from revoluzen_flow import runtime_config
    total = runtime_config.DEV_SAMPLE_ROWS + 25
    with open(os.path.join(data_dir, "big.csv"), "w", encoding="utf-8") as fh:
        fh.write("id,region\n")
        fh.writelines(f"{i},EU\n" for i in range(total))
    pipeline = _pipeline(_unique("big"), condition="region = '${param.region}'")
    pipeline["steps"][0]["params"]["file_path"] = "big.csv"
    pipeline["parameters"] = pipeline["parameters"][:1]
    wf = _body(authed_client.post("/api/workflows", json=pipeline))
    endpoint = _publish(authed_client, wf.get("id") or wf["workflow"]["id"])
    body = _body(_call(client, endpoint, key=api_key, body={"region": "EU"}))
    assert body["status"] == "success", body
    assert _step(body, "src")["row_count"] == total


def test_the_documented_example_really_runs(client, authed_client, api_key):
    """docs/examples/webhook-trigger.md tells people this works. Its earlier
    pipeline could not: a saved `db_source` needs a connection it did not have."""
    example = pathlib.Path(__file__).resolve().parents[2] / "docs" / "examples" / "webhook-order-pipeline.json"
    pipeline = json.loads(example.read_text(encoding="utf-8"))
    pipeline["id"] = _unique("doc-example")
    wf = _body(authed_client.post("/api/workflows", json=pipeline))
    endpoint = _publish(authed_client, wf.get("id") or wf["workflow"]["id"])
    body = _body(_call(client, endpoint, key=api_key, body={"country": "US", "min_quantity": 2}))
    assert body["status"] == "success", body
    assert [s["step_id"] for s in body["steps"]] == ["src", "flt", "sink"]
    assert 0 < _step(body, "flt")["row_count"] < _step(body, "src")["row_count"], body


# ── The response ─────────────────────────────────────────────────────

def test_response_shape_and_no_data_leaves(client, api_key, endpoint):
    r = _call(client, endpoint, key=api_key, body={"region": "US"})
    body = _body(r)
    assert set(body) == {
        "status", "execution_id", "pipeline_id", "parameter_values", "duration_ms", "steps",
    }
    assert body["pipeline_id"] == endpoint["workflow_id"]
    assert body["parameter_values"] == {"region": "US", "min_amount": 0}
    for step in body["steps"]:
        assert set(step) == {"step_id", "name", "status", "row_count"}
    assert _step(body, "flt")["name"] == "Keep one region"
    # Names from the two US rows in the file. The step's own captured sample
    # holds them, and they must not be anywhere in what the caller receives.
    assert "Alice" not in r.text and "Carol" not in r.text


def test_failed_run_reports_the_step_not_the_error_text(client, api_key, authed_client):
    wf_id = _save_pipeline(authed_client, condition="no_such_column = '${param.region}' AND ${param.min_amount} = 0")
    endpoint = _publish(authed_client, wf_id)
    r = _call(client, endpoint, key=api_key, body={"region": "EU"})
    assert r.status_code == 200, _body(r)
    body = _body(r)
    assert body["status"] == "error"
    assert "Keep one region" in body["message"]
    assert _step(body, "flt")["status"] == "error"
    assert "no_such_column" not in r.text, "raw engine error text left the product"
    # The operator still gets the reason, on the run.
    run = _body(authed_client.get(f"/api/monitor/executions/{body['execution_id']}"))
    assert "no_such_column" in (run.get("error_message") or "")


def test_run_refused_before_its_first_step_says_so(client, api_key, authed_client):
    """A saved inline `db_source` needs a connection, so validation refuses the
    run. The reason has to reach the run record, not "Unknown error"."""
    pipeline = {
        "name": _unique("refused"),
        "steps": [{
            "id": "src", "type": "db_source", "label": "Inline",
            "params": {"query": "SELECT 1 AS id"}, "position": {"x": 0, "y": 0},
        }],
        "connections": [],
    }
    wf = _body(authed_client.post("/api/workflows", json=pipeline))
    endpoint = _publish(authed_client, wf.get("id") or wf["workflow"]["id"])
    r = _call(client, endpoint, key=api_key)
    body = _body(r)
    assert body["status"] == "error", body
    assert "could not start" in body["message"]
    assert "connection_id" not in r.text
    run = _body(authed_client.get(f"/api/monitor/executions/{body['execution_id']}"))
    assert "connection_id" in run["error_message"]


# ── The endpoint's own state ─────────────────────────────────────────

def test_unknown_path_is_404(client, api_key):
    r = client.post("/api/published/no/such/path", headers={"X-API-Key": api_key}, json={})
    assert r.status_code == 404, _body(r)


def test_turned_off_endpoint_is_503_then_works_again(client, authed_client, api_key, endpoint):
    off = authed_client.put(f"/api/gateway/endpoints/{endpoint['id']}", json={"is_active": 0})
    assert off.status_code == 200, _body(off)
    r = _call(client, endpoint, key=api_key, body={"region": "EU"})
    assert r.status_code == 503, _body(r)

    on = authed_client.put(f"/api/gateway/endpoints/{endpoint['id']}", json={"is_active": 1})
    assert on.status_code == 200, _body(on)
    assert _call(client, endpoint, key=api_key, body={"region": "EU"}).status_code == 200


def test_deactivated_pipeline_is_423(client, authed_client, api_key, endpoint):
    off = authed_client.post(
        f"/api/workflows/{endpoint['workflow_id']}/toggle-active", json={"active": False},
    )
    assert off.status_code == 200, _body(off)
    r = _call(client, endpoint, key=api_key, body={"region": "EU"})
    assert r.status_code == 423, _body(r)
    assert _runs_for(authed_client, endpoint["workflow_id"]) == []


def test_archived_pipeline_is_423(client, authed_client, api_key, endpoint):
    archived = authed_client.post(f"/api/workflows/{endpoint['workflow_id']}/archive")
    assert archived.status_code == 200, _body(archived)
    r = _call(client, endpoint, key=api_key, body={"region": "EU"})
    assert r.status_code == 423, _body(r)


def test_rate_limit_is_429(client, authed_client, api_key):
    endpoint = _publish(authed_client, _save_pipeline(authed_client), rate_limit_rpm=1)
    assert _call(client, endpoint, key=api_key, body={"region": "EU"}).status_code == 200
    r = _call(client, endpoint, key=api_key, body={"region": "EU"})
    assert r.status_code == 429, _body(r)


def test_public_endpoint_is_rate_limited_too(client, authed_client):
    pipeline = _pipeline(_unique("public-rate"), condition="region = 'US'")
    pipeline["parameters"] = []
    wf = _body(authed_client.post("/api/workflows", json=pipeline))
    endpoint = _publish(
        authed_client, wf.get("id") or wf["workflow"]["id"], require_api_key=False, rate_limit_rpm=1,
    )
    assert _call(client, endpoint).status_code == 200
    assert _call(client, endpoint).status_code == 429


def test_get_cannot_fire_a_post_endpoint(client, api_key, endpoint):
    r = client.get(f"/api/published/{endpoint['path']}", headers={"X-API-Key": api_key})
    assert r.status_code == 405, _body(r)


# ── The SQL-injection guard on keyless endpoints ─────────────────────

def test_param_in_sql_refuses_a_keyless_endpoint(authed_client):
    wf_id = _save_pipeline(authed_client)
    r = authed_client.post("/api/gateway/endpoints", json={
        "workflow_id": wf_id, "path": _unique("public"), "require_api_key": False,
    })
    assert r.status_code == 400, _body(r)
    assert "flt" in _body(r)["detail"]["steps"]


def test_param_in_sql_is_fine_with_a_key(authed_client):
    assert _publish(authed_client, _save_pipeline(authed_client))["require_api_key"] == 1


def test_turning_the_key_off_later_is_refused_too(authed_client, endpoint):
    r = authed_client.put(f"/api/gateway/endpoints/{endpoint['id']}", json={"require_api_key": False})
    assert r.status_code == 400, _body(r)


def test_guard_sees_nested_sql_fields_but_not_other_text():
    from revoluzen_flow.api.gateway import _templated_sql_step_ids

    class _Step:
        def __init__(self, sid, params):
            self.id, self.params = sid, params

    class _Workflow:
        steps = [
            _Step("query", {"query": "SELECT * FROM t WHERE id = ${param.id}"}),
            _Step("pre", {"pre_sql": "DELETE FROM t WHERE d = '${param.d}'"}),
            _Step("rule", {"rules": [{"column": "a", "value": "${param.a}"}]}),
            _Step("nested", {"columns": [{"expression": "x + ${param.n}"}]}),
            _Step("path", {"file_path": "/data/${param.name}.csv"}),
            _Step("plain", {"query": "SELECT 1"}),
            _Step("clock", {"query": "SELECT '${utcnow:%Y}'"}),
        ]

    assert _templated_sql_step_ids(_Workflow) == ["query", "pre", "rule", "nested"]


def test_public_endpoint_stops_when_its_pipeline_later_gains_sql_params(client, authed_client):
    """Publishing checked the pipeline as it was that day. An edit afterwards
    must not turn a public endpoint into an unauthenticated injection."""
    pipeline = _pipeline(_unique("later"), condition="region = 'US'")
    pipeline["parameters"] = pipeline["parameters"][:1]
    pipeline["parameters"][0]["required"] = False
    wf = _body(authed_client.post("/api/workflows", json=pipeline))
    wf_id = wf.get("id") or wf["workflow"]["id"]
    endpoint = _publish(authed_client, wf_id, require_api_key=False)
    assert _call(client, endpoint).status_code == 200

    current = _body(authed_client.get(f"/api/workflows/{wf_id}"))["workflow"]
    current["steps"][1]["params"]["condition"] = "region = '${param.region}'"
    assert authed_client.put(f"/api/workflows/{wf_id}", json={"workflow": current}).status_code == 200

    r = _call(client, endpoint, body={"region": "EU' OR '1'='1"})
    assert r.status_code == 403, _body(r)


def test_explicit_override_is_remembered(client, authed_client):
    wf_id = _save_pipeline(authed_client)
    endpoint = _publish(authed_client, wf_id, require_api_key=False, allow_templated_sql_public=True)
    r = _call(client, endpoint, body={"region": "EU"})
    assert r.status_code == 200, _body(r)
    assert _step(_body(r), "flt")["row_count"] == 2


# ── Managing endpoints and keys ──────────────────────────────────────

def test_full_key_is_returned_once_and_never_listed(authed_client):
    created = _body(authed_client.post("/api/gateway/keys", json={"name": _unique("once")}))
    assert created["api_key"].startswith(created["prefix"])
    listed = authed_client.get("/api/gateway/keys")
    assert created["api_key"] not in listed.text
    row = next(k for k in _body(listed) if k["id"] == created["id"])
    assert row["key_prefix"] == created["prefix"]
    assert "key_hash" not in row


@pytest.mark.parametrize("path", ["", "a b", "../up", "a/../b", "/", "a//b", "x?y=1"])
def test_unsafe_path_is_400(authed_client, path):
    wf_id = _save_pipeline(authed_client)
    r = authed_client.post("/api/gateway/endpoints", json={"workflow_id": wf_id, "path": path})
    assert r.status_code == 400, _body(r)


def test_path_already_in_use_is_409(authed_client, endpoint):
    r = authed_client.post("/api/gateway/endpoints", json={
        "workflow_id": endpoint["workflow_id"], "path": endpoint["path"],
    })
    assert r.status_code == 409, _body(r)


def test_endpoint_for_an_unknown_pipeline_is_404(authed_client):
    r = authed_client.post("/api/gateway/endpoints", json={
        "workflow_id": "no-such-pipeline", "path": _unique("nothing"),
    })
    assert r.status_code == 404, _body(r)


def test_delete_stops_it_answering(client, authed_client, api_key, endpoint):
    assert authed_client.delete(f"/api/gateway/endpoints/{endpoint['id']}").status_code == 200
    assert _call(client, endpoint, key=api_key, body={"region": "EU"}).status_code == 404


def test_another_workspace_cannot_touch_them(authed_client, endpoint):
    from revoluzen_flow.main import app_state
    store = app_state["gateway_store"]
    foreign_key = store.create_api_key(name="foreign", workspace_id="another-workspace")
    foreign_endpoint = store.publish_endpoint(
        workflow_id="wf", path=_unique("foreign"), workspace_id="another-workspace",
    )
    assert authed_client.delete(f"/api/gateway/keys/{foreign_key['id']}").status_code == 404
    assert authed_client.delete(f"/api/gateway/endpoints/{foreign_endpoint['id']}").status_code == 404
    assert authed_client.put(
        f"/api/gateway/endpoints/{foreign_endpoint['id']}", json={"is_active": 0},
    ).status_code == 404


def test_roles_the_page_hides_the_buttons_from_are_refused(role_clients):
    viewer = role_clients.get("viewer")
    if viewer is None:
        pytest.skip("viewer role not provisioned in this environment")
    assert viewer.post("/api/gateway/keys", json={"name": "nope"}).status_code == 403
    assert viewer.get("/api/gateway/keys").status_code == 200


def test_data_engineer_can_create(role_clients):
    engineer = role_clients.get("data_engineer")
    if engineer is None:
        pytest.skip("data_engineer role not provisioned in this environment")
    assert engineer.post("/api/gateway/keys", json={"name": _unique("eng")}).status_code == 200
