"""Connection types whose driver is an add-on, not part of every install.

Db2 and Informix (ibm_db), SAP HANA (hdbcli) and Teradata (teradatasql) come
under their vendors' own licences, and Kafka's client is native code, so a
standard install leaves them out (backend/requirements.txt, 2026-11-11).

The New Connection picker, Test connection, browsing and runs all read this
one list, so they all say the same plain thing. The message names the Python
package for whoever set Revoluzen Flow up; it does not tell the person on
screen to run pip, which a packaged desktop install cannot do.
"""

from __future__ import annotations

import importlib.util

# type -> (name on screen, modules (any one is enough), Python package to add)
ADDONS: dict[str, tuple[str, tuple[str, ...], str]] = {
    "db2": ("IBM Db2", ("ibm_db",), "ibm_db"),
    "informix": ("Informix", ("ibm_db",), "ibm_db"),
    "sap_hana": ("SAP HANA", ("hdbcli",), "hdbcli"),
    "teradata": ("Teradata", ("teradatasql",), "teradatasql"),
    "kafka": ("Kafka", ("confluent_kafka", "kafka"), "confluent-kafka"),
    # 2026-11-11: browsing Trino and Presto needs their own clients, which a
    # standard install leaves out. Neither can be read in a pipeline yet.
    "trino": ("Trino", ("trino",), "trino"),
    "presto": ("Presto", ("prestodb",), "presto-python-client"),
}


def _importable(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        # find_spec raises when a parent package is missing.
        return False


def addon_missing(conn_type: str | None) -> bool:
    """True when this connection type needs an add-on that is not installed."""
    entry = ADDONS.get(str(conn_type or "").lower())
    return entry is not None and not any(_importable(m) for m in entry[1])


def addon_label(conn_type: str | None) -> str:
    entry = ADDONS.get(str(conn_type or "").lower())
    return entry[0] if entry else str(conn_type or "")


def addon_message(conn_type: str | None) -> str:
    """The plain sentence shown wherever the missing add-on stops something."""
    entry = ADDONS.get(str(conn_type or "").lower())
    label = entry[0] if entry else str(conn_type or "This")
    package = f" (Python package: {entry[2]})" if entry else ""
    return (
        f"The {label} connector needs an add-on that isn't included in this "
        f"Revoluzen Flow install. Ask whoever set up Revoluzen Flow to add it{package}."
    )


def driver_message(label: str, package: str) -> str:
    """The same sentence for a driver that normally ships but is missing."""
    return (
        f"The {label} connector needs an add-on that isn't included in this "
        f"Revoluzen Flow install. Ask whoever set up Revoluzen Flow to add it "
        f"(Python package: {package})."
    )
