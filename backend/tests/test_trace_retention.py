"""Tests for the opt-in agent-trace age-out.

Covers the two halves that were missing before: that
``TraceStore.purge_older_than`` actually deletes by age (it existed but
nothing called it, so nothing exercised it end to end), and that the
scheduling decision in ``main`` is driven by
``REVOLUZEN_FLOW_TRACE_RETENTION_DAYS`` and is OFF by default.

The default matters more than it looks: shipping a finite default would
delete operator data on upgrade with no way back, so "0 means keep
forever" is a guarantee, not an implementation detail.
"""

from __future__ import annotations

import importlib
import time

import pytest

from revoluzen_flow.ai.agent import TraceStep
from revoluzen_flow.ai.trace_store import TraceStore


def _step() -> TraceStep:
    return TraceStep(
        step_id=f"step-{int(time.time() * 1000)}",
        tool_name="summarize_pipeline",
        tool_tier="read",
        input_hash="a" * 64,
        output_hash="b" * 64,
        timestamp="2026-04-29T12:00:00+00:00",
        latency_ms=42,
        tokens_in=100,
        tokens_out=20,
        decision_reason="ok",
        redactions_applied={},
        outcome="success",
        policy_rules_fired=[],
    )


@pytest.fixture
def store(_revoluzen_flow_test_db):
    return TraceStore(db=_revoluzen_flow_test_db)


def _add(store, run_id: str) -> None:
    store.store(
        run_id=run_id,
        user_id="u-1",
        workspace_id="ws-1",
        page="pipelines",
        user_intent="why did this fail",
        outcome="success",
        iterations=1,
        total_tokens_in=100,
        total_tokens_out=20,
        elapsed_ms=10,
        steps=[_step()],
        final_text="done",
    )


def _age(db, run_id: str, days: int) -> None:
    """Backdate one trace by N days, whatever column holds its timestamp."""
    from datetime import datetime, timedelta, timezone

    when = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    cols = [r[1] for r in db.execute("PRAGMA table_info(agent_traces)")]
    col = next((c for c in ("created_at", "timestamp", "started_at") if c in cols), None)
    assert col, f"no timestamp column found in agent_traces: {cols}"
    db.execute(f"UPDATE agent_traces SET {col} = ? WHERE run_id = ?", (when, run_id))


# ── purge_older_than ────────────────────────────────────────────────────

def test_purge_deletes_only_rows_past_the_window(store, _revoluzen_flow_test_db):
    _add(store, "old")
    _add(store, "fresh")
    _age(_revoluzen_flow_test_db, "old", 120)

    deleted = store.purge_older_than(90)

    assert deleted == 1
    remaining = {r["run_id"] for r in store.list_recent(user_id="u-1")}
    assert remaining == {"fresh"}


def test_purge_keeps_everything_inside_the_window(store, _revoluzen_flow_test_db):
    _add(store, "recent")
    _age(_revoluzen_flow_test_db, "recent", 10)

    assert store.purge_older_than(90) == 0
    assert store.count() == 1


def test_purge_rejects_nonpositive(store):
    # 0 is the "keep forever" sentinel at the config layer and must never
    # reach the store as "delete everything older than now".
    with pytest.raises(ValueError):
        store.purge_older_than(0)
    with pytest.raises(ValueError):
        store.purge_older_than(-1)


# ── scheduling decision ─────────────────────────────────────────────────

def _reload_config(monkeypatch, value: str | None):
    import revoluzen_flow.runtime_config as rc

    if value is None:
        monkeypatch.delenv("REVOLUZEN_FLOW_TRACE_RETENTION_DAYS", raising=False)
    else:
        monkeypatch.setenv("REVOLUZEN_FLOW_TRACE_RETENTION_DAYS", value)
    return importlib.reload(rc)


def test_retention_is_off_by_default(monkeypatch):
    rc = _reload_config(monkeypatch, None)
    try:
        assert rc.TRACE_RETENTION_DAYS == 0
    finally:
        _reload_config(monkeypatch, None)


def test_retention_reads_the_env_var(monkeypatch):
    rc = _reload_config(monkeypatch, "90")
    try:
        assert rc.TRACE_RETENTION_DAYS == 90
    finally:
        _reload_config(monkeypatch, None)


def test_retention_reported_in_config_snapshot(monkeypatch):
    """Operators read the live value off /api/health/ready rather than
    guessing at env vars, so the snapshot has to carry it."""
    rc = _reload_config(monkeypatch, "30")
    try:
        assert rc.snapshot()["trace_retention_days"] == 30
    finally:
        _reload_config(monkeypatch, None)


@pytest.mark.asyncio
async def test_pruner_loop_returns_immediately_when_disabled(monkeypatch):
    """At 0 the loop must exit rather than sleep — otherwise a disabled
    pruner still holds a task for the life of the process."""
    _reload_config(monkeypatch, None)
    import revoluzen_flow.main as main

    importlib.reload(main)
    await main._trace_pruner_loop()  # returns, does not hang
    _reload_config(monkeypatch, None)
