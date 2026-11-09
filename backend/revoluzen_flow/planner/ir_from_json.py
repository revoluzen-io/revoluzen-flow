"""Turn the planner's JSON into an IR with real step ids.

`SYSTEM_PROMPT` asks the model for connections by array INDEX, because at
the point it writes them the steps have no ids yet:

    "connections": [{"from_step": 0, "to_step": 1}]

Something has to translate those indices into the ids the IR actually uses.
`api/planner.py` did it inline, and `draft_pipeline_from_intent` did not —
so the Copilot's drafts carried `from_step: 0`, a step id that matches
nothing, and the pipeline arrived with its steps unconnected. One function,
used by both, so a second caller cannot forget again.

It also accepts what models send instead of the documented shape: an index
as a string, a step id when the model invented ids of its own, or a label.
Each of those is a thing a model does, and each one silently lost every
connection in the pipeline before.
"""

from __future__ import annotations

import uuid
from typing import Any


def _resolve(ref: Any, ids: list[str], by_key: dict[str, str]) -> str | None:
    """One connection endpoint → a step id, or None if it names nothing.

    The documented form is an index. The rest are what models send anyway.
    """
    if isinstance(ref, bool):
        # bool is an int in Python, and True would silently mean "step 1".
        return None
    if isinstance(ref, int):
        return ids[ref] if 0 <= ref < len(ids) else None
    if isinstance(ref, str):
        key = ref.strip()
        if not key:
            return None
        # A step id or label the model used, including an id it generated
        # itself and put on the step.
        hit = by_key.get(key) or by_key.get(key.lower())
        if hit:
            return hit
        # "0" — an index that arrived as a string.
        if key.lstrip("-").isdigit():
            idx = int(key)
            return ids[idx] if 0 <= idx < len(ids) else None
    return None


def _current_param_names(step_type: str, params: dict[str, Any]) -> dict[str, Any]:
    """A model can still write a sort the way the prompt once told it to. The
    node reads ``sort_by``, and the editor refuses to run a sort without it."""
    if step_type == "sort" and not params.get("sort_by") and isinstance(params.get("columns"), list):
        ascending = params.get("ascending") or []
        sort_by = [
            f"{col} {'ASC' if (ascending[i] if i < len(ascending) else True) else 'DESC'}"
            for i, col in enumerate(params["columns"])
        ]
        rest = {k: v for k, v in params.items() if k not in ("columns", "ascending")}
        return {**rest, "sort_by": sort_by}
    return params


def planner_json_to_ir(
    data: dict[str, Any] | None,
    *,
    name: str = "",
    project_id: str = "",
    workspace_id: str = "",
) -> dict[str, Any] | None:
    """Normalise planner JSON into an IR dict, or None when there are no steps.

    Every step comes back with an `id`, and every connection that resolves
    comes back pointing at those ids. A connection naming a step that does
    not exist is dropped rather than carried: a draft with a dangling edge
    fails IR validation later, with an error that does not mention the
    planner.
    """
    if not data or not data.get("steps"):
        return None

    raw_steps = list(data.get("steps") or [])
    steps: list[dict[str, Any]] = []
    ids: list[str] = []
    # Everything a connection might name, pointed at the real id.
    by_key: dict[str, str] = {}

    for i, raw in enumerate(raw_steps):
        if not isinstance(raw, dict):
            continue
        step = dict(raw)
        step_id = str(step.get("id") or "").strip() or uuid.uuid4().hex[:8]
        step["id"] = step_id
        step_type = str(step.get("type") or "transform")
        step.setdefault("label", step_type.replace("_", " ").title())
        step["params"] = _current_param_names(step_type, step.get("params") or {})
        pos = step.get("position") or {}
        step["position"] = {
            "x": pos.get("x", i * 350),
            "y": pos.get("y", 100),
        }
        ids.append(step_id)
        by_key[step_id] = step_id
        label = str(step.get("label") or "").strip()
        if label:
            by_key.setdefault(label, step_id)
            by_key.setdefault(label.lower(), step_id)
        steps.append(step)

    connections: list[dict[str, Any]] = []
    for conn in data.get("connections") or []:
        if not isinstance(conn, dict):
            continue
        src = _resolve(conn.get("from_step"), ids, by_key)
        dst = _resolve(conn.get("to_step"), ids, by_key)
        if not src or not dst or src == dst:
            continue
        edge: dict[str, Any] = {"from_step": src, "to_step": dst}
        # Branch ports (a conditional split's named outputs) survive as-is.
        for extra in ("from_port", "to_port", "label"):
            if conn.get(extra):
                edge[extra] = conn[extra]
        connections.append(edge)

    ir: dict[str, Any] = {
        "name": (name or data.get("name") or "New pipeline"),
        "description": data.get("description", "") or "",
        "steps": steps,
        "connections": connections,
    }
    if project_id:
        ir["project_id"] = project_id
    if workspace_id:
        ir["workspace_id"] = workspace_id
    return ir
