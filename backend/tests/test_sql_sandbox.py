"""DuckDB file-system sandbox for pipeline SQL (security/sql_sandbox.py).

Pins the 2026-11-13 pen-test chain: pipeline SQL read the master key and the
SQLite database with read_text()/read_blob(), and a stacked COPY wrote a file
outside the data dir. With the sandbox on (server mode), a pipeline keeps the
files it names and loses the instance's secrets.
"""

from __future__ import annotations

import duckdb
import pytest

from revoluzen_flow import runtime_config
from revoluzen_flow.engine.executor import WorkflowExecutor
from revoluzen_flow.ir.schema import Step, StepConnection, Workflow
from revoluzen_flow.security import sql_sandbox


def _p(path) -> str:
    return str(path).replace("\\", "/")


@pytest.fixture
def instance(tmp_path, monkeypatch):
    """A data dir shaped like a real install: key, app DB + WAL, user data."""
    data = tmp_path / "data"
    (data / "uploads" / "ws1").mkdir(parents=True)
    (data / "backups").mkdir()
    (data / "secret.key").write_text("FAKE-MASTER-KEY\n")
    (data / "revoluzen_flow.db").write_bytes(b"SQLite format 3\x00 app db")
    (data / "revoluzen_flow.db-wal").write_bytes(b"wal")
    (data / "backups" / "revoluzen_flow_1.db").write_bytes(b"backup")
    (data / "uploads" / "ws1" / "orders.csv").write_text("id,amount\n1,10\n2,20\n")
    (data / "rootdata.csv").write_text("id,amount\n3,30\n4,40\n")
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "declared.csv").write_text("id,amount\n5,50\n6,60\n")
    (outside / "undeclared.csv").write_text("id,amount\n7,70\n8,80\n")
    monkeypatch.setenv("REVOLUZEN_FLOW_MASTER_KEY_FILE", str(data / "secret.key"))
    monkeypatch.setenv("REVOLUZEN_FLOW_SQL_SANDBOX", "1")
    monkeypatch.setattr(runtime_config, "DUCKDB_TEMP_DIRECTORY", str(data / "duckdb_spill"))
    return data, outside


def _wf(*steps, edges=()):
    return Workflow(id="wf_sandbox", name="sandbox", steps=list(steps),
                    connections=[StepConnection(from_step=a, to_step=b) for a, b in edges])


def _csv(step_id, path):
    return Step(id=step_id, type="csv_source", label=step_id, params={"file_path": str(path)})


def _transform(step_id, expression):
    return Step(id=step_id, type="transform", label=step_id, params={"expression": expression})


def _sandboxed(workflow, data):
    conn = duckdb.connect(":memory:")
    assert sql_sandbox.apply(conn, workflow, str(data)) is True
    return conn


def _blocked(conn, sql):
    with pytest.raises(duckdb.Error) as exc:
        conn.execute(sql).fetchall()
    return str(exc.value)


# ── what the sandbox allows ───────────────────────────────────────────────

def test_pipeline_files_stay_readable(instance):
    data, outside = instance
    conn = _sandboxed(_wf(_csv("s", outside / "declared.csv")), data)
    assert conn.execute(f"SELECT count(*) FROM read_csv('{_p(outside / 'declared.csv')}')").fetchone()[0] == 2
    assert conn.execute(f"SELECT count(*) FROM read_csv('{_p(data / 'uploads/ws1/orders.csv')}')").fetchone()[0] == 2
    assert conn.execute(f"SELECT count(*) FROM read_csv('{_p(data / 'rootdata.csv')}')").fetchone()[0] == 2
    conn.execute(f"COPY (SELECT 1 AS x) TO '{_p(data / 'uploads/ws1/out.csv')}'")


# ── what it refuses ───────────────────────────────────────────────────────

@pytest.mark.parametrize("sql", [
    "SELECT content FROM read_text('{data}/secret.key')",
    "SELECT content FROM read_blob('{data}/revoluzen_flow.db')",
    "SELECT content FROM read_blob('{data}/revoluzen_flow.db-wal')",
    "SELECT content FROM read_blob('{data}/backups/revoluzen_flow_1.db')",
    "SELECT content FROM read_text('{data}/uploads/ws1/../../secret.key')",
    "SELECT * FROM glob('{data}/*')",
    "SELECT count(*) FROM read_csv('{outside}/undeclared.csv')",
    "COPY (SELECT 1 AS x) TO '{outside}/escape.csv'",
    "ATTACH '{data}/revoluzen_flow.db' AS app",
    "SET enable_external_access=true",
    "SET allowed_directories=['/']",
])
def test_secrets_and_undeclared_paths_are_refused(instance, sql):
    data, outside = instance
    conn = _sandboxed(_wf(_csv("s", outside / "declared.csv")), data)
    _blocked(conn, sql.format(data=_p(data), outside=_p(outside)))
    assert not (outside / "escape.csv").exists()


@pytest.mark.parametrize("declared", ["secret.key", "revoluzen_flow.db", "*.key", ".", "backups/*.db"])
def test_a_pipeline_cannot_declare_its_way_to_a_secret(instance, declared):
    """Naming the key (or a folder or glob around it) in a step's params does
    not put it on the allowlist."""
    data, _ = instance
    conn = _sandboxed(_wf(_csv("s", declared)), data)
    _blocked(conn, f"SELECT content FROM read_text('{_p(data)}/secret.key')")
    _blocked(conn, f"SELECT content FROM read_blob('{_p(data)}/backups/revoluzen_flow_1.db')")


def test_user_files_with_secret_in_their_name_are_still_readable(instance):
    """Name heuristics belong to the data dir root only."""
    data, outside = instance
    named = outside / "secret_santa.csv"
    named.write_text("id,amount\n1,1\n2,2\n")
    conn = _sandboxed(_wf(_csv("s", named)), data)
    assert conn.execute(f"SELECT count(*) FROM read_csv('{_p(named)}')").fetchone()[0] == 2


def test_the_config_home_is_never_allowed(instance, tmp_path, monkeypatch):
    data, _ = instance
    home_cfg = tmp_path / "cfg"
    home_cfg.mkdir()
    (home_cfg / "storage_settings.json").write_text("{}")
    monkeypatch.setenv("REVOLUZEN_FLOW_CONFIG_HOME", str(home_cfg))
    dirs, files, _ = sql_sandbox.allowlist(_wf(_csv("s", home_cfg / "storage_settings.json")), str(data))
    assert not any(_p(home_cfg).lower() in d.lower() for d in dirs + files)


# ── end to end through the executor ───────────────────────────────────────

def test_executor_runs_are_sandboxed(instance):
    data, outside = instance
    wf = _wf(
        _csv("src", outside / "declared.csv"),
        _transform("t", f"SELECT content FROM read_text('{_p(data)}/secret.key')"),
        edges=[("src", "t")],
    )
    result = WorkflowExecutor(data_dir=str(data)).execute_workflow(wf)
    assert result.step_results["src"].status == "success"
    t = result.step_results["t"]
    assert t.status == "error"
    assert "FAKE-MASTER-KEY" not in str(t.sample_data)
    assert "Permission" in (t.error or "")


def test_a_transform_cannot_stack_statements(instance, monkeypatch):
    """Always on, sandbox or not: the stacked COPY from the pen test."""
    monkeypatch.setenv("REVOLUZEN_FLOW_SQL_SANDBOX", "0")
    data, outside = instance
    target = outside / "stacked.csv"
    wf = _wf(
        _csv("src", outside / "declared.csv"),
        _transform("t", f"SELECT 1 AS a; COPY (SELECT 'x' AS v) TO '{_p(target)}'"),
        edges=[("src", "t")],
    )
    result = WorkflowExecutor(data_dir=str(data)).execute_workflow(wf)
    assert result.step_results["t"].status == "error"
    assert "single SQL statement" in (result.step_results["t"].error or "")
    assert not target.exists()


# ── when it applies ───────────────────────────────────────────────────────

def test_defaults_follow_the_security_mode(monkeypatch):
    monkeypatch.delenv("REVOLUZEN_FLOW_SQL_SANDBOX", raising=False)
    monkeypatch.setattr(runtime_config, "IS_SERVER_MODE", False)
    assert sql_sandbox.sandbox_enabled() is False
    monkeypatch.setattr(runtime_config, "IS_SERVER_MODE", True)
    assert sql_sandbox.sandbox_enabled() is True
    monkeypatch.setenv("REVOLUZEN_FLOW_SQL_SANDBOX", "0")
    assert sql_sandbox.sandbox_enabled() is False


def test_semicolons_inside_strings_are_not_stacked_statements():
    conn = duckdb.connect()
    sql_sandbox.require_single_statement(conn, "SELECT ';' AS s -- ; comment")
    with pytest.raises(ValueError):
        sql_sandbox.require_single_statement(conn, "SELECT 1; SELECT 2")


# ── secrets are never read for the Copilot either ─────────────────────────

def test_copilot_column_probe_skips_secrets(instance, monkeypatch):
    data, outside = instance
    from revoluzen_flow import main as main_module
    from revoluzen_flow.ai.draft_choices import _file_columns
    monkeypatch.setitem(main_module.app_state, "data_dir", str(data))
    assert _file_columns(str(data / "secret.key"), "csv") == []
    assert [c["name"] for c in _file_columns(str(outside / "declared.csv"), "csv")] == ["id", "amount"]
