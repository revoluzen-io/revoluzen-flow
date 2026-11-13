"""Draft-clarification engine — May 17 2026.

The biggest UX gap surfaced by the 300-prompt sample run (May 17 2026):
when a user says *"Build a pipeline to fetch Oracle BIP every 6 hours and
load into SQL Server"*, the rule planner today silently guesses every
config (auth method, BIP report path, schedule precise cron, SQL Server
target table, write mode, cleaning rules). The user gets back a draft
they have to fix in 5 places.

This module changes the flow to **clarify BEFORE drafting**. Given a
build-pipeline prompt, ``detect_missing_draft_fields`` returns a list of
focused questions; the agent endpoint surfaces them as chips and only
calls the planner once the user has answered.

Sibling to ``clarify.py`` (which handles "which pipeline did you mean?"
disambiguation). This one handles "what details do you want?" for net-new
pipeline construction.

Design:
- **Heuristic, not ML.** Pure regex / keyword detection. No LLM in this
  hot path — clarification has to be sub-10ms or it adds latency to
  every build-pipeline request.
- **Question set is SMALL.** More than a few and users perceive it as
  "the bot is interrogating me." Each question must be a real blocker,
  not a nice-to-have.
- **Chip-friendly.** Every question has a recommended set of one-click
  answers so users don't have to type.
- **No I/O here.** The caller reads the workspace and passes it in as a
  ``WorkspaceInventory``, so the questions can list real connections,
  uploaded files and managed tables while this module stays pure. The
  inventory only ever carries ids and names, never a connection's config.

Public surface:
  * ``is_build_request(prompt) -> bool``
  * ``detect_draft_slots(prompt, inventory) -> DraftSlots | None``
  * ``detect_missing_draft_fields(prompt, inventory=None) -> ClarificationSet | None``
  * ``ClarificationSet`` / ``ClarificationQuestion`` / ``ClarificationOption``
  * ``clarification_payload(set) -> dict`` — what the chat card receives
  * ``render_clarification_card(set) -> str`` — plain text for chat
  * ``render_questions_for_model(set) -> str`` — for the Copilot's tool loop
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Callable, Iterable


# The follow-up turn is recognised by this phrase in the previous reply.
CLARIFY_MARKER = "A few quick questions before I draft it"


# ── Detection patterns ────────────────────────────────────────────────────
#
# Trigger words that tell us this is a build/create-pipeline request.
# Pattern is conservative — we'd rather miss a clarification opportunity
# than ask questions for prompts that aren't actually pipeline builds. The
# words allowed between the verb and "pipeline" keep out requests about a
# pipeline that already exists ("make this pipeline faster", "create a
# schedule for the pipeline").
_BUILD_INTENT_RE = re.compile(
    r"\b(build|create|draft|make|generate|design|set\s?up|scaffold)\b"
    r"(?:\s+(?!this\b|that\b|my\b|our\b|the\b|these\b|those\b|current\b|existing\b"
    r"|your\b|it\b|for\b|of\b|on\b|sure\b)[\w-]+){0,4}?"
    r"\s+(pipeline|pipelines|workflow|workflows|etl|flow|flows)\b",
    re.IGNORECASE,
)

# Data-moving requests that never say "pipeline".
_MOVE_VERB_RE = re.compile(
    r"^\s*(?:please\s+)?(copy|sync|replicate|migrate|mirror|transfer|extract|pull|fetch"
    r"|ingest|export|load|read|import)\b",
    re.IGNORECASE,
)
_MOVE_ENDPOINT_RE = re.compile(r"\b(from|to|into)\b", re.IGNORECASE)
_MOVE_BOTH_ENDS_RE = re.compile(r"^\s*(?:please\s+)?move\b.*\bfrom\b.*\b(to|into)\b", re.IGNORECASE | re.DOTALL)
_COMBINE_VERB_RE = re.compile(r"^\s*(?:please\s+)?(join|merge|combine)\b", re.IGNORECASE)

_FILE_NAME_RE = re.compile(
    r"(?<![\w./\\-])[\w\-./\\]*[\w\-]\.(csv|tsv|txt|json|ndjson|jsonl|parquet|pq|xlsx|xls|xml)\b",
    re.IGNORECASE,
)

_FILE_FORMAT_BY_EXTENSION = {
    "csv": "csv", "tsv": "csv", "txt": "csv",
    "json": "json", "ndjson": "json", "jsonl": "json",
    "parquet": "parquet", "pq": "parquet",
    "xlsx": "excel", "xls": "excel",
    "xml": "xml",
}

# Schedule explicitly stated (don't ask if user already said it).
_SCHEDULE_MENTIONED_RE = re.compile(
    r"\b(every|daily|hourly|weekly|monthly|on\s+demand|schedule|cron|"
    r"each\s+day|each\s+hour|nightly|morning|evening|interval)\b",
    re.IGNORECASE,
)

# Recurring, but the interval was never given.
_SCHEDULE_HINT_RE = re.compile(
    r"\b(recurring|periodically|automatically|on\s+a\s+schedule|"
    r"on\s+intervals?|regularly|continuously)\b",
    re.IGNORECASE,
)

# How often the person asked for, kept so the draft can say it was heard.
_SCHEDULE_PHRASE_RE = re.compile(
    r"\b(every\s+(?:\d+\s+)?(?:night|day|morning|evening|hour|minute|week|month|weekday|hours|minutes|days)"
    r"|nightly|hourly|daily|weekly|monthly)\b",
    re.IGNORECASE,
)

# Write mode stated outright. "merge", "update" and "replace" alone say nothing:
# "merge these two files" and "replace nulls with 0" are not about the target,
# and reading them as "replace" would drop a table nobody asked to drop.
_WRITE_MODE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("merge", re.compile(r"\bupsert(?:s|ing)?\b|\b(?:merges?|updates?)\s+(?:into|on|by)\b", re.IGNORECASE)),
    ("append", re.compile(r"\bappend(?:s|ing)?\b|\binserts?\s+into\b", re.IGNORECASE)),
    ("replace", re.compile(
        r"\boverwrit(?:e|es|ing)\b|\btruncat(?:e|es|ing)\b"
        r"|\breplac(?:e|es|ing)\s+(?:the\s+)?(?:table|target|existing|data|contents?)\b",
        re.IGNORECASE,
    )),
)
_MERGE_KEY_RE = re.compile(
    r"\b(?:upsert|merge|update)(?:s|ing)?\b[^.]*?\b(?:on|by)\s+([A-Za-z_]\w*)",
    re.IGNORECASE,
)

# Cleaning rules mentioned (filter X, remove Y, validate Z, dedupe by W).
_CLEANING_DETAIL_RE = re.compile(
    r"\b(filter|remove|removes|drop|drops|deduplicate|dedup|trim|normalize|standardize|"
    r"mask|encrypt|hash|cast|convert|where|status\s*=|null|validate)\b",
    re.IGNORECASE,
)

# Words that say which end of the pipeline the next thing named belongs to.
# The last one before a name wins, which is what reads "from Postgres to
# Snowflake" correctly: scanning the whole sentence for a sink keyword finds
# Postgres first and writes back to the source.
_ROLE_CUE_RE = re.compile(
    r"\b(?P<source>from|read|reads|reading|load|loads|loading|fetch|fetches|fetching"
    r"|pull|pulls|pulling|ingest|ingests|ingesting|extract|extracts|extracting"
    r"|import|imports|importing|poll|polls|polling|call|calls|calling"
    r"|query|queries|querying|scan|scans|scanning|join|joins|joining"
    r"|merge|merges|merging|combine|combines|combining|consume|consumes|consuming"
    r"|copy|copies|copying|sync|syncs|syncing|replicate|replicates|replicating"
    r"|migrate|migrates|migrating|mirror|mirrors|mirroring|move|moves|moving"
    r"|transfer|transfers|transferring|export|exports|exporting"
    r"|send|sends|sending|push|pushes|pushing|upload|uploads|uploading)\b"
    r"|\b(?P<destination>to|into|onto|write|writes|writing|save|saves|saving"
    r"|store|stores|storing|output|outputs|land|lands|landing|sink)\b",
    re.IGNORECASE,
)

_MANAGED_TABLE_RE = re.compile(
    r"\b(?:managed|workspace|local)\s+table\b"
    r"(?:\s+(?:called|named)\s+[`'\"]?(?P<name>[A-Za-z_][\w.]*)[`'\"]?)?",
    re.IGNORECASE,
)

# "<name> table" / "table <name>", for a table the prompt names in passing.
_TABLE_THEN_NAME_RE = re.compile(
    r"\b(?:table|collection)\s+(?:called\s+|named\s+)?[`'\"]?(?P<name>[A-Za-z_][\w.]*)[`'\"]?",
    re.IGNORECASE,
)
_NAME_THEN_TABLE_RE = re.compile(
    r"\b(?P<name>[A-Za-z_][\w.]*)\s+(?:table|collection)\b",
    re.IGNORECASE,
)
# "Copy customers from ..." names the table without saying "table".
_COPY_WHAT_RE = re.compile(
    r"\b(?:copy|copies|sync|syncs|replicate|replicates|migrate|migrates|mirror|move|moves"
    r"|transfer|transfers|extract|extracts|pull|pulls|fetch|fetches|load|loads|read|reads"
    r"|export|exports|ingest|ingests|import|imports)\s+"
    r"(?:all\s+|the\s+|my\s+|our\s+|of\s+)*(?P<name>[A-Za-z_][\w.]*)\s+"
    r"(?:data\s+|records\s+|rows\s+)?from\b",
    re.IGNORECASE,
)
_NOT_A_TABLE_NAME = frozenset({
    "a", "an", "the", "this", "that", "these", "those", "new", "my", "our", "all", "any", "each",
    "data", "result", "results", "rows", "records", "output", "input", "it", "them",
    "managed", "workspace", "local", "target", "source", "destination", "staging", "sql",
    "database", "warehouse", "db", "and", "or", "to", "into", "from", "in", "on", "of", "as",
    "called", "named", "with", "for", "by", "only", "same", "one", "another",
    "every", "daily", "hourly", "weekly", "monthly", "nightly", "where", "which", "when", "then",
    "if", "per", "at", "is", "are", "was", "should", "will", "so", "but", "not", "after", "before",
    "while", "using", "via", "has", "have",
})

# What the pipeline is for, turned into a name for a new table.
_PURPOSE_RE = re.compile(
    r"\b(?:shows?|showing|lists?|listing|reports?|reporting|summari[sz]es?|summari[sz]ing"
    r"|calculates?|calculating|computes?|computing|counts?|counting|tracks?|tracking"
    r"|aggregates?|aggregating|produces?|producing)\s+"
    r"(?:the\s+|all\s+|our\s+|my\s+)?(?P<what>[A-Za-z][\w ]{2,60}?)\s*(?:[.,;:]|$)",
    re.IGNORECASE,
)


# ── Connector names ───────────────────────────────────────────────────────
#
# The names people read. Mirrors CONNECTION_TYPES in ConnectionsPage.tsx.
CONNECTOR_LABELS: dict[str, str] = {
    "postgresql": "PostgreSQL", "mysql": "MySQL", "mssql": "SQL Server", "oracle": "Oracle DB",
    "sqlite": "SQLite", "mariadb": "MariaDB", "cockroachdb": "CockroachDB", "db2": "IBM Db2",
    "sap_hana": "SAP HANA", "teradata": "Teradata", "informix": "Informix",
    "mongodb": "MongoDB", "cassandra": "Cassandra", "couchbase": "Couchbase",
    "dynamodb": "DynamoDB", "cosmosdb": "Cosmos DB", "neo4j": "Neo4j", "arangodb": "ArangoDB",
    "firebase": "Firebase",
    "snowflake": "Snowflake", "bigquery": "BigQuery", "redshift": "Redshift",
    "databricks": "Databricks", "synapse": "Azure Synapse", "clickhouse": "ClickHouse",
    "duckdb": "DuckDB", "trino": "Trino", "presto": "Presto", "athena": "AWS Athena",
    "elasticsearch": "Elasticsearch", "opensearch": "OpenSearch", "redis": "Redis",
    "s3": "AWS S3 / MinIO", "azure_blob": "Azure Blob Storage", "adls_gen2": "Azure Data Lake Gen2",
    "gcs": "Google Cloud Storage", "minio": "MinIO",
    "sharepoint": "SharePoint", "onedrive": "OneDrive", "gdrive": "Google Drive",
    "dropbox": "Dropbox", "box": "Box", "ftp": "FTP / SFTP", "sftp": "SFTP", "gsheet": "Google Sheets",
    "rest_api": "REST API", "graphql": "GraphQL", "odata": "OData",
    "microsoft_graph": "Microsoft Graph",
    "kafka": "Kafka", "rabbitmq": "RabbitMQ", "pulsar": "Apache Pulsar",
    "eventhub": "Azure Event Hubs", "kinesis": "AWS Kinesis",
    "salesforce": "Salesforce", "dynamics365": "Dynamics 365", "sap_s4hana": "SAP S/4HANA",
    "sap_successfactors": "SAP SuccessFactors", "sap": "SAP", "servicenow": "ServiceNow",
    "jira": "Jira", "workday": "Workday", "hubspot": "HubSpot", "zendesk": "Zendesk",
    "netsuite": "NetSuite", "oracle_fusion": "Oracle Fusion Cloud",
    "oracle_bip": "Oracle BI Publisher", "oracle_api": "Oracle Fusion Cloud",
    "github": "GitHub", "shopify": "Shopify", "stripe": "Stripe", "notion": "Notion", "asana": "Asana",
    "smtp": "SMTP Email", "sendgrid": "SendGrid", "slack": "Slack", "teams": "Microsoft Teams",
    "twilio": "Twilio", "pagerduty": "PagerDuty", "custom": "Custom",
}


def connector_label(connection_type: str) -> str:
    return CONNECTOR_LABELS.get(connection_type) or connection_type.replace("_", " ").title()


_DATABASE_TYPES: tuple[str, ...] = (
    "postgresql", "mysql", "mssql", "oracle", "sqlite", "mariadb", "db2", "sap_hana",
    "teradata", "informix", "cockroachdb",
    "mongodb", "cassandra", "couchbase", "dynamodb", "cosmosdb", "neo4j", "arangodb", "firebase",
)
_WAREHOUSE_TYPES: tuple[str, ...] = (
    "snowflake", "bigquery", "redshift", "databricks", "synapse", "clickhouse",
    "duckdb", "trino", "presto", "athena",
)
_REST_TYPES: tuple[str, ...] = (
    "rest_api", "graphql", "odata",
    "salesforce", "hubspot", "zendesk", "jira", "servicenow", "sap", "sap_s4hana",
    "sap_successfactors", "netsuite", "dynamics365", "workday", "oracle_fusion", "oracle_bip",
    "oracle_api", "github", "shopify", "stripe", "notion", "asana", "custom",
)

# Connection type -> what the generic Source node routes it through. Mirrors
# CONNECTION_TO_CONNECTOR in ConfigPanel.tsx. A type missing here can't be read
# by a pipeline, so it is never offered as a source.
SOURCE_CONNECTORS: dict[str, str] = {
    **{t: "database" for t in (*_DATABASE_TYPES, *_WAREHOUSE_TYPES)},
    **{t: "rest_api" for t in _REST_TYPES},
    "s3": "s3", "minio": "s3", "gcs": "gcs", "azure_blob": "azure_blob", "adls_gen2": "azure_blob",
    "ftp": "ftp", "sftp": "ftp", "kafka": "kafka",
    "sharepoint": "sharepoint", "onedrive": "onedrive", "gsheet": "gsheet",
    "microsoft_graph": "microsoft_graph",
}

# Same for the generic Destination node. Notification types only ever go here.
DESTINATION_CONNECTORS: dict[str, str] = {
    **{t: "database" for t in (*_DATABASE_TYPES, *_WAREHOUSE_TYPES)},
    **{t: "rest_api" for t in _REST_TYPES},
    "s3": "s3", "minio": "s3", "gcs": "gcs", "azure_blob": "azure_blob", "adls_gen2": "azure_blob",
    "ftp": "ftp", "sftp": "ftp", "kafka": "kafka",
    "sharepoint": "sharepoint", "onedrive": "onedrive",
    "slack": "webhook", "teams": "webhook", "twilio": "webhook", "pagerduty": "webhook",
    "smtp": "email", "sendgrid": "email",
}

# Connection types whose catalog lists tables, so the card can offer them.
TABLE_CONNECTION_TYPES: tuple[str, ...] = (*_DATABASE_TYPES, *_WAREHOUSE_TYPES)

# Where a write mode means something. The warehouse route only inserts.
WRITE_MODE_CONNECTION_TYPES: tuple[str, ...] = ("postgresql", "mysql", "mssql", "sqlite")

# Ranked ahead of the rest in a mixed list because the engine can really run them.
_RUNNABLE_DATABASE_TYPES = frozenset({
    "postgresql", "mysql", "mssql", "sqlite", "mongodb",
    "snowflake", "bigquery", "redshift", "databricks", "clickhouse", "oracle",
})


@dataclass(frozen=True)
class Technology:
    """A system a request can name without naming a saved connection."""
    slug: str                   # stable key; clarify_to_template's table uses it
    label: str                  # what the person reads
    pattern: re.Pattern[str]
    connection_types: tuple[str, ...] = ()
    new_connection_type: str = ""  # pre-selected in the inline form
    kind: str = "connection"    # connection | file | family
    file_format: str = ""       # for kind="file"
    write_only: bool = False


def _tech(slug, label, pattern, types=(), new_type="", kind="connection", file_format="", write_only=False):
    return Technology(
        slug=slug, label=label, pattern=re.compile(pattern, re.IGNORECASE),
        connection_types=tuple(types), new_connection_type=new_type,
        kind=kind, file_format=file_format, write_only=write_only,
    )


# Order matters — the first match claims the text, so "Oracle BIP" is never
# also read as plain "Oracle".
_TECHNOLOGIES: tuple[Technology, ...] = (
    _tech("oracle_bip", "Oracle BI Publisher", r"oracle\s*bip|bi\s*publisher", ("oracle_bip",), "oracle_bip"),
    _tech("oracle_fusion", "Oracle Fusion Cloud", r"oracle\s*fusion|fusion\s*cloud",
          ("oracle_fusion", "oracle_api"), "oracle_fusion"),
    _tech("oracle", "Oracle", r"\boracle\b", ("oracle", "oracle_bip", "oracle_fusion", "oracle_api"), "oracle"),
    _tech("sap", "SAP", r"\bsap\b", ("sap_s4hana", "sap_successfactors", "sap", "sap_hana"), "sap_s4hana"),
    _tech("salesforce", "Salesforce", r"salesforce|sfdc", ("salesforce",), "salesforce"),
    _tech("servicenow", "ServiceNow", r"servicenow|service\s*now", ("servicenow",), "servicenow"),
    _tech("hubspot", "HubSpot", r"\bhubspot\b", ("hubspot",), "hubspot"),
    _tech("jira", "Jira", r"\bjira\b", ("jira",), "jira"),
    _tech("zendesk", "Zendesk", r"\bzendesk\b", ("zendesk",), "zendesk"),
    _tech("rest_api", "REST API", r"rest\s*api|http\s*api|web\s*api", ("rest_api", "graphql", "odata"), "rest_api"),
    _tech("api", "REST API", r"\bapi\b", ("rest_api", "graphql", "odata"), "rest_api"),
    _tech("postgres", "PostgreSQL", r"\bpostgres(?:ql)?\b|\bpsql\b", ("postgresql",), "postgresql"),
    _tech("mysql", "MySQL", r"\bmysql\b", ("mysql",), "mysql"),
    _tech("mariadb", "MariaDB", r"\bmariadb\b", ("mariadb",), "mariadb"),
    _tech("sql_server", "SQL Server", r"sql\s*server|\bmssql\b|\bt-sql\b", ("mssql",), "mssql"),
    _tech("sqlite", "SQLite", r"\bsqlite\b", ("sqlite",), "sqlite"),
    _tech("mongodb", "MongoDB", r"\bmongo(?:db)?\b", ("mongodb",), "mongodb"),
    _tech("snowflake", "Snowflake", r"\bsnowflake\b", ("snowflake",), "snowflake"),
    _tech("bigquery", "BigQuery", r"big\s*query|\bgbq\b", ("bigquery",), "bigquery"),
    _tech("redshift", "Redshift", r"\bredshift\b", ("redshift",), "redshift"),
    _tech("databricks", "Databricks", r"\bdatabricks\b", ("databricks",), "databricks"),
    _tech("clickhouse", "ClickHouse", r"\bclickhouse\b", ("clickhouse",), "clickhouse"),
    _tech("s3", "Amazon S3", r"\bs3\b|amazon\s*s3", ("s3", "minio"), "s3"),
    _tech("azure_blob", "Azure Blob Storage", r"azure\s*blob|\badls\b", ("azure_blob", "adls_gen2"), "azure_blob"),
    _tech("gcs", "Google Cloud Storage", r"\bgcs\b|google\s*cloud\s*storage", ("gcs",), "gcs"),
    _tech("sharepoint", "SharePoint", r"\bsharepoint\b", ("sharepoint",), "sharepoint"),
    _tech("onedrive", "OneDrive", r"\bonedrive\b", ("onedrive",), "onedrive"),
    _tech("gsheet", "Google Sheets", r"google\s*sheets?|\bgsheets?\b", ("gsheet",), "gsheet"),
    _tech("kafka", "Kafka", r"\bkafka\b|\bredpanda\b", ("kafka",), "kafka"),
    _tech("sftp", "FTP / SFTP", r"\bsftp\b|\bftp\b", ("ftp", "sftp"), "ftp"),
    _tech("slack", "Slack", r"\bslack\b", ("slack",), "slack", write_only=True),
    _tech("teams", "Microsoft Teams", r"microsoft\s*teams|\bms\s*teams\b", ("teams",), "", write_only=True),
    # Not a bare "email": that is usually a column ("copy customer email from ...").
    _tech("email", "email", r"\bsmtp\b|\bsendgrid\b|\b(?:by|via)\s+e-?mail\b|\be-?mail\s+(?:it|them|the|a)\b",
          ("smtp", "sendgrid"), "smtp", write_only=True),
    _tech("delta_lake", "Delta Lake", r"delta\s*lake|delta\s*table", kind="file", file_format="delta"),
    _tech("parquet", "Parquet", r"\bparquet\b", kind="file", file_format="parquet"),
    _tech("csv_file", "CSV", r"\bcsv\b", kind="file", file_format="csv"),
    _tech("excel_file", "Excel", r"\bexcel\b|\bxlsx\b", kind="file", file_format="excel"),
    _tech("json_file", "JSON", r"\bjson\b", kind="file", file_format="json"),
    _tech("xml_file", "XML", r"\bxml\b", kind="file", file_format="xml"),
    _tech("warehouse", "data warehouse", r"data\s*warehouse|\bwarehouse\b", _WAREHOUSE_TYPES, kind="family"),
    _tech("database", "database", r"\bdatabase\b|\bdb\b", (*_DATABASE_TYPES, *_WAREHOUSE_TYPES), kind="family"),
)
_TECH_BY_SLUG = {t.slug: t for t in _TECHNOLOGIES}

_SLUG_BY_CONNECTION_TYPE = {
    "postgresql": "postgres", "mssql": "sql_server", "oracle": "oracle",
    "oracle_bip": "oracle_bip", "oracle_fusion": "oracle_fusion", "oracle_api": "oracle_fusion",
}


def slug_for_connection_type(connection_type: str) -> str:
    """The slug a connection of this type would have got if the prompt named it."""
    return _SLUG_BY_CONNECTION_TYPE.get(connection_type, connection_type)


# ── Workspace inventory ───────────────────────────────────────────────────


@dataclass(frozen=True)
class InventoryConnection:
    id: str
    name: str
    type: str
    capabilities: tuple[str, ...] = ()

    def can(self, capability: str) -> bool:
        # An empty list means a row from before capabilities existed: both.
        return not self.capabilities or capability in self.capabilities

    def can_read(self) -> bool:
        return self.type in SOURCE_CONNECTORS and self.can("read")

    def can_write(self) -> bool:
        return self.type in DESTINATION_CONNECTORS and self.can("write")


@dataclass(frozen=True)
class InventoryFile:
    id: str
    name: str
    path: str
    format: str = ""


@dataclass(frozen=True)
class InventoryTable:
    schema_name: str
    name: str

    @property
    def qualified(self) -> str:
        return f"{self.schema_name}.{self.name}"


@dataclass(frozen=True)
class WorkspaceInventory:
    """What this workspace really has, as ids and names only."""
    connections: tuple[InventoryConnection, ...] = ()
    files: tuple[InventoryFile, ...] = ()
    tables: tuple[InventoryTable, ...] = ()
    # Whether a path the prompt names exists outside Storage (a sample file,
    # or one already in the data folder). Left to the caller: this module does
    # no I/O.
    file_on_disk: Callable[[str], bool] | None = None

    def connection(self, connection_id: str) -> InventoryConnection | None:
        return next((c for c in self.connections if c.id == connection_id), None)

    def find_file(self, name: str) -> InventoryFile | None:
        lowered = (name or "").strip().lower()
        for candidate in self.files:  # newest first
            if candidate.name.lower() == lowered or candidate.path.lower() == lowered:
                return candidate
        return None

    def find_table(self, name: str) -> InventoryTable | None:
        lowered = (name or "").strip().lower()
        if not lowered:
            return None
        for table in self.tables:
            if table.qualified.lower() == lowered:
                return table
        return next((t for t in self.tables if t.name.lower() == lowered), None)


# ── Data shapes ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ClarificationOption:
    """One real choice on the card. Ids and names only, never a secret."""
    id: str
    label: str
    kind: str   # connection | file | keep_file | table | new_table | new_connection
    recommended: bool = False
    connection_id: str = ""
    connection_name: str = ""
    connection_type: str = ""
    file_id: str = ""
    file_name: str = ""
    schema_name: str = ""
    table_name: str = ""
    new_connection_type: str = ""
    new_connection_label: str = ""


@dataclass(frozen=True)
class ClarificationQuestion:
    """One question to put to the user."""
    field: str              # internal key, e.g. "source_auth"
    question: str           # display text
    chips: tuple[str, ...]  # one-click answer options; first chip = recommended
    required: bool = True   # if False, "Skip" is also a valid answer
    slot: str = ""          # source | destination | source_table | destination_table | write_mode
    label: str = ""         # short name for the answer in the transcript
    options: tuple[ClarificationOption, ...] = ()
    connection_id: str = ""     # the connection a table question is about
    connection_name: str = ""
    table_hint: str = ""        # the table the prompt seems to mean


@dataclass(frozen=True)
class DraftEndpoint:
    """A source or destination the draft has to use. Ids and names only."""
    kind: str   # connection | file | table | new_table | file_output | text
    name: str = ""              # what the person reads
    connection_id: str = ""
    connection_type: str = ""
    table: str = ""             # table on a connection, or a managed table's name
    schema: str = ""
    file_path: str = ""
    file_format: str = ""
    # False when the name was only implied, so a model's better one may stand.
    explicit: bool = True
    # The missing file this one stands in for.
    replaces: str = ""


@dataclass(frozen=True)
class ClarificationSet:
    """The full clarification ask for one prompt."""
    source_type: str | None     # detected source kind ("oracle_bip", "csv_file", …)
    sink_type: str | None       # detected sink kind ("sql_server", "parquet", …)
    questions: tuple[ClarificationQuestion, ...]
    detected_intent: str        # short echo back to user
    original_prompt: str        # round-trip back to planner after answers
    write_mode: str = ""        # the mode the prompt already stated
    merge_on: tuple[str, ...] = ()


@dataclass(frozen=True)
class DraftSlots:
    """What a request already settles, and what still has to be asked."""
    source_type: str | None
    sink_type: str | None
    source: DraftEndpoint | None
    destination: DraftEndpoint | None
    # Every file a request reads, when it names more than one.
    source_files: tuple[DraftEndpoint, ...]
    questions: tuple[ClarificationQuestion, ...]
    detected_intent: str
    original_prompt: str
    write_mode: str = ""
    merge_on: tuple[str, ...] = ()
    schedule: str = ""
    # The file the request named that Storage doesn't have.
    missing_file: str = ""


# ── Reading the sentence ──────────────────────────────────────────────────


@dataclass
class _Mention:
    kind: str       # connection | file | table | managed | technology
    start: int
    role: str = ""  # source | destination | "" when the sentence doesn't say
    text: str = ""
    connection: InventoryConnection | None = None
    table: InventoryTable | None = None
    technology: Technology | None = None


def _mask(text: str, start: int, end: int) -> str:
    return text[:start] + " " * (end - start) + text[end:]


def _role_before(masked: str, position: int) -> str:
    role = ""
    for cue in _ROLE_CUE_RE.finditer(masked[:position]):
        role = "source" if cue.group("source") else "destination"
    return role


def _scan_mentions(text: str, inventory: WorkspaceInventory) -> tuple[list[_Mention], str]:
    """Find what a request names, in the order it names them.

    Saved names go first and are blanked out as they match, so a connection
    called "Sales Postgres" is that connection and not also the Postgres
    technology. Roles are read from what is left, so words inside a name can't
    pose as cues.
    """
    masked = text
    mentions: list[_Mention] = []

    for conn in sorted(inventory.connections, key=lambda c: len(c.name), reverse=True):
        if len(conn.name.strip()) < 3:
            continue
        found = re.search(rf"(?<![\w-]){re.escape(conn.name.strip())}(?![\w-])", masked, re.IGNORECASE)
        if found:
            mentions.append(_Mention("connection", found.start(), text=conn.name, connection=conn))
            masked = _mask(masked, found.start(), found.end())

    for table in sorted(inventory.tables, key=lambda t: len(t.qualified), reverse=True):
        found = re.search(rf"(?<![\w.-]){re.escape(table.qualified)}(?![\w-])", masked, re.IGNORECASE)
        if found:
            mentions.append(_Mention("table", found.start(), text=table.qualified, table=table))
            masked = _mask(masked, found.start(), found.end())

    for found in list(_FILE_NAME_RE.finditer(masked)):
        mentions.append(_Mention("file", found.start(), text=found.group(0)))
        masked = _mask(masked, found.start(), found.end())

    # "the orders file" means the uploaded orders.csv.
    for candidate in inventory.files:
        stem = candidate.name.rsplit(".", 1)[0]
        if len(stem) < 3:
            continue
        found = re.search(rf"(?<![\w-]){re.escape(stem)}\s+(?:file|csv|json|parquet|excel|spreadsheet)\b", masked, re.IGNORECASE)
        if found:
            mentions.append(_Mention("file", found.start(), text=candidate.name))
            masked = _mask(masked, found.start(), found.end())

    for found in list(_MANAGED_TABLE_RE.finditer(masked)):
        mentions.append(_Mention("managed", found.start(), text=found.group("name") or ""))
        masked = _mask(masked, found.start(), found.end())

    for tech in _TECHNOLOGIES:
        for found in list(tech.pattern.finditer(masked)):
            mentions.append(_Mention("technology", found.start(), text=found.group(0), technology=tech))
            masked = _mask(masked, found.start(), found.end())

    mentions.sort(key=lambda m: m.start)
    for mention in mentions:
        mention.role = _role_before(masked, mention.start)
        if mention.technology is not None and mention.technology.write_only:
            mention.role = "destination"
    return mentions, masked


_STORAGE_SLUGS = frozenset({"s3", "azure_blob", "gcs", "sharepoint", "onedrive", "sftp"})


def _specificity(mention: _Mention) -> int:
    """Which mention decides an end of the pipeline, lowest first.

    A saved name beats a technology, and a system beats a format: in "Load CSV
    from S3 to Snowflake" S3 is the source and CSV only describes it. A storage
    system also beats a file name, because "orders.csv from S3" is on S3, not in
    Storage. "database" and "warehouse" come last since they are often just
    words ("copy warehouse stock from Postgres").
    """
    tech = mention.technology
    if tech is None:
        return 1 if mention.kind == "file" else 0
    if tech.slug in _STORAGE_SLUGS:
        return 0
    if tech.kind == "connection":
        return 1
    return 2 if tech.kind == "file" else 3


def _assign_roles(mentions: list[_Mention]) -> tuple[list[_Mention], list[_Mention]]:
    """Split mentions by end of the pipeline. No cue means first free end.

    Each list comes back with the mention that decides that end first.
    """
    sources = [m for m in mentions if m.role == "source"]
    destinations = [m for m in mentions if m.role == "destination"]
    for mention in mentions:
        if mention.role:
            continue
        if not sources:
            mention.role = "source"
            sources.append(mention)
        elif not destinations:
            mention.role = "destination"
            destinations.append(mention)
    # sorted() is stable, so equal ones stay in the order they were named.
    return sorted(sources, key=_specificity), sorted(destinations, key=_specificity)


def _clean_table_word(word: str) -> str:
    word = (word or "").strip().strip("`'\".,")
    if not word or word.lower() in _NOT_A_TABLE_NAME or not re.match(r"^[A-Za-z_]", word):
        return ""
    return word


def _table_words(masked: str) -> dict[str, str]:
    """Tables named in passing, by the end of the pipeline they sit at."""
    named: dict[str, str] = {}
    for pattern in (_TABLE_THEN_NAME_RE, _NAME_THEN_TABLE_RE):
        for found in pattern.finditer(masked):
            word = _clean_table_word(found.group("name"))
            if not word:
                continue
            role = _role_before(masked, found.start()) or "source"
            named.setdefault(role, word)
    return named


def _copied_table(masked: str) -> str:
    found = _COPY_WHAT_RE.search(masked)
    return _clean_table_word(found.group("name")) if found else ""


def _slug(text: str) -> str:
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    return "_".join(words[:6])[:50].strip("_")


def _suggest_table_name(prompt: str, hint: str) -> str:
    if hint:
        return _slug(hint.rsplit(".", 1)[-1])
    found = _PURPOSE_RE.search(prompt or "")
    return _slug(found.group("what")) if found else ""


def _split_table(name: str) -> tuple[str, str]:
    """"public.customers" -> ("public", "customers")."""
    if "." in (name or ""):
        schema, _, table = name.rpartition(".")
        return schema, table
    return "", name or ""


def _file_format(name: str, declared: str = "") -> str:
    if declared in ("csv", "json", "parquet", "excel", "xml"):
        return declared
    extension = (name or "").rsplit(".", 1)[-1].lower()
    return _FILE_FORMAT_BY_EXTENSION.get(extension, declared or "csv")


def _stated_write_mode(prompt: str) -> tuple[str, tuple[str, ...]]:
    mode = next((m for m, pattern in _WRITE_MODE_PATTERNS if pattern.search(prompt or "")), "")
    key = _MERGE_KEY_RE.search(prompt or "") if mode == "merge" else None
    return mode, ((key.group(1),) if key else ())


# ── Building the card's options ───────────────────────────────────────────


_MAX_REAL_OPTIONS = 5  # plus the "New connection" chip: about six in all


def _prompt_words(prompt: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", (prompt or "").lower()) if len(w) > 2}


def _relevance(name: str, words: set[str]) -> int:
    return len({w for w in re.findall(r"[a-z0-9]+", (name or "").lower()) if w in words})


def _connection_option(conn: InventoryConnection, *, with_type: bool) -> ClarificationOption:
    return ClarificationOption(
        id=f"connection:{conn.id}",
        label=f"{conn.name} ({connector_label(conn.type)})" if with_type else conn.name,
        kind="connection",
        connection_id=conn.id,
        connection_name=conn.name,
        connection_type=conn.type,
    )


def _file_option(item: InventoryFile, *, with_note: bool = True) -> ClarificationOption:
    return ClarificationOption(
        id=f"file:{item.id or item.path}",
        label=f"{item.name} (uploaded file)" if with_note else item.name,
        kind="file",
        file_id=item.id,
        file_name=item.name,
    )


def _table_option(table: InventoryTable) -> ClarificationOption:
    return ClarificationOption(
        id=f"table:{table.qualified}",
        label=f"{table.qualified} (managed table)",
        kind="table",
        schema_name=table.schema_name,
        table_name=table.name,
    )


def _new_connection_option(tech: Technology | None = None) -> ClarificationOption:
    label = f"New {tech.label} connection" if tech is not None and tech.kind == "connection" else "New connection"
    return ClarificationOption(
        id="new_connection",
        label=label,
        kind="new_connection",
        new_connection_type=tech.new_connection_type if tech is not None else "",
        new_connection_label=tech.label if tech is not None and tech.kind == "connection" else "",
    )


def _new_table_option(suggested: str) -> ClarificationOption:
    return ClarificationOption(
        id="new_table",
        label="New managed table in this workspace",
        kind="new_table",
        recommended=True,
        schema_name="default",
        table_name=suggested,
    )


def _mixed_options(
    groups: Iterable[list[ClarificationOption]],
    *,
    reserved: int = 0,
) -> list[ClarificationOption]:
    """Take from each kind in turn, so one long list can't crowd out the rest.
    Each kind stays together in what comes back."""
    queues = [list(group) for group in groups]
    taken: list[list[ClarificationOption]] = [[] for _ in queues]
    room = _MAX_REAL_OPTIONS - reserved
    while room > 0 and any(queues):
        for i, queue in enumerate(queues):
            if queue and room > 0:
                taken[i].append(queue.pop(0))
                room -= 1
    return [option for group in taken for option in group]


def _rank_connections(conns: Iterable[InventoryConnection], words: set[str]) -> list[InventoryConnection]:
    """What the prompt points at first, then databases a pipeline can really run
    against, then the rest. Databases with no runtime yet come last."""
    def family(conn: InventoryConnection) -> int:
        if conn.type in _RUNNABLE_DATABASE_TYPES:
            return 0
        return 2 if conn.type in TABLE_CONNECTION_TYPES else 1

    return sorted(conns, key=lambda c: (-_relevance(c.name, words), family(c), c.name.lower()))


def _rank_named(items: Iterable, words: set[str], key: Callable) -> list:
    # sorted() is stable, so equally relevant files stay newest first.
    return sorted(items, key=lambda item: -_relevance(key(item), words))


def _question(field: str, slot: str, label: str, text: str, options: list[ClarificationOption], **extra) -> ClarificationQuestion:
    return ClarificationQuestion(
        field=field,
        question=text,
        chips=tuple(o.label for o in options),
        slot=slot,
        label=label,
        options=tuple(options),
        **extra,
    )


def _generic_source_question(prompt: str, inventory: WorkspaceInventory, hint: str) -> ClarificationQuestion:
    words = _prompt_words(prompt)
    connections = _rank_connections([c for c in inventory.connections if c.can_read()], words)
    files = _rank_named(inventory.files, words, lambda f: f.name)
    tables = _rank_named(inventory.tables, words, lambda t: t.name)
    groups = [
        (_relevance(connections[0].name, words) if connections else 0,
         [_connection_option(c, with_type=True) for c in connections]),
        (_relevance(files[0].name, words) if files else 0, [_file_option(f) for f in files]),
        (_relevance(tables[0].name, words) if tables else 0, [_table_option(t) for t in tables]),
    ]
    # Whatever the prompt's own words point at goes first, whatever its kind:
    # "shows open tickets" puts an uploaded tickets.csv at the front.
    groups.sort(key=lambda group: -group[0])
    options = _mixed_options(group[1] for group in groups) + [_new_connection_option()]
    return _question(
        "source", "source", "Source",
        "Where should I read the data from?",
        options,
        table_hint=hint,
    )


def _generic_destination_question(
    prompt: str,
    inventory: WorkspaceInventory,
    hint: str,
    fallback_name: str = "",
) -> ClarificationQuestion:
    words = _prompt_words(prompt)
    connections = _rank_connections([c for c in inventory.connections if c.can_write()], words)
    tables = _rank_named(inventory.tables, words, lambda t: t.name)
    options = (
        [_new_table_option(_suggest_table_name(prompt, hint) or fallback_name)]
        + _mixed_options(
            [
                [_connection_option(c, with_type=True) for c in connections],
                [_table_option(t) for t in tables],
            ],
            reserved=1,
        )
        + [_new_connection_option()]
    )
    return _question(
        "destination", "destination", "Destination",
        "Where should I write the result?",
        options,
        table_hint=_suggest_table_name(prompt, hint) or fallback_name,
    )


def _technology_source_question(tech: Technology, prompt: str, inventory: WorkspaceInventory, hint: str) -> ClarificationQuestion:
    if tech.kind == "file":
        matching = [f for f in inventory.files if _file_format(f.name, f.format) == tech.file_format]
        options = [_file_option(f, with_note=False) for f in matching[:_MAX_REAL_OPTIONS]]
        text = (
            f"Which {tech.label} file should I read?" if matching
            else f"This workspace has no {tech.label} files in Storage yet. "
                 "Upload one on the Storage page, or type its name here."
        )
        return _question("source", "source", "Source", text, options + [_new_connection_option()])

    connections = _rank_connections(
        [c for c in inventory.connections if c.type in tech.connection_types and c.can_read()],
        _prompt_words(prompt),
    )
    options = [_connection_option(c, with_type=tech.kind == "family") for c in connections[:_MAX_REAL_OPTIONS]]
    if connections:
        text = f"Which {tech.label} connection should I read from?"
    else:
        text = f"This workspace has no {tech.label} connection yet. Create one here so I can read from it."
    return _question(
        "source", "source", "Source", text, options + [_new_connection_option(tech)], table_hint=hint,
    )


def _technology_destination_question(tech: Technology, prompt: str, inventory: WorkspaceInventory, hint: str) -> ClarificationQuestion:
    connections = _rank_connections(
        [c for c in inventory.connections if c.type in tech.connection_types and c.can_write()],
        _prompt_words(prompt),
    )
    options = [_connection_option(c, with_type=tech.kind == "family") for c in connections[:_MAX_REAL_OPTIONS]]
    if connections:
        text = f"Which {tech.label} connection should I write to?"
    else:
        text = f"This workspace has no {tech.label} connection yet. Create one here so I can write to it."
    return _question(
        "destination", "destination", "Destination", text, options + [_new_connection_option(tech)],
        table_hint=_suggest_table_name(prompt, hint),
    )


def _source_table_question(conn: InventoryConnection) -> ClarificationQuestion:
    return ClarificationQuestion(
        field="source_table",
        question=f"Which table in {conn.name} should I read?",
        chips=(),
        slot="source_table",
        label="Table",
        connection_id=conn.id,
        connection_name=conn.name,
    )


def _destination_table_question(conn: InventoryConnection, hint: str) -> ClarificationQuestion:
    return ClarificationQuestion(
        field="destination_table",
        question=f"Which table in {conn.name} should I write to?",
        chips=(),
        slot="destination_table",
        label="Table",
        connection_id=conn.id,
        connection_name=conn.name,
        table_hint=hint,
    )


# The wording here is what a person reads; the value is the card's id for it.
WRITE_MODE_CHIPS: tuple[str, ...] = (
    "Add new rows to the table",
    "Replace the whole table",
    "Update rows that match",
)


def _write_mode_question() -> ClarificationQuestion:
    return ClarificationQuestion(
        field="write_mode",
        question="How should the rows be written?",
        chips=WRITE_MODE_CHIPS,
        slot="write_mode",
        label="How to write",
    )


def _merge_key_question() -> ClarificationQuestion:
    return ClarificationQuestion(
        field="merge_key",
        question="Which column identifies a row?",
        chips=(),
        slot="merge_key",
        label="Match on",
    )


# ── Resolving each end of the pipeline ────────────────────────────────────


def _resolve_source(
    mentions: list[_Mention],
    masked: str,
    prompt: str,
    inventory: WorkspaceInventory,
    hint: str,
) -> tuple[DraftEndpoint | None, tuple[DraftEndpoint, ...], list[ClarificationQuestion]]:
    first = mentions[0] if mentions else None
    if first is None:
        return _bare_table_source(masked, prompt, inventory, hint)

    if first.kind == "file":
        files = [m for m in mentions if m.kind == "file"]
        found: list[DraftEndpoint] = []
        missing: list[str] = []
        for mention in files:
            stored = inventory.find_file(mention.text)
            if stored is not None:
                found.append(DraftEndpoint(
                    kind="file", name=stored.name, file_path=stored.path,
                    file_format=_file_format(stored.name, stored.format),
                ))
            elif inventory.file_on_disk is not None and inventory.file_on_disk(mention.text):
                found.append(DraftEndpoint(
                    kind="file", name=mention.text, file_path=mention.text,
                    file_format=_file_format(mention.text),
                ))
            else:
                missing.append(mention.text)
        if missing:
            return None, tuple(found), [_missing_file_question(missing[0], prompt, inventory)]
        if len(found) == 1:
            return found[0], tuple(found), []
        return None, tuple(found), []

    if first.kind == "connection":
        conn = first.connection
        if not conn.can_read():
            question = _generic_source_question(prompt, inventory, hint)
            return None, (), [_with_text(question, f"{conn.name} cannot be read from. Where should I read the data from instead?")]
        schema, table = _split_table(hint)
        endpoint = DraftEndpoint(
            kind="connection", name=conn.name, connection_id=conn.id, connection_type=conn.type,
            table=table, schema=schema,
        )
        if conn.type in TABLE_CONNECTION_TYPES and not table:
            return endpoint, (), [_source_table_question(conn)]
        return endpoint, (), []

    if first.kind == "table":
        return _managed_table_endpoint(first.table), (), []

    if first.kind == "managed":
        table = inventory.find_table(first.text) if first.text else None
        if table is not None:
            return _managed_table_endpoint(table), (), []
        return None, (), [_managed_source_question(first.text, inventory)]

    return None, (), [_technology_source_question(first.technology, prompt, inventory, hint)]


def _bare_table_source(masked, prompt, inventory, hint):
    """"from the tickets table" counts only if that managed table exists."""
    word = _table_words(masked).get("source", "")
    table = inventory.find_table(word) if word else None
    if table is not None:
        return _managed_table_endpoint(table), (), []
    return None, (), [_generic_source_question(prompt, inventory, hint)]


def _managed_table_endpoint(table: InventoryTable) -> DraftEndpoint:
    return DraftEndpoint(kind="table", name=table.qualified, table=table.name, schema=table.schema_name)


def _with_text(question: ClarificationQuestion, text: str) -> ClarificationQuestion:
    return _question(
        question.field, question.slot, question.label, text, list(question.options),
        table_hint=question.table_hint,
    )


def _missing_file_question(name: str, prompt: str, inventory: WorkspaceInventory) -> ClarificationQuestion:
    words = _prompt_words(prompt)
    files = _rank_named(inventory.files, words, lambda f: f.name)
    options = [_file_option(f) for f in files[:_MAX_REAL_OPTIONS - 1]]
    options.append(ClarificationOption(
        id=f"keep_file:{name}",
        label=f"Keep {name}. I will upload it later.",
        kind="keep_file",
        file_name=name,
    ))
    return _question(
        "source", "source", "Source",
        f"I could not find {name} in this workspace's Storage. Which file should I read instead?",
        options + [_new_connection_option()],
    )


def _managed_source_question(name: str, inventory: WorkspaceInventory) -> ClarificationQuestion:
    options = [_table_option(t) for t in inventory.tables[:_MAX_REAL_OPTIONS]]
    if name:
        text = f"I could not find a managed table called {name}. Which one should I read?"
    elif options:
        text = "Which managed table should I read?"
    else:
        text = "This workspace has no managed tables yet. Where should I read the data from instead?"
    return _question("source", "source", "Source", text, options + [_new_connection_option()])


def _resolve_destination(
    mentions: list[_Mention],
    masked: str,
    prompt: str,
    inventory: WorkspaceInventory,
    hint: str,
    has_write_mode: bool,
    fallback_name: str = "",
) -> tuple[DraftEndpoint | None, list[ClarificationQuestion]]:
    first = mentions[0] if mentions else None
    if first is None:
        return _bare_table_destination(masked, prompt, inventory, hint, has_write_mode, fallback_name)

    if first.kind == "file":
        return DraftEndpoint(
            kind="file_output", name=first.text, file_path=first.text,
            file_format=_file_format(first.text),
        ), []

    if first.kind == "connection":
        conn = first.connection
        if not conn.can_write():
            question = _generic_destination_question(prompt, inventory, hint, fallback_name)
            return None, [_with_text(question, f"{conn.name} cannot be written to. Where should I write the result instead?")]
        schema, table = _split_table(hint)
        endpoint = DraftEndpoint(
            kind="connection", name=conn.name, connection_id=conn.id, connection_type=conn.type,
            table=table, schema=schema,
        )
        questions: list[ClarificationQuestion] = []
        if conn.type in TABLE_CONNECTION_TYPES and not table:
            questions.append(_destination_table_question(conn, ""))
        if conn.type in WRITE_MODE_CONNECTION_TYPES and not has_write_mode:
            questions.append(_write_mode_question())
        return endpoint, questions

    if first.kind == "table":
        return _existing_table_destination(first.table, has_write_mode)

    if first.kind == "managed":
        if first.text:
            table = inventory.find_table(first.text)
            if table is not None:
                return _existing_table_destination(table, has_write_mode)
            schema, name = _split_table(first.text)
            return DraftEndpoint(
                kind="new_table", name=_managed_name(schema, name), table=name, schema=schema or "default",
            ), []
        return DraftEndpoint(
            kind="new_table", name="a new managed table", schema="default", explicit=False,
            table=_suggest_table_name(prompt, hint) or fallback_name,
        ), []

    tech = first.technology
    if tech.kind == "file":
        extension = {"parquet": "parquet", "csv": "csv", "json": "json", "excel": "xlsx", "xml": "xml", "delta": ""}
        stem = _suggest_table_name(prompt, hint) or "result"
        suffix = extension.get(tech.file_format, "")
        return DraftEndpoint(
            kind="file_output", name=f"a {tech.label} file", explicit=False,
            file_path=f"output/{stem}.{suffix}" if suffix else f"output/{stem}",
            file_format=tech.file_format,
        ), []
    return None, [_technology_destination_question(tech, prompt, inventory, hint)]


def _bare_table_destination(masked, prompt, inventory, hint, has_write_mode, fallback_name):
    """"into the team_summary table" with no connection means a managed table."""
    word = _table_words(masked).get("destination", "")
    if not word:
        return None, [_generic_destination_question(prompt, inventory, hint, fallback_name)]
    table = inventory.find_table(word)
    if table is not None:
        return _existing_table_destination(table, has_write_mode)
    schema, name = _split_table(word)
    return DraftEndpoint(
        kind="new_table", name=_managed_name(schema, name), table=name, schema=schema or "default",
    ), []


def _existing_table_destination(table: InventoryTable, has_write_mode: bool):
    endpoint = _managed_table_endpoint(table)
    return endpoint, ([] if has_write_mode else [_write_mode_question()])


def _managed_name(schema: str, name: str) -> str:
    return f"{schema or 'default'}.{name}"


def _endpoint_label(endpoint: DraftEndpoint | None, tech: Technology | None) -> str:
    if endpoint is not None and endpoint.kind != "text":
        return endpoint.name
    if tech is None:
        return ""
    if tech.kind == "file":
        return f"a {tech.label} file"
    if tech.kind == "family":
        return f"a {tech.label}"
    return tech.label


def _first_technology(mentions: list[_Mention]) -> Technology | None:
    return next((m.technology for m in mentions if m.technology is not None), None)


def _slug_for(mentions: list[_Mention]) -> str | None:
    first = mentions[0] if mentions else None
    if first is None:
        return None
    if first.technology is not None:
        return first.technology.slug
    if first.connection is not None:
        return slug_for_connection_type(first.connection.type)
    if first.kind == "file":
        extension = first.text.rsplit(".", 1)[-1].lower()
        return {"csv": "csv_file", "tsv": "csv_file", "txt": "csv_file", "json": "json_file",
                "xlsx": "excel_file", "xls": "excel_file", "xml": "xml_file"}.get(extension, "parquet")
    return "managed_table"


# ── Main entry points ─────────────────────────────────────────────────────


def is_build_request(prompt: str) -> bool:
    """Does this ask for a new pipeline, in so many words or by moving data?"""
    if not prompt or not prompt.strip():
        return False
    if _BUILD_INTENT_RE.search(prompt):
        return True
    if _MOVE_VERB_RE.search(prompt) and (_MOVE_ENDPOINT_RE.search(prompt) or _FILE_NAME_RE.search(prompt)):
        return True
    if _MOVE_BOTH_ENDS_RE.search(prompt):
        return True
    return bool(_COMBINE_VERB_RE.search(prompt) and _FILE_NAME_RE.search(prompt))


def asks_for_new_pipeline(prompt: str) -> bool:
    """Says "build a pipeline" in so many words, so it isn't an answer to a card."""
    return bool(_BUILD_INTENT_RE.search(prompt or ""))


def _endpoint_from_mention(mention: _Mention, role: str, inventory: WorkspaceInventory) -> DraftEndpoint | None:
    """What one thing named in a typed answer points at, if it is that clear."""
    if mention.kind == "connection":
        conn = mention.connection
        allowed = conn.can_read() if role == "source" else conn.can_write()
        if not allowed:
            return None
        return DraftEndpoint(kind="connection", name=conn.name, connection_id=conn.id, connection_type=conn.type)
    if mention.kind == "table":
        return _managed_table_endpoint(mention.table)
    if mention.kind == "file":
        stored = inventory.find_file(mention.text)
        if role == "destination":
            return DraftEndpoint(
                kind="file_output", name=mention.text, file_path=mention.text,
                file_format=_file_format(mention.text),
            )
        if stored is not None:
            return DraftEndpoint(
                kind="file", name=stored.name, file_path=stored.path,
                file_format=_file_format(stored.name, stored.format),
            )
        return DraftEndpoint(
            kind="file", name=mention.text, file_path=mention.text, file_format=_file_format(mention.text),
        )
    if mention.kind == "managed":
        table = inventory.find_table(mention.text) if mention.text else None
        if table is not None:
            return _managed_table_endpoint(table)
        if role != "destination":
            return None
        if mention.text:
            schema, name = _split_table(mention.text)
            return DraftEndpoint(kind="new_table", name=_managed_name(schema, name), table=name, schema=schema or "default")
        return DraftEndpoint(kind="new_table", name="a new managed table", schema="default", explicit=False)
    tech = mention.technology
    if tech is None or tech.kind == "file":
        return None
    # "the Postgres one" only settles it when there is just one.
    matching = [
        c for c in inventory.connections
        if c.type in tech.connection_types and (c.can_read() if role == "source" else c.can_write())
    ]
    if len(matching) != 1:
        return None
    conn = matching[0]
    return DraftEndpoint(kind="connection", name=conn.name, connection_id=conn.id, connection_type=conn.type)


def endpoints_in_text(text: str, inventory: WorkspaceInventory) -> tuple[DraftEndpoint | None, DraftEndpoint | None]:
    """Read (source, destination) out of an answer typed in the chat box."""
    sources, destinations = _assign_roles(_scan_mentions(text or "", inventory)[0])
    source = _endpoint_from_mention(sources[0], "source", inventory) if sources else None
    destination = _endpoint_from_mention(destinations[0], "destination", inventory) if destinations else None
    return source, destination


def endpoint_for_typed_answer(text: str, role: str, inventory: WorkspaceInventory) -> DraftEndpoint:
    """An answer typed into one question's box, so the end is already known.

    Something that names nothing here is kept as text: it can still guide a
    model, but it won't be bound to a step.
    """
    typed = (text or "").strip()
    mentions, _ = _scan_mentions(typed, inventory)
    for mention in sorted(mentions, key=_specificity):
        endpoint = _endpoint_from_mention(mention, role, inventory)
        if endpoint is not None:
            return endpoint
    if role == "destination" and re.fullmatch(r"[A-Za-z_][\w.]*", typed):
        schema, name = _split_table(typed)
        return DraftEndpoint(kind="new_table", name=_managed_name(schema, name), table=name, schema=schema or "default")
    table = inventory.find_table(typed)
    if table is not None:
        return _managed_table_endpoint(table)
    return DraftEndpoint(kind="text", name=typed)


def split_table_name(name: str) -> tuple[str, str]:
    """"public.customers" -> ("public", "customers")."""
    return _split_table(name)


def file_format_of(name: str, declared: str = "") -> str:
    """The Source connector that reads this file."""
    return _file_format(name, declared)


def _extra_questions(prompt: str) -> list[ClarificationQuestion]:
    """The questions that aren't about either end of the pipeline."""
    lowered = prompt.lower()
    questions: list[ClarificationQuestion] = []
    # Schedule question — only if the user said this should be recurring
    # but didn't pin an interval.
    if _SCHEDULE_HINT_RE.search(lowered) and not _SCHEDULE_MENTIONED_RE.search(lowered):
        questions.extend(_questions_for_schedule())
    # Cleaning — user said "clean" but didn't say what to clean.
    if "clean" in lowered and not _CLEANING_DETAIL_RE.search(lowered):
        questions.extend(_questions_for_cleaning())
    return questions


def detect_draft_slots(
    prompt: str,
    inventory: WorkspaceInventory,
    *,
    assume_build: bool = False,
) -> DraftSlots | None:
    """Work out both ends of a build request against this workspace.

    Returns None when the prompt is not a build request. Otherwise a
    ``DraftSlots`` saying what the request already settles (with real ids) and
    which questions are left; no questions means draft straight away.
    ``assume_build`` is for a caller that already knows it is one.

    Sub-10ms. No LLM, no I/O, no tool calls.
    """
    if not assume_build and not is_build_request(prompt):
        return None

    mentions, masked = _scan_mentions(prompt, inventory)
    source_mentions, destination_mentions = _assign_roles(mentions)

    table_words = _table_words(masked)
    copied = _copied_table(masked)
    source_hint = table_words.get("source") or copied
    destination_hint = table_words.get("destination") or source_hint

    write_mode, merge_on = _stated_write_mode(prompt)

    source, source_files, source_questions = _resolve_source(
        source_mentions, masked, prompt, inventory, source_hint,
    )
    # A new table with nothing else to go on is named after what it reads.
    fallback_name = _slug("_".join(f.name.rsplit(".", 1)[0] for f in source_files))
    destination, destination_questions = _resolve_destination(
        destination_mentions, masked, prompt, inventory, destination_hint, bool(write_mode), fallback_name,
    )

    # An upsert the request asked for outright, onto a target it named outright,
    # still needs to know what to match on.
    if write_mode == "merge" and not merge_on and destination is not None and (
        destination.kind == "table" or destination.connection_type in WRITE_MODE_CONNECTION_TYPES
    ):
        destination_questions = [*destination_questions, _merge_key_question()]

    source_label = _endpoint_label(source, _first_technology(source_mentions))
    if not source_label and len(source_files) > 1:
        source_label = " and ".join(f.name for f in source_files)
    if not source_label and source_mentions and source_mentions[0].kind == "file":
        source_label = source_mentions[0].text
    destination_label = _endpoint_label(destination, _first_technology(destination_mentions))

    schedule = _SCHEDULE_PHRASE_RE.search(prompt)
    return DraftSlots(
        source_type=_slug_for(source_mentions),
        sink_type=_slug_for(destination_mentions),
        source=source,
        destination=destination,
        source_files=source_files,
        questions=tuple(source_questions + destination_questions + _extra_questions(prompt)),
        detected_intent=_summary_sentence(source_label, destination_label),
        original_prompt=prompt,
        write_mode=write_mode,
        merge_on=merge_on,
        schedule=schedule.group(1).lower() if schedule else "",
        missing_file=next(
            (o.file_name for q in source_questions for o in q.options if o.kind == "keep_file"), "",
        ),
    )


def clarification_set_from_slots(slots: DraftSlots) -> ClarificationSet:
    return ClarificationSet(
        source_type=slots.source_type,
        sink_type=slots.sink_type,
        questions=slots.questions,
        detected_intent=slots.detected_intent,
        original_prompt=slots.original_prompt,
        write_mode=slots.write_mode,
        merge_on=slots.merge_on,
    )


def detect_missing_draft_fields(
    prompt: str,
    inventory: WorkspaceInventory | None = None,
) -> ClarificationSet | None:
    """Inspect a build-pipeline prompt and return the questions to ask
    before drafting. Returns None when the prompt is either:
      * not a build-pipeline request, or
      * already specifies enough detail to draft directly.

    With an inventory the questions list this workspace's real options. Without
    one (the workspace could not be read) they fall back to generic chips.

    Sub-10ms. No LLM, no I/O, no tool calls.
    """
    if inventory is not None:
        slots = detect_draft_slots(prompt, inventory)
        if slots is None or not slots.questions:
            return None
        return clarification_set_from_slots(slots)

    if not is_build_request(prompt):
        return None

    source_mentions, destination_mentions = _assign_roles(_scan_mentions(prompt, WorkspaceInventory())[0])
    source_type = _slug_for(source_mentions)
    sink_type = _slug_for(destination_mentions)

    questions: list[ClarificationQuestion] = []

    # Source-specific questions.
    if source_type == "oracle_bip":
        questions.extend(_questions_for_oracle_bip())
    elif source_type in ("api", "rest_api"):
        questions.extend(_questions_for_rest_api())
    elif source_type == "oracle":
        # Generic Oracle — ask whether it's BIP or direct DB.
        questions.append(ClarificationQuestion(
            field="oracle_kind",
            question="Which Oracle source is this?",
            chips=("Oracle BIP report API", "Oracle Fusion REST", "Oracle DB (direct SQL)"),
        ))

    # Sink-specific questions.
    if sink_type == "sql_server":
        questions.extend(_questions_for_sql_server_sink())
    elif sink_type in ("postgres", "mysql", "snowflake", "bigquery", "redshift",
                       "warehouse", "database"):
        questions.extend(_questions_for_db_sink())

    questions.extend(_extra_questions(prompt))

    if not questions:
        return None

    detected = _build_intent_summary(source_type, sink_type)
    return ClarificationSet(
        source_type=source_type,
        sink_type=sink_type,
        questions=tuple(questions),
        detected_intent=detected,
        original_prompt=prompt,
    )


def _summary_sentence(source_label: str, destination_label: str) -> str:
    """One-line echo so the user knows we understood the request. Plain text:
    the card shows it as it is, so markdown here shows up as raw asterisks."""
    if source_label and destination_label:
        return f"Build a pipeline from {source_label} to {destination_label}"
    if source_label:
        return f"Build a pipeline from {source_label}"
    if destination_label:
        return f"Build a pipeline that writes to {destination_label}"
    return "Build a new pipeline"


def _build_intent_summary(source_type: str | None, sink_type: str | None) -> str:
    def label(slug: str | None) -> str:
        tech = _TECH_BY_SLUG.get(slug or "")
        return tech.label if tech is not None else ""

    return _summary_sentence(label(source_type), label(sink_type))


# ── Question generators per source/sink type ──────────────────────────────
#
# Used when the workspace could not be read. With an inventory, a saved
# connection already carries its auth, so these aren't asked.


def _questions_for_oracle_bip() -> list[ClarificationQuestion]:
    """Oracle BIP is a two-step report API; auth + report path are both
    required. Provide chip-style answers covering the common cases."""
    return [
        ClarificationQuestion(
            field="oracle_bip_auth",
            question="How should Revoluzen Flow authenticate to Oracle BIP?",
            chips=(
                "Basic auth (username + password)",
                "OAuth2 token",
                "Session cookie",
                "I'll configure later",
            ),
        ),
        ClarificationQuestion(
            field="oracle_bip_report_path",
            question="What's the Oracle BIP report path (e.g. /Custom/HR/Workers)?",
            chips=("I'll fill in later",),
            required=False,
        ),
        ClarificationQuestion(
            field="oracle_bip_format",
            question="What output format does the BIP report return?",
            chips=("XML (most common)", "CSV", "JSON", "Excel"),
        ),
    ]


def _questions_for_rest_api() -> list[ClarificationQuestion]:
    return [
        ClarificationQuestion(
            field="api_auth",
            question="How does the API authenticate?",
            chips=(
                "Bearer token",
                "Basic auth",
                "API key in header",
                "OAuth2",
                "No auth (public)",
            ),
        ),
        ClarificationQuestion(
            field="api_pagination",
            question="Does the API paginate?",
            chips=("Yes — page param", "Yes — cursor / next_token", "No pagination"),
        ),
    ]


def _questions_for_sql_server_sink() -> list[ClarificationQuestion]:
    """SQL Server sink needs connection + table + write mode minimum."""
    return [
        ClarificationQuestion(
            field="sql_server_connection",
            question="Which SQL Server connection should I write to? (Or create one in Connections first.)",
            chips=("Use existing connection", "I'll set this up after the draft"),
        ),
        ClarificationQuestion(
            field="sql_server_write_mode",
            question="How should the data land in SQL Server?",
            chips=(
                "Append (insert new rows)",
                "Upsert / Merge (update if key exists)",
                "Overwrite (truncate + load)",
                "Staging table + MERGE statement",
            ),
        ),
        ClarificationQuestion(
            field="sql_server_table",
            question="What's the target table name?",
            chips=("I'll fill in later",),
            required=False,
        ),
    ]


def _questions_for_db_sink() -> list[ClarificationQuestion]:
    """Generic SQL DB sink — slightly lighter than SQL-Server-specific."""
    return [
        ClarificationQuestion(
            field="db_write_mode",
            question="How should the data land?",
            chips=("Append", "Upsert by key", "Overwrite (truncate + load)"),
        ),
        ClarificationQuestion(
            field="db_connection",
            question="Which database connection should I write to?",
            chips=("Use existing connection", "I'll configure after the draft"),
        ),
    ]


def _questions_for_schedule() -> list[ClarificationQuestion]:
    """User said something like 'on a schedule' or 'periodically' but
    didn't pin the interval. Common chips cover the 80% case."""
    return [
        ClarificationQuestion(
            field="schedule_interval",
            question="How often should this pipeline run?",
            chips=(
                "Every hour",
                "Every 6 hours",
                "Daily at 2 AM UTC",
                "Daily during business hours",
                "Weekly (Mondays)",
                "On demand only — no schedule",
            ),
            label="Schedule",
        ),
    ]


def _questions_for_cleaning() -> list[ClarificationQuestion]:
    return [
        ClarificationQuestion(
            field="cleaning_rules",
            question="What cleaning should I apply before writing?",
            chips=(
                "Just remove duplicates",
                "Drop null rows + dedupe",
                "Full data-quality rules (I'll define them)",
                "No cleaning — load as-is",
            ),
            label="Cleaning",
        ),
    ]


# ── Rendering ─────────────────────────────────────────────────────────────


def _without_blanks(values: dict) -> dict:
    return {key: value for key, value in values.items() if value not in ("", (), False)}


def clarification_payload(cset: ClarificationSet) -> dict:
    """What the chat card receives. Ids and names only."""
    return {
        "source_type": cset.source_type,
        "sink_type": cset.sink_type,
        "detected_intent": cset.detected_intent,
        "question_count": len(cset.questions),
        # Phase 3.1 (May 18 2026) — structured questions for the frontend
        # ClarifyCard. When a question bank is provided the chat panel renders
        # chips instead of plain text, and submission produces a parseable
        # answer string.
        "questions": [
            {
                "field": q.field,
                "question": q.question,
                "chips": list(q.chips),
                "required": q.required,
                **_without_blanks({
                    "slot": q.slot,
                    "label": q.label,
                    "connection_id": q.connection_id,
                    "connection_name": q.connection_name,
                    "table_hint": q.table_hint,
                }),
                **({"options": [_without_blanks(asdict(o)) for o in q.options]} if q.options else {}),
            }
            for q in cset.questions
        ],
        **_without_blanks({"write_mode": cset.write_mode}),
        **({"merge_on": list(cset.merge_on)} if cset.merge_on else {}),
        # So a connection made on the card gets the same follow-up questions as
        # one the backend listed.
        "table_connection_types": list(TABLE_CONNECTION_TYPES),
        "write_mode_connection_types": list(WRITE_MODE_CONNECTION_TYPES),
    }


def render_clarification_card(cset: ClarificationSet) -> str:
    """Render a ClarificationSet as the plain text kept in the transcript.
    Each question gets a `Q1.` line and its chips listed as options. The chat
    panel shows the structured card instead; this is the fallback and also what
    the follow-up turn is recognised by.

    The first chip in each question's list is marked as the recommended
    answer (per `_questions_for_*` ordering)."""
    lines: list[str] = [
        f"{cset.detected_intent}. {CLARIFY_MARKER}:\n",
    ]
    for i, q in enumerate(cset.questions, start=1):
        opt = "" if q.required else " (optional, you can skip it)"
        lines.append(f"Q{i}. {q.question}{opt}")
        for chip in q.chips:
            lines.append(f"  • {chip}")
        lines.append("")
    lines.append(
        "Reply with your answers in any order and I will draft the pipeline once I have what I need."
    )
    return "\n".join(lines)


def render_questions_for_model(cset: ClarificationSet) -> str:
    """The same questions, written for the Copilot's tool loop to relay.

    Kept short on purpose: a tool refusal reaches the model cut at 1000 characters.
    """
    lines = [
        "Do not draft yet. Ask the person these questions in plain words, list the "
        "options, and wait for the answer. Then call this tool again with an intent "
        "that names exactly what they chose.",
    ]
    for i, q in enumerate(cset.questions, start=1):
        options = (
            [o.label for o in q.options if o.kind != "new_connection"]
            if q.options else list(q.chips)
        )[:4]
        listed = f" Options: {'; '.join(options)}." if options else ""
        lines.append(f"{i}. {q.question}{listed}")
    lines.append("A missing connection can be added on the Connections page.")
    return "\n".join(lines)
