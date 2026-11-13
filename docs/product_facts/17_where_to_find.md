# Where do I find…? — Revoluzen Flow navigation reference

UI navigation Q&A — every entry is a self-contained "where is X" question
with the exact path. The Copilot retrieves these chunks when asked about
locating any feature.

**Convention:** primary path first, alternatives second. Keyboard
shortcuts called out where they exist.

---

## Where is the Dashboard?

**Sidebar → first entry** (the home icon). URL hash: `#dashboard`. Also clickable from the Revoluzen Flow logo at the top of the sidebar. The Dashboard is the home / landing page — greeting + date/time, hero KPIs (4 gradient cards, env-specific), workspace inventory or operations cards, system usage (CPU, memory, threads, throughput, DB size, uptime), admin-only cards (seats, users, workspaces, role) when isAdmin, a chart + donut, and three recent-activity feed tables. DEV and PROD render different KPIs and different feeds.

## Where is the Pipelines page?

**Sidebar** → **Pipelines** (clipboard / list icon). Keyboard: `g` then `p`. URL hash: `#pipelines`.

This is the per-pipeline list — every pipeline in the workspace with its status badge, step count, run button, and editor entry-point. **Distinct from the Dashboard**, which is the home / overview page above.

## Where do I see my projects?

**Sidebar** → **Pipelines**. The project panel on the left of the list shows every project with its folders and how many pipelines each holds. Click a project or a folder to list only its pipelines; **All projects** lists every pipeline, and **Unassigned** lists pipelines whose project you cannot see or that no longer exists. The address keeps the choice: `#pipelines?project=<id>`, plus `&folder=<id>` for a folder. On a narrow screen the panel opens from the **Projects** button above the list. There is no separate Projects page any more; an old `#projects` link opens this panel. Each project is a collection of pipelines sharing a logical purpose; pipelines belong to exactly one project.

## Where are the pipelines I have open?

In two sets of tabs, up to ten each. On the Pipelines page, tabs above the list, beside the project panel: first **Home** (the list itself), then each pipeline opened from the list, shown in full (preview, last run, and Publish, Run, Version history, Duplicate, Edit in Editor); address `#pipelines/<id>`; **Open** at their right end picks another. In the Editor, tabs above the canvas, beside the Nodes panel, hold only the pipelines being built there, each keeping its canvas, test results and unsaved edits; address `#editor/<id>`; at their right end, **Open** brings in another saved pipeline and **New Pipeline** opens an empty canvas in a new tab. Looking at a pipeline on the Pipelines page does not add it to the Editor's tabs; **Edit** on its row or **Edit in Editor** does.

## Where are my schedules?

**Pipelines page** → the pipeline's row, and the **Schedule** button in the Editor toolbar. There is no separate Schedules page. Active schedules also appear on the Dashboard.

## Where do I pause a pipeline?

**Pipelines page** → the row's **⋯** menu → **Deactivate**. Its schedules are skipped and its endpoints answer `423` until you click **Activate**.

## Where do I let another application start a pipeline?

**Workflows** → **Triggers** tab. URL hash: `#triggers`. Create an endpoint for a saved pipeline and an API key there, then copy the ready-to-use command. On the Pipelines page, a row's **⋯** menu → **Run from another app** opens the same page with that pipeline chosen.

## Where are my alerts / notifications?

- **Alert RULES**: configured per-pipeline. Open the pipeline → **Alerts** tab in the editor.
- **Notification HISTORY** (the bell): top-right header → **🔔 bell icon** for the last 20; full history at **#notifications**.
- **Notification CHANNELS** (email SMTP, Slack webhook, etc.): Settings → **Notifications** tab.

## Where are my reports? Where do I see if a scheduled report went out?

**Insights → Reports**. URL hash: `#reports`. The list shows each saved report's schedule, next send and last result. Open one and choose **Sent history** for every send, who it went to, and the reason for anything that failed. The installation inventory document is under **Start from** on the same page.

## Where is Storage / where do I find my files and managed tables?

**Sidebar** → **Storage** (stacked-cylinders icon, between Pool and Insights). URL hash: `#storage`. Three sub-tabs:

* **Files** — uploaded files (CSV / JSON / Parquet / Excel / XML). Each row shows scope (Global vs project), size, "Used by N pipelines" pill, last-updated, and row actions (Preview, Replace bytes, Promote to managed table, Delete).
* **Managed Tables** — Parquet-backed tables addressable by `schema.name` from Managed Table Source / Managed Table Sink nodes. Each row shows rows / columns / size / part count / "Used by N" / Drop, and under the name **Prepared by**: the pipeline that writes the table and the version of it the table holds (click the name to edit it). A table's **History** tab lists its last loads. Its **Tests** tab holds its checks and **Run these tests on their own**: after every load and/or every 15 minutes to a day; a failed test lands in the Steward and the admins' notifications.
* **Pipeline Outputs** — files written by pipeline runs, grouped by `(pipeline_id, run_id)`. A **Pipeline Output** node keeps one file here per pipeline and name, replaced on every run.

The Storage page is workspace-scoped; the active project context (the project picked in the project panel on the Pipelines page) auto-filters via the All / Global / Project chip strip.

## Where do I upload a data file?

**Storage page** → **+ Upload file** button (top right). Opens a dialog asking:

1. **Scope** — Global (visible to every project) or Project (scoped to one).
2. **Project** picker — appears when Project scope is chosen.
3. **Folder** picker — optional sub-folder within the project.
4. **Description** — optional note.
5. **File** — drag-and-drop or click-to-browse.

Files land under `$REVOLUZEN_FLOW_DATA_DIR/uploads/{workspace_id}/...` and are indexed in `storage_objects` so the Files tab can show them. Allowed formats: CSV, TSV, JSON, NDJSON, Parquet, Excel, XML.

## Where do I see what pipelines use a file or managed table?

**Storage page** → **Files** or **Managed Tables** tab. The **"Used by"** column shows a "Used by N" pill on each row. Click it to open a popover listing every referencing pipeline with name + "Open in Editor" link.

Backend scanner walks all workflows for the workspace and detects four reference shapes: `local_table_source` / `local_table_sink`, generic `source`/`destination` with `connector_type='local_table'`, file-path matches against `storage_object.path`, and promote-to-table provenance (a file that seeded a managed table inherits the table's pipeline list).

Destructive actions (Delete, Drop, Replace) warn before proceeding when usage exists.

## Where do I manage credentials?

**Sidebar** → **Connections** → **Credentials**. URL: `#credentials`. Workspace-scoped list.

For project-scoped credentials: open the project → **Credentials** tab.

## Where do I turn credential expiry warnings on or off?

**Settings** → **Notifications** tab → **Automatic Alerts** → **Credential expiry warnings**. On by default. A credential is warned about only if it has an expiry date, which you set on the Credentials page.

## Where do I manage connections?

**Sidebar** → **Connections** (plug icon). URL: `#connections`. Connections reference credentials by ID; pick one under **Saved credential** and the fields it holds fill themselves.

## Where do I configure the AI Copilot?

**Insights** → **AI Provider** tab. Or click the **Configure »** chip in the chat dock header.

## Where do I see what the AI has done, and what it cost?

Insights → **Activity** subtab. One row per AI request: what was asked, where it came from (the Copilot, or a feature such as drafting a pipeline in the Editor, SQL help, or a run or notification summary), the model that answered, tokens, estimated cost, and the result (Answered, Failed or Blocked). The cards at the top total the last 7 days. Click a request to see its steps.

Pipeline runs are not on Activity: they are on the **Executions** page (Workflows → Executions).

Who changed a pipeline, and when, is in its **Version history** (Pipelines → the pipeline's **⋯** menu → Version history). This edition has no audit-log page.

## Where is the Trust page?

**Sidebar** → **Insights** → **Trust** subtab. URL: `#trust` (legacy direct link still works). Shows the live posture, what Revoluzen Flow never does, deployment modes, and the eval pass rate.

## Where do I find the eval harness output?

- **UI**: Trust page → **AI eval pass rate** section.
- **API**: `GET /api/trust/eval-summary` (returns the latest run's summary as JSON).
- **CLI**: results land under `eval_results/<timestamp>.json` after `python -m revoluzen_flow.eval.run`. Latest is always at `data/eval/latest.json`.

## Where do I find the connector certification matrix?

**Sidebar** → there's a direct entry, OR navigate to URL `#cert-matrix`. API: `GET /api/connectors/cert-matrix`.

Shows depth score (0-5) per manifest with category, vendor, validation status, and per-stream breakdown.

## Where is the Pool / Worker monitoring page?

**Sidebar** → **Pool**. Shows the governor banner, hardware presets, queue depth, throughput, spill-disk health (SSD/HDD badge).

## Where do I see lineage?

**Revoluzen Flow Enterprise only.** Sidebar → **Lineage**. Marquez-compatible cross-pipeline dataset provenance graph.

OSS Free has no lineage view; the closest substitutes are `recall_history` (chat fast lane) and `summarize_pipeline` for individual pipeline reasoning.

## Where do I configure approval policy?

**Revoluzen Flow Enterprise only.** Settings → **Approvals** tab. Configure who can approve, optional two-person rule, escalation policy.

OSS Free has no approval workflow.

## Where do I see my role / permissions?

**Two paths**:
- **Chat fast lane**: type `what's my role` → instant answer with role + environment + edition + tool tiers + can-approve + can-deploy-prod.
- **UI**: top-right user menu → **Account**.

## Where do I see my AI usage / token wallet?

- **Chat dock header**: shows `% of daily cap` next to the model name.
- **API**: `GET /api/ai/agent/budget` — returns user + workspace usage, cap, request count, today's cost USD.
- **UI**: Insights → **AI Provider** tab.

## Where is the Settings page?

**Sidebar** → **Settings** (gear icon). URL: `#settings`. Has tabs: **General**, **Security**, **Notifications**, **About**.

## Where do I configure DuckDB tuning / memory limits?

**Settings → General → Execution Tuning** section. Read-only display of the live values from `/api/pool/config`. To change, set the env var (`REVOLUZEN_FLOW_DUCKDB_MEMORY_LIMIT`, `REVOLUZEN_FLOW_DUCKDB_THREADS`, `REVOLUZEN_FLOW_MAX_CONCURRENT_RUNS`, `REVOLUZEN_FLOW_DUCKDB_TEMP_DIR`) and restart.

## Where do I configure CORS / IP allowlist / proxy trust?

**Settings → Security → Operator Config** section. Read-only display of env-var values: `REVOLUZEN_FLOW_CORS_ORIGINS`, `REVOLUZEN_FLOW_PLUS_IP_ALLOWLIST` (Plus only), `REVOLUZEN_FLOW_TRUSTED_PROXIES`. Restart to apply changes.

## Where do I configure telemetry?

**Settings → Security → Privacy** section. Default OFF. Opt-in is informational only — the telemetry sender is not active.

## Where do I configure backup destinations?

**Settings → Backup** (separate page or section). Configure local filesystem / S3 / Azure / GCS destinations.

## Where do I view the docs from inside the app?

**Sidebar** → **Help**. Has tabs:
- **Getting Started** — 7-step onboarding for new users
- **How-To Guides** — recipe-style step-by-step guides grouped by category
- **Shortcuts** — keyboard shortcuts reference
- **Nodes** — searchable catalog of every node type with descriptions
- **Reference** — full docs synced from `docs/*.md` (architecture, scaling, deployment, troubleshooting, API, etc.)

## Where is the deployment runbook?

`docs/deployment.md` (in-app: Help → Documentation → Deployment & Upgrade). Three-component upgrade model (Revoluzen Flow / Ollama runtime / Ollama models, independently versioned).

## Where do I see the version of Revoluzen Flow I'm running?

**Settings → About** tab. Shows Revoluzen Flow version, schema version, edition (Free/Plus), backend hostname, build commit.

## Where do I find the master encryption key file?

`~/.revoluzen-flow/secret.key` on POSIX (or `$REVOLUZEN_FLOW_DATA_DIR/secret.key` if `REVOLUZEN_FLOW_DATA_DIR` is set). Created on first run; chmod 600 on POSIX, or an NTFS ACL limited to SYSTEM, Administrators and the owner on Windows. **Back this up** — without it, encrypted credentials are unrecoverable. To check how it is protected on this install: Settings → Security & Diagnostics → **Master key permissions**.

To override the location: set env var `REVOLUZEN_FLOW_MASTER_KEY_FILE=/path/to/secret.key`.

## Where do I find the SQLite database file?

`<data_dir>/revoluzen_flow.db` — typically `data/revoluzen_flow.db` for from-source installs or inside the `revoluzen_flow_data` Docker volume for compose deployments.

## Where do I find pipeline checkpoint Parquet files?

`<data_dir>/checkpoints/<run_id>/<step_id>.parquet` — written after every successful step. Used by the Resume-from-step feature. TTL 7 days by default.

## Where do I find logs?

- **Backend stdout**: from-source: terminal where uvicorn runs. Docker: `docker compose logs flow`.
- **Per-pipeline-run output**: Executions page → click the run → **Logs** tab.
- **Audit events**: the backend logs them as `AUDIT:` lines at INFO level, which the default server setup does not print. There is no audit-log page in this edition; Insights → Activity lists AI requests, not audit events.

## Where do I find Ollama logs?

`docker compose logs ollama` (or the host service if Ollama is installed natively). Useful for debugging "why is the model slow" or "why did the pull fail".

## Where do I switch environments (DEV ↔ PROD)?

**Top header** → environment chip (next to the user menu). The chip color is **emerald for DEV** and **amber/red for PROD**. **PROD environment is Revoluzen Flow Enterprise only** — in OSS the chip may be visible but the contracted execution path is DEV.

## Where do I find the agent's tool list?

- **Chat fast lane**: type `what tools does the agent have` → instant answer.
- **API**: `GET /api/ai/agent/status` returns `tool_count`.
- **Code**: `backend/revoluzen_flow/ai/tools/` — one file per tool.
- **Doc**: `docs/product_facts/10_ai_copilot.md` enumerates all 37 tools by tier.

## Where do I see what's running right now?

- **Chat fast lane**: type `running now` → instant answer.
- **UI**: Executions page → filter **Status = running**.
- **API**: `GET /api/monitor/executions?status=running` or the agent tool `get_running_executions`.

## Where is the License key entered?

**Settings → License** (visible only on a Plus install or after entering a license key for the first time). Pasting a valid key flips `license_manager.is_plus = True` and unlocks Plus features immediately — no re-install.

## Where do I find pipeline parameters?

Open the pipeline → **Settings** (gear in editor toolbar) → **Parameters** tab. Define typed parameters that can be referenced from any node param via `${param.NAME}`.

## Where do I find pipeline templates / sample pipelines?

**Pipelines page → + New Pipeline** opens the template chooser. Revoluzen Flow ships **27 templates** covering the common starter shapes (simple ETL, dedup, aggregation, data quality, SCD2, API ingest with pagination, Slack alert on failure, etc.). Click any to start a pipeline pre-wired with that shape — you can edit it freely.

To start blank, click **Blank pipeline** at the bottom of the chooser.

Templates live under `data/templates/` (backend) + `frontend/src/templates/` (frontend). Drop new JSON files in those directories to add custom templates to the chooser. Revoluzen Flow Enterprise adds a **private template marketplace** — workspace-scoped templates shared across team members + parameter discovery for saved pipelines.

**Templates ≠ pipelines:** templates are starters; pipelines are your actual workflows. The Pipelines page lists your pipelines — the template chooser inside "+ New Pipeline" lists templates.
