"""Secrets typed straight into a pipeline step's own settings stay secret.

Until November 2026 a password, key or signed URL typed into a step was kept in
clear in every version, the Bin and every run record, and every API response
that returned a pipeline sent it back to the browser. Now the value is encrypted
when the pipeline is saved, decrypted only as the step runs, and shown
everywhere else as a placeholder. Saving with the placeholder keeps the value.
"""

from __future__ import annotations

import hashlib
import json
import re
import urllib.error
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import revoluzen_flow.main as main_module
from revoluzen_flow.api import monitor as monitor_api
from revoluzen_flow.api import templates as templates_api
from revoluzen_flow.api import workflows as workflows_api
from revoluzen_flow.auth.deps import require_auth
from revoluzen_flow.connections.inline_secrets import SECRET_PLACEHOLDER
from revoluzen_flow.engine.parameters import (
    ParameterError,
    resolve_parameter_values,
    resolve_workflow_parameters,
)
from revoluzen_flow.ir import step_secrets as step_secrets_module
from revoluzen_flow.ir.schema import Step, StepType, Workflow, WorkflowParameter
from revoluzen_flow.ir.step_secrets import (
    STEP_SECRET_SETTINGS,
    blank_step_secrets,
    fill_stored_secrets,
    is_step_secret_setting,
    mask_parameter_values,
    mask_parameters,
    mask_step_params,
    open_step_params,
    parameter_name_says_secret,
    prepare_incoming_workflow,
    seal_parameter_values,
    seal_workflow,
)
from revoluzen_flow.ir.versioning import WorkflowStore, compute_workflow_hash
from revoluzen_flow.monitoring.store import ExecutionRecord, ExecutionStore
from revoluzen_flow.nodes.base import BaseNode
from revoluzen_flow.scheduling.models import Schedule
from revoluzen_flow.scheduling.store import ScheduleStore
from revoluzen_flow.security import migrate_existing
from revoluzen_flow.security.encryptor import fresh_encryptor_for_tests
from revoluzen_flow.security.migrate_existing import encrypt_pipeline_step_secrets
from revoluzen_flow.security.step_secret_routes import MaskRunSnapshotsRoute, MaskStepSecretsRoute
from revoluzen_flow.storage.database import Database

SECRET = "synthetic-smtp-password-9Z"
HEADER = "Bearer synthetic-header-token-4K"
WEBHOOK = "https://hooks.example.test/services/T000/B000/synthetic"
PLAIN_VALUES = (SECRET, HEADER, WEBHOOK)
REPO = Path(__file__).parents[2]
CONFIG_PANEL = REPO / "frontend" / "src" / "components" / "ConfigPanel.tsx"
MANIFESTS = REPO / "backend" / "revoluzen_flow" / "connectors" / "manifests"


def _pipeline(workflow_id: str = "wf1", **mail_settings) -> Workflow:
    return Workflow(
        id=workflow_id,
        name="Mail report",
        workspace_id="default",
        steps=[
            Step(
                id="mail", type=StepType.SEND_EMAIL, label="Mail",
                params={"to": "ops@example.test", "smtp_pass": SECRET, **mail_settings},
            ),
            Step(
                id="call", type=StepType.HTTP_REQUEST, label="Call",
                params={
                    "url": "https://api.example.test/orders",
                    "headers": [
                        {"key": "Accept", "value": "application/json"},
                        {"key": "Authorization", "value": HEADER},
                    ],
                },
            ),
            Step(id="ping", type=StepType.SLACK_NOTIFY, label="Ping", params={"webhook_url": WEBHOOK}),
        ],
    )


def _raw_versions(db, table: str = "workflow_versions") -> str:
    return " ".join(row["data"] for row in db.fetchall(f"SELECT data FROM {table}"))


def _assert_nothing_shows(text: str) -> None:
    for value in PLAIN_VALUES:
        assert value not in text
    assert "ENC:v1:" not in text


@pytest.fixture
def encryptor():
    return fresh_encryptor_for_tests()


@pytest.fixture
def db(tmp_path):
    database = Database(str(tmp_path / "revoluzen_flow.db"))
    yield database
    database.close()


@pytest.fixture
def store(db, encryptor, monkeypatch):
    workflow_store = WorkflowStore(db=db)
    execution_store = ExecutionStore(db=db)
    state = {"db": db, "encryptor": encryptor, "store": workflow_store, "execution_store": execution_store}
    for key, value in state.items():
        monkeypatch.setitem(main_module.app_state, key, value)
    return workflow_store


@pytest.fixture
def client(store):
    app = FastAPI()
    app.include_router(workflows_api.router)
    app.include_router(monitor_api.router)
    app.include_router(templates_api.router)
    user = SimpleNamespace(id="u1", email="tester@example.test", role="admin", is_active=True)
    app.dependency_overrides[require_auth] = lambda: user
    app.dependency_overrides[workflows_api._AUTHOR.dependency] = lambda: user
    app.dependency_overrides[workflows_api._ADMIN.dependency] = lambda: user
    app.dependency_overrides[workflows_api._safe_workspace_id] = lambda: "default"
    app.dependency_overrides[monitor_api._safe_workspace_id] = lambda: "default"
    app.dependency_overrides[templates_api._safe_workspace_id] = lambda: "default"
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def schedule_client(store, db):
    from revoluzen_flow.api import backfills as backfills_api
    from revoluzen_flow.api import schedules as schedules_api

    app = FastAPI()
    app.include_router(schedules_api.router)
    app.include_router(backfills_api.router)
    user = SimpleNamespace(id="u1", email="tester@example.test", role="admin", is_active=True)
    app.dependency_overrides[require_auth] = lambda: user
    app.dependency_overrides[schedules_api._AUTHOR.dependency] = lambda: user
    app.dependency_overrides[schedules_api._safe_workspace_id] = lambda: "default"
    app.dependency_overrides[backfills_api._safe_workspace_id] = lambda: "default"
    with TestClient(app) as test_client:
        yield test_client


def _saved_as_typed(store, workflow: Workflow, monkeypatch, encryptor):
    """Save a pipeline the way a server from before November 2026 did."""
    monkeypatch.setitem(main_module.app_state, "encryptor", None)
    version = store.save(workflow)
    monkeypatch.setitem(main_module.app_state, "encryptor", encryptor)
    return version


# ── Which settings count ─────────────────────────────────────────────────


@pytest.mark.parametrize("name", [
    "password", "smtp_pass", "smtp_password", "auth_value", "bearer_token", "api_key_value",
    "api_key", "token", "secret", "secret_key", "secret_access_key", "private_key",
    "account_key", "sas_token", "connection_string", "hmac_secret", "sasl_password",
    "webhook_url", "credentials_json", "client_secret", "access_token", "SMTP-Pass",
])
def test_a_setting_that_holds_a_secret_is_recognised(name):
    assert is_step_secret_setting(name)


@pytest.mark.parametrize("name", [
    "key", "merge_key", "merge_keys", "join_key", "data_key", "lookup_key", "main_key", "key_mode",
    "key_pairs", "key_field", "key_column", "key_columns", "primary_keys", "business_key",
    "idempotency_key", "surrogate_key_column", "access_key", "access_key_id", "hmac_key_id",
    "auth", "auth_type", "auth_header", "auth_user", "auth_template", "api_key_header",
    "sasl_mechanism", "sasl_username", "credential_id", "routing_key", "token_url", "max_tokens",
    "url", "smtp_user", "connection_id",
])
def test_an_ordinary_setting_is_left_alone(name):
    assert not is_step_secret_setting(name)


def test_every_password_setting_a_node_declares_is_covered():
    from revoluzen_flow.nodes.registry import _REGISTRY, get_registry

    get_registry()
    declared = {
        (cls.__name__, param["name"])
        for cls in _REGISTRY.values()
        for param in cls.param_schema()
        if param.get("type") == "password" or param.get("secret")
    }
    assert declared, "no node declares a password setting: the scan is broken"
    assert sorted(item for item in declared if not is_step_secret_setting(item[1])) == []


def test_every_secret_input_in_the_step_editor_is_covered():
    names = set(re.findall(r"<SecretInput value=\{params\.(\w+)", CONFIG_PANEL.read_text(encoding="utf-8")))
    assert {"smtp_pass", "webhook_url", "account_key"} <= names
    assert sorted(name for name in names if not is_step_secret_setting(name)) == []


def test_every_secret_a_connector_manifest_declares_is_covered():
    # These three are marked secret by a manifest but are account identifiers,
    # and as setting names they are far too common to treat as secrets.
    identifiers = {"username", "client_id", "account_sid"}
    declared: set[str] = set()

    def walk(node):
        if isinstance(node, dict):
            if node.get("secret") is True and isinstance(node.get("name"), str):
                declared.add(node["name"])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    for path in MANIFESTS.glob("*.json"):
        walk(json.loads(path.read_text(encoding="utf-8")))
    assert "api_key" in declared
    assert sorted(n for n in declared - identifiers if not is_step_secret_setting(n)) == []


# ── Stored encrypted ─────────────────────────────────────────────────────


def test_every_stored_copy_is_encrypted(store, db):
    store.save(_pipeline())
    raw = _raw_versions(db)
    for value in PLAIN_VALUES:
        assert value not in raw
    assert raw.count("ENC:v1:") == 3

    assert store.trash("wf1") is not None
    in_bin = _raw_versions(db, "workflow_trash")
    for value in PLAIN_VALUES:
        assert value not in in_bin
    assert "ENC:v1:" in in_bin


def test_the_callers_pipeline_is_encrypted_too(store):
    workflow = _pipeline()
    store.save(workflow)
    assert SECRET not in workflow.model_dump_json()


def test_a_reference_stays_readable_and_an_ordinary_setting_is_untouched(store, db):
    workflow = _pipeline()
    workflow.steps[0].params["smtp_pass"] = "{{ $vars.smtp_pass }}"
    workflow.steps[2].params["webhook_url"] = "${param.hook}"
    workflow.steps[0].params["merge_key"] = "order_id"
    store.save(workflow)
    raw = _raw_versions(db)
    assert "{{ $vars.smtp_pass }}" in raw
    assert "${param.hook}" in raw
    assert "order_id" in raw


def test_a_saved_pipeline_keeps_the_fingerprint_of_its_values(store):
    expected = compute_workflow_hash(_pipeline())
    store.save(_pipeline())
    assert compute_workflow_hash(store.get("wf1").workflow) == expected
    match, stored, recomputed = store.verify_version_hash("wf1", 1)
    assert match and stored == recomputed == expected


def test_a_changed_secret_changes_the_fingerprint(store):
    store.save(_pipeline())
    assert compute_workflow_hash(store.get("wf1").workflow) != compute_workflow_hash(
        _pipeline(smtp_pass="a-different-password"),
    )


# ── Keeping a stored secret ──────────────────────────────────────────────


def _editor_copy(store, workflow_id: str = "wf1") -> Workflow:
    """A stored pipeline as the editor holds it: every secret shown as dots."""
    data = store.get(workflow_id).workflow.model_dump(mode="json")
    data["steps"] = [{**s, "params": mask_step_params(s["params"])} for s in data["steps"]]
    return Workflow(**data)


def test_saving_the_placeholder_keeps_the_stored_value(store):
    store.save(_pipeline())
    before = store.get("wf1").workflow.steps[0].params["smtp_pass"]
    edited = _editor_copy(store)
    assert edited.steps[0].params["smtp_pass"] == SECRET_PLACEHOLDER
    store.save(edited)
    assert store.get("wf1", version=2).workflow.steps[0].params["smtp_pass"] == before


def test_a_new_value_replaces_the_stored_one(store, encryptor):
    store.save(_pipeline())
    edited = _editor_copy(store)
    edited.steps[0].params["smtp_pass"] = "a-new-password"
    store.save(edited)
    stored = store.get("wf1").workflow.steps[0].params["smtp_pass"]
    assert stored.startswith("ENC:v1:") and encryptor.decrypt_value(stored) == "a-new-password"


def test_a_placeholder_is_found_in_an_older_version(store, encryptor):
    store.save(_pipeline())
    edited = _editor_copy(store)
    edited.steps = edited.steps[1:]
    store.save(edited)

    restored = _pipeline(smtp_pass=SECRET_PLACEHOLDER)
    fill_stored_secrets(restored, store, "default")
    assert encryptor.decrypt_value(restored.steps[0].params["smtp_pass"]) == SECRET


def test_a_placeholder_with_nothing_stored_is_dropped(store):
    workflow = _pipeline("wf-new", smtp_pass=SECRET_PLACEHOLDER)
    workflow.steps[1].params["headers"][1]["value"] = SECRET_PLACEHOLDER
    store.save(workflow)
    params = store.get("wf-new").workflow.steps
    assert "smtp_pass" not in params[0].params
    assert params[1].params["headers"][1] == {"key": "Authorization", "value": ""}


def test_dots_typed_into_an_ordinary_setting_are_just_text(store):
    workflow = _pipeline()
    workflow.steps[0].params["replacement"] = SECRET_PLACEHOLDER
    workflow.steps[0].params["note"] = "stored text"
    store.save(workflow)

    assert store.get("wf1").workflow.steps[0].params["replacement"] == SECRET_PLACEHOLDER
    edited = _editor_copy(store)
    edited.steps[0].params["note"] = SECRET_PLACEHOLDER
    store.save(edited)
    params = store.get("wf1").workflow.steps[0].params
    assert params["replacement"] == SECRET_PLACEHOLDER
    assert params["note"] == SECRET_PLACEHOLDER


def test_a_placeholder_never_reads_another_workspaces_pipeline(store):
    store.save(_pipeline())
    intruder = _pipeline(smtp_pass=SECRET_PLACEHOLDER)
    fill_stored_secrets(intruder, store, "other-workspace")
    assert "smtp_pass" not in intruder.steps[0].params


def test_a_header_row_is_found_by_its_name_after_reordering(store, encryptor):
    store.save(_pipeline())
    edited = _editor_copy(store)
    edited.steps[1].params["headers"].reverse()
    assert edited.steps[1].params["headers"][0] == {"key": "Authorization", "value": SECRET_PLACEHOLDER}
    fill_stored_secrets(edited, store, "default")
    rows = edited.steps[1].params["headers"]
    assert rows[1] == {"key": "Accept", "value": "application/json"}
    assert encryptor.decrypt_value(rows[0]["value"]) == HEADER


def test_header_values_are_walked_by_name_in_a_plain_mapping(store, db):
    workflow = _pipeline()
    workflow.steps[1].params["headers"] = {"X-Api-Key": "synthetic-api-key-Q", "Accept": "text/csv"}
    store.save(workflow)
    raw = _raw_versions(db)
    assert "synthetic-api-key-Q" not in raw
    assert "text/csv" in raw


# ── Decrypted only when a step runs ──────────────────────────────────────


class _CaptureNode(BaseNode):
    def execute(self, ctx):
        return None


def test_a_node_gets_the_value_and_the_pipeline_stays_encrypted(store, encryptor):
    store.save(_pipeline())
    step = store.get("wf1").workflow.steps[0]
    node = _CaptureNode(step.params)
    assert node.params["smtp_pass"] == SECRET
    assert step.params["smtp_pass"].startswith("ENC:v1:")


def test_a_node_reads_encrypted_header_rows(store):
    store.save(_pipeline())
    node = _CaptureNode(store.get("wf1").workflow.steps[1].params)
    assert node.params["headers"][1]["value"] == HEADER


def test_settings_with_nothing_encrypted_are_passed_through_as_they_are():
    params = {"to": "ops@example.test", "headers": {"Accept": "text/csv"}}
    assert open_step_params(params) is params


def test_the_dots_are_never_used_as_the_password():
    node = _CaptureNode({"password": SECRET_PLACEHOLDER, "replacement": SECRET_PLACEHOLDER})
    assert node.params == {"password": "", "replacement": SECRET_PLACEHOLDER}


def test_a_pipeline_with_no_kept_secret_does_not_need_the_store():
    def no_store():
        raise AssertionError("the store was read")

    prepare_incoming_workflow(Workflow(id="w", name="No secrets"), no_store, "default")


def test_a_value_saved_with_another_key_gives_a_clear_error(encryptor, store):
    foreign = fresh_encryptor_for_tests().encrypt_value(SECRET)
    with pytest.raises(ValueError, match="enter it again") as caught:
        _CaptureNode({"smtp_pass": foreign})
    assert SECRET not in str(caught.value) and foreign not in str(caught.value)


def test_a_run_of_an_unsaved_pipeline_uses_the_kept_value_and_records_none(store, monkeypatch, tmp_path):
    from revoluzen_flow.engine.executor import WorkflowExecutor
    from revoluzen_flow.nodes.registry import _REGISTRY, get_registry

    seen: dict = {}

    class _Mail(BaseNode):
        def execute(self, ctx):
            seen.update(self.params)
            return ctx.conn.sql("SELECT 1 AS sent")

    get_registry()
    monkeypatch.setitem(_REGISTRY, StepType.SEND_EMAIL, _Mail)
    store.save(_pipeline())
    draft = _editor_copy(store)
    draft.steps = draft.steps[:1]
    draft.steps[0].params["cc"] = "typed-in-the-draft@example.test"
    draft.steps[0].params["token"] = "typed-but-never-saved"

    prepare_incoming_workflow(draft, lambda: store, "default")
    snapshot = draft.model_dump_json()
    assert SECRET not in snapshot and "typed-but-never-saved" not in snapshot

    result = WorkflowExecutor(data_dir=str(tmp_path), app_state=main_module.app_state).execute_workflow(draft, 10)
    assert result.status == "success"
    assert seen["smtp_pass"] == SECRET
    assert seen["token"] == "typed-but-never-saved"


def test_a_webhook_error_names_only_the_host(monkeypatch):
    from revoluzen_flow.nodes import sinks

    def fail(*_args, **_kwargs):
        raise urllib.error.URLError("refused")

    monkeypatch.setattr(sinks, "_get_input", lambda ctx, params: SimpleNamespace(columns=[]))
    monkeypatch.setattr(sinks, "_relation_to_dicts", lambda *args, **kwargs: [])
    monkeypatch.setattr(sinks, "_ssrf_check_url", lambda *args, **kwargs: None)
    monkeypatch.setattr(sinks, "_no_redirect_opener", lambda: SimpleNamespace(open=fail))
    node = sinks.WebhookSinkNode({"webhook_url": WEBHOOK})
    with pytest.raises(ValueError) as caught:
        node.execute(SimpleNamespace(conn=None))
    assert "hooks.example.test" in str(caught.value)
    assert "/services/" not in str(caught.value)


# ── Never sent back ──────────────────────────────────────────────────────


def test_no_pipeline_response_shows_a_secret(client, store):
    store.save(_pipeline())
    for url in (
        "/api/workflows/wf1",
        "/api/workflows/wf1/versions",
        "/api/workflows/wf1/export",
    ):
        response = client.get(url)
        assert response.status_code == 200, url
        _assert_nothing_shows(response.text)
    body = client.get("/api/workflows/wf1").json()
    assert body["workflow"]["steps"][0]["params"]["smtp_pass"] == SECRET_PLACEHOLDER
    assert body["workflow"]["steps"][1]["params"]["headers"][1]["value"] == SECRET_PLACEHOLDER
    assert body["workflow"]["steps"][1]["params"]["headers"][0]["value"] == "application/json"


def test_the_export_file_shows_the_placeholder(client, store):
    store.save(_pipeline())
    text = client.get("/api/workflows/wf1/export").text
    assert SECRET_PLACEHOLDER in text or "\\u2022" in text


def test_the_content_length_matches_the_masked_body(client, store):
    store.save(_pipeline())
    response = client.get("/api/workflows/wf1")
    assert int(response.headers["content-length"]) == len(response.content)


def test_the_editors_save_unchanged_adds_no_version(client, store):
    store.save(_pipeline())
    workflow = client.get("/api/workflows/wf1").json()["workflow"]
    response = client.put("/api/workflows/wf1", json={"workflow": workflow})
    assert response.status_code == 200, response.text
    _assert_nothing_shows(response.text)
    assert len(store.get_versions("wf1")) == 1


def test_changing_only_a_secret_adds_a_version(client, store, encryptor):
    store.save(_pipeline())
    workflow = client.get("/api/workflows/wf1").json()["workflow"]
    workflow["steps"][0]["params"]["smtp_pass"] = "a-new-password"
    response = client.put("/api/workflows/wf1", json={"workflow": workflow})
    assert response.status_code == 200, response.text
    assert "a-new-password" not in response.text
    assert len(store.get_versions("wf1")) == 2
    assert encryptor.decrypt_value(store.get("wf1").workflow.steps[0].params["smtp_pass"]) == "a-new-password"


def test_a_plan_does_not_report_kept_secrets_as_changed(client, store):
    store.save(_pipeline())
    workflow = client.get("/api/workflows/wf1").json()["workflow"]
    response = client.post("/api/workflows/wf1/plan", json={"workflow": workflow})
    assert response.status_code == 200, response.text
    _assert_nothing_shows(response.text)
    assert response.json()["diff"]["steps"]["modified"] == []


def test_a_run_record_shows_no_secret(client, store):
    executions = main_module.app_state["execution_store"]
    store.save(_pipeline())
    sealed = ExecutionRecord(
        id="run-sealed", workflow_id="wf1",
        workflow_snapshot=store.get("wf1").workflow.model_dump(mode="json"),
    )
    typed = ExecutionRecord(
        id="run-typed", workflow_id="wf1", workflow_snapshot=_pipeline().model_dump(mode="json"),
    )
    executions.record(sealed)
    executions.record(typed)

    for url in (
        "/api/monitor/executions/run-sealed",
        "/api/monitor/executions/run-typed",
        "/api/monitor/executions",
    ):
        response = client.get(url)
        assert response.status_code == 200, url
        _assert_nothing_shows(response.text)
    typed_body = client.get("/api/monitor/executions/run-typed").json()
    assert typed_body["workflow_snapshot"]["steps"][0]["params"]["smtp_pass"] == SECRET_PLACEHOLDER
    assert typed_body["workflow_snapshot"]["steps"][0]["params"]["to"] == "ops@example.test"


def test_the_template_export_shows_no_secret(client, store):
    store.save(_pipeline())
    response = client.post("/api/templates/export/wf1")
    assert response.status_code == 200, response.text
    _assert_nothing_shows(response.text)


def test_a_user_template_keeps_no_secret(client, db):
    response = client.post("/api/templates/user", json={
        "name": "Mail template",
        "steps": [
            {"type": "send_email", "params": {"to": "ops@example.test", "smtp_pass": SECRET}},
            {"type": "slack_notify", "params": {"webhook_url": "{{ $vars.hook }}"}},
        ],
    })
    assert response.status_code == 200, response.text
    assert SECRET not in response.text
    stored = db.fetchone("SELECT data FROM user_templates")["data"]
    assert SECRET not in stored
    steps = json.loads(stored)["steps"]
    assert steps[0]["params"] == {"to": "ops@example.test", "smtp_pass": ""}
    assert steps[1]["params"]["webhook_url"] == "{{ $vars.hook }}"


def test_blanking_counts_encrypted_values_and_placeholders(encryptor):
    steps, blanked = blank_step_secrets([
        {"params": {"password": encryptor.encrypt_value("x"), "token": SECRET_PLACEHOLDER, "url": "u"}},
    ])
    assert blanked == 2
    assert steps[0]["params"] == {"password": "", "token": "", "url": "u"}


@pytest.mark.parametrize("route", [MaskStepSecretsRoute, MaskRunSnapshotsRoute])
def test_the_route_masks_only_encrypted_values(route):
    body = b'{"typed":"plain","stored":"ENC:v1:gAAAAA-_=="}'
    cleaned = route.clean_body(body)
    assert b"ENC:v1:" not in cleaned
    assert b'"typed":"plain"' in cleaned
    assert route.clean_body(b'{"typed":"plain"}') == b'{"typed":"plain"}'


# ── Existing pipelines, at server start ──────────────────────────────────


def test_the_startup_pass_encrypts_versions_bin_and_templates(store, db, encryptor, monkeypatch):
    _saved_as_typed(store, _pipeline(), monkeypatch, encryptor)
    _saved_as_typed(store, _pipeline("wf-binned"), monkeypatch, encryptor)
    store.trash("wf-binned")
    db.execute(
        "INSERT INTO user_templates (id, workspace_id, name, data, created_at, updated_at) "
        "VALUES ('t1', 'default', 'T', ?, 'now', 'now')",
        (json.dumps({"steps": [{"type": "send_email", "params": {"smtp_pass": SECRET}}], "connections": []}),),
    )
    db.commit()
    assert SECRET in _raw_versions(db)

    report = encrypt_pipeline_step_secrets(db, encryptor)

    assert report["versions"] == 1 and report["bin"] == 1 and report["templates"] == 1
    for table in ("workflow_versions", "workflow_trash"):
        raw = _raw_versions(db, table)
        for value in PLAIN_VALUES:
            assert value not in raw
    assert SECRET not in db.fetchone("SELECT data FROM user_templates")["data"]
    node = _CaptureNode(store.get("wf1").workflow.steps[0].params)
    assert node.params["smtp_pass"] == SECRET


def test_the_startup_pass_is_safe_to_run_twice(store, db, encryptor, monkeypatch):
    _saved_as_typed(store, _pipeline(), monkeypatch, encryptor)
    encrypt_pipeline_step_secrets(db, encryptor)
    first = _raw_versions(db)
    report = encrypt_pipeline_step_secrets(db, encryptor)
    assert not any(report.values()), report
    assert _raw_versions(db) == first


def test_the_marker_saves_later_starts_from_reading_every_row(store, db, encryptor, monkeypatch):
    encrypt_pipeline_step_secrets(db, encryptor)
    _saved_as_typed(store, _pipeline(), monkeypatch, encryptor)
    assert encrypt_pipeline_step_secrets(db, encryptor)["versions"] == 0
    assert SECRET in _raw_versions(db)


def test_a_full_run_of_the_pass_does_not_touch_the_marker_when_a_row_fails(store, db, encryptor, monkeypatch):
    _saved_as_typed(store, _pipeline(), monkeypatch, encryptor)
    db.execute(
        "INSERT INTO workflow_versions (workflow_id, version, data, created_at, workspace_id) "
        "VALUES ('bad', 1, 'not json', 'now', 'default')",
    )
    db.commit()
    assert encrypt_pipeline_step_secrets(db, encryptor)["failed"] == 1
    marker = json.loads(db.fetchone("SELECT data FROM settings WHERE id = 'secret_migrations'")["data"])
    assert not marker.get("pipeline_secrets_done")
    assert SECRET not in _raw_versions(db).replace("not json", "")


def test_old_versions_still_verify_after_the_pass(store, db, encryptor, monkeypatch):
    _saved_as_typed(store, _pipeline(), monkeypatch, encryptor)
    stored_hash = store.get_versions("wf1")[0]["content_hash"]
    encrypt_pipeline_step_secrets(db, encryptor)
    match, stored, recomputed = store.verify_version_hash("wf1", 1)
    assert match and stored == recomputed == stored_hash


def test_one_secret_gets_one_token_across_versions(store, db, encryptor, monkeypatch):
    _saved_as_typed(store, _pipeline(), monkeypatch, encryptor)
    changed = _pipeline()
    changed.description = "edited"
    _saved_as_typed(store, changed, monkeypatch, encryptor)
    encrypt_pipeline_step_secrets(db, encryptor)
    assert store.diff("wf1", 1, 2)["modified_steps"] == []


def test_run_records_are_done_in_batches_across_starts(store, db, encryptor, monkeypatch):
    executions = main_module.app_state["execution_store"]
    for number in range(5):
        executions.record(ExecutionRecord(
            id=f"run-{number}", workflow_id="wf1", workflow_snapshot=_pipeline().model_dump(mode="json"),
        ))
    monkeypatch.setattr(migrate_existing, "_RUN_RECORD_BATCH", 2)

    assert encrypt_pipeline_step_secrets(db, encryptor, seconds=0)["runs"] == 2
    assert SECRET in " ".join(r["data"] for r in db.fetchall("SELECT data FROM executions"))
    assert encrypt_pipeline_step_secrets(db, encryptor, seconds=0)["runs"] == 2
    assert encrypt_pipeline_step_secrets(db, encryptor, seconds=0)["runs"] == 1
    assert SECRET not in " ".join(r["data"] for r in db.fetchall("SELECT data FROM executions"))
    assert encrypt_pipeline_step_secrets(db, encryptor, seconds=0)["runs"] == 0

    replay = _CaptureNode(executions.get("run-0").workflow_snapshot["steps"][0]["params"])
    assert replay.params["smtp_pass"] == SECRET


def test_the_pass_does_nothing_when_encryption_is_off(store, db, encryptor, monkeypatch):
    _saved_as_typed(store, _pipeline(), monkeypatch, encryptor)
    assert encrypt_pipeline_step_secrets(db, None)["versions"] == 0
    assert SECRET in _raw_versions(db)


def test_a_pipeline_with_no_secret_needs_no_encryptor(store, monkeypatch):
    monkeypatch.setitem(main_module.app_state, "encryptor", None)
    workflow = Workflow(id="plain", name="Plain", steps=[Step(id="a", type=StepType.SLACK_NOTIFY, params={"channel": "#x"})])
    seal_workflow(workflow)
    assert workflow.steps[0].params == {"channel": "#x"}


# ── Pipeline parameter defaults ──────────────────────────────────────────


TOKEN = "synthetic-parameter-token-8H"
DSN = "postgres://reader:synthetic@db.example.test/sales"
STORED_SECRETS_TS = REPO / "frontend" / "src" / "utils" / "storedSecrets.ts"


def _with_parameters(workflow_id: str = "wf-params") -> Workflow:
    return Workflow(
        id=workflow_id,
        name="Orders by API",
        workspace_id="default",
        steps=[
            Step(
                id="call", type=StepType.HTTP_REQUEST, label="Call",
                params={
                    "url": "https://api.example.test/orders?day=${param.day}",
                    "headers": [{"key": "Authorization", "value": "Bearer ${param.api_token}"}],
                    "conn": "${param.dsn}",
                },
            ),
        ],
        parameters=[
            WorkflowParameter(name="api_token", default=TOKEN),
            WorkflowParameter(name="dsn", default=DSN, secret=True),
            WorkflowParameter(name="batch_size", type="int", default=500),
            WorkflowParameter(name="day", default="2026-11-11"),
        ],
    )


def _defaults(store, workflow_id: str = "wf-params", version=None) -> dict:
    return {p.name: p.default for p in store.get(workflow_id, version=version).workflow.parameters}


def _editor_parameters(store, workflow_id: str = "wf-params") -> Workflow:
    data = store.get(workflow_id).workflow.model_dump(mode="json")
    data["parameters"] = mask_parameters(data["parameters"])
    return Workflow(**data)


@pytest.mark.parametrize("name", [
    "password", "db_password", "dbPassword", "api_key", "apiKey", "STRIPE_API_KEY", "github_token",
    "smtp_pass", "mysql_pass", "client_secret", "webhook_url", "connection_string",
    "aws_secret_access_key", "access_key", "signing_key", "my-secret",
])
def test_a_parameter_name_that_holds_a_secret_is_recognised(name):
    assert parameter_name_says_secret(name)


@pytest.mark.parametrize("name", [
    "batch_size", "dataset", "run_date", "region", "max_tokens", "token_limit", "token_url",
    "secret_name", "merge_key", "partition_key", "key", "access_key_id", "pass_rate", "password_file",
])
def test_an_ordinary_parameter_name_stays_readable(name):
    assert not parameter_name_says_secret(name)


def test_every_step_secret_name_counts_for_a_parameter_too():
    assert sorted(name for name in STEP_SECRET_SETTINGS if not parameter_name_says_secret(name)) == []


def test_the_parameters_dialog_uses_the_same_lists_as_the_server():
    text = STORED_SECRETS_TS.read_text(encoding="utf-8")

    def listed(constant: str) -> set:
        return set(re.findall(r"'([a-z0-9_]+)'", re.search(rf"const {constant} = new Set\(\[(.*?)\]\);", text).group(1)))

    assert listed("PARAMETER_SECRET_WORDS") == set(step_secrets_module._PARAMETER_SECRET_WORDS)
    assert listed("PARAMETER_SECRET_LAST_WORDS") == set(step_secrets_module._PARAMETER_SECRET_LAST_WORDS)
    assert listed("PARAMETER_NOT_SECRET_LAST_WORDS") == set(step_secrets_module._PARAMETER_NOT_SECRET_LAST_WORDS)
    endings = re.search(r"const PARAMETER_SECRET_ENDINGS = /\((.+?)\)\$/;", text).group(1)
    assert set(endings.split("|")) == set(step_secrets_module._PARAMETER_SECRET_ENDINGS)


def test_a_secret_default_is_stored_encrypted_and_an_ordinary_one_is_not(store, db):
    store.save(_with_parameters())
    raw = _raw_versions(db)
    assert TOKEN not in raw and DSN not in raw
    defaults = _defaults(store)
    assert defaults["api_token"].startswith("ENC:v1:") and defaults["dsn"].startswith("ENC:v1:")
    assert defaults["batch_size"] == 500 and defaults["day"] == "2026-11-11"

    store.trash("wf-params")
    in_bin = _raw_versions(db, "workflow_trash")
    assert TOKEN not in in_bin and DSN not in in_bin


def test_a_reference_in_a_secret_default_stays_readable(store):
    workflow = _with_parameters()
    workflow.parameters[0].default = "${utcnow:%Y}"
    store.save(workflow)
    assert _defaults(store)["api_token"] == "${utcnow:%Y}"


def test_a_default_keeps_the_fingerprint_of_its_value(store):
    expected = compute_workflow_hash(_with_parameters())
    store.save(_with_parameters())
    assert store.verify_version_hash("wf-params", 1) == (True, expected, expected)


def test_the_secret_switch_leaves_old_fingerprints_alone():
    def as_hashed_before_the_switch(workflow: Workflow) -> str:
        data = workflow.model_dump(mode="json")
        for field in (
            "status", "updated_at", "deployed_at", "deployed_by", "deployed_version",
            "published_at", "published_by", "rollback_from", "test_results",
            "published_version", "has_unpublished_changes",
        ):
            data.pop(field, None)
        for parameter in data["parameters"]:
            parameter.pop("secret", None)
        return hashlib.sha256(
            json.dumps(data, sort_keys=True, default=str, separators=(",", ":")).encode("utf-8"),
        ).hexdigest()

    workflow = _with_parameters()
    workflow.parameters[1].secret = False
    assert compute_workflow_hash(workflow) == as_hashed_before_the_switch(workflow)


def test_turning_the_secret_switch_on_counts_as_a_change():
    plain, marked = _with_parameters(), _with_parameters()
    marked.parameters[3].secret = True
    assert compute_workflow_hash(plain) != compute_workflow_hash(marked)


def test_saving_dots_keeps_a_stored_default(store):
    store.save(_with_parameters())
    before = _defaults(store)
    edited = _editor_parameters(store)
    assert edited.parameters[0].default == SECRET_PLACEHOLDER
    store.save(edited)
    assert _defaults(store, version=2) == before


def test_a_new_default_replaces_the_stored_one(store, encryptor):
    store.save(_with_parameters())
    edited = _editor_parameters(store)
    edited.parameters[0].default = "a-new-token"
    store.save(edited)
    assert encryptor.decrypt_value(_defaults(store)["api_token"]) == "a-new-token"


def test_a_default_is_found_in_an_older_version(store, encryptor):
    store.save(_with_parameters())
    edited = _editor_parameters(store)
    edited.parameters = edited.parameters[2:]
    store.save(edited)

    restored = _with_parameters()
    restored.parameters[0].default = SECRET_PLACEHOLDER
    fill_stored_secrets(restored, store, "default")
    assert encryptor.decrypt_value(restored.parameters[0].default) == TOKEN


def test_dots_with_nothing_stored_clear_a_secret_default(store):
    workflow = _with_parameters("wf-empty")
    workflow.parameters[0].default = SECRET_PLACEHOLDER
    store.save(workflow)
    assert _defaults(store, "wf-empty")["api_token"] is None


def test_dots_in_an_ordinary_default_are_just_text(store):
    workflow = _with_parameters()
    workflow.parameters[3].default = SECRET_PLACEHOLDER
    store.save(workflow)
    assert _defaults(store)["day"] == SECRET_PLACEHOLDER


def test_a_default_never_comes_from_another_workspace(store):
    store.save(_with_parameters())
    intruder = _with_parameters()
    intruder.parameters[0].default = SECRET_PLACEHOLDER
    fill_stored_secrets(intruder, store, "other-workspace")
    assert intruder.parameters[0].default is None


def test_a_run_gets_the_value_inside_a_longer_setting(store):
    store.save(_with_parameters())
    stored = store.get("wf-params").workflow
    resolved = resolve_workflow_parameters(stored, {})
    assert resolved.steps[0].params["headers"][0]["value"] == f"Bearer {TOKEN}"
    assert resolved.steps[0].params["conn"] == DSN
    assert stored.parameters[0].default.startswith("ENC:v1:")
    assert "${param.api_token}" in stored.steps[0].params["headers"][0]["value"]
    assert resolved.metadata["_resolved_parameters"]["api_token"] == SECRET_PLACEHOLDER
    assert resolved.metadata["_resolved_parameters"]["dsn"] == SECRET_PLACEHOLDER
    assert resolved.metadata["_resolved_parameters"]["batch_size"] == 500


def test_an_override_replaces_the_default(store):
    store.save(_with_parameters())
    values = resolve_parameter_values(store.get("wf-params").workflow.parameters, {"api_token": "typed-for-this-run"})
    assert values["api_token"] == "typed-for-this-run"


def test_dots_as_an_override_mean_the_stored_default(store):
    store.save(_with_parameters())
    values = resolve_parameter_values(store.get("wf-params").workflow.parameters, {"api_token": SECRET_PLACEHOLDER})
    assert values["api_token"] == TOKEN


def test_dots_for_an_ordinary_parameter_are_a_value():
    values = resolve_parameter_values([WorkflowParameter(name="mask", default="x")], {"mask": SECRET_PLACEHOLDER})
    assert values["mask"] == SECRET_PLACEHOLDER


def test_dots_never_stand_in_for_a_required_secret():
    with pytest.raises(ParameterError, match="api_token"):
        resolve_parameter_values(
            [WorkflowParameter(name="api_token", required=True)], {"api_token": SECRET_PLACEHOLDER},
        )


def test_a_replay_can_read_the_encrypted_value_it_was_given(store, encryptor):
    store.save(_with_parameters())
    values = resolve_parameter_values(
        store.get("wf-params").workflow.parameters, {"api_token": encryptor.encrypt_value("from-the-first-run")},
    )
    assert values["api_token"] == "from-the-first-run"


def test_a_value_of_the_wrong_type_is_not_quoted_in_the_error():
    with pytest.raises(ParameterError) as caught:
        resolve_parameter_values([WorkflowParameter(name="api_token", type="int")], {"api_token": TOKEN})
    assert "api_token" in str(caught.value) and TOKEN not in str(caught.value)


def test_a_default_saved_with_another_key_names_the_parameter_only(store):
    foreign = fresh_encryptor_for_tests().encrypt_value(TOKEN)
    with pytest.raises(ParameterError, match="api_token.*enter it again") as caught:
        resolve_parameter_values([WorkflowParameter(name="api_token", default=foreign)], {})
    assert TOKEN not in str(caught.value) and foreign not in str(caught.value)


def test_no_pipeline_response_shows_a_secret_default(client, store):
    store.save(_with_parameters())
    for url in ("/api/workflows/wf-params", "/api/workflows/wf-params/export"):
        response = client.get(url)
        assert response.status_code == 200, url
        assert TOKEN not in response.text and DSN not in response.text and "ENC:v1:" not in response.text
    parameters = client.get("/api/workflows/wf-params").json()["workflow"]["parameters"]
    shown = {p["name"]: p["default"] for p in parameters}
    assert shown["api_token"] == SECRET_PLACEHOLDER and shown["dsn"] == SECRET_PLACEHOLDER
    assert shown["batch_size"] == 500 and shown["day"] == "2026-11-11"


def test_the_export_masks_a_secret_default_still_as_typed():
    shown = mask_parameters([{"name": "api_token", "default": TOKEN}, {"name": "day", "default": "x"}])
    assert shown[0]["default"] == SECRET_PLACEHOLDER and shown[1]["default"] == "x"


def test_the_editors_save_unchanged_keeps_the_default_and_adds_no_version(client, store, encryptor):
    store.save(_with_parameters())
    workflow = client.get("/api/workflows/wf-params").json()["workflow"]
    response = client.put("/api/workflows/wf-params", json={"workflow": workflow})
    assert response.status_code == 200, response.text
    assert len(store.get_versions("wf-params")) == 1
    assert encryptor.decrypt_value(_defaults(store)["api_token"]) == TOKEN


def test_changing_only_a_default_adds_a_version(client, store, encryptor):
    store.save(_with_parameters())
    workflow = client.get("/api/workflows/wf-params").json()["workflow"]
    workflow["parameters"][0]["default"] = "a-new-token"
    response = client.put("/api/workflows/wf-params", json={"workflow": workflow})
    assert response.status_code == 200, response.text
    assert "a-new-token" not in response.text
    assert len(store.get_versions("wf-params")) == 2
    assert encryptor.decrypt_value(_defaults(store)["api_token"]) == "a-new-token"


def test_an_unsaved_run_uses_the_kept_default(store):
    store.save(_with_parameters())
    draft = _editor_parameters(store)
    prepare_incoming_workflow(draft, lambda: store, "default")
    assert resolve_parameter_values(draft.parameters, {})["api_token"] == TOKEN


def test_a_run_value_is_encrypted_for_the_record_and_masked_for_show(encryptor, store):
    declared = _with_parameters().parameters
    values = {"api_token": "typed", "dsn": "typed-dsn", "day": "2026-01-01", "note_token": "typed-too"}
    kept = seal_parameter_values(values, declared)
    assert encryptor.decrypt_value(kept["api_token"]) == "typed"
    assert encryptor.decrypt_value(kept["dsn"]) == "typed-dsn"
    assert encryptor.decrypt_value(kept["note_token"]) == "typed-too"
    assert kept["day"] == "2026-01-01"
    assert seal_parameter_values({"day": "x"}, declared) == {"day": "x"}
    for shown in (mask_parameter_values(values, declared), mask_parameter_values(kept, declared)):
        assert shown == {
            "api_token": SECRET_PLACEHOLDER, "dsn": SECRET_PLACEHOLDER,
            "day": "2026-01-01", "note_token": SECRET_PLACEHOLDER,
        }


def test_a_real_run_keeps_no_typed_value_in_its_record_or_alert(store, monkeypatch, tmp_path):
    import asyncio

    from revoluzen_flow.api import execution as execution_api
    from revoluzen_flow.nodes.registry import _REGISTRY, get_registry

    seen: dict = {}
    alerts: dict = {}

    class _Mail(BaseNode):
        def execute(self, ctx):
            seen.update(self.params)
            return ctx.conn.sql("SELECT 1 AS sent")

    get_registry()
    monkeypatch.setitem(_REGISTRY, StepType.SEND_EMAIL, _Mail)
    monkeypatch.setitem(main_module.app_state, "data_dir", str(tmp_path))
    monkeypatch.setattr(execution_api, "_admit", lambda _state, **kwargs: kwargs["fn"]())
    monkeypatch.setattr(execution_api, "_trigger_pipeline_alerts", lambda **kwargs: alerts.update(kwargs))

    workflow = Workflow(
        id="wf-run", name="Mail by parameter", workspace_id="default",
        steps=[Step(id="mail", type=StepType.SEND_EMAIL, params={"to": "ops@example.test", "token": "${param.api_token}"})],
        parameters=[WorkflowParameter(name="api_token", default=TOKEN)],
    )
    version = store.save(workflow)

    result = asyncio.run(execution_api.run_saved_workflow(
        version, workflow_id="wf-run", workspace_id="default",
        parameter_values={"api_token": "typed-for-this-run"}, full_run=True,
    ))

    assert result["status"] == "success", result
    assert seen["token"] == "typed-for-this-run"
    record = main_module.app_state["execution_store"].get(result["execution_id"])
    text = record.model_dump_json()
    assert "typed-for-this-run" not in text and TOKEN not in text
    assert record.metadata["parameter_values"]["api_token"].startswith("ENC:v1:")
    assert alerts["parameter_values"] == {"api_token": SECRET_PLACEHOLDER}


def test_a_run_record_from_before_shows_no_parameter_secret(client, store):
    executions = main_module.app_state["execution_store"]
    executions.record(ExecutionRecord(
        id="run-old", workflow_id="wf-params",
        workflow_snapshot=_with_parameters().model_dump(mode="json"),
        metadata={"parameter_values": {"dsn": "typed-dsn", "day": "2026-01-01"}},
    ))
    for url in ("/api/monitor/executions/run-old", "/api/monitor/executions"):
        response = client.get(url)
        assert response.status_code == 200, url
        assert TOKEN not in response.text and DSN not in response.text and "typed-dsn" not in response.text
    body = client.get("/api/monitor/executions/run-old").json()
    assert body["metadata"]["parameter_values"] == {"dsn": SECRET_PLACEHOLDER, "day": "2026-01-01"}
    assert body["workflow_snapshot"]["parameters"][3]["default"] == "2026-11-11"


def test_the_startup_pass_encrypts_defaults_and_what_runs_were_given(store, db, encryptor, monkeypatch):
    _saved_as_typed(store, _with_parameters(), monkeypatch, encryptor)
    stored_hash = store.get_versions("wf-params")[0]["content_hash"]
    main_module.app_state["execution_store"].record(ExecutionRecord(
        id="run-old", workflow_id="wf-params",
        workflow_snapshot=_with_parameters().model_dump(mode="json"),
        metadata={"parameter_values": {"dsn": "typed-dsn", "day": "2026-01-01"}},
    ))
    assert TOKEN in _raw_versions(db)

    report = encrypt_pipeline_step_secrets(db, encryptor)

    assert report["versions"] == 1 and report["runs"] == 1 and report["failed"] == 0
    raw = _raw_versions(db)
    assert TOKEN not in raw and DSN not in raw
    run_row = db.fetchone("SELECT data FROM executions WHERE id = 'run-old'")["data"]
    assert TOKEN not in run_row and DSN not in run_row and "typed-dsn" not in run_row
    assert "2026-01-01" in run_row
    assert store.verify_version_hash("wf-params", 1) == (True, stored_hash, stored_hash)
    assert resolve_parameter_values(store.get("wf-params").workflow.parameters, {})["api_token"] == TOKEN

    first = _raw_versions(db)
    assert not any(encrypt_pipeline_step_secrets(db, encryptor).values())
    assert _raw_versions(db) == first


# ── Parameter values saved on schedules and backfills ────────────────────


SAVED_VALUES = {"api_token": "typed-token-S", "dsn": "typed-dsn-S", "day": "2026-01-01", "run_date": "${utcnow:%Y}"}


@pytest.fixture
def schedules(store, db, monkeypatch):
    schedule_store = ScheduleStore(db=db)
    monkeypatch.setitem(main_module.app_state, "schedule_store", schedule_store)
    return schedule_store


def _raw_rows(db, table: str) -> str:
    return " ".join(row["data"] for row in db.fetchall(f"SELECT data FROM {table}"))


def _assert_typed_values_gone(text: str) -> None:
    assert "typed-token-S" not in text and "typed-dsn-S" not in text


def test_a_schedule_keeps_a_secret_value_encrypted(store, schedules, db, encryptor):
    store.save(_with_parameters())
    saved = schedules.create(Schedule(id="s1", workflow_id="wf-params", parameter_values=dict(SAVED_VALUES)))
    _assert_typed_values_gone(_raw_rows(db, "schedules"))
    _assert_typed_values_gone(saved.model_dump_json())
    values = schedules.get("s1").parameter_values
    assert encryptor.decrypt_value(values["api_token"]) == "typed-token-S"
    assert encryptor.decrypt_value(values["dsn"]) == "typed-dsn-S"
    assert values["day"] == "2026-01-01" and values["run_date"] == "${utcnow:%Y}"


def test_a_scheduled_run_gets_the_value(store, schedules):
    store.save(_with_parameters())
    schedules.create(Schedule(id="s1", workflow_id="wf-params", parameter_values=dict(SAVED_VALUES)))
    declared = store.get("wf-params").workflow.parameters + [WorkflowParameter(name="run_date")]
    values = resolve_parameter_values(declared, schedules.get("s1").parameter_values)
    assert values["api_token"] == "typed-token-S" and values["dsn"] == "typed-dsn-S"


def test_saving_a_schedule_with_dots_keeps_the_value(store, schedules):
    store.save(_with_parameters())
    schedules.create(Schedule(id="s1", workflow_id="wf-params", parameter_values=dict(SAVED_VALUES)))
    before = schedules.get("s1").parameter_values
    schedules.update("s1", {"parameter_values": {
        "api_token": SECRET_PLACEHOLDER, "dsn": "a-new-dsn", "day": "2026-02-02",
    }})
    after = schedules.get("s1").parameter_values
    assert after["api_token"] == before["api_token"]
    assert after["dsn"].startswith("ENC:v1:") and after["dsn"] != before["dsn"]
    assert after["day"] == "2026-02-02"


def test_dots_on_a_schedule_with_nothing_stored_are_dropped(store, schedules):
    store.save(_with_parameters())
    schedules.create(Schedule(id="s1", workflow_id="wf-params", parameter_values={"day": "x"}))
    schedules.update("s1", {"parameter_values": {"api_token": SECRET_PLACEHOLDER, "day": "y"}})
    assert schedules.get("s1").parameter_values == {"day": "y"}


def test_dots_for_an_ordinary_schedule_value_are_just_text(store, schedules):
    store.save(_with_parameters())
    schedules.create(Schedule(id="s1", workflow_id="wf-params", parameter_values={"day": "x"}))
    schedules.update("s1", {"parameter_values": {"day": SECRET_PLACEHOLDER}})
    assert schedules.get("s1").parameter_values == {"day": SECRET_PLACEHOLDER}


def test_recording_a_scheduled_run_leaves_the_value_alone(store, schedules):
    store.save(_with_parameters())
    schedules.create(Schedule(id="s1", workflow_id="wf-params", parameter_values=dict(SAVED_VALUES)))
    before = schedules.get("s1").parameter_values
    schedules.record_run("s1", "success")
    assert schedules.get("s1").parameter_values == before


def test_the_schedules_api_shows_dots_and_keeps_the_value(store, schedules, schedule_client, encryptor):
    store.save(_with_parameters())
    created = schedule_client.post("/api/schedules/", json={
        "workflow_id": "wf-params", "parameter_values": SAVED_VALUES, "acknowledge_side_effects": True,
    })
    assert created.status_code == 200, created.text
    schedule_id = created.json()["id"]
    for response in (created, schedule_client.get(f"/api/schedules/{schedule_id}"), schedule_client.get("/api/schedules/")):
        assert response.status_code == 200
        _assert_typed_values_gone(response.text)
        assert "ENC:v1:" not in response.text

    shown = schedule_client.get(f"/api/schedules/{schedule_id}").json()["parameter_values"]
    assert shown["api_token"] == SECRET_PLACEHOLDER and shown["day"] == "2026-01-01"

    updated = schedule_client.put(f"/api/schedules/{schedule_id}", json={"parameter_values": shown})
    assert updated.status_code == 200, updated.text
    assert encryptor.decrypt_value(schedules.get(schedule_id).parameter_values["api_token"]) == "typed-token-S"


def test_an_export_with_schedules_shows_dots(store, schedules, encryptor):
    store.save(_with_parameters())
    schedules.create(Schedule(id="s1", workflow_id="wf-params", parameter_values=dict(SAVED_VALUES)))
    payload = workflows_api.build_pipeline_export_payload(
        store.get("wf-params").workflow, "default", include_schedules=True,
    )
    text = json.dumps(payload, default=str)
    _assert_typed_values_gone(text)
    assert "ENC:v1:" not in text
    assert payload["schedules"][0]["parameter_values"]["dsn"] == SECRET_PLACEHOLDER
    assert payload["schedules"][0]["parameter_values"]["day"] == "2026-01-01"


def test_a_binned_schedule_comes_back_with_its_value(store, schedules, db, encryptor, tmp_path):
    from revoluzen_flow.ir.workflow_bin import delete_pipeline, restore_pipeline

    store.save(_with_parameters())
    schedules.create(Schedule(id="s1", workflow_id="wf-params", parameter_values=dict(SAVED_VALUES)))

    delete_pipeline("wf-params", "default", store=store, schedule_store=schedules, data_dir=str(tmp_path))
    assert schedules.get("s1") is None
    _assert_typed_values_gone(db.fetchone("SELECT schedules FROM workflow_trash_meta")["schedules"])

    restore_pipeline("wf-params", "default", store=store, schedule_store=schedules)
    assert encryptor.decrypt_value(schedules.get("s1").parameter_values["api_token"]) == "typed-token-S"


def test_a_backfill_keeps_a_secret_value_encrypted(store, schedule_client, db, encryptor, monkeypatch):
    from revoluzen_flow.api import backfills as backfills_api
    from revoluzen_flow.backfills.store import get_backfill_store

    backfill_store = get_backfill_store()
    monkeypatch.setattr(backfill_store, "_db", db)
    dispatched: dict = {}
    monkeypatch.setattr(backfills_api, "run_backfill_async", lambda *args, **kwargs: dispatched.update(kwargs))
    store.save(_with_parameters())

    created = schedule_client.post("/api/executions/backfill", json={
        "pipeline_id": "wf-params", "start_date": "2026-01-01", "end_date": "2026-01-02",
        "parameter_values": {"api_token": "typed-token-S", "day": "2026-01-01"},
        "acknowledge_side_effects": True, "acknowledge_no_cursor_usage": True,
    })
    assert created.status_code == 200, created.text
    _assert_typed_values_gone(_raw_rows(db, "backfill_runs"))
    assert dispatched["extra_params"]["api_token"] == "typed-token-S", "the run needs the value"

    backfill_id = created.json()["backfill_id"]
    for url in ("/api/executions/backfill", f"/api/executions/backfill/{backfill_id}"):
        response = schedule_client.get(url)
        assert response.status_code == 200, url
        _assert_typed_values_gone(response.text)
        assert "ENC:v1:" not in response.text
    parent = schedule_client.get(f"/api/executions/backfill/{backfill_id}").json()["backfill"]
    assert parent["params_template"] == {"api_token": SECRET_PLACEHOLDER, "day": "2026-01-01"}


def test_each_backfill_window_records_the_value_encrypted(store, db, encryptor, monkeypatch, tmp_path):
    from revoluzen_flow.backfills.models import BackfillRun
    from revoluzen_flow.backfills.orchestrator import _run_one_window
    from revoluzen_flow.backfills.store import BackfillStore
    from revoluzen_flow.engine.executor import WorkflowExecutor
    from revoluzen_flow.nodes.registry import _REGISTRY, get_registry

    seen: dict = {}

    class _Mail(BaseNode):
        def execute(self, ctx):
            seen.update(self.params)
            return ctx.conn.sql("SELECT 1 AS sent")

    get_registry()
    monkeypatch.setitem(_REGISTRY, StepType.SEND_EMAIL, _Mail)
    workflow = Workflow(
        id="wf-window", name="Window mail", workspace_id="default",
        steps=[Step(id="mail", type=StepType.SEND_EMAIL, params={"to": "ops@example.test", "token": "${param.api_token}"})],
        parameters=[WorkflowParameter(name="api_token"), WorkflowParameter(name="window_start"), WorkflowParameter(name="window_end")],
    )
    backfills = BackfillStore(db=db)
    parent = BackfillRun(pipeline_id="wf-window", window_start="2026-01-01", window_end="2026-01-02")
    backfills.create_parent(parent)
    child = BackfillRun(pipeline_id="wf-window", window_start="2026-01-01", window_end="2026-01-02")
    backfills.create_children(parent.id, [child])

    _run_one_window(
        parent, child,
        executor=WorkflowExecutor(data_dir=str(tmp_path), app_state=main_module.app_state),
        workflow=workflow, exe_store=main_module.app_state["execution_store"], store=backfills,
        extra_params={"api_token": "typed-token-S"},
    )

    assert seen["token"] == "typed-token-S"
    _assert_typed_values_gone(_raw_rows(db, "backfill_runs"))
    assert backfills.get(child.id).params_template["api_token"].startswith("ENC:v1:")


def test_the_startup_pass_encrypts_schedules_backfills_and_the_bin(store, schedules, db, encryptor, monkeypatch):
    from revoluzen_flow.backfills.models import BackfillRun
    from revoluzen_flow.backfills.store import BackfillStore

    _saved_as_typed(store, _with_parameters(), monkeypatch, encryptor)
    _saved_as_typed(store, _with_parameters("wf-binned"), monkeypatch, encryptor)
    monkeypatch.setitem(main_module.app_state, "encryptor", None)
    schedules.create(Schedule(id="s1", workflow_id="wf-params", parameter_values=dict(SAVED_VALUES)))
    schedules.create(Schedule(id="s2", workflow_id="wf-binned", parameter_values=dict(SAVED_VALUES)))
    store.trash("wf-binned", schedules=list(schedules.list_by_workflow("wf-binned")))
    schedules.delete("s2")
    monkeypatch.setitem(main_module.app_state, "encryptor", encryptor)
    BackfillStore(db=db).create_parent(BackfillRun(
        id="b1", pipeline_id="wf-params", window_start="2026-01-01", window_end="2026-01-02",
        params_template=dict(SAVED_VALUES),
    ))
    assert "typed-token-S" in _raw_rows(db, "schedules") and "typed-dsn-S" in _raw_rows(db, "backfill_runs")

    report = encrypt_pipeline_step_secrets(db, encryptor)

    assert report["schedules"] == 2 and report["backfills"] == 1 and report["failed"] == 0
    _assert_typed_values_gone(_raw_rows(db, "schedules"))
    _assert_typed_values_gone(_raw_rows(db, "backfill_runs"))
    _assert_typed_values_gone(db.fetchone("SELECT schedules FROM workflow_trash_meta")["schedules"])
    assert encryptor.decrypt_value(schedules.get("s1").parameter_values["dsn"]) == "typed-dsn-S"
    assert schedules.get("s1").parameter_values["day"] == "2026-01-01"

    first = _raw_rows(db, "schedules")
    again = encrypt_pipeline_step_secrets(db, encryptor)
    assert again["schedules"] == 0 and again["backfills"] == 0
    assert _raw_rows(db, "schedules") == first


def test_a_server_past_the_earlier_passes_still_does_saved_values(store, schedules, db, encryptor, monkeypatch):
    _saved_as_typed(store, _with_parameters(), monkeypatch, encryptor)
    monkeypatch.setitem(main_module.app_state, "encryptor", None)
    schedules.create(Schedule(id="s1", workflow_id="wf-params", parameter_values=dict(SAVED_VALUES)))
    monkeypatch.setitem(main_module.app_state, "encryptor", encryptor)
    db.execute(
        "INSERT INTO settings (id, data, created_at, updated_at) VALUES ('secret_migrations', ?, 'now', 'now')",
        (json.dumps({"pipeline_secrets_done": True, "run_secrets_done": True}),),
    )
    db.commit()
    assert encrypt_pipeline_step_secrets(db, encryptor)["schedules"] == 1
    _assert_typed_values_gone(_raw_rows(db, "schedules"))


def test_a_server_that_ran_the_step_only_pass_still_does_the_defaults(store, db, encryptor, monkeypatch):
    _saved_as_typed(store, _with_parameters(), monkeypatch, encryptor)
    db.execute(
        "INSERT INTO settings (id, data, created_at, updated_at) VALUES ('secret_migrations', ?, 'now', 'now')",
        (json.dumps({"step_secrets_done": True, "run_records_done": True, "run_records_after": 99}),),
    )
    db.commit()
    assert encrypt_pipeline_step_secrets(db, encryptor)["versions"] == 1
    assert TOKEN not in _raw_versions(db)
