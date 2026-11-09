#!/usr/bin/env python3
"""Postgres -> transform -> Postgres benchmark: Revoluzen Flow vs self-hosted peers.

Measures the REAL Revoluzen Flow code paths against a live Postgres, then runs
the same job through competitor tools on the same machine and database so the
numbers are directly comparable.

Revoluzen Flow legs (in-process, production code):
  rzf_read_fetch        DbSourceNode._execute_real   psycopg2 fetchall
  rzf_read_materialize  DbSourceNode._relation_from_rows   rows -> DuckDB
  rzf_transform         DuckDB join + filter + derived column
  rzf_write_dbsink      DbSinkNode._write_postgresql   (Database Sink, executemany)
  rzf_write_copy        bulk_load -> PostgresBulkLoader (Copy Data + staging, COPY)

Reference legs:
  duckdb_pg_scanner     DuckDB postgres extension, whole ETL in one statement.
                        Shows the engine ceiling if the read/write used
                        DuckDB's native scanner instead of Python row tuples.
  pg_elt_transform      The same transform as CREATE TABLE AS in Postgres.
                        This is the "T" an EL-only tool (dlt, Singer, Airbyte,
                        Fivetran) would still need after loading.

Competitor legs (optional, each in its own virtualenv, run as subprocesses):
  dlt_pyarrow           dlt sql_database source (pyarrow backend) -> postgres
  singer_meltanolabs    MeltanoLabs tap-postgres | target-postgres

Usage
-----
    python tools/benchmark_postgres.py \
        --dsn postgresql://bench:bench@localhost:55432/bench --rows 100000 \
        [--dlt-python PATH] [--singer-bin DIR] [--repeat 3] [--json out.json]

Numbers are machine-relative. Record host, Docker limits and commit alongside
them. This is a repeatable harness, not a published claim.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import statistics
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable
from urllib.parse import urlparse

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

import duckdb  # noqa: E402
import psycopg2  # noqa: E402

SRC = "bench_src"
DST = "bench_dst"

TRANSFORM_SQL = """
SELECT o.order_id, o.customer_id, o.status, o.amount,
       round(o.amount * 1.18, 2) AS amount_gross,
       o.created_at, c.region, c.tier
FROM {orders} o
JOIN {customers} c ON c.customer_id = o.customer_id
WHERE o.status <> 'cancelled'
"""

OUT_DDL = """
CREATE TABLE {table} (
    order_id BIGINT, customer_id INTEGER, status TEXT, amount NUMERIC(12,2),
    amount_gross NUMERIC(14,2), created_at TIMESTAMP, region TEXT, tier TEXT
)
"""


# ── helpers ──────────────────────────────────────────────────────────────

def _cfg_from_dsn(dsn: str) -> dict[str, Any]:
    u = urlparse(dsn)
    return {
        "host": u.hostname or "localhost", "port": u.port or 5432,
        "database": (u.path or "/postgres").lstrip("/"),
        "user": u.username or "postgres", "password": u.password or "",
        "sslmode": "disable",
    }


def _pg(cfg: dict):
    return psycopg2.connect(host=cfg["host"], port=cfg["port"], dbname=cfg["database"],
                            user=cfg["user"], password=cfg["password"])


def _exec(cfg: dict, *stmts: str) -> None:
    conn = _pg(cfg)
    conn.autocommit = True
    try:
        cur = conn.cursor()
        for s in stmts:
            cur.execute(s)
    finally:
        conn.close()


def _scalar(cfg: dict, sql: str):
    conn = _pg(cfg)
    try:
        cur = conn.cursor()
        cur.execute(sql)
        return cur.fetchone()[0]
    finally:
        conn.close()


def _timed(fn: Callable[[], Any]) -> tuple[Any, float]:
    t0 = time.perf_counter()
    out = fn()
    return out, time.perf_counter() - t0


def _rate(rows: int, secs: float) -> int:
    return int(rows / secs) if secs > 0 else 0


def _peak_rss_mb() -> float | None:
    try:
        import psutil  # type: ignore
        info = psutil.Process().memory_info()
        return round(getattr(info, "peak_wset", info.rss) / 1_048_576, 1)
    except Exception:  # noqa: BLE001
        return None


# ── fixture ──────────────────────────────────────────────────────────────

def seed(cfg: dict, rows: int) -> None:
    _exec(
        cfg,
        f"DROP SCHEMA IF EXISTS {SRC} CASCADE",
        f"DROP SCHEMA IF EXISTS {DST} CASCADE",
        "DROP SCHEMA IF EXISTS bench_dlt CASCADE",
        "DROP SCHEMA IF EXISTS bench_dlt_staging CASCADE",
        "DROP SCHEMA IF EXISTS bench_singer CASCADE",
        f"CREATE SCHEMA {SRC}",
        f"CREATE SCHEMA {DST}",
        f"""CREATE TABLE {SRC}.customers AS
            SELECT i AS customer_id, 'region_' || (i % 12) AS region,
                   (ARRAY['bronze','silver','gold'])[1 + i % 3] AS tier
            FROM generate_series(1, 10000) AS i""",
        f"ALTER TABLE {SRC}.customers ADD PRIMARY KEY (customer_id)",
        f"""CREATE TABLE {SRC}.orders AS
            SELECT i::bigint AS order_id,
                   1 + (i * 7919) % 10000 AS customer_id,
                   (ARRAY['new','paid','shipped','cancelled'])[1 + i % 4] AS status,
                   (((i * 37) % 50000) / 100.0)::numeric(12,2) AS amount,
                   timestamp '2026-01-01' + (i || ' seconds')::interval AS created_at,
                   md5(i::text) AS note
            FROM generate_series(1, {rows}) AS i""",
        f"ALTER TABLE {SRC}.orders ADD PRIMARY KEY (order_id)",
        f"ANALYZE {SRC}.orders",
        f"ANALYZE {SRC}.customers",
    )


def _expected_out(cfg: dict) -> int:
    return int(_scalar(cfg, f"SELECT count(*) FROM {SRC}.orders WHERE status <> 'cancelled'"))


# ── Revoluzen Flow legs ──────────────────────────────────────────────────

def run_rzf(cfg: dict, rows: int) -> dict:
    from revoluzen_flow.engine.bulk_load.runner import bulk_load
    from revoluzen_flow.engine.bulk_load.types import BulkLoadRequest
    from revoluzen_flow.nodes.activities import DbSinkNode
    from revoluzen_flow.nodes.db_source import DbSourceNode

    con = duckdb.connect()
    ctx = SimpleNamespace(conn=con, scoped_name=lambda n: n)
    src = DbSourceNode({"source_mode": "query"})
    res: dict[str, Any] = {}

    # E: fetch (psycopg2) then materialise into DuckDB, as DbSourceNode.execute does.
    def read(table: str, scoped: str):
        (r, cols), t_fetch = _timed(lambda: src._execute_real(
            "postgresql", cfg, f"SELECT * FROM {SRC}.{table}"))
        ctx.scoped_name = lambda n, s=scoped: s  # keep the two sources apart
        rel, t_mat = _timed(lambda: src._relation_from_rows(ctx, r, cols))
        con.execute(f"CREATE OR REPLACE TABLE {table} AS SELECT * FROM {scoped}")
        del r
        return t_fetch, t_mat

    f1, m1 = read("orders", "__db_source_orders")
    f2, m2 = read("customers", "__db_source_customers")
    res["rzf_read_fetch"] = f1 + f2
    res["rzf_read_materialize"] = m1 + m2

    # T: DuckDB transform.
    _, t = _timed(lambda: con.execute(
        "CREATE OR REPLACE TABLE out AS " + TRANSFORM_SQL.format(orders="orders", customers="customers")))
    res["rzf_transform"] = t
    out_rel = con.sql("SELECT * FROM out")
    out_rows = con.execute("SELECT count(*) FROM out").fetchall()[0][0]

    # L1: default Database Sink (executemany), same steps as DbSinkNode.execute.
    sink = DbSinkNode({})

    def dbsink():
        data = con.sql("SELECT * FROM out")
        r = data.fetchall()
        sink._write_postgresql(cfg["host"], cfg["port"], cfg["database"], cfg["user"],
                               cfg["password"], DST, "rzf_dbsink_out", data.columns, r,
                               "create", data.types)
    _, t = _timed(dbsink)
    res["rzf_write_dbsink"] = t

    # L2: Copy Data with staging -> PostgresBulkLoader (COPY FROM STDIN), typed target.
    _exec(cfg, f"DROP TABLE IF EXISTS {DST}.rzf_copy_out", OUT_DDL.format(table=f"{DST}.rzf_copy_out"))

    def copy():
        bulk_load(BulkLoadRequest(conn_type="postgresql", config=cfg, table="rzf_copy_out",
                                  schema_name=DST, mode="append", relation=out_rel,
                                  duckdb_conn=con, columns=list(out_rel.columns)))
    _, t = _timed(copy)
    res["rzf_write_copy"] = t

    res["rzf_out_rows"] = out_rows
    res["rzf_dbsink_rows"] = int(_scalar(cfg, f"SELECT count(*) FROM {DST}.rzf_dbsink_out"))
    res["rzf_copy_rows"] = int(_scalar(cfg, f"SELECT count(*) FROM {DST}.rzf_copy_out"))
    res["rzf_peak_rss_mb"] = _peak_rss_mb()
    con.close()
    return res


# ── reference legs ───────────────────────────────────────────────────────

def run_duckdb_scanner(cfg: dict, dsn: str) -> dict:
    con = duckdb.connect()
    try:
        con.execute("INSTALL postgres; LOAD postgres;")
        con.execute(f"ATTACH '{dsn}' AS pg (TYPE postgres)")
        _exec(cfg, f"DROP TABLE IF EXISTS {DST}.duck_out")
        _, t = _timed(lambda: con.execute(
            f"CREATE TABLE pg.{DST}.duck_out AS " + TRANSFORM_SQL.format(
                orders=f"pg.{SRC}.orders", customers=f"pg.{SRC}.customers")))
        return {"duckdb_pg_scanner": t,
                "duckdb_out_rows": int(_scalar(cfg, f"SELECT count(*) FROM {DST}.duck_out"))}
    except Exception as exc:  # noqa: BLE001
        return {"duckdb_pg_scanner_error": str(exc)[:300]}
    finally:
        con.close()


def run_pg_elt(cfg: dict) -> dict:
    _exec(cfg, f"DROP TABLE IF EXISTS {DST}.elt_out")
    _, t = _timed(lambda: _exec(cfg, f"CREATE TABLE {DST}.elt_out AS " + TRANSFORM_SQL.format(
        orders=f"{SRC}.orders", customers=f"{SRC}.customers")))
    return {"pg_elt_transform": t}


# ── competitor legs ──────────────────────────────────────────────────────

DLT_SCRIPT = textwrap.dedent("""
    import sys, dlt
    from dlt.sources.sql_database import sql_database
    from dlt.sources.credentials import ConnectionStringCredentials
    dsn = sys.argv[1]
    sa_dsn = dsn.replace("postgresql://", "postgresql+psycopg2://", 1)
    src = sql_database(ConnectionStringCredentials(sa_dsn), schema="bench_src",
                       table_names=["orders", "customers"], backend="pyarrow")
    p = dlt.pipeline(pipeline_name="rzf_bench", destination=dlt.destinations.postgres(dsn),
                     dataset_name="bench_dlt", pipelines_dir=sys.argv[2])
    info = p.run(src, write_disposition="replace")
    print("DLT_OK", info.has_failed_jobs)
""")


def run_dlt(cfg: dict, dsn: str, py: str) -> dict:
    with tempfile.TemporaryDirectory(prefix="rzf-dlt-") as td:
        script = Path(td) / "run_dlt.py"
        script.write_text(DLT_SCRIPT, encoding="utf-8")
        env = dict(os.environ, RUNTIME__LOG_LEVEL="WARNING")
        proc, t = _timed(lambda: subprocess.run(
            [py, "-I", str(script), dsn, str(Path(td) / "pipelines")],
            capture_output=True, text=True, env=env, timeout=3600))
    if proc.returncode != 0 or "DLT_OK" not in proc.stdout:
        return {"dlt_pyarrow_error": (proc.stderr or proc.stdout)[-600:]}
    return {"dlt_pyarrow": t,
            "dlt_rows": int(_scalar(cfg, "SELECT count(*) FROM bench_dlt.orders"))}


def run_singer(cfg: dict, bin_dir: str) -> dict:
    exe = ".exe" if os.name == "nt" else ""
    tap = str(Path(bin_dir) / f"tap-postgres{exe}")
    target = str(Path(bin_dir) / f"target-postgres{exe}")
    with tempfile.TemporaryDirectory(prefix="rzf-singer-") as td:
        tap_cfg = Path(td) / "tap.json"
        tgt_cfg = Path(td) / "target.json"
        tap_cfg.write_text(json.dumps({
            "host": cfg["host"], "port": cfg["port"], "user": cfg["user"],
            "password": cfg["password"], "database": cfg["database"],
            "filter_schemas": [SRC],
        }), encoding="utf-8")
        tgt_cfg.write_text(json.dumps({
            "host": cfg["host"], "port": cfg["port"], "user": cfg["user"],
            "password": cfg["password"], "database": cfg["database"],
            "default_target_schema": "bench_singer", "add_record_metadata": False,
            "activate_version": False,
        }), encoding="utf-8")
        log = open(Path(td) / "singer.log", "w", encoding="utf-8")

        # Singer taps emit only selected streams: discover, then select both tables.
        # Discovery is setup work, so it is timed separately from the sync.
        def discover():
            out = subprocess.run([tap, "--config", str(tap_cfg), "--discover"],
                                 capture_output=True, text=True, timeout=600, check=True)
            cat = json.loads(out.stdout)
            for s in cat["streams"]:
                for m in s.get("metadata", []):
                    if m.get("breadcrumb") == []:
                        m["metadata"]["selected"] = True
            catalog = Path(td) / "catalog.json"
            catalog.write_text(json.dumps(cat), encoding="utf-8")
            return catalog
        catalog, discover_secs = _timed(discover)

        def pipe():
            t = subprocess.Popen([tap, "--config", str(tap_cfg), "--catalog", str(catalog)],
                                 stdout=subprocess.PIPE, stderr=log)
            g = subprocess.Popen([target, "--config", str(tgt_cfg)], stdin=t.stdout,
                                 stdout=subprocess.DEVNULL, stderr=log)
            t.stdout.close()
            g.wait(timeout=7200)
            t.wait(timeout=60)
            return t.returncode, g.returncode
        (rc_tap, rc_tgt), secs = _timed(pipe)
        log.close()
        tail = (Path(td) / "singer.log").read_text(encoding="utf-8", errors="replace")[-600:]
    if rc_tap or rc_tgt:
        return {"singer_error": f"tap={rc_tap} target={rc_tgt}: {tail}"}
    try:
        n = int(_scalar(cfg, "SELECT count(*) FROM bench_singer.orders"))
    except Exception as exc:  # noqa: BLE001
        return {"singer_error": f"no output table: {exc}; {tail}"}
    return {"singer_meltanolabs": secs, "singer_discover": discover_secs, "singer_rows": n}


# ── driver ───────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(prog="benchmark_postgres")
    ap.add_argument("--dsn", required=True)
    ap.add_argument("--rows", type=int, default=100_000)
    ap.add_argument("--repeat", type=int, default=3)
    ap.add_argument("--dlt-python", help="python.exe of a venv with dlt[postgres,sql_database]")
    ap.add_argument("--singer-bin", help="Scripts/bin dir holding tap-postgres and target-postgres")
    ap.add_argument("--skip-rzf", action="store_true")
    ap.add_argument("--json", help="write raw + median results here")
    args = ap.parse_args()

    cfg = _cfg_from_dsn(args.dsn)
    print(f"seeding {args.rows:,} rows ...", flush=True)
    seed(cfg, args.rows)
    expected = _expected_out(cfg)

    runs: list[dict] = []
    for i in range(args.repeat):
        r: dict[str, Any] = {}
        if not args.skip_rzf:
            r.update(run_rzf(cfg, args.rows))
        r.update(run_duckdb_scanner(cfg, args.dsn))
        r.update(run_pg_elt(cfg))
        if args.dlt_python:
            r.update(run_dlt(cfg, args.dsn, args.dlt_python))
        if args.singer_bin:
            r.update(run_singer(cfg, args.singer_bin))
        # End-to-end totals per run, so the median is of whole jobs, not of parts.
        if "rzf_read_fetch" in r:
            read_t = r["rzf_read_fetch"] + r["rzf_read_materialize"] + r["rzf_transform"]
            r["rzf_e2e_default_sink"] = read_t + r["rzf_write_dbsink"]
            r["rzf_e2e_copy_sink"] = read_t + r["rzf_write_copy"]
        for k in ("dlt_pyarrow", "singer_meltanolabs"):
            if k in r:
                r[k + "_plus_elt"] = r[k] + r["pg_elt_transform"]
        runs.append(r)
        print(f"run {i + 1}/{args.repeat}: " + json.dumps(
            {k: (round(v, 3) if isinstance(v, float) else v) for k, v in r.items()}), flush=True)

    timing_keys = sorted({k for r in runs for k, v in r.items() if isinstance(v, float)
                          and not k.endswith("_mb")})
    median = {k: statistics.median([r[k] for r in runs if k in r]) for k in timing_keys}

    summary = {
        "rows": args.rows, "expected_out_rows": expected, "repeat": args.repeat,
        "host": {"python": platform.python_version(), "duckdb": duckdb.__version__,
                 "machine": platform.machine(), "os": platform.platform(),
                 "cpus": os.cpu_count()},
        "median_seconds": {k: round(v, 3) for k, v in median.items()},
        "rows_per_sec": {k: _rate(args.rows, v) for k, v in median.items()},
        "runs": runs,
    }
    print(json.dumps({k: summary[k] for k in ("rows", "median_seconds", "rows_per_sec")}, indent=2))
    if args.json:
        Path(args.json).write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
