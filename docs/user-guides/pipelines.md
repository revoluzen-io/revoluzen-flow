# Pipelines — User Guide

**Audience:** All users
**Prerequisites:** Optionally [a project](projects.md) and [a connection](connections.md)

A **Pipeline** in Revoluzen Flow (internally called a *workflow*) is a directed graph of nodes that reads from sources, transforms data, and writes to sinks. Pipelines are the primary unit of work in Revoluzen Flow.

---

## Table of contents

1. [Anatomy of a pipeline](#1-anatomy-of-a-pipeline)
2. [Pipeline lifecycle (OSS)](#2-pipeline-lifecycle-oss)
3. [Creating a pipeline](#3-creating-a-pipeline)
4. [Building the graph on the canvas](#4-building-the-graph-on-the-canvas)
5. [Validating a pipeline](#5-validating-a-pipeline)
6. [Running a pipeline](#6-running-a-pipeline)
7. [Scheduling and triggers](#7-scheduling-and-triggers)
8. [Monitoring execution](#8-monitoring-execution)
9. [Archiving, restoring and deleting](#9-archiving-restoring-and-deleting)
10. [Cloning a pipeline](#10-cloning-a-pipeline)
11. [Exporting and importing](#11-exporting-and-importing)
12. [Versioning model](#12-versioning-model)
13. [API reference](#13-api-reference)
14. [Troubleshooting](#14-troubleshooting)
15. [Revoluzen Flow Enterprise: governance & approvals](#15-revoluzen-flow-governance--approvals)

---

## 1. Anatomy of a pipeline

A pipeline has four parts:

| Part | Description |
|---|---|
| **Metadata** | `name`, `description`, `project_id`, owner, tags, deployed version |
| **Steps** (nodes) | Individual operations — one per box on the canvas. Each step has a `type` (e.g. `source`, `filter`, `destination`) and `params`. |
| **Connections** (edges) | Directed edges between steps. Each edge carries the output of the source step to the input of the destination step. |
| **Schema contract** (optional) | Declares the expected columns and types at the final sink. Enables drift detection. |

Under the hood, the pipeline is stored as a Pydantic `Workflow` object, serialised to JSON in the `workflow_versions.data` column. Every save creates a new immutable version row.

### 1.1 Node categories

Revoluzen Flow ships with **45 nodes across 6 categories**:

| Category | Nodes | Purpose |
|---|---|---|
| **Data Movement** (6) | `source`, `destination`, `copy_data`, `local_table_source`, `local_table_sink`, `saas_connector` | Read data in and write it out. One generic Source and one generic Destination — pick the connector inside the node, rather than a node per format. SaaS Connector reads a SaaS application, through a built-in connector or one saved from the API Explorer. |
| **Transform** (12) | `transform`, `filter`, `derived_column`, `sort`, `deduplicate`, `sample`, `schema_mapper`, `data_wrangler`, `data_quality`, `data_profile`, `flatten_explode`, `scd2` | Change the shape of one input |
| **Combine** (7) | `join`, `union`, `aggregate`, `lookup`, `pivot`, `unpivot`, `window` | Bring rows or inputs together |
| **Control Flow** (10) | `if_condition`, `conditional_split`, `foreach_loop`, `foreach_pipeline`, `execute_pipeline`, `lookup_activity`, `set_variable`, `wait_delay`, `retry_handler`, `fail` | Conditional and looping logic |
| **Action** (7) | `http_request`, `send_email`, `slack_notify`, `get_metadata`, `delete_data`, `file_system`, `execute_sql_task` | Operational primitives and side effects |
| **AI / Semantic** (3) | `embedder`, `llm_guardrail`, `semantic_router` | Embeddings and LLM-assisted routing |

> Older docs listed per-format nodes such as `csv_source` or `db_sink`. Those
> types are still registered so existing pipelines keep running, but they are
> hidden from the palette — use the generic `source` / `destination` nodes and
> choose the connector in the node's config panel.

For each connector, see the [connector catalog](../connectors.md). For each node, open the **Nodes** tab in this Help page.

---

## 2. Pipeline lifecycle (OSS)

In Revoluzen Flow, every pipeline moves through a simple set of states:

```
   ┌─────────┐   save    ┌─────────┐   /test passes   ┌───────────┐
   │  DRAFT  │ ────────▶ │  DRAFT  │ ───────────────▶ │ PUBLISHED │
   └─────────┘           └─────────┘                  └─────┬─────┘
        ▲                                                    │
        │ /restore                                           │ runs on
        │                                                    │ schedule
   ┌─────────┐                                               ▼
   │ARCHIVED │                                          ┌─────────┐
   └─────────┘                                          │ RUNNING │
                                                        └─────────┘
```

- **DRAFT** is editable by the author. Runnable as a test.
- **PUBLISHED** means at least one test passed. Runnable on a schedule.
- **ARCHIVED** is hidden from normal lists but preserved. Restore to DRAFT at any time.

> **Revoluzen Flow Enterprise** adds two more states (`PENDING APPROVAL`, `APPROVED`, `REJECTED`) and a separate `DEPLOYED` notion that pins a specific version for PROD scheduled runs. See [section 15](#15-revoluzen-flow-governance--approvals).

---

## 3. Creating a pipeline

### 3.1 Via the UI

1. Go to the **Pipelines** page.
2. Optionally pick a project, or a folder inside one, in the project panel on the left. The new pipeline is saved there.
3. Click **+ New Pipeline**. The Templates gallery opens: pick a template, or **Blank canvas** for an empty one.
4. Build on the canvas, then click **Save** and give the pipeline a name.

### 3.2 Via the API

```bash
curl -X POST http://localhost:8001/api/workflows/ \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Orders ETL",
    "description": "Nightly load of orders from Postgres to S3"
  }'
```

A pipeline is created in `DRAFT` status, empty of steps.

### 3.3 Several pipelines open at once

The Pipelines page has tabs above the list, beside the project panel. The first, **Home**, is the list of every pipeline. Click a pipeline in the list and it opens in a tab of its own on that page, in full: its preview, last run, and Publish, Run, Version history, Duplicate and Edit in Editor. **Open**, at the right end of the tabs, picks another pipeline to show. Up to ten can be open there, and closing one loses nothing.

The Editor has tabs of its own, above the canvas and beside the Nodes panel. They hold only the pipelines you open in it to build (**Edit** on a row, or **Edit in Editor**), up to ten; looking at a pipeline on the Pipelines page does not add it.

- Click an editor tab to switch canvases. Each keeps its test results and unsaved edits as you left them.
- A run carries on when you switch to another tab, and its results land in its own tab.
- A dot on a tab marks unsaved changes. Closing that tab asks whether to save or discard them.
- At the right end of the editor tabs, **New Pipeline** opens an empty canvas in a tab of its own, without leaving the Editor; describe the pipeline there, or pick **Use template**. **Open** brings in another saved pipeline. (**New Pipeline** in the Pipelines page header opens the Templates gallery first.)
- After a reload, both sets of tabs come back (saved pipelines only). Unsaved edits do not: the browser asks before you leave a page with any.

---

## 4. Building the graph on the canvas

The canvas is where you assemble the pipeline.

### 4.1 Adding nodes

- **Drag** from the palette (left) onto the canvas, or **click** a tile to add it at the current cursor.
- Click a node to open the configuration panel (right).
- Fill in required fields (marked with `*`).
- Source and sink nodes need a **Connection** — select it from the connection picker. See [Connections Guide](connections.md).

### 4.2 Wiring nodes

- Click and drag from the **output port** (right side / bottom of a node) to the **input port** (left side / top of the next node).
- Edges show the column count at their midpoint once the upstream node has sample data.
- You cannot connect a node to itself or create cycles. The validator refuses.

### 4.3 The ghost node (AI-assisted)

After you add a node, Revoluzen Flow proposes the next node as a translucent **ghost node** on the canvas based on common patterns. Click to accept; ignore to proceed manually.

### 4.4 Saving

Press `Ctrl+S` (`Cmd+S` on macOS) to save. Every save creates a **new version** in the backing store.

```bash
curl -X PUT http://localhost:8001/api/workflows/{id} \
  -H "Content-Type: application/json" \
  -d '{
    "workflow": { /* full Workflow object */ },
    "change_summary": "Added filter after source"
  }'
```

---

## 5. Validating a pipeline

Validation is automatic on save. To run it explicitly:

```bash
curl -X POST http://localhost:8001/api/workflows/{id}/validate
```

Validation checks:

1. **Structure** — no broken edges, no orphan nodes, no cycles
2. **Required params** — every node has its required params filled
3. **Type consistency** — source nodes use read-capable connections; sink nodes use write-capable connections
4. **Schema mapping** — if a schema contract is attached, columns line up

Structural errors block save + test. Warnings do not.

---

## 6. Running a pipeline

A test executes the pipeline end-to-end with a sample row limit.

### 6.1 Via the UI

- Click **Run** in the canvas top bar.
- Watch the live log stream at the bottom.
- On success → status becomes `PUBLISHED`, the test summary is saved to the pipeline.
- On failure → the error is shown inline with suggested fixes.

### 6.2 Via the API

```bash
curl -X POST "http://localhost:8001/api/workflows/{id}/test?preview_limit=50"
```

`preview_limit` caps rows read from each source. Default 50 in dev sample mode, unbounded in full mode.

> ⚠️ **Tests run against real connections.** A source node with a Postgres connection will actually query Postgres. Use a dev/staging database for testing.

### 6.3 Run safety modes

The toolbar **Run** button has four safety modes:

| Mode | What it does |
|---|---|
| **Live** | Run on full upstream data and write to configured destinations |
| **Sample** | Run on the first 100 rows; no effect on destinations |
| **Dry-run** | Plan only — validate the IR and produce previews without writing |
| **Validate-only** | Schema + connection sanity check; no execution |

---

## 7. Scheduling and triggers

A published pipeline can run by itself in four ways. The first three are set up
on the **Pipelines** page: click **+ Schedule** (or the schedule shown) on the
pipeline's row, then choose **What starts it**. The fourth is on the
**Triggers** page.

| What starts it | Use it when |
|---|---|
| **A timetable** | It should run at set times: every 15 minutes, nightly, on weekdays. |
| **A file arriving** | Another system drops files in a folder, on this server, an SFTP server, a Windows file share, Amazon S3, Google Cloud Storage or Azure Blob Storage. |
| **Another pipeline finishing** | It depends on another pipeline's output, or should clean up after one fails. |
| **Another system calling it** | An application, a job scheduler or a webhook sender says when. See [Triggering pipelines from external systems](external-triggers.md). |

Whatever starts it, the run:

- uses the **published version** only. A pipeline that is not published is never
  started by a schedule or trigger, and a draft is never run in its place;
- is admitted through the same pool and resource governor as a run started by hand;
- keeps the pipeline's **run time limit** and **overlap** setting (Save with
  details → Execution);
- appears on the **Executions** page with what started it: *Scheduled*,
  *File arrived*, *After another pipeline*, *API* or *Webhook*.

Unpublishing a pipeline, or switching it off, pauses all of these. Schedules
and triggers are managed through `/api/schedules/*` (see the
[API reference](../api.md#schedules)).

### 7.1 On a timetable

Choose every N minutes, every hour, daily or weekly. Behind the screen a
schedule is a cron expression. Common patterns:

| Cron | Meaning |
|---|---|
| `0 2 * * *` | Nightly at 2 AM |
| `*/15 * * * *` | Every 15 minutes |
| `0 0 1 * *` | First of each month at midnight |
| `0 9 * * 1-5` | 9 AM on weekdays |
| `0 0 * * 1` | Midnight every Monday |

The last field is the day of the week, as in standard cron: 0 or 7 is Sunday, 1 is Monday, and 6 is Saturday. Times and days are in the time zone set on the schedule.

Through the API a schedule can also be an interval, daily or weekly
(`schedule_type`), without writing cron.

### 7.2 When a file arrives

Each new file in a folder starts one run of the pipeline, and the run is told
which file it is for.

**Set it up.** Choose **A file arriving**, then:

1. **Where the files arrive.** A folder on the Flow server, an SFTP server, a
   Windows file share, Amazon S3 (or MinIO), Google Cloud Storage, or Azure Blob
   Storage. For all but the first, choose a saved connection; the server, share,
   bucket or container and the sign-in come from it. An SFTP server saved as an
   FTP connection works when its protocol is SFTP. A Windows file share is a
   **Windows File Share (SMB)** connection (see
   [Mainframe and older files](mainframe-and-legacy-files.md#files-on-a-windows-file-share)).
2. **Folder.** On the Flow server, a full path or a folder inside Flow's data
   folder. On a Windows file share, a folder inside the connection's folder:
   leave it empty for that folder, or start with `\` for the top of the share.
   Elsewhere, a folder on the SFTP server, or a key prefix in the bucket or
   container (empty for the top). Only files directly in it count, not those in
   folders inside it.
3. **File names.** A pattern such as `orders_*.csv`, where `*` stands for any
   characters. Case does not matter. Several patterns can be separated with `;`,
   as in `*.csv;*.txt`.
4. **Give the file's path to.** The pipeline parameter that receives the path.
   Add a text parameter such as `file_path` in the Editor (**Parameters**), and
   use `${param.file_path}` as the Source step's path. The path is in the form
   that Source reads: a full path on the Flow server, a path on the SFTP server,
   the file's full path on a Windows file share
   (`\\fileserver01\mainframe\outbound\daily\CUSTMAST.DAT`), or the object's key
   in the connection's bucket or container. On a share, the Source step must use
   the same connection: a path on another share is refused rather than read.
   Without a parameter, the pipeline runs as saved and has to find the files
   itself.
5. **Wait.** A file must keep the same size and time for this long (one minute
   unless changed) before its run starts, so a file still being copied in is not
   read half-written. A file is always seen at least twice first. On a Windows
   file share, the folder can show a file's final size before the copy has
   finished, so just before the run starts Flow also opens the file the way the
   File step will; while the program writing it still has it open, the file
   waits another round.
6. **Look for new files.** Every 30 seconds to every hour; every minute unless
   changed.

**Check the folder now** lists the matching files there at that moment, so you
can see the connection, folder and pattern are right before saving.

**What counts as new.**

- Files already in the folder when you save are left alone, unless you tick
  **Also run for files already in the folder**. A file changed after you saved
  counts as new. Changing the folder or the pattern later takes a new look at
  what is already there.
- Each file runs once. What was seen is kept in Flow's database, so a restart,
  or a second Flow server, never starts a file twice.
- A file replaced by a new version (new content tag in a cloud store, or a new
  size or time) is a new file and starts its own run.
- A file that left the folder is forgotten after 30 days; if it comes back
  after that, it counts as new.

**How the runs go.** One run at a time for each trigger, oldest file first. A
file that arrives during a run waits, and starts as soon as the run before it
ends. A run that fails is not repeated: open it on the **Executions** page and
use **Replay**, which uses the same file. If the pipeline is not published,
is paused, or its overlap setting says to wait, nothing is used up: the files
wait, and the trigger's line in the Schedule dialog says why. It also shows
when the folder was last checked and what was found, and **Check now** looks at
once instead of at the next check.

The run's record on the Executions page keeps the file's path, size and time.

### 7.3 After another pipeline

Choose **Another pipeline finishing**, the pipeline to run after, and when:
it **Succeeds**, **Fails**, or **Finishes either way**.

- Runs of the other pipeline that nobody is watching count: those started by a
  schedule, a file, another pipeline, or another application through a
  published endpoint. Runs someone starts by hand (the Run button, a replay, a
  test, a backfill) do not, and neither does a run someone stops. A run stopped
  by its time limit is a failure.
- If this pipeline is still running when the other finishes again, it runs once
  more afterwards, not once for each finish.
- A loop is refused when you save it, naming the pipelines in it (A runs after
  B, B after C, C after A). A chain of more than 25 pipelines stops too.
- If this pipeline is not published or is paused when the other one finishes,
  it is not started, and its line in the Schedule dialog says so.

The run's record keeps which run of the other pipeline started it.

### 7.4 When another system calls it

An endpoint on the **Triggers** page gives a pipeline a web address. Callers
prove who they are with an **API key**, a **signature** made with a shared
secret (for webhooks), or both. A signed call that was changed, was already
received, or is more than 5 minutes old is refused. See
[Triggering pipelines from external systems](external-triggers.md).

---

## 8. Monitoring execution

Every pipeline run creates an **ExecutionRecord** with:

- `id`, `workflow_id`, `workflow_name`, `project_id`
- `status` — `success`, `error`, `timeout`, `cancelled`
- `triggered_by` — `manual`, `schedule`, `file_arrival`, `after_pipeline`, `api`, `webhook`, `test`, and others such as `replay:<id>`
- `started_at`, `completed_at`, `duration_ms`
- `steps_total`, `steps_completed`, `steps_failed`
- `step_logs[]` — per-step log with row count, duration, error
- `rows_processed_total`
- `workflow_snapshot` — the full Workflow IR at execution time

Access via the **Executions** page (filter by workflow, status, time range) or the API:

```bash
curl "http://localhost:8001/api/executions?workflow_id=wf_..."
```

### 8.1 Cancelling a running pipeline

```bash
curl -X POST http://localhost:8001/api/admin/execution/{handle_id}/cancel
```

The execution's subprocess is sent SIGTERM, then SIGKILL after the grace period.

### 8.2 Logs

Per-step logs are kept as `StepLog` records on the execution and shown in
**Executions → a run → Logs**. Captured output samples are stored separately
in the `step_outputs` store and pruned after 30 days.

> An earlier version of this page described a 1 MB truncation limit, a
> `REVOLUZEN_FLOW_STEP_LOG_MAX_MB` setting and raw logs on disk under
> `exec_logs/`. None of the three exists in the codebase — there is no
> size cap to tune and no such directory.

---

## 9. Archiving, restoring and deleting

Archive takes a pipeline out of use while preserving all versions and history. In the app: Pipelines page → row → **⋯** menu → **Delete** → **Archive**. The pipeline stays in the list with the status Archived (it also has its own **Archived** tab), and **Restore** on its row brings it back as a draft. The same through the API:

```bash
curl -X POST http://localhost:8001/api/workflows/{id}/archive
curl -X POST http://localhost:8001/api/workflows/{id}/restore
```

An archived pipeline cannot be tested, and it keeps all its version rows + execution history. Schedules become inert. Restore to `DRAFT` at any time.

A published pipeline cannot be archived or deleted, by anyone or through the API, until it is unpublished (row **⋯** menu → **Unpublish**). Unpublishing puts it back to draft and pauses its schedules. See [12.3](#123-published-pipelines-are-locked).

### 9.1 The Bin

**Delete is not Archive.** **Delete** (same dialog) moves the pipeline to the **Bin**, a tab on the Pipelines page. It leaves the list, its schedules are removed, and its saved versions are kept. Its past runs stay in Executions.

A pipeline stays in the Bin for **30 days**, then a daily job removes it for good. Your administrator can change the number of days, or turn the automatic removal off, with `REVOLUZEN_FLOW_BIN_RETENTION_DAYS` (`0` keeps everything until someone deletes it from the Bin). The Bin tab says how many days apply, and the **Removed for good** column says how long each pipeline has left.

In the Bin, each pipeline has two buttons:

- **Restore** brings it back as a **draft**, with all its saved versions and a "Restored from the Bin" note in its history. It does not come back published, even if it was before; publish it again when you are ready. Schedules it had come back **paused**, so nothing starts running by surprise. If its name has been taken in the meantime it comes back as "Name (2)", and a project or folder that no longer exists is replaced by Default or the project root. A toast lists each of these changes.
- **Delete forever** removes it from the Bin at once. This cannot be undone.

Admins also see **Empty Bin**, which removes everything in the Bin for good.

Deleting, restoring and removing from the Bin need the Developer role or above; emptying the Bin needs Admin. Anyone who can see the project can see its pipelines in the Bin.

A pipeline that reads a database change feed (CDC) gets a new replication slot when it is restored. It does not replay the changes made to the source while it was in the Bin.

The same through the API:

```bash
curl -X DELETE http://localhost:8001/api/workflows/{id}                 # into the Bin
curl http://localhost:8001/api/workflows/bin                            # what is in the Bin
curl -X POST http://localhost:8001/api/workflows/bin/{id}/restore       # back as a draft
curl -X DELETE http://localhost:8001/api/workflows/bin/{id}             # delete forever
curl -X DELETE "http://localhost:8001/api/workflows/{id}?permanent=true"  # skip the Bin (API only)
```

Archive is the other way to take a pipeline out of use: it stays in the list as Archived and keeps its place in the project, and it is never removed automatically. Choose Archive when you expect to use the pipeline again; choose Delete when you are finished with it.

---

## 10. Cloning a pipeline

Cloning creates a new pipeline with a fresh ID, copying all steps and connections but none of the history.

```bash
curl -X POST "http://localhost:8001/api/workflows/{id}/clone?name=Orders+ETL+v2"
```

Use clone when you want to iterate on a variant without affecting the original.

---

## 11. Exporting and importing

Export produces portable JSON that can be imported into any Revoluzen Flow instance.

### 11.1 Export

```bash
curl http://localhost:8001/api/workflows/{id}/export > orders-etl.json
```

Internal IDs are stripped. Connection references are kept as **names**, not IDs, so the importer can remap them.

### 11.2 Import

```bash
curl -X POST http://localhost:8001/api/workflows/import \
  -H "Content-Type: application/json" \
  -d '{
    "pipeline": { /* the exported pipeline object */ },
    "rename": "Orders ETL (imported)",
    "connection_map": {
      "orders-dev-pg": "conn_abc",
      "s3-dev-bucket": "conn_xyz"
    }
  }'
```

The `connection_map` replaces connection-name references with connection IDs. If you omit it, the import still works but source/sink nodes will be unconfigured until you set connections manually.

---

## 12. Versioning model

Every save that changes a pipeline creates a new version row in `workflow_versions`. A save that changes nothing creates none.

- `version` — monotonically increasing integer per workflow
- `created_by`, `created_at`, `change_summary` — who saved it, when, and the note
- `kind` — what made it: `saved` (Save, Import, a rename), `auto` (auto-save and the save before a test or run), `restored` (an older version saved again) or `review` (submitted, approved, rejected, deployed)
- `version_name` — a name someone gave it, such as "Go-live"
- `data` — the full Workflow IR as JSON
- `content_hash` — SHA-256 of the canonicalised IR (v15+)

### 12.1 The Versions view

Open a pipeline on the Pipelines page and choose **Versions** at the top of its tab (or **Version history** in its row's **⋯** menu).

- The list shows every kept version, newest first: its name or note, who saved it and when, and how many runs used it. Auto-saves in a row fold into one line. The current version is marked, and so are the one that is live (what schedules and endpoints run), the published versions and, in Flow Enterprise, the one live in PROD.
- Clicking a version compares it with the current one. **On the canvas** marks each step added, changed or removed (a removed step stays faded where it was); click a step to see its settings in both versions. **List** shows every change: steps, the links between them, and pipeline settings such as parameters. Either version can be changed at the top. Schedules are kept apart from versions, so they are not compared.
- **Open** shows the version read-only in a tab of its own (`#pipelines/<id>~v<N>`).
- **Restore…** opens the version in the Editor as unsaved changes on the pipeline's tab. Auto-save leaves it alone; test it, then **Save** to make it the newest version, noted "Restored version N". Nothing changes until you save, and the versions after it are kept. On a published pipeline that save is a draft, and the live version keeps running until you publish it.
- **New pipeline** copies the version into a new draft in the same project and folder.
- The **⋯** menu names a version (named versions are never removed) or downloads it as a file, the same file Export makes.

### 12.2 How many versions are kept

Always kept while the pipeline exists: the current version, named versions, published versions, the live one, the one deployed to PROD, and review steps. Of the rest, Flow keeps the newest 50 saves (`REVOLUZEN_FLOW_VERSION_RETENTION`) and the newest 10 auto-saves (`REVOLUZEN_FLOW_AUTOSAVE_RETENTION`); 0 means no limit. The Versions view says how many older versions have been removed. Versions saved before this release count as saves, because their notes cannot tell a Save from an auto-save.

| Endpoint | Effect |
|---|---|
| `GET /api/workflows/{id}` | Returns latest version |
| `GET /api/workflows/{id}?version=N` | Returns version N |
| `GET /api/workflows/{id}/versions` | Lists all versions |
| `GET /api/workflows/{id}/history` | The Versions view: kept versions newest first, with kind, name, author and runs; how many were removed; the keeping rules |
| `PUT /api/workflows/{id}/versions/{N}/name` | Names version N (`{"name": "..."}`; empty removes the name) |
| `GET /api/workflows/{id}/export?version=N` | Exports version N as a file |
| `GET /api/workflows/{id}/diff?v1=N&v2=M` | Diffs two versions |

Versions are **immutable**: a name is the only thing that can change on one, and it does not change the fingerprint. Deleting the workflow moves all its versions to the Bin (see [9.1](#91-the-bin)), and they are removed for good when the pipeline is; individual versions cannot be deleted. A restore from the Bin adds a version noted "Restored from the Bin".

### 12.3 Published pipelines are locked

Publishing makes one version **live**. From then until the next publish, schedules, endpoints, backfills and pipelines that run this one as a step all use exactly that version.

- **Saving does not change what is live.** In the Editor, **Save** on a published pipeline makes a draft beside it, and the Pipelines list marks it **Draft changes**. The same goes for any other change, such as one made by the Copilot.
- **Run** in the Editor, Test Node and Sample use what is on the canvas. **Run** on the Pipelines page uses what is live.
- **Publish changes** tests the draft on a sample and puts it live. **Discard draft** goes back to what is live, and the draft stays in Versions.
- Renaming a pipeline, moving it, editing its README or switching it off changes no step, so none of those make a draft.
- A published pipeline cannot be archived or deleted until it is unpublished. Neither can a connection, credential, managed table, uploaded file, saved connector or pipeline that its live version uses: the message names the pipelines that use it. Editing a connection is still allowed, and its History records each change.
- Publish, Unpublish and Archive need at least the Developer role. Deploy and Rollback need an admin.

### 12.4 A copy in Git

Versions live in Flow's own database, so they cannot help if that database is lost. To keep a copy somewhere else, an admin opens **Settings → Security & Diagnostics → Keep a copy of published pipelines in Git**, and enters the repository's https address, a branch, a folder and an access token that can write to it. The `git` program has to be installed on the server (2.31 or newer); the Docker image includes it.

Each publish, unpublish, deploy and rollback becomes a commit, with the person who did it as the author and the note from the Publish dialog in the message. Each pipeline has a folder, `<folder>/<project>/<pipeline>--<id>/`, holding:

| File | What it holds |
|---|---|
| `pipeline.json` | The published definition, in the same form as Export, so it can be imported to put the pipeline back |
| `depends-on.json` | The connections (without passwords), the names of credentials, the tables, files and pipelines it uses, and its schedules |
| `README.md` | What it is for, who published it, when and why, and its steps |

Passwords, keys and tokens are never written. Secrets are masked first, and then the whole copy is refused if anything that looks like one is still in it. The access token is stored encrypted, is never shown again, and is kept out of the git command line and config.

Copies are made in the background, so a publish never waits for Git and never fails because of it. The pipeline's page shows how its copy went. A failed copy is tried again over the following day, and **Try again now** does it at once. To include what was published before this was set up, click **Copy everything already published**.

To put a pipeline back, import its `pipeline.json` on the Pipelines page. It comes back as a draft. Add the connections listed in `depends-on.json`, then test and publish it.

---

## 13. API reference

Key endpoints (full list in the [API reference](../api.md)):

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/workflows/` | List workflows |
| `POST` | `/api/workflows/` | Create a workflow |
| `GET` | `/api/workflows/{id}` | Get latest version |
| `PUT` | `/api/workflows/{id}` | Save a new version |
| `DELETE` | `/api/workflows/{id}` | Delete: moves the workflow and all its versions to the Bin (`?permanent=true` skips the Bin) |
| `GET` | `/api/workflows/bin` | List the Bin |
| `POST` | `/api/workflows/bin/{id}/restore` | Restore from the Bin, as a draft |
| `DELETE` | `/api/workflows/bin/{id}` | Remove one workflow from the Bin for good |
| `DELETE` | `/api/workflows/bin` | Empty the Bin (admins) |
| `GET` | `/api/workflows/{id}/versions` | List all versions |
| `GET` | `/api/workflows/{id}/history` | Version history for the Versions view |
| `PUT` | `/api/workflows/{id}/versions/{N}/name` | Name a version |
| `POST` | `/api/workflows/{id}/validate` | Validate structure |
| `POST` | `/api/workflows/{id}/test` | Run a test |
| `POST` | `/api/workflows/{id}/archive` | Archive |
| `POST` | `/api/workflows/{id}/restore` | Restore from archive |
| `POST` | `/api/workflows/{id}/clone` | Clone a pipeline |
| `GET` | `/api/workflows/{id}/export` | Export as portable JSON |
| `POST` | `/api/workflows/import` | Import from portable JSON |

---

## 14. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| "Test execution failed: connection refused" | Source/sink connection unreachable | Test the connection under **Connections → Test Connection** |
| "Cannot test an archived pipeline" | Pipeline is in ARCHIVED state | `POST /{id}/restore` first |
| "Pipeline has not been tested yet" | Test has never passed | Run `/test` at least once |
| Pipeline saves but won't run on schedule | Pipeline is in DRAFT (test never passed) | Run a successful test first |
| Schedule doesn't fire on time | System clock skew or scheduler stalled | Check the **Pool** page Governor banner |
| "Cannot rollback to vN: content hash mismatch" | The version's `data` row was modified post-save | This is a tamper-evidence signal — investigate |
| Imported pipeline has unconfigured connections | `connection_map` was incomplete | Edit each source/sink node and pick a connection |

---

## 15. Revoluzen Flow Enterprise: governance & approvals

Revoluzen Flow Enterprise adds production governance on top of the OSS lifecycle:

| Feature | Effect |
|---|---|
| **DEV/PROD environments** | Pipelines exist in DEV by default. Promotion to PROD requires the approval workflow below. |
| **Submit for review** | Sets `approval_status = "pending"`, notifies approvers via in-app + email/Slack/Teams |
| **Plan stage** | Diff modal showing exactly what will change vs the currently-deployed version, with baseline run statistics |
| **Two-gate approval** | Gate 1: Sandbox approval — run against PROD connections in an isolated namespace. Gate 2: Deploy approval — review sandbox evidence and approve. |
| **Pinned `deployed_version`** | PROD schedules execute the approved version, not "whatever is latest" |
| **Pre-deploy checklist** | 8+ automated checks: structural validation, approval, test history, connections mapped, schedule set, alerts configured |
| **Rollback with hash verification** | Roll back to any prior version; the stored content hash is re-verified before pinning |
| **Approval gates** | Per-pipeline / per-project / global approver lists; supports two-person rule |
| **Activate/Deactivate** | In PROD, a request goes to an approver. In DEV, and on the free edition, a row's ⋯ menu switches it directly: while paused, schedules are skipped and endpoints answer 423, and it can still be run by hand |
| **Audit log** | Every approval, deploy, rollback, activate event is persisted with retention controls |

[Learn more about Revoluzen Flow Enterprise →](https://revoluzen.io/flow#revoluzen-flow-plus)

---

**Document change log**

| Date | Change |
|---|---|
| 2026-05-03 | Rewritten for Revoluzen Flow audience. DEV→PROD approval flow + admin-tier features consolidated into section 15 (Revoluzen Flow Enterprise section). |
| 2026-04-22 | Initial publication. |
