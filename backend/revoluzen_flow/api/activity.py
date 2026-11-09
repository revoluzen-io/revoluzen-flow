"""
GET /api/v1/activity — activity feed for one workspace.

Two scopes:

    scope=ai   What the AI did: every AI request in the trace store — the
               Copilot's runs and the AI calls other features make (the
               Editor's drafting and SQL help, run summaries, notification
               summaries; see revoluzen_flow/ai/usage_sources.py) — with
               which feature, the model, tokens, how it ended, and totals
               for the whole time window. Insights → Activity reads this.
               Pipeline runs are left out; their history is on
               Workflows → Executions.

    scope=all  The default, unchanged for existing callers: one timeline
               joining three streams, newest first.
                 - audit_log       (authenticated actions; only where the
                                    table exists — this edition sends audit
                                    events to the server log instead, see
                                    revoluzen_flow/audit.py)
                 - agent_traces    (Copilot runs)
                 - executions      (pipeline runs)

AI requests follow the same rule in both scopes: admins see everyone's in
the workspace, other signed-in people see their own, and a caller who is
not signed in (local mode) sees only requests made without signing in,
which include automatic ones such as notification summaries. This
matches who may open a run's trace (GET /api/ai/agent/trace/{run_id}).

Filters: workspace_id is automatic. Optional `since` / `until` (ISO
timestamps; the window defaults to the last 7 days), `user_id`,
`workflow_id` (pipeline runs and audit rows only), `kinds`
(comma-separated subset of [audit, agent, execution]; ignored when
scope=ai).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, Request

from revoluzen_flow.ai.usage_sources import feature_label
from revoluzen_flow.auth.deps import (
    ADMIN_ROLES,
    current_user_optional,
    current_workspace_id,
)

router = APIRouter(prefix="/api/v1/activity", tags=["activity"])

# How a Copilot run ended, grouped for counting. Kept in step with
# revoluzen_flow.ai.trace_store.FAILED_OUTCOMES / BLOCKED_OUTCOMES.
_FAILED_OUTCOMES = ("llm_failure", "tool_failure", "timeout")
_BLOCKED_OUTCOMES = ("policy_block",)


def _parse_iso(s: str | None) -> datetime | None:
    """Parse an ISO timestamp as an aware UTC datetime (naive means UTC)."""
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None
    return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d.astimezone(timezone.utc)


def _normalize_ts(v: Any) -> datetime | None:
    """Coerce a datetime / ISO string / None to a tz-aware datetime."""
    if v is None:
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _ai_visibility(request: Request) -> tuple[str, str | None]:
    """Whose Copilot runs the caller may see, and the user id that limits it.

        ("workspace", None)      admins: everyone's runs in the workspace
        ("own", user id)         other signed-in people: their own runs
        ("not_signed_in", None)  no session (local mode): runs made
                                 without signing in
    """
    user = current_user_optional(request)
    uid = getattr(user, "id", None) if user is not None else None
    if not uid:
        return "not_signed_in", None
    if getattr(user, "role", None) in ADMIN_ROLES:
        return "workspace", None
    return "own", uid


def _ai_severity(outcome: str) -> str:
    if outcome in ("llm_failure", "tool_failure"):
        return "error"
    if outcome in ("timeout", "policy_block"):
        return "warning"
    return "info"


def _ai_event(r: dict[str, Any]) -> dict[str, Any] | None:
    """One AI request from the trace store as an activity event."""
    ts = _normalize_ts(r.get("created_at"))
    if ts is None:
        return None
    outcome = r.get("outcome") or "success"
    uid = r.get("user_id") or None
    if uid == "anonymous":
        uid = None
    source = r.get("source") or None
    feature = feature_label(source)
    tokens_in = int(r.get("total_tokens_in") or 0)
    tokens_out = int(r.get("total_tokens_out") or 0)
    intent = (r.get("user_intent") or "").strip()[:200]
    return {
        "kind": "agent",
        "timestamp": ts.isoformat(),
        "actor": uid or "anonymous",
        "subject": r.get("run_id"),
        "summary": intent or feature,
        "severity": _ai_severity(outcome),
        "details": {
            "page": r.get("page"),
            "elapsed_ms": r.get("elapsed_ms"),
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "tokens": tokens_in + tokens_out,
            "model": r.get("model") or None,
            "provider": r.get("provider") or None,
            "user_intent": intent or None,
            # Where the request came from: "copilot", or the feature's id
            # with its plain name in `feature`.
            "source": source or "copilot",
            "feature": feature,
            "outcome": outcome,
            "step_count": int(r.get("step_count") or 0),
            "failure_reason": r.get("failure_reason") or None,
        },
    }


def _empty_usage() -> dict[str, Any]:
    return {
        "requests": 0, "tokens_in": 0, "tokens_out": 0,
        "failed": 0, "blocked": 0, "by_model": [], "by_source": [],
    }


@router.get("")
def get_activity(
    request: Request,
    since: str | None = None,
    until: str | None = None,
    user_id: str | None = None,
    workflow_id: str | None = None,
    kinds: str = "audit,agent,execution",
    scope: Literal["all", "ai"] = "all",
    limit: int = 100,
    workspace_id: str = Depends(current_workspace_id),
) -> dict[str, Any]:
    """Activity feed for the calling workspace, newest first.

    Each event has a stable shape:
        {
          kind: "audit" | "agent" | "execution",
          timestamp: ISO 8601,
          actor: user_id or "system",
          subject: workflow_id / agent run_id / audit resource_id,
          summary: short human-readable line,
          severity: "info" | "warning" | "error",
          details: {...kind-specific fields...}
        }

    An AI request (kind "agent") carries in ``details``: source ("copilot"
    or a feature id) and feature (its plain name), user_intent, model,
    provider, tokens_in, tokens_out, tokens, elapsed_ms, step_count, page,
    outcome (success | llm_failure | tool_failure | timeout | policy_block)
    and failure_reason. There is no "who asked": this edition has one user.

    ``scope=ai`` returns AI requests only, plus ``summary``: totals over
    every request in the window (requests, tokens_in, tokens_out, failed,
    blocked, by_model, by_source), not just the ``limit`` rows returned.
    """
    limit = max(1, min(int(limit or 100), 500))
    since_dt = _parse_iso(since)
    until_dt = _parse_iso(until)
    if since_dt is None:
        # Default window — last 7 days. Avoids a giant scan on a busy workspace.
        since_dt = datetime.now(timezone.utc) - timedelta(days=7)

    if scope == "ai":
        wanted_kinds = {"agent"}
    else:
        wanted_kinds = {k.strip().lower() for k in (kinds or "").split(",") if k.strip()}
        if not wanted_kinds:
            wanted_kinds = {"audit", "agent", "execution"}

    visibility, own_user_id = _ai_visibility(request)
    usage: dict[str, Any] | None = None

    events: list[dict[str, Any]] = []

    try:
        from revoluzen_flow.main import app_state  # type: ignore
    except Exception:
        app_state = None  # type: ignore

    # ── Pipeline executions ────────────────────────────────────────────
    if "execution" in wanted_kinds and app_state:
        execution_store = app_state.get("execution_store")
        if execution_store is not None:
            try:
                rows = (
                    execution_store.list_by_workflow(workflow_id, limit, workspace_id=workspace_id)
                    if workflow_id
                    else execution_store.list_all(limit, workspace_id=workspace_id)
                )
                for r in rows or []:
                    ts = _normalize_ts(r.get("started_at") or r.get("created_at"))
                    if ts is None or ts < since_dt:
                        continue
                    if until_dt and ts > until_dt:
                        continue
                    status = r.get("status", "unknown")
                    sev = "error" if status == "error" else ("warning" if status == "cancelled" else "info")
                    events.append({
                        "kind": "execution",
                        "timestamp": ts.isoformat(),
                        "actor": r.get("triggered_by", "system"),
                        "subject": r.get("workflow_id") or r.get("workflow_name"),
                        "summary": (
                            f"Pipeline {r.get('workflow_name') or r.get('workflow_id')!r} "
                            f"{status} in {r.get('duration_ms') or 0}ms"
                        ),
                        "severity": sev,
                        "details": {
                            "execution_id": r.get("id"),
                            "duration_ms": r.get("duration_ms"),
                            "steps_failed": r.get("steps_failed"),
                            "error_summary": (r.get("error_message") or "")[:200] or None,
                        },
                    })
            except Exception:
                # Best-effort — partial activity feed is better than 500.
                pass

    # ── AI requests: Copilot runs and other features' AI calls ─────────
    if "agent" in wanted_kinds and app_state:
        trace_store = app_state.get("trace_store")
        if trace_store is not None:
            window = {
                "workspace_id": workspace_id,
                "since": since_dt.isoformat(),
                "until": until_dt.isoformat() if until_dt else None,
                "user_id": own_user_id if visibility == "own" else user_id,
                "unattributed_only": visibility == "not_signed_in",
            }
            try:
                for r in trace_store.list_usage(limit=limit, **window):
                    event = _ai_event(r)
                    if event is not None:
                        events.append(event)
                if scope == "ai":
                    usage = trace_store.usage_totals(**window)
            except Exception:
                pass

    # ── Audit log ──────────────────────────────────────────────────────
    if "audit" in wanted_kinds and app_state:
        # We don't have a dedicated audit_log "store" object; the table is
        # written to directly. Use the database handle to read.
        try:
            db = app_state.get("db")
            if db is not None:
                conditions = ["workspace_id = ?", "created_at >= ?"]
                params: list[Any] = [workspace_id, since_dt.isoformat()]
                if until_dt:
                    conditions.append("created_at <= ?")
                    params.append(until_dt.isoformat())
                if user_id:
                    conditions.append("user_id = ?")
                    params.append(user_id)
                if workflow_id:
                    conditions.append("(metadata LIKE ? OR resource_id = ?)")
                    params.append(f"%{workflow_id}%")
                    params.append(workflow_id)
                where = " AND ".join(conditions)
                sql = (
                    f"SELECT user_id, action, resource_type, resource_id, "
                    f"created_at, metadata "
                    f"FROM audit_log WHERE {where} "
                    f"ORDER BY created_at DESC LIMIT ?"
                )
                params.append(limit)
                rows = db.fetchall(sql, tuple(params))
                for r in rows or []:
                    ts = _normalize_ts(r.get("created_at") if isinstance(r, dict) else r["created_at"])
                    if ts is None or ts < since_dt:
                        continue
                    rd = dict(r)
                    action = rd.get("action", "")
                    sev = "warning" if "delete" in action or "reject" in action else "info"
                    events.append({
                        "kind": "audit",
                        "timestamp": ts.isoformat(),
                        "actor": rd.get("user_id") or "system",
                        "subject": rd.get("resource_id") or rd.get("resource_type"),
                        "summary": f"{action} on {rd.get('resource_type', '?')}",
                        "severity": sev,
                        "details": {
                            "resource_type": rd.get("resource_type"),
                            "metadata": (rd.get("metadata") or "")[:300] if isinstance(rd.get("metadata"), str) else None,
                        },
                    })
        except Exception:
            pass

    # Sort newest-first; trim to limit.
    events.sort(key=lambda e: e["timestamp"], reverse=True)
    events = events[:limit]

    counts = {"audit": 0, "agent": 0, "execution": 0}
    severity_counts = {"info": 0, "warning": 0, "error": 0}
    for e in events:
        counts[e["kind"]] = counts.get(e["kind"], 0) + 1
        severity_counts[e["severity"]] = severity_counts.get(e["severity"], 0) + 1

    result: dict[str, Any] = {
        "events": events,
        "count": len(events),
        "kind_counts": counts,
        "severity_counts": severity_counts,
        "filter": {
            "since": since_dt.isoformat(),
            "until": until_dt.isoformat() if until_dt else None,
            "user_id": user_id,
            "workflow_id": workflow_id,
            "kinds": sorted(wanted_kinds),
            "scope": scope,
            # Whose Copilot runs were included: "workspace", "own" or
            # "not_signed_in" (see _ai_visibility).
            "ai_visibility": visibility,
        },
    }
    if scope == "ai":
        result["summary"] = usage or _empty_usage()
    return result
