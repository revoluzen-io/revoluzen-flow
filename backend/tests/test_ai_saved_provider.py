"""AI paths must honour the provider saved in the app, and trust OS roots."""

from __future__ import annotations

import ssl
from types import SimpleNamespace

import pytest

from revoluzen_flow.ai import anthropic_client
from revoluzen_flow.ai.fast_router import _try_atlas_match
from revoluzen_flow.ai.single_shot import should_use_single_shot
from revoluzen_flow.api import planner
from revoluzen_flow.ir.schema import Step, StepType
from revoluzen_flow.planner import ai_client


def test_is_ai_available_consults_saved_config(monkeypatch):
    seen = {}

    def fake_resolve(user_id=None, workspace_id=None):
        seen.update(user_id=user_id, workspace_id=workspace_id)
        return ("claude", "key", "model", "")

    monkeypatch.setattr(ai_client, "resolve_provider", fake_resolve)
    assert ai_client.is_ai_available(user_id="u1", workspace_id="w1") is True
    assert seen == {"user_id": "u1", "workspace_id": "w1"}


@pytest.mark.asyncio
async def test_planner_chat_passes_caller_to_generation(monkeypatch):
    seen = {}

    async def fake_generate(messages, user_id=None, workspace_id=None):
        seen.update(user_id=user_id, workspace_id=workspace_id)
        return {"explanation": "drafted by the model", "steps": []}

    monkeypatch.setattr(planner, "ai_generate_pipeline", fake_generate)
    monkeypatch.setattr(planner, "is_ai_available", lambda user_id=None, workspace_id=None: True)
    monkeypatch.setattr(planner, "current_user_optional", lambda request: SimpleNamespace(id="u1"))
    # This workspace has no orders.csv, and that isn't what's under test here.
    monkeypatch.setattr(planner, "read_workspace_inventory", lambda workspace_id: None)

    reply = await planner.chat(
        [planner.ChatMessage(role="user", content="Build a pipeline: read orders.csv and write parquet")],
        request=SimpleNamespace(),
        workspace_id="w1",
    )

    assert seen == {"user_id": "u1", "workspace_id": "w1"}
    assert reply["ai_powered"] is True


@pytest.mark.parametrize(
    "prompt",
    [
        "Explain what this pipeline does, node by node",
        "Walk me through my pipeline",
        "Describe the current canvas",
    ],
)
def test_own_pipeline_questions_skip_static_atlas(prompt):
    assert _try_atlas_match(prompt) is None


@pytest.mark.parametrize(
    "prompt",
    ["Why did the Revenue Rollup pipeline fail?", "Explain what this pipeline does"],
)
def test_live_data_questions_get_tools(prompt):
    assert should_use_single_shot(prompt) is False


def test_general_reasoning_question_stays_single_shot():
    assert should_use_single_shot("what is the difference between a join and a lookup") is True


class _RecordingStore:
    def __init__(self):
        self.saved = []

    def save(self, workflow, change_summary=""):
        self.saved.append(workflow)


@pytest.mark.asyncio
async def test_preview_chat_saves_nothing(monkeypatch):
    from revoluzen_flow.main import app_state

    store = _RecordingStore()
    monkeypatch.setitem(app_state, "store", store)

    async def fake_generate(messages, user_id=None, workspace_id=None):
        return {"name": "Draft", "steps": [{"type": "csv_source", "label": "Read"}], "connections": []}

    monkeypatch.setattr(planner, "ai_generate_pipeline", fake_generate)
    monkeypatch.setattr(planner, "is_ai_available", lambda user_id=None, workspace_id=None: True)
    monkeypatch.setattr(planner, "current_user_optional", lambda request: None)

    reply = await planner.chat(
        [planner.ChatMessage(role="user", content="Build a pipeline: read orders.csv and write parquet")],
        request=SimpleNamespace(),
        preview=True,
        workspace_id="w1",
    )

    assert reply["workflow"]["name"] == "Draft"
    assert store.saved == []


@pytest.mark.asyncio
async def test_failed_ai_call_is_disclosed_when_rules_take_over(monkeypatch):
    async def failing_generate(messages, user_id=None, workspace_id=None):
        return None

    monkeypatch.setattr(planner, "ai_generate_pipeline", failing_generate)
    monkeypatch.setattr(planner, "is_ai_available", lambda user_id=None, workspace_id=None: True)
    monkeypatch.setattr(planner, "current_user_optional", lambda request: None)

    reply = await planner.chat(
        [planner.ChatMessage(role="user", content="Build a pipeline: read orders.csv, deduplicate by order_id, output to parquet")],
        request=SimpleNamespace(),
        preview=True,
        workspace_id="w1",
    )

    assert "could not complete this request" in reply["reply"]


@pytest.mark.asyncio
async def test_apply_draft_saves_a_new_pipeline_in_caller_workspace(monkeypatch):
    from revoluzen_flow.main import app_state

    store = _RecordingStore()
    monkeypatch.setitem(app_state, "store", store)
    step = Step(type=StepType.CSV_SOURCE, label="Read")

    # Extra fields such as id, status or workspace in the request are ignored.
    body = planner.ApplyDraftRequest.model_validate(
        {"id": "existing-id", "status": "published", "workspace_id": "other", "name": "Draft", "steps": [step.model_dump(mode="json")]}
    )
    # `apply_draft` stamps the author from the request via `_owner_fields`.
    # Anonymous local mode is the documented no-user path, and it keeps this
    # test about what it is named for: the workspace and the fresh id.
    from revoluzen_flow.auth import deps as auth_deps
    monkeypatch.setattr(auth_deps, "current_user_optional", lambda request: None)
    result = await planner.apply_draft(body, SimpleNamespace(), workspace_id="w1")

    assert store.saved[0].workspace_id == "w1"
    assert store.saved[0].id != "existing-id"
    assert result["workflow"]["name"] == "Draft"


def test_step_settings_sent_to_the_model_lose_credentials():
    from revoluzen_flow.ai.sanitize import sanitize_for_llm

    extra = {"workflow": {"steps": [{"label": "Load", "params": {"password": "hunter2", "api_key": "abc123", "table": "orders"}}]}}
    clean = sanitize_for_llm(extra).payload
    params = clean["workflow"]["steps"][0]["params"]

    assert "hunter2" not in str(clean) and "abc123" not in str(clean)
    assert params["table"] == "orders"


def test_claude_text_skips_thinking_blocks():
    data = {"content": [{"type": "thinking", "thinking": "..."}, {"type": "text", "text": "answer"}]}
    assert ai_client.claude_text(data) == "answer"


def test_general_knowledge_question_still_uses_atlas():
    assert _try_atlas_match("what is a node") is not None


@pytest.mark.asyncio
async def test_anthropic_client_uses_ai_trust_context(monkeypatch):
    context = ssl.create_default_context()
    seen = {}

    class FakeClient:
        def __init__(self, **kwargs):
            seen.update(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, *args, **kwargs):
            return SimpleNamespace(
                raise_for_status=lambda: None,
                json=lambda: {"content": [], "stop_reason": "end_turn", "usage": {}},
            )

    monkeypatch.setattr(anthropic_client, "ai_ssl_context", lambda: context)
    monkeypatch.setattr(anthropic_client.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(ai_client, "resolve_provider", lambda **_: ("claude", "key", "model", ""))

    await anthropic_client.AnthropicAgentClient().call(system="s", messages=[], tools=[])
    assert seen["verify"] is context
