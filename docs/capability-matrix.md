# Revoluzen Flow — Capability Matrix

**What this is:** an honest, code-verified statement of what Revoluzen Flow does
and, just as importantly, what it does **not** do. Every row was checked
against the running backend, not against marketing copy. If a capability
isn't listed as **Supported**, don't sell it.

**What Revoluzen Flow is:** a single-operator **batch / ELT** tool — move data
from sources through SQL/transform steps into sinks, check its quality, and
orchestrate it on a schedule with retries. It runs on an in-process DuckDB
engine. It is *not* a streaming platform, a Spark distribution, a feature
store, or a governed lakehouse.

Legend: ✅ Supported · ◐ Partial / limited · ✕ Not in OSS

## Core data engineering

| Capability | Status | Notes |
|---|---|---|
| Batch / ELT pipelines (sources → transforms → sinks) | ✅ | JSON/CSV/Parquet/Excel/XML/S3/FTP/Google Sheets/Delta sources & sinks; joins, aggregates, SCD2, dedupe, SQL transform on DuckDB. |
| SQL transforms | ✅ | `SQL Transform` node runs SELECT/CTE SQL on DuckDB over named inputs. |
| Data quality / validation / profiling | ✅ | `DataQuality` node (13 checks; drop/fail/tag/reject/split modes, thresholds, per-row score) + data profiling. |
| Orchestration & scheduling | ✅ | Cron / interval / daily / weekly schedules, overlap watchdog, missed-run alerts, retries with failure classification. |
| Intra-pipeline DAG (dependency order, topological run) | ✅ | Steps run in dependency order within a pipeline. |
| Cross-pipeline orchestration (one pipeline triggers another) | ◐ | Only via an explicit `execute_pipeline` step inside a pipeline — there is **no** scheduler-level cross-pipeline dependency graph. |
| Change data capture (CDC) | ◐ | Kafka/Debezium + Postgres logical-decoding code exists, but reads a **bounded snapshot batch**, not a continuous stream. Edition docs mark CDC **Plus-only**. |
| Lineage | ◐ | A base run/column-level lineage store exists in OSS code, but the edition matrix currently lists lineage as Plus-only — see *Known inconsistencies*. |
| AI Copilot / assistants | ✅ | Pipeline-authoring Copilot + node helpers (explain, fix-error, suggest-SQL). LLM-first with deterministic rule fallbacks; embedding/guardrail/semantic-router nodes run offline via a hash-embedding fallback. This is an **assistant**, not model training. |

## Connectors

| | Status | Notes |
|---|---|---|
| Databases (Postgres, MySQL, SQL Server, plus 2 bulk-load dialects) | ✅ | Real drivers + tested query paths. |
| Generic REST connector runtime | ✅ | Works against arbitrary REST APIs; author one from an OpenAPI spec. |
| ~27 first-party SaaS connectors | ◐ | Shipped as **manifests** run through the generic adapter. They validate structurally, but only `github` has recorded acceptance fixtures; the rest are **not** verified against live vendors. Treat as Beta/Experimental unless the connector's own row shows an attestation date. |

> **Which number means what.** The catalog ships 66 connectors
> (`CONNECTION_TYPES`, excluding roadmap entries) — that is the count to
> quote. The ~27 SaaS REST manifests graded above are a *subset* of those
> 66, so the cert matrix's grades describe that subset and must never be
> restated against the full catalog ("19 of our 66 are Beta" is wrong —
> the matrix never looked at the other 39).
>
> Do not derive a verified-vs-total ratio from any of these numbers. The
> API deliberately stops providing one, for the reason given in
> `cert_matrix()`: unattested means no fixtures recorded, not tests failed.

## ETL and ELT

Both, by different mechanisms — worth knowing which you are using, because
they put the compute in different places.

**ETL** is the default. Sources load into DuckDB relations, transform nodes
run in the engine, results are written to sinks. `compile/sql_preview.py` puts
it plainly: pipelines execute imperatively, each node building a DuckDB
relation at runtime. This is what you get from `transform`, `aggregate`,
`join`, `pivot`, `window`, `scd2` and the rest.

**ELT** goes through `execute_sql_task`, which fires a statement at a real
database connection instead of transforming data in memory. Load raw first,
then transform where the data landed.

| Engine | Load into | Transform in (Execute SQL) |
|---|---|---|
| Postgres, MySQL/MariaDB, SQLite | ✅ | ✅ |
| SQL Server, Azure Synapse | ✅ | ✅ |
| Snowflake, BigQuery, Redshift | ✅ | ✅ |
| Databricks / lakehouse | ✅ (Parquet to ADLS or S3) | ✅ |

Warehouse drivers are optional extras (`pip install revoluzen-flow[snowflake]`
and so on); Redshift needs none, being Postgres wire-compatible.

### Medallion architecture

The layers compose without the intermediate data returning through the engine:

1. **Bronze** — any source to `adls_gen2_sink` or `s3_sink`, which write
   `FORMAT PARQUET` (parquet is the default format).
2. **Silver / gold** — `execute_sql_task` against a Databricks connection,
   building Delta tables on Databricks compute.
3. **Serve** — `db_sink` to SQL Server, routed through the bulk loader.

### What this is not

The dbt importer is a **migration path, not ELT orchestration**. It converts a
compiled `manifest.json` into Flow pipelines where each model becomes a SQL
Transform step that runs *in DuckDB*, not in your warehouse. Its own module
docstring lists DuckDB dialect risk among the caveats. If you want dbt's
in-warehouse semantics, use `execute_sql_task` rather than the importer.

`compile/sql_preview.py` is likewise a **preview**, not pushdown — it shows
the SQL the engine would run so you can read and keep it, and it marks
anything it cannot express as an explicit boundary comment.

---

## Explicitly NOT in Revoluzen Flow

State these plainly; do not imply otherwise.

| Capability | Status | Reality |
|---|---|---|
| Streaming / real-time stream processing | ✕ | Everything is batch on in-memory DuckDB. Kafka sources read a **bounded** batch then close. "Real-time" in the UI means live WebSocket **progress**, not stream processing. |
| Apache Spark / PySpark | ✕ | No Spark anywhere. The "Spark-style" worker-pool label is cosmetic UI naming only. |
| Feature store | ✕ | Not implemented, not claimed. |
| Governed lakehouse (Iceberg / Delta catalog / governance) | ✕ | Per-table Delta read/write exists, but there is **no** catalog or governance layer. A governed lakehouse is **Foundation**, a separate Revoluzen product — not Revoluzen Flow. |
| ML model training | ✕ | Only embeddings + cosine routing. No training/fitting is offered or needed. |

## Known inconsistencies to resolve before a public launch

These aren't code bugs, but the story contradicts itself in places. Pick one
source of truth for each:

1. **Lineage edition.** OSS code implements a base lineage store
   (`lineage/__init__.py`, `api/lineage.py`), yet `edition-matrix.md` (at the repo root) and
   `docs/product_facts/25_anti_patterns.md` declare lineage **Plus-only**. The
   README says it "ships in the box." A reader can currently be told opposite
   things — decide whether base lineage is an OSS feature and align all three.
2. **Connector count vs. readiness.** See the Connectors note above — the
   headline "66 connectors / working connector in 90 seconds" overstates how
   many integrations are actually verified.
3. **Plus-only nodes shipping in the OSS repo.** `connectors/cdc.py`,
   `connectors/jdbc.py`, and `connectors/vector_db.py` are present and
   registered in OSS, but the edition matrix's Boundary Rule says "Plus
   features never live in the Revoluzen Flow repo, even as stubs." Either the matrix
   is wrong (these are OSS) or the nodes shouldn't be here.

## Honest wins

The README already avoids the biggest traps: it does **not** claim streaming,
Spark, feature store, governed lakehouse, Iceberg, or ML — all verified absent
in code, and several are proactively disclaimed in
`docs/product_facts/25_anti_patterns.md`. The remaining exposure is narrow and
listed above.

---
*Generated from a code-level capability audit. Re-verify against the source
before publishing; treat the code, not this file, as the ground truth.*
