"""Insights → Activity shows what the AI did, not pipeline runs.

The page reads GET /api/v1/activity?scope=ai. These tests pin:

  * the trace store's usage queries: one workspace, one time window, the
    right people, and totals over every run rather than the rows shown;
  * the endpoint: scope=ai returns AI requests only, with where each came
    from and totals, and no "who asked" (this edition has one user); the
    default scope still returns the full timeline for callers that rely
    on it;
  * who sees whose runs: admins everyone's, other people their own, a
    caller who is not signed in only runs made without signing in;
  * the Copilot's one-call answers (single-shot and hybrid lanes) are
    recorded, so their tokens and cost are counted.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from revoluzen_flow.ai.trace_store import TraceStore
from revoluzen_flow.auth.models import User
from revoluzen_flow.auth.store import UserStore
from revoluzen_flow.monitoring.store import ExecutionRecord, ExecutionStore


def _store_run(
    store: TraceStore,
    run_id: str,
    *,
    user_id: str | None = "u-ana",
    workspace_id: str = "ws-1",
    outcome: str = "success",
    tokens_in: int = 100,
    tokens_out: int = 20,
    provider: str | None = "openai",
    model: str | None = "gpt-4o-mini",
    intent: str = "Why did the orders pipeline fail?",
    steps: list | None = None,
    source: str | None = None,
) -> None:
    store.store(
        run_id=run_id,
        user_id=user_id,
        workspace_id=workspace_id,
        page="executions.list",
        user_intent=intent,
        outcome=outcome,
        iterations=1,
        total_tokens_in=tokens_in,
        total_tokens_out=tokens_out,
        elapsed_ms=1500,
        steps=steps or [],
        final_text="",
        model=model,
        provider=provider,
        source=source,
    )


def _age(db, run_id: str, days: float) -> None:
    """Move a run back in time; store() always stamps it with now."""
    when = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    db.execute("UPDATE agent_traces SET created_at = ? WHERE run_id = ?", (when, run_id))
    db.commit()


def _week_ago() -> str:
    return (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()


@pytest.fixture
def store(_revoluzen_flow_test_db):
    return TraceStore(db=_revoluzen_flow_test_db)


# ── Trace store ─────────────────────────────────────────────────────────


def test_list_usage_keeps_to_one_workspace_and_window(store, _revoluzen_flow_test_db):
    _store_run(store, "r-here")
    _store_run(store, "r-other-ws", workspace_id="ws-2")
    _store_run(store, "r-old")
    _age(_revoluzen_flow_test_db, "r-old", days=10)

    rows = store.list_usage(workspace_id="ws-1", since=_week_ago())
    assert [r["run_id"] for r in rows] == ["r-here"]


def test_list_usage_user_filter_does_not_cross_workspaces(store):
    # list_recent(user_id=...) ignores the workspace; the usage query must not.
    _store_run(store, "r-ws1", user_id="u-ana", workspace_id="ws-1")
    _store_run(store, "r-ws2", user_id="u-ana", workspace_id="ws-2")
    _store_run(store, "r-bo", user_id="u-bo", workspace_id="ws-1")

    rows = store.list_usage(workspace_id="ws-1", user_id="u-ana")
    assert [r["run_id"] for r in rows] == ["r-ws1"]


def test_list_usage_unattributed_only(store):
    _store_run(store, "r-signed-in", user_id="u-ana")
    _store_run(store, "r-local", user_id=None)

    rows = store.list_usage(workspace_id="ws-1", unattributed_only=True)
    assert [r["run_id"] for r in rows] == ["r-local"]


def test_list_usage_carries_the_failure_reason(store):
    _store_run(store, "r-ok")
    _store_run(
        store, "r-bad", outcome="llm_failure", tokens_in=0, tokens_out=0,
        steps=[
            {"step_id": "s1", "tool_name": "list_executions", "outcome": "success",
             "decision_reason": "ok"},
            {"step_id": "s2", "tool_name": "llm_call:HTTPStatusError", "outcome": "llm_failure",
             "decision_reason": "llm_call:HTTPStatusError: 401 Unauthorized"},
        ],
    )

    by_id = {r["run_id"]: r for r in store.list_usage(workspace_id="ws-1")}
    assert by_id["r-bad"]["failure_reason"] == "llm_call:HTTPStatusError: 401 Unauthorized"
    assert by_id["r-ok"]["failure_reason"] is None
    assert "failure_data" not in by_id["r-bad"]


def test_usage_totals_cover_every_run_not_just_the_rows_shown(store):
    for i in range(5):
        _store_run(store, f"r-mini-{i}", tokens_in=1000, tokens_out=200)
    _store_run(store, "r-local", provider="ollama", model="qwen2.5:7b", tokens_in=50, tokens_out=5)
    _store_run(store, "r-timeout", outcome="timeout")
    _store_run(store, "r-capped", outcome="policy_block", tokens_in=0, tokens_out=0)

    assert len(store.list_usage(workspace_id="ws-1", limit=2)) == 2
    totals = store.usage_totals(workspace_id="ws-1")

    assert totals["requests"] == 8
    assert totals["tokens_in"] == 5 * 1000 + 50 + 100
    assert totals["tokens_out"] == 5 * 200 + 5 + 20
    assert totals["failed"] == 1
    assert totals["blocked"] == 1
    by_model = {(m["provider"], m["model"]): m for m in totals["by_model"]}
    assert by_model[("openai", "gpt-4o-mini")]["requests"] == 7
    assert by_model[("ollama", "qwen2.5:7b")]["tokens_in"] == 50


def test_usage_queries_without_a_database_are_empty():
    s = TraceStore(db=None)
    assert s.list_usage(workspace_id="ws-1") == []
    assert s.usage_totals(workspace_id="ws-1")["requests"] == 0


# ── Endpoint ────────────────────────────────────────────────────────────


@pytest.fixture
def world(_revoluzen_flow_test_db, monkeypatch):
    """Stores wired into app_state, two people, Copilot runs and pipeline runs."""
    from revoluzen_flow.api import activity
    from revoluzen_flow.main import app_state

    traces = TraceStore(db=_revoluzen_flow_test_db)
    executions = ExecutionStore(db=_revoluzen_flow_test_db)
    users = UserStore(db=_revoluzen_flow_test_db)
    users.create_user(User(id="u-ana", email="ana@example.com", name="Ana Admin", role="admin"))
    users.create_user(User(id="u-bo", email="bo@example.com", name="", role="developer"))

    for key, value in (
        ("trace_store", traces),
        ("execution_store", executions),
        ("user_store", users),
        ("db", _revoluzen_flow_test_db),
    ):
        monkeypatch.setitem(app_state, key, value)

    _store_run(traces, "r-ana", user_id="u-ana", intent="Explain the sales pipeline")
    _store_run(traces, "r-bo", user_id="u-bo", outcome="llm_failure", tokens_in=0, tokens_out=0)
    _store_run(traces, "r-local", user_id=None, intent="what does the filter node do?")
    _store_run(traces, "r-gone", user_id="u-deleted")
    executions.record(ExecutionRecord(
        workflow_id="wf-orders", workflow_name="Orders", workspace_id="ws-1", status="error",
    ))

    signed_in: dict[str, User | None] = {"user": None}
    monkeypatch.setattr(activity, "current_user_optional", lambda _request: signed_in["user"])

    def call(as_user: str | None = None, **params):
        signed_in["user"] = users.get_user(as_user) if as_user else None
        return activity.get_activity(request=SimpleNamespace(), workspace_id="ws-1", **params)

    return call


def test_ai_scope_has_copilot_runs_only_with_totals(world):
    out = world("u-ana", scope="ai")

    assert {e["kind"] for e in out["events"]} == {"agent"}
    assert {e["subject"] for e in out["events"]} == {"r-ana", "r-bo", "r-local", "r-gone"}
    assert out["kind_counts"] == {"audit": 0, "agent": 4, "execution": 0}
    assert out["filter"]["scope"] == "ai"
    assert out["filter"]["ai_visibility"] == "workspace"
    assert out["summary"]["requests"] == 4
    assert out["summary"]["failed"] == 1


def test_ai_events_say_what_was_asked_and_how_it_ended(world):
    events = {e["subject"]: e for e in world("u-ana", scope="ai")["events"]}

    assert events["r-ana"]["summary"] == "Explain the sales pipeline"
    assert events["r-ana"]["details"]["outcome"] == "success"
    assert events["r-ana"]["details"]["model"] == "gpt-4o-mini"
    assert events["r-bo"]["details"]["outcome"] == "llm_failure"
    assert events["r-bo"]["severity"] == "error"
    assert events["r-local"]["actor"] == "anonymous"
    # One user in this edition: no "who asked" on the rows.
    assert all("asked_by" not in e["details"] for e in events.values())


def test_other_people_see_only_their_own_requests(world):
    out = world("u-bo", scope="ai")

    assert [e["subject"] for e in out["events"]] == ["r-bo"]
    assert out["filter"]["ai_visibility"] == "own"
    assert out["summary"]["requests"] == 1
    # Asking for someone else's runs does not widen the view.
    assert [e["subject"] for e in world("u-bo", scope="ai", user_id="u-ana")["events"]] == ["r-bo"]


def test_a_caller_who_is_not_signed_in_sees_only_unattributed_requests(world):
    out = world(None, scope="ai")

    assert [e["subject"] for e in out["events"]] == ["r-local"]
    assert out["filter"]["ai_visibility"] == "not_signed_in"


def test_default_scope_still_returns_the_full_timeline(world):
    out = world("u-ana")

    kinds = {e["kind"] for e in out["events"]}
    assert kinds == {"agent", "execution"}
    assert "summary" not in out
    assert out["filter"]["scope"] == "all"
    assert out["filter"]["kinds"] == ["agent", "audit", "execution"]
    # Copilot runs follow the same visibility rule in this scope too.
    assert {e["subject"] for e in world("u-bo")["events"] if e["kind"] == "agent"} == {"r-bo"}


def test_kinds_are_ignored_in_the_ai_scope(world):
    out = world("u-ana", scope="ai", kinds="execution")
    assert {e["kind"] for e in out["events"]} == {"agent"}


def test_an_unknown_scope_is_rejected(_revoluzen_flow_test_db):
    from fastapi.testclient import TestClient
    from fastapi import FastAPI
    from revoluzen_flow.api import activity
    from revoluzen_flow.auth.deps import current_workspace_id

    app = FastAPI()
    app.include_router(activity.router)
    app.dependency_overrides[current_workspace_id] = lambda: "ws-1"
    with TestClient(app) as client:
        assert client.get("/api/v1/activity?scope=pipelines").status_code == 422
        assert client.get("/api/v1/activity?scope=ai").status_code == 200


# ── One-call Copilot answers are recorded ───────────────────────────────


@pytest.fixture
def recorded(store, monkeypatch):
    from revoluzen_flow.main import app_state

    monkeypatch.setitem(app_state, "trace_store", store)
    return store


def _page(user_id: str = "u-ana"):
    from revoluzen_flow.ai.context import PageContext

    return PageContext(page="executions.list", user_id=user_id, tenant_id="ws-1", workspace_id="ws-1")


def test_a_quick_answer_is_saved_with_its_tokens(recorded):
    from revoluzen_flow.api.agent import _record_quick_answer

    _record_quick_answer(
        run_id="r-shot", lane="single_shot", page_ctx=_page(),
        user_intent="why is this run slow?", provider="openai", model="gpt-4o-mini",
        text="It waited on the database.", tokens_in=800, tokens_out=60, elapsed_ms=2100,
    )

    row = recorded.list_usage(workspace_id="ws-1")[0]
    assert row["run_id"] == "r-shot"
    assert row["outcome"] == "success"
    assert (row["total_tokens_in"], row["total_tokens_out"]) == (800, 60)
    assert (row["provider"], row["model"]) == ("openai", "gpt-4o-mini")
    assert row["user_id"] == "u-ana"


def test_a_failed_quick_answer_is_saved_as_a_failed_ai_call(recorded):
    from revoluzen_flow.api.agent import _record_quick_answer

    _record_quick_answer(
        run_id="r-hybrid", lane="hybrid", page_ctx=_page("anonymous"),
        user_intent="why did it fail?", provider="claude", model="claude-haiku-4-5",
        text="3 failed runs today.", tokens_in=0, tokens_out=0, elapsed_ms=900,
        error="ReadTimeout: provider did not answer",
    )

    row = recorded.list_usage(workspace_id="ws-1")[0]
    assert row["outcome"] == "llm_failure"
    assert row["failure_reason"] == "ReadTimeout: provider did not answer"
    assert row["user_id"] is None
    assert recorded.usage_totals(workspace_id="ws-1")["failed"] == 1


def test_recording_never_breaks_a_reply(monkeypatch):
    from revoluzen_flow.api.agent import _record_quick_answer
    from revoluzen_flow.main import app_state

    class Broken:
        def store(self, **_kw):
            raise RuntimeError("disk full")

    monkeypatch.setitem(app_state, "trace_store", Broken())
    _record_quick_answer(
        run_id="r-x", lane="single_shot", page_ctx=_page(), user_intent="hi",
        provider="openai", model="gpt-4o-mini", text="hello", tokens_in=1,
        tokens_out=1, elapsed_ms=1,
    )


# ── The Copilot knows where AI usage lives ──────────────────────────────


def test_ai_usage_questions_route_to_the_insights_topic():
    from revoluzen_flow.ai.atlas import find_topics_by_alias

    for prompt in ("ai usage", "where can i see ai cost", "token usage"):
        matches = find_topics_by_alias(prompt)
        assert matches and matches[0][0].id == "page.ai_hub", (prompt, matches[:3])


# ── AI calls from other features are counted too ────────────────────────


def _rows(store):
    return {r["source"] or "copilot": r for r in store.list_usage(workspace_id="ws-1")}


def test_a_hub_call_is_recorded_with_its_feature_and_tokens(recorded, monkeypatch):
    import asyncio

    from revoluzen_flow.planner import ai_client

    async def answer(api_key, model, system_prompt, messages):
        return {"explanation": "Adds a total column."}, {"input": 120, "output": 30}

    monkeypatch.setattr(ai_client, "resolve_provider", lambda **_kw: ("openai", "k", "gpt-4o-mini", ""))
    monkeypatch.setattr(ai_client, "_call_text_openai", answer)

    result = asyncio.run(ai_client.ai_generate_json(
        [{"role": "user", "content": "SELECT *, a+b AS total FROM t"}],
        system_prompt="explain", source_label="ai.transform.explain",
        user_id="u-ana", workspace_id="ws-1",
    ))

    assert result == {"explanation": "Adds a total column."}
    row = _rows(recorded)["ai.transform.explain"]
    assert (row["total_tokens_in"], row["total_tokens_out"]) == (120, 30)
    assert row["outcome"] == "success"
    assert row["user_id"] == "u-ana"
    assert row["page"] == "editor"
    # The prompt built from the step is not shown; the feature's name is.
    assert row["user_intent"] == "Explain a transform"


def test_an_unreadable_answer_counts_as_a_failed_ai_call(recorded, monkeypatch):
    import asyncio

    from revoluzen_flow.planner import ai_client

    async def garbled(api_key, model, system_prompt, messages):
        return None, {"input": 50, "output": 7}

    monkeypatch.setattr(ai_client, "resolve_provider", lambda **_kw: ("openai", "k", "gpt-4o-mini", ""))
    monkeypatch.setattr(ai_client, "_call_text_openai", garbled)

    assert asyncio.run(ai_client.ai_generate_json(
        [{"role": "user", "content": "x"}], system_prompt="s",
        source_label="ai.transform.suggest_sql", user_id="u-ana", workspace_id="ws-1",
    )) is None
    row = _rows(recorded)["ai.transform.suggest_sql"]
    assert row["outcome"] == "llm_failure"
    assert row["failure_reason"] == "The model's answer could not be used."
    assert (row["total_tokens_in"], row["total_tokens_out"]) == (50, 7)


def test_callers_can_name_the_feature_and_what_the_person_typed(recorded, monkeypatch):
    import asyncio

    from revoluzen_flow.ai.usage_sources import ai_call_details
    from revoluzen_flow.planner import ai_client

    async def draft(api_key, model, messages):
        return {"steps": [{"type": "csv_source"}]}, {"input": 900, "output": 300}

    monkeypatch.setattr(ai_client, "resolve_provider", lambda **_kw: ("openai", "k", "gpt-4o-mini", ""))
    monkeypatch.setattr(ai_client, "_call_openai", draft)
    msgs = [{"role": "user", "content": "PROMPT BUILT FROM THE REQUEST"}]

    with ai_call_details(intent="Load sales.csv and total by region"):
        asyncio.run(ai_client.ai_generate_pipeline(msgs, user_id="u-ana", workspace_id="ws-1"))
    with ai_call_details(source="copilot.draft_pipeline", intent="orders by day"):
        asyncio.run(ai_client.ai_generate_pipeline(msgs, user_id="u-ana", workspace_id="ws-1"))

    rows = _rows(recorded)
    assert rows["planner.ai_generate_pipeline"]["user_intent"] == "Load sales.csv and total by region"
    assert rows["copilot.draft_pipeline"]["user_intent"] == "orders by day"
    assert rows["copilot.draft_pipeline"]["page"] == "copilot"


def test_no_provider_means_no_call_and_no_row(recorded, monkeypatch):
    import asyncio

    from revoluzen_flow.planner import ai_client

    monkeypatch.setattr(ai_client, "resolve_provider", lambda **_kw: ("none", "", "", ""))
    assert asyncio.run(ai_client.ai_generate_text(
        [{"role": "user", "content": "hi"}], system_prompt="s", source_label="canvas-chat",
    )) is None
    assert recorded.list_usage(workspace_id="default") == []


def test_notification_summaries_are_recorded(recorded, monkeypatch):
    from revoluzen_flow.ai import notification_summary
    from revoluzen_flow.planner import ai_client

    monkeypatch.setattr(ai_client, "resolve_provider", lambda **_kw: ("openai", "k", "gpt-4o-mini", ""))
    monkeypatch.setattr(
        notification_summary, "_call_openai_sync",
        lambda key, model, msg: ('{"summary": "Orders failed: file missing."}',
                                 {"usage": {"prompt_tokens": 80, "completion_tokens": 12}}),
    )

    body, ai_powered = notification_summary.summarize_notification_body(
        event_type="execution_failed", subject="Orders failed", body="static body",
    )
    assert ai_powered and body == "Orders failed: file missing."

    # No person and no workspace come with a notification.
    row = recorded.list_usage(workspace_id="default")[0]
    assert row["source"] == "notifications.summary"
    assert row["workspace_id"] == "default"
    assert row["user_id"] is None
    assert (row["total_tokens_in"], row["total_tokens_out"]) == (80, 12)

    from revoluzen_flow.api import activity
    event = activity._ai_event(row)
    assert event["details"]["source"] == "notifications.summary"
    assert event["details"]["feature"] == "Write a notification"
    assert event["summary"] == "Write a notification"


def test_a_failed_notification_summary_is_a_failed_call(recorded, monkeypatch):
    from revoluzen_flow.ai import notification_summary
    from revoluzen_flow.planner import ai_client

    def boom(key, model, msg):
        raise TimeoutError("provider slow")

    monkeypatch.setattr(ai_client, "resolve_provider", lambda **_kw: ("openai", "k", "gpt-4o-mini", ""))
    monkeypatch.setattr(notification_summary, "_call_openai_sync", boom)

    body, ai_powered = notification_summary.summarize_notification_body(
        event_type="x", subject="s", body="static body",
    )
    assert (body, ai_powered) == ("static body", False)
    row = recorded.list_usage(workspace_id="default")[0]
    assert row["outcome"] == "llm_failure"
    assert row["failure_reason"] == "TimeoutError: provider slow"


def test_the_copilots_failure_explanation_is_recorded(recorded, monkeypatch):
    import asyncio

    import httpx

    from revoluzen_flow.ai import fast_router
    from revoluzen_flow.ai.tools.base import ToolContext
    from revoluzen_flow.planner import ai_client

    class Reply:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": "The source file was missing."}}],
                    "usage": {"prompt_tokens": 210, "completion_tokens": 40}}

    class Client:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, *a, **kw):
            return Reply()

    monkeypatch.setattr(ai_client, "resolve_provider", lambda **_kw: ("openai", "k", "gpt-4o-mini", ""))
    monkeypatch.setattr(httpx, "AsyncClient", Client)
    ctx = ToolContext(tenant_id="ws-1", user_id="u-ana", workspace_id="ws-1", environment="dev")

    text = asyncio.run(fast_router._llm_explain_failure(
        pipeline_name="Orders", error_text="File not found", duration_ms=1200,
        rows_processed=0, heuristic_suggestion=None, ctx=ctx,
    ))

    assert text == "The source file was missing."
    row = _rows(recorded)["copilot.explain_failure"]
    assert (row["total_tokens_in"], row["total_tokens_out"]) == (210, 40)
    assert row["user_id"] == "u-ana"
    assert row["model"] == "gpt-4o-mini"


def test_activity_says_where_each_request_came_from(world, recorded):
    _store_run(recorded, "r-editor", user_id="u-ana", intent="Load sales.csv",
               source="planner.ai_generate_pipeline")
    out = world("u-ana", scope="ai")

    events = {e["subject"]: e for e in out["events"]}
    assert events["r-editor"]["details"]["source"] == "planner.ai_generate_pipeline"
    assert events["r-editor"]["details"]["feature"] == "Draft a pipeline"
    assert events["r-ana"]["details"]["source"] == "copilot"
    assert events["r-ana"]["details"]["feature"] == "Copilot"
    split = {s["source"]: s["requests"] for s in out["summary"]["by_source"]}
    assert split == {"copilot": 4, "planner.ai_generate_pipeline": 1}


def test_an_older_database_gains_the_source_column(_revoluzen_flow_test_db):
    db = _revoluzen_flow_test_db
    db.execute(
        "CREATE TABLE agent_traces (run_id TEXT PRIMARY KEY, user_id TEXT, "
        "workspace_id TEXT NOT NULL DEFAULT 'default', page TEXT, user_intent TEXT, "
        "outcome TEXT NOT NULL, iterations INTEGER NOT NULL DEFAULT 0, "
        "total_tokens_in INTEGER NOT NULL DEFAULT 0, total_tokens_out INTEGER NOT NULL DEFAULT 0, "
        "elapsed_ms INTEGER NOT NULL DEFAULT 0, step_count INTEGER NOT NULL DEFAULT 0, "
        "created_at TEXT NOT NULL, model TEXT, provider TEXT, data TEXT NOT NULL)"
    )
    db.commit()

    store = TraceStore(db=db)
    _store_run(store, "r-new", source="ai.post_run_summary")
    assert store.list_usage(workspace_id="ws-1")[0]["source"] == "ai.post_run_summary"


def test_the_router_audit_reads_copilot_prompts_only(recorded):
    import asyncio

    from revoluzen_flow.api.router_audit import router_audit

    _store_run(recorded, "r-copilot", intent="list pipelines")
    _store_run(recorded, "r-feature", intent="Explain a transform", source="ai.transform.explain")

    out = asyncio.run(router_audit(SimpleNamespace(), include_reference=False))
    assert [r["prompt"] for r in out["rows"]] == ["list pipelines"]
