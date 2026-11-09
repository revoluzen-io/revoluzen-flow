"""
SQLite-backed durable store for agent execution traces.

Step 1.5b-3 of the AI arc — every successful or failed `AgentRunner.run()`
persists its `AgentRunResult` here, and every other AI call (the Editor's
drafting, the SQL helpers, summaries; see `revoluzen_flow.ai.usage_sources`)
is saved as a one-step row with `source` naming the feature, so we have:
  - Replayable trace history per user/workspace
  - SOC2 / DORA-grade audit evidence (per docs/TRUST.md Pillar 3)
  - Cost / outcome analytics over time

Per the locked retention default (90 days, configurable per workspace),
old traces are pruned by the existing retention runner on its daily pass —
that hookup is wired separately. This module only provides store + fetch.

Schema is one JSON-shaped table; per-step querying across runs is rare in
practice. If it becomes hot we'll normalize into `agent_trace_steps` later
without breaking the API.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS agent_traces (
    run_id TEXT PRIMARY KEY,
    user_id TEXT,
    workspace_id TEXT NOT NULL DEFAULT 'default',
    page TEXT,
    user_intent TEXT,
    outcome TEXT NOT NULL,
    iterations INTEGER NOT NULL DEFAULT 0,
    total_tokens_in INTEGER NOT NULL DEFAULT 0,
    total_tokens_out INTEGER NOT NULL DEFAULT 0,
    elapsed_ms INTEGER NOT NULL DEFAULT 0,
    step_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    model TEXT,
    provider TEXT,
    source TEXT,
    data TEXT NOT NULL
);
"""

# Idempotent backfill for installs that pre-date the model/provider/source
# columns. SQLite has no ADD COLUMN IF NOT EXISTS, so we swallow the
# duplicate-column error. `source` is empty for a Copilot run and names the
# feature for any other AI call (see revoluzen_flow.ai.usage_sources).
_BACKFILL_SQL = [
    "ALTER TABLE agent_traces ADD COLUMN model TEXT",
    "ALTER TABLE agent_traces ADD COLUMN provider TEXT",
    "ALTER TABLE agent_traces ADD COLUMN source TEXT",
]

_INDEX_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_agent_traces_user_time ON agent_traces(user_id, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_agent_traces_workspace_time ON agent_traces(workspace_id, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_agent_traces_outcome ON agent_traces(outcome)",
]

# Run outcomes, grouped the way the AI usage view counts them. A failed run
# is one where the model call errored, a tool broke the run, or it ran out of
# time; a blocked run was stopped by a limit (for example a daily token cap)
# before the model was asked.
FAILED_OUTCOMES = ("llm_failure", "tool_failure", "timeout")
BLOCKED_OUTCOMES = ("policy_block",)


def _first_failure_reason(data_json: str | None) -> str | None:
    """The reason recorded on a run's first failed step, if any."""
    if not data_json:
        return None
    try:
        steps = json.loads(data_json).get("steps") or []
    except Exception:
        return None
    for step in steps:
        if not isinstance(step, dict) or step.get("outcome") in (None, "success"):
            continue
        reason = str(step.get("decision_reason") or "").strip()
        if reason:
            return reason[:300]
    return None


class TraceStore:
    """Durable trace store for AgentRunResult.

    Construct with a Database instance from `revoluzen_flow.storage.database`.
    The schema is created idempotently on first construction.
    """

    def __init__(self, db=None):
        self._db = db
        if db is not None:
            self._ensure_schema()

    def set_db(self, db) -> None:
        self._db = db
        if db is not None:
            self._ensure_schema()

    def _ensure_schema(self) -> None:
        try:
            self._db.execute(_CREATE_SQL)
            for sql in _BACKFILL_SQL:
                try:
                    self._db.execute(sql)
                except Exception:
                    # Column already exists (post-migration installs hit this every boot).
                    pass
            for sql in _INDEX_SQL:
                self._db.execute(sql)
            self._db.commit()
        except Exception as exc:
            logger.warning("TraceStore: schema init failed (will retry on next call): %s", exc)

    # ── Write ───────────────────────────────────────────────────────────

    def store(
        self,
        *,
        run_id: str,
        user_id: str | None,
        workspace_id: str,
        page: str,
        user_intent: str,
        outcome: str,
        iterations: int,
        total_tokens_in: int,
        total_tokens_out: int,
        elapsed_ms: int,
        steps: list[Any],
        final_text: str,
        tool_results: list[dict[str, Any]] | None = None,
        model: str | None = None,
        provider: str | None = None,
        source: str | None = None,
    ) -> None:
        """Persist one AgentRunResult. Best-effort — never raises.

        ``source`` stays None for a Copilot run; an AI call from another
        feature names that feature (see revoluzen_flow.ai.usage_sources).

        Caller (AgentRunner) doesn't await this beyond the normal SQLite
        write; if the store is unavailable we log + continue. Trace loss
        is preferable to crashing an agent run.
        """
        if self._db is None:
            return

        # Truncate user_intent for the indexed column. Full text is in `data`.
        intent_short = (user_intent or "")[:256]

        # Normalize step dataclasses → dicts so JSON serialization works.
        steps_dicts = [
            asdict(s) if hasattr(s, "__dataclass_fields__") else s
            for s in (steps or [])
        ]

        payload = {
            "run_id": run_id,
            "final_text": final_text,
            "outcome": outcome,
            "iterations": iterations,
            "total_tokens_in": total_tokens_in,
            "total_tokens_out": total_tokens_out,
            "elapsed_ms": elapsed_ms,
            "steps": steps_dicts,
            "tool_results": tool_results or [],
        }

        try:
            self._db.execute(
                """
                INSERT OR REPLACE INTO agent_traces
                (run_id, user_id, workspace_id, page, user_intent,
                 outcome, iterations, total_tokens_in, total_tokens_out,
                 elapsed_ms, step_count, created_at, model, provider,
                 source, data)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    user_id,
                    workspace_id or "default",
                    page or "",
                    intent_short,
                    outcome,
                    iterations,
                    total_tokens_in,
                    total_tokens_out,
                    elapsed_ms,
                    len(steps_dicts),
                    datetime.now(timezone.utc).isoformat(),
                    model or None,
                    provider or None,
                    source or None,
                    json.dumps(payload, default=str),
                ),
            )
            self._db.commit()
        except Exception as exc:
            # Best-effort: agent runs must not break because trace storage failed.
            logger.warning("TraceStore.store failed for run_id=%s: %s", run_id, exc)

    # ── Read ────────────────────────────────────────────────────────────

    def get(self, run_id: str) -> dict | None:
        """Return the stored trace as a dict, or None on miss / DB error."""
        if self._db is None or not run_id:
            return None
        try:
            rows = self._db.fetchall(
                "SELECT user_id, workspace_id, page, user_intent, created_at, data "
                "FROM agent_traces WHERE run_id = ? LIMIT 1",
                (run_id,),
            )
        except Exception as exc:
            logger.warning("TraceStore.get DB error: %s", exc)
            return None
        if not rows:
            return None
        row = rows[0]
        try:
            data = json.loads(row["data"])
        except Exception:
            return None
        return {
            "run_id": run_id,
            "user_id": row["user_id"],
            "workspace_id": row["workspace_id"],
            "page": row["page"],
            "user_intent": row["user_intent"],
            "created_at": row["created_at"],
            **data,
        }

    def list_recent(
        self,
        *,
        user_id: str | None = None,
        workspace_id: str | None = None,
        limit: int = 20,
    ) -> list[dict]:
        """List recent runs, newest first.

        - If `user_id` given, scoped to that user (most common case).
        - If `workspace_id` given without user_id, scoped to workspace
          (for admin / Plus governance views).
        - With neither, returns the most recent runs across the install
          (suitable for OSS local-dev only — Plus middleware should always
          pass at least workspace_id).
        """
        if self._db is None:
            return []
        limit = max(1, min(int(limit), 200))

        if user_id:
            sql = (
                "SELECT run_id, user_id, workspace_id, page, user_intent, "
                "outcome, iterations, total_tokens_in, total_tokens_out, "
                "elapsed_ms, step_count, created_at, model, provider "
                "FROM agent_traces WHERE user_id = ? "
                "ORDER BY created_at DESC LIMIT ?"
            )
            params: tuple = (user_id, limit)
        elif workspace_id:
            sql = (
                "SELECT run_id, user_id, workspace_id, page, user_intent, "
                "outcome, iterations, total_tokens_in, total_tokens_out, "
                "elapsed_ms, step_count, created_at, model, provider "
                "FROM agent_traces WHERE workspace_id = ? "
                "ORDER BY created_at DESC LIMIT ?"
            )
            params = (workspace_id, limit)
        else:
            sql = (
                "SELECT run_id, user_id, workspace_id, page, user_intent, "
                "outcome, iterations, total_tokens_in, total_tokens_out, "
                "elapsed_ms, step_count, created_at, model, provider "
                "FROM agent_traces ORDER BY created_at DESC LIMIT ?"
            )
            params = (limit,)

        try:
            rows = self._db.fetchall(sql, params)
        except Exception as exc:
            logger.warning("TraceStore.list_recent DB error: %s", exc)
            return []

        return [dict(r) for r in rows]

    # ── AI usage (Insights → Activity) ──────────────────────────────────

    @staticmethod
    def _usage_where(
        *,
        workspace_id: str,
        since: str | None,
        until: str | None,
        user_id: str | None,
        unattributed_only: bool,
    ) -> tuple[str, list[Any]]:
        """WHERE clause shared by the usage list and its totals.

        Always scoped to one workspace, unlike ``list_recent``, whose user
        filter would return a person's runs from every workspace.
        ``since`` / ``until`` are UTC ISO strings, compared as text the same
        way ``purge_older_than`` does.
        """
        clauses = ["workspace_id = ?"]
        params: list[Any] = [workspace_id or "default"]
        if since:
            clauses.append("created_at >= ?")
            params.append(since)
        if until:
            clauses.append("created_at <= ?")
            params.append(until)
        if unattributed_only:
            clauses.append("(user_id IS NULL OR user_id IN ('', 'anonymous'))")
        elif user_id:
            clauses.append("user_id = ?")
            params.append(user_id)
        return " AND ".join(clauses), params

    def list_usage(
        self,
        *,
        workspace_id: str,
        since: str | None = None,
        until: str | None = None,
        user_id: str | None = None,
        unattributed_only: bool = False,
        limit: int = 200,
    ) -> list[dict]:
        """Copilot runs in one workspace and time window, newest first.

        Each row carries the indexed columns plus ``failure_reason``: for a
        run that did not succeed, the reason recorded on its first failed
        step (for example the provider's error message), else ``None``.
        """
        if self._db is None:
            return []
        limit = max(1, min(int(limit), 500))
        where, params = self._usage_where(
            workspace_id=workspace_id, since=since, until=until,
            user_id=user_id, unattributed_only=unattributed_only,
        )
        sql = (
            "SELECT run_id, user_id, workspace_id, page, user_intent, "
            "outcome, iterations, total_tokens_in, total_tokens_out, "
            "elapsed_ms, step_count, created_at, model, provider, source, "
            "CASE WHEN outcome != 'success' THEN data END AS failure_data "
            f"FROM agent_traces WHERE {where} "
            "ORDER BY created_at DESC LIMIT ?"
        )
        try:
            rows = self._db.fetchall(sql, tuple(params + [limit]))
        except Exception as exc:
            logger.warning("TraceStore.list_usage DB error: %s", exc)
            return []
        out: list[dict] = []
        for r in rows:
            row = dict(r)
            row["failure_reason"] = _first_failure_reason(row.pop("failure_data", None))
            out.append(row)
        return out

    def usage_totals(
        self,
        *,
        workspace_id: str,
        since: str | None = None,
        until: str | None = None,
        user_id: str | None = None,
        unattributed_only: bool = False,
    ) -> dict[str, Any]:
        """Totals for the same window as ``list_usage``, over every run in
        it rather than only the rows a page shows.

        ``by_model`` splits the tokens per provider and model so a caller
        can price them with per-model rates; ``by_source`` counts requests
        per feature, with the Copilot's under ``"copilot"``.
        """
        totals: dict[str, Any] = {
            "requests": 0, "tokens_in": 0, "tokens_out": 0,
            "failed": 0, "blocked": 0, "by_model": [], "by_source": [],
        }
        if self._db is None:
            return totals
        where, params = self._usage_where(
            workspace_id=workspace_id, since=since, until=until,
            user_id=user_id, unattributed_only=unattributed_only,
        )
        failed = ", ".join(f"'{o}'" for o in FAILED_OUTCOMES)
        blocked = ", ".join(f"'{o}'" for o in BLOCKED_OUTCOMES)
        sql = (
            "SELECT provider, model, COUNT(*) AS requests, "
            "COALESCE(SUM(total_tokens_in), 0) AS tokens_in, "
            "COALESCE(SUM(total_tokens_out), 0) AS tokens_out, "
            f"SUM(CASE WHEN outcome IN ({failed}) THEN 1 ELSE 0 END) AS failed, "
            f"SUM(CASE WHEN outcome IN ({blocked}) THEN 1 ELSE 0 END) AS blocked "
            f"FROM agent_traces WHERE {where} "
            "GROUP BY provider, model ORDER BY requests DESC"
        )
        try:
            rows = self._db.fetchall(sql, tuple(params))
        except Exception as exc:
            logger.warning("TraceStore.usage_totals DB error: %s", exc)
            return totals
        for r in rows:
            requests = int(r.get("requests") or 0)
            tokens_in = int(r.get("tokens_in") or 0)
            tokens_out = int(r.get("tokens_out") or 0)
            totals["requests"] += requests
            totals["tokens_in"] += tokens_in
            totals["tokens_out"] += tokens_out
            totals["failed"] += int(r.get("failed") or 0)
            totals["blocked"] += int(r.get("blocked") or 0)
            totals["by_model"].append({
                "provider": r.get("provider") or None,
                "model": r.get("model") or None,
                "requests": requests,
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
            })
        try:
            source_rows = self._db.fetchall(
                "SELECT COALESCE(NULLIF(source, ''), 'copilot') AS source, "
                "COUNT(*) AS requests "
                f"FROM agent_traces WHERE {where} "
                "GROUP BY 1 ORDER BY requests DESC",
                tuple(params),
            )
        except Exception as exc:
            logger.warning("TraceStore.usage_totals source split failed: %s", exc)
            source_rows = []
        totals["by_source"] = [
            {"source": r.get("source") or "copilot", "requests": int(r.get("requests") or 0)}
            for r in source_rows
        ]
        return totals

    def purge_older_than(self, days: int) -> int:
        """Delete traces older than N days. Returns rows deleted."""
        if self._db is None:
            return 0
        if days < 1:
            raise ValueError("days must be >= 1")
        cutoff_iso = (
            datetime.now(timezone.utc).timestamp() - days * 86400
        )
        # Stored as ISO8601 strings — compare lexicographically (safe for UTC ISO)
        cutoff_dt = datetime.fromtimestamp(cutoff_iso, tz=timezone.utc).isoformat()
        try:
            cur = self._db.execute(
                "DELETE FROM agent_traces WHERE created_at < ?",
                (cutoff_dt,),
            )
            self._db.commit()
            return int(cur.rowcount or 0)
        except Exception as exc:
            logger.warning("TraceStore.purge_older_than failed: %s", exc)
            return 0

    def count(self) -> int:
        """Total trace rows. Useful for tests + diagnostics."""
        if self._db is None:
            return 0
        try:
            rows = self._db.fetchall("SELECT COUNT(*) AS c FROM agent_traces")
            return int(rows[0]["c"]) if rows else 0
        except Exception:
            return 0
