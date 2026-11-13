"""ASCII armor: the ``-----BEGIN PGP MESSAGE-----`` text form of PGP data."""

from __future__ import annotations

import base64
import binascii
import re
from typing import BinaryIO

from revoluzen_flow.openpgp.algorithms import PgpError

_CRC24_INIT = 0xB704CE
_CRC24_POLY = 0x1864CFB


def _crc_table() -> list[int]:
    table = []
    for index in range(256):
        crc = index << 16
        for _ in range(8):
            crc <<= 1
            if crc & 0x1000000:
                crc ^= _CRC24_POLY
        table.append(crc & 0xFFFFFF)
    return table


_TABLE = _crc_table()


def crc24(data: bytes, crc: int = _CRC24_INIT) -> int:
    table = _TABLE
    for byte in data:
        crc = ((crc << 8) & 0xFFFFFF) ^ table[((crc >> 16) ^ byte) & 0xFF]
    return crc & 0xFFFFFF


# The checksum only guards against transmission damage; the encrypted data
# has its own integrity check. Big armored files skip it, which saves time.
_CHECK_CRC_UP_TO = 8 * 1024 * 1024

_BEGIN = re.compile(r"-----BEGIN PGP ([A-Z ,/0-9]+)-----")
_HEADER = re.compile(r"^[A-Za-z][A-Za-z0-9-]*: ")


def is_armored(data: bytes) -> bool:
    return data[:4096].lstrip().startswith(b"-----BEGIN PGP")


def dearmor(text: str | bytes) -> list[tuple[str, bytes]]:
    """Every armored block in ``text`` as ``(kind, data)``, for example
    ``("PUBLIC KEY BLOCK", b"...")``."""
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    blocks: list[tuple[str, bytes]] = []
    position = 0
    while True:
        begin = _BEGIN.search(text, position)
        if not begin:
            break
        kind = begin.group(1).strip()
        end_marker = f"-----END PGP {kind}-----"
        end = text.find(end_marker, begin.end())
        if end < 0:
            raise PgpError(f"The armored {kind.lower()} has no END line.")
        blocks.append((kind, _decode_body(text[begin.end():end], kind)))
        position = end + len(end_marker)
    return blocks


def _decode_body(body: str, kind: str) -> bytes:
    lines = body.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if len([line for line in lines if line.strip()]) <= 1 and len(body.strip()) > 100:
        # The line breaks were lost (pasted into a one-line box).
        flat = body.strip()
        if ": " in flat[:200]:
            raise PgpError(
                "The armored text lost its line breaks and has header lines, so it cannot be read. "
                "Paste it again into the multi-line box."
            )
        lines = [flat]
    index = 0
    while index < len(lines) and not lines[index].strip():
        index += 1
    if index < len(lines) and _HEADER.match(lines[index].strip()):
        while index < len(lines) and lines[index].strip():
            index += 1
    data_lines: list[str] = []
    checksum = None
    for line in lines[index:]:
        line = line.strip()
        if not line:
            continue
        if line.startswith("=") and len(line) == 5:
            checksum = line[1:]
            break
        data_lines.append(line)
    joined = "".join(data_lines)
    if checksum is None:
        match = re.search(r"=([A-Za-z0-9+/]{4})$", joined)
        if match and (len(joined) - 5) % 4 == 0:
            checksum, joined = match.group(1), joined[:-5]
    try:
        data = base64.b64decode(joined, validate=False)
    except (binascii.Error, ValueError):
        raise PgpError(f"The armored {kind.lower()} is not valid base64.") from None
    if checksum is not None and len(data) <= _CHECK_CRC_UP_TO:
        try:
            expected = int.from_bytes(base64.b64decode(checksum), "big")
        except (binascii.Error, ValueError):
            expected = None
        if expected is not None and crc24(data) != expected:
            raise PgpError(f"The armored {kind.lower()} is damaged: its checksum does not match.")
    return data


def dearmor_stream(source: BinaryIO, out: BinaryIO) -> str:
    """Decode the first armored block of ``source`` into ``out`` line by line.
    Returns its kind, for example ``"MESSAGE"``."""
    kind = ""
    for raw in source:
        match = _BEGIN.search(raw.decode("ascii", "replace"))
        if match:
            kind = match.group(1).strip()
            break
    if not kind:
        raise PgpError("The file says it is armored but has no BEGIN PGP line.")
    end_marker = f"-----END PGP {kind}-----".encode("ascii")
    in_headers = True
    pending = b""
    finished = False
    for raw in source:
        line = raw.strip()
        if line.startswith(end_marker):
            finished = True
            break
        if in_headers:
            if not line:
                in_headers = False
                continue
            if _HEADER.match(line.decode("ascii", "replace")):
                continue
            in_headers = False
        if not line:
            continue
        if line.startswith(b"=") and len(line) == 5:
            continue  # checksum line; the data has its own integrity check
        pending += line
        usable = len(pending) - len(pending) % 4
        if usable:
            try:
                out.write(base64.b64decode(pending[:usable]))
            except (binascii.Error, ValueError):
                raise PgpError("The armored file is not valid base64.") from None
            pending = pending[usable:]
    if not finished:
        raise PgpError(f"The armored {kind.lower()} has no END line; the file may be cut short.")
    if pending:
        try:
            out.write(base64.b64decode(pending + b"=" * (-len(pending) % 4)))
        except (binascii.Error, ValueError):
            raise PgpError("The armored file is not valid base64.") from None
    return kind


def armor_bytes(kind: str, data: bytes) -> str:
    encoded = base64.b64encode(data).decode("ascii")
    lines = [encoded[i:i + 64] for i in range(0, len(encoded), 64)]
    crc = base64.b64encode(crc24(data).to_bytes(3, "big")).decode("ascii")
    return "\n".join([f"-----BEGIN PGP {kind}-----", "", *lines, f"={crc}", f"-----END PGP {kind}-----", ""])


class ArmorWriter:
    """Writes armored output a piece at a time."""

    def __init__(self, out: BinaryIO, kind: str = "MESSAGE"):
        self._out = out
        self._kind = kind
        self._pending = b""
        self._crc = _CRC24_INIT
        self._line = b""
        out.write(f"-----BEGIN PGP {kind}-----\n\n".encode("ascii"))

    def write(self, data: bytes) -> int:
        self._crc = crc24(data, self._crc)
        joined = self._pending + data
        usable = len(joined) - len(joined) % 3
        if usable:
            self._emit(base64.b64encode(joined[:usable]))
        self._pending = joined[usable:]
        return len(data)

    def _emit(self, encoded: bytes) -> None:
        text = self._line + encoded
        cut = len(text) - len(text) % 64
        if cut:
            for start in range(0, cut, 64):
                self._out.write(text[start:start + 64] + b"\n")
        self._line = text[cut:]

    def close(self) -> None:
        if self._pending:
            self._emit(base64.b64encode(self._pending))
            self._pending = b""
        if self._line:
            self._out.write(self._line + b"\n")
            self._line = b""
        crc = base64.b64encode((self._crc & 0xFFFFFF).to_bytes(3, "big"))
        self._out.write(b"=" + crc + b"\n")
        self._out.write(f"-----END PGP {self._kind}-----\n".encode("ascii"))
