# Anti-patterns — patterns the Copilot must NOT suggest

This is a reference list of incorrect, misleading, or hallucinated
responses the LLM may generate when it doesn't have accurate Revoluzen Flow
knowledge. The Copilot should use this file to catch itself before
serving these answers.

Each entry has: the bad pattern, why it's wrong, and the correct
Revoluzen Flow-specific answer.

---

## Credentials and security

### 1. Don't recommend HashiCorp Vault for OSS Free credentials

**Bad:** "Store your Postgres password in HashiCorp Vault and reference
it from Revoluzen Flow."

**Why wrong:** HashiCorp Vault integration is Revoluzen Flow Enterprise only (Vault-Ref
pattern). OSS Free has no vault adapter.

**Correct:** "Use the built-in Credentials page (sidebar → Credentials
→ + New Credential). Revoluzen Flow encrypts secrets at rest with Fernet
(AES-128-CBC + HMAC-SHA256). The master key is at
`~/.revoluzen-flow/secret.key`."

**Source:** `edition-matrix.md` line 145, `07_credentials.md`.

### 2. Don't recommend AWS Secrets Manager for OSS Free

**Bad:** "Configure AWS Secrets Manager as your credential backend."

**Why wrong:** Same as above — external secret managers are Plus only.

**Correct:** Same as #1 — use the Credentials page.

### 3. Don't say "store credentials as environment variables"

**Bad:** "Set your database password as `DB_PASSWORD` env var."

**Why wrong:** Revoluzen Flow does NOT read database credentials from env vars.
The Credentials page is the only path. Env vars (`REVOLUZEN_FLOW_*`) are for
system config (memory limits, ports), not secrets.

**Correct:** "Save the password in the Credentials page. Revoluzen Flow
encrypts it at rest. Pipelines reference credentials by ID."

### 4. Don't claim SOC 2 certification of the software

**Bad:** "Revoluzen Flow is SOC 2 certified."

**Why wrong:** The Revoluzen Flow software itself is not separately certified.
The operator's deployment can be brought into scope of the operator's
own SOC 2 attestation.

**Correct:** "Revoluzen Flow provides the controls (encryption at rest, audit
log, RBAC in Plus) to support your SOC 2 compliance. Revoluzen Flow Enterprise customers
get a Revoluzen SOC 2 Type II attestation report as supporting evidence."

**Source:** `edition-matrix.md` line 147, `18_analytical_questions.md`.

---

## Product terminology

### 5. Don't conflate Dashboard with Pipelines

**Bad:** "Navigate to the Revoluzen Flow Dashboard to see your list of
pipelines and their schedules."

**Why wrong:** Dashboard and Pipelines are **two different pages**.
Dashboard is the first sidebar entry — the home / landing page with
a greeting, hero KPIs, system usage, and recent-activity feeds.
Pipelines is the page that lists every pipeline with status + step
count + actions.

**Correct:**
- "**Dashboard** (sidebar → first entry, the home icon) shows hero
  KPIs, system usage (CPU/memory/throughput/DB size/uptime), and
  recent-activity feeds. DEV and PROD render different KPIs."
- "**Pipelines page** (sidebar → Pipelines) is the per-pipeline list
  with status badges, step counts, run buttons, and the editor
  entry-point."

**Source:** `frontend/src/components/pages/DashboardPage.tsx`,
`frontend/src/components/Sidebar.tsx`.

### 6. Don't say "124 connectors" or "60+ connectors"

**Bad:** "Revoluzen Flow supports 124 connectors out of the box."

**Why wrong:** The actual count is **66 connectors visible by default** —
the New Connection picker's own number. `CONNECTION_TYPES` declares 80
entries and `CONNECTOR_STATUS` marks 14 as `roadmap`, which the picker
filters out: 80 − 14 = 66. Earlier marketing claimed 60+, then 45, then
43, then 37, then 33 — each pass counted a different population without
saying which. The 33 counted only SaaS manifests on disk and missed every
database, warehouse and file connector.

**Correct:** "Revoluzen Flow ships 66 connectors visible by default:
Databases 11 · Warehouses & Lake 12 · Files & Storage 11 · APIs & Apps 21
· Operations & AI 11. Plus an OpenAPI import page to build your own."

**Two traps when quoting this number:**

- Say **"66 connectors"**, never *"66 open-source connectors"*. Nine
  (sap, sap_hana, netsuite, workday, dynamics365, servicenow, salesforce,
  teradata, db2) carry a Plus tier label in the catalog. There is no
  license check behind it — it is a pricing posture, not a runtime gate —
  but marketing them as paid while calling all 66 open-source is an
  overclaim.
- The cert matrix grades a **different population**: the SaaS REST
  manifests only, not the 66 connection types. Never say "19 of our 66 are
  Beta" — say "of the listed SaaS connectors".

**Definition — count what a user can actually use.** 45 manifest JSON
files exist on disk (37 unique slugs + 8 `.v2.json` overlays), but 10
carry a Hidden tier flag: they are slug reservations, out of scope for
enterprise data engineering, and reachable only via
`?include_hidden=true`. Counting them yields 43 and overstates what
ships. Counting unique slugs yields 37 and is a file-count artifact,
not a user-facing number. **33 is the visible, usable count** — see
`docs/product_facts/08_connectors.md` for the full breakdown.

**Source:** `docs/product_facts/08_connectors.md` (breakdown) + `edition-matrix.md`.

### 7. Don't say PROD environment is available in OSS Free

**Bad:** "Switch to PROD in your OSS Free install for production runs."

**Why wrong:** PROD environment + DEV→PROD promotion is Revoluzen Flow Enterprise only.
OSS Free has DEV as the only contracted environment.

**Correct:** "OSS Free runs in the DEV environment. For production
promotion with approval gates, you need Revoluzen Flow Enterprise."

**Source:** `edition-matrix.md` lines 60-61.

---

## Technology stack

### 8. Don't suggest PySpark or Spark SQL

**Bad:** "Write a PySpark transformation to aggregate your data."

**Why wrong:** Revoluzen Flow uses DuckDB for all data processing. PySpark is
not in the execution engine. There is no Spark context, no SparkSession,
no DataFrame API.

**Correct:** "Write a DuckDB SQL query in the Transform node. DuckDB
supports standard SQL with extensions (window functions, CTEs, PIVOT,
UNNEST, JSON path extraction)."

### 9. Don't suggest Apache Airflow as an alternative or comparison

**Bad:** "If Revoluzen Flow doesn't work for you, try Apache Airflow."

**Why wrong:** Locked feedback rule — no competitor product names in
user-visible content. This applies to Airflow, Prefect, Dagster, n8n,
Talend, SSIS, ADF, Informatica, Fivetran.

**Correct:** Answer the user's actual question using Revoluzen Flow features.
If the feature genuinely doesn't exist, say so honestly without naming
alternatives.

### 10. Don't compare Revoluzen Flow to monitoring/APM tools

**Bad:** "Revoluzen Flow is similar to Datadog / Splunk / Grafana for pipeline
monitoring."

**Why wrong:** Revoluzen Flow is a data pipeline platform, not an
APM/observability tool. It has an Activity page for audit + execution
history, but it's not a monitoring product.

**Correct:** "Revoluzen Flow's Activity page (Insights → Activity) shows audit
logs, agent traces, and execution history. For infrastructure
monitoring, use your existing observability stack — Revoluzen Flow is the data
pipeline tool, not the monitoring tool."

---

## AI and agent

### 11. Don't suggest fine-tuning the local model

**Bad:** "Fine-tune the Ollama model on your pipeline data for better
answers."

**Why wrong:** Revoluzen Flow uses prompt-injection of context (Layer 1
session context + Layer 2 product knowledge RAG + Layer 3 tools), not
fine-tuning. No model training is offered or needed.

**Correct:** "The Copilot learns about Revoluzen Flow through curated product
knowledge files (`docs/product_facts/*.md`) retrieved via RAG, plus 19
tools that access live workspace state. To improve answers, edit the
product knowledge files and reindex."

### 12. Don't promise "agent remembers previous chats"

**Bad:** "The Copilot remembers your previous conversations and can
reference past context."

**Why wrong:** Cross-session conversational memory is Revoluzen Flow Enterprise only.
OSS Free starts each chat session fresh.

**Correct:** "In OSS Free, each chat session is independent. The trace
store keeps per-run records (History tab in the dock), but there's no
cross-session memory. Cross-session conversational memory is a Revoluzen Flow Enterprise
feature."

**Source:** `edition-matrix.md` line 110.

### 13. Don't tell a Free user to set up Llama-Guard

**Bad:** "Enable Llama-Guard for content safety filtering."

**Why wrong:** Llama-Guard / safety classifier on every agent turn is
Revoluzen Flow Enterprise only.

**Correct:** "Content safety filtering via Llama-Guard is Revoluzen Flow Enterprise
only. OSS Free has the sanitization gateway (PII/credential redaction)
and the prompt signing check as its safety layers."

**Source:** `edition-matrix.md` line 109.

---

## Architecture

### 14. Don't say "run multiple OSS containers for scaling"

**Bad:** "Scale by running `docker compose up --scale revoluzen-flow=4`."

**Why wrong:** The worker-role guard in OSS refuses to start in
multi-worker mode. Running multiple OSS containers against the same
SQLite would corrupt state.

**Correct:** "OSS Free is single-process. For horizontal scaling, you
need Revoluzen Flow Enterprise with its containerized worker pool + Redis queue. For
OSS, scale vertically: raise `REVOLUZEN_FLOW_MAX_CONCURRENT_RUNS`,
`REVOLUZEN_FLOW_DUCKDB_MEMORY_LIMIT`, and use an SSD for the spill directory."

**Source:** `edition-matrix.md` lines 55, 58-59.

### 15. Don't say "switch OSS to Postgres"

**Bad:** "Migrate your OSS database from SQLite to Postgres."

**Why wrong:** There's no SQLite→Postgres migration path in OSS. Postgres
support is a Plus optimization for multi-worker horizontal scaling.

**Correct:** "OSS Free uses SQLite exclusively. It handles hundreds of
pipelines and weeks of execution history comfortably. Postgres is
available in Revoluzen Flow Enterprise for multi-worker deployments."

**Source:** `edition-matrix.md` line 74.

### 16. Don't suggest Kubernetes autoscaling for OSS

**Bad:** "Deploy Revoluzen Flow on Kubernetes with HPA for autoscaling."

**Why wrong:** Multi-pod OSS is not supported (same SQLite corruption
risk). K8s deployment is a Plus pattern.

**Correct:** "OSS is single-binary, single-node. Run it in Docker or
natively. For K8s-style orchestration, Revoluzen Flow Enterprise supports multi-worker
deployment with its queue-based architecture."

---

## Features that don't exist

### 17. Don't say "Notebook Node" or suggest Papermill

**Bad:** "Use the Notebook Node to run your Jupyter notebooks in the
pipeline."

**Why wrong:** There is no Notebook Node in Revoluzen Flow. Papermill
integration does not exist.

**Correct:** "Revoluzen Flow doesn't have a Notebook Node, and there is no
built-in run-your-own-Python node in either edition. Use the DuckDB SQL
Transform node for custom logic, write a first-class node type in Python
and register it (see docs/extend/build-a-node.md), or run your notebook
externally and ingest the output via CSV/JSON Source."

Do NOT tell anyone Revoluzen Flow Enterprise has a "Python Transform" node — it does not.

### 18. Don't mention internal architecture phase names

**Bad:** "Revoluzen Flow implements the Six Planes architecture for survivability."

**Why wrong:** Internal design concepts and roadmap phase names are not
user-facing features. Users shouldn't encounter these terms.

**Correct:** Describe the actual feature in plain language. "Revoluzen Flow
runs pipelines via a single-process executor with DuckDB-backed
transforms and a checkpoint store for resume-on-failure."

### 19. Don't mention "HMAC machine fingerprinting"

**Bad:** "Revoluzen Flow uses HMAC machine fingerprinting for license
validation."

**Why wrong:** Not a user-facing feature. The prompt signing (HMAC
system prompt integrity) is the only HMAC feature documented — and it's
about system prompt integrity, not machine fingerprinting.

**Correct:** "Revoluzen Flow uses HMAC-SHA256 prompt signing to verify system
prompt integrity — the agent refuses tampered prompts."

---

## Edition boundary violations

### 20. Don't suggest quiet hours / debounce / daily digest for OSS

**Bad:** "Set up quiet hours to suppress alerts overnight."

**Why wrong:** Quiet hours, debounce, and daily digest emails are
Revoluzen Flow Enterprise notification features.

**Correct:** "In OSS Free, alerts fire immediately to the configured
channel. Quiet hours, debounce, and daily digest are Revoluzen Flow Enterprise features.
For OSS, you can work around this by scheduling pipelines only during
business hours."

**Source:** `edition-matrix.md` lines 68-69.

### 21. Don't tell a Free user to "configure escalation"

**Bad:** "Set up an escalation policy so unacknowledged alerts go to
the next person."

**Why wrong:** Escalation is Plus-only. OSS Free is single-user —
there's nobody to escalate to.

**Correct:** "Escalation policies are Revoluzen Flow Enterprise only. In OSS Free,
configure multiple channels (email + Slack) on the same alert rule so
you see it in multiple places."

### 22. Don't suggest lineage for OSS

**Bad:** "Check the Lineage page to see cross-pipeline data flow."

**Why wrong:** Lineage (Marquez-compatible) is Revoluzen Flow Enterprise only.

**Correct:** "Lineage is Revoluzen Flow Enterprise only. In OSS, the closest substitute
is the `recall_history` tool (search across executions, pipeline
definitions, catalog) and `summarize_pipeline` for individual pipeline
reasoning."

**Source:** `edition-matrix.md` line 144.

### 23. Don't suggest CDC sources for OSS

**Bad:** "Use the CDC Source node for real-time change capture from
Postgres."

**Why wrong:** CDC source nodes (Debezium-style) are Revoluzen Flow Enterprise only.

**Correct:** "CDC sources are Revoluzen Flow Enterprise only. In OSS, poll for changes
using a Database Source with an incremental query
(`WHERE updated_at > $last_sync`) on a schedule."

**Source:** `edition-matrix.md` line 46.

---

## Numeric / factual accuracy

### 24. Don't say "the agent has 16 tools" (stale number)

**Bad:** "The Copilot has 16 tools."

**Why wrong:** The canonical count is **26 tools** (21 READ + 4
SAFE_WRITE + 1 HIGH_IMPACT_WRITE) as of the 2026-05-28 SSOT pass.
16 / 19 / 20 were stale numbers from earlier reconciliations.

**Correct:** "The Copilot has 26 tools organized in three tiers. Full
list in `docs/product_facts/10_ai_copilot.md`."

**Source:** the registry at `backend/revoluzen_flow/ai/tools/__init__.py` is
the source of truth; `edition-matrix.md` line 96 is mirrored from it.

### 25. Don't say "40 node types" without clarifying Plus-only

**Bad:** "Revoluzen Flow has 40 node types, all in OSS."

**Why wrong:** Some node types (JDBC Source/Sink, CDC Source, Vector
Source/Sink) are Plus-only. The StepType enum has 108 entries but many
are Plus-gated or internal. The SSOT for the user-visible palette is
`frontend/src/components/hiddenNodeTypes.ts` (`VALID_GHOST_TYPES`),
which contains exactly 40. Note: there is no Python-code node in either
edition — do not list "Python Transform" as a Plus node type.

**Correct:** "Revoluzen Flow ships 40 node types across 6 categories in OSS.
Plus adds enterprise node types (JDBC, CDC, Vector DB). See
`02_node_types.md` for the per-node breakdown."

**Source:** `edition-matrix.md` lines 22-48.

### 26. Don't claim 14 fast-lane intents if the count changes

**Bad:** Hardcoding a specific count without noting the source.

**Correct:** "The fast lane has 14 rule-based intents as of May 4 2026.
The canonical list is in `backend/revoluzen_flow/ai/fast_router.py` — always
reference the code."

---

## Process and workflow

### 27. Don't suggest running `REVOLUZEN_FLOW_ROLE=worker` in OSS

**Bad:** "Start a dedicated worker with `REVOLUZEN_FLOW_ROLE=worker`."

**Why wrong:** The worker-role guard refuses this in OSS. Multi-worker
is Plus-only.

**Correct:** "Worker mode is Revoluzen Flow Enterprise only. OSS runs everything in a
single process."

### 28. Don't tell users to "check the docs" without specifics

**Bad:** "Check the Revoluzen Flow documentation for more details."

**Why wrong:** Vague. The user is already asking the Copilot because
they want a specific answer.

**Correct:** Always name the specific page, endpoint, env var, or file.
"Open the Executions page → filter Status = error → click the run →
read the error_message field."

### 29. Don't suggest Jinja templates for expressions

**Bad:** "Use Jinja template syntax: `{{ json.amount }}`."

**Why wrong:** Revoluzen Flow has its own expression engine. Syntax is
different: `$json.amount` (not `json.amount`), and template literals
use `{{ $json.amount }}` (note the `$` prefix).

**Correct:** "Use Revoluzen Flow's expression engine: `$json.amount * 1.18`.
Inside string params, wrap in template literals:
`'total_{{ $json.amount * 1.18 }}'`."

**Source:** `12_templates_expressions.md`.

### 30. Don't recommend `$env.VAR` for sensitive values

**Bad:** "Reference your API key with `$env.API_KEY` in the node
config."

**Why wrong:** Even though the expression engine supports `$env.VAR`
for whitelisted env vars, credentials should NEVER be in env vars or
expressions. They go through the Credentials page.

**Correct:** "Store the API key in the Credentials page → type
`api_key`. Reference the credential by ID in the node config. The
runtime resolves it securely."
