# Changelog

All notable changes to Revoluzen Flow are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

API stability policy: see `docs/api.md`. The HTTP API surface under `/api/*`
follows the compatibility rules documented there.

## [Unreleased]

### Added

- **Start a pipeline when a file arrives.** In a pipeline's Schedule dialog,
  "A file arriving" watches a folder on the Flow server, on an SFTP server, on a
  Windows file share, in Amazon S3 (or MinIO), Google Cloud Storage or Azure Blob
  Storage. Each new file
  whose name matches the pattern starts one run, with the file's path in the
  parameter you choose. A file must stop changing for a set time before its run
  starts, so one still being copied is not read half-written. What was seen is
  remembered in the database, so a file never starts two runs, across restarts
  and servers; a file replaced by a new version starts its own. Files already in
  the folder are left alone unless you ask for them. Runs go one at a time, oldest
  file first. "Check the folder now" shows what the trigger would see before you
  save.

- **Run a pipeline after another one.** "Another pipeline finishing" starts it
  when another pipeline succeeds, fails, or finishes either way. Runs started by
  a schedule, a file, another pipeline or another application count; runs started
  by hand do not. A loop is refused when it is saved, naming the pipelines in it,
  and again at run time.

- **Signed calls to published endpoints.** An endpoint can require each call to
  be signed with a secret Flow makes, shows once and keeps encrypted, in the open
  Standard Webhooks format (`webhook-id`, `webhook-timestamp`,
  `webhook-signature`). A changed call, a message already received, or one more
  than 5 minutes old is refused. Works with or without an API key, and the
  Triggers page writes the signing command for a terminal or PowerShell.

  All three only ever run a published pipeline, at its published version, and
  follow its overlap setting and run time limit. See
  `docs/user-guides/pipelines.md` (Scheduling and triggers) and `docs/api.md`.

- **Windows file shares.** A new connection, **Windows File Share (SMB)**, reads
  and writes `\\server\share` files with a saved sign-in (domain, user name,
  password), the same whether Flow runs on Windows, Linux or Docker. Pick it in a
  Source or Destination and the File step does the rest, so every format it reads
  works on a share, including fixed-width text, COBOL copybook records, EBCDIC and
  PGP-encrypted files; `*` and `?` in a file name read every matching file. A file
  is written under a temporary name and renamed once complete, so a program
  watching the folder never picks up half of it. Test connection signs in and
  lists the folder. A file landing on a share can start a pipeline: choose "A
  file arriving" and "A Windows file share" in the Schedule dialog, and each new
  file starts one run with its full `\\server\share\...` path, which the File
  step reads with the same connection. A file that the program writing it still
  has open waits until it is closed. The client, `smbprotocol` (MIT, with
  `pyspnego` and on Windows `sspilib`), now ships with Flow, and the connector
  count is 67. See `docs/user-guides/mainframe-and-legacy-files.md`.

- **Kafka with a company's own certificate authority and client certificates.** A
  Kafka connection that uses `SSL` or `SASL_SSL` now takes a CA certificate, so
  Flow can reach brokers whose certificates the company signs itself, and a client
  certificate with its private key (and the key's password, if it is locked) for
  brokers that ask each client for one. They are pasted as text, so there is no
  file to copy to the Flow server, and they reach both Kafka client libraries in
  the Kafka Source, the Kafka Sink, Test connection and topic browsing. A
  certificate that cannot be read, a wrong key password or a key that does not
  belong to its certificate is named before Kafka is contacted. The private key and
  its password are kept encrypted, never sent back to the browser and never
  repeated in a message or a log line. Until now `SSL` trusted only the
  certificates the computer trusts, and no client certificate could be given. See
  `docs/nodes-and-canvas-reference.md` (Kafka Source).

- **Reports you design and schedule.** Insights > Reports used to offer one fixed
  document. Now you choose the sections (runs, failures, successes, usage, storage,
  AI use, environment, new builds, schedules, alerts, or a table you design), narrow
  them to the period, projects and pipelines you care about, and save the report.
  A saved report can be sent daily, weekly or monthly by email, Slack, Teams or
  webhook, using the existing scheduler and notification settings. Each report keeps
  a history of what went out, to whom, and why anything failed. The fixed inventory
  document stays, as a starting point on the same page. See
  `docs/user-guides/reports.md`.

- **The Bin for deleted pipelines.** Deleting a pipeline now moves it to the Bin
  tab on the Pipelines page instead of removing it. It keeps its saved versions
  and stays for 30 days (`REVOLUZEN_FLOW_BIN_RETENTION_DAYS`; `0` means never
  removed automatically), then a daily job removes it for good. The Bin lives on
  the server, so everyone in the workspace sees the same list, and the tab reads
  it again each time it is opened: a pipeline someone else deleted or restored in
  the meantime shows without reloading the page.

  **Restore** brings a pipeline back as a draft, never published, with its
  schedules back but paused. A name taken in the meantime gets a "(2)" suffix, and
  a project or folder that is gone is replaced by Default or the project root; the
  toast says which. **Delete forever** removes one at once, and admins can
  **Empty Bin**. Deleting, restoring and removing need the Developer role, and
  emptying needs Admin. Deleting a folder together with its pipelines goes to the
  Bin too.

  The API gains `GET /api/workflows/bin`, `POST /api/workflows/bin/{id}/restore`,
  `DELETE /api/workflows/bin/{id}` and `DELETE /api/workflows/bin`.
  `DELETE /api/workflows/{id}` now answers `{"deleted": true, "binned": true, ...}`,
  and `?permanent=true` skips the Bin for scripts that must leave no copy (the app
  has no button for it). Delete, restore and purge are recorded in the audit log.
  A CDC pipeline gets new replication slots on restore and does not replay changes
  made to its source while it was in the Bin.

- **Triggers page.** Workflows → Triggers lets another application start a
  pipeline and pass it values: endpoints, API keys, and a ready-to-use command.
  The runs appear on Executions with the trigger API, and the inventory report
  lists endpoints and masked keys.

- **Credential expiry warnings.** Give a credential an expiry date and
  administrators are warned on the bell and by email a week ahead, a day ahead
  and when it expires. Settings → Notifications → Credential expiry warnings
  turns them off.

- **Saved credentials fill a connection's fields.** Pick one under Saved
  credential and the fields it holds fill themselves. A SaaS Connector step takes
  a saved credential too.

- **The Copilot asks before drafting.** It asks where the data comes from and
  where it goes, from the workspace's real connections, files and tables, and a
  missing connection can be created in the chat.

- **`inspect_step_columns` Copilot tool.** Lists the columns a step receives, as
  names and types only, so the agent can fix a renamed column instead of
  guessing from an error message. It refuses if an upstream step could have a
  side effect. The agent also gets guidance on repairing pipelines: draft instead
  of describing, never guess a column, and leave a failing check in place.

- **Chained pipeline drafts.** `modify_pipeline_step` takes a `draft_id` to build
  on an earlier edit, so a fix can span several steps. The editor now tells the
  agent every step's id, which it needs to edit more than the selected step.

- **Repair evaluation for the Copilot.** `python -m revoluzen_flow.eval.repair`
  runs broken pipelines through the real agent loop, then runs each draft on the
  real engine against held-out data and answers computed in plain Python. It
  scores repairs, whether failing checks were left alone, honest refusals and
  false claims. CI gates the scripted ideal assistant at 100% and replays
  recorded models. See `docs/repair-evaluation.md`.

- **The Copilot refuses to guess when repairing a pipeline.** An edit that
  picks between columns, invents a replacement for one that is gone, or points a
  source at something nobody named is refused until the person says which one.
  Edits in one request now build on each other, a repair gets ten steps and must
  end with an answer, and a refusal reaches the model whole. A draft test now
  fails on a value that only breaks when rows are read.

- **The Copilot repairs from column names and types only.** It cannot see row
  values, and an edit that writes a label, number or date nobody gave is refused.
  A draft test's verdict is now `runs` rather than `passed`, and `output_checked`
  says whether anything looked at the output.

- **More than one instance is now safe to run.** Two additions, together:

  `REVOLUZEN_FLOW_ENABLE_SCHEDULER=0` makes a node API-only — it serves
  requests without running the scheduler loop. Defaults on, so a single-node
  install is unchanged.

  `scheduling/lease.py` holds a durable single-row lease in the metadata
  database; only the holder fires schedules. Acquire and renew are one
  conditional `UPDATE` so two instances cannot both win between a read and a
  write, the TTL is three tick intervals, and the holder renews every tick.
  A clean `stop()` releases the lease so a standby takes over on its next
  tick rather than waiting out the TTL.

  Previously the scheduler tracked in-flight work in process-local dicts and
  started unconditionally on every instance. Those dicts stop one process
  double-running a schedule but say nothing about a second process, so two
  instances behind a load balancer fired **every schedule twice** — silently,
  with both runs looking legitimate. Verified with three instances over ten
  ticks: ten firings, all from one node, where the old behaviour would have
  produced thirty.

  The lease fails closed. An instance that cannot reach the lease row does
  not schedule, because missing a tick is recoverable and already alerted on
  by schedule-miss detection, while a duplicate run can double-charge a
  customer or duplicate rows in their warehouse.

  Supported topology is now N API instances plus whichever one holds the
  lease. A worker pool spanning machines is still not supported — see
  `edition-matrix.md`.

- **Edition gate** — `revoluzen_flow/licensing.py` provides `is_plus()` and
  `require_plus(feature)`, the first runtime enforcement of the Plus-only node
  declarations in `edition-matrix.md`.

  Those declarations previously had nothing behind them: CDC is listed
  Plus-only in the edition matrix, in `ai/session_context.py` and in
  anti-pattern rule 25, but `connectors/cdc.py` registered
  `StepType.CDC_SOURCE` with no licence check and ran on free instances. No
  node-level gate existed anywhere to follow — the `is_plus` checks in the
  codebase were all in AI tools and catalog listings.

  Detection fails closed: any import failure, missing `app_state` or
  partially initialised licence manager reads as free, because an edition
  check that defaults to paid when something breaks is not a check.
  `PlusRequiredError` is its own type so the UI can offer an upgrade path, and
  `engine/failure_class.py` maps it to `USER_INPUT` so the retry policy stops
  — no number of attempts produces a licence.

  The gate runs at **execution**, not registration, so a free instance still
  shows the node and can build a pipeline against it. The catalog stays
  identical across editions and the refusal names the feature when it runs.

  CDC is gated. Vector DB sinks and the JDBC dialect registry were also
  declared Plus-only with no enforcement; rather than add the gate, both
  moved to OSS on 2026-10-23 — see below.

### Changed

- **Two things that were declared but never built are gone.** The schedule type
  `event`, with `event_trigger` and `event_source_id`, never fired; the API no
  longer takes it. A row saved with it is read as the trigger it described
  ("pipeline_completed" becomes "after another pipeline"), switched off, for
  someone to check and turn on. The step type `webhook_trigger` had no
  implementation and left the palette in May; it has left the step types too.
  A saved pipeline that still holds one opens as before, rewritten to an API
  source. Another system starts a pipeline through a published endpoint, which
  can now require signed calls.

- **Vector DB sinks and the JDBC dialect registry are now OSS.** Both were
  listed Plus-only and neither was ever enforced, so the choice was to add a
  gate or drop the claim. Dropped it.

  OSS already ships the entire local RAG stack — Ollama embeddings and a
  SQLite-vec store — so charging to write pipeline output to a vector
  database while embedding freely for our own features was the row a buyer
  attacks first. The JDBC registry is the extensibility mechanism, and
  Postgres, MySQL, MSSQL, SQLite and DuckDB all reach their databases
  without touching it; gating it protected no revenue and only stopped
  someone with an unusual database helping themselves. The *maintained*
  enterprise dialects shipped through it (SAP HANA, Teradata, DB2, Informix)
  stay Enterprise.

- **SSO is designated OSS**, and the matrix now says `planned` rather than
  implying it ships — OIDC/SAML is not implemented in this repository at
  all. Enterprise keeps SCIM provisioning and directory sync. The audit-log
  row is clarified: the record, and the ability to read it, are OSS;
  retention policy, signed export and SIEM formats are Enterprise.

- `edition-matrix.md` gains a **"which side does a new feature go on?"**
  test, so the split is decided by rule rather than case by case. The
  mechanism and the interface are OSS; maintained integrations,
  administration-at-scale and governance over third parties are Enterprise.
  Never gate correctness, and never gate security basics.

- **ELT on warehouses and the lakehouse.** `execute_sql_task` — the node that
  fires SQL at a real connection rather than transforming in-memory — now
  reaches `redshift`, `mssql`/`synapse`, `snowflake`, `bigquery` and
  `databricks`, alongside the existing postgres, mysql/mariadb and sqlite.

  Previously the matrix stopped at Postgres, MySQL and SQLite, so Revoluzen
  Flow could *load into* Snowflake, BigQuery, Redshift and a lake but not
  *transform in* them — the half of ELT that people mean by the word. The
  dialect matrix is deliberately one function shared by Execute SQL and the
  Lookup activity, so all of these arrived in one place.

  This completes a Medallion pipeline end to end: land bronze as Parquet with
  `adls_gen2_sink` or `s3_sink` (both emit `FORMAT PARQUET`, and parquet is
  the default), build silver and gold with SQL running on Databricks or the
  warehouse itself, then serve to SQL Server via `db_sink`, which routes
  through the bulk loader. The intermediate layers never travel back through
  the engine.

  Drivers stay optional extras — `pip install revoluzen-flow[databricks]` and
  friends — and a missing one now reports the install command rather than an
  ImportError naming an unfamiliar module. Redshift needs no new dependency;
  it is Postgres wire-compatible and uses psycopg2, differing only in its
  default port.

### Security

Fixes from an internal penetration test. Upgrade before exposing an instance
to anyone but yourself.

- **Roles are enforced on every route that runs or authors a pipeline.**
  Running, replaying, testing and previewing pipelines, editing or testing
  connections, uploads, imports, deployments and backfills now need the
  developer role or above; approving needs an admin, and the approver is the
  signed-in user, never a name sent with the request.
- **Pipeline SQL can no longer reach the instance's own secrets.** In server
  mode, DuckDB runs with an allowlist: the data folder's working folders and
  the files a pipeline names. The master key, the app database, backups and
  the config folder are never on it. `REVOLUZEN_FLOW_SQL_SANDBOX=1|0` forces it
  on or off. A SQL Transform must be a single statement.
- **Secrets stay masked.** The notification settings (SMTP password, webhook
  URLs) need an admin and never return secrets; secret variables are masked in
  every response.
- **Anonymous access is closed in server mode** for notifications, run
  history and system information, and the Docker image and compose file now
  start in server mode. A local-mode instance refuses requests that arrive
  over the network, however it was started.
- **The first account on a server needs a one-time setup token**, printed in
  the log at startup and saved as `SETUP_TOKEN.txt`.
- **Password-reset links** use `REVOLUZEN_FLOW_PUBLIC_URL` only, never the
  request's own headers.
- **Cross-site requests** with an opaque (`null`) origin are refused on a
  local install, and connection tests no longer block the server.
- Workspace admins stay in their own workspaces, admins cannot grant a role
  above their own, comments are tied to their author and workspace, and
  `/register` no longer reveals which emails have accounts.

### Fixed

- **A Kafka Source read the same messages on every run.** Both Kafka readers
  turned off Kafka's own position saving and never kept a position themselves,
  so each run started again at the oldest message the topic still held, and an
  append destination filled with copies. Each run now carries on after the last
  message the previous successful run read. Flow keeps that position for each
  pipeline step and saves it only when the whole run succeeds, so the messages of
  a failed run are read again rather than lost: after a failure a message can
  arrive twice, but none is skipped. Sample runs and Test Node leave the position
  alone. The step no longer joins a consumer group, so two pipelines reading one
  topic each get every message. A run also uses the brokers saved in the Kafka
  connection (it ignored them and went to `localhost:9092` unless the step named
  other servers), stops once it has caught up instead of waiting out its timeout,
  and stops with a plain message when the topic does not exist or Kafka cannot be
  reached, where it used to succeed with no rows. See
  `docs/nodes-and-canvas-reference.md` (Kafka Source).

- **Kafka steps never signed in, so a secured cluster could not be used.** A Kafka
  connection's security protocol, SASL mechanism, username and password reached no
  Kafka client: the Kafka Source, the Kafka Sink, Test connection and topic
  browsing all connected without signing in. They now sign in with `PLAIN`,
  `SCRAM-SHA-256` or `SCRAM-SHA-512` over `SASL_PLAINTEXT` or `SASL_SSL`, with
  either Kafka client library, and a refused sign-in stops the run with what Kafka
  said. The password never appears in a message or a log line. The Kafka Sink also
  read only the older `bootstrap_servers` key, so with a saved connection it wrote
  to `localhost:9092`; it now uses the connection's brokers. It also finished as if
  every message had gone even when Kafka took none; it now checks each message and
  stops the run, saying how many were not written. Topic browsing now works with
  the confluent-kafka add-on too.

- **The Kafka forms offered settings that runs ignored.** The Kafka Source's
  Operation, Auto Offset Reset, Partition, Starting Offset and Value Deserializer,
  and the Kafka Sink's Value Serializer and Compression, did nothing. The Source
  now has "First run starts at": the oldest message Kafka keeps, or only new
  messages. A step saved with Operation "consume from latest" keeps that choice.
  The Sink's Compression now reaches Kafka. The other settings are gone, with the
  steps' own broker and sign-in fields: a Kafka step uses a saved Kafka
  connection, as every network step has since May. The Source form also shows the
  step's saved read position with a Reset button, shows the defaults a run uses
  (100 messages, not 1,000; a Sink batch of 500, not 16,384), and has a Timeout
  field. Consumer Group is now optional.

- **Cron schedules that name a weekday ran a day late.** The scheduler counted
  days from Monday, while the schedule dialog, the descriptions and the docs all
  use standard cron, where 1 is Monday. A schedule set for Monday ran on Tuesday,
  and `0 9 * * 1-5` ran Tuesday to Saturday. Now 0 and 7 are Sunday and 1 is
  Monday. **Any such schedule moves one day earlier, to the day it has always
  shown.** Hourly, daily, every-N-minutes and `weekly`-type schedules are not
  affected.

- **A cron deployment could run twice in one minute.** The check for "already
  ran" looked for a gap of under 30 seconds, and the next tick comes just over
  30 seconds later. A cron schedule or deployment now fires once in a matching
  minute, and a long run that ends inside the next matching minute no longer
  costs that minute its run.

- **The NATS event bus never worked against a real server.** With
  `REVOLUZEN_FLOW_EVENT_BUS=nats`, creating the JetStream stream failed with
  "invalid JSON": the stream's retention age was converted to nanoseconds
  twice and overflowed. And every durable event was published a second time
  over core NATS "for live fan-out", which the stream also captured, so each
  event took two sequence numbers and a replay returned everything twice.
  One publish now, and the age is passed in seconds as the client expects.
  Found by running the end-to-end test against a server in CI for the first
  time.

- **A folder that held pipelines could not be deleted.** The confirm said its
  pipelines would be "permanently deleted", but the screen sent no mode and the
  server will not guess what to do with them (409), so the user only saw a failure.
  The screen now asks the server what is inside, says how many pipelines will be
  moved to the Bin and how to get them back (they return to the project's top
  level, since the folder is gone), and sends `force`. An empty folder deletes as
  before.

- **Creating a pipeline in a folder never checked the folder.** The check passed
  an argument the folder store does not take, and its error was swallowed, so a
  pipeline could be created in a folder that does not exist or belongs to another
  project. It now answers 404 (400 for another project's folder).

- **Anyone signed in could delete a folder and the pipelines in it.** Deleting a
  pipeline takes the Developer role, but `DELETE /api/folders/{id}?force=true` only
  asked for a signed-in user, so an analyst or viewer could send every pipeline in
  a folder to the Bin (before the Bin, delete them). It takes the Developer role
  now. Creating and renaming folders, and moving pipelines between them, still ask
  only for a signed-in user.

- **A "+ Sub-folder" button that always failed.** Every folder row on the Projects
  page offered it, but folders are one level deep and the server has always refused
  a parent folder ("Sub-folders are not supported"), so it only produced an error.
  The button is gone. "+ Folder" on a project is unchanged, and a folder nested in
  older data is still shown.

- **Deleting a folder could send published pipelines to the Bin.** The row menu
  offers Delete only for a draft, failed or archived pipeline, because a published
  one has to be revoked first, but deleting the folder around it never looked at
  status. A folder with a published pipeline in it now refuses to be deleted: the
  screen names the pipelines to revoke before it asks anything, and
  `DELETE /api/folders/{id}?force=true` answers 409 (`published_pipelines_in_folder`)
  without deleting the drafts either. `move_to_root=true`, which keeps the
  pipelines, is not affected.

- **The Copilot could return another pipeline's draft.** The idempotency key
  ignored which pipeline was open, so the same edit on a second pipeline returned
  the first pipeline's draft for 24 hours. The key now covers the workspace,
  environment and selection, and draft tools are never served from the cache.

- **The write-tool dry-run gate never unlocked.** It forced dry runs until a user
  had 3 successes but counted only live ones. Forced dry runs now count, their
  stub results are no longer cached, and draft-only tools skip the gate for named
  users, because a dry run only replaced a useful draft with a stub.

- **Local models could not edit a failing pipeline.** A request mentioning a
  failure filled the eight-tool cap, leaving out the edit tool. The editor's
  repair tools now always keep their slots.

- **Execute SQL on MySQL silently discarded writes.** `pymysql` connects with
  `autocommit=False` and the branch never committed, so an `INSERT` or
  `UPDATE` was rolled back when the connection closed — while the cursor
  still reported a rowcount and the node still reported success. A write that
  is lost this way is indistinguishable from one that worked. Commit
  semantics are now pinned per driver and covered by tests, because they
  genuinely differ: psycopg2 commits on context-manager exit, pymysql and
  pyodbc need an explicit commit, Snowflake autocommits, and BigQuery and
  Databricks have no interactive transaction to commit.

- **Six screens said one thing and the product did another.**
  - Projects: a project card now has an edit (pencil) button. The Edit Project
    dialog existed but nothing opened it, so a project's name, description,
    colour and icon could never be changed.
  - Dashboard, Avg duration: it now averages successful runs only, as its note
    says. It used to average every finished run, failed ones included. The
    Executions page's Avg Elapsed card reads the same figure and now says so.
  - Dashboard, Pipeline status: the ring mixed run counts with a pipeline total.
    It is now Run outcome mix and counts runs only; the slices add up to the run
    count in the middle.
  - Dashboard, empty state: a workspace with only hand-started runs read "no
    runs yet". The Run volume chart, the ring and the greeting now count every
    run; the Success rate card stays automated-only.
  - Account: the subtitle no longer promises a list of active sessions.
  - Help: Node Reference lists the 45 nodes in the editor's node panel rather
    than 91 entries, says the panel is on the left, and says Delete Data removes
    rows only. A test holds the list to the palette.

- **Six more screens said one thing and the product did another.**
  - Pipelines: the Bin tab and its "moved to Bin, auto-removed after 30 days"
    wording described a list kept in one browser while the server deleted the
    pipeline for good. The Bin is now real and on the server (see **The Bin**
    under Added). An archived pipeline also has a **Restore** button now (the
    endpoint existed; no screen called it).
  - Projects: the Archive button's tooltip no longer promises deletion after 30
    days. Nothing deletes an archived project automatically.
  - Delete Data: the settings no longer offer a "files" target, which was never
    implemented and failed when the pipeline ran. A step saved with it still fails,
    and its settings offer "Switch to deleting rows". File cleanup is the File
    System node.
  - Help: the connector strip counts the 66 connectors you can set up, not the 102
    types the catalog registers, and tallies the certification mix over those 66.
  - The Copilot's first-pipeline answer says the node panel is on the left and that
    a node's settings open on double-click, along the bottom of the editor.
  - Canvas tooltips now exist for Delete Data, File System, Execute SQL, Data
    Profile and SaaS Connector, and the Data Quality tooltip no longer lists a
    "unique" rule the node does not have. `docs/nodes.md` lists the 45 nodes.

- **The built-in templates' Data Quality steps checked nothing.** Eleven steps in
  nine templates used setting names the Data Quality node does not read (`type`,
  `columns`, `check`, `min`, `max`, `on_fail`, `on_failure`), and a rule the node
  cannot read lets every row through. "Drop empty rows" kept the empty rows, steps
  meant to fail the run never failed it, and a range or pattern check only tested
  for a missing value. Every step now uses the node's own rules and On Failure
  modes, and a test runs each one on data with a bad row.
  - The node has no unique-value rule, so the steps that asked for one now check
    what the node can: SharePoint → Managed Table fails the run when an id is
    missing, and Daily sync drops rows with no id. CSV cleanup keeps its
    Deduplicate step, which is what removes duplicate keys.
  - Data quality + dead-letter queue sent rows to a Switch Case step that cannot
    read the settings the template gave it, so the run failed there. Passing rows
    now leave the Data Quality step by its Pass output to the clean table and
    failing rows by its Reject output to the dead-letter table. The description no
    longer promises a failure reason; the node does not record one.
  - Medallion: the Silver step now drops rows that fail its rules. It was set to
    mark them, which let them into Silver and the Gold totals; Bronze still keeps
    every raw row.
  - Inmon 3NF: the two key checks are named for what they do (the key columns are
    filled in) instead of "PK" and "FK", and the description no longer says it
    validates referential integrity.

- **The rest of the built-in templates did not use the settings their steps read.**
  Besides the Data Quality steps, 92 steps in all 27 templates were given setting
  names their node ignores, left out one it requires, or used a step or connector
  the app no longer has. The editor refused to run them ("Missing required field"),
  or a step quietly did something else. Every step now uses its node's own settings,
  and tests hold each one to the node's list of settings and run each template on
  small data with a bad row.
  - First pipeline, the demo that promises real output on a fresh install, stopped
    at its Derived Column step, which wants a list of columns where the template
    gave it a SQL query. It runs now, as do the 19 other steps set up the same way.
  - Managed Table steps (eight templates) used `schema` and `name`, which the node
    does not read, so Run was refused with "Missing required field: table_name".
  - Aggregate, Join and Union steps used `aggregations`, `left_key` / `right_key`
    and `distinct` where the nodes read `functions`, `join_key` and `mode`, so a Join
    stopped with "at least one join key is required". Two Aggregate steps grouped by
    an expression, which the node reads as a column name; the day (DB →
    Warehouse) and the month (Medallion) are now worked out in an earlier step.
  - Database destinations asked for `upsert` with `key_columns`; the node's mode is
    merge, on `merge_on`. Incremental database sources wrote `:last_run`, which
    nothing fills in; they now use the source's incremental setting (a sync mode and
    a watermark column). REST steps set `method`, `auth_type`, a pagination object
    and a URL, none of which the node reads from a template; they now set the path
    to add to the saved connection and the node's own cursor settings, and an
    incremental REST step carries its cursor in the path. Oracle Fusion → Managed
    Table named a connector that does not exist (Oracle Fusion is a kind of saved
    connection, used with the REST source), and SharePoint → Managed Table now uses
    the SharePoint source.
  - Fan-out by region and AI classify + conditional route used the retired Switch
    Case step; they use Switch now, one branch per case. AI classify had no labels
    with example phrases, which the Semantic Router needs, and its description now
    says that the built-in hash provider is a demo (it only matches an example
    phrase word for word) and a real embedding provider should be chosen. Its Spam
    archive wrote a CSV that every run overwrote; it is a Managed Table in append
    mode.
  - Medallion, Kimball, Data Vault and One Big Table wrote to a Warehouse
    destination, which needs a database connection that a Parquet path cannot give.
    They write to Managed Tables now, so they run with nothing to pick: Bronze and
    the facts append, Silver and the Data Vault tables merge by key, the rest
    replace. Medallion gained an "Add month" step for its Gold roll-up.
  - A webhook cannot hand a pipeline rows: the request body becomes the pipeline's
    parameters. Webhook → Flatten → Managed Table and Webhook → enrich → DB
    started from a Source and a trigger node that do not exist. They now declare an
    `event_id` parameter and fetch that event from the sender's API; the enrich
    template reads its users from a second Source, which makes five nodes. Templates
    can declare parameters.
  - Pipeline failure → Slack alert had a SQL step with nothing feeding it; it now
    reads the latest runs from the Executions API. Scheduled alert tested a
    `row_count` column that does not exist; it now tests each job's failure count.
    Slack messages use `{column}`; `{{column}}` came out with stray braces.
  - Smaller: `has_header` is `header` on eight CSV sources; Lookup's Return Columns
    is a list; Dedupe by ID keeps the newest row; Trim whitespace names columns the
    sample orders.csv has; CSV cleanup no longer promises a trim step; the Data Vault
    Dedupe steps set an Order By; the editor's own Aggregation starter asked for
    `aggregations` where Aggregate reads `functions`; the Oracle BIP → SQL Server
    scaffold's Data Quality step uses the node's rules.
  - The pipeline check no longer warns that a Switch adds `_split_output` to a
    destination when its rows leave by one of its named outputs, which strip it.
    Scheduled alert and AI classify say what Slack Notify does: one message per run,
    from the first row that reaches it, sent even when no row does.

- **Incremental database sources read every row on every run.** A source set to
  incremental is meant to save the newest value it read and ask only for rows after
  it the next time. The save looked for its working table under the wrong name,
  failed without a word, and nothing was ever stored, so every scheduled run read and
  loaded the whole table again and "Last cursor" in the source's settings stayed
  empty. The value now comes from the rows the run loaded. It is saved only for a
  run that succeeds and really loads rows: a Sample run, which reads a capped number
  of rows and writes nothing, no longer moves it (once the save worked, it would
  have sent the next real run past rows that were never loaded), and a warning is
  logged when the cursor column is not in the result. A test runs the source twice
  against a SQLite file with the real cursor store.
- **The editor refused to run a pipeline whose database source used a SQL query.**
  Once a saved connection was picked, the pipeline check asked every database source
  for a table ("Missing required field: table"), even in the default SQL Query mode
  and with the query filled in, so Run stayed blocked however the source was set up.
  The check read a setting that no panel writes any more. It now follows the source's
  Mode, as the node does: a query source needs its query, a table source its table, a
  stored-procedure source its procedure, and a database or warehouse destination its
  table. Every database template can be run from the editor once its connection is
  picked, and a test loads each of the 27 templates, picks their connections and Slack
  webhook the way a user does, and expects no error.
- **Data Quality: three fixes in the node and its settings.**
  - A row a check could not answer passed when the step was set to fail the run. A
    missing amount under "between", "greater than", "equals" or "matches a pattern"
    gives no answer. The node dropped such a row or sent it to Reject, but in Fail
    mode it counted the row as good, so the run went through, and in Tag mode it left
    the mark empty. Every mode now treats the row as failed: Fail stops the run, Tag
    marks it false, and Split keeps it with the failures.
  - The settings could not set up a "between", "in" or "not in" check. They had one
    text box that saved what you typed as plain text, so "10,100" or "a,b" matched
    nothing and every row was dropped. "Between" now has a low and a high box, "in"
    and "not in" take a comma-separated list, and a number you type is saved as a
    number (a column known to hold text keeps it as text). A check that was saved the
    old way shows a note asking you to type it again.
  - The pipeline check warned that any Data Quality step before a destination
    "injects internal metadata columns". Only Tag mode (`__dq_passed`), the quality
    score option (`__dq_score`) and Reject mode, when the rows do not leave by its
    Pass or Reject output, add columns. The warning now appears only then and names
    the columns.

- **A Data Quality rule the step could not use let every row through, silently.**
  A rule with no column, a check the step does not have, or a value it cannot read
  (a "between" without both limits, an empty "in" list, a pattern that is not text,
  a length that is not a whole number) was treated as true for every row. The step
  now stops the run before it checks any row, and the error names each rule by its
  number, its column and what it needs. A pipeline saved from the old templates,
  whose checks used setting names the step never read, now stops with "no column
  chosen" instead of passing every row. The editor says the same before Run: the
  pipeline check lists such a rule as an error, and a low limit above the high one
  as a warning.

- **The editor now warns about Data Quality settings the step ignores.** A rule is
  read as a column, a check and a value, and nothing else; the step reads only
  its rules, On Failure, pass-rate threshold, score and profiling settings. The old
  templates wrote more than that, and the step ignored it without a word: a rule
  written as `check: between` with `min` and `max` ran as a plain "not empty", and
  an `on_failure` setting left the step dropping rows instead of failing the run.
  The pipeline check now lists each such key as a warning, saying what the rule or
  step does as written. In the step's settings the same keys show under the rule
  (or under On Failure) with a Remove button.

### Changed

- `GET /api/connectors/cert-matrix` no longer returns the `by_tier` or
  `production_total` aggregates. Certification is reported per connector:
  each row carries `tier` and `cat_green`, and an attested connector also
  carries `attested_at` and `attested_checks`.

  A connector reaches an attested tier only once real vendor responses have
  been recorded for it, so an unattested connector means "no fixtures
  recorded yet" — not "this connector failed". An aggregate renders those
  two states identically, and the reading that wins is the wrong one.
  Callers that want a count can tally `rows` and own the framing.

- The Trust page drops the attestation totals and gains an **Attested**
  column on the per-connector matrix, showing the date a connector's
  acceptance suite last replayed green. Connectors without one show an
  em-dash rather than a zero or a "not verified" badge.

### Added

- **Source Watch** — daily detection that a connector's upstream API changed,
  in `backend/revoluzen_flow/connectors/source_watch.py` and
  `.github/workflows/source-watch.yml`.

  Nothing previously could notice this. The acceptance suite replays recorded
  cassettes, which proves our parsing against *the responses we recorded* and
  by construction never contacts the vendor; the live-smoke allow-list is
  empty. So the first party to learn that a connector had broken was whoever
  it broke for.

  Four signals, none of which needs a vendor account:
  - the vendor advertises a newer API version than a connector is pinned to
  - an endpoint one of our streams reads is gone from the vendor's own spec
  - the vendor's published spec or changelog moved at all
  - RFC 8594 `Deprecation` / `Sunset` headers, harvested passively from
    responses the product already received — no extra request, and it covers a
    customer's own internal APIs too

  Connectors opt in with a `source_watch` block in their runtime manifest
  (`docs/connector-authoring.md`). Eight ship with one — Asana, GitHub,
  Intercom, Mailchimp, PagerDuty, Slack, Stripe and Twilio — each verified
  against the vendor's live spec before being added. A connector without a
  block is reported as `unwatched` rather than healthy. Read the state at
  `GET /api/connectors/source-watch`, which makes no outbound call of its own.

  Version severity reflects what the evidence supports: `critical` only when
  the vendor no longer lists the version a connector pins, `warn` when a newer
  version exists but ours is still supported, `info` when we pin the newest.
  A probe narrowed to a single value cannot establish support at all, so it is
  capped at `warn`. GitHub currently reports `warn` — 2026-03-10 has shipped
  and 2022-11-28 remains supported.

  Spec comparison reconciles base prefixes from either side, since Swagger 2
  puts them in `basePath` (Slack `/api`, Mailchimp `/3.0`) while OpenAPI 3
  puts them in `servers[].url` (Stripe `/`, Twilio none). JSON and YAML specs
  are both read; an unparseable one degrades to fingerprint-only rather than
  asserting that endpoints vanished.

- `.github/workflows/connector-attest.yml` re-runs the connector acceptance
  suite twice weekly and on manifest or fixture changes. A green CAT result
  older than 14 days stops counting, so without this every attested
  connector silently decayed back to Beta.

## [1.0.1] — 2026-10-18

First release published as **Revoluzen Flow**, by Revoluzen Private Limited.

The engine, connectors and UI are unchanged from the 1.0.x line; this release
renames the project and its published artifacts.

### Changed

- Distribution is now `revoluzen-flow` on PyPI. The CLI entry point is
  `revoluzen` (alias `rz`); `python -m revoluzen_flow` works as well.
- Python package is `revoluzen_flow`.
- Environment variables use the `REVOLUZEN_FLOW_` prefix, and the config
  directory is `.revoluzen-flow`.
- Docker image is `revoluzen/flow`; the Compose service is `flow` and the
  data volume is `revoluzen_flow_data`.
- OpenLineage events are emitted with the `revoluzen-flow` namespace and a
  producer URI pointing at this repository.

### Added

- `scripts/check_repo_coordinates.py`, run by the `architecture-invariants`
  CI job. It fails the build when a self-referential GitHub slug disagrees
  with `backend/revoluzen_flow/app_meta.py`, or when documentation tells a
  reader to `cd` into a directory that `git clone` does not create.

### Notes for existing installs

**This is a clean break. There is no migration path, and none is planned.**

The rename is total — nothing keeps the old spelling. In particular, these
carry stored or externally-observed state, and all of them changed:

- **Workflow node type** `fpulseNode` → `revoluzenFlowNode`. Recorded inside
  every saved workflow, so pipelines saved by an earlier release will not
  load.
- **Database file** `fpulse.db` → `revoluzen_flow.db` (and
  `fpulse_events.db` → `revoluzen_flow_events.db`). An existing data
  directory will not be found; the instance starts empty.
- **Session cookies** `fpulse_token` / `fpulse_session` / `fpulse_csrf` →
  `revoluzen_flow_*`. Everyone is signed out once.
- **Prometheus metrics** `fpulse_*` → `revoluzen_flow_*`. Existing
  dashboards and alert rules stop matching.
- **Bootstrap account** `admin@fpulse.local` → `admin@revoluzen.local`.
- **CSS classes, DOM data-attributes and custom events** — internal, but
  listed for anyone who has written custom styling against them.

Treat an upgrade as a fresh install.
