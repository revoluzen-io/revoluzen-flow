# dbt import sample

`manifest.json` here is a hand-built but faithful **compiled dbt manifest** —
the artifact dbt writes to `target/manifest.json`. The importer reads that
file and never the raw `models/*.sql`, because dbt has already resolved
`{{ ref }}`, `{{ source }}`, macros and `is_incremental()` into
`compiled_code` plus an explicit `depends_on` DAG.

## Using it

UI: **Pipelines → Import → dbt project**, then pick this file.

API:

```bash
curl -X POST http://localhost:8001/api/workflows/import-dbt \
  -H "Content-Type: application/json" \
  -H "X-Workspace-Id: default" \
  -d "{\"manifest\": $(cat samples/dbt/manifest.json)}"
```

## What it covers

Chosen so the import exercises every mapping branch, not just the happy one:

| dbt node | materialization | imports as |
|---|---|---|
| `stg_orders`, `stg_customers` | `view` | Transform only — a view persists nothing, so no sink |
| `int_customer_country` | `ephemeral` | Transform, flagged redundant (dbt inlines these as CTEs) |
| `customers` | `table` | Transform → Destination, `mode: create` |
| `fct_orders` | `incremental` + `unique_key` | Transform → Destination, `mode: merge`, `merge_on: ["order_id"]` |
| `order_status_counts` | `incremental`, no key | Transform → Destination, `mode: append` |
| `raw.raw_orders`, `raw.raw_customers` | source | placeholder node, bind a connection |
| `country_codes` | seed | placeholder node |
| `customers_snapshot` | snapshot | placeholder node |
| `unique_customers_customer_id` | test | ignored — not a relation |

Expected result: **6 models, 2 sources, 1 seed, 1 snapshot, 12 edges, 3
destinations.**

## After importing

The import deliberately does NOT run. Every placeholder source and every
destination needs a real connection chosen first, and each reports a
validation error until you pick one — that is the importer refusing to guess
where your data lives.

Two caveats the report also raises, worth reading before scheduling:

- The compiled SQL is **warehouse dialect** (this sample says `duckdb` in its
  metadata). A manifest compiled against Snowflake or BigQuery may use syntax
  the SQL Transform does not accept.
- For incremental models, dbt's `is_incremental()` filter is baked into the
  compiled SQL, but the run state it depends on is not. Review the `WHERE`
  clause before putting it on a schedule.

## Getting a real manifest

This sample is for exercising the importer. For a real one, compile any dbt
project — `dbt compile` or `dbt docs generate` writes `target/manifest.json`.

If you have no warehouse to point at, `jaffle_shop_duckdb` (dbt Labs' learning
project, DuckDB-backed) runs entirely locally and produces a manifest with the
same shape as the one here.
