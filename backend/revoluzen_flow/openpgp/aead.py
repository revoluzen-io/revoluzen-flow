"""Authenticated encryption (AEAD) for OpenPGP.

Two kinds of encrypted data use it:

* the OCB packet that GnuPG 2.3 and later write (packet type 20, version 1,
  from the LibrePGP drafts), and
* integrity-protected data version 2 from RFC 9580 (SEIPD v2), written by
  OpenPGP.js 6, Sequoia and others for keys that ask for it.

Both cut the data into chunks. Each chunk carries its own authentication
tag, so nothing is handed on before it has been checked, and a final tag at
the end proves that no chunk was removed or reordered and nothing was cut
off.

OCB and GCM come from the ``cryptography`` package. EAX is not in it, so it
is built here from AES-CTR and CMAC, which ``cryptography`` does provide,
exactly as the EAX paper (Bellare, Rogaway and Wagner) defines it.
"""

from __future__ import annotations

import hmac
import io
from typing import BinaryIO

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import cmac, hashes
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from revoluzen_flow.openpgp import algorithms as alg
from revoluzen_flow.openpgp.algorithms import PgpError

EAX, OCB, GCM = 1, 2, 3
AEAD_NAMES = {EAX: "EAX", OCB: "OCB", GCM: "GCM"}
NONCE_SIZES = {EAX: 16, OCB: 15, GCM: 12}
TAG_SIZE = 16
# Chunks of 2 ** (octet + 6) bytes. RFC 9580 asks readers to accept octets up
# to 16 (4 MiB chunks), which is also GnuPG's largest; bigger ones are
# refused so a damaged or hostile file cannot ask for a huge buffer.
MAX_CHUNK_OCTET = 16

_DAMAGED = "The file failed its integrity check: it was changed or damaged after it was encrypted."


class _Eax:
    """EAX mode on AES: CTR for secrecy, CMAC (OMAC) for the tag."""

    def __init__(self, key: bytes):
        self._key = key

    def _omac(self, tweak: int, data: bytes) -> bytes:
        mac = cmac.CMAC(algorithms.AES(self._key))
        mac.update(b"\x00" * 15 + bytes([tweak]) + data)
        return mac.finalize()

    def _ctr(self, counter: bytes, data: bytes) -> bytes:
        engine = Cipher(algorithms.AES(self._key), modes.CTR(counter)).encryptor()
        return engine.update(data) + engine.finalize()

    def encrypt(self, nonce: bytes, data: bytes, associated: bytes | None) -> bytes:
        start = self._omac(0, nonce)
        header = self._omac(1, associated or b"")
        ciphertext = self._ctr(start, data)
        body = self._omac(2, ciphertext)
        return ciphertext + bytes(a ^ b ^ c for a, b, c in zip(start, body, header))

    def decrypt(self, nonce: bytes, data: bytes, associated: bytes | None) -> bytes:
        if len(data) < TAG_SIZE:
            raise InvalidTag()
        ciphertext, tag = data[:-TAG_SIZE], data[-TAG_SIZE:]
        start = self._omac(0, nonce)
        header = self._omac(1, associated or b"")
        body = self._omac(2, ciphertext)
        expected = bytes(a ^ b ^ c for a, b, c in zip(start, body, header))
        if not hmac.compare_digest(expected, tag):
            raise InvalidTag()
        return self._ctr(start, ciphertext)


def nonce_size(aead_algorithm: int) -> int:
    if aead_algorithm not in NONCE_SIZES:
        raise PgpError(f"The file uses AEAD mode {aead_algorithm}, which is not known.")
    return NONCE_SIZES[aead_algorithm]


def engine(aead_algorithm: int, cipher_algorithm: int, key: bytes):
    """An object with ``encrypt(nonce, data, ad)`` and ``decrypt(nonce, data, ad)``."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM, AESOCB3

    if cipher_algorithm not in (alg.AES128, alg.AES192, alg.AES256):
        raise PgpError(
            f"The file uses {alg.SYMMETRIC_NAMES.get(cipher_algorithm, cipher_algorithm)} with "
            f"{AEAD_NAMES.get(aead_algorithm, aead_algorithm)}; only AES is supported with AEAD."
        )
    if len(key) != alg.key_size(cipher_algorithm):
        raise PgpError("The session key could not be opened with this key.")
    if aead_algorithm == OCB:
        return AESOCB3(key)
    if aead_algorithm == GCM:
        return AESGCM(key)
    if aead_algorithm == EAX:
        return _Eax(key)
    raise PgpError(f"The file uses AEAD mode {aead_algorithm}, which is not known.")


def open_sealed(aead_algorithm: int, cipher_algorithm: int, key: bytes, nonce: bytes, data: bytes,
                associated: bytes, problem: str) -> bytes:
    """Decrypt one AEAD-sealed value (a session key or a secret key); ``problem``
    is the message when the tag does not match."""
    try:
        return engine(aead_algorithm, cipher_algorithm, key).decrypt(nonce, data, associated)
    except InvalidTag:
        raise PgpError(problem) from None


def hkdf_sha256(secret: bytes, info: bytes, size: int, salt: bytes | None = None) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=size, salt=salt, info=info).derive(secret)


class AeadReader(io.RawIOBase):
    """Decrypts the chunks of an AEAD container and checks the final tag.

    ``version`` is 1 for GnuPG's OCB packet (type 20) or 2 for SEIPD v2. The
    body is read just after its first octet (the version)."""

    def __init__(self, body: BinaryIO, version: int, session_key: bytes, header: bytes):
        self._body = body
        self._version = version
        cipher, aead_algorithm, chunk_octet = header[0], header[1], header[2]
        if chunk_octet > MAX_CHUNK_OCTET:
            raise PgpError("The file uses AEAD chunks larger than 4 MiB, which are not supported.")
        self._chunk = 1 << (chunk_octet + 6)
        size = nonce_size(aead_algorithm)
        if version == 1:
            # GnuPG / LibrePGP: the starting nonce is in the packet, and the
            # chunk index is XORed into its last eight octets.
            self._prefix = bytes([0xC0 | 20, 1, cipher, aead_algorithm, chunk_octet])
            self._iv = _read_exact(body, size)
            if len(self._iv) != size:
                raise PgpError("The encrypted data is cut short.")
            key = session_key
        else:
            # RFC 9580: a key and nonce prefix are derived from the session
            # key and a salt; the chunk index follows the prefix.
            self._prefix = bytes([0xC0 | 18, 2, cipher, aead_algorithm, chunk_octet])
            salt = _read_exact(body, 32)
            if len(salt) != 32:
                raise PgpError("The encrypted data is cut short.")
            key_length = alg.key_size(cipher)
            if len(session_key) != key_length:
                raise PgpError("The session key could not be opened with this key.")
            derived = hkdf_sha256(session_key, self._prefix, key_length + size - 8, salt=salt)
            key, self._iv = derived[:key_length], derived[key_length:]
        self._engine = engine(aead_algorithm, cipher, key)
        self._raw = bytearray()
        self._plain = bytearray()
        self._index = 0
        self._total = 0
        self._ended = False      # the body has no more data
        self._finished = False   # the final tag has been checked

    def readable(self) -> bool:
        return True

    def _nonce(self, index: int) -> bytes:
        counter = index.to_bytes(8, "big")
        if self._version == 1:
            head, tail = self._iv[:-8], self._iv[-8:]
            return head + bytes(a ^ b for a, b in zip(tail, counter))
        return self._iv + counter

    def _associated(self, index: int, final: bool) -> bytes:
        if self._version == 1:
            data = self._prefix + index.to_bytes(8, "big")
        else:
            data = self._prefix
        if final:
            data += self._total.to_bytes(8, "big")
        return data

    def _open(self, sealed: bytes) -> None:
        try:
            plain = self._engine.decrypt(self._nonce(self._index), sealed, self._associated(self._index, False))
        except InvalidTag:
            raise PgpError(_DAMAGED) from None
        self._index += 1
        self._total += len(plain)
        self._plain += plain

    def _fill(self, wanted: int) -> None:
        while len(self._raw) < wanted and not self._ended:
            piece = self._body.read(1 << 16)
            if not piece:
                self._ended = True
                break
            self._raw += piece

    def _advance(self) -> None:
        full = self._chunk + TAG_SIZE
        # More than a full chunk and the final tag means this chunk is not the last.
        self._fill(full + TAG_SIZE + 1)
        if len(self._raw) > full + TAG_SIZE:
            sealed = bytes(self._raw[:full])
            del self._raw[:full]
            self._open(sealed)
            return
        rest = bytes(self._raw)
        self._raw = bytearray()
        if len(rest) < TAG_SIZE or 0 < len(rest) - TAG_SIZE < TAG_SIZE:
            raise PgpError("The encrypted data is cut short; the file may be incomplete.")
        last, final_tag = rest[:-TAG_SIZE], rest[-TAG_SIZE:]
        if last:
            self._open(last)
        try:
            self._engine.decrypt(self._nonce(self._index), final_tag, self._associated(self._index, True))
        except InvalidTag:
            raise PgpError(
                "The file failed its final integrity check: it was cut short, or changed after it was encrypted."
            ) from None
        self._finished = True

    def readinto(self, target) -> int:
        view = memoryview(target)
        while not self._plain and not self._finished:
            self._advance()
        count = min(len(self._plain), len(view))
        if not count:
            return 0
        view[:count] = self._plain[:count]
        del self._plain[:count]
        return count


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


# ── Writing (used by the tests and by anyone sealing a value) ──

def seal(aead_algorithm: int, cipher_algorithm: int, key: bytes, nonce: bytes, data: bytes,
         associated: bytes) -> bytes:
    return engine(aead_algorithm, cipher_algorithm, key).encrypt(nonce, data, associated)
