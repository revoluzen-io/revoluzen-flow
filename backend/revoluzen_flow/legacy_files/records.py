"""Splitting a file into records.

A mainframe dataset is a list of records, and how they sit in a file depends on
how it was copied off the machine:

* ``lines``: one record per line. Text files, and files already converted to
  ASCII. In EBCDIC a line ends with NL (0x15) or LF (0x25).
* ``fixed``: every record is the same length and nothing separates them
  (RECFM=F or FB copied in binary).
* ``rdw``: each record starts with a 4-byte Record Descriptor Word: two bytes
  of length (counting the RDW itself, big-endian) and two zero bytes
  (RECFM=V or VB copied in binary with the RDW kept, for example FTP
  ``quote site rdw``).
* ``bdw_rdw``: as ``rdw``, with records grouped in blocks that each start with
  a 4-byte Block Descriptor Word (RECFM=VB copied block by block).

Every record comes with its number (from 1) and the byte it starts at, so an
error can say where to look.
"""

from __future__ import annotations

from typing import BinaryIO, Iterator

RECORD_FORMATS: list[tuple[str, str]] = [
    ("lines", "One record per line"),
    ("fixed", "Fixed length, no line breaks"),
    ("rdw", "Variable length, each record starts with its length (RDW)"),
    ("bdw_rdw", "Variable length in blocks (BDW and RDW)"),
]

_CHUNK = 1 << 20  # 1 MiB


class RecordError(ValueError):
    """The file does not split into records the way the step says."""


def iter_records(
    stream: BinaryIO,
    record_format: str,
    *,
    record_length: int = 0,
    ebcdic: bool = False,
    limit: int | None = None,
) -> Iterator[tuple[int, int, bytes]]:
    """Yield ``(record_number, byte_offset, record_bytes)``."""
    fmt = (record_format or "lines").strip().lower()
    if fmt == "lines":
        yield from _lines(stream, ebcdic=ebcdic, limit=limit)
    elif fmt == "fixed":
        if record_length <= 0:
            raise RecordError("Fixed-length records need a record length above 0.")
        yield from _fixed(stream, record_length, ebcdic=ebcdic, limit=limit)
    elif fmt == "rdw":
        yield from _rdw(stream, limit=limit)
    elif fmt == "bdw_rdw":
        yield from _bdw_rdw(stream, limit=limit)
    else:
        known = ", ".join(value for value, _ in RECORD_FORMATS)
        raise RecordError(f"Unknown record format '{record_format}'. Choose one of: {known}.")


def _lines(stream: BinaryIO, *, ebcdic: bool, limit: int | None) -> Iterator[tuple[int, int, bytes]]:
    # EBCDIC NL (0x15) and LF (0x25) both end a line; CR is 0x0D in both sets.
    ends = (b"\x15", b"\x25") if ebcdic else (b"\n",)
    number = 0
    offset = 0
    pending = b""
    while True:
        chunk = stream.read(_CHUNK)
        if not chunk:
            break
        data = pending + chunk
        start = 0
        while True:
            cut = _next_end(data, start, ends)
            if cut < 0:
                break
            line = data[start:cut]
            if line.endswith(b"\r"):
                line = line[:-1]
            number += 1
            yield number, offset + start, line
            if limit is not None and number >= limit:
                return
            start = cut + 1
        offset += start
        pending = data[start:]
    if pending:
        line = pending[:-1] if pending.endswith(b"\r") else pending
        # A file that ends with a DOS end-of-file mark (Ctrl-Z) has no last record.
        if line and line != b"\x1a":
            number += 1
            yield number, offset, line


def _next_end(data: bytes, start: int, ends: tuple[bytes, ...]) -> int:
    found = -1
    for end in ends:
        position = data.find(end, start)
        if position >= 0 and (found < 0 or position < found):
            found = position
    return found


def _read_exact(stream: BinaryIO, size: int) -> bytes:
    parts = []
    remaining = size
    while remaining > 0:
        piece = stream.read(remaining)
        if not piece:
            break
        parts.append(piece)
        remaining -= len(piece)
    return b"".join(parts)


def _only_padding(data: bytes, ebcdic: bool) -> bool:
    filler = set(b"\r\n\x1a\x00 ")
    if ebcdic:
        filler |= {0x15, 0x25, 0x40}
    return all(byte in filler for byte in data)


def _fixed(stream: BinaryIO, size: int, *, ebcdic: bool, limit: int | None) -> Iterator[tuple[int, int, bytes]]:
    number = 0
    offset = 0
    while True:
        record = _read_exact(stream, size)
        if not record:
            return
        if len(record) < size:
            if _only_padding(record, ebcdic):
                return
            raise RecordError(
                f"The file ends with a partial record: {len(record)} bytes at byte {offset:,}, "
                f"where every record should be {size:,} bytes. Check the record length."
            )
        number += 1
        yield number, offset, record
        offset += size
        if limit is not None and number >= limit:
            return


def _rdw_length(header: bytes, offset: int, number: int) -> int:
    length = int.from_bytes(header[0:2], "big")
    if length < 4 or header[2:4] != b"\x00\x00":
        raise RecordError(
            f"Record {number:,} at byte {offset:,} does not start with a valid length (RDW). "
            "Check that the file was copied with its record lengths, or choose another record format."
        )
    return length - 4


def _rdw(stream: BinaryIO, *, limit: int | None) -> Iterator[tuple[int, int, bytes]]:
    number = 0
    offset = 0
    while True:
        header = _read_exact(stream, 4)
        if not header:
            return
        if len(header) < 4:
            if _only_padding(header, ebcdic=True):
                return
            raise RecordError(f"The file ends in the middle of a record length at byte {offset:,}.")
        number += 1
        size = _rdw_length(header, offset, number)
        record = _read_exact(stream, size)
        if len(record) < size:
            raise RecordError(
                f"Record {number:,} at byte {offset:,} says it is {size + 4:,} bytes long, "
                "but the file ends first."
            )
        yield number, offset + 4, record
        offset += 4 + size
        if limit is not None and number >= limit:
            return


def _bdw_rdw(stream: BinaryIO, *, limit: int | None) -> Iterator[tuple[int, int, bytes]]:
    number = 0
    offset = 0
    while True:
        bdw = _read_exact(stream, 4)
        if not bdw:
            return
        if len(bdw) < 4:
            if _only_padding(bdw, ebcdic=True):
                return
            raise RecordError(f"The file ends in the middle of a block length at byte {offset:,}.")
        if bdw[0] & 0x80:
            # Extended BDW: 31-bit length in all four bytes.
            block_length = int.from_bytes(bdw, "big") & 0x7FFFFFFF
        else:
            block_length = int.from_bytes(bdw[0:2], "big")
            if bdw[2:4] != b"\x00\x00":
                raise RecordError(f"The block at byte {offset:,} does not start with a valid length (BDW).")
        if block_length < 4:
            raise RecordError(f"The block at byte {offset:,} has an impossible length of {block_length}.")
        block = _read_exact(stream, block_length - 4)
        if len(block) < block_length - 4:
            raise RecordError(f"The block at byte {offset:,} is cut short by the end of the file.")
        position = 0
        while position < len(block):
            if len(block) - position < 4:
                raise RecordError(f"The block at byte {offset:,} ends in the middle of a record length.")
            number += 1
            record_offset = offset + 4 + position
            size = _rdw_length(block[position:position + 4], record_offset, number)
            start = position + 4
            if start + size > len(block):
                raise RecordError(
                    f"Record {number:,} at byte {record_offset:,} runs past the end of its block."
                )
            yield number, record_offset + 4, block[start:start + size]
            if limit is not None and number >= limit:
                return
            position = start + size
        offset += block_length
