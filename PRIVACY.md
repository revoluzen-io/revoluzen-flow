# Privacy Policy — Revoluzen Flow

**Last updated:** 2026-05-29

This document describes what data Revoluzen Flow collects, where it lives, who can access it, and how long it is retained. For the AI-specific data-handling rules see [`docs/ai-boundary-contract.md`](docs/ai-boundary-contract.md). For security reporting see [`security.md`](SECURITY.md).

## Scope

This policy covers **Revoluzen Flow** — the open-source pipeline orchestrator distributed under Apache License 2.0 from this repository. **Revoluzen Flow Enterprise** (the commercial edition) has its own privacy + DPA terms; see https://revoluzen.io/flow/legal.

## Plain-language summary

- **Revoluzen Flow runs entirely on your infrastructure.** It has no phone-home, no analytics SDK, no telemetry hook to any vendor. Set it up on a VM with the network completely off and it still works.
- **No data leaves your install** unless *you* configure something that sends it (a Slack alert webhook, an LLM provider API key, an email recipient).
- **You own your data.** Pipelines, credentials, run history, audit logs, AI traces — all stored in your local database. You can export, back up, or delete everything via the standard backup endpoints.

## Data Revoluzen Flow stores locally

All data is stored on your install's filesystem under `REVOLUZEN_FLOW_DATA_DIR` (default `./data`). Nothing in this list is transmitted to Revoluzen Technologies Pvt Ltd or any third party.

| Category | What's stored | Where |
|---|---|---|
| **User accounts** | Email, name, hashed password (PBKDF2 / Argon2), role, workspace membership, last-login timestamp | `users` table in SQLite |
| **Pipeline definitions** | IR JSON for every workflow version you create, parameter values, schedule configs | `workflow_versions`, `schedules` tables |
| **Connection credentials** | Connection metadata + **encrypted** credential values (Fernet AES-128-CBC + HMAC-SHA256) | `connections`, `credentials` tables + `secrets` vault |
| **Execution history** | Per-run row counts, durations, step-level logs, error messages, lineage edges | `executions`, `execution_logs`, `step_logs` tables |
| **Audit log** | Login events, pipeline publishes, credential changes, AI agent actions — append-only | `audit_log` table |
| **AI traces** | Per-run tool calls, input/output hashes, PII-redaction counts, decision reasons (no raw prompts after redaction window) | `ai_traces` table |
| **Uploaded files** | CSV / Excel / JSON / Parquet you upload via the Storage page | `$REVOLUZEN_FLOW_DATA_DIR/uploads/` directory |
| **Backups** | Snapshot DBs taken on demand or by schedule | `$REVOLUZEN_FLOW_DATA_DIR/backups/` directory |

## Data Revoluzen Flow does NOT collect or transmit

- **No telemetry / analytics / usage tracking.** The OSS build has no analytics SDK, no PostHog, no Mixpanel, no Google Analytics, no homecall.
- **No phone-home.** Revoluzen Flow does not contact Revoluzen Technologies Pvt Ltd or any third-party server during boot, upgrade, or normal operation.
- **No crash reporting.** Errors stay in your local logs unless you wire up your own observability stack.
- **No license check.** Revoluzen Flow does not validate a license against any remote endpoint.

## Outbound network connections Revoluzen Flow makes — and when

Revoluzen Flow only initiates network traffic when an explicit operator action requires it:

| Connection target | Why | When |
|---|---|---|
| Any URL in a `webhook` / `api_source` / `api_sink` node | Pipeline execution touches systems you've configured | Only during pipeline runs you trigger or schedule |
| SMTP server you've configured | Sending alert emails | Only when alerts you've created fire |
| Slack / Teams webhook URLs you've configured | Sending alert notifications | Only when alerts you've created fire |
| LLM provider (Anthropic / OpenAI / OpenRouter / Ollama / Gemini) | AI agent / autoconfig / error diagnosis features | Only when you invoke an AI feature; uses the API keys YOU configure; subject to [`docs/ai-boundary-contract.md`](docs/ai-boundary-contract.md) |
| Vendor-published OpenAPI URLs | Connector authoring (`Insights → Author Connector`) | Only when you paste a URL into the authoring form; subject to SSRF allowlist (see [`backend/revoluzen_flow/connectors/ai_authoring.py`](backend/revoluzen_flow/connectors/ai_authoring.py)) |

If a network connection is critical for your threat model, you can verify in code — every outbound call lives in the modules above, none are hidden inside dependencies.

## Data retention

**Revoluzen Flow keeps your data indefinitely, with one exception.** This
build has no time-based retention policy and no retention settings UI — a
retention scheduler is a Revoluzen Flow Enterprise feature. Everything stays
on your disk until you delete it.

What is deleted automatically:

| What | Behaviour | Control |
|---|---|---|
| Captured step-output **samples** (the example rows shown on a run's steps) | Sample rows are dropped 30 days after capture. Row counts and schema are kept indefinitely, so historical lineage still resolves. | `REVOLUZEN_FLOW_DISABLE_STEP_OUTPUT_PRUNE=1` turns the pruner off |
| AI **agent traces** | **Off by default** — traces are kept forever. Set a number of days and a daily sweep deletes traces older than that. | `REVOLUZEN_FLOW_TRACE_RETENTION_DAYS=90` (any positive number; `0` = keep forever) |

Two stores are bounded **by count, not by age** — the oldest entries are
discarded once the limit is reached:

| What | Default | Control |
|---|---|---|
| Pipeline versions kept per pipeline | 20 | `REVOLUZEN_FLOW_VERSION_RETENTION` |
| Backups kept | 5 | `REVOLUZEN_FLOW_BACKUP_RETENTION` |

Sign-in sessions expire on their own schedule
(`REVOLUZEN_FLOW_SESSION_TTL_DAYS`).

Everything else — execution history, the audit log, token-wallet records —
grows without bound in this build. If you handle regulated data,
treat those stores as in scope for your own retention process; deleting a
project or pipeline in the UI is the supported way to remove its records.

> **Correction (2026-10-28).** Earlier revisions of this page described
> execution-log and audit-log retention settings under
> `Settings → Retention`, a 90-day AI-trace default via
> `REVOLUZEN_FLOW_AI_TRACE_RETENTION_DAYS`, a
> `REVOLUZEN_FLOW_BACKUP_RETENTION_DAYS` variable, and a nightly cleanup job
> enforcing all of it. None of that existed. The settings page, both
> variables and the general cleanup job were never implemented, and the real
> backup limit is a count (`REVOLUZEN_FLOW_BACKUP_RETENTION`), not a number
> of days. AI-trace age-out was genuinely not running — `purge_older_than()`
> existed but nothing called it — and has since been wired up as the opt-in
> `REVOLUZEN_FLOW_TRACE_RETENTION_DAYS` above. The tables describe what the
> build actually does.

## Access control

- **Workspace isolation:** every query in Revoluzen Flow scopes by `workspace_id`. A user in workspace A cannot see resources in workspace B even at the API level (enforced in 30+ store classes).
- **Role-based access control:** roles are Viewer / Developer / Operator / Admin / Platform Admin; per-endpoint authorization runs on every authenticated request.
- **Audit-log read access:** restricted to admins.
- **Credential decryption:** only ever happens server-side, in-process, when a pipeline runs. Plaintext credentials are never sent to the frontend or to the LLM.

## Data export and deletion (operator's responsibility)

Revoluzen Flow provides primitives — operators are responsible for using them under your own privacy regulations (GDPR, CCPA, India's DPDP Act, etc.).

- **Export:** the backup endpoint dumps the entire workspace (pipelines, credentials encrypted, audit log) to a single archive.
- **Per-user deletion:** delete a user account through the admin UI; their owned resources are reassigned per the workspace policy.
- **Full-workspace deletion:** delete the workspace via the admin UI to remove all per-workspace data.
- **Encryption-at-rest reset:** for full data destruction, delete `$REVOLUZEN_FLOW_DATA_DIR` and the master key file (`REVOLUZEN_FLOW_MASTER_KEY_FILE`); credential values become permanently unrecoverable.

## AI data handling

The AI subsystem follows a strict boundary contract that's machine-enforced in CI (10 architecture invariants in `backend/tests/architecture/test_invariants.py`). Highlights:

- **Plaintext credentials, vault values, API keys never sent to LLMs** — enforced by a redaction layer at the AI boundary.
- **PII redaction** (email / phone / credit card / Aadhaar / SSN / IP / API-key patterns) before any LLM request.
- **No-train guarantee** with the cloud providers Revoluzen Flow integrates with (Anthropic zero-retention, OpenAI `store:false` + account opt-out, Azure tenant-bound).
- **Ollama is local** — never leaves the host.

Full details: [`docs/ai-boundary-contract.md`](docs/ai-boundary-contract.md).

## Changes to this policy

This policy is versioned in the repo. Material changes are listed in [`changelog.md`](CHANGELOG.md) under the relevant release. The `Last updated` header at the top reflects the date of the most recent revision.

## Questions

- **General questions:** open a GitHub Discussion at https://github.com/revoluzen-io/revoluzen-flow/discussions
- **Privacy concerns about Revoluzen Flow Enterprise (commercial):** email `info@revoluzen.io`
- **Security vulnerability reports:** see [`security.md`](SECURITY.md) — do **not** open public issues
