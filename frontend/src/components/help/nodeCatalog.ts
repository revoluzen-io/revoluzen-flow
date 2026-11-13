/**
 * The Help page's Node Reference: every node in the editor's node panel,
 * grouped and named the way the panel shows them.
 *
 * It is a data file, not part of HelpPage.tsx, so a test can hold it to the
 * palette (see __tests__/helpNodeCatalog.test.ts). The page used to carry its
 * own hand-written list of 91 entries — one per file format, cloud and sink,
 * plus nodes that are hidden or switched off — against the 45 nodes the
 * editor actually offers. Formats and connections are chosen inside the
 * Source and Destination nodes; they are not nodes of their own.
 *
 * Keep it to what the panel offers. When a node is added to or hidden from
 * the palette (modulesPanelData.ts, hiddenNodeTypes.ts) or registered on the
 * backend, change it here too; the test says which side drifted.
 */
import type { IconName } from '../shared/Icon';

export interface HelpNode {
  /** The label the node panel shows. */
  name: string;
  desc: string;
}

export interface HelpNodeCategory {
  /** The group name the node panel shows. */
  category: string;
  icon: IconName;
  color: string;
  nodes: HelpNode[];
}

export const NODE_CATALOG: HelpNodeCategory[] = [
  { category: 'Data Movement', icon: 'package', color: 'bg-blue-50 border-blue-200', nodes: [
    { name: 'Source', desc: 'Read data from a file, database, API or storage location. Choose the connector inside the node: CSV, JSON, Parquet, Excel or XML files, SQL databases, REST APIs, S3-compatible storage, Azure Blob Storage, Google Cloud Storage, SharePoint, OneDrive, Kafka, FTP / SFTP, Windows file shares, Google Sheets, Delta Lake, Microsoft Graph, or a managed table. For files from a mainframe or an older system, choose Read the file as: Any format, which reads fixed-width text, COBOL copybook records, EBCDIC text and PGP-encrypted files; a Windows file share is always read this way. A saved connection carries the sign-in details.' },
    { name: 'Destination', desc: 'Write data to a file, database or storage location. Choose the connector inside the node: CSV, JSON, Parquet or Excel files, SQL databases, warehouses, S3-compatible storage, Azure Blob Storage, Google Cloud Storage, SharePoint, OneDrive, Kafka, FTP / SFTP, Windows file shares, REST APIs, webhooks, email, Delta Lake, or a managed table. Write the file as: Any format can encrypt the file with PGP for a partner.' },
    { name: 'Copy Data', desc: 'Copy rows from a source database into a sink database in one step, with column mapping, a table action, a write behaviour (append, overwrite, upsert or merge) and optional bulk loading. With no source connection it copies the rows coming in from the previous node.' },
    { name: 'Managed Table Source', desc: 'Read a managed table, a Parquet dataset the workspace keeps for you (Storage → Managed Tables), by its schema and name.' },
    { name: 'Managed Table Sink', desc: 'Write the incoming rows to a managed table: replace it, append to it, or merge on key columns. A schema policy decides what happens when the columns change. + New schema… names a schema the first run creates. Storage shows which version of the pipeline loaded the table.' },
    { name: 'Pipeline Output', desc: 'Save the incoming rows as one file (CSV, Parquet or JSON) in Storage → Pipeline Outputs, under this pipeline. Needs only a name; every run replaces the file.' },
    { name: 'SaaS Connector', desc: 'Read from a SaaS application such as Salesforce, HubSpot, Stripe or ServiceNow, or from a SOAP service or XML API, using a built-in connector or one you generated in the API Explorer. Takes a saved credential, so the key stays out of the pipeline.' },
  ]},
  { category: 'Transform', icon: 'zap', color: 'bg-amber-50 border-amber-200', nodes: [
    { name: 'Data Wrangler', desc: 'Make several small changes in one node, step by step, with a preview after each: filter, select, rename, change type, derive a column, group, sort, remove duplicates, sample or flatten. The steps run together as one query.' },
    { name: 'SQL Transform', desc: 'Write SQL against the data coming in. The first input is called source_table; other inputs can be used by their node name.' },
    { name: 'Filter', desc: 'Keep only the rows that match a condition, written as SQL or built from visual rules (equals, greater than, contains, between, is empty and so on).' },
    { name: 'Derived Column', desc: 'Add or replace columns using SQL expressions. A column can use a window (partition and order) for things like running totals or the previous row\'s value.' },
    { name: 'Sort', desc: 'Order rows by one or more columns, ascending or descending, with empty values first or last.' },
    { name: 'Deduplicate', desc: 'Remove duplicate rows by key columns, keeping the first or the last row for each key. It can also send the removed rows to a second output.' },
    { name: 'Sample', desc: 'Keep a subset of the rows: the first N, a percentage, or a random sample (set a seed to make it repeatable).' },
    { name: 'Schema Mapper', desc: 'Map source columns to a target schema: rename, reorder, change types and set defaults in one step.' },
    { name: 'Data Quality', desc: 'Check rows against rules you define (not empty, equals, greater or less than, between, in a list, matches a pattern and more). Drop the bad rows, fail the run, tag them, or send them to a separate Reject output.' },
    { name: 'Reconcile', desc: 'Compare the data with the system of record before it is written: the number of rows, the totals of chosen columns, and the keys on each side, within a tolerance you set. The reference is a second input or one read-only query on a connection. A mismatch stops the run, or only warns.' },
    { name: 'Flatten', desc: 'Expand nested data: turn a struct or JSON column into top-level columns, or explode an array column into one row per element.' },
    { name: 'Data Profile', desc: 'Summary statistics per column: share of empty values, distinct count, min and max, most common value. One row per column, so it works well as a first look at new data.' },
    { name: 'SCD Type 2', desc: 'Keep the history of changes per business key (a slowly changing dimension). A change closes the old version and opens a new one, with valid-from, valid-to and current-version columns.' },
  ]},
  { category: 'Combine', icon: 'link', color: 'bg-purple-50 border-purple-200', nodes: [
    { name: 'Join', desc: 'SQL-style join of two inputs on key columns: inner, left, right, full, cross, semi or anti.' },
    { name: 'Aggregate', desc: 'Group by columns and apply SUM, COUNT, AVG, MIN, MAX and more. With no group columns you get one row for the whole input.' },
    { name: 'Lookup Join', desc: 'Enrich rows with columns from a reference dataset matched on a key (a data lookup; distinct from the control-flow Lookup node).' },
    { name: 'Union', desc: 'Stack rows from several inputs: keep every row, keep distinct rows, or match columns by name.' },
    { name: 'Pivot', desc: 'Turn the values of one column into separate columns (crosstab / spreadsheet-style).' },
    { name: 'Unpivot', desc: 'Turn columns back into rows (melt wide data into a long layout).' },
    { name: 'Window', desc: 'Window functions: rank, lag, lead, row number and running totals over partitions, keeping every row.' },
  ]},
  { category: 'Control Flow', icon: 'shuffle', color: 'bg-indigo-50 border-indigo-200', nodes: [
    { name: 'If Condition', desc: 'Route each row to a True or False output by a condition. Legacy single-output edges map to True.' },
    { name: 'Switch', desc: 'Route each row to a named output branch by condition — true multi-output.' },
    { name: 'ForEach', desc: 'Run a saved sub-pipeline once per input row, injecting the row\'s columns as parameters.' },
    { name: 'Execute Pipeline', desc: 'Run another saved pipeline as a child step, optionally passing parameters (runs synchronously).' },
    { name: 'Lookup', desc: 'Lookup activity — fetch a value or reference row into {{ $vars.X }} for control flow (watermarks, config lookups, row-count gates).' },
    { name: 'Set Variable', desc: 'Set runtime variables ({{ $vars.NAME }}) from a constant or SQL expression — read by any downstream step. Input rows pass through unchanged.' },
    { name: 'Wait', desc: 'Pause the run for a fixed duration before continuing.' },
    { name: 'Fail', desc: 'Deliberately halt the run with a custom error message — use it to fast-fail when something you rely on breaks.' },
    { name: 'Retry', desc: 'Wrap a step with a retry policy: N attempts with a back-off before the failure is passed on.' },
    { name: 'Batch Rows', desc: 'Advanced — split rows into fixed-size batches, tagging each with its batch index.' },
  ]},
  { category: 'Action', icon: 'globe', color: 'bg-teal-50 border-teal-200', nodes: [
    { name: 'HTTP Request', desc: 'Call a web API (GET, POST, PUT, PATCH or DELETE) with headers, a JSON, XML, form or text body and authentication, once or once per input row, and keep the response. An XML reply becomes rows through a row path.' },
    { name: 'Send Email', desc: 'Send an email over SMTP, as plain text or HTML, with CC and BCC, once or once per input row. Use a saved SMTP connection or enter the server in the node.' },
    { name: 'Slack / Teams', desc: 'Post a message to a Slack or Teams channel through an incoming webhook. The text can use values from the first input row.' },
    { name: 'Get Metadata', desc: 'Return a description of the incoming data instead of the data itself: one row per column with its type, empty count, distinct count, min and max, plus the total row count.' },
    { name: 'Delete Data', desc: 'Drop the rows that match your condition from the data passing through (the opposite of Filter). It changes the pipeline\'s data only: it does not delete anything in a database or in storage. To delete files use File System; to run a DELETE in a database use Execute SQL.' },
    { name: 'File System', desc: 'Copy, move, rename or delete files and folders on the machine running Revoluzen Flow. A relative path means inside the data folder, and missing folders are created.' },
    { name: 'Execute SQL', desc: 'Run a SQL statement on a database connection (or on the pipeline\'s own engine when none is chosen): create, insert, update, delete and similar. It can return the number of rows affected or the full result.' },
  ]},
  { category: 'AI / Semantic', icon: 'activity', color: 'bg-violet-50 border-violet-200', nodes: [
    { name: 'Embedder', desc: 'Turn a text column into a vector column using Ollama, OpenAI, Cohere, sentence-transformers, or a deterministic hash fallback.' },
    { name: 'LLM Guardrail', desc: 'PII / profanity / prompt-injection detection — routes flagged rows to a separate output for review.' },
    { name: 'Semantic Router', desc: 'Classify each row into a label via embeddings or an LLM. Use it to send rows down different branches by their content.' },
  ]},
];

/** How many nodes the Node Reference lists. */
export const NODE_COUNT = NODE_CATALOG.reduce((total, group) => total + group.nodes.length, 0);
