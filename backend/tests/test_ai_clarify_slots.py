"""The Copilot asks where the data comes from and where it goes, before drafting.

Covers the rule-based reading of a request (from/to parts, what it already
names), the questions built from this workspace's real connections, files and
managed tables, the answers reaching the draft, and secrets staying out of it
all. Without an AI provider the whole flow must still work, so that is the path
most of these take.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from types import SimpleNamespace

import duckdb
import pytest

from revoluzen_flow.ai.clarify_draft import (
    ClarificationSet,
    InventoryConnection,
    InventoryFile,
    InventoryTable,
    WorkspaceInventory,
    clarification_payload,
    clarification_set_from_slots,
    detect_draft_slots,
    detect_missing_draft_fields,
    is_build_request,
    render_clarification_card,
    render_questions_for_model,
)
from revoluzen_flow.ai.clarify_to_template import populate_template
from revoluzen_flow.ai.draft_choices import (
    DraftChoices,
    apply_choices_to_workflow,
    choices_from_answers,
    choices_from_reply,
    choices_from_slots,
    describe_choices_for_planner,
    read_workspace_inventory,
)
from revoluzen_flow.ai.clarify_draft import DraftEndpoint
from revoluzen_flow.api import planner
from revoluzen_flow.connections.models import Connection
from revoluzen_flow.datastore.models import StorageObject, StorageTable
from revoluzen_flow.engine.executor import WorkflowExecutor
from revoluzen_flow.ir.schema import Step, StepConnection, StepType, Workflow
from revoluzen_flow.ir.validator import validate_workflow
from revoluzen_flow.nodes.base import ExecutionContext
from revoluzen_flow.nodes.generic import GenericDestinationNode, GenericSourceNode
from revoluzen_flow.planner.rule_planner import RulePlanner


FILM_PROMPT = "Build a pipeline that shows returns by reason"
SYNTHETIC_SECRET = "synthetic-password-not-real-42"

DEMO = WorkspaceInventory(
    connections=(
        InventoryConnection("c_stock", "Stockroom database", "sqlite", ("read", "write")),
        InventoryConnection("c_price", "Partner price API", "rest_api", ("read", "write")),
    ),
    files=(InventoryFile("obj_tickets", "tickets.csv", "uploads/default/tickets-1.csv", "csv"),),
)

WITH_DATABASES = WorkspaceInventory(
    connections=(
        InventoryConnection("c_pg", "Sales Postgres", "postgresql"),
        InventoryConnection("c_pg2", "Finance Postgres", "postgresql"),
        InventoryConnection("c_sf", "Analytics Snowflake", "snowflake"),
    ),
    files=(
        InventoryFile("obj_o", "orders.csv", "uploads/default/orders-1.csv", "csv"),
        InventoryFile("obj_c", "customers.csv", "uploads/default/customers-1.csv", "csv"),
    ),
    tables=(InventoryTable("default", "customers_clean"),),
)


def _question(slots, field):
    return next(q for q in slots.questions if q.field == field)


# ── Reading the from and to parts ─────────────────────────────────────────


@pytest.mark.parametrize("prompt,source,sink", [
    ("Copy customers from Postgres to Snowflake every night", "postgres", "snowflake"),
    ("Build a pipeline: Copy customers from Postgres to Snowflake every night", "postgres", "snowflake"),
    ("Sync Salesforce accounts to BigQuery", "salesforce", "bigquery"),
    ("Build a Snowflake to BigQuery pipeline", "snowflake", "bigquery"),
    ("Build a pipeline that reads from a Postgres orders table and writes to S3 as Parquet", "postgres", "s3"),
    # A format describes the system, it isn't one.
    ("Load CSV from S3 to Snowflake", "s3", "snowflake"),
    # Everyday words that happen to name something.
    ("Copy warehouse stock from Postgres to Snowflake", "postgres", "snowflake"),
    ("Copy customer email from Postgres to Snowflake", "postgres", "snowflake"),
    ("Create a pipeline to fetch Oracle BIP data every 6 hours and load into SQL Server", "oracle_bip", "sql_server"),
])
def test_from_and_to_are_read_from_the_sentence(prompt, source, sink):
    slots = detect_draft_slots(prompt, WorkspaceInventory())
    assert slots.source_type == source
    assert slots.sink_type == sink


@pytest.mark.parametrize("prompt", [
    "why did my pipeline fail?",
    "what is a pipeline?",
    "show me failed pipelines",
    "make this pipeline faster",
    "create a schedule for the pipeline",
    "generate documentation for this pipeline",
    "add a filter step",
    "",
])
def test_not_a_build_request(prompt):
    assert not is_build_request(prompt)
    assert detect_draft_slots(prompt, DEMO) is None
    assert detect_missing_draft_fields(prompt, DEMO) is None


def test_a_request_that_names_nothing_asks_both_ends():
    slots = detect_draft_slots("Build a pipeline that shows open tickets by team", DEMO)
    assert [q.field for q in slots.questions] == ["source", "destination"]
    assert slots.source is None and slots.destination is None


def test_the_heading_is_plain_and_uses_real_names():
    slots = detect_draft_slots("Copy customers from Postgres to Snowflake every night", WITH_DATABASES)
    assert slots.detected_intent == "Build a pipeline from PostgreSQL to Snowflake"
    rendered = render_clarification_card(clarification_set_from_slots(slots))
    assert "*" not in rendered
    assert "postgres to postgres" not in rendered.lower()


# ── Real options from this workspace ──────────────────────────────────────


def test_film_request_lists_the_real_workspace():
    slots = detect_draft_slots(FILM_PROMPT, DEMO)
    source = _question(slots, "source")
    assert source.question == "Where should I read the data from?"
    assert source.chips == (
        "Stockroom database (SQLite)",
        "Partner price API (REST API)",
        "tickets.csv (uploaded file)",
        "New connection",
    )
    destination = _question(slots, "destination")
    assert destination.question == "Where should I write the result?"
    assert destination.chips == (
        "New managed table in this workspace",
        "Stockroom database (SQLite)",
        "Partner price API (REST API)",
        "New connection",
    )
    assert destination.options[0].recommended is True
    assert destination.options[0].table_name == "returns_by_reason"


def test_prompt_words_put_the_matching_file_first():
    slots = detect_draft_slots("Build a pipeline that shows open tickets by team", DEMO)
    assert _question(slots, "source").chips[0] == "tickets.csv (uploaded file)"


def test_named_technology_lists_only_its_connections():
    slots = detect_draft_slots("Copy customers from Postgres to Snowflake", WITH_DATABASES)
    source = _question(slots, "source")
    assert source.question == "Which PostgreSQL connection should I read from?"
    assert source.chips == ("Finance Postgres", "Sales Postgres", "New PostgreSQL connection")
    assert source.options[-1].new_connection_type == "postgresql"
    assert source.table_hint == "customers"
    destination = _question(slots, "destination")
    assert destination.question == "Which Snowflake connection should I write to?"
    assert destination.chips == ("Analytics Snowflake", "New Snowflake connection")


def test_named_technology_with_no_connection_says_so():
    slots = detect_draft_slots("Copy customers from Postgres to Snowflake", DEMO)
    destination = _question(slots, "destination")
    assert destination.question.startswith("This workspace has no Snowflake connection yet.")
    assert destination.chips == ("New Snowflake connection",)
    assert destination.options[0].new_connection_type == "snowflake"


def test_the_list_stays_short_and_ends_with_new_connection():
    many = WorkspaceInventory(
        connections=tuple(InventoryConnection(f"c{i}", f"Conn {i}", "postgresql") for i in range(9)),
        files=tuple(InventoryFile(f"o{i}", f"file{i}.csv", f"uploads/f{i}.csv") for i in range(9)),
        tables=tuple(InventoryTable("default", f"table{i}") for i in range(9)),
    )
    slots = detect_draft_slots(FILM_PROMPT, many)
    for field in ("source", "destination"):
        question = _question(slots, field)
        assert len(question.chips) <= 6
        assert question.options[-1].kind == "new_connection"
        # One long list doesn't crowd out the other kinds.
        assert {o.kind for o in question.options} >= {"connection", "table"}


def test_connections_are_filtered_by_what_they_can_do():
    inventory = WorkspaceInventory(connections=(
        InventoryConnection("r", "Read only db", "postgresql", ("read",)),
        InventoryConnection("w", "Write only db", "postgresql", ("write",)),
        InventoryConnection("s", "Team channel", "slack", ("write",)),
        InventoryConnection("d", "Metrics", "datadog", ("read", "write")),
    ))
    slots = detect_draft_slots(FILM_PROMPT, inventory)
    source_ids = {o.connection_id for o in _question(slots, "source").options}
    destination_ids = {o.connection_id for o in _question(slots, "destination").options}
    assert "r" in source_ids and "w" not in source_ids and "s" not in source_ids
    assert "w" in destination_ids and "s" in destination_ids and "r" not in destination_ids
    # No pipeline step can use this type, so it isn't offered.
    assert "d" not in source_ids and "d" not in destination_ids


# ── A request that already names what exists ──────────────────────────────


def test_named_uploaded_files_and_a_managed_table_draft_straight_away():
    prompt = "Join orders.csv with customers.csv on customer_id and save to a managed table"
    assert detect_missing_draft_fields(prompt, WITH_DATABASES) is None
    slots = detect_draft_slots(prompt, WITH_DATABASES)
    assert [f.file_path for f in slots.source_files] == [
        "uploads/default/orders-1.csv", "uploads/default/customers-1.csv",
    ]
    assert slots.destination.kind == "new_table"
    # Nothing else names the table, so it is named after what it reads.
    assert slots.destination.table == "orders_customers"


def test_a_new_table_offered_for_a_named_file_is_named_after_it():
    slots = detect_draft_slots("Build a pipeline that reads tickets.csv", DEMO)
    assert _question(slots, "destination").options[0].table_name == "tickets"


def test_hero_framing_does_not_change_it():
    prompt = "Build a pipeline: Join orders.csv with customers.csv on customer_id and save to a managed table"
    assert detect_missing_draft_fields(prompt, WITH_DATABASES) is None


def test_named_connection_needs_no_question_for_that_end():
    slots = detect_draft_slots("Copy customers from Sales Postgres to a managed table", WITH_DATABASES)
    assert slots.questions == ()
    assert slots.source.connection_id == "c_pg"
    assert slots.source.table == "customers"
    assert slots.destination.table == "customers"


def test_named_connection_without_a_table_asks_only_for_the_table():
    slots = detect_draft_slots("Build a pipeline that reads Sales Postgres into a managed table", WITH_DATABASES)
    assert [q.field for q in slots.questions] == ["source_table"]
    assert slots.questions[0].connection_id == "c_pg"
    assert slots.questions[0].question == "Which table in Sales Postgres should I read?"


def test_named_destination_connection_asks_how_to_write():
    slots = detect_draft_slots("Copy customers from Sales Postgres to Finance Postgres", WITH_DATABASES)
    assert [q.field for q in slots.questions] == ["write_mode"]
    assert slots.destination.connection_id == "c_pg2"


def test_a_stated_write_mode_is_not_asked_again():
    slots = detect_draft_slots("Copy customers from Sales Postgres and upsert on id into Finance Postgres", WITH_DATABASES)
    assert slots.write_mode == "merge"
    assert slots.merge_on == ("id",)
    assert slots.questions == ()


def test_merging_files_is_not_a_write_mode():
    slots = detect_draft_slots("merge orders.csv with customers.csv and replace nulls with 0", WITH_DATABASES)
    assert slots.write_mode == ""


def test_a_named_file_storage_lacks_is_asked_about():
    slots = detect_draft_slots("Build a pipeline that reads sales.csv and writes to a Parquet file", DEMO)
    question = _question(slots, "source")
    assert question.question == "I could not find sales.csv in this workspace's Storage. Which file should I read instead?"
    assert "tickets.csv (uploaded file)" in question.chips
    assert "Keep sales.csv. I will upload it later." in question.chips
    assert slots.missing_file == "sales.csv"
    # The destination was named, so it isn't asked.
    assert [q.field for q in slots.questions] == ["source"]


def test_a_file_outside_storage_counts_when_it_exists():
    inventory = WorkspaceInventory(file_on_disk=lambda name: name == "sales.csv")
    assert detect_draft_slots("Build a pipeline that reads sales.csv and writes to a Parquet file", inventory).questions == ()


def test_without_an_inventory_the_generic_chips_stay():
    cset = detect_missing_draft_fields("Build a pipeline that loads daily.csv into SQL Server.")
    assert isinstance(cset, ClarificationSet)
    assert cset.source_type == "csv_file" and cset.sink_type == "sql_server"
    assert "sql_server_connection" in {q.field for q in cset.questions}


def test_card_payload_is_ready_for_the_frontend():
    payload = clarification_payload(clarification_set_from_slots(detect_draft_slots(FILM_PROMPT, DEMO)))
    assert payload["question_count"] == 2
    assert payload["questions"][0]["slot"] == "source"
    option = payload["questions"][0]["options"][0]
    assert option == {
        "id": "connection:c_stock",
        "label": "Stockroom database (SQLite)",
        "kind": "connection",
        "connection_id": "c_stock",
        "connection_name": "Stockroom database",
        "connection_type": "sqlite",
    }
    assert "sqlite" in payload["table_connection_types"]
    assert payload["write_mode_connection_types"] == ["postgresql", "mysql", "mssql", "sqlite"]


# ── The answers reach the draft ───────────────────────────────────────────


def _rule_draft(prompt: str, choices: DraftChoices) -> Workflow:
    workflow = RulePlanner().plan(prompt, assume_source=choices.binds_source).workflow
    return apply_choices_to_workflow(workflow, choices)


def _step(workflow: Workflow, step_type: StepType) -> Step:
    return next(s for s in workflow.steps if s.type == step_type)


def test_card_answers_are_looked_up_again_and_reach_both_ends():
    slots = detect_draft_slots("Copy customers from Postgres to Snowflake every night", WITH_DATABASES)
    choices = choices_from_answers(
        {
            "source": {"kind": "connection", "connection_id": "c_pg", "table": "customers", "schema": "public"},
            "destination": {"kind": "connection", "connection_id": "c_sf", "table": "customers"},
        },
        slots, WITH_DATABASES,
    )
    workflow = _rule_draft("Copy customers from Postgres to Snowflake every night", choices)
    assert validate_workflow(workflow) == []
    assert [s.type for s in workflow.steps] == [StepType.SOURCE, StepType.DESTINATION]
    source, sink = workflow.steps
    assert source.params == {
        "connector_type": "database", "connection_id": "c_pg", "_connection_type": "postgresql",
        "source_mode": "table", "table": "customers", "schema": "public",
    }
    assert sink.params["connection_id"] == "c_sf"
    assert sink.params["connector_type"] == "database"
    assert sink.params["table"] == "customers"
    assert source.label == "Read customers from Sales Postgres"
    assert sink.label == "Write to customers in Analytics Snowflake"
    assert len(workflow.connections) == 1
    assert choices.schedule == "every night"


def test_an_id_from_elsewhere_resolves_to_nothing():
    choices = choices_from_answers(
        {"source": {"kind": "connection", "connection_id": "from-another-workspace", "table": "t"}},
        None, WITH_DATABASES,
    )
    assert choices.source is None


def test_the_type_comes_from_the_workspace_not_the_card():
    choices = choices_from_answers(
        {"source": {"kind": "connection", "connection_id": "c_pg", "connection_type": "mssql", "name": "Forged"}},
        None, WITH_DATABASES,
    )
    assert choices.source.connection_type == "postgresql"
    assert choices.source.name == "Sales Postgres"


def test_a_chosen_file_and_new_table_reach_the_draft():
    choices = choices_from_answers(
        {
            "source": {"kind": "file", "file_id": "obj_tickets", "file_name": "tickets.csv"},
            "destination": {"kind": "new_table", "table_name": "Open Tickets By Team"},
        },
        detect_draft_slots("Build a pipeline that shows open tickets by team", DEMO), DEMO,
    )
    workflow = _rule_draft("Build a pipeline that shows open tickets by team", choices)
    assert workflow.steps[0].params == {"connector_type": "csv", "file_path": "uploads/default/tickets-1.csv"}
    sink = _step(workflow, StepType.LOCAL_TABLE_SINK)
    assert sink.params == {"schema_name": "default", "table_name": "open_tickets_by_team", "mode": "replace"}
    assert validate_workflow(workflow) == []


def test_write_modes_map_to_what_the_nodes_read():
    def mode_for(write_mode, merge_on=()):
        choices = DraftChoices(
            source=DraftEndpoint(kind="file", name="a.csv", file_path="a.csv", file_format="csv"),
            destination=DraftEndpoint(kind="connection", name="Sales Postgres", connection_id="c_pg",
                                      connection_type="postgresql", table="t"),
            write_mode=write_mode, merge_on=merge_on,
        )
        return _step(_rule_draft("load a.csv into Sales Postgres", choices), StepType.DESTINATION).params

    assert mode_for("append")["mode"] == "append"
    assert mode_for("replace")["mode"] == "create"
    merged = mode_for("merge", ("id",))
    assert merged["mode"] == "merge" and merged["merge_on"] == ["id"]


def test_a_model_query_that_does_the_work_survives():
    workflow = Workflow(name="x", steps=[
        Step(id="a", type=StepType.DB_SOURCE, params={"query": "SELECT reason, COUNT(*) AS n FROM returns GROUP BY reason"}),
        Step(id="b", type=StepType.OUTPUT, params={"format": "parquet"}),
    ], connections=[StepConnection(from_step="a", to_step="b")])
    choices = DraftChoices(
        source=DraftEndpoint(kind="connection", name="Returns database", connection_id="c1", connection_type="sqlite", table="returns"),
        destination=DraftEndpoint(kind="new_table", name="default.x", table="x", schema="default"),
    )
    apply_choices_to_workflow(workflow, choices)
    assert workflow.steps[0].params["source_mode"] == "query"
    assert "GROUP BY" in workflow.steps[0].params["query"]
    assert workflow.steps[0].params["connection_id"] == "c1"


def test_a_draft_missing_either_end_gets_it():
    workflow = Workflow(name="x", steps=[Step(id="f", type=StepType.FILTER, params={"condition": "a > 1"})])
    choices = DraftChoices(
        source=DraftEndpoint(kind="table", name="default.a", table="a", schema="default"),
        destination=DraftEndpoint(kind="new_table", name="default.b", table="b", schema="default"),
    )
    apply_choices_to_workflow(workflow, choices)
    assert [s.type for s in workflow.steps] == [StepType.LOCAL_TABLE_SOURCE, StepType.FILTER, StepType.LOCAL_TABLE_SINK]
    assert [(c.from_step, c.to_step) for c in workflow.connections][0][1] == "f"
    assert [(c.from_step, c.to_step) for c in workflow.connections][1][0] == "f"


def test_a_model_table_name_stands_when_ours_was_only_implied():
    workflow = Workflow(name="x", steps=[
        Step(id="a", type=StepType.CSV_SOURCE, params={"file_path": "a.csv"}),
        Step(id="b", type=StepType.LOCAL_TABLE_SINK, params={"table_name": "model_named_it"}),
    ], connections=[StepConnection(from_step="a", to_step="b")])
    slots = detect_draft_slots(
        "Join orders.csv with customers.csv and save to a managed table", WITH_DATABASES,
    )
    apply_choices_to_workflow(workflow, choices_from_slots(slots))
    assert workflow.steps[1].params["table_name"] == "model_named_it"


def test_each_named_file_is_pointed_at_its_upload():
    workflow = Workflow(name="x", steps=[
        Step(id="a", type=StepType.CSV_SOURCE, params={"file_path": "orders.csv"}),
        Step(id="b", type=StepType.CSV_SOURCE, params={"file_path": "customers.csv"}),
        Step(id="j", type=StepType.JOIN, params={"join_key": "id"}),
        Step(id="o", type=StepType.LOCAL_TABLE_SINK, params={"table_name": "joined"}),
    ], connections=[
        StepConnection(from_step="a", to_step="j"),
        StepConnection(from_step="b", to_step="j"),
        StepConnection(from_step="j", to_step="o"),
    ])
    slots = detect_draft_slots("Join orders.csv with customers.csv and save to a managed table", WITH_DATABASES)
    apply_choices_to_workflow(workflow, choices_from_slots(slots))
    assert workflow.steps[0].params["file_path"] == "uploads/default/orders-1.csv"
    assert workflow.steps[1].params["file_path"] == "uploads/default/customers-1.csv"


def test_template_path_keeps_the_choices():
    choices = DraftChoices(
        source=DraftEndpoint(kind="connection", name="HR BIP", connection_id="c_bip", connection_type="oracle_bip"),
        destination=DraftEndpoint(kind="connection", name="Prod SQL", connection_id="c_sql",
                                  connection_type="mssql", table="employees"),
    )
    wf = populate_template(
        "oracle_bip_to_sql_server",
        {"sql_server_connection": "Prod SQL", "sql_server_table": "employees"},
        choices=choices,
    )
    source = wf.steps[0]
    assert source.params["connection_id"] == "c_bip"
    assert "url" not in source.params
    sink = next(s for s in wf.steps if s.type == StepType.DB_SINK)
    assert sink.params["connection_id"] == "c_sql"
    assert sink.params["table"] == "employees"
    merge = next(s for s in wf.steps if s.type == StepType.EXECUTE_SQL_TASK)
    assert merge.params["connection_id"] == "c_sql"
    assert "dbo.employees" in merge.params["sql"]


def test_a_typed_answer_is_read_for_names():
    slots = detect_draft_slots(FILM_PROMPT, DEMO)
    choices = choices_from_reply("Use Stockroom database and write to a managed table", slots, DEMO)
    assert choices.source.connection_id == "c_stock"
    assert choices.destination.kind == "new_table"


def test_a_typed_answer_to_the_one_open_end():
    slots = detect_draft_slots("Build a pipeline that reads tickets.csv", DEMO)
    assert [q.field for q in slots.questions] == ["destination"]
    choices = choices_from_reply("Stockroom database", slots, DEMO)
    assert choices.source.name == "tickets.csv"
    assert choices.destination.connection_id == "c_stock"


# ── No AI provider: the flow still works, through the route ───────────────


@pytest.fixture
def workspace(monkeypatch, connection_store, datastore, tmp_path):
    """A real workspace behind the planner route, with no AI provider."""
    from revoluzen_flow.main import app_state

    state = {
        "connection_store": connection_store,
        "datastore": datastore,
        "data_dir": str(tmp_path),
    }
    for key, value in state.items():
        monkeypatch.setitem(app_state, key, value)
    monkeypatch.setitem(app_state, "credential_store", None)
    monkeypatch.setattr(planner, "is_ai_available", lambda user_id=None, workspace_id=None: False)
    monkeypatch.setattr(planner, "current_user_optional", lambda request: None)

    connection_store.create(Connection(
        id="c_stock", name="Stockroom database", type="sqlite", workspace_id="default",
        config={"file_path": "stock.db", "password": SYNTHETIC_SECRET},
    ))
    connection_store.create(Connection(
        id="c_price", name="Partner price API", type="rest_api", workspace_id="default",
        config={"base_url": "https://example.invalid", "api_key": SYNTHETIC_SECRET},
    ))
    connection_store.create(Connection(
        id="c_other", name="Another tenant db", type="postgresql", workspace_id="other",
        config={"password": SYNTHETIC_SECRET},
    ))
    datastore.save_object(StorageObject(
        id="obj_tickets", workspace_id="default", name="tickets.csv",
        path="uploads/default/tickets-1.csv", format="csv",
    ))
    datastore.save_table(StorageTable(
        workspace_id="other", schema_name="default", name="their_table", path="tables/other/default/their_table",
    ))
    return SimpleNamespace(connections=connection_store, datastore=datastore, data_dir=tmp_path, state=state)


def _chat(messages, *, preview=True):
    return asyncio.run(planner.chat(messages, request=SimpleNamespace(), preview=preview, workspace_id="default"))


def _user(content, choices=None):
    return planner.ChatMessage(role="user", content=content, choices=choices)


def _assistant(content):
    return planner.ChatMessage(role="assistant", content=content)


def test_the_inventory_is_scoped_and_holds_names_only(workspace):
    inventory = read_workspace_inventory("default")
    assert {c.name for c in inventory.connections} == {"Stockroom database", "Partner price API"}
    assert {f.name for f in inventory.files} == {"tickets.csv"}
    assert inventory.tables == ()
    assert SYNTHETIC_SECRET not in repr(inventory.connections)


def test_route_asks_first_and_never_shows_another_workspace(workspace):
    response = _chat([_user(FILM_PROMPT)])
    assert response["intent"] == "clarify_first"
    assert response["workflow"] is None
    assert response["clarification"]["detected_intent"] == "Build a new pipeline"
    blob = json.dumps(response)
    assert "Stockroom database" in blob
    assert "Another tenant db" not in blob
    assert "their_table" not in blob
    assert SYNTHETIC_SECRET not in blob


def test_film_flow_end_to_end_with_a_connection_made_in_the_chat(workspace):
    """The request, the new SQLite connection, the card's answers, the run."""
    database = workspace.data_dir / "warehouse.db"
    with sqlite3.connect(database) as conn:
        conn.execute("CREATE TABLE returns (id INTEGER, reason TEXT)")
        conn.executemany("INSERT INTO returns VALUES (?, ?)", [(1, "damaged"), (2, "damaged"), (3, "wrong size")])
    conn.close()

    first = _chat([_user(FILM_PROMPT)])
    assert first["intent"] == "clarify_first"

    # What the inline form's Save does: the connections API, not the chat.
    workspace.connections.create(Connection(
        id="c_returns", name="Returns database", type="sqlite", workspace_id="default",
        config={"file_path": "warehouse.db"},
    ))
    from revoluzen_flow.connections.catalog import get_catalog
    from revoluzen_flow.connections.tester import ConnectionTester
    assert ConnectionTester().test_connection("sqlite", {"file_path": "warehouse.db"})["success"] is True
    assert [i.name for i in get_catalog("sqlite", {"file_path": "warehouse.db"}).items] == ["returns"]

    answers = {
        "source": {"kind": "connection", "connection_id": "c_returns", "table": "returns", "schema": "main"},
        "destination": {"kind": "new_table", "table_name": "returns_by_reason"},
    }
    second = _chat([
        _user(FILM_PROMPT),
        _assistant(first["reply"]),
        _user("Read from Returns database, table returns. Write to a new managed table called returns_by_reason.", answers),
    ])
    assert "clarification" not in second
    draft = second["workflow"]
    assert draft is not None
    types = [s["type"] for s in draft["steps"]]
    assert types == ["source", "aggregate", "local_table_sink"]
    assert draft["steps"][0]["params"]["connection_id"] == "c_returns"
    assert draft["steps"][0]["params"]["table"] == "returns"
    assert draft["steps"][1]["params"]["group_by"] == ["reason"]
    assert draft["steps"][2]["params"]["table_name"] == "returns_by_reason"
    assert "Returns database" in second["reply"]
    assert draft["name"] == "Returns By Reason Pipeline"

    # It really runs: rows come out of the new connection into the managed table.
    workflow = Workflow(**draft)
    assert validate_workflow(workflow) == []
    result = WorkflowExecutor(data_dir=str(workspace.data_dir), app_state=workspace.state).execute_workflow(
        workflow, full_run=True,
    )
    assert result.status == "success", [
        (s.step_id, s.error) for s in result.step_results.values() if s.error
    ]
    table = workspace.datastore.find_table_by_name("default", "default", "returns_by_reason")
    assert table is not None and table.row_count == 2


def test_uploaded_file_answer_runs_too(workspace):
    upload = workspace.data_dir / "uploads" / "default"
    upload.mkdir(parents=True)
    (upload / "tickets-1.csv").write_text("id,team\n1,alpha\n2,alpha\n3,beta\n", encoding="utf-8")

    prompt = "Build a pipeline that shows open tickets by team"
    first = _chat([_user(prompt)])
    second = _chat([
        _user(prompt),
        _assistant(first["reply"]),
        _user("answers", {
            "source": {"kind": "file", "file_id": "obj_tickets", "file_name": "tickets.csv"},
            "destination": {"kind": "new_table", "table_name": "tickets_by_team"},
        }),
    ])
    workflow = Workflow(**second["workflow"])
    assert workflow.steps[0].params["file_path"] == "uploads/default/tickets-1.csv"
    result = WorkflowExecutor(data_dir=str(workspace.data_dir), app_state=workspace.state).execute_workflow(
        workflow, full_run=True,
    )
    assert result.status == "success", [
        (s.step_id, s.error) for s in result.step_results.values() if s.error
    ]
    assert workspace.datastore.find_table_by_name("default", "default", "tickets_by_team").row_count == 2


def test_card_answer_goes_to_the_planner_as_context_without_secrets(workspace, monkeypatch):
    """Ids and names reach the model; nothing from a connection's config does."""
    seen = []

    async def fake_generate(messages, user_id=None, workspace_id=None):
        seen.append(messages)
        return {
            "name": "Returns",
            "steps": [
                {"type": "db_source", "label": "Query", "params": {"query": "SELECT 1"}},
                {"type": "aggregate", "label": "By reason", "params": {
                    "group_by": ["reason"],
                    "functions": [{"column": "*", "function": "COUNT", "alias": "count"}],
                }},
                {"type": "db_sink", "label": "Out", "params": {"table_name": "x", "connection_string": ""}},
            ],
            "connections": [{"from_step": 0, "to_step": 1}, {"from_step": 1, "to_step": 2}],
        }

    monkeypatch.setattr(planner, "is_ai_available", lambda user_id=None, workspace_id=None: True)
    monkeypatch.setattr(planner, "ai_generate_pipeline", fake_generate)

    first = _chat([_user(FILM_PROMPT)])
    assert seen == [], "the card comes before any model call"

    second = _chat([
        _user(FILM_PROMPT),
        _assistant(first["reply"]),
        _user("Read from Stockroom database, table returns.", {
            "source": {"kind": "connection", "connection_id": "c_stock", "table": "returns"},
            "destination": {"kind": "new_table", "table_name": "returns_by_reason"},
        }),
    ])
    assert second["ai_powered"] is True

    sent = json.dumps(seen)
    assert "c_stock" in sent and "Stockroom database" in sent
    assert SYNTHETIC_SECRET not in sent
    # The connection's config stays home: its file path isn't in there either.
    assert "stock.db" not in sent
    # The card itself isn't part of what the model reads.
    assert "A few quick questions" not in sent
    assert len(seen[0]) == 1 and FILM_PROMPT in seen[0][0]["content"]

    # Whatever the model wrote, the draft uses the choices.
    draft = second["workflow"]
    assert draft["steps"][0]["params"]["connection_id"] == "c_stock"
    assert draft["steps"][0]["params"]["table"] == "returns"
    assert draft["steps"][-1]["type"] == "local_table_sink"
    assert draft["steps"][-1]["params"]["table_name"] == "returns_by_reason"
    assert SYNTHETIC_SECRET not in json.dumps(second)


def test_named_files_draft_with_no_card_through_the_route(workspace):
    workspace.datastore.save_object(StorageObject(
        id="obj_orders", workspace_id="default", name="orders.csv", path="uploads/default/orders-1.csv", format="csv",
    ))
    workspace.datastore.save_object(StorageObject(
        id="obj_customers", workspace_id="default", name="customers.csv", path="uploads/default/customers-1.csv", format="csv",
    ))
    response = _chat([_user("Build a pipeline: Join orders.csv with customers.csv on customer_id and save to a managed table")])
    assert "clarification" not in response
    assert response["workflow"] is not None
    assert response["workflow"]["steps"][-1]["type"] == "local_table_sink"


def test_no_provider_join_reads_both_files_and_really_runs(workspace):
    upload = workspace.data_dir / "uploads" / "default"
    upload.mkdir(parents=True)
    (upload / "orders-1.csv").write_text("order_id,customer_id\n1,10\n2,10\n3,20\n", encoding="utf-8")
    (upload / "customers-1.csv").write_text("customer_id,name\n10,Asha\n20,Ravi\n", encoding="utf-8")
    workspace.datastore.save_object(StorageObject(
        id="obj_orders", workspace_id="default", name="orders.csv", path="uploads/default/orders-1.csv", format="csv",
    ))
    workspace.datastore.save_object(StorageObject(
        id="obj_customers", workspace_id="default", name="customers.csv", path="uploads/default/customers-1.csv", format="csv",
    ))

    response = _chat([_user("Join orders.csv with customers.csv on customer_id and save to a managed table called order_customers")])
    assert "clarification" not in response
    assert "orders.csv and customers.csv" in response["reply"]
    workflow = Workflow(**response["workflow"])
    assert [s.type for s in workflow.steps] == [
        StepType.SOURCE, StepType.SOURCE, StepType.JOIN, StepType.LOCAL_TABLE_SINK,
    ]
    assert validate_workflow(workflow) == []
    result = WorkflowExecutor(data_dir=str(workspace.data_dir), app_state=workspace.state).execute_workflow(
        workflow, full_run=True,
    )
    assert result.status == "success", [
        (s.step_id, s.error) for s in result.step_results.values() if s.error
    ]
    assert workspace.datastore.find_table_by_name("default", "default", "order_customers").row_count == 3


def test_a_file_the_draft_does_not_read_is_not_claimed():
    workflow = Workflow(name="x", steps=[
        Step(id="a", type=StepType.CSV_SOURCE, params={"file_path": "orders.csv"}),
        Step(id="b", type=StepType.LOCAL_TABLE_SINK, params={"table_name": "t"}),
    ], connections=[StepConnection(from_step="a", to_step="b")])
    slots = detect_draft_slots("Join orders.csv with customers.csv and save to a managed table", WITH_DATABASES)
    choices = choices_from_slots(slots)
    apply_choices_to_workflow(workflow, choices)
    from revoluzen_flow.ai.draft_choices import describe_choices_for_person, draft_notes
    assert "customers.csv" not in describe_choices_for_person(choices, workflow)
    assert any("doesn't read customers.csv yet" in note for note in draft_notes(choices, workflow))


def test_template_answer_through_the_route_binds_connections_in_plain_words(workspace):
    workspace.connections.create(Connection(
        id="c_bip", name="HR BIP", type="oracle_bip", workspace_id="default",
        config={"password": SYNTHETIC_SECRET},
    ))
    workspace.connections.create(Connection(
        id="c_sql", name="Prod SQL", type="mssql", workspace_id="default",
        config={"password": SYNTHETIC_SECRET},
    ))
    prompt = "Create a pipeline to fetch Oracle BIP employee data every 6 hours and load into SQL Server."
    first = _chat([_user(prompt)])
    assert first["clarification"]["detected_intent"] == "Build a pipeline from Oracle BI Publisher to SQL Server"
    second = _chat([
        _user(prompt),
        _assistant(first["reply"]),
        _user("answers", {
            "source": {"kind": "connection", "connection_id": "c_bip"},
            "destination": {"kind": "connection", "connection_id": "c_sql", "table": "employees"},
            "write_mode": "replace",
        }),
    ])
    assert second["intent"] == "template_populated"
    steps = second["workflow"]["steps"]
    assert steps[0]["params"]["connection_id"] == "c_bip"
    assert next(s for s in steps if s["type"] == "db_sink")["params"]["connection_id"] == "c_sql"
    assert "sql_server" not in second["reply"]
    assert "_" not in second["reply"].split("Steps:")[0]
    # The template's request body is still the person's to write, and it says so.
    assert "placeholder in angle brackets" in second["reply"]
    assert SYNTHETIC_SECRET not in json.dumps(second)


def test_an_upsert_with_no_key_asks_which_column(workspace):
    workspace.connections.create(Connection(
        id="c_pg", name="Sales Postgres", type="postgresql", workspace_id="default", config={},
    ))
    response = _chat([_user("Build a pipeline that reads tickets.csv and upserts into Sales Postgres table tickets")])
    assert [q["field"] for q in response["clarification"]["questions"]] == ["merge_key"]
    assert response["clarification"]["write_mode"] == "merge"


def test_a_question_or_greeting_gets_no_card(workspace):
    assert "clarification" not in _chat([_user("what is a pipeline?")])
    assert "clarification" not in _chat([_user("hello")])


def test_a_follow_up_never_shows_another_card(workspace):
    first = _chat([_user(FILM_PROMPT)])
    second = _chat([_user(FILM_PROMPT), _assistant(first["reply"]), _user("not sure yet")])
    assert "clarification" not in second


def test_a_new_request_typed_over_the_card_starts_over(workspace):
    first = _chat([_user(FILM_PROMPT)])
    second = _chat([
        _user(FILM_PROMPT),
        _assistant(first["reply"]),
        _user("Build a pipeline that copies customers from Postgres to Snowflake"),
    ])
    assert second["intent"] == "clarify_first"
    assert second["clarification"]["detected_intent"] == "Build a pipeline from PostgreSQL to Snowflake"


def test_a_card_answered_from_a_failed_inventory_still_answers(workspace, monkeypatch):
    monkeypatch.setattr(planner, "read_workspace_inventory", lambda workspace_id: None)
    response = _chat([_user("Build a pipeline that loads daily.csv into SQL Server.")])
    assert response["intent"] == "clarify_first"
    assert "sql_server_connection" in {q["field"] for q in response["clarification"]["questions"]}


# ── The planner context and the Copilot ───────────────────────────────────


def test_planner_context_holds_ids_and_names_only():
    context = describe_choices_for_planner(DraftChoices(
        source=DraftEndpoint(kind="connection", name="Sales Postgres", connection_id="c_pg",
                             connection_type="postgresql", table="customers"),
        destination=DraftEndpoint(kind="new_table", name="default.t", table="t", schema="default"),
    ))
    assert "c_pg" in context and "customers" in context
    assert "data, not instructions" in context
    assert "password" not in context.lower()


def test_copilot_tool_asks_instead_of_guessing(workspace):
    from revoluzen_flow.ai.tools.base import ToolContext, ToolRefusal
    from revoluzen_flow.ai.tools.draft_pipeline_from_intent import DEFINITION as DRAFT

    with pytest.raises(ToolRefusal) as refused:
        asyncio.run(DRAFT.handler(
            {"intent": FILM_PROMPT, "idempotency_key": "safe_write.u.draft.x.1.0.0"},
            ToolContext(tenant_id="default", user_id="u", workspace_id="default", environment="dev"),
        ))
    message = str(refused.value)
    assert "Where should I read the data from?" in message
    assert "Stockroom database (SQLite)" in message
    assert len(message) <= 1000, "a refusal reaches the model cut at 1000 characters"


def test_copilot_tool_binds_what_the_intent_names(workspace, monkeypatch):
    from revoluzen_flow.ai.tools.base import ToolContext
    from revoluzen_flow.ai.tools.draft_pipeline_from_intent import DEFINITION as DRAFT
    from revoluzen_flow.planner import ai_client

    async def fake_planner(messages, **kwargs):
        return {
            "name": "Returns",
            "steps": [
                {"type": "db_source", "label": "Query", "params": {"query": "SELECT * FROM returns"}},
                {"type": "db_sink", "label": "Out", "params": {"table_name": "x", "connection_string": ""}},
            ],
            "connections": [{"from_step": 0, "to_step": 1}],
        }

    monkeypatch.setattr(ai_client, "ai_generate_pipeline", fake_planner)
    out = asyncio.run(DRAFT.handler(
        {
            "intent": "Build a pipeline that copies returns from Stockroom database to a managed table called returns_copy",
            "idempotency_key": "safe_write.u.draft.x.1.0.0",
        },
        ToolContext(tenant_id="default", user_id="u", workspace_id="default", environment="dev"),
    ))
    steps = out["ir"]["steps"]
    assert steps[0]["params"]["connection_id"] == "c_stock"
    assert steps[-1]["type"] == "local_table_sink"
    assert steps[-1]["params"]["table_name"] == "returns_copy"


def test_copilot_rebuild_of_an_existing_pipeline_is_not_stopped(workspace, monkeypatch):
    from revoluzen_flow.ai.tools.base import ToolContext
    from revoluzen_flow.ai.tools.draft_pipeline_from_intent import DEFINITION as DRAFT
    from revoluzen_flow.planner import ai_client

    async def fake_planner(messages, **kwargs):
        return None

    monkeypatch.setattr(ai_client, "ai_generate_pipeline", fake_planner)
    out = asyncio.run(DRAFT.handler(
        {"intent": "Rebuild this pipeline.", "idempotency_key": "safe_write.u.draft.x.1.0.0"},
        ToolContext(tenant_id="default", user_id="u", workspace_id="default", environment="dev"),
    ))
    assert out["step_count"] == 0


def test_questions_for_the_model_keep_managed_tables_on_offer():
    text = render_questions_for_model(clarification_set_from_slots(detect_draft_slots(FILM_PROMPT, DEMO)))
    assert "New managed table in this workspace" in text
    assert "New connection" not in text


# ── Warehouse connections run through the dialects ────────────────────────


@pytest.fixture
def duck():
    conn = duckdb.connect()
    yield conn
    conn.close()


def test_a_snowflake_destination_writes_through_the_dialect(duck, monkeypatch):
    from revoluzen_flow.connectors import jdbc

    calls = {}

    def fake_insert(dialect, cfg, table, cols, rows):
        calls.update(dialect=dialect, cfg=cfg, table=table, cols=list(cols), rows=rows)
        return len(rows)

    monkeypatch.setattr(jdbc, "_get_connection_config", lambda cid: ({"user": "u", "password": "p", "account": "a"}, "snowflake"))
    monkeypatch.setattr(jdbc, "_basic_insert", fake_insert)

    ctx = ExecutionContext(conn=duck, full_run=True)
    upstream = duck.sql("SELECT 1 AS id, 'x' AS name")
    ctx.set_result("up", upstream)
    GenericDestinationNode({
        "connector_type": "database", "connection_id": "c_sf", "table": "customers",
        "_input_step_ids": ["up"],
    }).execute(ctx)
    assert calls["dialect"] == "snowflake"
    assert calls["table"] == "customers"
    assert calls["rows"] == [(1, "x")]


def test_a_snowflake_source_reads_through_the_dialect(duck, monkeypatch):
    from revoluzen_flow.connectors import jdbc, jdbc_dialects

    seen = {}

    def fake_reader(cfg, query, table, limit):
        seen.update(query=query, table=table, limit=limit)
        return ["id"], [(1,)]

    monkeypatch.setattr(jdbc, "_get_connection_config", lambda cid: ({"user": "u"}, "snowflake"))
    monkeypatch.setattr(jdbc_dialects.get_dialect("snowflake"), "reader", fake_reader)

    relation = GenericSourceNode({
        "connector_type": "database", "connection_id": "c_sf",
        "source_mode": "table", "schema": "PUBLIC", "table": "CUSTOMERS",
    }).execute(ExecutionContext(conn=duck, full_run=False))
    assert seen["table"] == "PUBLIC.CUSTOMERS"
    assert seen["limit"] == 1000
    assert relation.fetchall() == [(1,)]


def test_a_source_table_name_cannot_carry_sql(duck, monkeypatch):
    from revoluzen_flow.connectors import jdbc

    monkeypatch.setattr(jdbc, "_get_connection_config", lambda cid: ({}, "snowflake"))
    with pytest.raises(ValueError):
        GenericSourceNode({
            "connector_type": "database", "connection_id": "c_sf",
            "source_mode": "table", "table": "x; DROP TABLE y",
        }).execute(ExecutionContext(conn=duck, full_run=True))


def test_postgres_still_goes_to_the_database_node(duck, monkeypatch):
    from revoluzen_flow.connectors import jdbc
    from revoluzen_flow.nodes import generic

    monkeypatch.setattr(jdbc, "_get_connection_config", lambda cid: ({}, "postgresql"))
    assert generic._connection_dialect({"connection_id": "c_pg"}) is None


def test_saved_connection_field_names_reach_the_dialects(monkeypatch):
    from revoluzen_flow.connectors import jdbc
    from revoluzen_flow.main import app_state

    store = SimpleNamespace(get=lambda cid: SimpleNamespace(
        config={"username": "u", "token": "t"}, credential_id=None, type="snowflake",
    ))
    monkeypatch.setitem(app_state, "connection_store", store)
    monkeypatch.setitem(app_state, "credential_store", None)
    config, conn_type = jdbc._get_connection_config("c")
    assert config["user"] == "u"
    assert config["access_token"] == "t"
    assert conn_type == "snowflake"


# ── The rule planner ──────────────────────────────────────────────────────


def test_rule_planner_groups_by_what_was_asked():
    workflow = RulePlanner().plan(FILM_PROMPT, assume_source=True).workflow
    aggregate = _step(workflow, StepType.AGGREGATE)
    assert aggregate.params["group_by"] == ["reason"]
    assert aggregate.params["functions"][0]["function"] == "COUNT"


def test_rule_planner_adds_nothing_to_a_plain_copy():
    workflow = RulePlanner().plan("Copy accounts from Postgres to Snowflake daily", assume_source=True).workflow
    assert [s.type for s in workflow.steps] == [StepType.CSV_SOURCE, StepType.FILE_SINK]


def test_rule_planner_still_refuses_with_no_source_and_no_promise():
    result = RulePlanner().plan("shows open tickets by team")
    assert result.workflow is None and result.needs_ai is True
