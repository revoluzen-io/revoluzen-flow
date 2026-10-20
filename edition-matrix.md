# Revoluzen Flow Edition Matrix

**Status:** Active 2026-05-01 · canonical source of truth for *what's free* and *what requires Revoluzen Flow Enterprise*.

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
| 40 node types across 6 categories | ✅ | ✅ |
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
| CLI | ✅ | ✅ + admin commands |

## Pipeline building (Plus only)

| Capability | Revoluzen Flow | Revoluzen Flow Enterprise |
|---|:-:|:-:|
| Enterprise connectors: SAP, SAP HANA, NetSuite, Workday, Dynamics 365, ServiceNow, Salesforce, Informix, Teradata, DB2 | — | ✅ |
| CDC (change-data-capture) source nodes | — | ✅ |
| Vector DB sinks (Pinecone, Weaviate, Qdrant) | — | ✅ |
| JDBC dialect registry (custom DB driver registration) | — | ✅ |

## Execution & operations

| Capability | Revoluzen Flow | Revoluzen Flow Enterprise |
|---|:-:|:-:|
| Single-binary local execution | ✅ | ✅ |
| Containerized worker pool (Spark-style) | — | ✅ |
| Worker pool monitoring page | ✅ governor banner, spill-disk health, hardware presets, queue depth, throughput | ✅ + per-worker memory sparklines, validate-config button, retention, P&L per pipeline |
| Vertical scaling (workers + DuckDB tuning + spill + global governor) | ✅ | ✅ |
| Worker-role guard (anti-footgun for naïve `docker compose --scale`) | ✅ | ✅ |
| Multi-worker horizontal scaling | — | 🟠 designed |
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

The agentic Copilot and its 26 tools all ship in OSS. Plus adds organizational guardrails.

| Capability | Revoluzen Flow | Revoluzen Flow Enterprise |
|---|:-:|:-:|
| Copilot dock (resizable, page-aware, slash commands) | ✅ | ✅ |
| 26 tools (21 READ + 4 SAFE_WRITE + 1 HIGH_IMPACT_WRITE) — full list in `docs/product_facts/10_ai_copilot.md` | ✅ | ✅ |
| Fast-lane router (rule-based; sub-1 s answers for the 14 most-common questions) | ✅ | ✅ |
| Three-layer chat knowledge: session context (Layer 1) + product knowledge RAG (Layer 2) + 26 tools (Layer 3) | ✅ | ✅ |
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

| Capability | Revoluzen Flow | Revoluzen Flow Enterprise |
|---|:-:|:-:|
| Agent-tool RBAC (4 roles × 2 envs × 3 tiers) — gates what the LLM can call | ✅ | ✅ |
| Workspace RBAC (5-tier: Super Admin → Workspace Admin → Data Engineer → Analyst → Viewer) | — | ✅ |
| Local user accounts | ✅ | ✅ |
| Single-user / solo mode | ✅ | — |
| Multi-user projects, comments, sticky notes | — | ✅ |
| OIDC / SAML SSO | — | ✅ |
| IP allowlist, session controls, password policy | — | ✅ |
| Seat limits | unlimited | per subscription tier |

## Audit, governance, compliance

| Capability | Revoluzen Flow | Revoluzen Flow Enterprise |
|---|:-:|:-:|
| `audit_log` table with every authenticated action | basic | ✅ retention + export + sigstore-signed |
| Trace store for every agent run | ✅ | ✅ + extended retention |
| Lineage (Marquez-compatible) | — | ✅ |
| Vault (AES-256, credential references, rotation) | — | ✅ |
| Export to enterprise platforms (signed migration bundles) | — | ✅ |
| Compliance posture document | ✅ (`docs/ai-ops-contract.md`) | ✅ + SOC 2 attestation, DPA |

## Insights (consolidated nav, May 1 2026 — renamed from "AI-Hub" May 17 2026 PR 4)

A single Sidebar entry "**Insights**" replaces three previously-scattered entries (Trust, Activity, Reports). The hub holds four subtabs in OSS:

| Subtab | OSS | Plus extension |
|---|:-:|:-:|
| **Activity** — audit + agent-trace + execution timeline (compact KPI strip + 5-col table) | ✅ | ✅ + cross-workspace view, retention controls |
| **Trust** — security posture, "what we never do" pillars, kill-switch instructions | ✅ | ✅ + SOC 2 attestation embedded |
| **Reports** — one-shot installation report (PDF/MD download) | ✅ | ✅ + **custom report builder + scheduler** (pick metrics + grouping + filters → save as template → schedule daily/weekly/monthly → email to distribution list) |
| **AI Provider** — per-user provider config, model browser, OpenRouter free-tier filter | ✅ per-user | ✅ + workspace-wide admin-managed config + monthly_budget_usd + allow_user_override toggle |
| **Governance** (Plus only) | — | ✅ workspace RBAC config, approval policies, dry-run thresholds |
| **Retention** (Plus only) | — | ✅ per-store retention overrides, archive-to-Parquet schedule, S3 sink |
| **Audit Export** (Plus only) | — | ✅ sigstore-signed audit-log export, SIEM-compatible formats |

Legacy `/#trust`, `/#activity`, `/#reports` URLs continue to work — App.tsx routes them through Insights on the matching subtab.

## What this means in practice

**You should choose Revoluzen Flow if** you're a solo builder, a small team prototyping, an evaluator, or you have strong sovereignty requirements (everything runs locally including embeddings). The agent, RAG, the activity timeline, all 43 OSS connectors, schedules, alerts, embedded AI — they all ship for free, forever.

**You should choose Revoluzen Flow Enterprise if** you're shipping pipelines to production and need: enterprise connectors (SAP / NetSuite / etc.), DEV→PROD promotion with approval gates, multi-user collaboration, OIDC/SAML, audit-log retention for compliance, lineage, vault-managed credentials, or 4-hour SLA support. Plus is positioned for *production convenience* — it's not enterprise-only.

**Both editions share the same architecture**: same agent loop, same boundary contract, same trust posture. Plus is additive. Revoluzen Flow never gets worse when Plus exists.

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

**Last reviewed:** 2026-05-04.
**Next mandatory review:** before OSS launch (date flexible per launch posture lock).
**May 3 updates:** corrected connector count (45, not 60+); split notification expansion; split pool page additions; added scaling tier (vertical OSS, horizontal Plus).
**May 4 updates:** corrected tool count (19, not 16) — full list in `docs/product_facts/10_ai_copilot.md`; added the chat fast-lane + Layer 1/2 knowledge architecture rows; added Sprint 1 ETL depth references (SCD2, checkpoint store, bulk-load runner, Postgres + Snowflake dialects); fixed credentials encryption claim — was misrepresented as "always-on" in earlier text but the OSS gate had been Plus-only until the May 4 fix made encryption genuinely always-on (Fernet AES-128-CBC + HMAC-SHA256, master key at `~/.revoluzen-flow/secret.key`); **connector count corrected from 45→43** after audit (the "8 db dialects" was overcounted; honest count is 4 db + 2 bulk-load); **production-grade depth claim removed** — the cert matrix only certifies v2 manifests, and only Salesforce has migrated; the 18/12/7 split was aspirational, not current state. v1→v2 migration is the post-1.0 Tier-1 uplift roadmap.
