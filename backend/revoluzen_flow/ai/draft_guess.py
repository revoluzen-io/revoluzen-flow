"""
Refuse a draft that guesses, however confident the model is.

A real model told to ask when two columns could fit still picked one, and a test
cannot catch it, because a guess that runs passes. So when an edit replaces a
column that is gone, this compares the edit with the columns the step really
receives:

  - nothing there resembles the missing column: there is nothing to fix it with,
  - several do: choosing one is a business decision,
  - one does, and the edit uses something else: that is not the rename either.

The same goes for repointing a source at a file or connection nobody named, and for
a value in an edit that nobody gave. The agent sees column names and types, never
row values, so a new date threshold or category it writes is made up. It ran, so
the test would pass it.

The user settles it by saying which one in their request, which is the only text
here a pipeline's own content cannot write. A person can also make any of these
edits in the editor.

The check needs the real columns. If they cannot be read safely it does not apply,
and the draft still has to pass test_pipeline_draft and a person's review.
"""

from __future__ import annotations

import re
from typing import Any

_MAX_LISTED = 12

_QUOTED_TEXT = re.compile(r"'(?:[^']|'')*'")
_NAME = re.compile(r'"([^"]+)"|([A-Za-z_][A-Za-z0-9_]*)(\s*\()?')
_TYPE_AFTER_AS = re.compile(r"\bas\s+\w+(\s*\(\s*\d+(\s*,\s*\d+)?\s*\))?", re.IGNORECASE)
_NUMBER = re.compile(r"(?<![\w.])\d+(?:\.\d+)?(?![\w.])")
# Words that make a column a different thing, not a new name for the same one:
# tax_amount is not amount, however much the names overlap.
_DIFFERENT_THING = frozenset({
    "tax", "vat", "gst", "fee", "fees", "discount", "shipping", "freight", "refund", "refunded", "cost",
    "margin", "profit", "net", "gross", "min", "max", "avg", "average", "previous", "prev", "old", "budget",
    "target", "estimated", "estimate", "forecast",
})
_SQL_WORDS = frozenset({
    "and", "or", "not", "in", "is", "null", "like", "ilike", "between", "case", "when", "then", "else",
    "end", "true", "false", "as", "distinct", "exists", "select", "from", "where", "over", "partition",
    "by", "order", "asc", "desc", "interval", "date", "timestamp",
})
_WORD_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|[^A-Za-z0-9]+")

# Where a source reads from. Changing one of these points the pipeline at other data.
_SOURCE_LOCATION = ("file_path", "path", "url", "uri", "bucket", "container", "table", "table_name",
                    "connection_id", "connector_type", "query")


def _step_kind(step: dict[str, Any]) -> str:
    kind = step.get("type")
    return str(getattr(kind, "value", kind) or "")


def _sql_names(text: Any) -> set[str]:
    if not isinstance(text, str):
        return set()
    bare = _TYPE_AFTER_AS.sub(" ", _QUOTED_TEXT.sub(" ", text))
    names = set()
    for match in _NAME.finditer(bare):
        quoted, word, call = match.groups()
        if quoted:
            names.add(quoted.lower())
        elif word and not call and word.lower() not in _SQL_WORDS:
            names.add(word.lower())
    return names


def _listed(value: Any) -> set[str]:
    if isinstance(value, str):
        return {v.strip().lower() for v in value.split(",") if v.strip()}
    if isinstance(value, (list, tuple)):
        return {str(v).strip().lower() for v in value if str(v).strip()}
    return set()


def _rule_columns(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        if isinstance(value.get("column"), str):
            found.add(value["column"].lower())
        for item in value.values():
            found |= _rule_columns(item)
    elif isinstance(value, list):
        for item in value:
            found |= _rule_columns(item)
    return found


def references(kind: str, params: dict[str, Any]) -> set[str]:
    """The input columns a step's settings name. Output names such as an alias are not inputs."""
    if kind == "filter":
        return _sql_names(params.get("condition")) | _rule_columns(params.get("rules")) | _rule_columns(params.get("rule_groups"))
    if kind == "derived_column":
        return {n for c in params.get("columns") or [] if isinstance(c, dict) for n in _sql_names(c.get("expression"))}
    if kind == "aggregate":
        functions = params.get("functions") or []
        columns = set(functions) if isinstance(functions, dict) else {
            str(f.get("column")) for f in functions if isinstance(f, dict) and f.get("column")
        }
        return _listed(params.get("group_by")) | {c.lower() for c in columns if c != "*"}
    if kind == "deduplicate":
        return _listed(params.get("key"))
    return set()


def _tokens(name: str) -> set[str]:
    return {t.lower() for t in _WORD_BOUNDARY.split(name) if t}


def _abbreviates(short: str, word: str) -> bool:
    """qty / quantity, amt / amount, cust / customer: same first letter, letters in order."""
    if len(short) < 3 or len(short) >= len(word) or short[0] != word[0]:
        return False
    letters = iter(word)
    return all(letter in letters for letter in short)


def _resembles(missing: str, column: str) -> bool:
    a, b = _tokens(missing), _tokens(column)
    if not a or not b:
        return False
    if a <= b or b <= a:
        return True
    return any(_abbreviates(x, y) or _abbreviates(y, x) for x in a for y in b)


def _is_different_thing(missing: str, column: str) -> bool:
    return bool((_tokens(column) - _tokens(missing)) & _DIFFERENT_THING)


def _said_name(request: str, name: str) -> bool:
    return name.lower() in {t.lower() for t in re.findall(r"[A-Za-z0-9_]+", request or "")}


def _named_in(request: str, column: str, others: list[str]) -> bool:
    """Did the user say this column, in full or by the word that sets it apart?"""
    said = {t.lower() for t in re.findall(r"[A-Za-z0-9_]+", request or "")}
    if column.lower() in said:
        return True
    shared = set().union(*(_tokens(o) for o in others if o.lower() != column.lower())) if others else set()
    return bool((_tokens(column) - shared) & {w for t in said for w in _tokens(t)})


def _shown(columns: list[str]) -> str:
    return ", ".join(columns[:_MAX_LISTED])


def column_guess_reason(
    kind: str, old: dict[str, Any], new: dict[str, Any], available: list[str], request: str,
) -> str:
    """Why this edit is a guess at a missing column, or an empty string."""
    was, now = references(kind, old), references(kind, new)
    by_lower = {c.lower(): c for c in available}
    for missing in sorted(was - set(by_lower)):
        if missing in now:
            continue  # it does not replace the column, so the test will show that
        candidates = [c for low, c in by_lower.items() if low not in was and _resembles(missing, c)]
        chosen = [by_lower[c] for c in sorted(now - was) if c in by_lower]
        if not candidates:
            if _said_name(request, missing) or any(_said_name(request, c) for c in chosen):
                continue
            return (
                f"Column {missing!r} no longer exists and nothing the step receives looks like its "
                f"replacement ({_shown(available)}). Do not draft a change: say it is missing and ask "
                "the user what should replace it."
            )
        # Weigh a choice against the other options, or against the missing name when
        # there is only one, so the word they share never counts as naming it.
        if any(_named_in(request, c, candidates if len(candidates) > 1 else [missing]) for c in chosen):
            continue
        if len(candidates) > 1:
            return (
                f"Column {missing!r} no longer exists and more than one column could replace it: "
                f"{_shown(candidates)}. Choosing one changes what this step means. Do not draft a change: "
                "ask the user which one to use."
            )
        if _is_different_thing(missing, candidates[0]):
            return (
                f"Column {missing!r} no longer exists. {candidates[0]!r} shares a word with it but looks like a "
                "different thing, so using it would change every result. Do not draft a change: ask the user "
                "where this value should come from."
            )
        if candidates[0].lower() not in now:
            return (
                f"Column {missing!r} no longer exists. The closest column is {candidates[0]!r}, but this "
                "edit does not use it. Do not guess: ask the user what should replace it."
            )
    return ""


def source_guess_reason(step: dict[str, Any], new: dict[str, Any], request: str) -> str:
    """Why this edit repoints a source at something the user did not name, or an empty string."""
    kind = _step_kind(step)
    if not (kind == "source" or kind.endswith("_source")):
        return ""
    old = step.get("params") or {}
    said = (request or "").lower()
    for key in _SOURCE_LOCATION:
        value = new.get(key)
        if value == old.get(key) or value in (None, ""):
            continue
        if str(value).lower() not in said:
            return (
                f"This edit points source step {step.get('id')!r} at a different {key} the user did not "
                "name. Do not guess where the data moved: say what is missing and ask."
            )
    return ""


def _expressions(kind: str, params: dict[str, Any]) -> list[str]:
    if kind == "filter":
        rules = [str(r.get("value")) for r in params.get("rules") or [] if isinstance(r, dict) and r.get("value") not in (None, "")]
        return [str(params.get("condition") or ""), *(repr(v) for v in rules)]
    if kind == "derived_column":
        return [str(c.get("expression") or "") for c in params.get("columns") or [] if isinstance(c, dict)]
    return []


def _literals(text: str) -> set[str]:
    quoted = {m.group()[1:-1].replace("''", "'") for m in _QUOTED_TEXT.finditer(text)}
    numbers = set(_NUMBER.findall(_TYPE_AFTER_AS.sub(" ", _QUOTED_TEXT.sub(" ", text))))
    return quoted | numbers


def value_guess_reason(kind: str, old: dict[str, Any], new: dict[str, Any], request: str) -> str:
    """Why this edit writes a data value nobody gave, or an empty string.

    Punctuation, format strings such as '%b %d %Y' and 0 or 1 say how to read data,
    not what it holds, so they pass. Anything else new must be in the user's words.
    """
    was = {v for text in _expressions(kind, old) for v in _literals(text)}
    said = (request or "").lower()
    for value in sorted({v for text in _expressions(kind, new) for v in _literals(text)} - was):
        if value in ("0", "1") or "%" in value or not re.search(r"[A-Za-z0-9]", value) or value.lower() in said:
            continue
        return (
            f"This edit uses the value {value!r}, which is not in the pipeline or the user's request. You can "
            "see column names and types, not the values in the data, so this would be a guess. Do not draft it. "
            "Say you cannot see the values, and ask the user what they are or for a sample."
        )
    return ""


def upstream_columns(ir: dict[str, Any], step_id: str, data_dir: str, app_state: Any) -> list[str] | None:
    """The columns this step receives, read through the engine. None when unknown.

    An upstream step may itself be broken, as in a pipeline with two faults, so the
    nearest steps that did run stand in for it: a step that fails is the fault being
    fixed, not a sign that its input columns are gone.
    """
    from revoluzen_flow.ai.draft_checks import unsafe_to_test_reason
    from revoluzen_flow.ai.tools.inspect_step_columns import _can_preview
    from revoluzen_flow.engine.executor import WorkflowExecutor
    from revoluzen_flow.ir.schema import Workflow

    payload = {k: v for k, v in ir.items() if not k.startswith("_")}
    workflow = Workflow(**{**payload, "id": f"{ir.get('id') or 'pipeline'}-draft"})
    # A step in the base may be model-written, from an earlier edit in this request.
    if unsafe_to_test_reason(ir.get("steps") or [], ir.get("steps") or []):
        return None
    executor = WorkflowExecutor(data_dir=data_dir, app_state=app_state)
    inputs = executor._build_input_map(workflow).get(step_id, [])
    if not inputs:
        return None
    by_id = {s.id: s for s in workflow.steps}
    for target in inputs:
        needed = [by_id[target], *executor._get_dependencies(workflow, target)]
        if not all(_can_preview(s) for s in needed):
            return None

    columns: list[str] = []
    for target in inputs:
        trace = executor.execute_step_trace(workflow, target, preview_limit=1)
        target_result = trace.step_results.get(target)
        if target_result is not None and target_result.status == "success":
            columns.extend(target_result.columns or [])
            continue
        ran = [r for r in trace.step_results.values() if r.status == "success" and r.columns]
        if not ran:
            return None
        columns.extend(ran[-1].columns)
    return list(dict.fromkeys(columns))
