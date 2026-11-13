"""Connectors whose driver is an add-on say so plainly, everywhere (2026-11-11).

Db2, Informix, SAP HANA, Teradata and Kafka need drivers a standard install
leaves out. Test connection used to answer "No tester implemented", browsing
said "pip install revoluzen-flow[db2]", and a run raised a raw import error.
They now share one list (`connections/addons.py`) and one sentence, and the
New Connection picker reads the same list.
"""

from __future__ import annotations

import asyncio

import pytest

from revoluzen_flow.connections import addons
from revoluzen_flow.connections.addons import ADDONS, addon_message, addon_missing


@pytest.fixture
def nothing_installed(monkeypatch):
    monkeypatch.setattr(addons, "_importable", lambda module: False)


@pytest.fixture
def everything_installed(monkeypatch):
    monkeypatch.setattr(addons, "_importable", lambda module: True)


def test_the_add_on_list_covers_the_vendor_drivers():
    assert set(ADDONS) == {"db2", "informix", "sap_hana", "teradata", "kafka", "trino", "presto"}
    # Drivers that now ship are not add-ons.
    for shipped in ("bigquery", "databricks", "snowflake", "oracle", "postgresql",
                    "clickhouse", "redshift", "gcs", "azure_blob", "adls_gen2", "elasticsearch"):
        assert not addon_missing(shipped)


def test_missing_follows_what_is_installed(nothing_installed):
    assert addon_missing("teradata")
    assert addon_missing("KAFKA")
    assert not addon_missing("mysql")
    assert not addon_missing(None)


def test_one_kafka_client_is_enough(monkeypatch):
    monkeypatch.setattr(addons, "_importable", lambda module: module == "kafka")
    assert not addon_missing("kafka")


def test_the_message_is_plain_and_names_the_package():
    text = addon_message("sap_hana")
    assert text.startswith("The SAP HANA connector needs an add-on")
    assert "Ask whoever set up Revoluzen Flow" in text
    assert "hdbcli" in text
    assert "pip install" not in text


def test_test_connection_says_the_add_on_is_missing(nothing_installed):
    from revoluzen_flow.connections.tester import ConnectionTester

    result = ConnectionTester().test_connection("teradata", {"host": "td.example"})
    assert result["success"] is False
    assert result["message"] == "The Teradata connector isn't available in this install"
    assert "needs an add-on" in result["error"]


def test_with_the_driver_installed_the_add_on_check_steps_aside(everything_installed):
    from revoluzen_flow.connections.tester import ConnectionTester

    result = ConnectionTester().test_connection("teradata", {"host": "td.example"})
    assert "add-on" not in (result.get("error") or "")


def test_browsing_says_the_add_on_is_missing(nothing_installed, monkeypatch):
    import sys

    from revoluzen_flow.connections.catalog_extensions import (
        _db2_catalog, _informix_catalog, _sap_hana_catalog, _teradata_catalog,
    )

    for module in ("ibm_db", "ibm_db_dbi", "hdbcli", "teradatasql"):
        monkeypatch.setitem(sys.modules, module, None)
    for provider, label in (
        (_db2_catalog, "IBM Db2"), (_informix_catalog, "Informix"),
        (_sap_hana_catalog, "SAP HANA"), (_teradata_catalog, "Teradata"),
    ):
        catalog = provider({})
        assert catalog.supported is False
        assert catalog.reason.startswith(f"The {label} connector needs an add-on")
        assert "pip install" not in catalog.reason


def test_a_missing_warehouse_driver_at_run_time_is_plain():
    from revoluzen_flow.connectors.jdbc_dialects import _import_or_raise

    with pytest.raises(RuntimeError) as err:
        _import_or_raise("no_such_driver_module_for_tests", "no-such-driver")
    assert "needs an add-on" in str(err.value)
    assert "no-such-driver" in str(err.value)


def test_the_types_list_tells_the_picker(nothing_installed):
    from revoluzen_flow.api.connections import list_connection_types

    items = {item["type"]: item for item in asyncio.run(list_connection_types(detail=True))}
    assert items["db2"]["addon_missing"] is True
    assert items["kafka"]["addon_missing"] is True
    assert items["bigquery"]["addon_missing"] is False
    assert items["postgresql"]["addon_missing"] is False
