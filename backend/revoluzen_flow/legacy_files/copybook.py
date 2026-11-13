"""COBOL copybooks: turning mainframe records into typed columns.

A copybook is the COBOL description of a record. This module reads one into a
layout (every field's position, length and type) and decodes records with it.

What is understood:

* ``PIC X`` / ``A`` text, ``PIC 9`` numbers with ``S`` (signed), ``V``
  (implied decimal point) and ``P`` (scaling), numeric-edited pictures such as
  ``ZZ,ZZ9.99-``, and ``PIC N`` national text (UTF-16).
* Usage DISPLAY (zoned decimal, sign carried in the last digit, or
  ``SIGN LEADING`` / ``SEPARATE``), COMP / COMP-4 / BINARY / COMP-5,
  COMP-3 / PACKED-DECIMAL, COMP-1 and COMP-2 (IBM or IEEE floating point).
* ``OCCURS n TIMES`` (each item becomes numbered columns, ``ITEM_1``,
  ``ITEM_2``...) and ``OCCURS m TO n TIMES DEPENDING ON field`` (items past
  the count are empty, and fields after the table move up).
* ``REDEFINES``: every branch is read unless a rule says which one a record
  uses. Several 01 records in one copybook are treated the same way.
* 88 levels, 66 RENAMES, VALUE, JUSTIFIED, BLANK WHEN ZERO, INDEXED BY and
  KEY phrases are read past.

What stops with a clear message: SYNCHRONIZED (it adds hidden slack bytes),
POINTER / INDEX / COMP-X items, nested COPY statements, OCCURS DEPENDING ON
inside another table or inside a REDEFINES.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, BinaryIO, Callable, Iterable

from revoluzen_flow.legacy_files import codepages, values
from revoluzen_flow.legacy_files.landing import Lander
from revoluzen_flow.legacy_files.records import iter_records


class CopybookError(ValueError):
    """The copybook cannot be read. The message names the line."""


# ── Tokens and statements ──

_CLAUSE_WORDS = {
    "PIC", "PICTURE", "USAGE", "REDEFINES", "OCCURS", "SIGN", "LEADING", "TRAILING",
    "SEPARATE", "SYNC", "SYNCHRONIZED", "JUST", "JUSTIFIED", "BLANK", "VALUE", "VALUES",
    "EXTERNAL", "GLOBAL", "DISPLAY", "DISPLAY-1", "COMP", "COMPUTATIONAL", "COMP-1",
    "COMPUTATIONAL-1", "COMP-2", "COMPUTATIONAL-2", "COMP-3", "COMPUTATIONAL-3", "COMP-4",
    "COMPUTATIONAL-4", "COMP-5", "COMPUTATIONAL-5", "COMP-X", "COMPUTATIONAL-X", "COMP-6",
    "BINARY", "PACKED-DECIMAL", "NATIONAL", "INDEX", "POINTER", "PROCEDURE-POINTER",
    "FUNCTION-POINTER", "INDEXED", "ASCENDING", "DESCENDING", "GROUP-USAGE", "TIMES", "DEPENDING",
}

_USAGES = {
    "DISPLAY": "display",
    "COMP": "binary", "COMPUTATIONAL": "binary", "COMP-4": "binary", "COMPUTATIONAL-4": "binary",
    "BINARY": "binary", "COMP-5": "binary", "COMPUTATIONAL-5": "binary",
    "COMP-3": "packed", "COMPUTATIONAL-3": "packed", "PACKED-DECIMAL": "packed",
    "COMP-1": "float4", "COMPUTATIONAL-1": "float4", "COMP-2": "float8", "COMPUTATIONAL-2": "float8",
    "NATIONAL": "national",
}
_UNSUPPORTED_USAGES = {
    "INDEX": "INDEX", "POINTER": "POINTER", "PROCEDURE-POINTER": "PROCEDURE-POINTER",
    "FUNCTION-POINTER": "FUNCTION-POINTER", "COMP-X": "COMP-X", "COMPUTATIONAL-X": "COMP-X",
    "COMP-6": "COMP-6", "DISPLAY-1": "DISPLAY-1 (DBCS)",
}
_DIRECTIVES = {"EJECT", "SKIP1", "SKIP2", "SKIP3", "TITLE", "SERVICE", "CBL", "PROCESS"}


@dataclass
class _Token:
    text: str
    line: int
    literal: bool = False


def _code_lines(text: str) -> list[tuple[int, str, bool]]:
    """``(line_number, code, is_continuation)`` with comments and the
    sequence and identification areas removed."""
    raw_lines = text.replace("\r\n", "\n").replace("\r", "\n").replace("\t", "    ").split("\n")

    def fixed_like(line: str) -> bool:
        # Fixed format: columns 1-6 hold a sequence number (or nothing) and
        # column 7 an indicator. "    05 NAME" or "01 CUST" is free format.
        if not line.strip():
            return True
        head = line[:6]
        if head.strip() and (len(head) < 6 or " " in head):
            return False
        if len(line) <= 6:
            return True
        return line[6] in " *-/Dd"

    fixed = all(fixed_like(line) for line in raw_lines) and any(len(l) > 7 for l in raw_lines)
    # Columns 73-80 are an identification area and are ignored, unless the
    # copybook was retyped with longer lines (a line ends its field there).
    cut: int | None = 72
    if fixed and any(len(l) > 72 and l[72:].rstrip().endswith(".") for l in raw_lines):
        cut = None
    out: list[tuple[int, str, bool]] = []
    for number, line in enumerate(raw_lines, start=1):
        continuation = False
        if fixed:
            indicator = line[6] if len(line) > 6 else " "
            if indicator in "*/":
                continue
            if indicator in "Dd":
                # Debugging lines are compiled only WITH DEBUGGING MODE; skip them.
                continue
            continuation = indicator == "-"
            code = line[7:cut]
        else:
            stripped = line.lstrip()
            if stripped.startswith("*") and not stripped.startswith("*>"):
                continue
            code = line
        code = _strip_floating_comment(code)
        if code.strip():
            out.append((number, code, continuation))
    return out


def _strip_floating_comment(code: str) -> str:
    quote = ""
    for index, char in enumerate(code):
        if quote:
            if char == quote:
                quote = ""
        elif char in "'\"":
            quote = char
        elif code.startswith("*>", index):
            return code[:index]
    return code


def _tokens(text: str) -> list[_Token | None]:
    """Tokens, with ``None`` marking the end of each statement (a period
    followed by a space or the end of a line)."""
    out: list[_Token | None] = []
    for number, code, continuation in _code_lines(text):
        index = 0
        if continuation:
            # A continued literal goes on after the first quote of this line.
            stripped = code.lstrip()
            if stripped[:1] in "'\"":
                closing = stripped.find(stripped[0], 1)
                index = len(code) - len(stripped) + (closing + 1 if closing > 0 else len(stripped))
        length = len(code)
        while index < length:
            char = code[index]
            if char.isspace():
                index += 1
                continue
            if char in "'\"":
                end = index + 1
                while end < length:
                    if code[end] == char:
                        if end + 1 < length and code[end + 1] == char:
                            end += 2
                            continue
                        break
                    end += 1
                out.append(_Token(code[index:end + 1], number, literal=True))
                index = end + 1
                if index < length and code[index] == ".":
                    if index + 1 >= length or code[index + 1].isspace():
                        out.append(None)
                        index += 1
                continue
            end = index
            while end < length and not code[end].isspace() and code[end] not in "'\"":
                end += 1
            word = code[index:end]
            index = end
            period = False
            while word and word[-1] in ".,;":
                if word[-1] == ".":
                    period = True
                word = word[:-1]
            if word:
                out.append(_Token(word, number))
            if period:
                out.append(None)
    return out


def _statements(text: str) -> list[list[_Token]]:
    statements: list[list[_Token]] = []
    current: list[_Token] = []
    for token in _tokens(text):
        if token is None:
            if current:
                statements.append(current)
            current = []
        else:
            current.append(token)
    if current:
        statements.append(current)
    return statements


# ── Pictures ──

@dataclass
class Picture:
    text: str
    kind: str          # text | number | edited | national
    length: int        # character positions
    digits: int = 0    # digit positions of a number
    scale: int = 0     # places after the decimal point (negative: trailing P)
    signed: bool = False


_REPEAT = re.compile(r"(CR|DB|.)\((\d+)\)")


def _expand(picture: str) -> str:
    return _REPEAT.sub(lambda m: m.group(1) * int(m.group(2)), picture.upper())


def parse_picture(picture: str, line: int = 0, name: str = "") -> Picture:
    where = f"Copybook line {line}: " if line else ""
    who = f" of {name}" if name else ""
    expanded = _expand(picture)
    if "(" in expanded or ")" in expanded:
        raise CopybookError(f"{where}the picture '{picture}'{who} has a repeat count it cannot read.")
    symbols = expanded.replace("CR", "c").replace("DB", "d")
    if not symbols:
        raise CopybookError(f"{where}the picture{who} is empty.")
    if "N" in symbols:
        if set(symbols) - {"N", "B", "0", "/"}:
            raise CopybookError(f"{where}the picture '{picture}'{who} mixes N with other symbols.")
        return Picture(picture, "national", len(symbols))
    if "G" in symbols:
        raise CopybookError(f"{where}DBCS pictures (G){who} are not supported.")
    if set(symbols) & {"X", "A"} or ("/" in symbols and "." not in symbols):
        # Alphanumeric, or an edited date such as 99/99/99: read as text.
        return Picture(picture, "text", len(symbols))
    if set(symbols) <= set("9SVP"):
        signed = symbols.startswith("S")
        body = symbols[1:] if signed else symbols
        if "S" in body or body.count("V") > 1:
            raise CopybookError(f"{where}the picture '{picture}'{who} is not a valid number.")
        digits = body.count("9")
        if digits == 0:
            raise CopybookError(f"{where}the picture '{picture}'{who} has no digit positions.")
        if digits > 38:
            raise CopybookError(f"{where}the picture '{picture}'{who} has {digits} digits; at most 38 are supported.")
        first9, last9 = body.find("9"), body.rfind("9")
        p_left = body[:first9].count("P")
        p_right = body[last9:].count("P")
        if p_left:
            scale = p_left + digits
        elif p_right:
            scale = -p_right
        else:
            point = body.find("V")
            scale = body[point:].count("9") if point >= 0 else 0
        return Picture(picture, "number", digits, digits=digits, scale=scale, signed=signed)
    # Numeric-edited: Z * + - . , $ CR DB B 0 / and 9.
    allowed = set("9Z*+-.,$cdB0/V")
    if not set(symbols) <= allowed:
        bad = "".join(sorted(set(symbols) - allowed))
        raise CopybookError(f"{where}the picture '{picture}'{who} uses symbols it cannot read: {bad}.")
    point = symbols.find(".")
    after = symbols[point + 1:] if point >= 0 else ""
    scale = sum(after.count(s) for s in "9Z*")  # a trailing + - CR DB is the sign, not a digit
    digits = sum(symbols.count(s) for s in "9Z*")
    floating = max(symbols.count("$"), symbols.count("+"), symbols.count("-"))
    if floating > 1:
        digits += floating - 1
    length = len(symbols) + symbols.count("c") + symbols.count("d") - symbols.count("V")
    signed = any(s in symbols for s in "+-cd")
    return Picture(picture, "edited", length, digits=max(digits, scale, 1), scale=scale, signed=signed)


# ── Items ──

@dataclass
class Item:
    level: int
    name: str
    line: int
    redefines: str | None = None
    implicit_redefines: bool = False
    occurs_min: int = 1
    occurs_max: int = 1
    has_occurs: bool = False
    depending_on: str | None = None
    picture: Picture | None = None
    usage: str | None = None
    sign_leading: bool = False
    sign_separate: bool = False
    children: list["Item"] = field(default_factory=list)
    parent: "Item | None" = None
    size: int = 0
    offset: int = 0
    effective_usage: str = "display"

    @property
    def is_filler(self) -> bool:
        return not self.name or self.name.upper() == "FILLER"

    @property
    def label(self) -> str:
        return self.name if not self.is_filler else "FILLER"


def _expect_int(tokens: list[_Token], index: int, what: str, name: str) -> int:
    if index >= len(tokens) or not tokens[index].text.isdigit():
        line = tokens[min(index, len(tokens) - 1)].line
        raise CopybookError(f"Copybook line {line}: {what} of {name or 'an item'} must be a number.")
    return int(tokens[index].text)


def _parse_statement(tokens: list[_Token]) -> Item | None:
    first = tokens[0]
    word = first.text.upper()
    if word in _DIRECTIVES:
        return None
    if word == "COPY":
        raise CopybookError(
            f"Copybook line {first.line}: this copybook includes another (COPY). "
            "Paste the included copybook in its place."
        )
    if not word.isdigit():
        raise CopybookError(
            f"Copybook line {first.line}: expected a level number such as 01 or 05, found '{first.text}'."
        )
    level = int(word)
    if level in (66, 88):
        return None
    if not (1 <= level <= 49 or level == 77):
        raise CopybookError(f"Copybook line {first.line}: {level} is not a level number COBOL allows.")
    index = 1
    name = ""
    if index < len(tokens) and not tokens[index].literal and tokens[index].text.upper() not in _CLAUSE_WORDS:
        name = tokens[index].text
        index += 1
    item = Item(level=level, name=name, line=first.line)
    label = name or "FILLER"
    while index < len(tokens):
        token = tokens[index]
        upper = token.text.upper()
        if token.literal:
            index += 1
            continue
        if upper == "REDEFINES":
            if index + 1 >= len(tokens):
                raise CopybookError(f"Copybook line {token.line}: REDEFINES needs the name of an item.")
            item.redefines = tokens[index + 1].text
            index += 2
        elif upper in ("PIC", "PICTURE"):
            index += 1
            if index < len(tokens) and tokens[index].text.upper() == "IS":
                index += 1
            if index >= len(tokens):
                raise CopybookError(f"Copybook line {token.line}: PIC of {label} has no picture.")
            item.picture = parse_picture(tokens[index].text, tokens[index].line, label)
            index += 1
        elif upper == "USAGE":
            index += 1
            if index < len(tokens) and tokens[index].text.upper() == "IS":
                index += 1
            if index >= len(tokens):
                raise CopybookError(f"Copybook line {token.line}: USAGE of {label} is missing.")
            item.usage = _usage(tokens[index], label)
            index += 1
        elif upper in _USAGES or upper in _UNSUPPORTED_USAGES:
            item.usage = _usage(token, label)
            index += 1
        elif upper == "OCCURS":
            index += 1
            low = _expect_int(tokens, index, "OCCURS", label)
            index += 1
            high = low
            if index < len(tokens) and tokens[index].text.upper() == "TO":
                high = _expect_int(tokens, index + 1, "OCCURS ... TO", label)
                index += 2
            if index < len(tokens) and tokens[index].text.upper() == "TIMES":
                index += 1
            item.has_occurs = True
            item.occurs_min, item.occurs_max = low, high
            if high < 1 or low > high:
                raise CopybookError(f"Copybook line {token.line}: OCCURS of {label} has an impossible count.")
            if index < len(tokens) and tokens[index].text.upper() == "DEPENDING":
                index += 1
                if index < len(tokens) and tokens[index].text.upper() == "ON":
                    index += 1
                if index >= len(tokens):
                    raise CopybookError(f"Copybook line {token.line}: DEPENDING ON of {label} needs a field name.")
                item.depending_on = tokens[index].text
                index += 1
        elif upper in ("ASCENDING", "DESCENDING", "INDEXED"):
            index += 1
            while index < len(tokens) and tokens[index].text.upper() in ("KEY", "IS", "BY"):
                index += 1
            while index < len(tokens) and tokens[index].text.upper() not in _CLAUSE_WORDS:
                index += 1
        elif upper == "SIGN":
            index += 1
            if index < len(tokens) and tokens[index].text.upper() == "IS":
                index += 1
        elif upper in ("LEADING", "TRAILING"):
            item.sign_leading = upper == "LEADING"
            index += 1
        elif upper == "SEPARATE":
            item.sign_separate = True
            index += 1
            if index < len(tokens) and tokens[index].text.upper() == "CHARACTER":
                index += 1
        elif upper in ("SYNC", "SYNCHRONIZED"):
            raise CopybookError(
                f"Copybook line {token.line}: {label} is SYNCHRONIZED, which adds hidden slack bytes to "
                "the record. This is not supported yet."
            )
        elif upper in ("JUST", "JUSTIFIED"):
            index += 1
            if index < len(tokens) and tokens[index].text.upper() == "RIGHT":
                index += 1
        elif upper == "BLANK":
            index += 1
            while index < len(tokens) and tokens[index].text.upper() in ("WHEN", "ZERO", "ZEROS", "ZEROES"):
                index += 1
        elif upper in ("VALUE", "VALUES"):
            index += 1
            while index < len(tokens) and (tokens[index].literal or tokens[index].text.upper() not in _CLAUSE_WORDS):
                index += 1
        elif upper in ("EXTERNAL", "GLOBAL"):
            index += 1
        elif upper == "GROUP-USAGE":
            index += 2
        elif upper.isdigit() and index > 1:
            raise CopybookError(
                f"Copybook line {token.line}: '{token.text}' looks like the level number of the next field. "
                f"Is the period missing at the end of {label}? Each field ends with a period."
            )
        else:
            raise CopybookError(
                f"Copybook line {token.line}: did not understand '{token.text}' in the definition of {label}."
            )
    return item


def _usage(token: _Token, label: str) -> str:
    upper = token.text.upper()
    if upper in _UNSUPPORTED_USAGES:
        raise CopybookError(
            f"Copybook line {token.line}: {label} is {_UNSUPPORTED_USAGES[upper]}, which is not supported."
        )
    if upper not in _USAGES:
        raise CopybookError(f"Copybook line {token.line}: '{token.text}' is not a USAGE COBOL allows.")
    return _USAGES[upper]


def _key(name: str) -> str:
    return (name or "").strip().upper().replace("_", "-")


def _runs(children: list[Item]) -> list[list[Item]]:
    """Children grouped as [item, items that REDEFINE it...]."""
    runs: list[list[Item]] = []
    for child in children:
        if child.implicit_redefines and runs:
            runs[-1].append(child)  # another 01 record: an alternative layout
            continue
        if child.redefines:
            target = _key(child.redefines)
            if not runs or _key(runs[-1][0].name) != target:
                raise CopybookError(
                    f"Copybook line {child.line}: {child.label} redefines {child.redefines}, which must be "
                    "the item just before it at the same level (or redefined by the items in between)."
                )
            if child.level != runs[-1][0].level:
                raise CopybookError(
                    f"Copybook line {child.line}: {child.label} and {child.redefines} must have the same level number."
                )
            runs[-1].append(child)
        else:
            runs.append([child])
    return runs


def _build_tree(text: str) -> Item:
    if not (text or "").strip():
        raise CopybookError("The copybook is empty. Paste the COBOL record layout or choose its file.")
    root = Item(level=0, name="", line=0)
    stack: list[Item] = [root]
    first_record: Item | None = None
    count = 0
    for tokens in _statements(text):
        item = _parse_statement(tokens)
        if item is None:
            continue
        count += 1
        level = 1 if item.level == 77 else item.level
        item.level = level
        while stack[-1] is not root and stack[-1].level >= level:
            stack.pop()
        parent = stack[-1]
        if parent.picture is not None:
            raise CopybookError(
                f"Copybook line {item.line}: {item.label} sits under {parent.label}, which has a PIC. "
                "Only a group (no PIC) can hold other items."
            )
        if parent is root and level == 1:
            if first_record is None:
                first_record = item
            elif not item.redefines:
                # Several 01 records describe the same buffer: alternative layouts.
                item.redefines = first_record.name or "FILLER"
                item.implicit_redefines = True
        item.parent = parent
        parent.children.append(item)
        stack.append(item)
    if count == 0:
        raise CopybookError("The copybook has no fields. Each field starts with a level number such as 01 or 05.")
    if first_record is not None and any(child.level != 1 for child in root.children):
        raise CopybookError(
            "The copybook mixes 01 records with fields that are not under any 01 record. "
            "Put every field under an 01 record."
        )
    return root


def _apply_usage(item: Item, inherited: str) -> None:
    item.effective_usage = item.usage or inherited
    for child in item.children:
        _apply_usage(child, item.effective_usage)


def _storage(item: Item) -> int:
    usage = item.effective_usage
    picture = item.picture
    if usage == "float4" and picture is None:
        return 4
    if usage == "float8" and picture is None:
        return 8
    if picture is None:
        raise CopybookError(f"Copybook line {item.line}: {item.label} has no PIC and no fields under it.")
    if usage in ("float4", "float8"):
        raise CopybookError(f"Copybook line {item.line}: {item.label} is COMP-1/COMP-2 and must not have a PIC.")
    if picture.kind == "text":
        return picture.length
    if picture.kind == "national":
        return picture.length * 2
    if usage == "national":
        raise CopybookError(f"Copybook line {item.line}: {item.label} is a NATIONAL number, which is not supported.")
    if picture.kind == "edited":
        return picture.length
    if usage == "packed":
        return picture.digits // 2 + 1
    if usage == "binary":
        if picture.digits <= 4:
            return 2
        if picture.digits <= 9:
            return 4
        if picture.digits <= 18:
            return 8
        raise CopybookError(f"Copybook line {item.line}: {item.label} is binary with more than 18 digits.")
    return picture.digits + (1 if item.sign_separate else 0)


def _size(item: Item) -> int:
    if item.children:
        if item.picture is not None:
            raise CopybookError(f"Copybook line {item.line}: {item.label} has a PIC and fields under it.")
        total = 0
        for run in _runs(item.children):
            total += max(_size(child) * child.occurs_max for child in run)
        item.size = total
    else:
        item.size = _storage(item)
    return item.size


def _place(item: Item, offset: int) -> None:
    item.offset = offset
    position = offset
    for run in _runs(item.children):
        for child in run:
            _place(child, position)
        position += max(child.size * child.occurs_max for child in run)


# ── Layout ──

@dataclass
class Options:
    code_page: str = "cp037"
    trim: str = "right"
    empty_as_null: bool = True
    big_endian: bool = True
    ibm_float: bool = True
    underscores: bool = True


@dataclass
class Slot:
    item: Item
    column: int | None
    offset: int
    length: int
    decode: Callable[[bytes], Any]
    arrow_type: Any
    table: int = -1          # which OCCURS DEPENDING ON table it is in
    occurrence: int = 0
    shift_index: int = 0
    branches: tuple = ()     # ((group, branch), ...)
    name: str = ""


@dataclass
class _Table:
    item: Item
    count_name: str
    start: int
    end: int
    element_size: int
    count_slot: "Slot | None" = None


@dataclass
class _Group:
    branches: list[Item]
    offset: int


def _text_decoder(options: Options):
    decode = codepages.decoder(options.code_page)
    rule = (options.trim or "right").lower()
    if rule == "both":
        tidy = lambda s: s.strip(" \x00")  # noqa: E731
    elif rule == "left":
        tidy = lambda s: s.lstrip(" ").rstrip("\x00")  # noqa: E731
    elif rule == "none":
        tidy = lambda s: s  # noqa: E731
    else:
        tidy = lambda s: s.rstrip(" \x00")  # noqa: E731
    empty_as_null = options.empty_as_null

    def text(data: bytes):
        value = tidy(decode(data))
        return None if (empty_as_null and value == "") else value

    return text


def _decoder_for(item: Item, options: Options):
    import pyarrow as pa

    usage = item.effective_usage
    picture = item.picture
    if usage in ("float4", "float8") and picture is None:
        if options.ibm_float:
            return values.ibm_float, pa.float64()
        big = options.big_endian
        return (lambda data: values.ieee_float(data, big_endian=big)), pa.float64()
    assert picture is not None
    if picture.kind == "text" or (picture.kind != "national" and usage == "national"):
        return _text_decoder(options), pa.string()
    if picture.kind == "national":
        def national(data: bytes):
            value = data.decode("utf-16-be", "replace").rstrip(" \x00")
            return None if (options.empty_as_null and value == "") else value
        return national, pa.string()
    if picture.kind == "edited":
        text = _text_decoder(Options(code_page=options.code_page, trim="both", empty_as_null=True))
        scale = max(picture.scale, 0)

        def edited(data: bytes):
            shown = text(data)
            if shown is None:
                return None
            return values.decimal_text(shown, scale)

        precision = min(38, max(picture.digits, scale + 1))
        return edited, (pa.decimal128(precision, scale) if scale else pa.int64())

    scale = picture.scale
    digits = picture.digits
    if usage == "packed":
        decode = lambda data, s=scale: values.packed(data, s)  # noqa: E731
    elif usage == "binary":
        signed = picture.signed
        big = options.big_endian
        decode = lambda data, s=scale: values.binary(data, s, signed=signed, big_endian=big)  # noqa: E731
        width = {2: 5, 4: 10, 8: 19 if signed else 20}[_storage(item)]
        digits = max(digits, width)
    elif codepages.is_ebcdic(options.code_page):
        leading, separate = item.sign_leading, item.sign_separate
        decode = lambda data, s=scale: values.zoned_ebcdic(  # noqa: E731
            data, s, sign_leading=leading, sign_separate=separate,
        )
    else:
        leading, separate = item.sign_leading, item.sign_separate
        decode = lambda data, s=scale: values.zoned_text(  # noqa: E731
            data.decode("latin-1"), s, sign_leading=leading, sign_separate=separate, micro_focus=True,
        )
    if scale <= 0:
        total = digits + max(0, -scale)
        if total <= 18 or (usage == "binary" and total == 19 and picture.signed):
            return decode, pa.int64()
        return decode, pa.decimal128(min(38, total), 0)
    precision = max(digits, scale)
    if precision > 38:
        raise CopybookError(f"Copybook line {item.line}: {item.label} needs more than 38 digits.")
    return decode, pa.decimal128(precision, scale)


@dataclass
class Layout:
    """A copybook ready to decode records."""
    root: Item
    options: Options
    slots: list[Slot]
    columns: list[tuple[str, Any]]
    record_length: int
    tables: list[_Table]
    groups: list[_Group]
    rules: dict[int, list[tuple[int, Slot, set, set]]] = field(default_factory=dict)
    unmatched_records: int = 0

    # ── description for the settings screen ──

    def describe(self) -> dict[str, Any]:
        fields = []
        for slot in self.slots:
            if slot.column is None:
                continue
            fields.append({
                "column": slot.name,
                "cobol_name": slot.item.name,
                "start": slot.offset + 1,
                "length": slot.length,
                "type": _plain_type(slot.item),
                "column_type": str(slot.arrow_type),
                "repeats": slot.table >= 0,
                "in_redefines": bool(slot.branches),
            })
        branches = []
        for index, group in enumerate(self.groups):
            branches.append({
                "group": index,
                "start": group.offset + 1,
                "branches": [b.label for b in group.branches],
            })
        selectors = sorted({
            slot.item.name for slot in self.slots
            if slot.column is not None and slot.table < 0 and not slot.item.has_occurs
            and not _in_occurs(slot.item)
        })
        return {
            "record_length": self.record_length,
            "column_count": len(self.columns),
            "fields": fields,
            "redefines": branches,
            "selector_fields": selectors,
            "variable_tables": [
                {"name": t.item.label, "depending_on": t.count_name, "max": t.item.occurs_max}
                for t in self.tables
            ],
        }

    # ── decoding ──

    def decode(self, record: bytes, number: int = 0, offset: int = 0, *, stop_on_bad: bool = True,
               bad: list | None = None) -> list[Any]:
        counts: list[int] = []
        shifts = [0]
        for table in self.tables:
            count = self._count(table, record, shifts, number, offset)
            counts.append(count)
            shifts.append(shifts[-1] + (table.item.occurs_max - count) * table.element_size)
        selected = self._select(record, shifts, number, offset) if self.rules else None
        out: list[Any] = [None] * len(self.columns)
        size = len(record)
        for slot in self.slots:
            if slot.column is None:
                continue
            if selected is not None and slot.branches:
                skip = False
                for group, branch in slot.branches:
                    chosen = selected.get(group)
                    if chosen is not None and branch not in chosen:
                        skip = True
                        break
                if skip:
                    continue
            if slot.table >= 0 and slot.occurrence >= counts[slot.table]:
                continue
            start = slot.offset - shifts[slot.shift_index]
            end = start + slot.length
            if end > size:
                if start >= size:
                    continue
                problem = values.BadValue(f"the record ends inside this field ({size:,} bytes long)")
                self._bad(slot, problem, number, offset, start, stop_on_bad, bad)
                continue
            try:
                out[slot.column] = slot.decode(record[start:end])
            except values.BadValue as problem:
                self._bad(slot, problem, number, offset, start, stop_on_bad, bad)
        return out

    def _bad(self, slot: Slot, problem: Exception, number: int, offset: int, start: int,
             stop_on_bad: bool, bad: list | None) -> None:
        if stop_on_bad:
            where = f"record {number:,} (byte {offset:,})" if number else "the record"
            hint = ""
            if slot.branches:
                hint = (" This field is in a REDEFINES; add a rule that says which records use it, "
                        "or set bad values to be left empty.")
            else:
                hint = " Check the code page, the record format and the copybook, or set bad values to be left empty."
            raise ValueError(
                f"Copybook: {where}, field {slot.item.label} (bytes {start + 1:,} to {start + slot.length:,}): "
                f"{problem}.{hint}"
            ) from None
        if bad is not None:
            bad.append(1)

    def _count(self, table: _Table, record: bytes, shifts: list[int], number: int, offset: int) -> int:
        slot = table.count_slot
        assert slot is not None
        # The count sits before its table, so only tables before the count move it.
        start = slot.offset - shifts[slot.shift_index]
        try:
            value = slot.decode(record[start:start + slot.length])
        except values.BadValue as problem:
            raise ValueError(
                f"Copybook: record {number:,} (byte {offset:,}): the count {table.count_name} for "
                f"{table.item.label} cannot be read: {problem}."
            ) from None
        count = int(value or 0)
        if count < table.item.occurs_min or count > table.item.occurs_max:
            raise ValueError(
                f"Copybook: record {number:,} (byte {offset:,}): {table.count_name} says {count}, but "
                f"{table.item.label} occurs {table.item.occurs_min} to {table.item.occurs_max} times."
            )
        return count

    def _select(self, record: bytes, shifts: list[int], number: int, offset: int) -> dict[int, set | None]:
        selected: dict[int, set | None] = {}
        for group_index, rules in self.rules.items():
            chosen: set | None = None
            ruled = set()
            for branch, slot, texts, numbers in rules:
                ruled.add(branch)
                start = slot.offset - shifts[slot.shift_index]
                try:
                    value = slot.decode(record[start:start + slot.length])
                except values.BadValue:
                    continue
                if _matches(value, texts, numbers):
                    chosen = {branch}
                    break
            if chosen is None:
                others = {i for i in range(len(self.groups[group_index].branches)) if i not in ruled}
                chosen = others
                if not others:
                    self.unmatched_records += 1
            selected[group_index] = chosen
        return selected


def _matches(value: Any, texts: set, numbers: set) -> bool:
    if value is None:
        return "" in texts
    if isinstance(value, (int, Decimal)) and numbers:
        return Decimal(value) in numbers
    return str(value).strip() in texts


def _in_occurs(item: Item) -> bool:
    node = item.parent
    while node is not None:
        if node.has_occurs:
            return True
        node = node.parent
    return False


def _plain_type(item: Item) -> str:
    usage = item.effective_usage
    picture = item.picture
    if picture is None:
        return "Floating point (COMP-1)" if usage == "float4" else "Floating point (COMP-2)"
    characters = "1 character" if picture.length == 1 else f"{picture.length} characters"
    if picture.kind == "text":
        return f"Text, {characters}"
    if picture.kind == "national":
        return f"National text, {characters}"
    if picture.kind == "edited":
        return f"Edited number ({picture.text})"
    sign = "signed " if picture.signed else ""
    places = f", {picture.scale} decimal places" if picture.scale > 0 else ""
    kind = {"packed": "packed decimal (COMP-3)", "binary": "binary (COMP)"}.get(usage, "zoned decimal")
    return f"Number, {sign}{kind}, {picture.digits} digits{places}"


def _column_names(leaves: list[tuple[Item, str]], underscores: bool) -> list[str]:
    """Leaf names, qualified by their group's name only where they repeat."""
    def clean(name: str) -> str:
        return name.replace("-", "_") if underscores else name

    def chain(item: Item) -> list[str]:
        names = []
        node = item.parent
        while node is not None and node.level > 0:
            if not node.is_filler:
                names.append(node.name)
            node = node.parent
        return names

    bases = [item.name for item, _ in leaves]
    depth = [0] * len(leaves)
    for _ in range(50):
        counts: dict[str, int] = {}
        for base, (item, suffix) in zip(bases, leaves):
            key = (base + suffix).upper()
            counts[key] = counts.get(key, 0) + 1
        repeated = [i for i, (base, (item, suffix)) in enumerate(zip(bases, leaves))
                    if counts[(base + suffix).upper()] > 1]
        if not repeated:
            break
        changed = False
        for i in repeated:
            item, _ = leaves[i]
            parents = chain(item)
            if depth[i] < len(parents):
                depth[i] += 1
                bases[i] = "-".join(reversed(parents[:depth[i]])) + "-" + item.name
                changed = True
        if not changed:
            break
    from revoluzen_flow.legacy_files.landing import unique_names
    return unique_names([clean(base + suffix) for base, (_, suffix) in zip(bases, leaves)])


def compile_copybook(text: str, options: Options | None = None,
                     rules: Iterable[dict[str, Any]] | None = None) -> Layout:
    """Read a copybook and build the layout that decodes its records."""
    options = options or Options()
    options.code_page = codepages.normalise(options.code_page, default="cp037")
    root = _build_tree(text)
    _apply_usage(root, "display")
    _size(root)
    _place(root, 0)

    slots: list[Slot] = []
    tables: list[_Table] = []
    groups: list[_Group] = []
    leaves: list[tuple[Item, str]] = []
    runs_cache: dict[int, list[list[Item]]] = {}
    group_of_run: dict[int, int] = {}

    def runs_of(item: Item) -> list[list[Item]]:
        found = runs_cache.get(id(item))
        if found is None:
            found = runs_cache[id(item)] = _runs(item.children)
        return found

    def walk(item: Item, delta: int, suffix: str, branches: tuple, table: int, occurrence: int,
             inside_occurs: bool) -> None:
        if item.depending_on:
            if inside_occurs or table >= 0:
                raise CopybookError(
                    f"Copybook line {item.line}: {item.label} has OCCURS DEPENDING ON inside another "
                    "table. This is not supported."
                )
            if branches:
                raise CopybookError(
                    f"Copybook line {item.line}: {item.label} has OCCURS DEPENDING ON inside a "
                    "REDEFINES. This is not supported."
                )
            tables.append(_Table(item, item.depending_on, item.offset + delta,
                                 item.offset + delta + item.size * item.occurs_max, item.size))
            table_index = len(tables) - 1
        else:
            table_index = table
        repeat = item.occurs_max > 1 or item.has_occurs
        for k in range(item.occurs_max):
            here = delta + k * item.size
            item_suffix = suffix + (f"_{k + 1}" if repeat else "")
            this_occurrence = k if item.depending_on else occurrence
            if item.children:
                for run in runs_of(item):
                    if len(run) == 1:
                        walk(run[0], here, item_suffix, branches, table_index, this_occurrence,
                             inside_occurs or repeat)
                        continue
                    # One group per REDEFINES, shared by every occurrence of a
                    # repeating parent, so a rule applies to all of them.
                    group_index = group_of_run.get(id(run))
                    if group_index is None:
                        groups.append(_Group(run, run[0].offset + here))
                        group_index = group_of_run[id(run)] = len(groups) - 1
                    for branch_index, child in enumerate(run):
                        walk(child, here, item_suffix, branches + ((group_index, branch_index),),
                             table_index, this_occurrence, inside_occurs or repeat)
            else:
                decode, arrow_type = _decoder_for(item, options)
                column = None
                if not item.is_filler:
                    column = len(leaves)
                    leaves.append((item, item_suffix))
                slots.append(Slot(
                    item=item, column=column, offset=item.offset + here, length=item.size,
                    decode=decode, arrow_type=arrow_type, table=table_index,
                    occurrence=this_occurrence, branches=branches,
                ))

    for run in runs_of(root):
        if len(run) == 1:
            walk(run[0], 0, "", (), -1, 0, False)
        else:
            groups.append(_Group(run, 0))
            group_index = len(groups) - 1
            for branch_index, child in enumerate(run):
                walk(child, 0, "", ((group_index, branch_index),), -1, 0, False)

    if not leaves:
        raise CopybookError("The copybook has no named fields to read (only FILLER).")

    names = _column_names(leaves, options.underscores)
    for slot in slots:
        if slot.column is not None:
            slot.name = names[slot.column]
    columns = [(names[i], None) for i in range(len(leaves))]
    for slot in slots:
        if slot.column is not None:
            columns[slot.column] = (slot.name, slot.arrow_type)

    # Each slot's shift = the shrinking of the variable tables before it.
    for slot in slots:
        position = slot.offset
        slot.shift_index = sum(1 for t in tables if t.end <= position) if slot.table < 0 else \
            sum(1 for t in tables[:slot.table])
    by_name: dict[str, list[Slot]] = {}
    for slot in slots:
        if slot.item.name:
            by_name.setdefault(_key(slot.item.name), []).append(slot)
        if slot.column is not None:
            by_name.setdefault(_key(slot.name), []).append(slot)
    for table in tables:
        found = [s for s in by_name.get(_key(table.count_name), []) if s.table < 0]
        if not found:
            raise CopybookError(
                f"Copybook line {table.item.line}: {table.item.label} depends on {table.count_name}, "
                "which is not a field in this copybook."
            )
        count_slot = found[0]
        if count_slot.item.picture is None or count_slot.item.picture.kind not in ("number", "edited"):
            raise CopybookError(
                f"Copybook line {table.item.line}: {table.count_name}, which counts {table.item.label}, "
                "must be a number."
            )
        if count_slot.offset >= table.start:
            raise CopybookError(
                f"Copybook line {table.item.line}: {table.count_name} must come before {table.item.label}, "
                "the table it counts."
            )
        table.count_slot = count_slot

    record_length = root.size
    layout = Layout(root=root, options=options, slots=slots, columns=columns,
                    record_length=record_length, tables=tables, groups=groups)
    layout.rules = _compile_rules(layout, rules or [], by_name)
    return layout


def _split_values(raw: Any) -> list[str]:
    if isinstance(raw, (list, tuple)):
        parts = [str(v) for v in raw]
    else:
        parts = []
        current = ""
        quote = ""
        for char in str(raw or ""):
            if quote:
                if char == quote:
                    quote = ""
                else:
                    current += char
            elif char in "'\"":
                quote = char
            elif char == ",":
                parts.append(current)
                current = ""
            else:
                current += char
        parts.append(current)
    return [p.strip() for p in parts]


def _compile_rules(layout: Layout, rules: Iterable[dict[str, Any]], by_name: dict[str, list[Slot]]):
    compiled: dict[int, list[tuple[int, Slot, set, set]]] = {}
    for number, rule in enumerate(rules, start=1):
        if not isinstance(rule, dict):
            continue
        branch_name = str(rule.get("branch") or "").strip()
        field_name = str(rule.get("field") or "").strip()
        raw_values = rule.get("values")
        if not branch_name and not field_name and not raw_values:
            continue  # an empty row in the form
        if not branch_name or not field_name:
            raise CopybookError(f"REDEFINES rule {number} needs the part to read and the field that decides.")
        found = None
        for group_index, group in enumerate(layout.groups):
            for branch_index, branch in enumerate(group.branches):
                if _key(branch.name) == _key(branch_name):
                    found = (group_index, branch_index)
        if found is None:
            names = sorted({b.label for g in layout.groups for b in g.branches})
            raise CopybookError(
                f"REDEFINES rule {number}: '{branch_name}' is not one of the REDEFINES parts. "
                f"Parts in this copybook: {', '.join(names) or 'none'}."
            )
        candidates = [
            s for s in by_name.get(_key(field_name), [])
            if s.table < 0 and not s.item.has_occurs and not _in_occurs(s.item)
        ]
        if not candidates:
            raise CopybookError(
                f"REDEFINES rule {number}: '{field_name}' is not a single field in this copybook."
            )
        selector = candidates[0]
        texts = set(_split_values(raw_values))
        numbers = set()
        for text in texts:
            try:
                numbers.add(Decimal(text))
            except (InvalidOperation, ValueError):
                pass
        group_index, branch_index = found
        compiled.setdefault(group_index, []).append((branch_index, selector, texts, numbers))
    return compiled


# ── Reading a file ──

def options_from_params(params: dict[str, Any]) -> Options:
    return Options(
        code_page=str(params.get("code_page") or "cp037"),
        trim=str(params.get("text_trim") or "right"),
        empty_as_null=params.get("empty_as_null", True) not in (False, "false", "0", 0),
        big_endian=str(params.get("binary_byte_order") or "big").lower() != "little",
        ibm_float=str(params.get("float_format") or "ibm").lower() != "ieee",
        underscores=str(params.get("column_names") or "underscores").lower() != "as_written",
    )


def read_copybook_file(conn, stream: BinaryIO, copybook_text: str, params: dict[str, Any], *,
                       limit: int | None = None):
    """Read records from one stream with the copybook."""
    return _read(conn, [stream], copybook_text, params, limit)


def read_copybook_files(conn, paths: list[str], copybook_text: str, params: dict[str, Any], *,
                        limit: int | None = None):
    """Read records from one or more files into one relation. Returns the
    relation and counts for the step's log."""
    streams = []
    try:
        for path in paths:
            streams.append(open(path, "rb"))
        return _read(conn, streams, copybook_text, params, limit)
    finally:
        for stream in streams:
            stream.close()


def _read(conn, streams: list[BinaryIO], copybook_text: str, params: dict[str, Any], limit: int | None):
    options = options_from_params(params)
    layout = compile_copybook(copybook_text, options, params.get("redefines_rules") or [])
    record_format = str(params.get("record_format") or "fixed").strip().lower()
    try:
        record_length = int(params.get("record_length") or 0) or layout.record_length
    except (TypeError, ValueError):
        raise ValueError(f"Record length must be a whole number, not '{params.get('record_length')}'.") from None
    stop_on_bad = str(params.get("bad_value_rule") or "stop").strip().lower() != "empty"
    lander = Lander(conn, layout.columns)
    bad: list = []
    for stream in streams:
        remaining = None if limit is None else limit - lander.total
        if remaining is not None and remaining <= 0:
            break
        for number, offset, record in iter_records(
            stream, record_format, record_length=record_length,
            ebcdic=codepages.is_ebcdic(options.code_page), limit=remaining,
        ):
            lander.add(layout.decode(record, number, offset, stop_on_bad=stop_on_bad, bad=bad))
    relation = lander.finish()
    return relation, {
        "rows": lander.row_count,
        "bad_values": len(bad),
        "unmatched_redefines": layout.unmatched_records,
        "record_length": layout.record_length,
        "files": len(streams),
    }
