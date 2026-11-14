# Node reference

Revoluzen Flow ships **45 nodes**, in six groups: Data Movement (6), Transform (12), Combine (7), Control Flow (10), Action (7) and AI / Semantic (3). They are the nodes in the editor's node panel, which sits on the left of the canvas (`MODULES` in `frontend/src/components/modulesPanelData.ts`, plus the backend types that `HIDDEN_TYPES` does not hide). Drag any node onto the canvas and click it to configure. Help → Node Reference in the app lists the same 45.

The panel opens at the **Standard** level, which groups nodes by purpose (Import, Prepare, Analyze, Automate, Publish) and hides the specialist ones. **Basic** shows only the starter nodes, and **All** shows every node under the six headings below.

File formats and connections are not nodes of their own: you drag a **Source** or **Destination** and choose the format or connection inside it. The parameters of each node are in [`NODE_REFERENCE.md`](NODE_REFERENCE.md).

## Data Movement (6)

| Node | What it does |
|---|---|
| **Source** | Read data from a file, database, API or storage location. Choose the connector inside the node (files, SQL databases, REST APIs, object storage, SharePoint, OneDrive, Kafka, FTP / SFTP, Windows file shares, Google Sheets, Delta Lake, Microsoft Graph, or a managed table); a saved connection carries the sign-in details. It is the entry point for any of the 68 connectors (see [`connectors.md`](connectors.md)). |
| **Destination** | Write data to a file, database or storage location. Choose the connector inside the node, as on Source. To upload a file over FTP or SFTP, pick that connection, then set **Remote Path** (for example `/uploads/orders.csv`) and a file format. The folder must already exist on the server. SFTP needs the `paramiko` package. To write to a Windows file share, pick that connection and give the file's path inside the connection's folder; missing folders are created, and the file appears only once it is complete (see [`user-guides/mainframe-and-legacy-files.md`](user-guides/mainframe-and-legacy-files.md)). |
| **Copy Data** | Copy rows from a source database into a sink database in one step, with column mapping, a table action, a write behaviour (append, overwrite, upsert or merge) and optional bulk loading. With no source connection it copies the rows coming in from the previous node. |
| **Managed Table Source** | Read a managed table, a Parquet dataset the workspace keeps for you, by `schema.name`. Tables live under `$REVOLUZEN_FLOW_DATA_DIR/tables/{ws}/{schema}/{name}/`. Promote an uploaded file to a managed table from **Storage → Files → Promote**, or write to one from a pipeline with Managed Table Sink. |
| **Managed Table Sink** | Write the incoming rows to a managed table by `schema.name`. Three modes: `replace` (drop the existing parts, write a fresh part-000), `append` (add a timestamp-suffixed part file next to the existing ones) and `merge` (upsert on the `merge_on` key columns; collapses to a single part). A schema policy decides what happens when the columns change. The Storage page tracks "Used by N pipelines" automatically, so destructive actions surface warnings. |
| **SaaS Connector** | Read from a SaaS application, using a built-in connector or one you saved from Insights → API Explorer. Pick the connector and a stream, then a saved credential if it needs a key: the pipeline stores only which credential to use. At the Standard level it is in the Import group. |

## Transform (12)

| Node | What it does |
|---|---|
| **Data Wrangler** | Make several small changes in one node, step by step, with a preview after each: filter, select, rename, change type, derive a column, group, sort, remove duplicates, sample or flatten. The steps run together as one query. |
| **SQL Transform** | Write SQL against the data coming in (full DuckDB syntax). The first input is called `source_table`; other inputs can be used by their node name. |
| **Filter** | Keep only the rows that match a condition, written as SQL or built from visual rules (equals, greater than, contains, between, is empty and so on). |
| **Derived Column** | Add or replace columns using SQL expressions. A column can use a window (partition and order) for things like running totals or the previous row's value. |
| **Sort** | Order rows by one or more columns, ascending or descending, with empty values first or last. |
| **Deduplicate** | Remove duplicate rows by key columns, keeping the first or the last row for each key. It can also send the removed rows to a second output. |
| **Sample** | Keep a subset of the rows: the first N, a percentage, or a random sample (set a seed to make it repeatable). |
| **Schema Mapper** | Map source columns to a target schema: rename, reorder, change types and set defaults in one step. |
| **Data Quality** | Check rows against rules you define: is empty, is not empty, equals, does not equal, greater or less than, in or not in a list, between, matches a pattern, minimum or maximum length. There is no rule for unique values; use Deduplicate to remove duplicate keys. Drop the bad rows, fail the run, tag them, or send them to a separate Reject output. |
| **Reconcile** | Compare the data with the system of record before it is written: the number of rows, the totals of chosen columns, and the keys on each side, within a tolerance you set. The reference is a second input or one read-only query on a connection. A mismatch stops the run, or only warns. |
| **Flatten** | Expand nested data: turn a struct or JSON column into top-level columns, or explode an array column into one row per element. |
| **Data Profile** | Summary statistics per column: share of empty values, distinct count, min and max, most common value. One row per column, so it works well as a first look at new data. |
| **SCD Type 2** | Keep the history of changes per business key (a slowly changing dimension). A change closes the old version and opens a new one, with valid-from, valid-to and current-version columns. |

## Combine (7)

| Node | What it does |
|---|---|
| **Join** | SQL-style join of two inputs on key columns: inner, left, right, full, cross, semi or anti. |
| **Aggregate** | Group by columns and apply SUM, COUNT, AVG, MIN, MAX and more. With no group columns you get one row for the whole input. |
| **Lookup Join** | Enrich rows with columns from a reference dataset matched on a key (a data lookup; distinct from the Control Flow **Lookup** node). |
| **Union** | Stack rows from several inputs: keep every row, keep distinct rows, or match columns by name. |
| **Pivot** | Turn the values of one column into separate columns (crosstab / spreadsheet-style). |
| **Unpivot** | Turn columns back into rows (melt wide data into a long layout). |
| **Window** | Window functions: rank, lag, lead, row number and running totals over partitions, keeping every row. |

## Control Flow (10)

| Node | What it does |
|---|---|
| **If Condition** | Route each row to a True or False output by a condition. Legacy single-output edges map to True. |
| **Switch** | Route each row to a named output branch by condition — true multi-output. |
| **ForEach** | Run a saved sub-pipeline once per input row, injecting the row's columns as parameters. |
| **Execute Pipeline** | Run another saved pipeline as a child step, optionally passing parameters (runs synchronously). |
| **Lookup** | Fetch a value or reference row into `{{ $vars.X }}` for control flow (watermarks, config lookups, row-count gates). |
| **Set Variable** | Set runtime variables (`{{ $vars.NAME }}`) from a constant or SQL expression, read by any downstream step. Input rows pass through unchanged. |
| **Wait** | Pause the run for a fixed duration before continuing. |
| **Fail** | Deliberately halt the run with a custom error message — use it to fast-fail when something you rely on breaks. |
| **Retry** | Wrap a step with a retry policy: N attempts with a back-off before the failure is passed on. |
| **Batch Rows** | Advanced — split rows into fixed-size batches, tagging each with its batch index. |

## Action (7)

| Node | What it does |
|---|---|
| **HTTP Request** | Call a web API (GET, POST, PUT, PATCH or DELETE) with headers, a JSON, XML, form or text body and authentication, once or once per input row, and keep the response. An XML reply becomes rows through a row path. |
| **Send Email** | Send an email over SMTP, as plain text or HTML, with CC and BCC, once or once per input row. Use a saved SMTP connection or enter the server in the node. |
| **Slack / Teams** | Post a message to a Slack or Teams channel through an incoming webhook. The text can use values from the first input row. |
| **Get Metadata** | Return a description of the incoming data instead of the data itself: one row per column with its type, empty count, distinct count, min and max, plus the total row count. |
| **Delete Data** | Drop the rows that match your condition from the data passing through (the opposite of Filter). It changes the pipeline's data only: it does not delete anything in a database or in storage. To delete files use File System; to run a `DELETE` in a database use Execute SQL. |
| **File System** | Copy, move, rename or delete files and folders on the machine running Revoluzen Flow. A relative path means inside the data folder, and missing folders are created. It does not reach S3, Azure Blob or GCS: use a Destination node for those. |
| **Execute SQL** | Run a SQL statement on a database connection (or on the pipeline's own engine when none is chosen): DDL, stored procedures, admin statements, create, insert, update, delete and similar. It can return the number of rows affected or the full result. |

## AI / Semantic (3)

| Node | What it does |
|---|---|
| **Embedder** | Turn a text column into a vector column using Ollama, OpenAI, Cohere, sentence-transformers, or a deterministic hash fallback. |
| **LLM Guardrail** | PII / profanity / prompt-injection detection — routes flagged rows to a separate output for review. |
| **Semantic Router** | Classify each row into a label via embeddings or an LLM. Use it to send rows down different branches by their content. |

See the [Changelog](../CHANGELOG.md) for release notes.
