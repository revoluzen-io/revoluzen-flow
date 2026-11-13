"""OpenPGP keys: reading public and secret keys, unlocking a secret key with
its passphrase, and choosing which (sub)key encrypts.

Version 4 keys (what GnuPG, Kleopatra, Bouncy Castle and most file-transfer
products make) and version 6 keys (RFC 9580: OpenPGP.js 6, Sequoia) are
read: RSA, Elgamal, DSA, ECDH and ECDSA on Curve25519 and the NIST and
Brainpool curves, EdDSA, X25519, X448, Ed25519 and Ed448. Each key keeps its
self-signatures, so ``signatures.py`` can check which (sub)key may sign.

Secret keys protected the way GnuPG exports them (CFB with an iterated and
salted passphrase) or with the newer AEAD protection (RFC 9580, often with
Argon2) are unlocked.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from revoluzen_flow.openpgp import aead
from revoluzen_flow.openpgp import algorithms as alg
from revoluzen_flow.openpgp.algorithms import PgpError
from revoluzen_flow.openpgp.armor import dearmor, is_armored
from revoluzen_flow.openpgp.packets import (
    PUBLIC_KEY, PUBLIC_SUBKEY, SECRET_KEY, SECRET_SUBKEY, SIGNATURE, USER_ATTRIBUTE, USER_ID,
    iter_packets, parse_signature, read_mpi,
)

# Key flags (signature subpacket 27)
FLAG_CERTIFY, FLAG_SIGN, FLAG_ENCRYPT_COMMS, FLAG_ENCRYPT_STORAGE = 0x01, 0x02, 0x04, 0x08
FEATURE_SEIPD_V1, FEATURE_LIBREPGP_AEAD, FEATURE_SEIPD_V2 = 0x01, 0x02, 0x08


@dataclass
class KeyPacket:
    version: int
    created: int
    algorithm: int
    public_body: bytes
    fields: dict[str, Any]
    fingerprint: bytes
    key_id: bytes
    is_subkey: bool
    secret_body: bytes | None = None   # what follows the public part, if any
    flags: int | None = None
    expires: int | None = None
    revoked: bool = False
    preferred_ciphers: list[int] = field(default_factory=list)
    features: int = 0
    # Signatures on this key as they appear in the key data: direct-key and
    # revocation signatures for the main key; binding and revocation
    # signatures for a subkey. Checked only when they matter (signatures.py).
    signatures: list[bytes] = field(default_factory=list)

    @property
    def can_encrypt(self) -> bool:
        if self.algorithm not in alg.ENCRYPTION_ALGORITHMS:
            return False
        if self.flags is None:
            return True
        return bool(self.flags & (FLAG_ENCRYPT_COMMS | FLAG_ENCRYPT_STORAGE))

    def hashed_form(self) -> bytes:
        """How the key is fed into a signature's hash (RFC 9580 section 5.2.4)."""
        if self.version == 6:
            return b"\x9b" + len(self.public_body).to_bytes(4, "big") + self.public_body
        return b"\x99" + len(self.public_body).to_bytes(2, "big") + self.public_body

    def expired(self, now: datetime | None = None) -> bool:
        if not self.expires:
            return False
        now = now or datetime.now(timezone.utc)
        return now.timestamp() > self.created + self.expires

    def describe_algorithm(self) -> str:
        name = alg.PUBLIC_KEY_NAMES.get(self.algorithm, f"algorithm {self.algorithm}")
        curve = self.fields.get("curve")
        if curve is not None:
            return f"{name} ({curve.name})"
        if "n" in self.fields:
            return f"{name} {self.fields['n'].bit_length()}"
        if "p" in self.fields:
            return f"{name} {self.fields['p'].bit_length()}"
        return name


@dataclass
class Key:
    primary: KeyPacket
    subkeys: list[KeyPacket] = field(default_factory=list)
    user_ids: list[str] = field(default_factory=list)
    # (User ID as stored, signature body) for every signature on a User ID.
    certifications: list[tuple[bytes, bytes]] = field(default_factory=list)

    @property
    def all_packets(self) -> list[KeyPacket]:
        return [self.primary, *self.subkeys]

    @property
    def has_secret(self) -> bool:
        return any(p.secret_body is not None for p in self.all_packets)

    def encryption_key(self) -> KeyPacket:
        """The (sub)key to encrypt to: the newest usable encryption subkey."""
        if self.primary.version == 6:
            raise PgpError(
                f"The key {self.describe()} is a version 6 key (RFC 9580). Flow can decrypt files sent to "
                "such keys but cannot encrypt to them yet. Ask the partner for a version 4 key."
            )
        usable = [p for p in self.subkeys if p.can_encrypt and not p.revoked and not p.expired()]
        if not usable and self.primary.can_encrypt and not self.primary.expired():
            usable = [self.primary]
        if not usable:
            expired = [p for p in self.all_packets if p.can_encrypt and p.expired()]
            if expired:
                raise PgpError(f"The key {self.describe()} has expired, so nothing can be encrypted to it.")
            raise PgpError(f"The key {self.describe()} has no key that can encrypt.")
        return max(usable, key=lambda p: p.created)

    def describe(self) -> str:
        name = self.user_ids[0] if self.user_ids else "without a name"
        return f"'{name}' ({format_fingerprint(self.primary.fingerprint)})"


def format_fingerprint(fingerprint: bytes) -> str:
    hexed = fingerprint.hex().upper()
    return " ".join(hexed[i:i + 4] for i in range(0, len(hexed), 4))


def _parse_public(body: bytes, is_subkey: bool) -> tuple[KeyPacket, int]:
    if not body:
        raise PgpError("A key packet is empty.")
    version = body[0]
    if version == 5:
        raise PgpError(
            "This is a version 5 key (the LibrePGP format of GnuPG's test releases). Flow reads version 4 "
            "and version 6 keys. Export a version 4 key instead (GnuPG 2.4 makes them by default)."
        )
    if version == 3:
        raise PgpError("This is an old version 3 key (PGP 2.x). Make a newer key.")
    if version not in (4, 6):
        raise PgpError(f"Key version {version} is not supported.")
    if len(body) < (10 if version == 6 else 6):
        raise PgpError("A key packet is cut short.")
    created = int.from_bytes(body[1:5], "big")
    algorithm = body[5]
    position = 6
    material_end = None
    if version == 6:
        # Version 6 states the length of the key material, so a key with an
        # algorithm Flow does not know can still be skipped cleanly.
        material_end = 10 + int.from_bytes(body[6:10], "big")
        position = 10
    fields: dict[str, Any] = {}
    if algorithm in (alg.RSA, alg.RSA_ENCRYPT_ONLY, alg.RSA_SIGN_ONLY):
        fields["n"], position, _ = read_mpi(body, position)
        fields["e"], position, _ = read_mpi(body, position)
    elif algorithm == alg.DSA:
        for name in ("p", "q", "g", "y"):
            fields[name], position, _ = read_mpi(body, position)
    elif algorithm == alg.ELGAMAL:
        for name in ("p", "g", "y"):
            fields[name], position, _ = read_mpi(body, position)
    elif algorithm in (alg.ECDH, alg.ECDSA, alg.EDDSA_LEGACY):
        oid_length = body[position]
        oid = body[position + 1:position + 1 + oid_length]
        position += 1 + oid_length
        curve = alg.CURVES.get(oid)
        fields["curve"] = curve or alg.Curve(f"curve {oid.hex()}", oid, 0)
        _, position, point = read_mpi(body, position)
        fields["point"] = point
        if algorithm == alg.ECDH:
            kdf_length = body[position]
            kdf = body[position + 1:position + 1 + kdf_length]
            position += 1 + kdf_length
            if len(kdf) < 3:
                raise PgpError("An ECDH key is missing its key-derivation settings.")
            fields["kdf_hash"], fields["kdf_cipher"] = kdf[1], kdf[2]
    elif algorithm in (alg.X25519, alg.ED25519):
        fields["raw"] = body[position:position + 32]
        position += 32
    elif algorithm in (alg.X448, alg.ED448):
        size = 56 if algorithm == alg.X448 else 57
        fields["raw"] = body[position:position + size]
        position += size
    else:
        raise PgpError(f"Public-key algorithm {algorithm} is not supported.")
    if material_end is not None and position != material_end:
        raise PgpError("A version 6 key packet is malformed (its key material has the wrong length).")
    if position > len(body):
        raise PgpError("A key packet is cut short.")
    public = body[:position]
    if version == 6:
        fingerprint = hashlib.sha256(b"\x9b" + len(public).to_bytes(4, "big") + public).digest()
        key_id = fingerprint[:8]
    else:
        fingerprint = hashlib.sha1(b"\x99" + len(public).to_bytes(2, "big") + public).digest()
        key_id = fingerprint[-8:]
    packet = KeyPacket(
        version=version, created=created, algorithm=algorithm, public_body=public,
        fields=fields, fingerprint=fingerprint, key_id=key_id, is_subkey=is_subkey,
    )
    return packet, position


def _apply_signature(body: bytes, target: KeyPacket | None, primary: KeyPacket | None) -> None:
    """Read key flags, expiry and preferences from a self-signature, to
    choose the key to encrypt to. The signature is not checked here: keys
    come from the person's own credential store, which is trusted input.
    Choosing a key to check a partner's signature with goes through
    ``signatures.py``, which does check them."""
    if target is None:
        return
    try:
        signature = parse_signature(body)
    except PgpError:
        return
    if signature.sig_type in (0x20, 0x28):  # key or subkey revocation
        target.revoked = True
        return
    if signature.sig_type not in (0x10, 0x11, 0x12, 0x13, 0x18, 0x1F):
        return
    if primary is not None:
        issuers = signature.issuer_key_ids
        if issuers and primary.key_id not in issuers:
            return  # made by someone else: a third-party certification
    if signature.key_flags is not None:
        target.flags = signature.key_flags
    data = signature._first(9)
    if data is not None and len(data) == 4:
        target.expires = int.from_bytes(data, "big") or None
    data = signature._first(11)
    if data is not None:
        target.preferred_ciphers = list(data)
    if signature.features is not None:
        target.features = signature.features


def read_keys(data: bytes | str) -> list[Key]:
    """Every key in armored or binary key data."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    blobs: list[bytes]
    if is_armored(data):
        blobs = [blob for kind, blob in dearmor(data) if "KEY BLOCK" in kind]
        if not blobs:
            raise PgpError("No PGP key block was found. Paste a key that starts with -----BEGIN PGP.")
    else:
        blobs = [data]
    keys: list[Key] = []
    for blob in blobs:
        current: Key | None = None
        last: KeyPacket | None = None
        # Where the next signature belongs: the main key or a subkey (its
        # packet), a User ID (its bytes), or nothing to keep.
        holder: KeyPacket | bytes | None = None
        for tag, body in iter_packets(blob):
            if tag in (PUBLIC_KEY, SECRET_KEY):
                packet, end = _parse_public(body, is_subkey=False)
                if tag == SECRET_KEY:
                    packet.secret_body = body[end:]
                current = Key(packet)
                keys.append(current)
                last = holder = packet
            elif tag in (PUBLIC_SUBKEY, SECRET_SUBKEY):
                if current is None:
                    raise PgpError("The key data starts with a subkey; the main key is missing.")
                try:
                    packet, end = _parse_public(body, is_subkey=True)
                except PgpError:
                    last = holder = None
                    continue  # an unsupported subkey is skipped, not fatal
                if tag == SECRET_SUBKEY:
                    packet.secret_body = body[end:]
                current.subkeys.append(packet)
                last = holder = packet
            elif tag == USER_ID and current is not None:
                current.user_ids.append(body.decode("utf-8", "replace"))
                last = current.primary
                holder = body
            elif tag == USER_ATTRIBUTE and current is not None:
                holder = None  # photo IDs: their signatures are not needed
            elif tag == SIGNATURE and current is not None:
                _apply_signature(body, last, current.primary)
                if isinstance(holder, KeyPacket):
                    holder.signatures.append(body)
                elif isinstance(holder, bytes):
                    current.certifications.append((holder, body))
    if not keys:
        raise PgpError("No PGP key was found in the text.")
    return keys


# ── Secret keys ──

def _secret_fields(packet: KeyPacket, plain: bytes) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    position = 0
    if packet.algorithm in (alg.RSA, alg.RSA_ENCRYPT_ONLY, alg.RSA_SIGN_ONLY):
        for name in ("d", "p", "q", "u"):
            fields[name], position, _ = read_mpi(plain, position)
    elif packet.algorithm in (alg.ELGAMAL, alg.DSA):
        fields["x"], position, _ = read_mpi(plain, position)
    elif packet.algorithm in (alg.ECDH, alg.ECDSA, alg.EDDSA_LEGACY):
        fields["secret"], position, fields["secret_raw"] = read_mpi(plain, position)
    elif packet.algorithm in (alg.X25519, alg.ED25519):
        fields["raw"] = plain[:32]
        position = 32
    elif packet.algorithm in (alg.X448, alg.ED448):
        size = 56 if packet.algorithm == alg.X448 else 57
        fields["raw"] = plain[:size]
        position = size
    fields["_end"] = position
    return fields


@dataclass
class _Protection:
    usage: int
    cipher: int = 0
    aead_algorithm: int = 0
    s2k: alg.S2K | None = None
    iv: bytes = b""
    data: bytes = b""


def _protection(packet: KeyPacket) -> _Protection:
    """How a secret key's secret part is protected (RFC 9580 section 5.5.3)."""
    body = packet.secret_body or b""
    if not body:
        raise PgpError("This key has no secret part. Decrypting needs the private key, not the public key.")
    usage = body[0]
    if usage == 0:
        return _Protection(0, data=body[1:])
    if usage not in (253, 254, 255):
        raise PgpError(
            "This private key uses an old protection scheme that is not supported. Export it again with GnuPG."
        )
    position = 1
    if packet.version == 6:
        if usage == 255:
            raise PgpError("The private key is malformed (a version 6 key with the old protection).")
        position = 2  # a count of the parameter octets that follow
    cipher = body[position] if position < len(body) else 0
    position += 1
    aead_algorithm = 0
    if usage == 253:
        aead_algorithm = body[position] if position < len(body) else 0
        position += 1
    if packet.version == 6:
        s2k_length = body[position] if position < len(body) else 0
        s2k, end = alg.read_s2k(body, position + 1)
        if end != position + 1 + s2k_length:
            raise PgpError("The private key is malformed (its passphrase settings have the wrong length).")
        position = end
    else:
        s2k, position = alg.read_s2k(body, position)
    if s2k.kind == 101:
        return _Protection(usage, cipher, aead_algorithm, s2k)
    if s2k.kind == 4 and usage != 253:
        raise PgpError("The private key is malformed (Argon2 is only allowed with AEAD protection).")
    iv_size = aead.nonce_size(aead_algorithm) if usage == 253 else alg.block_size(cipher)
    iv = body[position:position + iv_size]
    if len(iv) != iv_size:
        raise PgpError("The private key is cut short.")
    if packet.version == 6 and body[1] != position + iv_size - 2:
        raise PgpError("The private key is malformed (its protection settings have the wrong length).")
    return _Protection(usage, cipher, aead_algorithm, s2k, iv, body[position + iv_size:])


def unlock(packet: KeyPacket, passphrase: str | bytes | None) -> dict[str, Any]:
    """The secret numbers of a secret (sub)key. Raises ``PgpError`` with a
    plain message for a wrong or missing passphrase. The passphrase is never
    put into a message."""
    if packet.secret_body is None:
        raise PgpError("This key has no secret part. Decrypting needs the private key, not the public key.")
    if isinstance(passphrase, str):
        passphrase = passphrase.encode("utf-8")
    protection = _protection(packet)
    if protection.usage == 0:
        plain = protection.data
        fields = _secret_fields(packet, plain)
        end = fields["_end"]
        if packet.version == 4 and alg.checksum(plain[:end]) != plain[end:end + 2]:
            raise PgpError("The private key is damaged (its checksum does not match).")
        return fields
    s2k = protection.s2k
    if s2k is not None and s2k.kind == 101:
        if s2k.gnu_mode == 2:
            raise PgpError("This private key lives on a smartcard and cannot be used here.")
        raise PgpError("This part of the key was exported without its secret (a stub).")
    if not passphrase:
        raise PgpError("The private key is protected by a passphrase. Add the passphrase to the credential.")
    cipher = protection.cipher
    key = alg.derive_key(s2k, passphrase, alg.key_size(cipher))
    if protection.usage == 253:
        # RFC 9580: the key-encryption key comes from HKDF, and the public
        # key is bound in as associated data, so the two cannot be mixed up.
        packet_type = 0xC7 if packet.is_subkey else 0xC5
        info = bytes([packet_type, packet.version, cipher, protection.aead_algorithm])
        kek = aead.hkdf_sha256(key, info, alg.key_size(cipher))
        plain = aead.open_sealed(
            protection.aead_algorithm, cipher, kek, protection.iv, protection.data,
            bytes([packet_type]) + packet.public_body, "The passphrase is wrong (the private key did not unlock).",
        )
        fields = _secret_fields(packet, plain)
        if fields["_end"] != len(plain):
            raise PgpError("The private key is damaged (its secret part has the wrong length).")
        return fields
    decryptor = alg.cfb(cipher, key, protection.iv)
    plain = decryptor.update(protection.data) + decryptor.finalize()
    if protection.usage == 254:
        data, digest = plain[:-20], plain[-20:]
        if hashlib.sha1(data).digest() != digest:
            raise PgpError("The passphrase is wrong (the private key did not unlock).")
    else:
        data, check = plain[:-2], plain[-2:]
        if alg.checksum(data) != check:
            raise PgpError("The passphrase is wrong (the private key did not unlock).")
    return _secret_fields(packet, data)


def secret_key_usable(packet: KeyPacket) -> bool:
    """False for a stub (secret part not exported) or a smartcard key."""
    body = packet.secret_body
    if not body:
        return False
    if body[0] in (253, 254, 255):
        try:
            s2k = _protection(packet).s2k
        except PgpError:
            return True
        return s2k is None or s2k.kind != 101
    return True


def rsa_private_key(packet: KeyPacket, secret: dict[str, Any]):
    """A ``cryptography`` RSA private key from an unlocked OpenPGP key."""
    from cryptography.hazmat.primitives.asymmetric import rsa

    n, e = packet.fields["n"], packet.fields["e"]
    p, q, d = secret["p"], secret["q"], secret["d"]
    numbers = rsa.RSAPrivateNumbers(
        p=p, q=q, d=d, dmp1=d % (p - 1), dmq1=d % (q - 1), iqmp=pow(q, -1, p),
        public_numbers=rsa.RSAPublicNumbers(e, n),
    )
    return numbers.private_key()


def summary(keys: list[Key]) -> list[dict[str, Any]]:
    """What the settings screen shows about a key. No secret material."""
    from revoluzen_flow.openpgp.signatures import signing_key

    out = []
    for key in keys:
        try:
            encrypting = key.encryption_key()
            encrypt_with = format_fingerprint(encrypting.fingerprint)
            problem = ""
        except PgpError as exc:
            encrypt_with = ""
            problem = str(exc)
        try:
            signs_with = format_fingerprint(signing_key(key).fingerprint)
            sign_problem = ""
        except PgpError as exc:
            signs_with = ""
            sign_problem = str(exc)
        out.append({
            "version": key.primary.version,
            "signs_with": signs_with,
            "sign_problem": sign_problem,
            "user_ids": key.user_ids,
            "fingerprint": format_fingerprint(key.primary.fingerprint),
            "key_id": key.primary.key_id.hex().upper(),
            "algorithm": key.primary.describe_algorithm(),
            "created": datetime.fromtimestamp(key.primary.created, timezone.utc).date().isoformat(),
            "expires": (
                datetime.fromtimestamp(key.primary.created + key.primary.expires, timezone.utc).date().isoformat()
                if key.primary.expires else None
            ),
            "has_private_key": key.has_secret,
            "subkeys": [
                {
                    "key_id": p.key_id.hex().upper(),
                    "algorithm": p.describe_algorithm(),
                    "can_encrypt": p.can_encrypt,
                    "has_secret": p.secret_body is not None and secret_key_usable(p),
                    "expired": p.expired(),
                    "revoked": p.revoked,
                }
                for p in key.subkeys
            ],
            "encrypts_with": encrypt_with,
            "problem": problem,
            "asks_for_aead": bool(key.primary.features & FEATURE_LIBREPGP_AEAD)
            or any(p.features & FEATURE_LIBREPGP_AEAD for p in key.subkeys),
        })
    return out
