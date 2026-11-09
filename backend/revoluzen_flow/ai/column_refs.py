"""Which columns a step refers to, so "valid" can mean "will run".

`ir.validator` checks the shape of a pipeline: every step has a type, every
connection points at a step, required params are present. It says nothing
about whether `amount > 100` can be evaluated against a source whose column
is called `order_amount` — so `validate_pipeline` reported "valid" for a
pipeline that fails the moment it runs, which is the state the repair
evaluation's whole `renamed-column` family of cases is in.

This module answers the narrower question: which column names does this
step mention? Compare that with what its upstream actually produces and a
renamed column is a validation error rather than a run-time surprise.

Coverage is explicit rather than total. Named params (`join_key`,
`group_by`, a rule's `column`) are read exactly. Free-form expressions are
tokenised, and anything that is a SQL keyword, a function call, a literal
or a number is dropped — so an unknown identifier left over is a column
reference. A function this list does not know would be a false positive,
which is why `unknown_identifiers` is reported separately from the names
read out of structured params, and why the caller only raises an error
when it could resolve the upstream columns to compare against.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

# Params that hold a column name, or a list of them, directly.
_COLUMN_PARAMS = ("column", "columns", "key", "keys", "join_key", "group_by",
                  "by", "sort_by", "partition_by", "order_by", "on")
# Params that hold a list of dicts, each naming a column under one of these.
_COLUMN_IN_DICT = ("column", "name", "left", "right", "source", "field", "src")
# Params whose value is an expression to tokenise.
_EXPRESSION_PARAMS = ("condition", "expression", "expr", "filter", "where",
                      "custom_on", "sql", "formula")

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_STRING_LITERAL = re.compile(r"'[^']*'|\"[^\"]*\"")
# An identifier immediately followed by "(" is a function, not a column.
_FUNCTION_CALL = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s*\(")

# SQL and DuckDB words that are not column names. Not exhaustive, and does
# not need to be: an unknown word only becomes an error when the caller has
# the real column list to check it against.
_RESERVED = frozenset("""
and or not in is null true false between like ilike similar to escape
case when then else end cast as try_cast if ifnull nullif coalesce
select from where group by having order limit offset distinct all
join inner left right full outer cross on using union except intersect
asc desc nulls first last over partition rows range unbounded preceding
following current row interval date time timestamp year month day hour
minute second epoch extract exists any some
int integer bigint smallint tinyint decimal numeric real double float
varchar text string char boolean bool blob json struct list map array
sum avg min max count median mode stddev variance first_val last_val
abs round floor ceil ceiling trunc sqrt pow power exp ln log mod sign
length len lower upper trim ltrim rtrim replace substring substr split
concat concat_ws left_pad right_pad lpad rpad reverse repeat
strftime strptime now today datediff dateadd datepart date_trunc
regexp_matches regexp_replace regexp_extract contains starts_with ends_with
row_number rank dense_rank lag lead ntile cume_dist percent_rank
nan inf infinity unknown default value values
""".split())


def _strings_under(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings_under(item)


def columns_in_expression(text: str) -> set[str]:
    """Identifiers in an expression that look like column references."""
    if not text:
        return set()
    cleaned = _STRING_LITERAL.sub(" ", text)
    functions = {m.group(1).lower() for m in _FUNCTION_CALL.finditer(cleaned)}
    found: set[str] = set()
    for match in _IDENTIFIER.finditer(cleaned):
        word = match.group(0)
        lowered = word.lower()
        if lowered in _RESERVED or lowered in functions:
            continue
        found.add(word)
    return found


def referenced_columns(params: dict[str, Any]) -> tuple[set[str], set[str]]:
    """(names read from structured params, identifiers found in expressions).

    Kept apart because the first set is exact and the second is a guess
    that a function name this module does not know could pollute.
    """
    named: set[str] = set()
    guessed: set[str] = set()
    if not isinstance(params, dict):
        return named, guessed

    for key, value in params.items():
        if key.startswith("_"):
            continue
        lowered = key.lower()
        if lowered in _COLUMN_PARAMS:
            for text in _strings_under(value):
                if text:
                    named.add(text)
            if isinstance(value, (list, tuple)):
                for item in value:
                    if isinstance(item, dict):
                        for inner in _COLUMN_IN_DICT:
                            if isinstance(item.get(inner), str) and item[inner]:
                                named.add(item[inner])
        elif lowered in _EXPRESSION_PARAMS:
            for text in _strings_under(value):
                guessed |= columns_in_expression(text)
        elif isinstance(value, (list, tuple)):
            # key_pairs, rules, aggregations: a list of dicts that name
            # columns under a known key, alongside operators and values.
            for item in value:
                if not isinstance(item, dict):
                    continue
                for inner in _COLUMN_IN_DICT:
                    if isinstance(item.get(inner), str) and item[inner]:
                        named.add(item[inner])
                for inner in _EXPRESSION_PARAMS:
                    if isinstance(item.get(inner), str):
                        guessed |= columns_in_expression(item[inner])
    # A name that an expression also produced is already exact.
    return named, guessed - named


def missing_columns(
    params: dict[str, Any],
    available: Iterable[str],
) -> list[str]:
    """Column names this step mentions that its upstream does not provide.

    `available` must be the columns actually resolved for the step's input.
    Call with an empty list only when the schema is genuinely empty, never
    when it could not be determined — every reference would be reported.
    """
    have = {str(c) for c in available}
    have_lower = {c.lower() for c in have}
    named, guessed = referenced_columns(params)
    missing = [c for c in sorted(named | guessed) if c not in have and c.lower() not in have_lower]
    return missing
