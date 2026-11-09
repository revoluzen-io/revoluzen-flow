# Revoluzen Flow audit, lineage, and governance

Per `edition-matrix.md` line 137-144 (audit, governance, compliance) and line 96-107 (agent observability).

## Three audit surfaces in OSS Free

### 1. Audit events and pipeline version history

This edition has no `audit_log` table and no audit-log page. Some actions — deleting a pipeline, restoring one from the Bin or removing it from the Bin for good, an uploaded file or a managed table, renaming a table, cleaning up storage, testing a connection, starting a backfill, creating or revoking an endpoint API key, publishing, changing or removing an endpoint — call `revoluzen_flow.audit.audit_action`, which writes one `AUDIT:` line to the `revoluzen_flow.audit` logger at INFO level. The default server setup does not print INFO lines from the app, so keeping them needs Python logging configured for that logger.

For "who changed this pipeline?", use the pipeline's **Version history** (Pipelines → the pipeline's **⋯** menu → Version history): every save that changes a pipeline adds a version with who saved it and when, and **Compare** shows what it added or removed.

**Not here:** Insights → Activity lists AI requests (see 2), not audit events.

### 2. Agent trace store — every Copilot run

Per `edition-matrix.md` line 103: "Trace store (every run persisted with replay-safe step records) ✅ ✅".

Every agent run captures:
- `run_id` (UUID)
- `user_intent` (the prompt)
- `outcome` (success / llm_failure / tool_failure / policy_block / timeout / user_rejection)
- Per-step: `tool_name`, input/output **hashes only** (never raw values per AI_BOUNDARY_CONTRACT §2), latency, tokens in/out, redactions count
- Total tokens + estimated cost

**Where to view:** Insights → Activity: one row per AI request with the model, tokens, estimated cost and the result; click a row for its steps. Besides Copilot runs, the same store keeps the AI calls other features make (drafting a pipeline in the Editor, SQL help, run and notification summaries), each marked with where it came from.

**API:** `GET /api/v1/activity?scope=ai` (the Activity page's feed, with totals for the window), `GET /api/ai/agent/traces?limit=20`, `GET /api/ai/agent/trace/{run_id}` for the full detail.

**OSS retention:** kept in SQLite with no time limit by default. Set `REVOLUZEN_FLOW_TRACE_RETENTION_DAYS` and a daily job deletes older traces.

### 3. Execution log — every pipeline run

Per `04_concepts.md` Execution section. Each run captures status, duration_ms, peak_memory_mb, cpu_seconds, parameter_values, workflow_snapshot, exit_reason. Immutable once written.

**Where to view:** Executions page. Click a row for the metadata.

## Revoluzen Flow Enterprise adds

### Audit log retention + export + sigstore signing

- **Retention policy** — operator sets per-store retention (e.g. "keep audit_log for 7 years for SOC 2"). Auto-archives to Parquet on S3/GCS before deletion.
- **Export** — `POST /api/plus/audit/export` returns a sigstore-signed tarball. SIEM-compatible formats (JSON Lines, CSV).
- **Sigstore signing** — every export tarball is signed via a Revoluzen-issued sigstore certificate. Auditors can verify integrity without trusting the Revoluzen API.

### Trace store extended retention (1 year+)

Same trace data as OSS, but retained 1 year+ with admin-controlled retention overrides per store.

### Lineage (Marquez-compatible)

Cross-pipeline dataset provenance graph. For each pipeline run, Revoluzen Flow Enterprise emits an OpenLineage event to a configured backend (default: a built-in Marquez instance). Lets you answer "where did the rows in this dashboard come from?" by walking upstream.

**Compatible with:** Marquez, DataHub, OpenMetadata, any OpenLineage-compliant backend.

### Vault (AES-256, references, rotation)

Per `edition-matrix.md` line 142. Plus replaces local-encrypted credentials with **Vault-Ref** — credentials live in HashiCorp Vault / AWS Secrets Manager / Azure Key Vault / Google Secret Manager. Revoluzen Flow Enterprise stores only a reference (`vault:secret/data/foo`) that resolves at runtime.

Plus also adds **rotation hooks** — Revoluzen Flow Enterprise subscribes to vault rotation events and updates running pipelines without restart.

### Export to other platforms

Revoluzen Flow Enterprise can export pipelines as **signed migration bundles** via a documented bundle format.

### Compliance posture document

OSS ships `docs/ai-ops-contract.md` and `docs/compliance.md` (the compliance one-pager). Plus adds a Data Processing Agreement (DPA) suitable for procurement reviews. Revoluzen has not yet completed a SOC 2 audit; a Revoluzen SOC 2 Type II report for Enterprise customers is planned — ask sales for current status before relying on it.

## Anti-patterns

- ❌ Telling an OSS user "Revoluzen Flow is SOC 2 certified" — the SOFTWARE isn't certified; the OPERATOR's deployment may bring Revoluzen Flow into scope of the operator's own SOC 2. Equally, never promise a Revoluzen SOC 2 report: no audit has been completed yet.
- ❌ "Use Insights → Activity as your audit log" — it lists AI requests only (what the AI did and what it cost). For the agent, open a request there or use the trace store API; for changes to a pipeline, its Version history; for pipeline runs, the Executions page.
- ❌ Promising lineage in OSS — it's Plus only. The OSS path for "where did this data come from" is `recall_history` against the RAG index plus `summarize_pipeline` on each upstream node.
- ❌ Confusing audit log with execution log — they are different stores. Audit log = who did what (auth-scoped). Execution log = pipeline runs (system-scoped).

## Where to look for evidence

| Question | Where (OSS) | Plus extension |
| --- | --- | --- |
| Who changed pipeline X? | Pipelines → ⋯ → Version history | + audit log with sigstore-signed export |
| What did the agent do at 14:32? | Insights → Activity, or `GET /api/ai/agent/traces?limit=20` | + 1-year retention |
| Why did this run OOM? | Executions page → run row → `peak_memory_mb` | (same — retention extended) |
| Where did this row come from? | `recall_history` + `summarize_pipeline` | + Marquez lineage graph |
| Are credentials encrypted? | `GET /api/trust/posture` → `security_baseline.credential_encryption.status = ok` | + Vault-Ref pattern |
