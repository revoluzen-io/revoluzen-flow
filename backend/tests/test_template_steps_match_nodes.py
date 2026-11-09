"""Every step of every built-in template must use settings its node reads.

A template step is a dict of parameters. A node reads the names its
``param_schema()`` lists and nothing else, so a name outside that list does
nothing, and a missing required name leaves the node unable to run. Both went
unnoticed in ``frontend/src/templates/catalog.ts`` for most of its steps:
``derived_column`` was given ``expression`` (it reads ``columns``), the Managed
Table sink ``schema`` / ``name`` (it reads ``schema_name`` / ``table_name``),
Aggregate ``aggregations`` (it reads ``functions``), Join ``left_key`` /
``right_key`` (it reads ``join_key``), a database destination ``mode: upsert`` +
``key_columns`` (it has ``merge`` + ``merge_on``), and so on. The editor then
refused to Run, or the step quietly did something else.

This module reads the catalog and, for each template:

  * holds every step to its node's ``param_schema()`` (for a Source or
    Destination, the schema of the connector named by ``connector_type``):
    no name the node does not read, none that its ``show_when`` hides, every
    required one present, every value of the type and from the options the
    schema gives;
  * holds the list items to the shapes the nodes read (Derived Column
    ``columns``, Aggregate ``functions``, Switch ``conditions``, Semantic
    Router ``labels``);
  * runs the pipeline through the same validators the Save and Run paths use,
    where the only thing allowed to be missing is the connection the user picks;
  * checks the wiring: named output ports, parameters a step refers to,
    the Copilot's mirror of the catalog.

``test_template_steps_run.py`` runs the pipelines on small data.
``test_template_data_quality_steps.py`` does the same for Data Quality rules,
and ``frontend/src/__tests__/templateSteps.test.ts`` holds the catalog to the
palette and to the connector names.

What a template cannot ship, and the check lets through: a ``connection_id``
(the user picks a saved connection after loading) and a Slack ``webhook_url``.
"""
from __future__ import annotations

import re
from typing import Any

import pytest

from revoluzen_flow.ir.migrations import DEPRECATED_STEP_TYPES
from revoluzen_flow.ir.preflight import preflight_workflow
from revoluzen_flow.ir.schema import (
    Step as IrStep,
    StepConnection,
    StepType,
    Workflow,
    WorkflowParameter,
)
from revoluzen_flow.ir.validator import validate_workflow
from revoluzen_flow.nodes import generic
from revoluzen_flow.nodes.registry import NodeRegistry, get_registry
from tests._catalog_reader import CATALOG, Step, Template, read_catalog

pytestmark = pytest.mark.skipif(
    not CATALOG.exists(), reason=f"frontend template catalog not found at {CATALOG}"
)

get_registry()

TEMPLATES: list[Template] = read_catalog() if CATALOG.exists() else []
STEPS: list[tuple[Template, Step]] = [(t, s) for t in TEMPLATES for s in t.steps]
STEP_IDS = [f"{t.id}/{s.id}" for t, s in STEPS]

# A template cannot ship these; the user supplies them after loading it.
USER_SUPPLIED = {"connection_id", "webhook_url"}

# Connectors that read a file on disk. Every other connector is a network one,
# which reaches its system through a saved connection.
FILE_CONNECTORS = {"csv", "json", "parquet", "excel", "xml"}

SOURCE_SCHEMAS = generic.GenericSourceNode.connector_schemas()
DESTINATION_SCHEMAS = generic.GenericDestinationNode.connector_schemas()

AGGREGATE_ITEM_KEYS = {"column", "function", "alias", "expression", "percentile", "separator"}
DERIVED_ITEM_KEYS = {"name", "expression", "replace", "window"}
CONNECTION_KEYS = {"from_step", "to_step", "from_port"}
PARAMETER_KEYS = {"name", "type", "default", "description", "required"}
PARAMETER_TYPES = {"string", "int", "float", "bool", "json"}


# ── What a step's node reads ───────────────────────────────────────────────

def _connector(step: Step) -> str:
    return str(step.params.get("connector_type", "")).strip().lower()


def schema_for(step: Step) -> tuple[list[dict] | None, str | None]:
    """The node's parameter schema for this step, or (None, why there is none)."""
    if step.type in DEPRECATED_STEP_TYPES:
        return None, f"type {step.type!r} is deprecated: {DEPRECATED_STEP_TYPES[step.type].reason}"
    if step.type in ("source", "destination"):
        schemas = SOURCE_SCHEMAS if step.type == "source" else DESTINATION_SCHEMAS
        connector = _connector(step)
        if connector not in schemas:
            return None, (
                f"connector_type {connector!r} is not a {step.type} connector "
                f"(the node has: {', '.join(sorted(schemas))})"
            )
        return schemas[connector] + [{"name": "connector_type", "type": "select"}], None
    try:
        return NodeRegistry.get(StepType(step.type)).param_schema(), None
    except Exception as exc:  # noqa: BLE001 - a missing type is the finding
        return None, f"type {step.type!r} has no node ({exc})"


def defaults_for(step: Step) -> dict:
    try:
        if step.type in ("source", "destination"):
            table = generic.SOURCE_MAP if step.type == "source" else generic.DEST_MAP
            return NodeRegistry.get(table[_connector(step)]).default_params()
        return NodeRegistry.get(StepType(step.type)).default_params()
    except Exception:  # noqa: BLE001 - schema_for reports it
        return {}


def _empty(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}


def _options(param: dict) -> list:
    return [o["value"] if isinstance(o, dict) else o for o in param.get("options") or []]


def _hidden(param: dict, params: dict, defaults: dict) -> bool:
    """True when the schema shows this parameter only for other settings."""
    for key, allowed in (param.get("show_when") or {}).items():
        if params.get(key, defaults.get(key)) not in allowed:
            return True
    return False


# ── Checking a value ───────────────────────────────────────────────────────

_TEXT_TYPES = {"text", "string", "sql", "expression", "code", "column", "file", "password"}


def _item_problems(param: dict, items: Any) -> list[str]:
    """Problems with the entries of a list-valued parameter."""
    name, kind = param["name"], param.get("type")
    if kind == "derived_list":
        if not isinstance(items, list) or not items:
            return [f"{name} is not a list of {{name, expression}}"]
        out = []
        for item in items:
            if not isinstance(item, dict) or not item.get("name") or not item.get("expression"):
                out.append(f"{name} entry {str(item)[:70]!r} needs a name and an expression")
            elif set(item) - DERIVED_ITEM_KEYS:
                out.append(f"{name} entry sets {sorted(set(item) - DERIVED_ITEM_KEYS)}, which the node does not read")
        return out
    if kind == "aggregate_list":
        if not isinstance(items, list) or not items:
            return [f"{name} is not a list of {{column, function, alias}}"]
        out = []
        for item in items:
            if not isinstance(item, dict) or not item.get("function"):
                out.append(f"{name} entry {str(item)[:70]!r} needs a function")
            elif set(item) - AGGREGATE_ITEM_KEYS:
                out.append(f"{name} entry sets {sorted(set(item) - AGGREGATE_ITEM_KEYS)}, which the node does not read")
        return out
    if kind == "label_list":
        if not isinstance(items, list) or not items:
            return [f"{name} is not a list of labels"]
        return [
            f"{name} entry {str(item)[:60]!r} needs a name and example phrases"
            for item in items
            if not isinstance(item, dict) or not item.get("name") or not item.get("examples")
        ]
    if kind == "rule_list" and name == "conditions":
        if not isinstance(items, list) or not items:
            return [f"{name} is not a list of {{name, condition}}"]
        return [
            f"{name} entry {str(item)[:60]!r} needs a name and a condition"
            for item in items
            if not isinstance(item, dict) or not item.get("name") or not item.get("condition")
        ]
    return []


def _value_problems(param: dict, value: Any) -> list[str]:
    name, kind = param["name"], param.get("type")
    if kind == "select":
        options = _options(param)
        if options and value not in options and not param.get("freeform"):
            return [f"{name}={value!r} is not one of {options}"]
    elif kind == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return [f"{name}={value!r} is not a number"]
    elif kind == "boolean":
        if not isinstance(value, bool):
            return [f"{name}={value!r} is not true or false"]
    elif kind in _TEXT_TYPES:
        if not isinstance(value, str):
            return [f"{name}={str(value)[:60]!r} is not text"]
    elif kind == "column_list":
        if not (isinstance(value, str) or (isinstance(value, list) and all(isinstance(v, str) for v in value))):
            return [f"{name}={str(value)[:60]!r} is not a list of column names"]
    elif kind == "key_value":
        if not isinstance(value, dict):
            return [f"{name}={str(value)[:60]!r} is not a key/value map"]
    elif kind in ("derived_list", "aggregate_list", "label_list") or (kind == "rule_list" and name == "conditions"):
        return _item_problems(param, value)
    return []


def step_problems(step: Step) -> list[str]:
    """What is wrong with one step's params; empty when its node reads them as written."""
    schema, why = schema_for(step)
    if why:
        return [why]
    by_name = {p["name"]: p for p in schema}
    defaults = defaults_for(step)
    where = step.type + (f" ({_connector(step)})" if step.type in ("source", "destination") else "")
    problems: list[str] = []

    for key, value in step.params.items():
        if key.startswith("_"):  # added by the engine, never authored
            continue
        param = by_name.get(key)
        if param is None:
            problems.append(f"sets `{key}`, which {where} does not read")
            continue
        if _hidden(param, step.params, defaults):
            problems.append(f"sets `{key}`, which {where} shows only for {param['show_when']}")
        problems.extend(_value_problems(param, value))

    for param in schema:
        name = param["name"]
        if not param.get("required") or name in USER_SUPPLIED or param.get("type") == "connection_picker":
            continue
        if _hidden(param, step.params, defaults):
            continue
        if _empty(step.params.get(name)):
            problems.append(f"does not set `{name}`, which {where} requires")

    # The generic Source / Destination schemas do not mark the field that gives a
    # database step its job: a query, or a table in table mode.
    if step.type == "source" and _connector(step) == "database":
        mode = step.params.get("source_mode", "query")
        need = "query" if mode == "query" else "table"
        if _empty(step.params.get(need)):
            problems.append(f"does not set `{need}`, which a database source in {mode} mode needs")

    # Aggregate groups by column names; an expression there is read as a column.
    if step.type == "aggregate":
        for col in step.params.get("group_by") or []:
            if not re.fullmatch(r"[A-Za-z_][\w ]*", str(col)):
                problems.append(
                    f"group_by {col!r} is not a column name (Aggregate groups by columns; "
                    "compute an expression in an earlier step)"
                )

    # A Slack message fills {column} from the first row; {{ }} is left as it is.
    if step.type == "slack_notify" and "{{" in str(step.params.get("message", "")):
        problems.append("message uses {{ }}; Slack Notify fills single-brace {column} placeholders")

    # Network connectors reach their system through the connection the user picks.
    if step.type in ("source", "destination") and _connector(step) not in FILE_CONNECTORS:
        for key in ("url", "connection_id", "password", "token", "api_key"):
            if key in step.params:
                problems.append(f"sets `{key}`: a network {step.type} names the system through its saved connection")

    return problems


# ── Steps ──────────────────────────────────────────────────────────────────

def test_the_catalog_was_read():
    assert TEMPLATES, "no template was read from the catalog"
    ids = [t.id for t in TEMPLATES]
    assert len(ids) == len(set(ids)), "template ids are not unique"
    for t in TEMPLATES:
        step_ids = [s.id for s in t.steps]
        assert len(step_ids) == len(set(step_ids)), f"{t.id}: step ids are not unique"


@pytest.mark.parametrize("template,step", STEPS, ids=STEP_IDS)
def test_step_uses_settings_its_node_reads(template: Template, step: Step):
    assert step_problems(step) == [], f'{template.id} / {step}'


def test_the_check_would_have_caught_the_old_steps():
    """The shapes the catalog used before, each of which a node ignored or refused."""
    old = [
        Step("s", "derived_column", "x", {"expression": "SELECT *, 1 AS a FROM source_table"}),
        Step("s", "local_table_sink", "x", {"schema": "default", "name": "t", "mode": "replace"}),
        Step("s", "aggregate", "x", {"group_by": ["DATE(d) AS day"], "aggregations": [{"column": "a", "function": "SUM"}]}),
        Step("s", "join", "x", {"left_key": "id", "right_key": "id", "join_type": "left"}),
        Step("s", "destination", "x", {"connector_type": "database", "table": "t", "mode": "upsert", "key_columns": ["id"]}),
        Step("s", "destination", "x", {"connector_type": "warehouse", "file_path": "s3://b/x.parquet", "mode": "append"}),
        Step("s", "source", "x", {"connector_type": "http_trigger", "body_param": "payload"}),
        Step("s", "destination", "x", {"connector_type": "slack", "channel": "#a"}),
        Step("s", "switch_case", "x", {"expression": "region", "cases": ["EMEA"]}),
        Step("s", "webhook_trigger", "x", {"path": "/hooks/incoming"}),
        Step("s", "union", "x", {"distinct": False}),
        Step("s", "flatten_explode", "x", {"columns": ["data"]}),
        Step("s", "semantic_router", "x", {"input_column": "subject", "categories": ["a", "b"]}),
        Step("s", "lookup", "x", {"lookup_source": "users", "join_field": "user_id"}),
        Step("s", "source", "x", {"connector_type": "csv", "file_path": "a.csv", "has_header": True}),
        Step("s", "source", "x", {"connector_type": "rest_api", "url": "https://x", "method": "GET", "pagination": {"strategy": "cursor"}}),
    ]
    for step in old:
        assert step_problems(step), f"{step.type} {step.params} was accepted"


def test_the_check_accepts_the_shapes_the_nodes_read():
    good = [
        Step("s", "derived_column", "x", {"columns": [{"name": "a", "expression": "1"}]}),
        Step("s", "local_table_sink", "x", {"schema_name": "default", "table_name": "t", "mode": "merge", "merge_on": ["id"]}),
        Step("s", "aggregate", "x", {"group_by": ["day"], "functions": [{"column": "a", "function": "SUM", "alias": "s"}]}),
        Step("s", "join", "x", {"join_key": ["id"], "join_type": "LEFT"}),
        Step("s", "destination", "x", {"connector_type": "database", "table": "t", "mode": "merge", "merge_on": ["id"]}),
        Step("s", "destination", "x", {"connector_type": "warehouse", "table": "t", "mode": "append"}),
        Step("s", "source", "x", {"connector_type": "rest_api", "path": "/v1/x", "pagination": "cursor"}),
        Step("s", "slack_notify", "x", {"channel": "#a", "message": "{name} failed"}),
        Step("s", "conditional_split", "x", {"conditions": [{"name": "a", "condition": "x = 1"}]}),
    ]
    for step in good:
        assert step_problems(step) == [], step


# ── Connections ────────────────────────────────────────────────────────────

def _ports(step: Step) -> set[str]:
    """Named outputs a step offers; the mirror of frontend/src/utils/branchPorts.ts."""
    p = step.params
    if step.type == "data_quality" and p.get("mode") == "reject":
        return {"pass", "reject"}
    if step.type == "if_condition":
        return {"true", "false"}
    if step.type == "conditional_split":
        names = {str(c.get("name", "")).strip() for c in p.get("conditions", [])}
        return {n for n in names if n} | {str(p.get("default_output", "default")).strip() or "default"}
    if step.type == "deduplicate" and p.get("emit_duplicates"):
        return {"unique", "duplicate"}
    if step.type == "semantic_router" and p.get("route_outputs"):
        names = {str(label.get("name", "")).strip() for label in p.get("labels", [])}
        return {n for n in names if n} | {str(p.get("default_label", "other"))}
    return set()


@pytest.mark.parametrize("template", TEMPLATES, ids=[t.id for t in TEMPLATES])
def test_connections_join_steps_that_exist_by_ports_they_offer(template: Template):
    steps = {s.id: s for s in template.steps}
    problems = []
    for c in template.connections:
        stray = sorted(set(c) - CONNECTION_KEYS)
        if stray:
            problems.append(f"{c['from_step']} -> {c['to_step']} sets {stray}, which a connection does not carry")
        for end in ("from_step", "to_step"):
            if c[end] not in steps:
                problems.append(f"{c['from_step']} -> {c['to_step']}: no step {c[end]!r}")
        source = steps.get(c["from_step"])
        port = c.get("from_port")
        if source is None:
            continue
        offered = _ports(source)
        if port and port not in offered:
            problems.append(
                f"{c['from_step']} -> {c['to_step']} leaves by {port!r}, "
                f"but {source.type} offers {sorted(offered) or 'one output'}"
            )
        if not port and offered:
            problems.append(f"{c['from_step']} -> {c['to_step']} leaves by the default output of a {source.type}, which branches")
    assert problems == [], template.id


@pytest.mark.parametrize("template", TEMPLATES, ids=[t.id for t in TEMPLATES])
def test_a_branch_is_wired_to_every_port_it_is_given_a_case_for(template: Template):
    """A Switch case or a True / False output nothing reads is a branch that ends nowhere."""
    steps = {s.id: s for s in template.steps}
    used: dict[str, set[str]] = {}
    for c in template.connections:
        used.setdefault(c["from_step"], set()).add(c.get("from_port") or "output")
    problems = []
    for step in template.steps:
        if step.type != "conditional_split":
            continue
        cases = {str(x["name"]) for x in step.params.get("conditions", [])}
        unused = sorted(cases - used.get(step.id, set()))
        if unused:
            problems.append(f"{step}: no step reads the case(s) {unused}")
    assert problems == [], template.id
    assert steps  # the template has steps


# ── The pipeline, as Save and Run see it ───────────────────────────────────

def to_workflow(template: Template) -> Workflow:
    return Workflow(
        id=f"tpl-{template.id}",
        name=template.name,
        steps=[
            IrStep(id=s.id, type=StepType(s.type), label=s.label, params=dict(s.params))
            for s in template.steps
        ],
        connections=[
            StepConnection(
                from_step=c["from_step"], to_step=c["to_step"], from_port=c.get("from_port") or "output",
            )
            for c in template.connections
        ],
        parameters=[WorkflowParameter(**p) for p in template.parameters],
    )


def _needs_a_connection(message: str) -> bool:
    return "connection_id" in message and "requires" in message


@pytest.mark.parametrize("template", TEMPLATES, ids=[t.id for t in TEMPLATES])
def test_the_pipeline_validator_asks_only_for_the_connection(template: Template):
    """validate_workflow runs on every Save; only the saved connection is left to the user."""
    wf = to_workflow(template)
    errors = [e for e in validate_workflow(wf) if e.severity == "error"]
    assert [e.message for e in errors if not _needs_a_connection(e.message)] == [], template.id


@pytest.mark.parametrize("template", TEMPLATES, ids=[t.id for t in TEMPLATES])
def test_the_structure_check_finds_no_error(template: Template):
    findings = [f for f in preflight_workflow(to_workflow(template)) if f["severity"] == "error"]
    assert [f["message"] for f in findings] == [], template.id


# ── Parameters ─────────────────────────────────────────────────────────────

_PARAM_REF = re.compile(r"\$\{param\.([A-Za-z_][A-Za-z0-9_]*)\}")


def _strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)


@pytest.mark.parametrize("template", TEMPLATES, ids=[t.id for t in TEMPLATES])
def test_parameters_a_step_refers_to_are_declared(template: Template):
    declared = {p["name"] for p in template.parameters}
    used = {m for s in template.steps for text in _strings(s.params) for m in _PARAM_REF.findall(text)}
    assert sorted(used - declared) == [], f"{template.id}: ${{param.x}} with no declared parameter"


@pytest.mark.parametrize("template", TEMPLATES, ids=[t.id for t in TEMPLATES])
def test_declared_parameters_are_complete(template: Template):
    for p in template.parameters:
        assert set(p) == PARAMETER_KEYS, f"{template.id}: {p.get('name')} has keys {sorted(p)}"
        assert p["type"] in PARAMETER_TYPES, f"{template.id}: {p['name']} type {p['type']!r}"
        assert p["description"], f"{template.id}: {p['name']} has no description"
        WorkflowParameter(**p)


def test_a_webhook_template_declares_what_its_endpoint_passes():
    """The request body becomes parameter values for declared parameters only."""
    for template in TEMPLATES:
        if "Webhook" in template.tags:
            assert template.parameters, f"{template.id} is started by a webhook but declares no parameter"


# ── The Copilot's mirror of the catalog ────────────────────────────────────

def test_the_copilots_copy_of_the_catalog_counts_the_same_nodes():
    from revoluzen_flow.templates.catalog_meta import BUILTIN_TEMPLATES

    by_id = {t.id: t for t in TEMPLATES}
    problems = []
    for meta in BUILTIN_TEMPLATES:
        template = by_id.get(meta["id"])
        if template is None:
            problems.append(f"{meta['id']}: not in the catalog")
        elif meta["node_count"] != len(template.steps):
            problems.append(f"{meta['id']}: node_count {meta['node_count']}, catalog has {len(template.steps)} steps")
    assert problems == []


# ── The planner's scaffolds ────────────────────────────────────────────────

def test_planner_starter_pipelines_use_settings_their_nodes_read():
    """The four starters the chat panel offers (the frontend keeps a copy of three of them).

    The enterprise scaffolds beside them are left out on purpose: they carry <your-...>
    placeholders for a person to fill in, and their shape is pinned by
    test_planner_enterprise_templates.py. Their Data Quality steps are held below.
    """
    from revoluzen_flow.planner.templates import create_from_template

    problems = []
    for key in ("simple_etl", "dedup_pipeline", "aggregation", "data_quality"):
        wf = create_from_template(key)
        assert wf is not None, key
        for s in wf.steps:
            step = Step(s.id, s.type.value, s.label, dict(s.params))
            problems.extend(f"{key}/{s.id}: {p}" for p in step_problems(step))
    assert problems == []


def test_planner_scaffolds_write_data_quality_in_the_shape_the_node_reads():
    from revoluzen_flow.nodes.quality import DataQualityNode, _DQ_OPS
    from revoluzen_flow.planner.templates import TEMPLATES as SCAFFOLDS, create_from_template

    modes = next(p["options"] for p in DataQualityNode.param_schema() if p["name"] == "mode")
    allowed = set(DataQualityNode.default_params())
    problems = []
    for key in SCAFFOLDS:
        wf = create_from_template(key)
        for step in wf.steps if wf else []:
            if step.type != StepType.DATA_QUALITY:
                continue
            p = step.params
            stray = sorted(set(p) - allowed)
            if stray:
                problems.append(f"{key}/{step.id}: sets {stray}, which Data Quality does not read")
            if p.get("mode") not in modes:
                problems.append(f"{key}/{step.id}: mode {p.get('mode')!r} is not one of {modes}")
            for rule in p.get("rules") or []:
                if set(rule) - {"column", "op", "value"} or not rule.get("column") or rule.get("op") not in _DQ_OPS:
                    problems.append(f"{key}/{step.id}: rule {rule} is not {{column, op, value?}} with a known op")
            if not p.get("rules"):
                problems.append(f"{key}/{step.id}: no rules")
    assert problems == []
