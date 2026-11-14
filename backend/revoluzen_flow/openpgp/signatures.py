"""Checking a partner's signature on a file, and signing our own files.

A partner who signs a file before encrypting it puts three things inside the
encrypted data: a one-pass signature packet (who signs, with which hash),
the file itself, and the signature. Flow hashes the file as it is decrypted
and then checks the signature against the partner's public key, kept in
Credentials. A signed file that is not encrypted has the same structure.

What counts as a good signature:

* it is made by the partner's key: the main key, or a signing subkey that
  the main key has bound to itself and that has signed back (a "cross
  certification"), so a subkey someone else added to a copy of the key does
  not count;
* that (sub)key was not revoked, and had not expired when the file was
  signed;
* it uses SHA-256 or stronger. MD5, SHA-1 and RIPEMD-160 are refused for
  files, because colliding documents can be made for them (RFC 9580 section
  9.5); OpenPGP.js and Sequoia refuse them too;
* the hash matches the file and the signature verifies.

Supported: version 4 and version 6 signatures by RSA, DSA, ECDSA (NIST and
Brainpool curves), EdDSA (Ed25519), Ed25519 and Ed448 keys; binary and text
signatures. Signing uses a version 4 key: RSA, ECDSA, EdDSA, Ed25519 or Ed448.

Nothing here logs or returns key material or passphrases.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass

from revoluzen_flow.openpgp import algorithms as alg
from revoluzen_flow.openpgp.algorithms import PgpError
from revoluzen_flow.openpgp.keys import (
    FLAG_SIGN, Key, KeyPacket, format_fingerprint, rsa_private_key, secret_key_usable, unlock,
)
from revoluzen_flow.openpgp.packets import (
    ONE_PASS_SIGNATURE, SIGNATURE, Signature, mpi, packet, parse_signature, read_mpi,
)

BINARY, TEXT = 0x00, 0x01
_CERTIFICATIONS = (0x10, 0x11, 0x12, 0x13)
_SUBKEY_BINDING, _PRIMARY_BINDING, _DIRECT, _KEY_REVOCATION, _SUBKEY_REVOCATION = 0x18, 0x19, 0x1F, 0x20, 0x28
# Revocation reasons that do not withdraw earlier signatures: the key was
# replaced or retired, not stolen (RFC 9580 section 5.2.3.31).
_SOFT_REVOCATIONS = {1, 3, 32}
# Salt sizes for version 6 signatures (RFC 9580 table 23).
V6_SALT_SIZES = {8: 16, 9: 24, 10: 32, 11: 16, 12: 16, 14: 32}


# ── Hashing what was signed ──

class _TextCanonicalizer:
    """Feeds text to a hash with every line ending as CR LF, as text
    signatures (type 0x01) require, even when the text arrives in pieces."""

    _LONE_LF = re.compile(rb"(?<!\r)\n")

    def __init__(self, digest):
        self._digest = digest
        self._after_cr = False

    def update(self, data: bytes) -> None:
        if not data:
            return
        converted = self._LONE_LF.sub(b"\r\n", data)
        if self._after_cr and data[:1] == b"\n":
            converted = converted[1:]  # that LF ends a CR LF split between two pieces
        self._digest.update(converted)
        self._after_cr = data[-1:] == b"\r"

    def finish(self, trailer: bytes) -> bytes:
        self._digest.update(trailer)
        return self._digest.digest()


class _Binary:
    def __init__(self, digest):
        self._digest = digest

    def update(self, data: bytes) -> None:
        self._digest.update(data)

    def finish(self, trailer: bytes) -> bytes:
        self._digest.update(trailer)
        return self._digest.digest()


def data_hasher(hash_algorithm: int, sig_type: int, salt: bytes = b""):
    """A hash to feed the signed file through, before the signature is known."""
    digest = alg.new_hash(hash_algorithm)
    digest.update(salt)  # version 6 signatures hash their salt first
    return _TextCanonicalizer(digest) if sig_type == TEXT else _Binary(digest)


def _key_digest(signature: Signature, data: bytes) -> bytes:
    digest = alg.new_hash(signature.hash_algorithm)
    digest.update(signature.salt)
    digest.update(data)
    digest.update(signature.trailer())
    return digest.digest()


# ── The raw signature check, per algorithm ──

def _verify_raw(signer: KeyPacket, signature: Signature, digest: bytes) -> bool:
    """True when ``signature`` over ``digest`` was made by ``signer``."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric import dsa, ec, ed448, ed25519, padding, rsa, utils

    if signature.algorithm != signer.algorithm and not (
        {signature.algorithm, signer.algorithm} <= {alg.RSA, alg.RSA_SIGN_ONLY}
    ):
        return False
    if signature.left16 != digest[:2]:
        return False
    material = signature.material
    algorithm = signer.algorithm
    try:
        if algorithm in (alg.RSA, alg.RSA_SIGN_ONLY):
            value, _, _ = read_mpi(material, 0)
            n, e = signer.fields["n"], signer.fields["e"]
            size = (n.bit_length() + 7) // 8
            if value >= n:
                return False
            rsa.RSAPublicNumbers(e, n).public_key().verify(
                value.to_bytes(size, "big"), digest, padding.PKCS1v15(),
                utils.Prehashed(alg.prehashed(signature.hash_algorithm)),
            )
            return True
        if algorithm in (alg.DSA, alg.ECDSA):
            r, position, _ = read_mpi(material, 0)
            s, _, _ = read_mpi(material, position)
            der = utils.encode_dss_signature(r, s)
            hashed = utils.Prehashed(alg.prehashed(signature.hash_algorithm))
            if algorithm == alg.DSA:
                fields = signer.fields
                public = dsa.DSAPublicNumbers(
                    fields["y"], dsa.DSAParameterNumbers(fields["p"], fields["q"], fields["g"]),
                ).public_key()
                public.verify(der, digest, hashed)
            else:
                curve = alg.ec_curve(signer.fields["curve"])
                public = ec.EllipticCurvePublicKey.from_encoded_point(curve, signer.fields["point"])
                public.verify(der, digest, ec.ECDSA(hashed))
            return True
        if algorithm == alg.EDDSA_LEGACY:
            point = signer.fields["point"]
            if signer.fields["curve"].oid != alg.ED25519_LEGACY.oid or len(point) != 33 or point[0] != 0x40:
                raise PgpError("The sender's key uses an EdDSA curve that is not supported.")
            _, position, r = read_mpi(material, 0)
            _, _, s = read_mpi(material, position)
            if len(r) > 32 or len(s) > 32:
                return False
            ed25519.Ed25519PublicKey.from_public_bytes(point[1:]).verify(
                r.rjust(32, b"\x00") + s.rjust(32, b"\x00"), digest,
            )
            return True
        if algorithm == alg.ED25519:
            if len(material) != 64:
                return False
            ed25519.Ed25519PublicKey.from_public_bytes(signer.fields["raw"]).verify(material, digest)
            return True
        if algorithm == alg.ED448:
            if len(material) != 114:
                return False
            ed448.Ed448PublicKey.from_public_bytes(signer.fields["raw"]).verify(material, digest)
            return True
    except (InvalidSignature, ValueError, OverflowError):
        return False
    raise PgpError(f"Signatures made with {alg.PUBLIC_KEY_NAMES.get(algorithm, algorithm)} keys cannot be checked.")


def _hash_strong_enough(signer: KeyPacket, hash_algorithm: int) -> bool:
    """RFC 9580 section 5.2.3: the digest must be at least as long as the key needs."""
    size = alg.HASH_SIZES.get(hash_algorithm, 0)
    algorithm = signer.algorithm
    if algorithm in (alg.EDDSA_LEGACY, alg.ED25519):
        return size >= 32
    if algorithm == alg.ED448:
        return size >= 64
    if algorithm == alg.ECDSA:
        curve = signer.fields["curve"]
        if curve.oid == alg.NIST_P521.oid:
            return size >= 64
        return size >= curve.size
    if algorithm == alg.DSA:
        return size * 8 >= signer.fields["q"].bit_length()
    return True


# ── Which parts of a key may sign ──

@dataclass
class _State:
    usable: bool = True
    problem: str = ""
    flags: int | None = None
    expires: int | None = None      # absolute time, or None for never
    revoked_from: int | None = None  # signatures made at or after this time are refused; 0 = all


def _parsed(body: bytes) -> Signature | None:
    try:
        return parse_signature(body)
    except PgpError:
        return None


def _made_by(signature: Signature, signer: KeyPacket) -> bool:
    issuers = signature.issuer_key_ids
    return not issuers or signer.key_id in issuers


def _self_signature_ok(signature: Signature | None, signer: KeyPacket, data: bytes) -> bool:
    if signature is None or signature.hash_algorithm in (1, 3) or signature.unknown_critical:
        return False
    if signature.version != signer.version or not _made_by(signature, signer):
        return False
    try:
        return _verify_raw(signer, signature, _key_digest(signature, data))
    except PgpError:
        return False


def _revocation(signatures: list[bytes], sig_type: int, primary: KeyPacket, data: bytes) -> int | None:
    revoked_from = None
    for body in signatures:
        signature = _parsed(body)
        if signature is None or signature.sig_type != sig_type:
            continue
        if not _self_signature_ok(signature, primary, data):
            continue
        reason = signature.revocation_reason
        moment = (signature.created or 0) if reason in _SOFT_REVOCATIONS else 0
        revoked_from = moment if revoked_from is None else min(revoked_from, moment)
    return revoked_from


def _primary_state(key: Key) -> _State:
    primary = key.primary
    state = _State()
    newest: Signature | None = None
    flags_from: Signature | None = None
    candidates: list[tuple[Signature | None, bytes]] = [
        (_parsed(body), primary.hashed_form()) for body in primary.signatures
    ]
    for user_id, body in key.certifications:
        candidates.append((_parsed(body), primary.hashed_form() + b"\xb4" + len(user_id).to_bytes(4, "big") + user_id))
    for signature, data in candidates:
        if signature is None or signature.sig_type not in (*_CERTIFICATIONS, _DIRECT):
            continue
        if not _self_signature_ok(signature, primary, data):
            continue
        created = signature.created or 0
        if newest is None or created >= (newest.created or 0):
            newest = signature
        if signature.key_flags is not None and (flags_from is None or created >= (flags_from.created or 0)):
            flags_from = signature
    if newest is not None and newest.key_expires_after:
        state.expires = primary.created + newest.key_expires_after
    if flags_from is not None:
        state.flags = flags_from.key_flags
    state.revoked_from = _revocation(primary.signatures, _KEY_REVOCATION, primary, primary.hashed_form())
    return state


def _subkey_state(key: Key, subkey: KeyPacket, *, for_signing: bool) -> _State:
    primary = key.primary
    data = primary.hashed_form() + subkey.hashed_form()
    binding: Signature | None = None
    for body in subkey.signatures:
        signature = _parsed(body)
        if signature is None or signature.sig_type != _SUBKEY_BINDING:
            continue
        if not _self_signature_ok(signature, primary, data):
            continue
        if binding is None or (signature.created or 0) >= (binding.created or 0):
            binding = signature
    if binding is None:
        return _State(False, "the subkey has no valid binding signature from the main key")
    state = _State(flags=binding.key_flags)
    if binding.key_expires_after:
        state.expires = subkey.created + binding.key_expires_after
    if for_signing and (state.flags is None or state.flags & FLAG_SIGN):
        # A signing subkey must sign back, so nobody can attach someone
        # else's signing key to this key (RFC 9580 section 5.2.1.9).
        if not any(
            (back := _parsed(body)) is not None and back.sig_type == _PRIMARY_BINDING
            and _self_signature_ok(back, subkey, data)
            for body in binding.embedded
        ):
            return _State(False, "the signing subkey has no valid back-signature")
    state.revoked_from = _revocation(subkey.signatures, _SUBKEY_REVOCATION, primary, data)
    return state


def _signing_problem(key: Key, part: KeyPacket, at: int) -> str:
    """Why ``part`` of ``key`` could not have signed at time ``at`` ("" if it could)."""
    if part.algorithm not in alg.SIGNING_ALGORITHMS:
        return "it is not a signing key"
    primary = _primary_state(key)
    if primary.revoked_from is not None and at >= primary.revoked_from:
        return "the key has been revoked"
    if primary.expires is not None and at > primary.expires:
        return "the key had expired when the file was signed"
    if part.created > at + 300:
        return "the key is newer than the signature"
    if part is key.primary:
        if primary.flags is not None and not primary.flags & FLAG_SIGN:
            return "the main key is not allowed to sign (it is for certifying only)"
        return ""
    state = _subkey_state(key, part, for_signing=True)
    if not state.usable:
        return state.problem
    if state.flags is not None and not state.flags & FLAG_SIGN:
        return "that subkey is not a signing key"
    if state.revoked_from is not None and at >= state.revoked_from:
        return "the signing subkey has been revoked"
    if state.expires is not None and at > state.expires:
        return "the signing subkey had expired when the file was signed"
    return ""


def signing_key(key: Key, *, need_secret: bool = False, at: int | None = None) -> KeyPacket:
    """The (sub)key of ``key`` that signs now: the newest valid signing
    subkey, else the main key if it may sign."""
    moment = int(time.time()) if at is None else at
    parts = sorted(key.subkeys, key=lambda p: p.created, reverse=True) + [key.primary]
    problems = []
    for part in parts:
        if part.algorithm not in alg.SIGNING_ALGORITHMS:
            continue
        if need_secret and (part.secret_body is None or not secret_key_usable(part)):
            continue
        problem = _signing_problem(key, part, moment)
        if not problem:
            return part
        problems.append(problem)
    detail = f" ({problems[0]})" if problems else ""
    raise PgpError(f"The key {key.describe()} has no part that can sign{detail}.")


# ── Checking a signature on a file ──

@dataclass
class SignatureCheck:
    """A good signature: who made it and when."""
    signer: str          # the key's first User ID and fingerprint
    fingerprint: str     # of the (sub)key that signed
    key_id: str
    created: int
    hash_name: str


def _describe_issuer(signature: Signature) -> str:
    fingerprints = sorted(signature.issuer_fingerprints)
    if fingerprints:
        return format_fingerprint(fingerprints[0])
    ids = sorted(signature.issuer_key_ids)
    return ids[0].hex().upper() if ids else "unknown"


def check_signature(signature: Signature, digest: bytes, keys: list[Key], *, now: int | None = None) -> SignatureCheck:
    """Check one signature on a file. ``digest`` is the hash of the file and
    the signature's trailer. Raises ``PgpError`` saying what is wrong."""
    moment = int(time.time()) if now is None else now
    if signature.sig_type not in (BINARY, TEXT):
        raise PgpError("The file's signature is not a signature on a document.")
    if signature.hash_algorithm in alg.WEAK_HASHES:
        raise PgpError(
            f"The file was signed with the {alg.HASH_LABELS.get(signature.hash_algorithm)} hash, which is no "
            "longer safe. Ask the sender to sign with SHA-256 or stronger (in GnuPG: --digest-algo SHA256)."
        )
    if signature.unknown_critical:
        raise PgpError("The file's signature has a part marked critical that Flow does not understand, so it "
                       "cannot be relied on.")
    created = signature.created
    if created is None:
        raise PgpError("The file's signature has no creation time, so it cannot be relied on.")
    if signature.expires_after and moment > created + signature.expires_after:
        raise PgpError("The sender's signature on the file has expired.")
    issuers = signature.issuer_key_ids
    fingerprints = signature.issuer_fingerprints
    candidates: list[tuple[Key, KeyPacket]] = []
    for key in keys:
        for part in key.all_packets:
            if fingerprints and part.fingerprint in fingerprints:
                candidates.append((key, part))
            elif not fingerprints and (not issuers or part.key_id in issuers):
                candidates.append((key, part))
    if not candidates:
        have = ", ".join(format_fingerprint(key.primary.fingerprint) for key in keys) or "none"
        raise PgpError(
            f"The file was signed with key {_describe_issuer(signature)}, which is not the sender's key in the "
            f"credential (it holds {have}). Check that the credential has the sender's current public key."
        )
    problems = []
    for key, part in candidates:
        if part.version != signature.version:
            problems.append("the signature's version does not match the key")
            continue
        if signature.version == 6 and len(signature.salt) != V6_SALT_SIZES.get(signature.hash_algorithm):
            problems.append("the signature's salt has the wrong size")
            continue
        if not _hash_strong_enough(part, signature.hash_algorithm):
            problems.append("the signature uses a hash too short for its key")
            continue
        if not _verify_raw(part, signature, digest):
            if issuers or fingerprints:
                raise PgpError(
                    "The sender's signature does not match the file: the file was changed after it was "
                    "signed, or the signature is damaged. The file was not read."
                )
            continue
        problem = _signing_problem(key, part, created)
        if problem:
            problems.append(problem)
            continue
        return SignatureCheck(
            signer=key.describe(), fingerprint=format_fingerprint(part.fingerprint),
            key_id=part.key_id.hex().upper(), created=created,
            hash_name=alg.HASH_LABELS.get(signature.hash_algorithm, str(signature.hash_algorithm)),
        )
    if problems:
        raise PgpError(f"The sender's signature cannot be accepted: {problems[0]}.")
    raise PgpError(
        "The sender's signature does not match the file: the file was changed after it was signed, or it "
        "was signed with another key."
    )


def check_detached(data: bytes, signature_body: bytes, keys: list[Key], *, now: int | None = None) -> SignatureCheck:
    """Check a detached signature (a separate .sig file) over ``data``."""
    signature = parse_signature(signature_body)
    hasher = data_hasher(signature.hash_algorithm, signature.sig_type, signature.salt)
    hasher.update(data)
    return check_signature(signature, hasher.finish(signature.trailer()), keys, now=now)


# ── Signing our own files ──

def _hash_for(part: KeyPacket) -> int:
    if part.algorithm == alg.ED448:
        return alg.SHA512
    if part.algorithm == alg.ECDSA:
        size = part.fields["curve"].size
        return alg.SHA512 if size > 48 else alg.SHA384 if size > 32 else alg.SHA256
    return alg.SHA256


def _sign_raw(part: KeyPacket, secret: dict, hash_algorithm: int, digest: bytes) -> bytes:
    from cryptography.hazmat.primitives.asymmetric import ec, ed448, ed25519, padding, utils

    algorithm = part.algorithm
    if algorithm in (alg.RSA, alg.RSA_SIGN_ONLY):
        value = rsa_private_key(part, secret).sign(
            digest, padding.PKCS1v15(), utils.Prehashed(alg.prehashed(hash_algorithm)),
        )
        return mpi(value)
    if algorithm == alg.ECDSA:
        private = ec.derive_private_key(secret["secret"], alg.ec_curve(part.fields["curve"]))
        r, s = utils.decode_dss_signature(
            private.sign(digest, ec.ECDSA(utils.Prehashed(alg.prehashed(hash_algorithm))))
        )
        return mpi(r) + mpi(s)
    if algorithm == alg.EDDSA_LEGACY:
        if part.fields["curve"].oid != alg.ED25519_LEGACY.oid:
            raise PgpError("Signing with this EdDSA curve is not supported.")
        seed = secret["secret_raw"].rjust(32, b"\x00")[-32:]
        value = ed25519.Ed25519PrivateKey.from_private_bytes(seed).sign(digest)
        return mpi(value[:32]) + mpi(value[32:])
    if algorithm == alg.ED25519:
        return ed25519.Ed25519PrivateKey.from_private_bytes(secret["raw"]).sign(digest)
    if algorithm == alg.ED448:
        return ed448.Ed448PrivateKey.from_private_bytes(secret["raw"]).sign(digest)
    raise PgpError(
        f"Signing with a {alg.PUBLIC_KEY_NAMES.get(algorithm, algorithm)} key is not supported. "
        "Use an RSA, ECDSA or Curve25519 key."
    )


def _subpacket(kind: int, data: bytes, critical: bool = False) -> bytes:
    body = bytes([kind | (0x80 if critical else 0)]) + data
    length = len(body)
    if length < 192:
        return bytes([length]) + body
    length -= 192
    return bytes([(length >> 8) + 192, length & 0xFF]) + body


class Signer:
    """Signs a file as it is written: a one-pass signature packet goes before
    the file, and ``signature_packet()`` after it. Version 4 keys only."""

    def __init__(self, keys: list[Key], passphrase: str | bytes | None, *, sig_type: int = BINARY):
        part = None
        key = None
        problems = []
        for candidate in keys:
            try:
                part = signing_key(candidate, need_secret=True)
                key = candidate
                break
            except PgpError as exc:
                problems.append(str(exc))
        if part is None or key is None:
            if any(k.has_secret for k in keys):
                raise PgpError(problems[0] if problems else "The private key has no part that can sign.")
            raise PgpError("Signing needs your private key, not a public key. Choose the credential with your "
                           "private key (private_key) and its passphrase.")
        if part.version != 4:
            raise PgpError("Signing with a version 6 key is not supported yet. Use a version 4 key.")
        self._part = part
        self._secret = unlock(part, passphrase)
        self.sig_type = sig_type
        self.hash_algorithm = _hash_for(part)
        self.key_id = part.key_id.hex().upper()
        self.fingerprint = format_fingerprint(part.fingerprint)
        self.signer = key.describe()
        self._hasher = data_hasher(self.hash_algorithm, sig_type)

    def one_pass_packet(self) -> bytes:
        """Starts a new signed file: the packet that goes before it."""
        self._hasher = data_hasher(self.hash_algorithm, self.sig_type)
        body = bytes([3, self.sig_type, self.hash_algorithm, self._part.algorithm]) + self._part.key_id + b"\x01"
        return packet(ONE_PASS_SIGNATURE, body)

    def update(self, data: bytes) -> None:
        self._hasher.update(data)

    def signature_packet(self, created: int | None = None) -> bytes:
        moment = int(time.time()) if created is None else created
        part = self._part
        # Hashed: when it was signed, and the signing key's fingerprint.
        hashed = _subpacket(2, moment.to_bytes(4, "big"), critical=True) + _subpacket(33, b"\x04" + part.fingerprint)
        body = bytes([4, self.sig_type, part.algorithm, self.hash_algorithm]) + len(hashed).to_bytes(2, "big") + hashed
        digest = self._hasher.finish(body + b"\x04\xff" + len(body).to_bytes(4, "big"))
        unhashed = _subpacket(16, part.key_id)
        full = (body + len(unhashed).to_bytes(2, "big") + unhashed + digest[:2]
                + _sign_raw(part, self._secret, self.hash_algorithm, digest))
        return packet(SIGNATURE, full)
