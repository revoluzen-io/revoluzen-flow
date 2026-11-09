# Revoluzen Flow v1.0.0 — API Reference

**Base URL:** `http://localhost:8001/api`
**Health Check:** `http://localhost:8001/api/health`
**Interactive Docs:** `http://localhost:8001/docs` (Swagger UI, disabled unless `REVOLUZEN_FLOW_ENABLE_API_DOCS=1`)

If `revoluzen open` reports that port `8001` was already in use and selected
another port, use that printed port for every URL on this page.

## Health & Utility

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/health` | Health check with scheduler & notifier status |
| GET | `/scheduler/status` | Background scheduler status & active jobs |
| GET | `/node-types` | Available node types for canvas palette |
| POST | `/upload` | Upload CSV/JSON/Parquet file |
| GET | `/files` | List available data files |

## Workflows

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/workflows/` | List all workflows (latest version) |
| POST | `/workflows/` | Create new workflow |
| GET | `/workflows/{id}` | Get workflow (latest or specific version) |
| PUT | `/workflows/{id}` | Update workflow (creates new version) |
| DELETE | `/workflows/{id}` | Move the workflow to the Bin, where it can be restored until the retention window ends (30 days by default). `?permanent=true` removes it at once, for good. See [The Bin](#the-bin) |
| GET | `/workflows/bin` | The Bin: deleted workflows that can still be restored |
| POST | `/workflows/bin/{id}/restore` | Restore a workflow from the Bin, as a draft |
| DELETE | `/workflows/bin/{id}` | Remove one workflow from the Bin for good |
| DELETE | `/workflows/bin` | Empty the Bin (admins only) |
| GET | `/workflows/{id}/versions` | List all versions |
| GET | `/workflows/{id}/diff?v1=N&v2=M` | Diff two versions |
| POST | `/workflows/{id}/validate` | Validate workflow structure |
| POST | `/workflows/{id}/test` | Test run (auto-publishes on success) |
| POST | `/workflows/{id}/publish` | Publish workflow |
| POST | `/workflows/{id}/archive` | Archive workflow |
| POST | `/workflows/{id}/restore` | Restore archived workflow |
| POST | `/workflows/{id}/toggle-active` | Activate or deactivate. Body: `{"active": false}`. While deactivated, schedules skip the pipeline and its published endpoints answer 423; running it by hand still works |
| GET | `/workflows/{id}/lifecycle` | Get lifecycle event history |

### Create Workflow
```json
POST /api/workflows/
{
  "name": "My Pipeline",
  "description": "Loads and transforms data",
  "project_id": "default",
  "steps": [
    {"id": "s1", "type": "csv_source", "label": "Load CSV", "params": {"file_path": "data.csv"}},
    {"id": "s2", "type": "filter", "label": "Filter", "params": {"condition": "amount > 100"}}
  ],
  "connections": [
    {"from_step": "s1", "to_step": "s2"}
  ]
}
```

### The Bin

`DELETE /workflows/{id}` does not remove a workflow. It moves the workflow, with
all its saved versions, to the Bin, and takes it out of every list. Its
schedules are removed, its cached step output is cleared and its CDC replication
slots are dropped, but the Bin keeps a copy of the schedules so a restore can put
them back. Its past runs stay in Executions.

A workflow stays in the Bin for `REVOLUZEN_FLOW_BIN_RETENTION_DAYS` days
(default **30**; `0` means it is never removed automatically). A job that runs
once a day removes the ones past that, and records `workflow.purge` in the audit
log. Deleting, restoring and removing from the Bin are recorded as
`workflow.delete`, `workflow.restore` and `workflow.purge`.

```json
DELETE /api/workflows/{id}
{"deleted": true, "binned": true, "retention_days": 30, "purge_at": "2026-12-08T09:14:03+00:00"}

DELETE /api/workflows/{id}?permanent=true
{"deleted": true, "binned": false}
```

`purge_at` is null when `retention_days` is 0. `?permanent=true` is for scripts
that must not leave a copy behind; the app has no button for it.

```json
GET /api/workflows/bin
{
  "retention_days": 30,
  "count": 1,
  "items": [{
    "id": "8f3c…", "name": "Orders ETL", "project_id": "default", "folder_id": null,
    "status_before": "published", "deleted_at": "2026-11-08T09:14:03+00:00",
    "deleted_by_id": "u1", "deleted_by_name": "Asha", "version_count": 7,
    "purge_at": "2026-12-08T09:14:03+00:00"
  }]
}
```

The list is newest deletion first, and like the workflow list it leaves out
anything in a project the caller cannot see.

`POST /workflows/bin/{id}/restore` always brings the workflow back as a **draft**,
whatever its status was when it was deleted, and its schedules come back
**paused**. It adds a "Restored from the Bin" entry to the version history. What
had to change on the way back is listed in `notes`:

- a name that is taken again gets a `(2)` suffix
- a project that is gone or archived is replaced by Default
- a folder that is gone, or is no longer in that project, becomes the project root

```json
POST /api/workflows/bin/{id}/restore
{
  "restored": true, "id": "8f3c…", "name": "Orders ETL (2)", "renamed_from": "Orders ETL",
  "status": "draft", "project_id": "default", "folder_id": null,
  "schedules_restored": 1,
  "notes": ["The name \"Orders ETL\" is in use again, so it was restored as \"Orders ETL (2)\".",
            "1 schedule came back paused."]
}
```

A workflow that is not in the Bin, or is in a project the caller cannot see,
returns 404. Restoring returns 409 if a live workflow already has the id.

`DELETE /workflows/bin/{id}` removes one workflow from the Bin for good, and
`DELETE /workflows/bin` empties the workspace's Bin. Neither can be undone.

| Action | Needs |
|--------|-------|
| List the Bin | any signed-in user |
| Delete, restore, remove one from the Bin | Developer or above |
| Empty the Bin | Admin or above |

A CDC pipeline gets new replication slots when it is restored, and does not
replay the changes made to its source while it was in the Bin.

## Folders

Folders group pipelines inside a project, one level deep: a folder cannot sit
inside another. They are behind the folder tree on the Projects page.

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/folders?project_id=` | List a project's folders |
| POST | `/folders` | Create a folder. Body `{"name": "...", "project_id": "..."}` |
| GET | `/folders/{id}` | Get a folder |
| PATCH | `/folders/{id}` | Change a folder's name, description, colour or icon |
| GET | `/folders/{id}/delete-preview` | What deleting it would take with it: `affected_workflow_count`, `affected_workflow_ids`, `descendant_folder_ids`, and the published ones among the pipelines (`published_workflow_count`, `published_workflow_ids`, `published_workflow_names`). Changes nothing |
| DELETE | `/folders/{id}` | Delete a folder. A folder that holds pipelines needs a mode, see below. Developer rank or above |
| POST | `/folders/move-workflows` | Move pipelines into a folder, or to the project's top level with `"folder_id": null`. Body `{"workflow_ids": [...], "folder_id": "..."}` |

Creating a pipeline inside a folder (`POST /workflows/` with `folder_id`) checks
that the folder exists in the caller's workspace and belongs to the pipeline's
project. It answers 404 for a folder that does not exist or is in another
workspace, and 400 for one that belongs to another project.

**Deleting a folder** that holds pipelines is refused with 409 until the request
says what to do with them:

- `?move_to_root=true` keeps the pipelines: they move to the project's top level,
  then the folder is deleted.
- `?force=true` deletes the pipelines with the folder. They go to the Bin (see
  [The Bin](#the-bin)), where they can be restored until the retention window
  ends, and they come back in the project's top level, since the folder is gone.
  It is refused with 409 (`"code": "published_pipelines_in_folder"`, with the
  `published_workflow_*` fields) while any pipeline in the folder is published,
  because a published pipeline cannot be deleted from the Pipelines page either:
  revoke it first with `POST /workflows/{id}/revoke`. Nothing is deleted when it
  is refused, not even the drafts. `move_to_root` is not affected.

An empty folder deletes with neither. The reply says what happened:
`{"mode": "empty" | "move_to_root" | "force", "deleted_workflow_count": n,
"moved_to_root_workflow_count": n, ...}`. The Projects page asks `delete-preview`
first, tells the user how many pipelines are inside (or, if any are published,
to revoke them first), and sends `force`.

## Triggers

Published endpoints and the API keys that open them, behind **Workflows → Triggers**. See [`user-guides/external-triggers.md`](user-guides/external-triggers.md).

| Method | Path | Description |
|--------|------|-------------|
| POST | `/published/{path}` | Start the pipeline published at that path. Auth: `X-API-Key` header. The JSON body holds the parameter values |
| GET | `/gateway/endpoints` | List endpoints |
| POST | `/gateway/endpoints` | Create an endpoint |
| PUT | `/gateway/endpoints/{id}` | Change an endpoint, or switch it on or off |
| DELETE | `/gateway/endpoints/{id}` | Delete an endpoint |
| GET | `/gateway/keys` | List API keys, masked |
| POST | `/gateway/keys` | Create an API key. The full key is returned only in this response |
| DELETE | `/gateway/keys/{id}` | Revoke an API key |

## Execution

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/execute/workflow/{id}` | Execute entire workflow |
| POST | `/execute/workflow/{id}/step/{step_id}` | Execute single step (with deps) |
| POST | `/execute/workflow/{id}/pre-validate` | Pre-execution data validation |
| POST | `/preview` | Preview data file |

### Execution Response
```json
{
  "workflow_id": "abc123",
  "status": "success",
  "duration_ms": 145.2,
  "step_results": {
    "s1": {
      "step_id": "s1",
      "status": "success",
      "row_count": 1000,
      "columns": ["id", "name", "amount"],
      "sample_data": [{"id": 1, "name": "Alice", "amount": 100}],
      "schema_info": [{"name": "id", "type": "BIGINT"}, ...],
      "duration_ms": 23.5
    }
  }
}
```

## Projects

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/projects/` | List projects with pipeline counts |
| POST | `/projects/` | Create project |
| GET | `/projects/{id}` | Get project |
| PUT | `/projects/{id}` | Update project |
| DELETE | `/projects/{id}` | Delete project (not "default") |
| GET | `/projects/{id}/pipelines` | List pipelines in project |

## Schedules

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/schedules/` | List schedules (filter by workflow/project) |
| POST | `/schedules/` | Create schedule |
| GET | `/schedules/{id}` | Get schedule |
| PUT | `/schedules/{id}` | Update schedule |
| DELETE | `/schedules/{id}` | Delete schedule |
| POST | `/schedules/{id}/toggle` | Enable/disable schedule |
| POST | `/schedules/{id}/run-now` | Manually trigger |

### Schedule Types
- **interval** — Run every N minutes (`interval_minutes`)
- **daily** — Run at specific time (`daily_time: "HH:MM"`)
- **weekly** — Run on specific days (`weekly_days: [0-6]`, `weekly_time`)
- **cron** — Full cron expression (`cron_expression: "0 */6 * * *"`)
- **event** — Triggered externally (`event_trigger`, `event_source_id`)

## Alerts

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/alerts/rules` | List alert rules |
| POST | `/alerts/rules` | Create alert rule |
| GET | `/alerts/rules/{id}` | Get rule |
| PUT | `/alerts/rules/{id}` | Update rule |
| DELETE | `/alerts/rules/{id}` | Delete rule |
| POST | `/alerts/rules/{id}/test` | Send test notification |
| GET | `/alerts/logs` | List alert logs |

### Alert Channels
- **email** — SMTP delivery (dry-run if SMTP_HOST not set)
- **slack** — Incoming webhook
- **teams** — Adaptive Card via webhook
- **webhook** — Generic JSON POST

### Alert Conditions
- `on_failure` — Pipeline failed
- `on_success` — Pipeline succeeded
- `on_any` — Any completion
- `on_long_running` — Duration exceeds threshold
- `on_sla_breach` — SLA violation

## Monitor

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/monitor/executions` | List executions (filter by workflow/project) |
| GET | `/monitor/executions/{id}` | Get execution detail with step logs |
| GET | `/monitor/stats` | 24h execution statistics |
| GET | `/monitor/stats/multi` | 24h/7d/30d stats |
| GET | `/monitor/run-counts` | Every run in the window, counted by status in ten-minute slots |
| GET | `/monitor/active-schedules` | Enabled schedules |
| GET | `/monitor/failed` | Currently-failing pipelines (latest run failed; `failure_count` = consecutive streak) |

## Auth

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/auth/login` | Login (email + password) → token |
| POST | `/auth/register` | Register new user → token |
| GET | `/auth/me` | Get current user (requires Bearer token) |
| POST | `/auth/logout` | Invalidate session |
| GET | `/auth/users` | List all users |
| POST | `/auth/invite` | Invite user with temp password |
| DELETE | `/auth/users/{id}` | Delete user (not admin) |

## Variables

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/variables/` | List variables (filter by scope/project) |
| POST | `/variables/` | Create variable |
| GET | `/variables/{id}` | Get variable |
| PUT | `/variables/{id}` | Update variable |
| DELETE | `/variables/{id}` | Delete variable |
| POST | `/variables/resolve` | Resolve variable value |

### Variable Types
- `string` — Plain text
- `secret` — Masked in responses
- `number` — Numeric value
- `boolean` — True/false

### Scoping
- `global` — Available everywhere
- `project` — Scoped to a project (overrides global with same key)

## Credentials

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/credentials/` | List credentials (secrets masked) |
| POST | `/credentials/` | Create credential |
| GET | `/credentials/{id}` | Get credential (masked) |
| PUT | `/credentials/{id}` | Update credential |
| DELETE | `/credentials/{id}` | Delete credential |
| POST | `/credentials/{id}/test` | Test connectivity |

## Connections

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/connections/types` | List supported connection types |
| GET | `/connections/` | List connections |
| POST | `/connections/` | Create connection |
| GET | `/connections/{id}` | Get connection |
| PUT | `/connections/{id}` | Update connection |
| DELETE | `/connections/{id}` | Delete connection |
| POST | `/connections/{id}/test` | Test connection |
| POST | `/connections/{id}/reports` | Create parameterized report |
| POST | `/connections/{id}/reports/{report_id}/run` | Run report |

## Intelligence

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/intelligence/detect-schema` | Detect schema from CSV/JSON/XML |
| POST | `/intelligence/flatten` | Flatten nested data |
| POST | `/intelligence/suggest-pipeline` | Suggest pipeline from schema |

## Schema Contracts

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/contracts/` | Create schema contract |
| GET | `/contracts/{id}` | Get contract |
| POST | `/contracts/{id}/validate` | Validate schema against contract |
| GET | `/contracts/{workflow_id}/{step_id}/drift` | Check drift |

## Planner

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/planner/generate` | Generate workflow from intent |
| GET | `/planner/templates` | List pipeline templates |
| POST | `/planner/chat` | AI chat for pipeline building |

## Steward

Read-only background reliability layer. Detects duplicate sources,
duplicate pipelines, and (in later sub-agent releases) failure
patterns + schema drift. Ships in OSS — full design rationale in
[steward/architecture.md](steward/architecture.md). All endpoints
auth-gated, all responses workspace-scoped.

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/steward/findings?status=open` | Run a fresh scan and return findings matching `status` (`open` default; pass `status=all` to include dismissed / resolved / stale). Sub-50ms on typical OSS workspaces. |
| POST | `/steward/scan` | Force a re-scan. Backs the UI "Re-scan" button. Same detection path as `GET /findings`, returns `{scanned: true, count: N}`. |
| POST | `/steward/findings/{id}/dismiss` | Mark a finding as intentional and permanently suppress its signature. Optional body `{"reason": "..."}` is recorded in the memory journal for the future Curator sub-agent. Also marks related notification-bell rows as read. |
| POST | `/steward/findings/{id}/resolve` | Close a finding because action was taken. Does NOT permanently suppress (if the pattern recurs, it re-emits as `(rebounded)`). Also marks related notification-bell rows as read. |
| GET | `/steward/settings` | Per-workspace settings (enabled, min_severity, scan_on_save, auto_stale_days, escalate_after_n_occurrences, notify_on_finding, notify_min_severity). |
| PUT | `/steward/settings` | Partial-update friendly — pass only fields to change. Server merges, re-validates, persists to `<data_dir>/steward/<ws>/settings.json`. |
| GET | `/steward/memory?limit=200` | Recent journal events newest-first + persistent occurrence counts per signature. Backs the "Memory" tab and proves the learning behaviour to users. |
| GET | `/steward/memory/stats` | Aggregate counters (`total_events`, `total_scans`, `total_emits`, `total_dismisses`, `total_resolves`, `distinct_signatures_seen`). |

Each finding payload has this shape:

```json
{
  "id": "dup-src-a1b2c3d4e5f6g7h8",
  "kind": "duplicate_source",
  "severity": "p2",
  "status": "open",
  "title": "3 pipelines read the same source",
  "body": "The same source object is read by 3 pipelines...",
  "evidence": {
    "source_signature": "a1b2c3d4e5f6g7h8",
    "workflows": [{"id": "...", "name": "..."}, ...]
  },
  "proposed_actions": [
    {"label": "Consolidate via Managed Table", "action": "...", "params": {...}}
  ],
  "first_seen": "2026-06-05T10:14:32.000Z",
  "last_seen": "2026-06-05T10:14:32.000Z",
  "occurrences": 3
}
```

Severity enums: `p1` / `p2` / `p3`. Kind enums: `duplicate_source`,
`duplicate_pipeline` (1.1 — shipped); `failure_rca` (1.2),
`volume_anomaly` / `schema_drift` (1.3), `cost_recommendation` (2.0)
land progressively.

## Revoluzen Flow Memory Layer (Lessons)

Durable, human-approved lessons distilled from operator decisions.
Distinct from the operational event journal at `/api/steward/memory*`
above. Full conceptual description in
[steward/memory-layer.md](steward/memory-layer.md).

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/steward/lessons?status=approved&lesson_type=failure_pattern&source=X&pipeline=Y` | List lessons with optional filters. Newest first by `last_validated`. Auto-ages APPROVED lessons whose `validity_days` have elapsed. |
| GET | `/steward/lessons/stats` | Counters: `total_lessons`, `by_status` (proposed/approved/rejected/stale), `by_type` (10 categories). |
| GET | `/steward/lessons/{id}` | Single lesson by ID. |
| POST | `/steward/lessons` | Propose a new lesson. Body: `{lesson_type, issue, approved_fix, source?, pipeline?, symptom?, root_cause?, evidence?, tags?}`. Starts in `PROPOSED` status — does NOT influence Steward reasoning until approved. |
| POST | `/steward/lessons/{id}/approve` | Body: `{"approver": "..."}`. PROPOSED → APPROVED. The lesson now influences search and recommendations. |
| POST | `/steward/lessons/{id}/reject` | Body: `{"reviewer": "...", "reason": "..."}`. Marks the proposal incorrect; reason stored in evidence for audit. |
| POST | `/steward/lessons/{id}/revalidate` | Body: `{"reviewer": "..."}`. Bumps `occurrence_count`, refreshes `last_validated`, may promote confidence LOW→MEDIUM→HIGH. Revives STALE → APPROVED. |
| POST | `/steward/lessons/search` | Body: `{"source": "...", "error": "...", "max_results": 5}`. Step 2 of the failure-recovery workflow — returns matching APPROVED lessons ranked by confidence + `occurrence_count`. |
| DELETE | `/steward/lessons/{id}` | Hard-delete. Only suitable for REJECTED proposals — APPROVED lessons should be marked STALE so the audit trail survives. |

Lesson categories (`lesson_type` enum): `source_quirk`, `schema_drift`,
`failure_pattern`, `transformation_rule`, `retry_rule`, `cost_anomaly`,
`duplicate_warning`, `sla_pattern`, `user_fix`, `security_finding`.

Status enum: `proposed` / `approved` / `rejected` / `stale`.
Confidence enum: `low` / `medium` / `high`.

Lesson on-disk storage at
`<data_dir>/steward/<workspace_id>/lessons/<id>.{yaml,json}` — both
files are written together inside a file lock and cannot drift.

## Node Types

| Type | Category | Required Params |
|------|----------|-----------------|
| `csv_source` | source | `file_path` |
| `db_source` | source | `query` |
| `api_source` | source | `url` |
| `filter` | transform | `condition` |
| `transform` | transform | `expression` |
| `aggregate` | transform | `group_by`, `functions` |
| `join` | transform | `join_key` (2 inputs required) |
| `deduplicate` | transform | `key` |
| `sort` | transform | `column` |
| `rename` | transform | `columns` |
| `output` | sink | `format` |
| `db_sink` | sink | `connection_id`, `table` |

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `REVOLUZEN_FLOW_PORT` | `8001` | API server port |
| `REVOLUZEN_FLOW_DATA_DIR` | `./data` | Data file directory |
| `SMTP_HOST` | _(empty)_ | SMTP server for email alerts |
| `SMTP_PORT` | `587` | SMTP port |
| `SMTP_USER` | _(empty)_ | SMTP username |
| `SMTP_PASS` | _(empty)_ | SMTP password |
| `SMTP_FROM` | `revoluzen-flow@localhost` | Sender address |
| `SMTP_TLS` | `true` | Enable STARTTLS |
