"""Db2, SAP HANA and Teradata through their vendor drivers (2026-11-11).

These connections could be saved and browsed, but no pipeline step could
read them and Test connection had nothing to run. The Database Source (and
Copy Data, which reads through it) and Test connection now connect here, so
they connect the same way.

The drivers are add-ons (`connections/addons.py`): a standard install leaves
them out because they come under the vendors' own licences. Check
`addon_missing` before calling `connect`.

The password is handed to each driver as its own argument, never written
into a connection string.
"""

from __future__ import annotations

from typing import Any

VENDOR_DB_TYPES = ("db2", "sap_hana", "teradata")

LABELS = {"db2": "IBM Db2", "sap_hana": "SAP HANA", "teradata": "Teradata"}

# The cheapest query each database answers, for Test connection.
PROBE_SQL = {
    "db2": "SELECT 1 FROM SYSIBM.SYSDUMMY1",
    "sap_hana": "SELECT 1 FROM DUMMY",
    "teradata": "SELECT 1",
}


def truthy(value: Any, default: bool = False) -> bool:
    """A checkbox value as the form saves it ('true'/'false', or a bool)."""
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _dsn_value(name: str, value: str) -> str:
    # A Db2 DSN is `KEY=value;` pairs, so these characters would end a value
    # early or start another keyword.
    if any(ch in value for ch in ";{}="):
        raise ValueError(f"the {name} cannot contain ; {{ }} or =")
    return value


def connect(conn_type: str, config: dict[str, Any], *, timeout: int = 15):
    """A DB-API connection to Db2, SAP HANA or Teradata from a saved connection's settings."""
    host = str(config.get("host") or "").strip()
    user = str(config.get("user") or config.get("username") or "")
    password = str(config.get("password") or "")
    database = str(config.get("database") or "").strip()
    if not host:
        raise ValueError("the connection has no host")

    if conn_type == "db2":
        import ibm_db  # type: ignore
        import ibm_db_dbi  # type: ignore

        port = int(config.get("port") or 50000)
        dsn = (
            f"DATABASE={_dsn_value('database', database)};"
            f"HOSTNAME={_dsn_value('host', host)};"
            f"PORT={port};PROTOCOL=TCPIP;CONNECTTIMEOUT={int(timeout)};"
        )
        if truthy(config.get("ssl")):
            dsn += "SECURITY=SSL;"
        # ibm_db.connect takes the user and password apart from the DSN, then
        # ibm_db_dbi wraps the handle as a DB-API connection, as the Db2
        # catalog does. (ibm_db_dbi.connect writes them into the string.)
        return ibm_db_dbi.Connection(ibm_db.connect(dsn, user, password))

    if conn_type == "sap_hana":
        from hdbcli import dbapi  # type: ignore

        kwargs: dict[str, Any] = {
            "address": host,
            "port": int(config.get("port") or 30015),
            "user": user,
            "password": password,
            "connectTimeout": int(timeout) * 1000,
        }
        # The form ticks Encrypt by default; HANA Cloud requires it.
        if truthy(config.get("encrypt"), default=True):
            kwargs["encrypt"] = True
            kwargs["sslValidateCertificate"] = truthy(config.get("ssl_validate_certificate"), default=True)
        if database:
            kwargs["databaseName"] = database
        return dbapi.connect(**kwargs)

    if conn_type == "teradata":
        import teradatasql  # type: ignore

        # teradatasql takes every connection setting as text.
        kwargs = {
            "host": host,
            "user": user,
            "password": password,
            "logmech": str(config.get("logmech") or "TD2"),
            "connect_timeout": str(int(timeout) * 1000),
        }
        if database:
            kwargs["database"] = database
        if config.get("port"):
            kwargs["dbs_port"] = str(int(config["port"]))
        return teradatasql.connect(**kwargs)

    raise ValueError(f"'{conn_type}' is not a Db2, SAP HANA or Teradata connection")


def plain_value(value: Any) -> Any:
    """A value the rest of a pipeline can use.

    SAP HANA hands back large text and binary columns as LOB objects to be
    read; binary comes back as bytes or a memoryview, kept as hex text.
    """
    read = getattr(value, "read", None)
    if callable(read) and not isinstance(value, (str, bytes, bytearray)):
        value = read()
    if isinstance(value, memoryview):
        value = value.tobytes()
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).hex()
    return value


def sample_query(conn_type: str, query: str, limit: int) -> str:
    """The query capped at `limit` rows, in the database's own words.

    Teradata has no LIMIT; Db2 says FETCH FIRST; SAP HANA takes LIMIT.
    """
    if conn_type == "teradata":
        return f"SELECT TOP {int(limit)} * FROM ({query}) AS __sample"
    if conn_type == "db2":
        return f"SELECT * FROM ({query}) AS __sample FETCH FIRST {int(limit)} ROWS ONLY"
    return f"SELECT * FROM ({query}) AS __sample LIMIT {int(limit)}"
