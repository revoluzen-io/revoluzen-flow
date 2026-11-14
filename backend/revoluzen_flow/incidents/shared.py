"""One cause, several failing pipelines (2026-11-11).

When a warehouse refuses its login, every pipeline that reads it fails, and
each one opens its own incident. Read one at a time they look like five
problems; they are one. This groups the open incidents of a workspace by what
they have in common:

* the **connection** the failed step uses, with the same kind of failure
  (the failure class): five pipelines failing on one connection;
* otherwise the **error** itself, with its particulars taken out (numbers,
  quoted names, ids, paths), and the same failure class: the same mistake
  made by several pipelines, such as a column a shared upstream dropped.

A group needs at least two pipelines. Pure bookkeeping over incidents and
pipeline definitions: no data is read, no model is asked.
"""

from __future__ import annotations

import re
from typing import Any

MIN_PIPELINES = 2

# What each failure class means for a connection, in words a reader knows.
_CLASS_WORDS = {
    "dependency": "cannot be reached, or refused the login",
    "transient": "keeps timing out or failing for a moment",
    "data_quality": "returns data that breaks a rule",
    "user_input": "has a setting that is wrong",
    "fatal": "makes the run crash",
}

_SCRUB = [
    (re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I), "<id>"),
    (re.compile(r"'[^']*'|\"[^\"]*\"|`[^`]*`"), "<name>"),
    (re.compile(r"(?:[a-z]:)?[\\/][^\s,;:]+", re.I), "<path>"),
    (re.compile(r"\b\d+(?:\.\d+)*\b"), "<n>"),
    (re.compile(r"\b[0-9a-f]{10,}\b", re.I), "<id>"),
]


_QUOTED = re.compile(r"'[^']*'|\"[^\"]*\"|`[^`]*`")
_PATH = re.compile(r"(?:[a-z]:)?[\\/][^\s,;:]+", re.I)


def readable_cause(message: str) -> str:
    """The error as the group's reason: its first line with the names and
    paths that differ from one pipeline to the next left out, so it is true
    of every pipeline in the group, not only of the first."""
    first = (message or "").strip().splitlines()[0] if (message or "").strip() else ""
    return _PATH.sub("…", _QUOTED.sub('"…"', first))[:200]


def error_fingerprint(message: str) -> str:
    """The error with its particulars replaced, so the same failure in two
    pipelines reads the same."""
    text = (message or "").strip().splitlines()[0] if (message or "").strip() else ""
    text = text.lower()
    for pattern, repl in _SCRUB:
        text = pattern.sub(repl, text)
    return re.sub(r"\s+", " ", text).strip()[:160]


def _failed_step_connection(app_state: dict, inc: Any) -> tuple[str, str] | None:
    """(id, name) of the connection the failed step uses, when it uses one."""
    store = app_state.get("store")
    if store is None or not inc.failed_step:
        return None
    try:
        version = store.get(inc.workflow_id, workspace_id=inc.workspace_id)
        wf = version.workflow if version else None
    except Exception:  # noqa: BLE001
        return None
    if wf is None:
        return None
    step = next((s for s in wf.steps if (s.label or s.id) == inc.failed_step or s.id == inc.failed_step), None)
    conn_id = str(((step.params or {}).get("connection_id") or "") if step is not None else "").strip()
    if not conn_id:
        return None
    name = conn_id
    try:
        conn = (app_state.get("connection_store") or None)
        found = conn.get(conn_id, workspace_id=inc.workspace_id) if conn is not None else None
        name = getattr(found, "name", None) or conn_id
    except Exception:  # noqa: BLE001
        pass
    return conn_id, name


def shared_causes(app_state: dict, incidents: list[Any]) -> list[dict[str, Any]]:
    """Groups of open incidents with one cause, largest first:
    ``{cause, failure_class, connection, pipelines: [...], count}``."""
    groups: dict[tuple, dict[str, Any]] = {}
    for inc in incidents:
        if getattr(inc, "status", "open") != "open":
            continue
        conn = _failed_step_connection(app_state, inc)
        if conn is not None:
            key: tuple = ("connection", conn[0], inc.failure_class)
            cause = f'the connection "{conn[1]}" ' + _CLASS_WORDS.get(inc.failure_class, "fails the same way in each")
        else:
            fp = error_fingerprint(inc.error)
            if not fp:
                continue
            key = ("error", inc.failure_class, fp)
            cause = readable_cause(inc.error)
        g = groups.setdefault(key, {
            "cause": cause,
            "failure_class": inc.failure_class,
            "connection": {"id": conn[0], "name": conn[1]} if conn else None,
            "pipelines": [],
        })
        g["pipelines"].append({
            "workflow_id": inc.workflow_id, "workflow_name": inc.workflow_name,
            "incident_id": inc.id, "failed_step": inc.failed_step, "last_seen_at": inc.last_seen_at,
        })
    out = []
    for g in groups.values():
        if len({p["workflow_id"] for p in g["pipelines"]}) >= MIN_PIPELINES:
            g["count"] = len(g["pipelines"])
            out.append(g)
    out.sort(key=lambda g: -g["count"])
    return out


__all__ = ["error_fingerprint", "readable_cause", "shared_causes"]
