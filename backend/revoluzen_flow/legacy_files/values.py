"""Reading single values from mainframe and fixed-width records.

Each function takes the bytes (or text) of one field and returns a Python value,
or raises ``BadValue`` with a short reason. The caller adds where the field is.

Numbers come back as ``int`` when they have no decimal places and as
``decimal.Decimal`` when they do, so no digit is lost to floating point.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, localcontext
from typing import Callable

_DIGITS = frozenset("0123456789")


class BadValue(ValueError):
    """A field's bytes do not hold the kind of value the layout says."""


def _scaled(number: int, scale: int) -> int | Decimal:
    if scale == 0:
        return number
    if scale < 0:  # PIC 999PPP: the number is in thousands
        return number * (10 ** -scale)
    # Built from text so no digit is rounded by the decimal context.
    return Decimal(f"{number}E-{scale}")


# ── Packed decimal (COMP-3) ──

def packed(data: bytes, scale: int = 0) -> int | Decimal | None:
    """Two digits per byte, the last half-byte is the sign: C, A, E or F for
    plus, D or B for minus. All spaces or all binary zeros reads as empty."""
    if not data:
        return None
    if data.count(0) == len(data) or data.count(0x40) == len(data):
        return None
    hexed = data.hex()
    digits, sign = hexed[:-1], hexed[-1]
    if not digits.isdigit():
        raise BadValue(f"not a packed decimal (bytes {_show_hex(data)})")
    if sign in "cfae":
        negative = False
    elif sign in "db":
        negative = True
    else:
        raise BadValue(f"packed decimal with an unknown sign '{sign.upper()}' (bytes {_show_hex(data)})")
    number = int(digits)
    return _scaled(-number if negative else number, scale)


# ── Zoned decimal (DISPLAY numbers) ──

# In EBCDIC each digit is a byte 0xF0..0xF9; a signed field carries its sign in
# the top half of one byte: C (or A, E, F) plus, D (or B) minus.
_EBCDIC_SIGN_PLUS = frozenset((0xC, 0xA, 0xE, 0xF))
_EBCDIC_SIGN_MINUS = frozenset((0xD, 0xB))

# A file converted to ASCII shows the signed last digit as a letter.
_OVERPUNCH = {
    "{": ("0", False), "A": ("1", False), "B": ("2", False), "C": ("3", False), "D": ("4", False),
    "E": ("5", False), "F": ("6", False), "G": ("7", False), "H": ("8", False), "I": ("9", False),
    "}": ("0", True), "J": ("1", True), "K": ("2", True), "L": ("3", True), "M": ("4", True),
    "N": ("5", True), "O": ("6", True), "P": ("7", True), "Q": ("8", True), "R": ("9", True),
}
# Micro Focus and some PC COBOLs mark a negative last digit as p..y (0x70..0x79).
# Only copybook records use these: in a plain text column "12x" is an error.
_MICRO_FOCUS_NEGATIVE = {ch: (str(i), True) for i, ch in enumerate("pqrstuvwxy")}


def zoned_ebcdic(
    data: bytes,
    scale: int = 0,
    *,
    sign_leading: bool = False,
    sign_separate: bool = False,
) -> int | Decimal | None:
    """EBCDIC zoned decimal, with the sign in a digit's top half or as its own
    ``+`` / ``-`` byte."""
    if not data:
        return None
    if data.count(0x40) == len(data) or data.count(0) == len(data):
        return None
    negative = False
    body = data
    if sign_separate:
        sign_byte = data[0] if sign_leading else data[-1]
        body = data[1:] if sign_leading else data[:-1]
        if sign_byte == 0x60:  # '-'
            negative = True
        elif sign_byte not in (0x4E, 0x40):  # '+' or space
            raise BadValue(f"sign byte is not + or - (bytes {_show_hex(data)})")
    # Leading spaces are tolerated: some programs write numbers right-aligned.
    stripped = body.lstrip(b"\x40")
    if not stripped:
        return None
    digits = []
    last = len(stripped) - 1
    for index, byte in enumerate(stripped):
        zone, digit = byte >> 4, byte & 0x0F
        if digit > 9:
            raise BadValue(f"not a number (bytes {_show_hex(data)})")
        sign_here = (index == 0) if sign_leading else (index == last)
        if not sign_separate and sign_here:
            if zone in _EBCDIC_SIGN_MINUS:
                negative = True
            elif zone not in _EBCDIC_SIGN_PLUS:
                raise BadValue(f"not a number (bytes {_show_hex(data)})")
        elif zone != 0xF:
            raise BadValue(f"not a number (bytes {_show_hex(data)})")
        digits.append(digit)
    number = 0
    for digit in digits:
        number = number * 10 + digit
    return _scaled(-number if negative else number, scale)


def zoned_text(
    text: str,
    scale: int = 0,
    *,
    sign_leading: bool = False,
    sign_separate: bool = False,
    micro_focus: bool = False,
) -> int | Decimal | None:
    """Zoned decimal already turned into text: digits, with the sign as
    ``+``/``-`` or as an overpunch letter on the last (or first) digit.
    ``micro_focus`` also reads p..y as a negative last digit."""
    value = text.strip()
    if not value:
        return None
    negative = False
    if sign_separate or value[0] in "+-" or value[-1] in "+-":
        if value[0] in "+-":
            negative = value[0] == "-"
            value = value[1:]
        elif value[-1] in "+-":
            negative = value[-1] == "-"
            value = value[:-1]
        value = value.strip()
    elif value:
        position = 0 if sign_leading else -1
        mark = value[position]
        table = {**_OVERPUNCH, **_MICRO_FOCUS_NEGATIVE} if micro_focus else _OVERPUNCH
        if mark in table:
            digit, minus = table[mark]
            negative = minus
            value = (digit + value[1:]) if sign_leading else (value[:-1] + digit)
    if not value or not set(value) <= _DIGITS:
        raise BadValue(f"not a number ({_show_text(text)})")
    number = int(value)
    return _scaled(-number if negative else number, scale)


# ── Binary (COMP, COMP-4, COMP-5, BINARY) ──

def binary(data: bytes, scale: int = 0, *, signed: bool = True, big_endian: bool = True) -> int | Decimal | None:
    if not data:
        return None
    number = int.from_bytes(data, "big" if big_endian else "little", signed=signed)
    return _scaled(number, scale)


# ── Floating point (COMP-1, COMP-2) ──

def ibm_float(data: bytes) -> float | None:
    """IBM hexadecimal floating point, the mainframe's own format: a sign bit,
    a 7-bit power of 16 (offset by 64) and a fraction."""
    if not data:
        return None
    if data.count(0) == len(data):
        return 0.0
    first = data[0]
    negative = bool(first & 0x80)
    exponent = (first & 0x7F) - 64
    fraction = int.from_bytes(data[1:], "big")
    bits = 8 * (len(data) - 1)
    value = fraction / float(1 << bits) * (16.0 ** exponent)
    return -value if negative else value


def ieee_float(data: bytes, *, big_endian: bool = True) -> float | None:
    import struct

    if not data:
        return None
    if len(data) == 4:
        return struct.unpack(">f" if big_endian else "<f", data)[0]
    if len(data) == 8:
        return struct.unpack(">d" if big_endian else "<d", data)[0]
    raise BadValue(f"a floating point number must be 4 or 8 bytes, not {len(data)}")


# ── Numbers written as text ──

def decimal_text(text: str, scale: int, *, decimal_separator: str = ".") -> Decimal | None:
    """A number with its decimal point written out, such as ``-1,234.50`` or
    ``12.50-`` or ``(12.50)``. Thousands separators and a currency sign are
    skipped. More decimal places than ``scale`` is an error, not rounding."""
    value = text.strip()
    if not value:
        return None
    negative = False
    if value.startswith("(") and value.endswith(")"):
        negative, value = True, value[1:-1].strip()
    for mark in ("CR", "DB"):
        if value.upper().endswith(mark):
            negative, value = True, value[:-2].strip()
    if value.endswith("-"):
        negative, value = True, value[:-1].strip()
    elif value.endswith("+"):
        value = value[:-1].strip()
    if value.startswith("-"):
        negative, value = True, value[1:].strip()
    elif value.startswith("+"):
        value = value[1:].strip()
    thousands = "," if decimal_separator == "." else "."
    value = value.replace(thousands, "").replace(" ", "").lstrip("$£€*").replace("*", "")
    if decimal_separator != ".":
        value = value.replace(decimal_separator, ".")
    if not value or value.count(".") > 1 or not set(value) <= (_DIGITS | {"."}):
        raise BadValue(f"not a number ({_show_text(text)})")
    try:
        number = Decimal(value)
    except InvalidOperation:
        raise BadValue(f"not a number ({_show_text(text)})") from None
    whole, _, fraction = value.partition(".")
    if len(fraction.rstrip("0")) > scale:
        raise BadValue(f"has more than {scale} decimal places ({_show_text(text)})")
    if negative:
        number = -number
    if scale <= 0:
        return int(number)  # type: ignore[return-value]
    with localcontext() as context:
        context.prec = 80
        return number.quantize(Decimal(f"1E-{scale}"))


def integer_text(text: str) -> int | None:
    value = text.strip()
    if not value:
        return None
    result = zoned_text(value)
    return result  # type: ignore[return-value]


# ── Dates written as text ──

_DATE_TOKENS = [
    ("YYYY", "%Y"), ("CCYY", "%Y"), ("DDD", "%j"), ("YY", "%y"), ("MM", "%m"), ("DD", "%d"),
    ("HH", "%H"), ("MI", "%M"), ("SS", "%S"),
]


def date_pattern(pattern: str) -> tuple[str, bool]:
    """Turn ``YYYYMMDD`` or ``DD/MM/YYYY`` into a strptime format. Returns the
    format and whether it has a time."""
    spec = (pattern or "YYYYMMDD").strip().upper()
    out = ""
    has_time = False
    index = 0
    while index < len(spec):
        for token, code in _DATE_TOKENS:
            if spec.startswith(token, index):
                out += code
                has_time = has_time or token in ("HH", "MI", "SS")
                index += len(token)
                break
        else:
            char = spec[index]
            if char.isalpha():
                raise ValueError(
                    f"Date format '{pattern}' uses '{char}'. Use YYYY, YY, MM, DD, DDD, HH, MI and SS."
                )
            out += "%%" if char == "%" else char
            index += 1
    return out, has_time


def date_text(text: str, fmt: str, has_time: bool):
    from datetime import datetime

    value = text.strip()
    if not value or set(value) <= {"0"} or set(value) <= {"9"}:
        return None
    try:
        parsed = datetime.strptime(value, fmt)
    except ValueError:
        raise BadValue(f"not a date in the expected format ({_show_text(text)})") from None
    return parsed if has_time else parsed.date()


# ── Showing a bad value without dumping the record ──

def _show_hex(data: bytes, limit: int = 12) -> str:
    shown = data[:limit].hex(" ").upper()
    return shown + (" ..." if len(data) > limit else "")


def _show_text(text: str, limit: int = 20) -> str:
    shown = text.strip()
    if len(shown) > limit:
        shown = shown[:limit] + "..."
    return repr(shown)


TextDecoder = Callable[[bytes], str]
