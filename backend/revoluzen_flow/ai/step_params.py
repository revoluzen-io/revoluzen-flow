"""What a step's parameters mean, for the agent editing them.

Models fixing a join kept setting ``join_key`` to the new name, because nothing
told them that two different names need another mode. Only the step types the
repair path can change are here. Each line was checked against the node's code.
"""

from __future__ import annotations

from collections.abc import Iterable

STEP_PARAMS: dict[str, str] = {
    "join": (
        "key_mode same_key with join_key [\"col\"] needs the same column name on both sides. "
        "If the two sides name the key differently, set key_mode to mapped_keys and key_pairs to "
        "[{\"left\": \"left_col\", \"right\": \"right_col\", \"operator\": \"=\"}]. join_type is INNER, LEFT, RIGHT or FULL."
    ),
    "filter": (
        "mode expression with condition, a SQL WHERE clause. Use TRY_CAST(col AS DOUBLE) when text may "
        "hold values that are not numbers: CAST fails the run on any value it cannot convert, including "
        "ones not seen yet."
    ),
    "aggregate": (
        "group_by is a list of columns. functions is a list of {\"column\", \"function\", \"alias\"}. "
        "Both can name a renamed column, so check both."
    ),
    "deduplicate": "key is the list of columns that identify a row.",
    "derived_column": "columns is a list of {\"name\", \"expression\"}, where expression is SQL over the input columns.",
    "rename": "mappings is an object of {\"old_name\": \"new_name\"}. Only the columns listed are kept, so list every column to keep.",
}


def guide_for(kinds: Iterable[str]) -> dict[str, str]:
    """The parameter meanings for these step types, where we have them."""
    return {kind: STEP_PARAMS[kind] for kind in sorted(set(kinds)) if kind in STEP_PARAMS}
