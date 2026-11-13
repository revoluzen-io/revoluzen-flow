"""One way to open each warehouse and cloud-storage connection (2026-11-11).

Browsing, Test connection and pipeline runs each had their own connect code,
and they had drifted apart:

* BigQuery runs ignored the saved service account key, though browsing used it.
* GCS browsing ignored the saved service account key.
* Azure Blob and ADLS Gen2 browsing read an ``account_url`` the connection
  form never saves, so only a connection string worked (ADLS never did).
* ClickHouse runs ignored the form's SSL box.
* Redshift runs needed ``redshift_connector``, which no install ships, though
  browsing and the bulk loader already used psycopg2.
* Databricks runs ignored the form's Catalog and Schema.

Each function here takes a saved connection's settings, under the names the
connection form saves (older names are still read), and returns an open
client or DB-API connection. A missing driver raises a ``RuntimeError`` with
the plain add-on sentence; bad settings raise ``ValueError``.
"""

from __future__ import annotations

import importlib
import json
import time
from typing import Any
from urllib.parse import quote

from revoluzen_flow.connections.vendor_db import truthy

DEFAULT_TIMEOUT = 15


def _driver(module: str, label: str, package: str):
    try:
        return importlib.import_module(module)
    except ImportError as exc:
        from revoluzen_flow.connections.addons import driver_message
        raise RuntimeError(driver_message(label, package)) from exc


def _user(cfg: dict[str, Any], default: str = "") -> str:
    return str(cfg.get("user") or cfg.get("username") or default)


def _text(cfg: dict[str, Any], *names: str) -> str:
    for name in names:
        value = cfg.get(name)
        if value not in (None, ""):
            return str(value).strip()
    return ""


# ── Google: BigQuery and Cloud Storage ───────────────────────────────────────


def service_account_info(cfg: dict[str, Any]) -> dict | None:
    """The saved service account key as a dict, or None when none is saved."""
    raw = cfg.get("credentials_json") or cfg.get("service_account_json")
    if not raw:
        return None
    if isinstance(raw, dict):
        return raw
    try:
        info = json.loads(str(raw))
    except ValueError:
        raise ValueError("The connection's service account key is not valid JSON.") from None
    if not isinstance(info, dict):
        raise ValueError("The connection's service account key is not valid JSON.")
    return info


def _google_credentials(cfg: dict[str, Any]) -> tuple[Any, str]:
    """(credentials or None, project) from the saved key and project fields."""
    project = _text(cfg, "project_id", "project")
    info = service_account_info(cfg)
    if not info:
        return None, project
    from google.oauth2 import service_account  # type: ignore

    return service_account.Credentials.from_service_account_info(info), project or str(info.get("project_id") or "")


def bigquery_client(cfg: dict[str, Any]):
    """A BigQuery client signed in with the saved key, or the machine's own Google login."""
    bq = _driver("google.cloud.bigquery", "BigQuery", "google-cloud-bigquery")
    credentials, project = _google_credentials(cfg)
    kwargs: dict[str, Any] = {"project": project or None, "credentials": credentials}
    if cfg.get("location"):
        kwargs["location"] = str(cfg["location"]).strip()
    return bq.Client(**kwargs)


def gcs_client(cfg: dict[str, Any]):
    """A Cloud Storage client signed in with the saved key, or the machine's own Google login."""
    storage = _driver("google.cloud.storage", "Google Cloud Storage", "google-cloud-storage")
    credentials, project = _google_credentials(cfg)
    kwargs: dict[str, Any] = {"credentials": credentials}
    if project:
        # storage.Client treats project=None as "no project" rather than
        # "work it out", so it is passed only when known.
        kwargs["project"] = project
    return storage.Client(**kwargs)


def gcs_uses_hmac(cfg: dict[str, Any]) -> bool:
    """True when the GCS connection signs in with HMAC keys (S3-compatible)."""
    method = _text(cfg, "auth_method")
    return method == "hmac" or (not method and bool(cfg.get("hmac_key_id")))


# ── Snowflake, Redshift, Databricks, ClickHouse, Oracle ──────────────────────


def snowflake_connect(cfg: dict[str, Any], *, timeout: int | None = None, use_warehouse: bool = True):
    """A Snowflake connection. ``use_warehouse=False`` leaves the warehouse out,
    so nothing that needs compute can start by accident."""
    sf = _driver("snowflake.connector", "Snowflake", "snowflake-connector-python")
    account = _text(cfg, "account")
    user = _user(cfg)
    if not (account and user):
        raise ValueError("Snowflake needs the account identifier and a username.")
    kwargs: dict[str, Any] = {
        "account": account, "user": user, "password": str(cfg.get("password") or ""),
        "database": _text(cfg, "database") or None, "schema": _text(cfg, "schema") or None,
        "role": _text(cfg, "role") or None,
    }
    if use_warehouse:
        kwargs["warehouse"] = _text(cfg, "warehouse") or None
    if timeout:
        kwargs["login_timeout"] = timeout
        kwargs["network_timeout"] = timeout
    return sf.connect(**{k: v for k, v in kwargs.items() if v is not None})


def redshift_connect(cfg: dict[str, Any], *, timeout: int = DEFAULT_TIMEOUT):
    """A Redshift connection. Redshift speaks the PostgreSQL protocol, so the
    shipped psycopg2 is enough; ``redshift_connector`` is used when added."""
    host = _text(cfg, "host")
    if not host:
        raise ValueError("Redshift needs the cluster endpoint.")
    port = int(cfg.get("port") or 5439)
    database = _text(cfg, "database") or "dev"
    ssl = truthy(cfg.get("ssl"), default=True)
    try:
        import redshift_connector  # type: ignore
    except ImportError:
        redshift_connector = None
    if redshift_connector is not None:
        return redshift_connector.connect(
            host=host, port=port, database=database, user=_user(cfg),
            password=str(cfg.get("password") or ""), ssl=ssl, timeout=timeout,
        )
    psycopg2 = _driver("psycopg2", "Redshift", "psycopg2-binary")
    return psycopg2.connect(
        host=host, port=port, dbname=database, user=_user(cfg),
        password=str(cfg.get("password") or ""),
        sslmode="require" if ssl else "prefer", connect_timeout=timeout,
    )


def databricks_host(cfg: dict[str, Any]) -> str:
    """The workspace host without https:// or a trailing slash, as the driver wants it."""
    host = _text(cfg, "host", "server_hostname", "workspace_url")
    for prefix in ("https://", "http://"):
        if host.lower().startswith(prefix):
            host = host[len(prefix):]
    return host.rstrip("/")


def databricks_token(cfg: dict[str, Any]) -> str:
    return _text(cfg, "access_token", "token", "personal_access_token")


def databricks_connect(cfg: dict[str, Any]):
    """A Databricks SQL connection, in the form's Catalog and Schema when set."""
    dbsql = _driver("databricks.sql", "Databricks", "databricks-sql-connector")
    host, http_path, token = databricks_host(cfg), _text(cfg, "http_path"), databricks_token(cfg)
    if not (host and http_path and token):
        raise ValueError("Databricks needs the workspace URL, the SQL warehouse HTTP path and an access token.")
    kwargs: dict[str, Any] = {"server_hostname": host, "http_path": http_path, "access_token": token}
    if _text(cfg, "catalog"):
        kwargs["catalog"] = _text(cfg, "catalog")
    if _text(cfg, "schema"):
        kwargs["schema"] = _text(cfg, "schema")
    return dbsql.connect(**kwargs)


def clickhouse_client(cfg: dict[str, Any], *, timeout: int = DEFAULT_TIMEOUT):
    """A ClickHouse client over HTTP(S), the port the connection form saves."""
    ch = _driver("clickhouse_connect", "ClickHouse", "clickhouse-connect")
    secure = truthy(cfg.get("ssl")) or truthy(cfg.get("secure"))
    return ch.get_client(
        host=_text(cfg, "host") or "localhost",
        port=int(cfg.get("port") or (8443 if secure else 8123)),
        username=_user(cfg, "default"),
        password=str(cfg.get("password") or ""),
        database=_text(cfg, "database") or "default",
        secure=secure,
        connect_timeout=timeout,
    )


def oracle_connect(cfg: dict[str, Any], *, timeout: int = DEFAULT_TIMEOUT):
    """An Oracle connection (thin mode, no Oracle client needed).

    The form has one box for "Service Name / SID". It is tried as a service
    name first; a listener that does not know that service gets it as a SID.
    """
    ox = _driver("oracledb", "Oracle", "oracledb")
    host = _text(cfg, "host") or "localhost"
    port = int(cfg.get("port") or 1521)
    service = _text(cfg, "service_name", "sid")
    user, password = _user(cfg), str(cfg.get("password") or "")

    def _connect(dsn: str):
        return ox.connect(user=user, password=password, dsn=dsn, tcp_connect_timeout=timeout)

    if cfg.get("sid") and not cfg.get("service_name"):
        return _connect(ox.makedsn(host, port, sid=service))
    try:
        return _connect(ox.makedsn(host, port, service_name=service or None))
    except ox.DatabaseError as exc:
        text = str(exc)
        if service and ("ORA-12514" in text or "DPY-6001" in text):
            return _connect(ox.makedsn(host, port, sid=service))
        raise


# ── Azure Blob Storage and ADLS Gen2 ─────────────────────────────────────────


class ServicePrincipalCredential:
    """A token credential for an Azure service principal (tenant, client id and
    secret) without the azure-identity package: when the SDK asks for a token,
    one is fetched from Microsoft's sign-in service."""

    def __init__(self, tenant_id: str, client_id: str, client_secret: str, *, timeout: int = DEFAULT_TIMEOUT):
        if not (tenant_id and client_id and client_secret):
            raise ValueError("The connection's service principal is incomplete: it needs the tenant ID, "
                             "client ID and client secret.")
        self._tenant, self._client_id, self._secret = tenant_id, client_id, client_secret
        self._timeout = timeout
        self._token: Any = None

    def get_token(self, *scopes: str, **_kwargs: Any):
        from azure.core.credentials import AccessToken  # type: ignore
        import httpx

        if self._token is not None and self._token.expires_on - 60 > time.time():
            return self._token
        reply = httpx.post(
            f"https://login.microsoftonline.com/{quote(self._tenant, safe='')}/oauth2/v2.0/token",
            data={
                "grant_type": "client_credentials", "client_id": self._client_id,
                "client_secret": self._secret,
                "scope": scopes[0] if scopes else "https://storage.azure.com/.default",
            },
            timeout=self._timeout,
        )
        if reply.status_code != 200:
            raise ValueError(f"Azure did not accept the service principal (HTTP {reply.status_code}).")
        body = reply.json() or {}
        token = body.get("access_token")
        if not token:
            raise ValueError("Azure sent no access token for the service principal.")
        self._token = AccessToken(token, int(time.time()) + int(body.get("expires_in") or 3600))
        return self._token


def _azure_parts(cfg: dict[str, Any]) -> tuple[str, str, str, str]:
    """(account, blob endpoint, account key, SAS token), as the file watcher reads them."""
    from revoluzen_flow.triggers.file_listing import _azure_account

    return _azure_account(cfg)


def _azure_credential(cfg: dict[str, Any], account: str, key: str, sas: str):
    method = _text(cfg, "auth_method")
    if method == "service_principal":
        return ServicePrincipalCredential(
            _text(cfg, "tenant_id"), _text(cfg, "client_id"), str(cfg.get("client_secret") or ""),
        )
    if key:
        return {"account_name": account, "account_key": key}
    if sas:
        return sas
    raise ValueError("The connection has no account key, SAS token or service principal to sign in with.")


def _azure_connection_string(cfg: dict[str, Any]) -> str:
    method = _text(cfg, "auth_method")
    conn_str = _text(cfg, "connection_string")
    return conn_str if conn_str and method in ("", "connection_string") else ""


def azure_blob_service(cfg: dict[str, Any]):
    """A Blob service client from the form's connection string, account key,
    SAS token or service principal."""
    blob = _driver("azure.storage.blob", "Azure Blob Storage", "azure-storage-blob")
    conn_str = _azure_connection_string(cfg)
    if conn_str:
        return blob.BlobServiceClient.from_connection_string(conn_str)
    account, endpoint, key, sas = _azure_parts(cfg)
    endpoint = _text(cfg, "account_url") or endpoint
    if not endpoint:
        raise ValueError("The connection names no storage account.")
    return blob.BlobServiceClient(account_url=endpoint, credential=_azure_credential(cfg, account, key, sas))


def datalake_service(cfg: dict[str, Any]):
    """A Data Lake (ADLS Gen2) service client, signed in the same ways as Blob."""
    dl = _driver("azure.storage.filedatalake", "Azure Data Lake Storage", "azure-storage-file-datalake")
    conn_str = _azure_connection_string(cfg)
    if conn_str:
        return dl.DataLakeServiceClient.from_connection_string(conn_str)
    account, endpoint, key, sas = _azure_parts(cfg)
    dfs = _text(cfg, "account_url") or (endpoint.replace(".blob.", ".dfs.", 1) if endpoint else "")
    if not dfs:
        raise ValueError("The connection names no storage account.")
    return dl.DataLakeServiceClient(account_url=dfs, credential=_azure_credential(cfg, account, key, sas))
