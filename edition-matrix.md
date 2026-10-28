# Revoluzen Flow Edition Matrix

**Status:** Active · canonical source of truth for *what's free* and *what requires Revoluzen Flow Enterprise*. Last verified against the code 2026-10-28.

If a feature is not on this page, it is in Revoluzen Flow (free / Apache 2.0). Plus features never live in this repo — even as stubs (Boundary Rule 2).

---

## At a glance

| | **Revoluzen Flow** (Apache 2.0) | **Revoluzen Flow Enterprise** (commercial) |
|---|---|---|
| **Who it's for** | Solo builders, small teams, anyone evaluating, sovereignty buyers | Teams shipping pipelines to production with audit / approval / SLA needs |
| **Install** | Single binary, SQLite, runs on a laptop | Containerized worker pool, optional Postgres, runs in your cloud |
| **License** | Apache 2.0 — fork it, ship it, sell it | Commercial subscription |
| **Support** | Community (GitHub Issues, best-effort) | SLA-backed (4h response on Pro, 1h on Enterprise) |
| **Updates** | Public releases | Public + early access + LTS lines |

---

## Pipeline building (everything below is in OSS)

| Capability | Revoluzen Flow | Revoluzen Flow Enterprise |
|---|:-:|:-:|
| Pipeline canvas (drag-drop) | ✅ | ✅ |
| 44 node types across 6 categories (Data Movement · Transform · Combine · Control Flow · Action · AI / Semantic) — the palette count: 39 curated in `ModulesPanel.tsx` plus 5 reconciled in from the backend registry. The registry itself holds 105 `StepType` entries; the rest are legacy keys kept registered so old pipelines keep running, and hidden from the palette via `HIDDEN_TYPES`. | ✅ | ✅ |
| 66 connectors visible by default (Databases 11 · Warehouses & Lake 12 · Files & Storage 11 · APIs & Apps 21 · Operations & AI 11) — the New Connection picker count: 80 declared types minus 14 marked roadmap. Nine carry a Plus tier label in the catalog (a pricing posture, not a runtime gate), so say "66 connectors", not "66 open-source connectors". The cert matrix at `/api/connectors/cert-matrix` grades a different population — the SaaS REST manifests only, not the 66 connection types. v1→v2 + fixture coverage uplift is the post-1.0 connector roadmap. | ✅ | ✅ |
| Expression engine (`$json`, `$now`, `$('Node')`) | ✅ | ✅ |
| SQL Transform (DuckDB-backed) | ✅ | ✅ |
| Templates (27 OSS) | ✅ | ✅ + private marketplace |
| 27 OSS templates (simple ETL, dedup, aggregation, data quality) | ✅ | ✅ |
| Connections (project + global scope) | ✅ | ✅ + Vault-backed credential refs |
| Variables (pipeline + global) | ✅ | ✅ |
| Credentials store (Fernet at rest, `~/.revoluzen-flow/secret.key`) | ✅ | ✅ + Vault-managed rotation |
| Schedules (interval, daily, weekly, monthly, multiple run times) | ✅ | ✅ |
| Manual + scheduled execution | ✅ | ✅ |
| Plugins SDK | ✅ | ✅ |
| Vector DB sinks (Pinecone, Weaviate, Qdrant) — moved to OSS 2026-10-23 | ✅ | ✅ |
| JDBC dialect registry (register your own DB driver) — moved to OSS 2026-10-23 | ✅ | ✅ |
| CLI | ✅ | ✅ + admin commands |

## Pipeline building (Plus only)

| Capability | Revoluzen Flow | Revoluzen Flow Enterprise |
|---|:-:|:-:|
| Enterprise connectors: SAP, SAP HANA, NetSuite, Workday, Dynamics 365, ServiceNow, Salesforce, Informix, Teradata, DB2 | — | ✅ |
| CDC (change-data-capture) source nodes — enforced at execution via `licensing.require_plus` | — | ✅ |
| Maintained enterprise dialects shipped through the JDBC registry (SAP HANA, Teradata, DB2, Informix) | — | ✅ |

> **Enforcement note (2026-10-21).** Until this date the Plus-only node rows
> above were declarations with nothing behind them: `connectors/cdc.py`
> registered `StepType.CDC_SOURCE` with no licence check, so CDC ran on free
> instances. There was also no node-level gate anywhere to copy — the
> existing `is_plus` checks live in AI tools and catalog listings, not in node
> execution.
>
> `revoluzen_flow/licensing.py` is now that pattern: `is_plus()` as the single
> detection point (fails closed), and `require_plus(feature)` raising
> `PlusRequiredError`, which `engine/failure_class.py` maps to `USER_INPUT`
> so the retry policy stops rather than burning attempts on something no retry
> can fix.
>
> The gate is applied at **execution**, not registration. A free instance
> still shows the node and lets a pipeline be built against it, so the catalog
> does not differ between editions; the refusal happens on run and names the
> feature.
>
> **Vector sinks and the JDBC registry moved to OSS (2026-10-23).** Both were
> listed Plus-only and neither was ever enforced. Rather than add the gate,
> the decision was to drop the claim, for reasons specific to each.
>
> Vector sinks: OSS already ships the whole local RAG stack — Ollama
> embeddings and a SQLite-vec store. Charging to write pipeline output to a
> vector database while embedding freely for our own features is the row a
> buyer attacks first, and for an AI-native product vector output is table
> stakes rather than a premium add-on.
>
> The JDBC registry is the *extensibility mechanism*, and Postgres, MySQL,
> MSSQL, SQLite and DuckDB all reach their databases without touching it. So
> gating it protected no revenue; it only stopped someone with an unusual
> database helping themselves. The maintained enterprise dialects shipped
> *through* that registry stay Enterprise, which is the same split used for
> execution-engine adapters: the interface is open, the maintained
> integration is paid.
>
> Anti-pattern rule 25 in `docs/product_facts/25_anti_patterns.md` was
> updated to match: it now names CDC alone, and records Vector and JDBC as
> having moved to OSS on 2026-10-23.

## Execution & operations

| Capability | Revoluzen Flow | Revoluzen Flow Enterprise |
|---|:-:|:-:|
| Single-binary local execution | ✅ | ✅ |
| Containerized worker pool (Spark-style) | — | ✅ |
| Worker pool monitoring page | ✅ governor banner, spill-disk health, hardware presets, queue depth, throughput | ✅ + per-worker memory sparklines, validate-config button, retention, P&L per pipeline |
| Vertical scaling (workers + DuckDB tuning + spill + global governor) | ✅ | ✅ |
| Worker-role guard (anti-footgun for naïve `docker compose --scale`) | ✅ | ✅ |
| API-only instance mode (`REVOLUZEN_FLOW_ENABLE_SCHEDULER=0`) | ✅ | ✅ |
| Durable scheduler lease — N instances, only the holder fires schedules | ✅ | ✅ |
| Multi-worker horizontal scaling (worker pool across machines) | — | 🟠 designed |
| DEV environment | ✅ | ✅ |
| PROD environment + DEV→PROD promotion | — | ✅ |
| Two-gate approval workflow (Sandbox approval → Deploy approval) | — | ✅ |
| Sandbox isolated dry-run before promotion | — | ✅ |
| Notifications: in-app + email + Slack + Discord + generic webhook | ✅ | ✅ |
| Notifications: long-running threshold + schedule miss detection (per-pipeline) | ✅ | ✅ |
| Notifications: browser desktop alerts | ✅ | ✅ |
| Notifications: quiet hours (suppress non-critical during off-hours) | — | ✅ |
| Notifications: debounce (collapse repeated alerts within window) | — | ✅ |
| Notifications: daily digest emails | — | ✅ |
| Notifications: per-event policies, escalation, per-user preferences | — | ✅ |
| Compute-usage alerts (memory/CPU/runtime thresholds) | — | ✅ |
| Drift detection (scheduled scans + critical-event notifications) | — | ✅ |
| Manual backup + cloud providers (S3, Azure, GCS) | ✅ | ✅ + scheduled, retention, Parquet archive |
| Persistence | SQLite | SQLite + optional Postgres |

## AI — embedded features

These are the inline AI helpers that augment specific UI moments (SQL helper, pre-publish card, diagnose-error, post-run summary, cost-estimate, anomaly detect, pre-run validate). All ship in OSS. Plus adds workspace-level governance.

| Capability | Revoluzen Flow | Revoluzen Flow Enterprise |
|---|:-:|:-:|
| Inline AI helpers (SQL, transform, diagnose, summarize, validate) | ✅ | ✅ |
| Provider config (Claude, OpenAI, OpenRouter, Ollama, Gemini, DeepSeek, Groq, Mistral, Azure, Custom) | ✅ per-user | ✅ workspace-wide admin-managed |
| OpenRouter model browser (free-tier badge, tools-only filter) | ✅ | ✅ |
| Token wallet caps (per-user daily) | ✅ env-tunable | ✅ + workspace-wide budget caps |
| Audit log of every LLM call (provider, model, tokens, latency) | ✅ | ✅ + retention policy + export |
| Provider price comparison + recommendation | ✅ | ✅ |

## AI — agent & Copilot

The agentic Copilot and its 30 tools all ship in OSS. Plus adds organizational guardrails.

| Capability | Revoluzen Flow | Revoluzen Flow Enterprise |
|---|:-:|:-:|
| Copilot dock (resizable, page-aware, slash commands) | ✅ | ✅ |
| 30 tools (22 READ + 7 SAFE_WRITE + 1 HIGH_IMPACT_WRITE), plus 2 opt-in web tools registered only when the operator enables web access — full list in `docs/product_facts/10_ai_copilot.md`; source of truth is `INITIAL_TOOLS` in `backend/revoluzen_flow/ai/tools/__init__.py` | ✅ | ✅ |
| Fast-lane router (rule-based; sub-1 s answers for the 14 most-common questions) | ✅ | ✅ |
| Three-layer chat knowledge: session context (Layer 1) + product knowledge RAG (Layer 2) + 30 tools (Layer 3) | ✅ | ✅ |
| Provider-aware wall-clock cap (300 s local, 120 s cloud) + Stop button | ✅ | ✅ |
| Agent loop with bounded iterations + wall-clock cap | ✅ | ✅ |
| Tool-tier RBAC for the agent (4 roles: viewer / developer / admin / super_admin) | ✅ | ✅ |
| Sanitization gateway (PII / credentials redacted before LLM sees data) | ✅ | ✅ |
| Idempotency cache for write tools | ✅ | ✅ |
| Dry-run-by-default for new write tools (3-success threshold to unlock live) | ✅ | ✅ |
| Confirmation card for every write | ✅ | ✅ |
| Trace store (every run persisted with replay-safe step records) | ✅ | ✅ + 1-year retention + export |
| Activity timeline page (audit + agent + execution feed) | ✅ | ✅ + cross-workspace view |
| Prompt signing (HMAC system prompt integrity) | ✅ | ✅ |
| Llama-Guard / safety classifier on every turn | — | ✅ |
| Cross-session conversational memory (managed context, deletable) | — | ✅ |

## AI — RAG & retrieval

| Capability | Revoluzen Flow | Revoluzen Flow Enterprise |
|---|:-:|:-:|
| Local embeddings via Ollama nomic-embed-text | ✅ | ✅ |
| SQLite-vec workspace-scoped vector store | ✅ | ✅ |
| `recall_history` tool — search execution failures, pipeline definitions, catalog, docs | ✅ | ✅ |
| Daily 03:00 UTC indexer | ✅ | ✅ |
| Proactive trend detection ("this pipeline fails every Monday") | — | ✅ |
| Cross-workspace federated retrieval | — | ✅ |

## RBAC, identity, multi-user

There are **two RBAC systems** in Revoluzen Flow — the agent-tool RBAC (always on, in OSS) and the workspace RBAC (Plus). They serve different purposes.

> **SSO is designated OSS and is not yet built (decided 2026-10-23).**
>
> The row below says `🟠 planned` rather than `✅` because OIDC/SAML is not
> implemented in this repository at all. The only matches for "oidc" and
> "saml" in the codebase are prose in the AI knowledge base and
> `connectors/openapi_security.py`, which is about *connector* auth schemes,
> not signing in to Flow. Moving SSO to OSS is a commitment to build it, not
> a row to flip, and the table should not imply otherwise.
>
> Why OSS rather than Enterprise, when most of the field charges for it:
> that is exactly the reason. Governance in a *free self-hosted* build exists
> in Apache NiFi and partly Airflow, and essentially nowhere else — Airbyte,
> Dagster, Prefect, Kestra, Mage, n8n and StreamSets all paywall it. That
> empty intersection of sovereign + governed + AI-native is the whole
> positioning. Putting authentication behind the paywall moves Flow into the
> bucket it is trying to differentiate from, for marginal revenue.
>
> It also follows CISA's secure-by-demand guidance that single sign-on,
> multi-factor authentication and access to security logs should not carry a
> surcharge. Enterprise keeps SCIM provisioning and directory sync, which is
> the identity *administration at scale* that organisations actually buy.

| Capability | Revoluzen Flow | Revoluzen Flow Enterprise |
|---|:-:|:-:|
| Agent-tool RBAC (4 roles × 2 envs × 3 tiers) — gates what the LLM can call | ✅ | ✅ |
| Workspace RBAC (5-tier: Super Admin → Workspace Admin → Data Engineer → Analyst → Viewer) | — | ✅ |
| Local user accounts | ✅ | ✅ |
| Single-user / solo mode | ✅ | — |
| Multi-user projects, comments, sticky notes | — | ✅ |
| OIDC / SAML SSO — ⚠ **designated OSS, not yet built** (see note below) | 🟠 planned | ✅ |
| SCIM provisioning + directory sync | — | ✅ |
| IP allowlist, session controls, password policy | — | ✅ |
| Seat limits | unlimited | per subscription tier |

## Audit, governance, compliance

| Capability | Revoluzen Flow | Revoluzen Flow Enterprise |
|---|:-:|:-:|
| `audit_log` table with every authenticated action — the record, and the ability to read it, are OSS | ✅ | ✅ retention + export + sigstore-signed |
| Trace store for every agent run | ✅ | ✅ + extended retention |
| Lineage (Marquez-compatible) | — | ✅ |
| Vault (AES-256, credential references, rotation) | — | ✅ |
| Export to enterprise platforms (signed migration bundles) | — | ✅ |
| Compliance posture document | ✅ (`docs/ai-ops-contract.md`) | ✅ + DPA (Revoluzen SOC 2 report planned, not yet available) |

## Insights (consolidated nav, May 1 2026 — renamed from "AI-Hub" May 17 2026 PR 4)

A single Sidebar entry "**Insights**" replaces three previously-scattered entries (Trust, Activity, Reports). The hub holds four subtabs in OSS:

| Subtab | OSS | Plus extension |
|---|:-:|:-:|
| **Activity** — audit + agent-trace + execution timeline (compact KPI strip + 5-col table) | ✅ | ✅ + cross-workspace view, retention controls |
| **Trust** — security posture, "what we never do" pillars, kill-switch instructions | ✅ | ✅ (Revoluzen SOC 2 report planned, not yet available) |
| **Reports** — one-shot installation report (PDF/MD download) | ✅ | ✅ + **custom report builder + scheduler** (pick metrics + grouping + filters → save as template → schedule daily/weekly/monthly → email to distribution list) |
| **AI Provider** — per-user provider config, model browser, OpenRouter free-tier filter | ✅ per-user | ✅ + workspace-wide admin-managed config + monthly_budget_usd + allow_user_override toggle |
| **Governance** (Plus only) | — | ✅ workspace RBAC config, approval policies, dry-run thresholds |
| **Retention** (Plus only) | — | ✅ per-store retention overrides, archive-to-Parquet schedule, S3 sink |
| **Audit Export** (Plus only) | — | ✅ sigstore-signed audit-log export, SIEM-compatible formats |

Legacy `/#trust`, `/#activity`, `/#reports` URLs continue to work — App.tsx routes them through Insights on the matching subtab.

## What this means in practice

**You should choose Revoluzen Flow if** you're a solo builder, a small team prototyping, an evaluator, or you have strong sovereignty requirements (everything runs locally including embeddings). The agent, RAG, the activity timeline, the 66 connectors visible by default, schedules, alerts, embedded AI — they all ship for free, forever.

**You should choose Revoluzen Flow Enterprise if** you're shipping pipelines to production and need: enterprise connectors (SAP / NetSuite / etc.), DEV→PROD promotion with approval gates, multi-user collaboration, OIDC/SAML, audit-log retention for compliance, lineage, vault-managed credentials, or 4-hour SLA support. Plus is positioned for *production convenience* — it's not enterprise-only.

**Both editions share the same architecture**: same agent loop, same boundary contract, same trust posture. Plus is additive. Revoluzen Flow never gets worse when Plus exists.

---

## Which side does a new feature go on?

One test, added 2026-10-23 after several rows were decided ad hoc and three
had to be moved back to OSS:

| If the feature is needed… | Then it is… |
|---|---|
| for the product to produce **correct** results | OSS |
| to run the product **safely** at all | OSS |
| to **find out what happened** | OSS |
| to operate it at **organisational scale** | Enterprise |
| as a **maintained integration** with someone else's product | Enterprise |
| as **governance over third-party code or people** | Enterprise |
| as **managed operations, SLA, support** | Enterprise |

Said shorter: **the mechanism and the interface are OSS; maintained
integrations, administration-at-scale and governance over third parties are
Enterprise.**

Two consequences worth stating plainly, because both were got wrong once:

**Never gate correctness.** Recovery, checkpointing, idempotency and honest
failure reporting ship in OSS. Charging for correctness damages trust in the
whole product; charging for maintained integrations and support does not.

**Never gate security basics.** SSO, MFA and access to the audit log are not
upsells. This is both a principle and a strategy — it is the only
structurally empty seat in the market, and paywalling governance surrenders
it (see the SSO note above).

Where an interface and its integrations both exist, split them: the
execution-engine protocol, the connector SDK and the JDBC registry are OSS;
the maintained Databricks, dbt, SAP and Teradata integrations built on them
are Enterprise.

---

## Boundary rules (immutable)

1. **Anything in Revoluzen Flow stays in Revoluzen Flow.** OSS features are never moved behind the Plus paywall.
2. **Plus features never live in the Revoluzen Flow repo.** Even as stubs.
3. **Plus depends on Revoluzen Flow**, never the other way around. Revoluzen Flow runs standalone with no Plus code present; Plus extends Revoluzen Flow via the public plugin entry-point mechanism.
4. **Plus code never enters this repository.** Plus is shipped as a separate package that consumes Revoluzen Flow's public APIs.
5. **The Copilot never advertises Plus features by default to OSS users.** Tools like `list_catalog` filter Plus-only items unless the user explicitly asks ("what's in Plus?"). Honest discovery, not background upselling.

---

## How tier is determined at runtime

- Revoluzen Flow has **no environment variable** to declare itself Plus.
- Plus injects a `LicenseManager` into `app_state["license_manager"]` at startup.
- If absent: `tier = "free"`. If present and `license_mgr.is_plus`: `tier = "plus"`.
- This is the single source-of-truth check. Both backend (`api/auth.py`) and the agent's `list_catalog` tool consult the same place.

---

## Update process

This file is updated whenever a feature ships in either repo. Both maintainer teams keep it in sync via PR review. The version authoritative for a given Revoluzen Flow release is the file in that release's git tag.

**Last reviewed:** 2026-10-28.

### Counts, and where they actually come from

Every count in this file has gone stale at least once by being retyped from
another document rather than re-derived. Re-derive them:

| Count | Source of truth |
|---|---|
| Node types (44 / 6 categories) | `MODULES` in `frontend/src/components/ModulesPanel.tsx`, plus backend types not in `HIDDEN_TYPES` (`frontend/src/components/hiddenNodeTypes.ts`) |
| `StepType` registry (105) | `GET /api/node-types` |
| Connectors (66) | `CONNECTION_TYPES` minus `CONNECTOR_STATUS` roadmap entries, both in `frontend/src/components/pages/ConnectionsPage.tsx` |
| Agent tools (30) | `INITIAL_TOOLS` in `backend/revoluzen_flow/ai/tools/__init__.py`, or `GET /api/ai/agent/status` → `tool_count` |
| Templates (27) | `frontend/src/templates/catalog.ts` |

**2026-10-28 review:** re-derived every count against the running build.
Connectors (66, and all five group totals) and templates (27) were correct.
Node types corrected 40 → 44 and tools corrected 26 (21/4/1) → 30 (22/7/1);
both had drifted. Removed a stale note claiming anti-pattern rule 25 still
needed updating — it had already been updated. Dropped the May 3 / May 4
changelog entries below this line, which asserted 43 connectors and 19 tools
and so contradicted the body of the file they were attached to.
