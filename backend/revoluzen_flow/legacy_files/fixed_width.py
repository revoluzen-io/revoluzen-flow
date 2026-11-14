"""Fixed-width text files: each column sits at the same position on every line.

Columns are given either as a list of ``{name, start, length, type}`` (start
counts from 1) or as a list of widths, such as ``8, 30, 12``, with optional
names. Positions count characters; in the single-byte code pages (EBCDIC,
Latin-1, Windows-1252) that is the same as bytes.

Types: ``text``, ``integer``, ``decimal`` (a written decimal point),
``implied`` (digits only, with ``decimals`` places implied, such as
``0001234`` = 12.34; a sign may be written or carried on the last digit) and
``date`` (with a format such as ``YYYYMMDD``).
"""

from __future__ import annotations

import io
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable

from revoluzen_flow.legacy_files import codepages, values
from revoluzen_flow.legacy_files.landing import Lander, unique_names
from revoluzen_flow.legacy_files.records import iter_records

COLUMN_TYPES: list[tuple[str, str]] = [
    ("text", "Text"),
    ("integer", "Whole number"),
    ("decimal", "Number with a decimal point"),
    ("implied", "Number with implied decimals"),
    ("date", "Date"),
]
_TYPES = {value for value, _ in COLUMN_TYPES}

TRIM_RULES: list[tuple[str, str]] = [
    ("both", "Remove spaces on both sides"),
    ("right", "Remove trailing spaces only"),
    ("left", "Remove leading spaces only"),
    ("none", "Keep spaces"),
]


@dataclass
class Column:
    name: str
    start: int
    length: int
    type: str = "text"
    decimals: int = 0
    format: str = ""

    @property
    def end(self) -> int:
        return self.start + self.length - 1


def _int(value: Any, what: str) -> int:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        raise ValueError(f"{what} must be a whole number, not '{value}'.") from None
    return number


def _split_list(value: Any) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value]
    text = str(value or "").replace("\n", ",").replace(";", ",")
    return [part.strip() for part in text.split(",")]


def parse_columns(params: dict[str, Any]) -> list[Column]:
    """The step's column layout, checked."""
    raw = params.get("fixed_columns") or []
    columns: list[Column] = []
    if raw:
        if not isinstance(raw, list):
            raise ValueError("Fixed-width columns must be a list of name, start and length.")
        for index, entry in enumerate(raw, start=1):
            if not isinstance(entry, dict):
                raise ValueError(f"Fixed-width column {index} is not a name, start and length.")
            label = str(entry.get("name") or "").strip() or f"column_{index}"
            if entry.get("start") in (None, "") or entry.get("length") in (None, ""):
                raise ValueError(f"Fixed-width column '{label}' needs a start and a length.")
            start = _int(entry.get("start"), f"The start of column '{label}'")
            length = _int(entry.get("length"), f"The length of column '{label}'")
            col_type = str(entry.get("type") or "text").strip().lower()
            decimals = _int(entry.get("decimals") or 0, f"The decimals of column '{label}'")
            columns.append(Column(label, start, length, col_type, decimals, str(entry.get("format") or "")))
    else:
        widths = [w for w in _split_list(params.get("fixed_widths")) if w]
        if not widths:
            raise ValueError(
                "Fixed-width text needs its columns: give each column a start and length, or list the widths."
            )
        names = _split_list(params.get("fixed_names")) if params.get("fixed_names") else []
        position = 1
        for index, width_text in enumerate(widths, start=1):
            width = _int(width_text, f"Width {index}")
            name = names[index - 1] if index - 1 < len(names) and names[index - 1] else f"column_{index}"
            columns.append(Column(name, position, width))
            position += width
    for column in columns:
        if column.start < 1:
            raise ValueError(f"Column '{column.name}' starts at {column.start}; positions start at 1.")
        if column.length < 1:
            raise ValueError(f"Column '{column.name}' has a length of {column.length}; it must be at least 1.")
        if column.type not in _TYPES:
            known = ", ".join(value for value, _ in COLUMN_TYPES)
            raise ValueError(f"Column '{column.name}' has an unknown type '{column.type}'. Use one of: {known}.")
        if column.decimals < 0 or column.decimals > 37:
            raise ValueError(f"Column '{column.name}' has {column.decimals} decimals; use 0 to 37.")
        if column.type == "date":
            values.date_pattern(column.format or "YYYYMMDD")
    names = unique_names([c.name for c in columns])
    for column, name in zip(columns, names):
        column.name = name
    return columns


def _trimmer(rule: str) -> Callable[[str], str]:
    rule = (rule or "both").strip().lower()
    if rule == "right":
        return lambda s: s.rstrip(" \x00")
    if rule == "left":
        return lambda s: s.lstrip(" ")
    if rule == "none":
        return lambda s: s
    return lambda s: s.strip(" \x00")


def _converter(column: Column, *, trim: Callable[[str], str], empty_as_null: bool,
               decimal_separator: str):
    import pyarrow as pa

    if column.type == "text":
        def text(raw: str):
            value = trim(raw)
            return None if (empty_as_null and value == "") else value
        return text, pa.string()
    if column.type == "integer":
        return values.integer_text, pa.int64()
    if column.type == "decimal":
        scale = column.decimals
        precision = min(38, max(column.length, scale + 1))
        return (
            lambda raw: values.decimal_text(raw, scale, decimal_separator=decimal_separator),
            pa.decimal128(precision, scale) if scale else pa.int64(),
        )
    if column.type == "implied":
        scale = column.decimals
        precision = min(38, max(column.length, scale + 1))
        return (
            lambda raw: values.zoned_text(raw, scale),
            pa.decimal128(precision, scale) if scale else pa.int64(),
        )
    fmt, has_time = values.date_pattern(column.format or "YYYYMMDD")
    return (
        lambda raw: values.date_text(raw, fmt, has_time),
        pa.timestamp("us") if has_time else pa.date32(),
    )


def _text_records(stream, params: dict[str, Any], encoding: str, limit: int | None):
    """Yield ``(record_number, byte_offset, text)`` for each record."""
    record_format = str(params.get("record_format") or "lines").strip().lower()
    if record_format == "lines" and not codepages.single_byte(encoding) and encoding != "utf-8":
        # Multi-byte text such as UTF-16: let Python find the line ends.
        text = io.TextIOWrapper(stream, encoding=encoding, errors="replace", newline=None)
        for number, line in enumerate(text, start=1):
            yield number, None, line.rstrip("\r\n")
            if limit is not None and number >= limit:
                return
        return
    decode = codepages.decoder(encoding)
    record_length = int(params.get("record_length") or 0)
    if record_format == "fixed" and record_length <= 0:
        raise ValueError("Fixed-length records need the record length (bytes per record).")
    first = True
    for number, offset, record in iter_records(
        stream, record_format, record_length=record_length,
        ebcdic=codepages.is_ebcdic(encoding), limit=limit,
    ):
        line = decode(record)
        if first:
            line = line.lstrip("﻿")
            first = False
        yield number, offset, line


def read_fixed_width(conn, paths: str | list[str], params: dict[str, Any], *, limit: int | None = None):
    """Read one or more fixed-width files into one DuckDB relation. Returns
    the relation and counts for the step's log."""
    if isinstance(paths, str):
        paths = [paths]
    columns = parse_columns(params)
    encoding = codepages.normalise(params.get("encoding") or "utf-8", default="utf-8")
    skip_start = max(0, _int(params.get("skip_lines_start") or 0, "Lines to skip at the start"))
    skip_end = max(0, _int(params.get("skip_lines_end") or 0, "Lines to skip at the end"))
    trim = _trimmer(params.get("text_trim") or "both")
    empty_as_null = params.get("empty_as_null", True) not in (False, "false", "0", 0)
    stop_on_bad = str(params.get("bad_value_rule") or "stop").strip().lower() != "empty"
    separator = "," if str(params.get("decimal_separator") or ".").strip() == "," else "."

    converters = []
    schema = []
    for column in columns:
        convert, arrow_type = _converter(
            column, trim=trim, empty_as_null=empty_as_null, decimal_separator=separator,
        )
        converters.append((column.start - 1, column.start - 1 + column.length, convert, column))
        schema.append((column.name, arrow_type))

    lander = Lander(conn, schema)
    bad_values = 0
    data_rows = 0

    def emit(number: int, offset: int | None, line: str) -> None:
        nonlocal bad_values, data_rows
        row = []
        for start, end, convert, column in converters:
            raw = line[start:end]
            try:
                row.append(convert(raw))
            except values.BadValue as exc:
                if stop_on_bad:
                    where = f"line {number:,}" + (f" (byte {offset:,})" if offset is not None else "")
                    raise ValueError(
                        f"Fixed-width: {where}, column '{column.name}' "
                        f"(positions {column.start} to {column.end}): {exc}. "
                        "Check the column's start, length and type, or set bad values to be left empty."
                    ) from None
                bad_values += 1
                row.append(None)
        lander.add(row)
        data_rows += 1

    for path in paths:
        if limit is not None and data_rows >= limit:
            break
        # Header and trailer lines are skipped in every file.
        record_limit = None if limit is None else skip_start + (limit - data_rows) + skip_end
        held: deque = deque()
        with open(path, "rb") as stream:
            for number, offset, line in _text_records(stream, params, encoding, record_limit):
                if number <= skip_start:
                    continue
                held.append((number, offset, line))
                if len(held) > skip_end:
                    emit(*held.popleft())
                if limit is not None and data_rows >= limit:
                    break
    relation = lander.finish()
    return relation, {"rows": lander.row_count, "bad_values": bad_values, "files": len(paths)}
