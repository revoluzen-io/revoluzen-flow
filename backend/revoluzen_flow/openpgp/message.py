"""Decrypting an incoming PGP file and encrypting an outgoing one.

Decrypting reads: the session-key packets (public-key or passphrase), then
the encrypted data, then whatever is inside: compressed data, signatures,
and the file itself. Three kinds of encrypted data are read:

* integrity-protected data version 1 (SEIPD v1) with its MDC check, which
  every current tool writes: GnuPG, Kleopatra, Bouncy Castle-based
  file-transfer products, OpenPGP.js;
* the OCB packet GnuPG 2.3 and later write when the receiving key asks for
  AEAD (packet type 20);
* integrity-protected data version 2 (SEIPD v2, RFC 9580) with OCB, GCM or
  EAX, from OpenPGP.js 6, Sequoia and others.

A file that fails its integrity check is refused, and so are old files with
no integrity check at all.

When the sender's public key is given, the sender's signature is checked
too (``signatures.py``): the file is hashed as it is decrypted, and a file
that is unsigned, signed by someone else or changed after signing is
refused. A file that is signed but not encrypted is read the same way.

Encrypting writes one session-key packet per recipient, then SEIPD version 1
with AES-256, ZIP compression and the file, optionally signed with our key.

Nothing here logs or returns key material or passphrases.
"""

from __future__ import annotations

import bz2
import hashlib
import io
import os
import tempfile
import time
import zlib
from dataclasses import dataclass, field
from typing import Any, BinaryIO

from revoluzen_flow.openpgp import aead
from revoluzen_flow.openpgp import algorithms as alg
from revoluzen_flow.openpgp.algorithms import PgpError
from revoluzen_flow.openpgp.armor import ArmorWriter, dearmor_stream, is_armored
from revoluzen_flow.openpgp.keys import Key, KeyPacket, rsa_private_key, secret_key_usable, unlock
from revoluzen_flow.openpgp.packets import (
    AEAD_ENCRYPTED, COMPRESSED, LITERAL, MARKER, ONE_PASS_SIGNATURE, PADDING, PKESK,
    PUBLIC_KEY, SECRET_KEY, SEIPD, SIGNATURE, SKESK, SYMMETRIC_NO_MDC,
    OnePass, PartialWriter, Signature, mpi, packet, parse_one_pass, parse_signature, read_mpi, read_packet,
)
from revoluzen_flow.openpgp.signatures import SignatureCheck, Signer, check_signature, data_hasher

_COPY_CHUNK = 1 << 20
_COMPRESSION_NAMES = {0: "none", 1: "ZIP", 2: "ZLIB", 3: "BZip2"}


@dataclass
class DecryptResult:
    filename: str = ""
    key_id: str = ""
    cipher: str = ""
    integrity: str = ""            # how the encrypted data was checked: "MDC", "AEAD OCB", ...
    encrypted: bool = True         # False for a file that was only signed
    compression: str = "none"
    signed_by: list[str] = field(default_factory=list)   # key IDs named by the signatures
    signature: SignatureCheck | None = None              # set when the signature was checked and is good
    bytes_written: int = 0


# ── Streams used while decrypting ──

class _SeipdReader(io.RawIOBase):
    """Decrypts a version 1 SEIPD body and checks its MDC at the end."""

    _TAIL = 22  # the MDC packet: 0xD3 0x14 + SHA-1

    def __init__(self, body: BinaryIO, algorithm: int, key: bytes):
        self._body = body
        self._decryptor = alg.cfb(algorithm, key)
        self._sha1 = hashlib.sha1()
        self._buffer = bytearray()
        self._finished = False
        size = alg.block_size(algorithm)
        prefix = self._fill(size + 2)
        if len(prefix) < size + 2:
            raise PgpError("The encrypted data is cut short.")
        if prefix[size - 2:size] != prefix[size:size + 2]:
            raise PgpError("The key does not open this file (the session key is wrong or the file is damaged).")
        self._sha1.update(prefix)

    def readable(self) -> bool:
        return True

    def _fill(self, size: int) -> bytes:
        while len(self._buffer) < size:
            chunk = self._body.read(1 << 16)
            if not chunk:
                break
            self._buffer += self._decryptor.update(chunk)
        taken = bytes(self._buffer[:size])
        del self._buffer[:size]
        return taken

    def readinto(self, target) -> int:
        view = memoryview(target)
        while not self._finished and len(self._buffer) <= self._TAIL:
            chunk = self._body.read(1 << 16)
            if not chunk:
                self._buffer += self._decryptor.finalize()
                self._finish()
                break
            self._buffer += self._decryptor.update(chunk)
        available = len(self._buffer) if self._finished else len(self._buffer) - self._TAIL
        if available <= 0:
            return 0
        count = min(available, len(view))
        data = bytes(self._buffer[:count])
        del self._buffer[:count]
        self._sha1.update(data)
        view[:count] = data
        return count

    def _finish(self) -> None:
        if len(self._buffer) < self._TAIL:
            raise PgpError("The encrypted data has no integrity check at its end; the file may be cut short.")
        body, tail = self._buffer[:-self._TAIL], bytes(self._buffer[-self._TAIL:])
        if tail[:2] != b"\xd3\x14":
            raise PgpError("The encrypted data's integrity check is missing or damaged.")
        check = self._sha1.copy()
        check.update(bytes(body))
        check.update(tail[:2])
        if check.digest() != tail[2:]:
            raise PgpError(
                "The file failed its integrity check: it was changed or damaged after it was encrypted."
            )
        self._buffer = bytearray(body)
        self._finished = True


class _Decompressor(io.RawIOBase):
    def __init__(self, body: BinaryIO, algorithm: int):
        self._body = body
        self._algorithm = algorithm
        if algorithm == 1:
            self._engine = zlib.decompressobj(-15)
        elif algorithm == 2:
            self._engine = zlib.decompressobj()
        elif algorithm == 3:
            self._engine = bz2.BZ2Decompressor()
        elif algorithm == 0:
            self._engine = None
        else:
            raise PgpError(f"The file uses compression method {algorithm}, which is not supported.")
        self._buffer = bytearray()
        self._done = False

    def readable(self) -> bool:
        return True

    def readinto(self, target) -> int:
        view = memoryview(target)
        while not self._buffer and not self._done:
            chunk = self._body.read(1 << 16)
            if not chunk:
                if self._engine is not None and hasattr(self._engine, "flush"):
                    self._buffer += self._engine.flush()
                self._done = True
                break
            if self._engine is None:
                self._buffer += chunk
            else:
                try:
                    if isinstance(self._engine, bz2.BZ2Decompressor) and self._engine.eof:
                        continue
                    self._buffer += self._engine.decompress(chunk)
                except (zlib.error, OSError, EOFError):
                    raise PgpError("The compressed data inside the file is damaged.") from None
        count = min(len(self._buffer), len(view))
        view[:count] = self._buffer[:count]
        del self._buffer[:count]
        return count


def _buffered(raw: io.RawIOBase) -> io.BufferedReader:
    return io.BufferedReader(raw, buffer_size=1 << 16)


# ── Session keys ──

_NOT_OPENED = "The session key could not be opened with this key."


def _payload_session(payload: bytes, with_algorithm: bool) -> tuple[int | None, bytes]:
    """The session key from a decrypted payload: (cipher, key) for version 3
    packets, (None, key) for version 6 ones, which carry no cipher here."""
    if len(payload) < 3 + with_algorithm:
        raise PgpError(_NOT_OPENED)
    algorithm = payload[0] if with_algorithm else None
    key, check = payload[int(with_algorithm):-2], payload[-2:]
    if algorithm is not None:
        try:
            if len(key) != alg.key_size(algorithm):
                raise PgpError(_NOT_OPENED)
        except PgpError:
            raise PgpError(_NOT_OPENED) from None
    elif len(key) not in (16, 24, 32):
        raise PgpError(_NOT_OPENED)
    if alg.checksum(key) != check:
        raise PgpError(_NOT_OPENED)
    return algorithm, key


def _eme_pkcs1_decode(message: bytes) -> bytes:
    if len(message) < 11 or message[0] != 0 or message[1] != 2:
        raise PgpError(_NOT_OPENED)
    separator = message.find(b"\x00", 2)
    if separator < 10:
        raise PgpError(_NOT_OPENED)
    return message[separator + 1:]


def _eme_pkcs1_encode(message: bytes, size: int) -> bytes:
    padding_length = size - len(message) - 3
    if padding_length < 8:
        raise PgpError("The recipient's key is too small to encrypt to.")
    padding = bytearray()
    while len(padding) < padding_length:
        padding += bytes(b for b in os.urandom(padding_length) if b != 0)
    return b"\x00\x02" + bytes(padding[:padding_length]) + b"\x00" + message


def _x25519_from_legacy_secret(secret: dict[str, Any]):
    from cryptography.hazmat.primitives.asymmetric import x25519

    # GnuPG keeps the Curve25519 secret as a big-endian number of the reversed key.
    raw = secret["secret_raw"].rjust(32, b"\x00")[-32:]
    return x25519.X25519PrivateKey.from_private_bytes(raw[::-1])


def _native_kem(packet: KeyPacket, secret: dict[str, Any], data: bytes, v3: bool) -> tuple[int | None, bytes]:
    """X25519 and X448 (RFC 9580 sections 5.1.6 and 5.1.7)."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import x448, x25519
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    from cryptography.hazmat.primitives.keywrap import InvalidUnwrap, aes_key_unwrap

    if packet.algorithm == alg.X25519:
        size, curve, hash_object, info, kek_size = 32, x25519, hashes.SHA256(), b"OpenPGP X25519", 16
        private = x25519.X25519PrivateKey.from_private_bytes(secret["raw"])
        public_class = x25519.X25519PublicKey
    else:
        size, curve, hash_object, info, kek_size = 56, x448, hashes.SHA512(), b"OpenPGP X448", 32
        private = x448.X448PrivateKey.from_private_bytes(secret["raw"])
        public_class = x448.X448PublicKey
    del curve
    if len(data) < size + 2:
        raise PgpError(_NOT_OPENED)
    ephemeral = data[:size]
    length = data[size]
    rest = data[size + 1:size + 1 + length]
    if len(rest) != length:
        raise PgpError(_NOT_OPENED)
    session_algorithm = None
    if v3:
        session_algorithm, rest = rest[0], rest[1:]
    try:
        shared = private.exchange(public_class.from_public_bytes(ephemeral))
    except ValueError:
        raise PgpError(_NOT_OPENED) from None
    kek = HKDF(algorithm=hash_object, length=kek_size, salt=None, info=info).derive(
        ephemeral + packet.fields["raw"] + shared
    )
    try:
        key = aes_key_unwrap(kek, rest)
    except (InvalidUnwrap, ValueError):
        raise PgpError(_NOT_OPENED) from None
    if session_algorithm is not None and alg.key_size(session_algorithm) != len(key):
        raise PgpError(_NOT_OPENED)
    return session_algorithm, key


def _open_pkesk(packet: KeyPacket, secret: dict[str, Any], data: bytes, v3: bool) -> tuple[int | None, bytes]:
    from cryptography.hazmat.primitives.keywrap import InvalidUnwrap, aes_key_unwrap

    algorithm = packet.algorithm
    if algorithm in (alg.RSA, alg.RSA_ENCRYPT_ONLY):
        from cryptography.hazmat.primitives.asymmetric import padding

        value, _, _ = read_mpi(data, 0)
        size = (packet.fields["n"].bit_length() + 7) // 8
        try:
            payload = rsa_private_key(packet, secret).decrypt(value.to_bytes(size, "big"), padding.PKCS1v15())
        except (ValueError, OverflowError):
            raise PgpError(_NOT_OPENED) from None
        return _payload_session(payload, v3)
    if algorithm == alg.ELGAMAL:
        c1, position, _ = read_mpi(data, 0)
        c2, _, _ = read_mpi(data, position)
        p = packet.fields["p"]
        value = (c2 * pow(c1, p - 1 - secret["x"], p)) % p
        size = (p.bit_length() + 7) // 8
        return _payload_session(_eme_pkcs1_decode(value.to_bytes(size, "big")), v3)
    if algorithm == alg.ECDH:
        _, position, ephemeral = read_mpi(data, 0)
        if position >= len(data):
            raise PgpError(_NOT_OPENED)
        wrapped_length = data[position]
        wrapped = data[position + 1:position + 1 + wrapped_length]
        curve = packet.fields["curve"]
        if curve.oid == alg.CURVE25519_LEGACY.oid:
            from cryptography.hazmat.primitives.asymmetric import x25519

            if len(ephemeral) != 33 or ephemeral[0] != 0x40:
                raise PgpError("The file's Curve25519 key exchange is malformed.")
            shared = _x25519_from_legacy_secret(secret).exchange(
                x25519.X25519PublicKey.from_public_bytes(ephemeral[1:])
            )
        else:
            from cryptography.hazmat.primitives.asymmetric import ec

            curve_object = alg.ec_curve(curve)
            private = ec.derive_private_key(secret["secret"], curve_object)
            try:
                peer = ec.EllipticCurvePublicKey.from_encoded_point(curve_object, ephemeral)
            except ValueError:
                raise PgpError("The file's elliptic-curve key exchange is malformed.") from None
            shared = private.exchange(ec.ECDH(), peer)
        kek = alg.ecdh_kek(shared, curve, packet.fields["kdf_hash"], packet.fields["kdf_cipher"],
                           packet.fingerprint)
        try:
            padded = aes_key_unwrap(kek, wrapped)
        except (InvalidUnwrap, ValueError):
            raise PgpError(_NOT_OPENED) from None
        pad = padded[-1] if padded else 0
        if not 1 <= pad <= 8 or padded[-pad:] != bytes([pad]) * pad:
            raise PgpError(_NOT_OPENED)
        return _payload_session(padded[:-pad], v3)
    if algorithm in (alg.X25519, alg.X448):
        return _native_kem(packet, secret, data, v3)
    raise PgpError(f"Files encrypted with {alg.PUBLIC_KEY_NAMES.get(algorithm, algorithm)} cannot be decrypted.")


@dataclass
class _Recipient:
    """One public-key session-key packet, read."""
    algorithm: int
    fields: bytes
    key_id: bytes | None = None        # version 3; all zeros = anonymous
    fingerprint: bytes | None = None   # version 6; None = anonymous

    def named(self) -> str:
        if self.fingerprint:
            return f"fingerprint {self.fingerprint.hex().upper()}"
        return f"key ID {(self.key_id or b'').hex().upper()}"

    def anonymous(self) -> bool:
        return self.fingerprint is None and (self.key_id is None or self.key_id == b"\x00" * 8)

    def matches(self, candidate: KeyPacket) -> bool:
        if self.anonymous():
            return True
        if self.fingerprint is not None:
            return candidate.fingerprint == self.fingerprint
        return candidate.key_id == self.key_id


def _read_pkesk(body: bytes, version: int) -> _Recipient:
    if version == 3:
        if len(body) < 10:
            raise PgpError("A session-key packet in the file is cut short.")
        return _Recipient(body[9], body[10:], key_id=body[1:9])
    if len(body) < 3:
        raise PgpError("A session-key packet in the file is cut short.")
    size = body[1]
    if size == 0:
        return _Recipient(body[2], body[3:])
    if len(body) < 3 + size:
        raise PgpError("A session-key packet in the file is cut short.")
    return _Recipient(body[2 + size], body[3 + size:], fingerprint=body[3:2 + size])


def _open_skesk(body: bytes, passphrase: bytes) -> tuple[int | None, bytes]:
    """The session key from a passphrase packet (versions 4, 5 and 6)."""
    version = body[0]
    if version == 4:
        algorithm = body[1]
        s2k, position = alg.read_s2k(body, 2)
        key = alg.derive_key(s2k, passphrase, alg.key_size(algorithm))
        encrypted = body[position:]
        if not encrypted:
            return algorithm, key
        decryptor = alg.cfb(algorithm, key)
        plain = decryptor.update(encrypted) + decryptor.finalize()
        try:
            if len(plain) - 1 != alg.key_size(plain[0]):
                raise PgpError("The passphrase does not open this file.")
        except PgpError:
            raise PgpError("The passphrase does not open this file.") from None
        return plain[0], plain[1:]
    if version == 5:
        # GnuPG / LibrePGP: the passphrase key unwraps the session key directly.
        cipher, aead_algorithm = body[1], body[2]
        s2k, position = alg.read_s2k(body, 3)
        kek = alg.derive_key(s2k, passphrase, alg.key_size(cipher))
        info = bytes([0xC3, 5, cipher, aead_algorithm])
    else:
        # RFC 9580: lengths are given, and the key goes through HKDF first.
        if len(body) < 5:
            raise PgpError("A passphrase packet in the file is cut short.")
        cipher, aead_algorithm, s2k_length = body[2], body[3], body[4]
        s2k, position = alg.read_s2k(body, 5)
        if position != 5 + s2k_length:
            raise PgpError("A passphrase packet in the file is malformed.")
        info = bytes([0xC3, 6, cipher, aead_algorithm])
        kek = aead.hkdf_sha256(alg.derive_key(s2k, passphrase, alg.key_size(cipher)), info, alg.key_size(cipher))
    size = aead.nonce_size(aead_algorithm)
    nonce = body[position:position + size]
    sealed = body[position + size:]
    if len(nonce) != size or len(sealed) <= aead.TAG_SIZE:
        raise PgpError("A passphrase packet in the file is cut short.")
    if version == 6 and body[1] != position + size - 2:
        raise PgpError("A passphrase packet in the file is malformed.")
    key = aead.open_sealed(aead_algorithm, cipher, kek, nonce, sealed, info, "The passphrase does not open this file.")
    return None, key


def _session_key(pkesks: list[bytes], skesks: list[bytes], keys: list[Key],
                 passphrase: str | bytes | None, result: DecryptResult, container: str) -> tuple[int | None, bytes]:
    """The session key, from the first packet that opens with the given key
    or passphrase. ``container`` is "seipd1", "aead1" (GnuPG's OCB packet)
    or "seipd2"; RFC 9580 pairs version 6 session-key packets with SEIPD v2
    only, and versions 3, 4 and 5 with the others."""
    secrets = [p for key in keys for p in key.all_packets if p.secret_body is not None and secret_key_usable(p)]
    wanted: list[str] = []
    unlocked: dict[bytes, dict[str, Any]] = {}
    last_problem: PgpError | None = None
    pk_version = 6 if container == "seipd2" else 3
    for body in pkesks:
        if not body:
            continue
        if body[0] != pk_version:
            if body[0] not in (3, 6):
                last_problem = PgpError(f"The file has a version {body[0]} session-key packet, which is not supported.")
            continue
        recipient = _read_pkesk(body, pk_version)
        wanted.append(recipient.named())
        for candidate in secrets:
            if not recipient.matches(candidate):
                continue
            if candidate.algorithm != recipient.algorithm and not (
                {candidate.algorithm, recipient.algorithm} <= {alg.RSA, alg.RSA_ENCRYPT_ONLY}
            ):
                continue
            if candidate.key_id not in unlocked:
                unlocked[candidate.key_id] = unlock(candidate, passphrase)
            try:
                session = _open_pkesk(candidate, unlocked[candidate.key_id], recipient.fields, pk_version == 3)
            except PgpError as problem:
                last_problem = problem
                continue
            result.key_id = candidate.key_id.hex().upper()
            return session
    secret = passphrase.encode("utf-8") if isinstance(passphrase, str) else passphrase
    sk_versions = (6,) if container == "seipd2" else (4, 5)
    for body in skesks:
        if not body or body[0] not in sk_versions:
            continue
        if not secret:
            last_problem = PgpError("The file is encrypted with a passphrase. Add it to the credential.")
            continue
        try:
            session = _open_skesk(body, secret)
        except PgpError as problem:
            last_problem = problem
            continue
        result.key_id = "passphrase"
        return session
    if wanted and not any(name in wanted for name in _names(secrets)) and not _any_anonymous(pkesks, pk_version):
        have = ", ".join(sorted({p.key_id.hex().upper() for p in secrets})) or "none"
        raise PgpError(
            f"This file was encrypted for {', '.join(wanted)}, but the credential holds "
            f"private key ID {have}. Choose the credential with the matching private key."
        )
    if last_problem is not None:
        raise last_problem
    raise PgpError("The file has no session key that this credential can open.")


def _names(packets: list[KeyPacket]) -> set[str]:
    names = set()
    for p in packets:
        names.add(f"key ID {p.key_id.hex().upper()}")
        names.add(f"fingerprint {p.fingerprint.hex().upper()}")
    return names


def _any_anonymous(pkesks: list[bytes], version: int) -> bool:
    for body in pkesks:
        if body and body[0] == version:
            try:
                if _read_pkesk(body, version).anonymous():
                    return True
            except PgpError:
                continue
    return False


# ── What is inside: the file and its signatures ──

class _Signatures:
    """The signatures found while unpacking, and the hashes of the file they
    need. Hashes are only kept when the signatures are to be checked."""

    def __init__(self, checking: bool):
        self.checking = checking
        self.pending: list[tuple[OnePass | None, Any]] = []      # one-pass packets, in order
        self.before: list[tuple[Signature, Any]] = []            # signatures that came before the file
        self.done: list[tuple[Signature, bytes]] = []            # (signature, digest) ready to check
        self.problems: list[str] = []
        self.seen_file = False

    def hashers(self) -> list[Any]:
        return [h for _, h in self.pending if h is not None] + [h for _, h in self.before]

    def one_pass(self, body: bytes) -> None:
        if not self.checking:
            return
        try:
            ops = parse_one_pass(body)
            self.pending.append((ops, data_hasher(ops.hash_algorithm, ops.sig_type, ops.salt)))
        except PgpError as exc:
            self.problems.append(str(exc))
            self.pending.append((None, None))

    def signature(self, body: bytes) -> None:
        if not self.checking:
            return
        try:
            signature = parse_signature(body)
        except PgpError as exc:
            self.problems.append(str(exc))
            if self.seen_file and self.pending:
                self.pending.pop()
            return
        if not self.seen_file:
            try:
                self.before.append((signature, data_hasher(signature.hash_algorithm, signature.sig_type,
                                                           signature.salt)))
            except PgpError as exc:
                self.problems.append(str(exc))
            return
        if not self.pending:
            return  # a signature with no one-pass packet before the file: not over this file
        ops, hasher = self.pending.pop()
        if ops is None or hasher is None:
            return
        if (ops.sig_type, ops.hash_algorithm, ops.algorithm, ops.salt) != (
            signature.sig_type, signature.hash_algorithm, signature.algorithm, signature.salt,
        ) or (signature.issuer_key_ids and ops.key_id not in signature.issuer_key_ids):
            self.problems.append("a signature does not match the one-pass signature packet before the file")
            return
        self.done.append((signature, hasher.finish(signature.trailer())))

    def finish(self) -> None:
        for signature, hasher in self.before:
            self.done.append((signature, hasher.finish(signature.trailer())))
        self.before = []
        if any(ops is not None for ops, _ in self.pending):
            self.problems.append("the signature at the end of the file is missing")


def _signer(tag: int, body: bytes) -> str | None:
    try:
        if tag == ONE_PASS_SIGNATURE:
            return parse_one_pass(body).key_id.hex().upper()
        ids = sorted(parse_signature(body).issuer_key_ids)
        return ids[0].hex().upper() if ids else None
    except PgpError:
        return None


def _unpack(stream: BinaryIO, out: BinaryIO, result: DecryptResult, signatures: _Signatures,
            depth: int = 0, first: tuple[int, Any] | None = None) -> bool:
    if depth > 8:
        raise PgpError("The file nests compressed data too deeply.")
    found = False
    item = first if first is not None else read_packet(stream)
    while item is not None:
        tag, body = item
        if tag == COMPRESSED:
            head = body.read(1)
            if not head:
                raise PgpError("The compressed data inside the file is empty.")
            result.compression = _COMPRESSION_NAMES.get(head[0], str(head[0]))
            inner = _buffered(_Decompressor(body, head[0]))
            found = _unpack(inner, out, result, signatures, depth + 1) or found
            body.drain()
        elif tag == LITERAL:
            if found:
                raise PgpError("The file holds more than one file inside; only one is supported.")
            header = body.read_exact(2)
            if len(header) < 2:
                raise PgpError("The file's content header is cut short.")
            name = body.read_exact(header[1])
            body.read_exact(4)  # date
            result.filename = name.decode("utf-8", "replace")
            hashers = signatures.hashers()
            signatures.seen_file = True
            while True:
                chunk = body.read(_COPY_CHUNK)
                if not chunk:
                    break
                out.write(chunk)
                for hasher in hashers:
                    hasher.update(chunk)
                result.bytes_written += len(chunk)
            found = True
        elif tag in (ONE_PASS_SIGNATURE, SIGNATURE):
            data = body.read_all()
            signer = _signer(tag, data)
            if signer and signer not in result.signed_by:
                result.signed_by.append(signer)
            if tag == ONE_PASS_SIGNATURE:
                signatures.one_pass(data)
            else:
                signatures.signature(data)
        elif tag in (MARKER, PADDING):
            body.drain()
        else:
            raise PgpError(f"The decrypted data holds an unexpected part (packet type {tag}).")
        item = read_packet(stream)
    return found


def _check_signatures(signatures: _Signatures, verify_keys: list[Key]) -> SignatureCheck:
    """The sender's good signature, or ``PgpError`` saying why there is none."""
    sender_ids = {p.key_id for key in verify_keys for p in key.all_packets}
    good: SignatureCheck | None = None
    first_problem: PgpError | None = None
    for signature, digest in signatures.done:
        try:
            check = check_signature(signature, digest, verify_keys)
        except PgpError as exc:
            if signature.issuer_key_ids & sender_ids:
                raise  # the sender's own key made a signature that does not hold up
            first_problem = first_problem or exc
            continue
        good = good or check
    if good is not None:
        return good
    if first_problem is not None:
        raise first_problem
    if signatures.problems:
        raise PgpError(f"The file's signature cannot be checked: {signatures.problems[0]}.")
    raise PgpError(
        "The file is not signed, but this step requires the sender's signature. Ask the sender to sign it, "
        "or turn off 'Check the sender's signature'."
    )


def decrypt_stream(source: BinaryIO, out: BinaryIO, keys: list[Key], passphrase: str | bytes | None = None,
                   *, verify_keys: list[Key] | None = None) -> DecryptResult:
    """Decrypt ``source`` into ``out``. With ``verify_keys`` (the sender's
    public keys) the sender's signature must be present and good."""
    result = DecryptResult()
    signatures = _Signatures(checking=verify_keys is not None)
    pkesks: list[bytes] = []
    skesks: list[bytes] = []
    while True:
        item = read_packet(source)
        if item is None:
            raise PgpError("The file has no encrypted data in it.")
        tag, body = item
        if tag == PKESK:
            pkesks.append(body.read_all())
        elif tag == SKESK:
            skesks.append(body.read_all())
        elif tag in (MARKER, PADDING):
            body.drain()
        elif tag in (SEIPD, AEAD_ENCRYPTED):
            break
        elif tag == SYMMETRIC_NO_MDC:
            raise PgpError(
                "This file was encrypted without an integrity check (an old format). It is refused, because "
                "it could have been changed without anyone noticing. Ask the sender to use a current PGP tool."
            )
        elif tag in (PUBLIC_KEY, SECRET_KEY):
            raise PgpError("This is a PGP key, not an encrypted file.")
        elif tag in (LITERAL, COMPRESSED, ONE_PASS_SIGNATURE, SIGNATURE):
            if verify_keys is None:
                raise PgpError(
                    "This file is not encrypted (it is only signed or compressed). To read a signed file, turn "
                    "on 'Check the sender's signature' and choose the credential with the sender's public key."
                )
            result.encrypted = False
            if not _unpack(source, out, result, signatures, first=(tag, body)):
                raise PgpError("The signed data holds no file.")
            signatures.finish()
            result.signature = _check_signatures(signatures, verify_keys)
            return result
        else:
            body.drain()
    version = body.read(1)
    if tag == SEIPD and version == b"\x01":
        container, header = "seipd1", b""
    elif tag == SEIPD and version == b"\x02":
        container, header = "seipd2", body.read_exact(3)
    elif tag == AEAD_ENCRYPTED and version == b"\x01":
        container, header = "aead1", body.read_exact(3)
    else:
        raise PgpError(
            f"The file's encrypted data has version {version[0] if version else 'none'}, which is not supported."
        )
    if container != "seipd1" and len(header) < 3:
        raise PgpError("The encrypted data is cut short.")
    algorithm, key = _session_key(pkesks, skesks, keys, passphrase, result, container)
    if container == "seipd1":
        result.cipher = alg.SYMMETRIC_NAMES.get(algorithm, str(algorithm))
        result.integrity = "MDC"
        plain = _buffered(_SeipdReader(body, algorithm, key))
    else:
        cipher = header[0]
        if len(key) != alg.key_size(cipher):
            raise PgpError("The session key does not fit the file's cipher; the file may be damaged.")
        result.cipher = alg.SYMMETRIC_NAMES.get(cipher, str(cipher))
        result.integrity = f"AEAD {aead.AEAD_NAMES.get(header[1], header[1])}"
        plain = _buffered(aead.AeadReader(body, 2 if container == "seipd2" else 1, key, header))
    if not _unpack(plain, out, result, signatures):
        raise PgpError("The decrypted data holds no file.")
    plain.read()  # to the end, so the integrity check runs
    if verify_keys is not None:
        signatures.finish()
        result.signature = _check_signatures(signatures, verify_keys)
    return result


def decrypt_file(source_path: str, target_path: str, keys: list[Key], passphrase: str | bytes | None = None,
                 *, verify_keys: list[Key] | None = None) -> DecryptResult:
    """Decrypt ``source_path`` into ``target_path``. On any failure the
    partial output is removed and ``PgpError`` says why."""
    work: str | None = None
    try:
        with open(source_path, "rb") as head:
            armored = is_armored(head.read(4096))
        if armored:
            handle, work = tempfile.mkstemp(suffix=".pgp", dir=os.path.dirname(target_path) or None)
            with os.fdopen(handle, "wb") as raw_out, open(source_path, "rb") as text:
                dearmor_stream(text, raw_out)
        with open(work or source_path, "rb") as source, open(target_path, "wb") as out:
            return decrypt_stream(io.BufferedReader(source, buffer_size=1 << 16), out, keys, passphrase,
                                  verify_keys=verify_keys)
    except Exception:
        try:
            os.remove(target_path)
        except OSError:
            pass
        raise
    finally:
        if work:
            try:
                os.remove(work)
            except OSError:
                pass


# ── Encrypting ──

class _SeipdWriter:
    def __init__(self, out, algorithm: int, key: bytes):
        self._out = out
        self._encryptor = alg.cfb(algorithm, key, encrypt=True)
        self._sha1 = hashlib.sha1()
        prefix = os.urandom(alg.block_size(algorithm))
        self.write(prefix + prefix[-2:])

    def write(self, data: bytes) -> int:
        self._sha1.update(data)
        self._out.write(self._encryptor.update(data))
        return len(data)

    def close(self) -> None:
        self._sha1.update(b"\xd3\x14")
        trailer = b"\xd3\x14" + self._sha1.digest()
        self._out.write(self._encryptor.update(trailer) + self._encryptor.finalize())


class _Compressor:
    def __init__(self, out, algorithm: int):
        self._out = out
        self._engine = zlib.compressobj(6, zlib.DEFLATED, -15 if algorithm == 1 else 15)

    def write(self, data: bytes) -> int:
        compressed = self._engine.compress(data)
        if compressed:
            self._out.write(compressed)
        return len(data)

    def finish(self) -> None:
        self._out.write(self._engine.flush())


def _pkesk_body(target: KeyPacket, cipher: int, session: bytes) -> bytes:
    from cryptography.hazmat.primitives.keywrap import aes_key_wrap

    payload = bytes([cipher]) + session + alg.checksum(session)
    body = b"\x03" + target.key_id + bytes([target.algorithm])
    algorithm = target.algorithm
    if algorithm in (alg.RSA, alg.RSA_ENCRYPT_ONLY):
        from cryptography.hazmat.primitives.asymmetric import padding, rsa

        public = rsa.RSAPublicNumbers(target.fields["e"], target.fields["n"]).public_key()
        return body + mpi(public.encrypt(payload, padding.PKCS1v15()))
    if algorithm == alg.ELGAMAL:
        import secrets

        p, g, y = target.fields["p"], target.fields["g"], target.fields["y"]
        size = (p.bit_length() + 7) // 8
        message = int.from_bytes(_eme_pkcs1_encode(payload, size), "big")
        k = secrets.randbelow(p - 3) + 2
        return body + mpi(pow(g, k, p)) + mpi((message * pow(y, k, p)) % p)
    if algorithm == alg.ECDH:
        curve = target.fields["curve"]
        point = target.fields["point"]
        if curve.oid == alg.CURVE25519_LEGACY.oid:
            from cryptography.hazmat.primitives.asymmetric import x25519

            ephemeral = x25519.X25519PrivateKey.generate()
            shared = ephemeral.exchange(x25519.X25519PublicKey.from_public_bytes(point[1:]))
            encoded = b"\x40" + ephemeral.public_key().public_bytes_raw()
        else:
            from cryptography.hazmat.primitives import serialization
            from cryptography.hazmat.primitives.asymmetric import ec

            curve_object = alg.ec_curve(curve)
            peer = ec.EllipticCurvePublicKey.from_encoded_point(curve_object, point)
            ephemeral = ec.generate_private_key(curve_object)
            shared = ephemeral.exchange(ec.ECDH(), peer)
            encoded = ephemeral.public_key().public_bytes(
                serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint,
            )
        kek = alg.ecdh_kek(shared, curve, target.fields["kdf_hash"], target.fields["kdf_cipher"], target.fingerprint)
        pad = 8 - len(payload) % 8
        wrapped = aes_key_wrap(kek, payload + bytes([pad]) * pad)
        return body + mpi(encoded) + bytes([len(wrapped)]) + wrapped
    if algorithm in (alg.X25519, alg.X448):
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import x448, x25519
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF

        if cipher not in (alg.AES128, alg.AES192, alg.AES256):
            raise PgpError("Encrypting to an X25519 or X448 key needs AES.")
        recipient = target.fields["raw"]
        if algorithm == alg.X25519:
            ephemeral = x25519.X25519PrivateKey.generate()
            peer = x25519.X25519PublicKey.from_public_bytes(recipient)
            hash_object, info, kek_size = hashes.SHA256(), b"OpenPGP X25519", 16
        else:
            ephemeral = x448.X448PrivateKey.generate()
            peer = x448.X448PublicKey.from_public_bytes(recipient)
            hash_object, info, kek_size = hashes.SHA512(), b"OpenPGP X448", 32
        ephemeral_public = ephemeral.public_key().public_bytes_raw()
        shared = ephemeral.exchange(peer)
        kek = HKDF(algorithm=hash_object, length=kek_size, salt=None, info=info).derive(
            ephemeral_public + recipient + shared
        )
        wrapped = aes_key_wrap(kek, session)
        return body + ephemeral_public + bytes([1 + len(wrapped)]) + bytes([cipher]) + wrapped
    raise PgpError(f"Encrypting to a {alg.PUBLIC_KEY_NAMES.get(algorithm, algorithm)} key is not supported.")


def encrypt_stream(source: BinaryIO, out: BinaryIO, recipients: list[Key], *, filename: str = "",
                   compress: bool = True, cipher: int = alg.AES256, signer: Signer | None = None) -> list[str]:
    """Encrypt ``source`` to every recipient, signed by ``signer`` when given.
    Returns the key IDs encrypted to."""
    if not recipients:
        raise PgpError("Choose at least one key to encrypt to.")
    targets = [key.encryption_key() for key in recipients]
    session = os.urandom(alg.key_size(cipher))
    used: list[str] = []
    for target in targets:
        out.write(packet(PKESK, _pkesk_body(target, cipher, session)))
        used.append(target.key_id.hex().upper())
    seipd = PartialWriter(out, SEIPD)
    seipd.write(b"\x01")
    encryptor = _SeipdWriter(seipd, cipher, session)
    compressed_packet = None
    compressor = None
    inner: Any = encryptor
    if compress:
        compressed_packet = PartialWriter(encryptor, COMPRESSED)
        compressed_packet.write(b"\x01")  # ZIP, the most widely read
        compressor = _Compressor(compressed_packet, 1)
        inner = compressor
    if signer is not None:
        # One-pass signed: who signs comes first, the signature after the file.
        inner.write(signer.one_pass_packet())
    literal = PartialWriter(inner, LITERAL)
    name = filename.encode("utf-8")[:255]
    literal.write(b"b" + bytes([len(name)]) + name + int(time.time()).to_bytes(4, "big"))
    while True:
        chunk = source.read(_COPY_CHUNK)
        if not chunk:
            break
        literal.write(chunk)
        if signer is not None:
            signer.update(chunk)
    literal.close()
    if signer is not None:
        inner.write(signer.signature_packet())
    if compressor is not None and compressed_packet is not None:
        compressor.finish()
        compressed_packet.close()
    encryptor.close()
    seipd.close()
    return used


def encrypt_file(source_path: str, target_path: str, recipients: list[Key], *, armor: bool = False,
                 compress: bool = True, filename: str | None = None, signer: Signer | None = None) -> list[str]:
    """Encrypt ``source_path`` into ``target_path`` (binary, or armored text),
    signed by ``signer`` when given."""
    name = filename if filename is not None else os.path.basename(source_path)
    try:
        with open(source_path, "rb") as source, open(target_path, "wb") as raw:
            if armor:
                writer = ArmorWriter(raw, "MESSAGE")
                used = encrypt_stream(source, writer, recipients, filename=name, compress=compress, signer=signer)
                writer.close()
            else:
                used = encrypt_stream(source, raw, recipients, filename=name, compress=compress, signer=signer)
        return used
    except Exception:
        try:
            os.remove(target_path)
        except OSError:
            pass
        raise
