# Postgres processing benchmark: Revoluzen Flow vs self-hosted and cloud tools

Measured on 7 November 2026 with `tools/benchmark_postgres.py`. Raw results:
[`results/postgres-100k-2026-11-07.json`](results/postgres-100k-2026-11-07.json).

## The job

Postgres to Postgres, the shape most Revoluzen Flow customers run:

1. Read `orders` (100,000 rows: bigint, int, text, numeric, timestamp, text) and
   `customers` (10,000 rows).
2. Join, drop cancelled orders, add one derived column (`amount * 1.18`).
3. Write the 75,000 result rows to a new Postgres table.

EL-only tools (dlt, Singer) copy both tables and then run the same transform as
`CREATE TABLE AS` inside Postgres. That SQL step is added to their time so
every tool does the same work.

## Results (median of 3 runs, same laptop, same Postgres)

| Tool and path | Seconds | Rows per second |
|---|---:|---:|
| DuckDB Postgres scanner (engine reference) | 0.39 | 256,057 |
| dlt 1.17, pyarrow backend, plus SQL in Postgres | 11.96 | 8,364 |
| Meltano Singer tap/target-postgres, plus SQL in Postgres | 24.05 | 4,158 |
| **Revoluzen Flow, Copy Data with staging (COPY)** | **24.18** | **4,136** |
| **Revoluzen Flow, Database Sink (default)** | **109.68** | **911** |

Rows per second is the 100,000 source rows divided by the job's wall time.

### Where Revoluzen Flow's time goes

| Stage | Code path | Seconds |
|---|---|---:|
| Read from Postgres | `DbSourceNode._execute_real` (psycopg2 `fetchall`) | 0.43 |
| **Load rows into DuckDB** | `DbSourceNode._relation_from_rows` (rows rendered as SQL `VALUES` text) | **22.83** |
| Transform | DuckDB join, filter, derived column | 0.06 |
| Write, Copy Data with staging | `PostgresBulkLoader` (`COPY FROM STDIN`) | 1.09 |
| **Write, Database Sink** | `DbSinkNode._write_with_conn` (`executemany` INSERT) | **88.08** |

Memory: the whole in-process run peaked at 2.6 GB. Fetching the 100,000 rows
peaks at 109 MB. The generated `VALUES` text is 9.7 MB, but DuckDB parsing it
pushes the peak to 2.3 GB. At this rate a 1M-row read would need roughly 20 GB.

## What this says

- **The engine is fast; the Postgres edges are not.** DuckDB does the whole job
  in 0.39 s when it reads Postgres itself. Revoluzen Flow's best path is about
  60 times slower because of the row-to-SQL-text load step.
- **The default sink is the biggest loss.** `executemany` is about 80 times
  slower than the COPY loader that already ships in `engine/bulk_load`.
- **Against competitors today:** the best Revoluzen Flow path is level with
  Singer and about 2 times slower than dlt. The default path is about 9 times
  slower than dlt.
- **After the two fixes below**, the read and write legs would cost about
  0.4 s and 1.1 s, which would put Revoluzen Flow ahead of every self-hosted tool
  measured here, with the transform done in flight rather than in the target.

## Fixes, in order of payoff

1. Load query results into DuckDB as Arrow (or through DuckDB's `postgres`
   extension) instead of `_rows_to_values`. Removes most of the run time and
   most of the memory.
2. Route the Database Sink's Postgres writes through `PostgresBulkLoader` by
   default, falling back to `executemany` only when the driver is missing.
3. Re-run at 1M and 10M rows on a fixed cloud VM so the numbers can be published.

## Published figures from other machines

None of these is comparable with the laptop numbers above. Each was run by a
vendor of one of the tools in it.

| Test | Result | Source |
|---|---|---|
| Postgres to Postgres, 298M rows, 64 vCPU | dlt 1.0M rows/s, PeerDB 718k, pg_dump 610k, Sling 283k, Debezium 45k, Airbyte OSS 28.7k | [Galaxy](https://www.getgalaxy.io/articles/postgres-replication-benchmark-7-tools-tested-on-298m-rows) |
| Postgres to BigQuery, 24.6M rows, 8 vCPU | dlt ConnectorX 3.2 min, Sling 14 min, Airbyte 19.6 min, Meltano 162 min | [dltHub](https://dlthub.com/blog/self-hosted-tools-benchmarking) |
| Postgres to BigQuery, 191.5M rows, 8 vCPU | dlt ConnectorX 4.6 min, Fivetran 15.2 min, Airbyte 2.4 37.1 min, Stitch 100 min | [dltHub](https://dlthub.com/blog/sql-benchmark-saas) |
| Postgres to Iceberg full load | Fivetran 46k rows/s, Estuary 4k, Airbyte Cloud 457 (disputed) | [OLake](https://olake.io/docs/benchmarks) |

The local Singer result (about 4,200 rows/s) and the dltHub Meltano figure
(about 2,500 rows/s) agree in order of magnitude, which supports the harness.

## Cloud services

No cloud ETL vendor publishes a throughput figure for a Postgres source.
Estimated list cost for 10M changed rows a month (about 2 GB), one source,
daily batches:

| Service | Charged by | About per month |
|---|---|---:|
| Fivetran Standard | Monthly active rows | $1,358 |
| Hevo Starter | Events | $299 |
| Matillion | Credits per task-hour | $225 (unverified credit price) |
| Estuary Flow | GB plus connectors | $202 |
| Stitch Standard | Rows | $180 |
| Airbyte Cloud | Credits per GB | $40 |
| Rivery (Boomi) | Credits per 100 MB | $36 |
| Azure Data Factory | DIU-hours plus runs | $10 |
| AWS Glue | DPU-hours | $4.40 |

Sources are listed per row in the benchmark dashboard. Revoluzen Flow has no
managed cloud edition yet; self-hosted cost is the machine it runs on.

## Reproduce

```bash
docker run -d --name rzf-bench-pg -e POSTGRES_USER=bench -e POSTGRES_PASSWORD=bench -e POSTGRES_DB=bench -p 55432:5432 --memory 1g --cpus 2 postgres:16-alpine -c shared_buffers=256MB -c synchronous_commit=off
```

```bash
python tools/benchmark_postgres.py --dsn postgresql://bench:bench@localhost:55432/bench --rows 100000 --repeat 3 --dlt-python <venv-with-dlt>/python --singer-bin <venv-with-meltanolabs-tap-and-target>/Scripts --json out.json
```

Competitor environments: `dlt[postgres,sql_database]==1.17.1` with `pyarrow`
and `numpy`; `meltanolabs-tap-postgres==0.10.0` and
`meltanolabs-target-postgres==0.8.0`. Each goes in its own virtualenv.

## Test machine

Intel Core i7-13700H, 15.7 GB RAM with about 0.2 GB free during runs, Windows
11. Postgres 16.13 in Docker capped at 2 CPUs and 1 GB. Revoluzen Flow at commit
`a8e4d5c`, Python 3.11.8, DuckDB 2.0.0, psycopg2 2.9.13. Airbyte OSS was not run
locally because its installer needs at least 8 GB of free memory. The third
run of each tool was slower for every tool alike, from memory pressure; medians
absorb it.
