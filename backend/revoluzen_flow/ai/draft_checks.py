"""Whether a model-written draft is safe for this product to EXECUTE.

`_edit_guards.py` decides what the Copilot may draft. This decides what may
then be run, which is a different and sharper question: `test_pipeline_draft`
hands a draft to the real engine, and a draft's parameters are model-written
text. A `file_path` or a SQL fragment the model chose could read something
nobody offered it.

The rule is a comparison, not a whitelist of values. Parameters that came
from the saved pipeline were written by a person and are trusted as far as
that person's access goes. Anything the draft ADDED or CHANGED was written
by a model and is held to the stricter rules: it may not move where a step
reads from, and it may not contain SQL that reaches out of the pipeline.

Ported from the Enterprise edition, minus its saved-golden-test hooks, which
OSS has no store for.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

from revoluzen_flow.ai.tools._edit_guards import PROTECTED_STEP_TYPES
from revoluzen_flow.ai.tools.inspect_step_columns import (
    _READ_ONLY_STEPS,
    _can_preview,
    _is_source,
)

# Steps whose job is to stop bad data. A failing one is working correctly.
# Shared with the edit guards so "what counts as a check" is defined once.
CHECK_STEP_TYPES = frozenset(PROTECTED_STEP_TYPES | {"expectations", "fail"})

# Steps that remove rows, and so can hide them from a check placed after.
ROW_REMOVING_STEPS = frozenset({
    "filter", "transform", "deduplicate", "sample", "data_wrangler",
})

# Where a step reads from or writes to. A draft may not repoint one of these
# and then have the result executed: that would read data nobody chose to
# expose to the assistant.
_LOCATION_PARAMS = frozenset({
    "connection_id", "connector_type", "credential_id", "file_path", "path",
    "url", "uri", "bucket", "container", "table", "table_name", "database",
    "schema_name", "query", "sql_file",
})

# SQL that leaves the pipeline: reads a file, attaches a database, reaches a
# URL, or asks the engine about its own installation.
_UNSAFE_SQL = tuple(re.compile(p, re.IGNORECASE) for p in (
    r"\bread_\w+\s*\(",
    r"\b\w+_scan\s*\(",
    r"\bglob\s*\(",
    r"\bsniff_csv\s*\(",
    r"\bquery(_table)?\s*\(",
    r"\b(copy|attach|detach|install|load|export|import)\s",
    r"\bpragma\b",
    r"\bset\s+\w+",
    r"\bcreate\s+(or\s+replace\s+)?(persistent\s+)?secret",
    r"\bduckdb_\w+",
    r"\bgetenv\b",
    r"\b(from|join)\s+['\"]",
    r"://",
    r"\.\.[\\/]",
    r"\b[a-z]:[\\/]",
))


def step_kind(step: dict[str, Any]) -> str:
    kind = step.get("type")
    return str(getattr(kind, "value", kind) or "")


def is_sink(step: dict[str, Any]) -> bool:
    kind = step_kind(step)
    return kind in ("destination", "output", "db_sink") or kind.endswith("_sink")


def is_check(step: dict[str, Any]) -> bool:
    return step_kind(step) in CHECK_STEP_TYPES


def _all_strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _all_strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _all_strings(item)


class _StepView:
    """The attribute shape the column tool's helpers read."""

    def __init__(self, kind: str, params: dict[str, Any]) -> None:
        self.type = kind
        self.params = params


def bypass_reason(ir: dict[str, Any], args: dict[str, Any]) -> str:
    """Why inserting this step would route rows around a check, or "".

    Deleting a check is the obvious move and is already refused. Slipping a
    filter in just upstream of one is the same outcome with the check left
    visibly in place, which is worse: the pipeline still shows a check, and
    the rows it would have caught never reach it.
    """
    new_step = args.get("step") if isinstance(args.get("step"), dict) else {}
    anchor = args.get("after_step_id")
    if not anchor or step_kind(new_step) not in ROW_REMOVING_STEPS:
        return ""
    steps = {s.get("id"): s for s in ir.get("steps") or []}
    for conn in ir.get("connections") or []:
        if conn.get("from_step") != anchor:
            continue
        following = steps.get(conn.get("to_step"))
        if following is not None and is_check(following):
            return (
                f"A {step_kind(new_step)} step here would remove rows before "
                f"{step_kind(following)} step {following.get('id')!r} sees them, "
                f"which gets past the check without touching it."
            )
    return ""


def unsafe_to_test_reason(
    steps: list[dict[str, Any]],
    saved_steps: list[dict[str, Any]],
) -> str:
    """Why this draft must not be executed, or an empty string.

    Sinks are never run by the test, so their settings do not matter here.
    """
    saved = {s.get("id"): s for s in saved_steps}
    for step in steps:
        if is_sink(step):
            continue
        step_id = step.get("id")
        kind = step_kind(step)
        params = step.get("params") or {}
        before = saved.get(step_id)
        unchanged = (
            before is not None
            and (before.get("params") or {}) == params
            and step_kind(before) == kind
        )

        if _is_source(_StepView(kind, params)):
            # A source is where data enters. The draft may not introduce one
            # or move an existing one, and reading it must be a read.
            if not unchanged:
                return f"source step {step_id!r} differs from the saved pipeline"
            if not _can_preview(_StepView(kind, params)):
                return f"reading source step {step_id!r} may do more than read"
            continue
        if kind not in _READ_ONLY_STEPS:
            return f"step {step_id!r} of type {kind!r} may have side effects"
        if unchanged:
            continue
        old_params = (before or {}).get("params") or {}
        for key in _LOCATION_PARAMS:
            if params.get(key) != old_params.get(key):
                return f"step {step_id!r} changes where it reads from ({key})"
        for text in _all_strings(params):
            for pattern in _UNSAFE_SQL:
                if pattern.search(text):
                    return (
                        f"a parameter of step {step_id!r} could reach outside "
                        f"the pipeline"
                    )
    return ""
