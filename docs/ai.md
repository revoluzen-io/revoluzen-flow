# AI guide

Revoluzen Flow has an embedded AI agent (the **Copilot**) and inline AI helpers throughout the editor. Both work with local LLMs (Ollama) or cloud providers — your choice.

## Two surfaces

### Inline helpers

Specific UI moments where the agent augments a single action:

- **SQL helper** — describe what you want, get a working SQL transform
- **Pre-publish card** — checks your pipeline against best practices before deploy
- **Diagnose error** — given a failure, suggests root cause + fix
- **Post-run summary** — natural-language description of what just ran
- **Cost estimate** — predicts row count + duration + LLM tokens for the next run
- **Anomaly detect** — flags unusual rows in output
- **Pre-run validate** — schema + connection sanity checks

### Copilot dock

The floating chat panel in the bottom-right corner. Opens to a full conversation with 37 tools the agent can call (per `backend/revoluzen_flow/ai/tools/`):

| Read tools (24) | Draft / Write tools (8) |
|---|---|
| `get_user_role`, `get_workspace_overview`, `list_pipelines`, `list_projects`, `list_schedules`, `list_alerts`, `list_executions`, `list_storage`, `inspect_connections`, `list_catalog`, `list_templates`, `summarize_pipeline`, `validate_pipeline`, `explain_step`, `inspect_step_columns`, `test_pipeline_draft`, `query_metrics`, `recall_history`, `lookup_help_topic`, `get_running_executions`, `get_next_scheduled`, `get_installation_health`, `get_security_posture`, `list_steward_findings` | `draft_pipeline_from_intent`, `apply_pipeline_draft` (always confirms), `modify_pipeline_step`, `draft_alert_rule`, `compose_report`, `draft_connector_from_openapi`, `draft_connector_from_samples`, `test_connection` |

Asked to build a pipeline, the Copilot first asks where the data comes from and where it goes, offering your real connections, uploaded files and managed tables. A connection you don't have yet can be created in the chat.

Slash commands: `/fix`, `/explain`, `/optimize`, `/test`. Type `/` to see them.

## Local LLM setup (recommended)

The May 3 2026 lock makes **local-only LLM the Revoluzen Flow default** — your data never leaves your machine.

### 1. Install Ollama

[ollama.com/download](https://ollama.com/download) — one-click installer for macOS, Linux, Windows.

### 2. Pull a recommended model

| Model | Size | RAM | Best for |
|---|---|---|---|
| **`qwen2.5:7b`** | 4.7 GB | 8 GB | **CPU pick (floor)** — reliable tool-use, ~30–60 s per turn on CPU |
| `llama3.1:8b` | 4.9 GB | 8 GB | Alternative at the floor — strong tool-use |
| `phi-4` | 9.0 GB | 12 GB | 14B equivalent perf; needs more RAM |
| `mistral-nemo` | 7.1 GB | 10 GB | Tool-capable, 12B params, GPU recommended |
| `qwen2.5:1.5b` / `qwen2.5:3b` | 0.9 / 1.9 GB | 2 / 3 GB | **Below tool-use floor** — advertises tool support but returns greetings instead of calling tools. Not recommended. |
| `phi3` | 2.3 GB | 4 GB | Smallest, **text-only** (no agent tool support) |

> **Floor revision (2026-05-19):** the older `qwen2.5:1.5b` / `:3b` picks were demoted after they failed to drive the agent's tool-use loop reliably — the model would silently reply with greetings or empty text instead of calling a tool. The reliable floor is now ~7B (~6 GB RAM at Q4_K_M).

```bash
ollama pull qwen2.5:7b
```

You can also pull from inside Revoluzen Flow: open **Insights → AI Provider** and click **Pull** next to the recommended model. Streams progress inline. The first-launch banner also catches you if you're on a sub-floor model and offers a one-click upgrade.

### 3. Verify

The Copilot's status banner should show `ollama / qwen2.5:7b · 37 tools · 0% of daily cap`. Type "give me a quick overview of my workspace" — expect a response in 30–60 seconds on CPU at the floor.

If it times out, you're likely on a model heavier than 7B on a CPU-only host. Switch back to `qwen2.5:7b` or pick a cloud provider for faster responses.

## Cloud providers

Open **Insights → AI Provider** to configure:

- **Anthropic** (Claude Haiku / Sonnet) — recommended cloud default
- **OpenAI** (GPT-4o, GPT-4o-mini) — function-calling support
- **OpenRouter** (100+ models behind one API key) — try multiple providers cheaply
- **Google Gemini** (Gemini 2.0 Flash) — fast, strong reasoning
- **DeepSeek**, **Groq**, **Mistral**, **Azure OpenAI**, **Custom** (any OpenAI-compatible endpoint)

Your API key is **stored encrypted on this machine** (Fernet — AES-128-CBC + HMAC-SHA256, per `backend/revoluzen_flow/security/encryptor.py`). It is never sent to any service except the provider you chose.

The provider price comparison table on the AI Provider page shows live per-token rates from OpenRouter.

## Business glossary

**Insights → Glossary** (`#glossary`, `/api/glossary`) holds the workspace's own words: a **term** such as *active customer* or a **metric** such as *net revenue*, each with a definition in plain words, the other names people use for it, and optionally the SQL a person wrote for it (a metric's expression, a term's condition), where it lives and who owns it. Adding and changing entries needs developer rank.

When a request to the Copilot, or any pipeline draft (the Editor's or the Copilot's), uses one of these words or another name for it (spaces, `_` or `-` between the words, and plurals, all count), the matching entries go with the request, and the model is told to use them rather than a meaning of its own, and to say so when one does not fit the data. Only the entries a request mentions are sent, at most ten. The Copilot can also look entries up with `lookup_business_terms`. Entries are written by people; no row value is sent.

## Measuring request-to-pipeline accuracy

`python -m revoluzen_flow.eval.intent` scores how often a request in plain words becomes a pipeline that returns the right rows (`backend/revoluzen_flow/eval/intent/`). Each case is a request ("load orders.csv, keep only paid orders, total the amount by customer"), a few small input files, and the rows a correct pipeline returns, worked out by hand. The draft is run on those files in preview mode, nothing is written, and its rows are compared with the expected ones in any order, numbers within a small tolerance, and a computed column found by its values when the request does not name it. Each case reports drafted, valid, ran and correct; **accuracy** is the share that are correct.

- `--drafter rule` scores the rule planner, which needs no model. Every release runs it and stops below a floor (`release.yml`, job `intent-eval`); today it gets 8 of the 10 cases right (it cannot yet sort and keep the top rows, or average by a named column). `test_intent_eval.py` pins which cases it gets right, so a change that alters them fails on every pull request.
- `--drafter live` drafts through the configured provider, the same planner call the Editor and the Copilot use. It costs money, so it runs on demand (`intent-eval-live.yml`), and `--record` keeps each draft.
- `--drafter replay --replay FILE` scores recorded drafts again with no model. Recordings committed under `eval/intent/recorded/` are re-scored on every release, which catches a later change that breaks what a model drafted.

## Governance & safety

OSS Free is single-user, so role-based gating doesn't apply — everything below runs against the bootstrap user. The protections are still active:

- **Tool tiering** — every agent tool is classified `read` / `safe_write` / `high_impact_write`. Confirmation cards + dry-run-by-default attach to the write tiers regardless of who's logged in.
- **Sanitization gateway** — PII and credentials are redacted before any payload reaches the LLM (per the [AI boundary contract](ai-boundary-contract.md)).
- **Idempotency cache** — write tools never duplicate side effects on retry.
- **Dry-run-by-default** — the first 3 runs of a new write tool are forced to dry-run mode.
- **Confirmation card** — every write tool shows what it'll do and waits for explicit confirmation.
- **Trace store** — every agent run is persisted with replay-safe step records (open the **History** toggle in the Copilot to browse).
- **Wallet caps** — daily token cap (default 100K user / 1M workspace) prevents denial-of-wallet attacks.
- **Prompt signing** — system prompt is HMAC-signed; the agent refuses to run if it's been tampered with.
- **Copilot roles** (2026-11-11) — the Copilot's tools fall into four roles, each switched on or off per workspace by an admin in **Settings → Copilot roles** (`GET/PUT /api/v1/ai/roles`): **Discover** (read connections, the catalog, pipelines, storage, a step's columns, specs and rule sets; suggest join keys), **Build** (draft and change pipelines, connectors, alert rules and reports), **Test** (validate a draft, test-run it on sample rows, test a connection) and **Operate** (runs, schedules, incidents, health and findings; start or stop a run). All are on by default. A role that is off takes its tools out of the Copilot's hands in that workspace, including the quick actions that run, stop or test something, and the Copilot is told which roles are off so it says so when asked. Help, the workspace overview, history recall and the user's own role stay available. Roles only narrow what the Copilot may do: what it does for a person is still limited by that person's own rank.

Revoluzen Flow Enterprise adds: **4-role agent RBAC** (`viewer` / `developer` / `admin` / `super_admin`) so different team members see different tool tiers, **Llama-Guard safety classifier** on every turn, **cross-session conversational memory**, **proactive trend detection**, and **federated retrieval across workspaces**.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Agent times out at ~300 s on local | Model heavier than the floor on CPU | Stay at `qwen2.5:7b`, or move to cloud |
| Copilot returns greetings instead of acting | Model below the tool-use floor (e.g. `qwen2.5:1.5b`, `:3b`) | Pull `qwen2.5:7b` from the banner |
| "Text-only" badge on chat | Model isn't tool-trained (e.g. `phi3`, `mistral`, `codellama`) | Pull `qwen2.5:7b` or `llama3.1:8b` |
| "0 tokens" on every response | API key invalid or rate-limited | Check the AI Provider page; rotate the key |
| Tools never get called | Model is text-only; agent runs in chat-only mode | Use a tool-capable model |
| `Reached max iterations` | Agent looped on a tool call | Refresh chat; report the trace via Copilot **History** |

## See also

- [Trust posture](trust.md) — what we do and don't do with your data
- [AI boundary contract](ai-boundary-contract.md) — the 10 architecture invariants the agent code is required to satisfy
- [Performance budgets](performance.md) — latency budgets per tool
