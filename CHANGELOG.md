# Changelog

All notable changes to Revoluzen Flow are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

API stability policy: see `docs/api.md`. The HTTP API surface under `/api/*`
follows the compatibility rules documented there.

## [Unreleased]

### Added

- **More than one instance is now safe to run.** Two additions, together:

  `REVOLUZEN_FLOW_ENABLE_SCHEDULER=0` makes a node API-only — it serves
  requests without running the scheduler loop. Defaults on, so a single-node
  install is unchanged.

  `scheduling/lease.py` holds a durable single-row lease in the metadata
  database; only the holder fires schedules. Acquire and renew are one
  conditional `UPDATE` so two instances cannot both win between a read and a
  write, the TTL is three tick intervals, and the holder renews every tick.
  A clean `stop()` releases the lease so a standby takes over on its next
  tick rather than waiting out the TTL.

  Previously the scheduler tracked in-flight work in process-local dicts and
  started unconditionally on every instance. Those dicts stop one process
  double-running a schedule but say nothing about a second process, so two
  instances behind a load balancer fired **every schedule twice** — silently,
  with both runs looking legitimate. Verified with three instances over ten
  ticks: ten firings, all from one node, where the old behaviour would have
  produced thirty.

  The lease fails closed. An instance that cannot reach the lease row does
  not schedule, because missing a tick is recoverable and already alerted on
  by schedule-miss detection, while a duplicate run can double-charge a
  customer or duplicate rows in their warehouse.

  Supported topology is now N API instances plus whichever one holds the
  lease. A worker pool spanning machines is still not supported — see
  `edition-matrix.md`.

- **Edition gate** — `revoluzen_flow/licensing.py` provides `is_plus()` and
  `require_plus(feature)`, the first runtime enforcement of the Plus-only node
  declarations in `edition-matrix.md`.

  Those declarations previously had nothing behind them: CDC is listed
  Plus-only in the edition matrix, in `ai/session_context.py` and in
  anti-pattern rule 25, but `connectors/cdc.py` registered
  `StepType.CDC_SOURCE` with no licence check and ran on free instances. No
  node-level gate existed anywhere to follow — the `is_plus` checks in the
  codebase were all in AI tools and catalog listings.

  Detection fails closed: any import failure, missing `app_state` or
  partially initialised licence manager reads as free, because an edition
  check that defaults to paid when something breaks is not a check.
  `PlusRequiredError` is its own type so the UI can offer an upgrade path, and
  `engine/failure_class.py` maps it to `USER_INPUT` so the retry policy stops
  — no number of attempts produces a licence.

  The gate runs at **execution**, not registration, so a free instance still
  shows the node and can build a pipeline against it. The catalog stays
  identical across editions and the refusal names the feature when it runs.

  CDC is gated. Vector DB sinks and the JDBC dialect registry are still
  declared Plus-only with no enforcement; each is now a one-line change, left
  open as a per-feature pricing decision.

- **ELT on warehouses and the lakehouse.** `execute_sql_task` — the node that
  fires SQL at a real connection rather than transforming in-memory — now
  reaches `redshift`, `mssql`/`synapse`, `snowflake`, `bigquery` and
  `databricks`, alongside the existing postgres, mysql/mariadb and sqlite.

  Previously the matrix stopped at Postgres, MySQL and SQLite, so Revoluzen
  Flow could *load into* Snowflake, BigQuery, Redshift and a lake but not
  *transform in* them — the half of ELT that people mean by the word. The
  dialect matrix is deliberately one function shared by Execute SQL and the
  Lookup activity, so all of these arrived in one place.

  This completes a Medallion pipeline end to end: land bronze as Parquet with
  `adls_gen2_sink` or `s3_sink` (both emit `FORMAT PARQUET`, and parquet is
  the default), build silver and gold with SQL running on Databricks or the
  warehouse itself, then serve to SQL Server via `db_sink`, which routes
  through the bulk loader. The intermediate layers never travel back through
  the engine.

  Drivers stay optional extras — `pip install revoluzen-flow[databricks]` and
  friends — and a missing one now reports the install command rather than an
  ImportError naming an unfamiliar module. Redshift needs no new dependency;
  it is Postgres wire-compatible and uses psycopg2, differing only in its
  default port.

### Fixed

- **Execute SQL on MySQL silently discarded writes.** `pymysql` connects with
  `autocommit=False` and the branch never committed, so an `INSERT` or
  `UPDATE` was rolled back when the connection closed — while the cursor
  still reported a rowcount and the node still reported success. A write that
  is lost this way is indistinguishable from one that worked. Commit
  semantics are now pinned per driver and covered by tests, because they
  genuinely differ: psycopg2 commits on context-manager exit, pymysql and
  pyodbc need an explicit commit, Snowflake autocommits, and BigQuery and
  Databricks have no interactive transaction to commit.

### Changed

- `GET /api/connectors/cert-matrix` no longer returns the `by_tier` or
  `production_total` aggregates. Certification is reported per connector:
  each row carries `tier` and `cat_green`, and an attested connector also
  carries `attested_at` and `attested_checks`.

  A connector reaches an attested tier only once real vendor responses have
  been recorded for it, so an unattested connector means "no fixtures
  recorded yet" — not "this connector failed". An aggregate renders those
  two states identically, and the reading that wins is the wrong one.
  Callers that want a count can tally `rows` and own the framing.

- The Trust page drops the attestation totals and gains an **Attested**
  column on the per-connector matrix, showing the date a connector's
  acceptance suite last replayed green. Connectors without one show an
  em-dash rather than a zero or a "not verified" badge.

### Added

- **Source Watch** — daily detection that a connector's upstream API changed,
  in `backend/revoluzen_flow/connectors/source_watch.py` and
  `.github/workflows/source-watch.yml`.

  Nothing previously could notice this. The acceptance suite replays recorded
  cassettes, which proves our parsing against *the responses we recorded* and
  by construction never contacts the vendor; the live-smoke allow-list is
  empty. So the first party to learn that a connector had broken was whoever
  it broke for.

  Four signals, none of which needs a vendor account:
  - the vendor advertises a newer API version than a connector is pinned to
  - an endpoint one of our streams reads is gone from the vendor's own spec
  - the vendor's published spec or changelog moved at all
  - RFC 8594 `Deprecation` / `Sunset` headers, harvested passively from
    responses the product already received — no extra request, and it covers a
    customer's own internal APIs too

  Connectors opt in with a `source_watch` block in their runtime manifest
  (`docs/connector-authoring.md`). Eight ship with one — Asana, GitHub,
  Intercom, Mailchimp, PagerDuty, Slack, Stripe and Twilio — each verified
  against the vendor's live spec before being added. A connector without a
  block is reported as `unwatched` rather than healthy. Read the state at
  `GET /api/connectors/source-watch`, which makes no outbound call of its own.

  Version severity reflects what the evidence supports: `critical` only when
  the vendor no longer lists the version a connector pins, `warn` when a newer
  version exists but ours is still supported, `info` when we pin the newest.
  A probe narrowed to a single value cannot establish support at all, so it is
  capped at `warn`. GitHub currently reports `warn` — 2026-03-10 has shipped
  and 2022-11-28 remains supported.

  Spec comparison reconciles base prefixes from either side, since Swagger 2
  puts them in `basePath` (Slack `/api`, Mailchimp `/3.0`) while OpenAPI 3
  puts them in `servers[].url` (Stripe `/`, Twilio none). JSON and YAML specs
  are both read; an unparseable one degrades to fingerprint-only rather than
  asserting that endpoints vanished.

- `.github/workflows/connector-attest.yml` re-runs the connector acceptance
  suite twice weekly and on manifest or fixture changes. A green CAT result
  older than 14 days stops counting, so without this every attested
  connector silently decayed back to Beta.

## [1.0.1] — 2026-10-18

First release published as **Revoluzen Flow**, by Revoluzen Technologies Pvt Ltd.

The engine, connectors and UI are unchanged from the 1.0.x line; this release
renames the project and its published artifacts.

### Changed

- Distribution is now `revoluzen-flow` on PyPI. The CLI entry point is
  `revoluzen` (alias `rz`); `python -m revoluzen_flow` works as well.
- Python package is `revoluzen_flow`.
- Environment variables use the `REVOLUZEN_FLOW_` prefix, and the config
  directory is `.revoluzen-flow`.
- Docker image is `revoluzen/flow`; the Compose service is `flow` and the
  data volume is `revoluzen_flow_data`.
- OpenLineage events are emitted with the `revoluzen-flow` namespace and a
  producer URI pointing at this repository.

### Added

- `scripts/check_repo_coordinates.py`, run by the `architecture-invariants`
  CI job. It fails the build when a self-referential GitHub slug disagrees
  with `backend/revoluzen_flow/app_meta.py`, or when documentation tells a
  reader to `cd` into a directory that `git clone` does not create.

### Notes for existing installs

**This is a clean break. There is no migration path, and none is planned.**

The rename is total — nothing keeps the old spelling. In particular, these
carry stored or externally-observed state, and all of them changed:

- **Workflow node type** `fpulseNode` → `revoluzenFlowNode`. Recorded inside
  every saved workflow, so pipelines saved by an earlier release will not
  load.
- **Database file** `fpulse.db` → `revoluzen_flow.db` (and
  `fpulse_events.db` → `revoluzen_flow_events.db`). An existing data
  directory will not be found; the instance starts empty.
- **Session cookies** `fpulse_token` / `fpulse_session` / `fpulse_csrf` →
  `revoluzen_flow_*`. Everyone is signed out once.
- **Prometheus metrics** `fpulse_*` → `revoluzen_flow_*`. Existing
  dashboards and alert rules stop matching.
- **Bootstrap account** `admin@fpulse.local` → `admin@revoluzen.local`.
- **CSS classes, DOM data-attributes and custom events** — internal, but
  listed for anyone who has written custom styling against them.

Treat an upgrade as a fresh install.
