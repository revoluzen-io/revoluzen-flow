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
| PUT | `/workflows/{id}` | Update workflow (creates a new version when something changed). Body `{"workflow": {...}, "change_summary": "...", "kind": "saved"}`; `kind` is `saved` (default), `auto` or `restored`, and the version records the signed-in user as its author |
| DELETE | `/workflows/{id}` | Move the workflow to the Bin, where it can be restored until the retention window ends (30 days by default). `?permanent=true` removes it at once, for good. See [The Bin](#the-bin) |
| GET | `/workflows/bin` | The Bin: deleted workflows that can still be restored |
| POST | `/workflows/bin/{id}/restore` | Restore a workflow from the Bin, as a draft |
| DELETE | `/workflows/bin/{id}` | Remove one workflow from the Bin for good |
| DELETE | `/workflows/bin` | Empty the Bin (admins only) |
| GET | `/workflows/{id}/versions` | List all versions, oldest first, with each one's `kind` (`saved`, `auto`, `restored`, `review`), `name`, `published` and `deployed` |
| GET | `/workflows/{id}/history` | The Versions view: kept versions newest first with runs per version, `removed` (how many the keeping rules removed) and `keep` (the rules) |
| PUT | `/workflows/{id}/versions/{N}/name` | Name version N; body `{"name": "Go-live"}`, empty to remove. Named versions are never removed |
| GET | `/workflows/{id}/diff?v1=N&v2=M` | Diff two versions |
| POST | `/workflows/{id}/validate` | Validate workflow structure |
| POST | `/workflows/{id}/test` | Test run on a sample. Records the result; does not publish |
| POST | `/workflows/{id}/publish` | Make the newest version live. Needs a passing test of that version. Optional body `{"note": "why"}`, kept with the version and in the Git commit. Schedules and endpoints use this version until the next publish |
| POST | `/workflows/{id}/revoke` | Unpublish: back to draft, schedules paused. Optional body `{"note": "why"}` |
| POST | `/workflows/{id}/discard-draft` | Throw away saved changes that were never published, back to the live version |
| POST | `/workflows/{id}/archive` | Archive workflow. 409 while it is published |
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
inside another. They are behind the folders in the project panel on the Pipelines page.

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
  because a published pipeline cannot be deleted:
  unpublish it first with `POST /workflows/{id}/revoke`. Nothing is deleted when it
  is refused, not even the drafts. `move_to_root` is not affected.

An empty folder deletes with neither. The reply says what happened:
`{"mode": "empty" | "move_to_root" | "force", "deleted_workflow_count": n,
"moved_to_root_workflow_count": n, ...}`. The project panel asks `delete-preview`
first, tells the user how many pipelines are inside (or, if any are published,
to revoke them first), and sends `force`.

## Triggers

Published endpoints and the API keys that open them, behind **Workflows → Triggers**. See [`user-guides/external-triggers.md`](user-guides/external-triggers.md).

| Method | Path | Description |
|--------|------|-------------|
| POST | `/published/{path}` | Start the pipeline published at that path. Auth: `X-API-Key` header, a signature (below), or both, as the endpoint requires. The JSON body holds the parameter values |
| GET | `/gateway/endpoints` | List endpoints. Never includes a signing secret |
| POST | `/gateway/endpoints` | Create an endpoint. With `require_signature: true` the reply holds `signing_secret`, the only time it is shown |
| PUT | `/gateway/endpoints/{id}` | Change an endpoint, or switch it on or off. Turning `require_signature` on returns a new `signing_secret` once; turning it off deletes the secret |
| POST | `/gateway/endpoints/{id}/signing-secret` | Replace the signing secret. Calls signed with the old one are refused from then on; the reply holds the new one, once |
| DELETE | `/gateway/endpoints/{id}` | Delete an endpoint |
| GET | `/gateway/keys` | List API keys, masked |
| POST | `/gateway/keys` | Create an API key. The full key is returned only in this response |
| DELETE | `/gateway/keys/{id}` | Revoke an API key |

Endpoint fields for callers' proof: `require_api_key` (default `true`),
`require_signature` (default `false`) and `signature_tolerance_seconds` (the time
window, 300 unless set, between 30 and 3600). An endpoint with neither key nor
signature can be called by anyone, and is refused if its pipeline puts a
`${param.x}` or `{{ }}` value in SQL. A signed endpoint must be `POST` or `PUT`.

### Signed calls

The format is Standard Webhooks, so any of its libraries can sign. Each call
carries three headers:

| Header | Value |
|---|---|
| `webhook-id` | A unique id for this message, chosen by the sender (at most 200 printable characters) |
| `webhook-timestamp` | When it was sent, in whole seconds since 1970 (UTC) |
| `webhook-signature` | `v1,` then the base64 HMAC-SHA256 of `<webhook-id>.<webhook-timestamp>.<body>`. The key is the secret's bytes: the part after `whsec_`, base64-decoded. Several space-separated signatures may be sent; one match is enough |

The body is signed exactly as it is sent. Python, with only the standard library:

```python
import base64, hashlib, hmac, json, time, uuid, urllib.request

secret = "whsec_..."                      # shown once when the endpoint was made
body = json.dumps({"region": "EU"}).encode()
msg_id, ts = f"msg_{uuid.uuid4().hex}", str(int(time.time()))
key = base64.b64decode(secret.removeprefix("whsec_"))
sig = base64.b64encode(hmac.new(key, f"{msg_id}.{ts}.".encode() + body, hashlib.sha256).digest()).decode()
request = urllib.request.Request(
    "https://flow.example.com/api/published/hooks/orders", data=body, method="POST",
    headers={"Content-Type": "application/json", "webhook-id": msg_id,
             "webhook-timestamp": ts, "webhook-signature": f"v1,{sig}"},
)
print(urllib.request.urlopen(request).read())
```

A signed call is refused with `401` when a header is missing, the signature does
not match (wrong secret, or the body was changed), or the timestamp is more than
the window from the server's clock. A `webhook-id` already accepted is refused
with `409` and starts nothing, so a recorded call cannot be sent again; ids are
remembered for twice the window. The id is used up only when the run starts, so
a call turned away earlier (`423` paused, `429` too many calls) can be sent again
with the same id. A signed run shows on the Executions page as *Webhook*
(`triggered_by: "webhook"`), with the id in its record.

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
| GET | `/schedules/` | List schedules and triggers (filter by workflow/project) |
| POST | `/schedules/` | Create a schedule or trigger |
| GET | `/schedules/{id}` | Get one |
| PUT | `/schedules/{id}` | Update one. Fields left out keep their value |
| DELETE | `/schedules/{id}` | Delete one, with what a file trigger remembered |
| POST | `/schedules/{id}/toggle` | Switch it on or off. Switching a trigger on checks it as a create would |
| POST | `/schedules/{id}/run-now` | A timed schedule runs now. A file trigger looks in its folder now (`{"status": "checking"}`). An after-pipeline trigger answers `400`: run the pipeline itself |
| POST | `/schedules/check-folder` | What a file trigger would see now. Body: `file_location`, `file_connection_id`, `file_folder`, `file_pattern`. Returns `total`, `matching` and up to 50 `files` (`path`, `name`, `size`, `modified`), newest first. Starts nothing. `400` with the reason when the folder cannot be listed |
| GET | `/schedules/{id}/files` | The files a file trigger has started runs for, newest first: `path`, `version`, `status` (`started`, `done`), `run_status`, `execution_id` |

Creating or changing one needs the Developer role; reading needs any signed-in
role. Every call is limited to the caller's workspace.

### Schedule Types
- **interval** — Run every N minutes (`interval_minutes`)
- **daily** — Run at specific time (`daily_time: "HH:MM"`)
- **weekly** — Run on specific days (`weekly_days: [0-6]`, 0 is Monday; `weekly_time`)
- **cron** — Full cron expression (`cron_expression: "0 */6 * * *"`). In its day-of-week field 0 and 7 are Sunday and 1 is Monday, as in standard cron.
- **file_arrival** — Each new file in a folder starts one run. See below.
- **after_pipeline** — Another pipeline finishing starts a run. See below.

The two triggers only ever run a **published** pipeline, at its published
version: creating one, or switching it on, for a pipeline that is not published
answers `400`. Their runs go through the pool and keep the pipeline's run time
limit and overlap setting. Each row also carries `status_message` (what the
trigger last found or why it is waiting) and `last_checked_at`.

#### file_arrival

| Field | Meaning |
|---|---|
| `file_location` | `local` (a folder on the Flow server), `sftp`, `smb` (a Windows file share), `s3` (also MinIO), `gcs` or `azure_blob` |
| `file_connection_id` | The saved connection, for all but `local`. Its type must fit: `sftp` (or `ftp` with protocol SFTP), `smb`, `s3`/`minio`, `gcs`, `azure_blob` |
| `file_folder` | A full path or a folder in the data folder (`local`), a folder on the SFTP server, a folder on the share (`smb`: inside the connection's folder, empty for that folder, a leading `\` for the top of the share), or a key prefix. Only files directly in it count |
| `file_pattern` | File names that count, such as `orders_*.csv`. Case does not matter; several separated by `;`. Default `*` |
| `file_parameter` | The pipeline parameter that receives the file's path, or empty for none. Must be a parameter of the published version |
| `file_settle_seconds` | How long a file must keep its size and time before its run starts (0 to 86400, default 60). It is always seen twice first |
| `file_poll_seconds` | How often the folder is listed (30 to 86400, default 60) |
| `file_include_existing` | Also run for files already there when it is saved (default `false`) |

The path passed is a full path (`local`), a path on the SFTP server, the
file's full `\\server\share\...` path (`smb`, read by a File step that uses the
same connection), or the object's key in the connection's bucket or container.
On a share, a file is also opened just before its run starts, and waits while
the program writing it still has it open. Each file version runs
once, recorded in the database by path and version (content tag, or size and
time), so a restart or a second server never repeats it. A trigger runs one
file at a time, oldest first. A run's record keeps `metadata.file` (`path`,
`name`, `size`, `modified`, `location`, `folder`) and `metadata.parameter_values`,
so **Replay** reads the same file. Runs show as `triggered_by: "file_arrival"`.

Listing needs `paramiko` for SFTP (`pip install revoluzen-flow[sftp]`) and
`boto3` for S3 and for Google Cloud Storage with HMAC keys (`[aws]`). Google
Cloud Storage with a service account needs `google-cloud-storage` (`[google]`).
Azure Blob Storage needs nothing extra: it signs in with the connection's
account key, SAS token, connection string or service principal over the REST API.
A Windows file share needs nothing extra either: `smbprotocol` ships with Flow.

```json
{
  "workflow_id": "wf_123",
  "schedule_type": "file_arrival",
  "file_location": "sftp",
  "file_connection_id": "conn_partner_sftp",
  "file_folder": "/outbound/orders",
  "file_pattern": "orders_*.csv",
  "file_parameter": "file_path"
}
```

#### after_pipeline

| Field | Meaning |
|---|---|
| `after_workflow_id` | The pipeline to run after, in the same workspace |
| `after_outcome` | `success` (default), `failure` or `always` |

Runs of that pipeline started by a schedule, a file, another pipeline, or a
published endpoint count (`triggered_by` of `schedule`, `file_arrival`,
`after_pipeline`, `api`, `webhook`); runs started by hand, replays, tests,
backfills and stopped runs do not. If this pipeline is still running when the
other finishes again, it runs once more afterwards. A trigger that would close a
loop is refused with `409`:

```json
{"detail": {"code": "trigger_loop",
            "message": "This would start the pipelines in a loop: \"Load\" → \"Extract\" → \"Load\". Remove one of these triggers first.",
            "loop": ["wf_load", "wf_extract", "wf_load"]}}
```

A run's record keeps `metadata.after_pipeline` (`workflow_id`, `workflow_name`,
`execution_id`, `status`) and `metadata.chain`, the pipelines that led to it; a
chain that would run a pipeline twice, or more than 25, stops. Runs show as
`triggered_by: "after_pipeline"`.

A row saved before these existed with `schedule_type: "event"` reads as the
trigger it described, switched off. The API no longer accepts `event`.

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

## Reports

Reports people design, save and schedule. Reading needs any signed-in role; changing or sending needs at least Developer. Every call is limited to the caller's workspace.

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/reports/builder/catalog` | The sections, data for your own tables, templates, environments, and which channels are ready |
| POST | `/reports/builder/preview` | Make a report from a definition that has not been saved |
| GET | `/reports/saved` | List saved reports with their schedule and last result |
| POST | `/reports/saved` | Save a report |
| GET | `/reports/saved/{id}` | Get one |
| PUT | `/reports/saved/{id}` | Change it |
| DELETE | `/reports/saved/{id}` | Delete it and its sent history |
| POST | `/reports/saved/{id}/duplicate` | Copy it, with the schedule off |
| POST | `/reports/saved/{id}/schedule` | Turn the schedule on or off (`{"enabled": true}`) |
| POST | `/reports/saved/{id}/run` | Make it now and return the numbers |
| POST | `/reports/saved/{id}/send` | Send it now; `{"to_me": true}` sends only to the caller |
| GET | `/reports/saved/{id}/download?format=pdf\|docx` | Download it; a copy is kept in Storage |
| GET | `/reports/saved/{id}/deliveries` | Every send: when, who it reached, and why anything failed |
| GET | `/reports/saved/{id}/deliveries/{delivery_id}` | The report as it went out |
| GET | `/reports/inventory?format=pdf\|docx\|json&scope=&scope_id=&env=` | The fixed installation inventory document |

A stored Slack, Teams or webhook address is encrypted and comes back as eight bullets. Sending the bullets back on `PUT` keeps it. Webhooks on the private network are refused unless `REVOLUZEN_FLOW_REPORT_WEBHOOK_ALLOW_PRIVATE=1`.

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
| GET | `/connections/{id}/versions` | Kept versions, newest first: each one's settings, who made it and what changed, plus the settings now and the pipelines that use the connection. Each password or key appears as `{"secret": true, "tag": "…"}`; the same tag means the same secret, and the value is never kept |
| POST | `/connections/{id}/versions/{n}/restore` | Make the settings what they were in version `n`, saved as a new version. Body `{"secrets": {"password": "…"}}` gives old passwords typed in; any other secret keeps today's value. Returns which secrets kept today's value (`kept_secrets`), which the connection has none for (`missing_secrets`), and a linked credential, project or environment kept because the version's is gone (`kept_settings`) |
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
