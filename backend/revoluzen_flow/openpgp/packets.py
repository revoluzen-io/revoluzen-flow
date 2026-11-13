"""OpenPGP packets: reading headers and bodies from a stream, and writing them.

A PGP file is a sequence of packets. Each starts with a tag and a length. A
body whose length is not known in advance is sent as "partial" chunks, so
everything here works on streams and never needs a whole file in memory.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from typing import BinaryIO

from revoluzen_flow.openpgp.algorithms import PgpError

# Tags
PKESK = 1
SIGNATURE = 2
SKESK = 3
ONE_PASS_SIGNATURE = 4
SECRET_KEY = 5
PUBLIC_KEY = 6
SECRET_SUBKEY = 7
COMPRESSED = 8
SYMMETRIC_NO_MDC = 9
MARKER = 10
LITERAL = 11
TRUST = 12
USER_ID = 13
PUBLIC_SUBKEY = 14
USER_ATTRIBUTE = 17
SEIPD = 18
MDC = 19
AEAD_ENCRYPTED = 20
PADDING = 21


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


class BodyReader(io.RawIOBase):
    """The body of one packet, read as a stream."""

    def __init__(self, stream: BinaryIO, length: int | None, partial: bool):
        self._stream = stream
        self._remaining = length  # None = to the end of the stream
        self._partial = partial
        self._done = False

    def readable(self) -> bool:
        return True

    def _next_chunk_length(self) -> None:
        first = self._stream.read(1)
        if not first:
            raise PgpError("The PGP data is cut short (a packet ends early).")
        octet = first[0]
        if octet < 192:
            self._remaining, self._partial = octet, False
        elif octet < 224:
            second = _read_exact(self._stream, 1)
            self._remaining, self._partial = ((octet - 192) << 8) + second[0] + 192, False
        elif octet == 255:
            self._remaining, self._partial = int.from_bytes(_read_exact(self._stream, 4), "big"), False
        else:
            self._remaining, self._partial = 1 << (octet & 0x1F), True

    def readinto(self, buffer) -> int:
        if self._done:
            return 0
        view = memoryview(buffer)
        wanted = len(view)
        if wanted == 0:
            return 0
        while True:
            if self._remaining is None:
                data = self._stream.read(wanted)
                if not data:
                    self._done = True
                    return 0
                view[:len(data)] = data
                return len(data)
            if self._remaining > 0:
                data = self._stream.read(min(wanted, self._remaining))
                if not data:
                    raise PgpError("The PGP data is cut short (a packet ends early).")
                self._remaining -= len(data)
                view[:len(data)] = data
                return len(data)
            if not self._partial:
                self._done = True
                return 0
            self._next_chunk_length()

    def read_all(self) -> bytes:
        return self.read()

    def read_exact(self, size: int) -> bytes:
        """Exactly ``size`` octets (fewer only at the end of the body). A
        plain ``read`` can stop short where one partial chunk ends."""
        return _read_exact(self, size)

    def drain(self) -> None:
        while self.read(1 << 16):
            pass


def read_packet(stream: BinaryIO) -> tuple[int, BodyReader] | None:
    """The next packet's tag and body, or ``None`` at the end of the stream."""
    first = stream.read(1)
    if not first:
        return None
    octet = first[0]
    if not octet & 0x80:
        raise PgpError("This is not PGP data (a packet header is missing). Is the file really PGP-encrypted?")
    if octet & 0x40:
        tag = octet & 0x3F
        length_octet = _read_exact(stream, 1)
        if not length_octet:
            raise PgpError("The PGP data is cut short.")
        value = length_octet[0]
        if value < 192:
            return tag, BodyReader(stream, value, False)
        if value < 224:
            second = _read_exact(stream, 1)
            return tag, BodyReader(stream, ((value - 192) << 8) + second[0] + 192, False)
        if value == 255:
            return tag, BodyReader(stream, int.from_bytes(_read_exact(stream, 4), "big"), False)
        return tag, BodyReader(stream, 1 << (value & 0x1F), True)
    tag = (octet >> 2) & 0x0F
    length_type = octet & 0x03
    if length_type == 3:
        return tag, BodyReader(stream, None, False)
    size = (1, 2, 4)[length_type]
    return tag, BodyReader(stream, int.from_bytes(_read_exact(stream, size), "big"), False)


def iter_packets(data: bytes):
    """Every ``(tag, body_bytes)`` in a small blob such as a key."""
    stream = io.BytesIO(data)
    while True:
        packet = read_packet(stream)
        if packet is None:
            return
        tag, body = packet
        yield tag, body.read_all()


# ── Fields inside bodies ──

def read_mpi(data: bytes, position: int) -> tuple[int, int, bytes]:
    """``(value, next_position, raw_bytes)`` for the multiprecision integer at ``position``."""
    if position + 2 > len(data):
        raise PgpError("A key or message is cut short (a number is missing).")
    bits = int.from_bytes(data[position:position + 2], "big")
    size = (bits + 7) // 8
    raw = data[position + 2:position + 2 + size]
    if len(raw) < size:
        raise PgpError("A key or message is cut short (a number is truncated).")
    return int.from_bytes(raw, "big"), position + 2 + size, raw


def mpi(value: int | bytes) -> bytes:
    if isinstance(value, bytes):
        value = int.from_bytes(value, "big")
    bits = value.bit_length()
    return bits.to_bytes(2, "big") + value.to_bytes((bits + 7) // 8, "big")


def new_header(tag: int, length: int) -> bytes:
    if length < 192:
        size = bytes([length])
    elif length < 8384:
        length -= 192
        size = bytes([(length >> 8) + 192, length & 0xFF])
    else:
        size = b"\xff" + length.to_bytes(4, "big")
    return bytes([0xC0 | tag]) + size


def packet(tag: int, body: bytes) -> bytes:
    return new_header(tag, len(body)) + body


class PartialWriter:
    """Writes one packet whose length is not known in advance, in 64 KiB
    partial chunks (the first chunk must be at least 512 octets)."""

    CHUNK = 1 << 16
    _CHUNK_OCTET = 0xE0 | 16

    def __init__(self, out, tag: int):
        self._out = out
        self._buffer = bytearray()
        out.write(bytes([0xC0 | tag]))

    def write(self, data: bytes) -> int:
        self._buffer += data
        while len(self._buffer) > self.CHUNK:
            self._out.write(bytes([self._CHUNK_OCTET]))
            self._out.write(bytes(self._buffer[:self.CHUNK]))
            del self._buffer[:self.CHUNK]
        return len(data)

    def close(self) -> None:
        final = bytes(self._buffer)
        length = len(final)
        if length < 192:
            self._out.write(bytes([length]))
        elif length < 8384:
            value = length - 192
            self._out.write(bytes([(value >> 8) + 192, value & 0xFF]))
        else:
            self._out.write(b"\xff" + length.to_bytes(4, "big"))
        self._out.write(final)
        self._buffer = bytearray()


def read_subpackets(data: bytes) -> list[tuple[int, bytes]]:
    """Signature subpackets as ``(type, data)``."""
    out = []
    position = 0
    while position < len(data):
        first = data[position]
        if first < 192:
            length, position = first, position + 1
        elif first < 255:
            length = ((first - 192) << 8) + data[position + 1] + 192
            position += 2
        else:
            length = int.from_bytes(data[position + 1:position + 5], "big")
            position += 5
        if length == 0 or position + length > len(data):
            break
        kind = data[position] & 0x7F
        out.append((kind, data[position + 1:position + length]))
        position += length
    return out


# ── Signature packets ──

_MALFORMED_SIGNATURE = "A signature in the file is malformed."


def read_subpackets_strict(data: bytes) -> list[tuple[int, bool, bytes]]:
    """Signature subpackets as ``(type, critical, data)``. Unlike
    ``read_subpackets`` a damaged area is an error, because a signature
    that is checked must be read exactly."""
    out = []
    position = 0
    while position < len(data):
        first = data[position]
        if first < 192:
            length, position = first, position + 1
        elif first < 255:
            if position + 1 >= len(data):
                raise PgpError(_MALFORMED_SIGNATURE)
            length = ((first - 192) << 8) + data[position + 1] + 192
            position += 2
        else:
            if position + 5 > len(data):
                raise PgpError(_MALFORMED_SIGNATURE)
            length = int.from_bytes(data[position + 1:position + 5], "big")
            position += 5
        if length == 0 or position + length > len(data):
            raise PgpError(_MALFORMED_SIGNATURE)
        kind = data[position]
        out.append((kind & 0x7F, bool(kind & 0x80), data[position + 1:position + length]))
        position += length
    return out


# Subpacket types (RFC 9580 section 5.2.3.7)
SUB_CREATED, SUB_EXPIRES, SUB_KEY_EXPIRES, SUB_ISSUER, SUB_KEY_FLAGS = 2, 3, 9, 16, 27
SUB_REVOCATION_REASON, SUB_FEATURES, SUB_EMBEDDED, SUB_ISSUER_FINGERPRINT = 29, 30, 32, 33
# Types this reader understands; an unknown one marked critical makes the
# signature unusable (RFC 9580 section 5.2.3.7).
KNOWN_SUBPACKETS = frozenset({
    2, 3, 4, 5, 6, 7, 9, 11, 12, 16, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32, 33, 34, 35,
    37, 38, 39,
})


@dataclass
class Signature:
    """A version 4 or 6 Signature packet, read but not checked."""
    version: int
    sig_type: int
    algorithm: int
    hash_algorithm: int
    hashed_part: bytes   # from the version octet to the end of the hashed subpackets
    hashed: list[tuple[int, bool, bytes]]
    unhashed: list[tuple[int, bool, bytes]]
    left16: bytes
    salt: bytes          # version 6 only
    material: bytes      # the algorithm's signature fields
    body: bytes

    def _first(self, kind: int, *, hashed_only: bool = True) -> bytes | None:
        areas = self.hashed if hashed_only else self.hashed + self.unhashed
        for found, _, data in areas:
            if found == kind:
                return data
        return None

    @property
    def created(self) -> int | None:
        data = self._first(SUB_CREATED)
        return int.from_bytes(data, "big") if data is not None and len(data) == 4 else None

    @property
    def expires_after(self) -> int | None:
        """Seconds after creation when the signature stops being valid (None: never)."""
        data = self._first(SUB_EXPIRES)
        value = int.from_bytes(data, "big") if data is not None and len(data) == 4 else 0
        return value or None

    @property
    def key_expires_after(self) -> int | None:
        """For self-signatures: seconds after the key's creation when it expires (None: never)."""
        data = self._first(SUB_KEY_EXPIRES)
        value = int.from_bytes(data, "big") if data is not None and len(data) == 4 else 0
        return value or None

    @property
    def key_flags(self) -> int | None:
        data = self._first(SUB_KEY_FLAGS)
        return data[0] if data else None

    @property
    def features(self) -> int | None:
        data = self._first(SUB_FEATURES)
        return data[0] if data else None

    @property
    def revocation_reason(self) -> int | None:
        data = self._first(SUB_REVOCATION_REASON)
        return data[0] if data else None

    @property
    def issuer_key_ids(self) -> set[bytes]:
        ids = {data for kind, _, data in self.hashed + self.unhashed if kind == SUB_ISSUER and len(data) == 8}
        for fingerprint in self.issuer_fingerprints:
            ids.add(fingerprint[:8] if len(fingerprint) == 32 else fingerprint[-8:])
        return ids

    @property
    def issuer_fingerprints(self) -> set[bytes]:
        return {
            data[1:] for kind, _, data in self.hashed + self.unhashed
            if kind == SUB_ISSUER_FINGERPRINT and len(data) in (21, 33) and data[0] in (4, 6)
        }

    @property
    def embedded(self) -> list[bytes]:
        return [data for kind, _, data in self.hashed + self.unhashed if kind == SUB_EMBEDDED]

    @property
    def unknown_critical(self) -> list[int]:
        return [kind for kind, critical, _ in self.hashed if critical and kind not in KNOWN_SUBPACKETS]

    def trailer(self) -> bytes:
        """What is hashed after the signed data (RFC 9580 section 5.2.4)."""
        return self.hashed_part + bytes([self.version, 0xFF]) + (len(self.hashed_part) & 0xFFFFFFFF).to_bytes(4, "big")


def parse_signature(body: bytes) -> Signature:
    if not body:
        raise PgpError(_MALFORMED_SIGNATURE)
    version = body[0]
    if version == 3:
        raise PgpError("The file carries an old version 3 signature, which is not supported. "
                       "Ask the sender to use a current PGP tool.")
    if version not in (4, 6):
        raise PgpError(f"The file carries a version {version} signature, which is not supported.")
    size = 2 if version == 4 else 4
    if len(body) < 4 + size:
        raise PgpError(_MALFORMED_SIGNATURE)
    hashed_length = int.from_bytes(body[4:4 + size], "big")
    hashed_end = 4 + size + hashed_length
    if hashed_end + size > len(body):
        raise PgpError(_MALFORMED_SIGNATURE)
    unhashed_length = int.from_bytes(body[hashed_end:hashed_end + size], "big")
    unhashed_start = hashed_end + size
    unhashed_end = unhashed_start + unhashed_length
    if unhashed_end + 2 > len(body):
        raise PgpError(_MALFORMED_SIGNATURE)
    position = unhashed_end + 2
    salt = b""
    if version == 6:
        if position >= len(body):
            raise PgpError(_MALFORMED_SIGNATURE)
        salt_length = body[position]
        salt = body[position + 1:position + 1 + salt_length]
        if len(salt) != salt_length:
            raise PgpError(_MALFORMED_SIGNATURE)
        position += 1 + salt_length
    return Signature(
        version=version, sig_type=body[1], algorithm=body[2], hash_algorithm=body[3],
        hashed_part=body[:hashed_end],
        hashed=read_subpackets_strict(body[4 + size:hashed_end]),
        unhashed=read_subpackets_strict(body[unhashed_start:unhashed_end]),
        left16=body[unhashed_end:unhashed_end + 2], salt=salt, material=body[position:], body=body,
    )


@dataclass
class OnePass:
    """A One-Pass Signature packet: what the signature after the data will be."""
    version: int
    sig_type: int
    hash_algorithm: int
    algorithm: int
    key_id: bytes
    fingerprint: bytes
    salt: bytes
    last: bool


def parse_one_pass(body: bytes) -> OnePass:
    if len(body) < 4:
        raise PgpError(_MALFORMED_SIGNATURE)
    version, sig_type, hash_algorithm, algorithm = body[0], body[1], body[2], body[3]
    if version == 3:
        if len(body) != 13:
            raise PgpError(_MALFORMED_SIGNATURE)
        return OnePass(3, sig_type, hash_algorithm, algorithm, body[4:12], b"", b"", body[12] != 0)
    if version == 6:
        salt_length = body[4] if len(body) > 4 else 0
        salt = body[5:5 + salt_length]
        fingerprint = body[5 + salt_length:5 + salt_length + 32]
        if len(salt) != salt_length or len(fingerprint) != 32 or len(body) != 5 + salt_length + 33:
            raise PgpError(_MALFORMED_SIGNATURE)
        return OnePass(6, sig_type, hash_algorithm, algorithm, fingerprint[:8], fingerprint, salt, body[-1] != 0)
    raise PgpError(f"The file carries a version {version} one-pass signature, which is not supported.")
