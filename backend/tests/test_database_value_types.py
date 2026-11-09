"""Values from a database keep their types, on the way in and on the way out.

Reading: anything but a plain int or float used to be quoted as text, so a money
column arrived as VARCHAR and SUM(amount) failed. Writing: a bare DECIMAL column
rounded every amount to a whole number in MySQL and SQL Server.
"""

from __future__ import annotations

from datetime import date, datetime, time, timezone
from decimal import Decimal

import duckdb
import pytest

from revoluzen_flow.common.duckdb_compat import fetch_one, fetch_scalar
from revoluzen_flow.nodes.activities import DbSinkNode
from revoluzen_flow.nodes.db_source import DbSourceNode


def _read(*row):
    columns = [f"c{i}" for i in range(len(row))]
    node = DbSourceNode(params={})
    values = node._rows_to_values([tuple(row)], columns)
    con = duckdb.connect(":memory:")
    rel = con.sql(f"SELECT * FROM (VALUES {values}) AS v ({', '.join(columns)})")
    return [str(t) for t in rel.types], fetch_one(rel)


def test_decimals_stay_numbers_and_can_be_added_up():
    node = DbSourceNode(params={})
    values = node._rows_to_values([(Decimal("1299.50"),), (Decimal("849.25"),)], ["amount"])
    con = duckdb.connect(":memory:")
    total = fetch_scalar(con.sql(f"SELECT SUM(amount) FROM (VALUES {values}) AS v (amount)"))
    assert total == Decimal("2148.75")


def test_decimal_in_exponent_form_is_written_in_full():
    types, row = _read(Decimal("2E+3"))
    assert types[0].startswith("DECIMAL") or types[0] in ("INTEGER", "BIGINT")
    assert row[0] == 2000


def test_dates_stay_dates():
    types, row = _read(date(2026, 11, 4))
    assert types == ["DATE"]
    assert row == (date(2026, 11, 4),)


def test_timestamps_stay_timestamps():
    types, row = _read(datetime(2026, 11, 4, 6, 30, 5))
    assert types == ["TIMESTAMP"]
    assert row == (datetime(2026, 11, 4, 6, 30, 5),)


def test_timestamps_with_a_zone_keep_it():
    types, _ = _read(datetime(2026, 11, 4, 6, 30, tzinfo=timezone.utc))
    assert types == ["TIMESTAMP WITH TIME ZONE"]


def test_times_stay_times():
    types, row = _read(time(6, 30))
    assert types == ["TIME"]
    assert row == (time(6, 30),)


def test_booleans_stay_booleans():
    types, row = _read(True)
    assert types == ["BOOLEAN"]
    assert row == (True,)


def test_text_is_still_escaped():
    _, row = _read("O'Brien")
    assert row == ("O'Brien",)


def test_missing_values_are_null():
    _, row = _read(None, Decimal("1.5"))
    assert row == (None, Decimal("1.5"))


def test_a_float_that_is_not_a_number_does_not_break_the_load():
    types, row = _read(float("nan"))
    assert types == ["DOUBLE"]
    assert row[0] != row[0]


@pytest.mark.parametrize("target", ["mysql", "mssql", "postgresql"])
def test_writing_keeps_a_decimals_width(target):
    assert DbSinkNode._map_type("DECIMAL(38,2)", target) == "DECIMAL(38,2)"


def test_sqlite_has_no_decimal_type():
    assert DbSinkNode._map_type("DECIMAL(38,2)", "sqlite") == "REAL"


def test_a_decimal_with_no_width_is_left_alone():
    assert DbSinkNode._map_type("DECIMAL", "mysql") == "DECIMAL"
