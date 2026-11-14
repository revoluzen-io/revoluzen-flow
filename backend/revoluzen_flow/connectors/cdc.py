"""Change Data Capture: read inserts, updates and deletes as rows (rewritten 2026-11-13).

The step reads the changes waiting since its last successful run, from the
saved connection it is given:

* a PostgreSQL connection: straight from the database's change log, with no
  Debezium and no Kafka (``streams/pg_cdc.py``);
* a MySQL or MariaDB connection: from the server's binary log
  (``streams/mysql_cdc.py``);
* a Kafka connection: Debezium change events on a topic, for SQL Server,
  Oracle, MongoDB, Db2 and the other databases Debezium reads
  (``streams/debezium.py``).

Every change becomes a row with the table's columns and ``_op`` (insert,
update, delete, truncate, snapshot), ``_table`` and ``_committed_at``. Where
the step carries on reading moves only after the whole run has succeeded, so
after a failure a change can arrive twice but none is lost.

Before 2026-11-13 this step could not work: the PostgreSQL mode decoded the
binary ``pgoutput`` plugin as text, confirmed each change the moment it was
read, waited forever when fewer changes than Max Events were waiting, and took
host and password typed into the step; the Debezium mode joined a shared
consumer group that saved its place while reading. Steps saved in that shape
still load: a typed host is used as the connection, and Kafka steps are asked
to pick a saved Kafka connection.

Change Data Capture is a Revoluzen Flow Enterprise feature (edition-matrix.md);
the open-source build shows the step and refuses to run it.
"""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    import duckdb

from revoluzen_flow.ir.schema import StepType
from revoluzen_flow.licensing import require_plus
from revoluzen_flow.nodes.base import BaseNode, ExecutionContext
from revoluzen_flow.nodes.registry import register

WHO = "Change Data Capture"

POSTGRES_TYPES = {"postgresql", "postgres", "cockroachdb"}
MYSQL_TYPES = {"mysql", "mariadb"}
KAFKA_TYPES = {"kafka"}


def _legacy_postgres_config(params: dict) -> dict | None:
    """A PostgreSQL connection typed into a step saved before 2026-11-13."""
    if not params.get("host") or not params.get("database"):
        return None
    return {
        "host": params.get("host"), "port": params.get("port") or 5432,
        "database": params.get("database"), "username": params.get("user") or params.get("username"),
        "password": params.get("password"),
    }


def source_of(ctx: Any, params: dict) -> tuple[str, dict]:
    """``(kind, connection settings)`` where kind is postgres, mysql or debezium."""
    from revoluzen_flow.streams.connection import saved_connection

    connection_id = str(params.get("connection_id") or "").strip()
    if not connection_id:
        legacy = _legacy_postgres_config(params)
        if legacy is not None and params.get("mode") == "direct_logical":
            return "postgres", legacy
        if params.get("mode") == "kafka_topic" or params.get("kafka_topic"):
            raise ValueError(
                f"{WHO}: pick a saved Kafka connection for the Debezium topic. Steps saved before "
                f"November 2026 named the brokers in the step; those settings are no longer read."
            )
        raise ValueError(
            f"{WHO}: pick a saved connection: PostgreSQL, MySQL, MariaDB, or Kafka for Debezium events."
        )
    config, conn_type = saved_connection(ctx, connection_id, WHO)
    if conn_type in POSTGRES_TYPES:
        if conn_type == "cockroachdb":
            raise ValueError(
                f"{WHO}: CockroachDB keeps its changes differently (changefeeds). Send them to "
                f"Kafka with a CockroachDB changefeed and read that topic with a Kafka Source."
            )
        return "postgres", config
    if conn_type in MYSQL_TYPES:
        return "mysql", config
    if conn_type in KAFKA_TYPES:
        return "debezium", config
    raise ValueError(
        f"{WHO}: a {conn_type} connection has no change log Flow can read. Use PostgreSQL, "
        f"MySQL, MariaDB, or a Kafka topic that Debezium writes to."
    )


@register(StepType.CDC_SOURCE)
class CdcSourceNode(BaseNode):
    """Change Data Capture: inserts, updates and deletes since the last successful run."""

    display_name = "Change Data Capture"
    category = "source"
    description = "Read inserts, updates and deletes from PostgreSQL, MySQL / MariaDB, or Debezium events in Kafka"

    def execute(self, ctx: ExecutionContext) -> duckdb.DuckDBPyRelation:
        # Declared Enterprise-only in edition-matrix.md; gated at execution so
        # the step stays visible and configurable in the open-source build.
        require_plus("CDC Source (change data capture)")
        kind, config = source_of(ctx, self.params)
        step_id = str(self.params.get("_step_id") or "")
        if kind == "postgres":
            from revoluzen_flow.streams.pg_cdc import read_postgres

            return read_postgres(ctx, self.params, config, step_id)
        if kind == "mysql":
            from revoluzen_flow.streams.mysql_cdc import read_mysql

            return read_mysql(ctx, self.params, config, step_id)
        from revoluzen_flow.streams.debezium import read_debezium

        return read_debezium(ctx, self.params, config, step_id)

    @staticmethod
    def default_params() -> dict[str, Any]:
        return {
            "connection_id": "", "tables": "", "start_from": "new",
            "max_changes": 10000, "timeout_seconds": 120,
        }

    @staticmethod
    def param_schema() -> list[dict]:
        # Only settings a run reads. Which ones apply depends on the
        # connection's type: tables, slot and publication for PostgreSQL;
        # tables for MySQL / MariaDB; topic and format for Debezium on Kafka.
        return [
            {"name": "connection_id", "type": "connection_picker", "label": "Connection",
             "required": True,
             "description": "A saved PostgreSQL, MySQL, MariaDB or Kafka connection."},
            {"name": "tables", "type": "text", "label": "Tables",
             "placeholder": "public.orders, public.customers",
             "description": "The tables whose changes the step reads (PostgreSQL, MySQL, MariaDB). "
                            "Write schema.table (database.table on MySQL), separated by commas."},
            {"name": "start_from", "type": "select", "label": "First run starts at",
             "options": ["new", "copy"], "default": "new",
             "description": "new: only changes made from the first run on. copy: every row the "
                            "tables hold now, then the changes after it."},
            {"name": "max_changes", "type": "number", "label": "Max Changes", "default": 10000,
             "description": "About how many changes one run reads. A database transaction is "
                            "never split, so a run can read a few more."},
            {"name": "timeout_seconds", "type": "number", "label": "Timeout (seconds)", "default": 120,
             "description": "The longest a run spends reading changes."},
            {"name": "slot_name", "type": "text", "label": "Replication Slot",
             "description": "PostgreSQL only. Empty: Flow names one for this step."},
            {"name": "publication", "type": "text", "label": "Publication",
             "description": "PostgreSQL only. Empty: Flow creates one for the tables, if the "
                            "database user may."},
            {"name": "topic", "type": "text", "label": "Topic",
             "description": "Kafka (Debezium) only: the topic Debezium writes one table's changes to."},
            {"name": "message_format", "type": "select", "label": "Message Format",
             "options": ["json", "avro"], "default": "json",
             "description": "Kafka (Debezium) only: JSON, or Avro with a Schema Registry."},
        ]


def cleanup_workflow_cdc_slots(workflow: Any) -> int:
    """Remove the PostgreSQL replication slots of a deleted pipeline's CDC steps.

    A slot nobody reads keeps the database's change log from being cleaned up
    until its disk fills. Best effort; returns how many slots were removed.
    """
    from revoluzen_flow.streams.connection import connection_of_workflow
    from revoluzen_flow.streams.pg_cdc import drop_for_step

    dropped = 0
    workflow_id = str(getattr(workflow, "id", "") or "")
    for step in getattr(workflow, "steps", None) or []:
        try:
            if getattr(step, "type", None) != StepType.CDC_SOURCE:
                continue
            params = dict(getattr(step, "params", {}) or {})
            config = None
            if params.get("connection_id"):
                found = connection_of_workflow(workflow, params["connection_id"])
                if found and found[1] in ("postgresql", "postgres"):
                    config = found[0]
            elif params.get("mode") == "direct_logical":
                config = _legacy_postgres_config(params)
                if config is not None and not params.get("slot_name"):
                    params["slot_name"] = params.get("slot") or "revoluzen_flow_slot"
            if config is None or not workflow_id:
                continue
            if drop_for_step(params, config, workflow_id, str(step.id)):
                dropped += 1
        except Exception:  # noqa: BLE001 — clean-up must never break deleting a pipeline
            continue
    return dropped
