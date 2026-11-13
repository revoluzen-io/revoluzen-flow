"""ODBC database: any database with an ODBC driver (2026-11-11).

For a database Revoluzen Flow has no connector of its own for. The person
installs the database vendor's ODBC driver on the computer running Revoluzen
Flow (or uses a data source name, DSN, set up there) and saves:

* the driver's name as the computer lists it, or a DSN;
* "More settings": whatever else the driver needs, as name=value pairs
  separated by semicolons (Server=db1;Database=sales);
* a username and password, which go into the connection string escaped,
  never typed into "More settings".

Test connection, browsing and the Database Source read through it. Writing
to an ODBC database is not built: there is no one way to create a table or
load rows that every ODBC database accepts.

A sample run reads the first rows and stops, because there is no one way to
say LIMIT either.
"""

from __future__ import annotations

from typing import Any

DEFAULT_TIMEOUT = 15
_EXPLICIT = {"DRIVER", "DSN", "UID", "PWD", "FILEDSN"}


def odbc_value(value: Any) -> str:
    """A connection-string value, in braces when it holds ; { } = or edge spaces."""
    text = str(value)
    if text and text == text.strip() and not any(ch in text for ch in ";{}="):
        return text
    return "{" + text.replace("}", "}}") + "}"


def parse_settings(text: str) -> list[tuple[str, str]]:
    """The form's "More settings": ``name=value`` pairs separated by semicolons.

    A value in braces may hold semicolons; ``}}`` inside braces is one ``}``.
    """
    pairs: list[tuple[str, str]] = []
    text = str(text or "")
    i, n = 0, len(text)
    while i < n:
        while i < n and text[i] in "; \t\r\n":
            i += 1
        if i >= n:
            break
        eq = text.find("=", i)
        if eq < 0:
            raise ValueError(f"'{text[i:].strip()}' in More settings has no '=' (write it as name=value).")
        key = text[i:eq].strip()
        if not key:
            raise ValueError("A setting in More settings has no name before its '='.")
        i = eq + 1
        while i < n and text[i] in " \t":
            i += 1
        if i < n and text[i] == "{":
            j, buf = i + 1, []
            while True:
                if j >= n:
                    raise ValueError(f"The value of {key} in More settings starts with {{ but has no closing }}.")
                if text[j] == "}":
                    if j + 1 < n and text[j + 1] == "}":
                        buf.append("}")
                        j += 2
                        continue
                    break
                buf.append(text[j])
                j += 1
            value, i = "".join(buf), j + 1
        else:
            semi = text.find(";", i)
            semi = n if semi < 0 else semi
            value, i = text[i:semi].strip(), semi
        pairs.append((key, value))
    return pairs


def connection_string(cfg: dict[str, Any]) -> str:
    """The ODBC connection string for a saved connection."""
    driver = str(cfg.get("driver") or "").strip().strip("{}")
    dsn = str(cfg.get("dsn") or "").strip()
    if not (driver or dsn):
        raise ValueError("Name the ODBC driver, or a data source name (DSN) set up on this computer.")
    parts = [f"DSN={odbc_value(dsn)}"] if dsn else []
    if driver:
        parts.append(f"DRIVER={{{driver.replace('}', '}}')}}}")
    for key, value in parse_settings(cfg.get("settings") or cfg.get("connection_string") or ""):
        if key.upper() in _EXPLICIT:
            continue  # the form's own fields win
        parts.append(f"{key}={odbc_value(value)}")
    user = str(cfg.get("user") or cfg.get("username") or "")
    password = str(cfg.get("password") or "")
    if user:
        parts.append(f"UID={odbc_value(user)}")
    if password:
        parts.append(f"PWD={odbc_value(password)}")
    return ";".join(parts) + ";"


def _pyodbc():
    try:
        import pyodbc  # type: ignore
    except ImportError as exc:
        from revoluzen_flow.connections.addons import driver_message
        raise RuntimeError(driver_message("ODBC", "pyodbc")) from exc
    return pyodbc


def installed_drivers() -> list[str]:
    try:
        return list(_pyodbc().drivers())
    except Exception:  # noqa: BLE001 — no driver manager on this computer
        return []


def connect(cfg: dict[str, Any], *, timeout: int = DEFAULT_TIMEOUT):
    """An open pyodbc connection, in autocommit so a read leaves no transaction open."""
    pyodbc = _pyodbc()
    text = connection_string(cfg)
    try:
        return pyodbc.connect(text, timeout=timeout, autocommit=True)
    except pyodbc.Error as exc:
        state = exc.args[0] if exc.args else ""
        if state == "IM002":
            driver = str(cfg.get("driver") or cfg.get("dsn") or "").strip()
            have = ", ".join(installed_drivers()) or "none"
            raise ValueError(
                f"The ODBC driver or data source {driver!r} isn't set up on the computer running "
                f"Revoluzen Flow. ODBC drivers it has: {have}."
            ) from None
        raise


def describe(conn) -> str:
    """'Microsoft SQL Server 16.00.1000' or similar, from the driver."""
    pyodbc = _pyodbc()
    try:
        name = str(conn.getinfo(pyodbc.SQL_DBMS_NAME) or "").strip()
        version = str(conn.getinfo(pyodbc.SQL_DBMS_VER) or "").strip()
        return f"{name} {version}".strip()
    except Exception:  # noqa: BLE001 — some drivers answer neither
        return ""


def list_tables(conn, limit: int = 2000) -> list[tuple[str, str, str]]:
    """(parent, name, kind) for the tables and views the driver lists."""
    found: list[tuple[str, str, str]] = []
    cur = conn.cursor()
    for row in cur.tables():
        kind = str(getattr(row, "table_type", "") or "").upper()
        if kind not in ("TABLE", "VIEW"):
            continue
        parent = getattr(row, "table_schem", None) or getattr(row, "table_cat", None) or ""
        found.append((str(parent), str(row.table_name), kind.lower()))
        if len(found) >= limit:
            break
    return found


def read(cfg: dict[str, Any], query: str, *, params: tuple = (), max_rows: int | None = None):
    """(rows, columns) for a query. ``max_rows`` stops after that many rows."""
    from revoluzen_flow.connections.vendor_db import plain_value

    conn = connect(cfg)
    try:
        cur = conn.cursor()
        cur.execute(query, params) if params else cur.execute(query)
        if not cur.description:
            return [], []
        columns = [d[0] for d in cur.description]
        raw = cur.fetchmany(max_rows) if max_rows else cur.fetchall()
        return [tuple(plain_value(v) for v in row) for row in raw], columns
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
