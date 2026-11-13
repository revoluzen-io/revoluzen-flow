# Revoluzen Flow AI Copilot — what's in OSS, what's Plus

Per `edition-matrix.md` line 89-107, the agentic Copilot ships in OSS. Plus adds organizational guardrails.

## OSS Free includes

### The dock + chat

**Copilot dock** — resizable, page-aware floating chat panel. Slash commands (`/help`, `/explain`, `/diagnose`). Lives bottom-right, collapsible to a small button.

**Three-layer knowledge architecture** (May 4 2026):
- **Layer 1 — session context block** — every turn carries who the user is, role, environment, edition, current page, workspace counts, what they can/can't do. Cap ~600 tokens; never dropped under budget pressure.
- **Layer 2 — product knowledge RAG** — curated `docs/product_facts/*.md` chunks retrieved per turn. Auto-indexed at startup; admin reindex via `/api/ai/product-knowledge/reindex`.
- **Layer 3 — live workspace state** — the agent's tools (see below) fetch real workspace data when the LLM needs it.

### The fast lane (sub-1 s answers without LLM)

11+ rule-based intents that bypass the LLM entirely for the most-common questions:

- **Greetings** — "hi" / "hello"
- **Help** — "what can you do" / "help"
- **Product info** — "what is revoluzen-flow" / "what tier am I on" / "what's my role"
- **Workspace state** — "give me an overview" / "list pipelines" / "list projects" / "list schedules" / "list alerts" / "list connections"
- **Recent activity** — "what failed today" / "what's running now"
- **Catalog** — "what node types are supported"

The fast lane uses a reasoning gate — prompts containing "why", "explain", "compare", "diagnose", "should I", "walk me through" fall through to the LLM. So "list pipelines" is instant; "why did pipeline X fail" goes to the LLM.

### Asking before drafting

Asked to build a pipeline, the Copilot first works out where the data comes from and where it goes. Whatever the request left open, it asks on a card, offering this workspace's real connections, uploaded files and managed tables as one-click choices. For example, "load orders into the warehouse" asks which connection holds the warehouse. A connection that doesn't exist yet can be created in the chat, with the same form as the Connections page; what you type there goes straight to the connection store and never reaches the model or the transcript. Only ids and names go to the planner.

### The agent loop

Bounded tool-use loop with hard caps:
- **MAX_ITERATIONS = 6** — hard ceiling on tool-call cycles per run.
- **Wall-clock cap** — 300 s for local Ollama, 120 s for cloud providers (provider-aware since May 4 2026). Override via `REVOLUZEN_FLOW_AGENT_WALL_CLOCK_S`, clamped [10, 600].
- **Stop button** — user-cancellable mid-flight.

### The agent tools (canonical count: code in `backend/revoluzen_flow/ai/tools/`)

The tool count derived from the codebase is **37 tools** organised in three tiers (plus 2 opt-in web tools, registered only when the operator enables web access). The SOURCE OF TRUTH is `INITIAL_TOOLS` in `backend/revoluzen_flow/ai/tools/__init__.py` — the counts below went stale twice by being retyped, so check the code before quoting them. The breakdown:

**29 READ tier** (permissive RBAC, no confirmation, standard audit):

- `get_user_role` — caller's role + env + tier
- `get_workspace_overview` — top-level workspace counts
- `list_pipelines` — pipelines + status + step count
- `list_projects` — projects in the workspace
- `list_schedules` — schedules + cadence + enabled flag
- `list_alerts` — alert rules + channel + condition
- `list_executions` — recent runs with status filter
- `list_storage` — files, managed tables and pipeline outputs
- `inspect_connections` — connection metadata (NEVER credential values)
- `list_catalog` — available node types + connectors
- `list_templates` — built-in starters + user-saved templates
- `summarize_pipeline` — plain-English summary of a pipeline
- `get_pipeline_spec` — what a pipeline was asked to do: its goal, business owner, consumers, sources, acceptance criteria (and whether the contract or a person checks each), expected outcome, and the last contract verdict on the current version. Added 2026-11-11 so a repair keeps the criteria the pipeline was built to
- `validate_pipeline` — validation errors + warnings for a pipeline, or for a draft when given a `draft_id`. Two passes: the IR validator Publish and Run gate on, plus a column check that catches a step referring to a column its input does not have. Reports `columns_checked: false` when it could not read the sources, rather than implying a clean bill
- `explain_step` — what one step does, how it is configured, and every option that step type accepts with its choices, default and meaning. Added 2026-11-03 after three recorded model runs fixed a renamed join column by writing the new name into `join_key`, when the step needed `key_mode: mapped_keys`
- `inspect_step_columns` — the columns a step receives, as names and types only, plus what the parameters of the repairable step types mean (`ai/step_params.py`). Runs a one-row preview of upstream steps, and refuses if any of them could have a side effect
- `test_pipeline_draft` — runs a draft edit, reading up to 1,000 rows and discarding them, and reports each step's status, columns and row count, plus the first error and a warning for a plain `CAST`. A value that only fails when rows are read counts as a failure. A pass means the draft runs, not that it is right. Destinations never run, nothing is saved, and no row values are returned. Added 2026-11-03 so the agent can find the SECOND fault in a pipeline: the engine error names only the first, and without this no recorded model ever completed a two-fault repair
- `query_metrics` — execution metrics aggregation
- `recall_history` — RAG search across executions, definitions, catalog, docs
- `lookup_help_topic` — structured product-knowledge atlas lookup
- `get_running_executions` — pipelines running right now
- `get_next_scheduled` — upcoming schedule fires
- `get_installation_health` — installation health score + prioritised punch list (inline credentials, undeployed pipelines, top failing pipelines, 24h success rate, headline totals) in one call
- `get_security_posture` — the Settings → Security & Diagnostics baseline for THIS install: each control's status, the scope of what was actually tested, and for failures the exact fix. Added 2026-10-26; before it, security questions were answered from the model's general knowledge rather than from the install.
- `list_steward_findings` — Steward advisories (duplicates, repeated logic, governance, connector health)
- `suggest_join_keys` — for a Join step, the column pairs whose values overlap in both inputs, ranked with names and types; returns counts and column names, never values. Added 2026-11-11
- `get_incidents` — the pipelines failing now (open incidents), the groups of them that share one cause (the same connection or the same error), and for one incident: the failed step, the kind of failure, when it last worked, the versions saved since, and what the auto-recover policy tried. Added 2026-11-11
- `list_quality_rule_sets` — the workspace's shared quality rule sets (named Data Quality rules many pipelines use) with the columns each checks, so a draft picks a set instead of retyping its rules. Added 2026-11-11
- `lookup_business_terms` — the workspace's business glossary (its own terms and metrics, with definitions, other names and SQL). The terms a request mentions are also put in front of the Copilot and every pipeline draft without a call. Added 2026-11-11

**7 SAFE_WRITE tier** (standard RBAC, inline preview, idempotency cache):

- `compose_report` — drafts a markdown/PDF report (no live data mutation)
- `draft_pipeline_from_intent` — turns plain English into a pipeline IR draft
- `draft_alert_rule` — turns "alert me if X" into an alert rule draft
- `modify_pipeline_step` — drafts an edit to an existing pipeline. Nothing is saved until the draft is applied. Edits made for one request build on each other; pass `draft_id` to continue a draft from an earlier request, or `from_saved` to start over. Four kinds of edit are refused in code rather than by prompt, each because a recorded model run showed the prompt alone did not hold (`backend/revoluzen_flow/ai/tools/_edit_guards.py`, `backend/revoluzen_flow/ai/draft_guess.py`):
  - a guess: replacing a missing column when nothing the step receives resembles it, when several columns do, or when the one match is not used, and repointing a source at something nobody named. The person settles it by saying which one in their own message
  - deleting or loosening a `validate` / `data_quality` / `fail` step, or inserting a row-removing step in front of one, unless the **person's own message** asked for it. Text inside the pipeline, including a step's label, is never the person asking. The attempt is still recorded, so a model that reaches for it is not scored the same as one that did not
  - an argument left as a placeholder, such as `<new-condition>`
  - a join key that cannot work: a `join_key` naming a column only one side has (`same_key` compiles to `left."k" = right."k"`, so this is a run-time Binder Error naming an internal view), or `key_pairs` whose entries do not use the field names `left` and `right`. Each refusal lists the columns that side really has. The second one matters because the node used to read unusable pairs as an `ON TRUE` cross join — a wrong answer with no error — which it now rejects outright
- `draft_connector_from_openapi` — drafts a connector from a spec or URL
- `draft_connector_from_samples` — drafts a connector from 1–5 sample responses
- `test_connection` — tests a saved connection (no secrets reach the LLM)

**Copilot roles** (2026-11-11, `ai/roles.py`): the tools fall into four roles that an admin switches on or off per workspace in **Settings → Copilot roles**: **Discover** (read connections, catalog, pipelines, storage, columns, specs, rule sets, the glossary; suggest join keys), **Build** (draft and change pipelines, connectors, alert rules, reports), **Test** (validate a draft, test-run it, test a connection) and **Operate** (runs, schedules, incidents, health, findings; start or stop a run). All are on by default. A role that is off removes its tools, including the quick run, stop and test actions, and the Copilot is told which roles are off so it says so. Help, the workspace overview, history recall and the user's own role always stay. Roles never let the Copilot do more than the person's own rank allows.

**Not every tool is offered on every turn.** `ai/tool_selector.py` narrows
the candidate set to 8 (local models) or 14 (cloud) per request, because
each schema costs ~80 tokens on every loop iteration and small models
mis-route as the set grows. Selection combines a floor, the current page,
and prompt keywords — so a tool that is registered but absent from both
`_PAGE_TOOLS` and `_KEYWORD_TOOLS` can be invisible to the model in
practice. Registering a tool is only half of shipping it.

**1 HIGH_IMPACT_WRITE tier** (strict RBAC, required confirmation card, dry-run by default for first 3 successful runs):

- `apply_pipeline_draft` — promotes a draft into the workspace

All three tiers are workspace-scoped + sanitization-gated + audit-logged. The dry-run-by-default behaviour means the first 3 invocations of any new HIGH_IMPACT_WRITE tool execute in dry-run mode regardless of what the user clicked, until the operator has seen 3 successful runs and the tool unlocks live mode.

### Governance (always-on, OSS + Plus)

- **Sanitization gateway** — PII / credentials / API keys / connection strings stripped before LLM sees data
- **Idempotency cache** — write tools key on (tool_name + args + tenant); duplicate calls within TTL replay the cached result instead of re-executing
- **Dry-run-by-default** — new HIGH_IMPACT_WRITE tools force dry-run for the first 3 successful runs before unlocking live mode
- **Confirmation card** — every write surfaces a before/after preview the user must approve
- **Trace store** — every run persisted with replay-safe step records (input/output hashes, never raw values)
- **AI activity** (Insights → Activity) — each AI request, from the Copilot or from another feature (drafting a pipeline in the Editor, SQL help, run and notification summaries), with the model, tokens, estimated cost and whether it failed. Pipeline runs are on Executions, not here
- **Prompt signing** — HMAC system prompt integrity check, refuses tampered prompts at module-load time
- **Tool-tier RBAC** — 4 roles × 2 envs × 3 tiers gating who can call which tier
- **Wallet caps** — per-user daily token cap, per-request cap, max-iterations cap, wall-clock cap. Layered defense against runaway loops + denial-of-wallet.

### Inline AI helpers (separate from the agent)

These are AI-augmented UI moments that don't go through the agent loop:

- **SQL helper** — Transform node has an "Ask AI" button that translates plain English to a DuckDB query
- **Pre-publish card** — pipeline detail surfaces "this pipeline looks risky because…" before deploy
- **Diagnose error** — failed run row shows a "Why?" button that explains the error
- **Post-run summary** — successful run shows "you processed 12,400 rows in 2.4s; here's what changed"
- **Cost estimate** — pipeline list surfaces an estimated $/run before triggering
- **Anomaly detect** — execution history flags outlier runs (memory, duration, row count)
- **Pre-run validate** — before kicking a pipeline, AI sanity-checks node configs

### Provider config

- **9 cloud providers supported** as opt-in escape hatches: Anthropic, OpenAI, OpenRouter, Gemini, DeepSeek, Groq, Mistral, Azure, Custom (OpenAI-compatible)
- **Local default** — Ollama with `qwen2.5:7b` (4.7 GB on disk, ~6 GB RAM at Q4_K_M, 30–60 s/turn on CPU; the 2026-05-19 tool-use floor)
- **OpenRouter model browser** — free-tier filter, tools-only filter, browse 100+ models from one dashboard
- **Token wallet caps** — per-user daily, env-tunable
- **Audit log** — every LLM call (provider, model, tokens, latency, success/error)
- **Provider price comparison** — `GET /api/ai/providers/compare` returns live pricing for budgeting

### RAG layer

- **Local embeddings** via Ollama `nomic-embed-text` (768-dim)
- **sqlite-vec** workspace-scoped vector store
- **`recall_history` tool** — searches execution failures, pipeline definitions, catalog, docs
- **Daily 03:00 UTC indexer** — re-indexes failures (last 30 days), pipelines, catalog, docs
- **`/api/ai/rag/reindex`** — on-demand reindex endpoint (admin-only)

## Revoluzen Flow Enterprise adds

### Org-level governance

- **Llama-Guard / safety classifier** — runs on every agent turn; blocks unsafe outputs
- **Cross-session conversational memory** — managed context that persists across sessions, deletable
- **Workspace-wide AI provider config** — admin-managed, with `monthly_budget_usd` workspace cap and `allow_user_override` toggle
- **Workspace-wide token wallet caps** — alongside per-user
- **Audit log retention + export** — extended retention for the LLM call audit log
- **Trace store extended retention** — 1-year+ with export

### RAG enhancements

- **Proactive trend detection** — "this pipeline fails every Monday" surfaces unprompted
- **Cross-workspace federated retrieval** — search across multiple workspaces (only for users with access)

## Anti-patterns

- ❌ Suggesting fine-tuning to a user — Revoluzen Flow uses prompt-injection of context (Layers 1+2+3), not fine-tuning. The model isn't trained on Revoluzen Flow-specific data; it has the docs RAG'd in.
- ❌ Telling a Free user "set up Llama-Guard" — Plus only.
- ❌ Promising "the agent remembers your previous chats" — Free has no cross-session memory; each chat session is independent.
- ❌ "Just paste your AWS credentials in" — never. The agent's tools never accept raw credentials. Credentials go through the Credentials page.
