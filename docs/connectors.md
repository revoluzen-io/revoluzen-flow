# Connector catalog

Revoluzen Flow is built around **three connector tiers** — a first-party catalog you can use today, an open framework to extend in minutes, and a community contribution path.

| Tier | What you get | How to use it |
|---|---|---|
| **1. First-party catalog** (37 manifests) | 4 database dialects (PostgreSQL / MySQL / MSSQL / SQLite) + 2 bulk-load dialects (Postgres `COPY FROM STDIN`, Snowflake `PUT` + `COPY INTO`) + ~31 SaaS manifests, all tested + tracked in the cert matrix | Drop in any connection via the Connections page — no setup beyond credentials |
| **2. Open framework** (any connector you need, without a vendor release cycle) | Build a connector from an OpenAPI spec, a SOAP WSDL, an XML request or a response you tested — no compile step, no LLM required | **Insights → API Explorer**. See [docs/extend/build-a-connector.md](extend/build-a-connector.md) for the end-to-end tutorial |
| **3. Community contributions** | Share your manifest as a Gist for quick reuse, or open a PR to ship it first-party in the next release | [Open a connector-contribution PR](https://github.com/revoluzen-io/revoluzen-flow/issues/new/choose) |

**No connector is Plus-gated.** Every manifest, every authoring path, every extension point is open in OSS. The first-party catalog is the starter pack; the framework is the product.

## Airbyte connector scope

Revoluzen Flow includes an `airbyte` connection type for **integration metadata browsing**. It calls an existing Airbyte instance and lists configured Airbyte sources and connections so operators can inventory or reference what Airbyte already manages.

It does **not** bundle or re-host Airbyte's full connector catalog, and it does not execute Airbyte replication jobs inside Revoluzen Flow. If a team wants Airbyte's hundreds of ready-made replication connectors, Airbyte should remain the replication layer; Revoluzen Flow can sit beside it for local ETL workflows, orchestration, reporting pipelines, and workspace-level reliability checks.

Required Airbyte fields:

- `base_url`: Airbyte API base URL, for example `http://airbyte:8001`
- `workspace_id`: Airbyte workspace ID
- `api_key`, `token`, or `access_token`: optional bearer token when the Airbyte API requires it

> Don't see your tool in the first-party list below? Three options, in increasing order of effort:
>
> 1. **Build it yourself** — `Insights → API Explorer`: test an endpoint, generate from its OpenAPI spec, review the inferred auth, save. Works for any vendor with a public OpenAPI spec ([tutorial](extend/build-a-connector.md)). For a SOAP service, import its WSDL there, try an operation and save it. For an XML API, try the request and save that ([details](api-explorer.md)).
> 2. **Request it** — [open a connector-request issue](https://github.com/revoluzen-io/revoluzen-flow/issues/new/choose). Tell us what system + paste the API docs.
> 3. **Use a generic Database Source / HTTP API / CSV / Parquet connector** as a workaround for the specific pipeline.

The live status of each first-party connector is reported by `GET /api/connectors/cert-matrix`. Each row in the matrix carries:

- `depth_label`: `production` / `beta` / `alpha` / `stub` (v2 manifests) or `v1-functional` / `v1-basic` / `v1-stub` (legacy v1)
- `capabilities`: `source` / `sink` / `action` / `trigger` / `pagination` / `incremental` / `schema` / `test` / `oauth_refresh` / `rate_limit` / `schema_drift` / `backfill_safety` (the last 4 added 2026-05-30 — see P3 audit deltas)
- `known_gaps`: curated `manifest.known_gaps[]` ∪ auto-inferred ("no rate-limit policy declared", "OAuth without refresh handling", "schema-drift policy missing", "backfill safety not verified") for v2 manifests

The four 2026-05-30 capability flags surface honesty about a connector's production-readiness before adoption: a green ✓ for "Rate limit" means the manifest declares a backoff policy; the absence renders as a "⚠ N gaps" badge with the per-row breakdown on hover.

## 2026-05-23 — Connector family expansion

**New first-class connector families** (all have testers + catalog providers + picker entries):

| Family | Type | What it is |
|---|---|---|
| Oracle Cloud | `oracle_fusion` | Oracle Fusion Cloud REST API. `api_family` selector picks FSCM / HCM / CRM. Basic auth probe on `/{family}RestApi/resources/latest`. |
| Oracle Reports | `oracle_bip` | Oracle BI Publisher report runner. Tests against `/xmlpserver/services/rest/v1/catalog/folders`. |
| SAP S/4HANA | `sap_s4hana` | SAP S/4HANA via OData v2 or v4. `sap_client` routing + version selector. |
| SAP HR | `sap_successfactors` | SuccessFactors HRIS via OData. Login format `<user>@<company_id>`. |
| Microsoft 365 | `microsoft_graph` | Generic Microsoft Graph for any endpoint (`/users`, `/sites`, `/drives`, `/teams`, `/planner/*`, etc.). Client-credentials OAuth. |

**Five SaaS connectors promoted from manifest-only to first-class:** GitHub, Shopify, Stripe, Notion, Asana. Each has its own backend tester probing the real auth path (e.g. GitHub `/user`, Shopify `/shop.json`, Stripe `/account`) plus a catalog provider that enumerates the v1 manifest's streams.

**Five previously backend-only types are now visible in the picker:**
- Databases: `db2`, `sap_hana`, `teradata`
- Warehouses: `athena` (via Glue Data Catalog), `presto`

**Back-compat aliases.** Old `oracle_api` connections continue to load (resolves to `oracle_fusion`). Old `sap` connections continue to load (resolves to `sap_s4hana`). The DEPRECATED_TYPE_ALIASES resolver in `connections/models.py` handles the read-time translation.

## 1.0-rc certification state

### Tier vocabulary (2026-06-02)

The cert matrix surfaces a single user-facing **tier** per connector, alongside the existing `depth_label`. Tier vocabulary groups connectors by verification confidence (production / verified / beta / experimental), reflecting Revoluzen Flow's verification reality:

| Tier | Definition | Bar to clear |
|---|---|---|
| **Production** | Live-tested against a real tenant on a recurring schedule; safe for paid customers to run unattended. | All Verified criteria, plus: weekly scheduled CI run against a real vendor account, named owner, 30 days of green smoke runs, documented backfill + incremental story. |
| **Verified** | Smoke-tested end-to-end against the real vendor API in CI on every PR; pagination + schema discovery proven. | v2 depth_score ≥ 3; fixture at `backend/tests/fixtures/connectors/<id>/smoke.json`; entry in `backend/revoluzen_flow/connectors/ci/live_smoke.yml`; `tools/test_connector.py <id> --dry-run` green. |
| **Beta** | Well-formed manifest, framework-validated, exercised through the generic REST adapter tests, but not yet smoked against the live vendor. | Manifest parses; auth declared and supported; ≥1 stream with declared pagination; generic adapter tests green. |
| **Experimental** | Manifest exists and loads, but has known structural gaps (no pagination, single-stream coverage, or no incremental path). Hidden from default picker; visible behind "Show experimental" toggle. | Manifest parses; passes `validate_manifest`. |
| **Hidden** | Roadmap placeholder, deprecated, or out of catalog scope. Not shown in any picker, not enumerated by `/api/connectors/cert-matrix` by default. Manifest stays on disk so the slug is reserved and links don't 404. | Manifest declares `"tier": "hidden"` or `"visibility": "hidden"`. Recoverable via `?include_hidden=true`. |

A manifest may declare `"tier": "experimental"` to opt **down** (publishing honesty), but cannot opt **up** — the computed tier is a ceiling.

### Live tier breakdown

| Tier | Count |
| --- | --- |
| Production | **0** |
| Verified | **0** |
| Beta | **19** |
| Experimental | **8** |
| Hidden | **10** |
| **Default-visible total** | **27** |

The default picker shows Production + Verified + Beta; Experimental is gated behind a "Show experimental" toggle; Hidden never renders.

No connector is yet at Production or Verified tier — the bar is live-vendor CI on every PR plus a stored fixture, neither of which is in place yet for any specific connector. The first Verified candidates (per the launch plan): postgres, mysql, sqlite, mongodb, s3/minio, github, weaviate, qdrant, clickhouse — connectors with free tiers or local containers that CI can spin up without paid credentials.

### Hidden connectors (10)

The following are present on disk but suppressed from the picker because they fall outside the enterprise-data-engineering scope Revoluzen Flow targets at 1.0. They can be promoted later if the scope expands:

`airtable`, `facebook_ads`, `google_ads`, `google_analytics`, `linkedin_ads`, `mailchimp`, `monday`, `pipedrive`, `shopify`, `zoho_crm` — consumer marketing / ads / SMB-CRM territory better served by purpose-built tools (n8n for marketing automation; SMB-CRM vendors directly).

## Database and warehouse dialects

Reads and sinks are wired for all of these; schema-drift fixtures are still being added before any can be labeled Production:

- Snowflake
- BigQuery
- Redshift
- Databricks SQL
- MS SQL Server
- Oracle
- MongoDB
- ClickHouse

PostgreSQL, MySQL, and SQLite are the most exercised in the test suite (the demo template + Postgres bulk-load fixtures cover the happy path). MariaDB, CockroachDB and Azure Synapse (read and written through the SQL Server path) are functional but lighter on test coverage. Db2, SAP HANA and Teradata are read only, through add-on drivers. Trino and Presto can be browsed with their add-on clients, but not read in pipelines.

### Drivers

Most drivers ship with Revoluzen Flow. Db2 / Informix, SAP HANA, Teradata, Kafka, Trino and Presto need an add-on driver, which an admin installs on **Settings > Add-ons**. SQL Server, Azure SQL and Azure Synapse also need Microsoft's ODBC driver on the computer. What ships, the add-ons and Docker: **[`install/database-drivers.md`](install/database-drivers.md)**.

## ODBC database

**ODBC database** (2026-11-11, Beta) reads any database that has an ODBC driver
installed on the computer running Revoluzen Flow. Name the driver (or a DSN set
up there), add whatever else the driver needs as `name=value` pairs, and a
username and password. Test connection, browsing, the Database Source and the
Execute SQL step work through it; it cannot be a pipeline destination. Setup:
[`install/database-drivers.md`](install/database-drivers.md#odbc-database-any-database-with-an-odbc-driver).

## Windows file shares (SMB)

**Windows File Share (SMB)** (2026-11-11, Beta) reads and writes `\\server\share`
files with a saved sign-in: server, share, an optional folder, domain, user
name and password, on Windows, Linux and Docker alike. Pipelines use it through
the File step (pick the connection in a Source or Destination), so every format
the File step reads works on a share, including fixed-width text, COBOL
copybook records, EBCDIC and PGP-encrypted files. A file landing in a folder on
the share can also start a pipeline (Schedule dialog, **A file arriving**, **A
Windows file share**). Its client, `smbprotocol` (MIT), ships with Flow. How to
set it up:
[`user-guides/mainframe-and-legacy-files.md`](user-guides/mainframe-and-legacy-files.md).

## SaaS — v2 (beta validation in progress)

These have been migrated to the v2 manifest schema and are running through the certification fixtures. Per the cert matrix, all eight are currently `validation_status: fail` against the production rubric — usually because one or more of the five required fixtures (`auth_error`, `empty`, `happy_path`, `rate_limit`, `schema_drift`) is missing or red.

- GitHub, HubSpot, Jira, Notion, Salesforce, Shopify, Slack, Stripe

## SaaS — v1 functional

The 27 v1-functional connectors work but have known gaps. Typical: pagination capped at the first page set; sink uses basic INSERT instead of dialect-native bulk load; schema drift is logged but not enforced. Use them for prototyping freely; double-check at small data volumes before relying on any of them in scheduled production runs.

Examples (full list at `GET /api/connectors/cert-matrix`): Airtable, Asana, Confluence, Datadog, Dynamics 365, Elasticsearch, Facebook Ads, Freshdesk, GitLab, Google Ads, Google Analytics, Google Workspace, Intercom, LinkedIn Ads, Mailchimp, Microsoft Teams, Monday, NetSuite, PagerDuty, PayPal, Pipedrive, QuickBooks, RabbitMQ, Redis, SAP OData, SendGrid, ServiceNow, Twilio, Workday, Zendesk, Zoho CRM.

Two connectors are tagged **v1 basic** in the matrix — auth + read only, no sink. The picker hides them from the dropdown until they reach v1-functional.

## What's hidden today (UI-only stubs)

A handful of connectors appear in the catalog code but the backend isn't built yet. They're **hidden from the picker** until they ship — we'd rather show you nothing than a connector that fails on first use:

- Cassandra, Couchbase, Cosmos DB, DynamoDB, Neo4j, Firebase, Apache Pulsar, Datadog, Splunk, Oracle ERP API

## Generic Source / Destination

Don't see your tool? Revoluzen Flow has generic Source and Destination nodes that wrap any REST API or JDBC database as a connector. Configure auth + endpoint inside the node's config panel.

## Certification depth scoring

Connectors are scored 0–5 on how complete their implementation is. The scoring rubric:

| Score | Means |
|---|---|
| 0 | UI present, no backend |
| 1 | Basic API call works |
| 2 | Pagination handled |
| 3 | Incremental sync wired (watermark / cursor) |
| 4 | Primary key + upsert path |
| 5 | Full v2 contract including bulk-load sink and fixture coverage |

Run `python -m revoluzen_flow.connections.cli list` to see live depth scores per connector. Use `python -m revoluzen_flow.connections.cli verify-all` (after starting the sandbox compose stack) for the full markdown matrix.

## Need an enterprise connector?

Revoluzen Flow Enterprise is a paid extension for teams; see [revoluzen.io/flow](https://revoluzen.io/flow) for details.

For external sinks (email/webhook/api/Kafka/Slack), see [`idempotency.md`](idempotency.md) for the per-row dedup key story.
