"""A managed-table sink must refuse an empty table name, not invent one.

`safe_schema_or_table_name` falls back to "default" when a name sanitises
to nothing. That is correct for a SCHEMA — "default" is the real name of
the default schema — and silently wrong for a TABLE, because the sink's
own guard reads:

    table_name = safe_schema_or_table_name(params.get("table_name") or "")
    if not table_name:
        raise ValueError("local_table_sink: table_name is required")

The guard could never fire. A sink configured with the wrong parameter
key wrote its rows to a managed table literally called ``default`` and
reported success. Found the hard way: a pipeline's output went to
``default.default`` instead of ``default.orders_by_region``, and the
only clue was the Managed Tables list growing a table nobody created.
"""

from __future__ import annotations

import pytest

from revoluzen_flow.datastore.paths import safe_schema_or_table_name


class TestSanitiserFallback:
    def test_schema_still_falls_back_to_default(self):
        # Unchanged behaviour — other callers rely on it.
        assert safe_schema_or_table_name("") == "default"
        assert safe_schema_or_table_name("   ") == "default"
        assert safe_schema_or_table_name("___") == "default"

    def test_strict_mode_returns_empty_so_a_guard_can_fire(self):
        assert safe_schema_or_table_name("", fallback="") == ""
        assert safe_schema_or_table_name("___", fallback="") == ""
        # Punctuation-only sanitises away entirely; that is still "absent".
        assert safe_schema_or_table_name("!!!", fallback="") == ""

    def test_strict_mode_does_not_alter_real_names(self):
        assert safe_schema_or_table_name("orders_by_region", fallback="") == "orders_by_region"
        assert safe_schema_or_table_name("My Sales 2026", fallback="") == "my_sales_2026"


class TestSinkRefusesAnEmptyName:
    """The behaviour that actually matters, exercised through the node."""

    def _sink(self, params):
        from revoluzen_flow.nodes.local_table import LocalTableSinkNode

        return LocalTableSinkNode(params)

    @pytest.mark.parametrize("params", [
        {},                                  # omitted entirely
        {"table_name": ""},                  # present but blank
        {"table_name": "   "},               # whitespace
        {"table_name": "!!!"},               # sanitises to nothing
        {"table": "orders_by_region"},       # the real bug: wrong key
    ])
    def test_missing_or_unusable_table_name_raises(self, params):
        node = self._sink(params)
        with pytest.raises(ValueError, match="table_name"):
            node.execute(None)  # must fail before touching ctx

    def test_it_never_silently_targets_a_table_called_default(self):
        """The specific regression: wrong key -> writes to `default`."""
        node = self._sink({"table": "orders_by_region", "mode": "replace"})
        with pytest.raises(ValueError) as exc:
            node.execute(None)
        assert "default" not in str(exc.value).replace("default schema", "")
