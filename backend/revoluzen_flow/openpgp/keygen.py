"""Making a new OpenPGP key pair (version 4).

Two kinds: RSA (a signing primary key plus an encryption subkey, 3072 or 4096
bits), which every PGP product reads, and Curve25519 (an Ed25519 primary key
plus a Curve25519 encryption subkey), which is smaller and what GnuPG makes
by default. Both carry proper self-signatures, so the public key can be
imported by GnuPG, Kleopatra or a partner's file-transfer product.
"""

from __future__ import annotations

import hashlib
import os
import time
from dataclasses import dataclass

from revoluzen_flow.openpgp import algorithms as alg
from revoluzen_flow.openpgp.armor import armor_bytes
from revoluzen_flow.openpgp.keys import _parse_public, format_fingerprint
from revoluzen_flow.openpgp.packets import (
    PUBLIC_KEY, PUBLIC_SUBKEY, SECRET_KEY, SECRET_SUBKEY, SIGNATURE, USER_ID, mpi, packet,
)

KINDS = ("rsa3072", "rsa4096", "curve25519")


@dataclass
class GeneratedKey:
    private_key: str      # armored, protected by the passphrase when one was given
    public_key: str       # armored, safe to send to partners
    fingerprint: str
    user_id: str


def _subpacket(kind: int, data: bytes) -> bytes:
    return bytes([len(data) + 1, kind]) + data


def _key_hash_part(public_body: bytes) -> bytes:
    return b"\x99" + len(public_body).to_bytes(2, "big") + public_body


def _sign(primary_algorithm: int, signer, digest: bytes) -> bytes:
    if primary_algorithm == alg.RSA:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding, utils

        signature = signer.sign(digest, padding.PKCS1v15(), utils.Prehashed(hashes.SHA256()))
        return mpi(signature)
    signature = signer.sign(digest)  # Ed25519 over the hash value
    return mpi(signature[:32]) + mpi(signature[32:])


def _signature(primary_algorithm: int, signer, primary_fingerprint: bytes, sig_type: int,
               subpackets: bytes, hashed_prefix: bytes, created: int) -> bytes:
    hashed = (
        _subpacket(2, created.to_bytes(4, "big"))
        + subpackets
        + _subpacket(33, b"\x04" + primary_fingerprint)
    )
    body = bytes([4, sig_type, primary_algorithm, alg.SHA256]) + len(hashed).to_bytes(2, "big") + hashed
    trailer = b"\x04\xff" + len(body).to_bytes(4, "big")
    digest = hashlib.sha256(hashed_prefix + body + trailer).digest()
    unhashed = _subpacket(16, primary_fingerprint[-8:])
    return body + len(unhashed).to_bytes(2, "big") + unhashed + digest[:2] + _sign(primary_algorithm, signer, digest)


def _protect(public_body: bytes, secret_mpis: bytes, passphrase: bytes | None) -> bytes:
    if not passphrase:
        return public_body + b"\x00" + secret_mpis + alg.checksum(secret_mpis)
    salt = os.urandom(8)
    coded_count = 0xF0  # 33,554,432 octets hashed: slow to guess, quick to use
    s2k = alg.S2K(3, alg.SHA256, salt, (16 + (coded_count & 15)) << ((coded_count >> 4) + 6))
    key = alg.derive_key(s2k, passphrase, alg.key_size(alg.AES256))
    iv = os.urandom(16)
    plain = secret_mpis + hashlib.sha1(secret_mpis).digest()
    encryptor = alg.cfb(alg.AES256, key, iv, encrypt=True)
    encrypted = encryptor.update(plain) + encryptor.finalize()
    return public_body + bytes([254, alg.AES256, 3, alg.SHA256]) + salt + bytes([coded_count]) + iv + encrypted


def _rsa_parts(bits: int, created: int):
    from cryptography.hazmat.primitives.asymmetric import rsa

    private = rsa.generate_private_key(public_exponent=65537, key_size=bits)
    numbers = private.private_numbers()
    public = numbers.public_numbers
    p, q = sorted((numbers.p, numbers.q))
    body = bytes([4]) + created.to_bytes(4, "big") + bytes([alg.RSA]) + mpi(public.n) + mpi(public.e)
    secret = mpi(numbers.d) + mpi(p) + mpi(q) + mpi(pow(p, -1, q))
    return private, body, secret


def generate_key(name: str, email: str = "", *, kind: str = "rsa3072", passphrase: str = "") -> GeneratedKey:
    from cryptography.hazmat.primitives.asymmetric import ed25519, x25519

    if kind not in KINDS:
        raise alg.PgpError(f"Key type must be one of {', '.join(KINDS)}.")
    user_id = name.strip()
    if email.strip():
        user_id = f"{user_id} <{email.strip()}>" if user_id else f"<{email.strip()}>"
    if not user_id:
        raise alg.PgpError("Give the key a name or an email address.")
    created = int(time.time())
    secret_phrase = passphrase.encode("utf-8") if passphrase else None

    if kind.startswith("rsa"):
        bits = 4096 if kind == "rsa4096" else 3072
        signer, primary_body, primary_secret = _rsa_parts(bits, created)
        _, sub_body, sub_secret = _rsa_parts(bits, created)
        primary_algorithm = alg.RSA
    else:
        signer = ed25519.Ed25519PrivateKey.generate()
        ed_public = signer.public_key().public_bytes_raw()
        oid = alg.ED25519_LEGACY.oid
        primary_body = (bytes([4]) + created.to_bytes(4, "big") + bytes([alg.EDDSA_LEGACY, len(oid)]) + oid
                        + mpi(b"\x40" + ed_public))
        primary_secret = mpi(signer.private_bytes_raw())
        native = bytearray(x25519.X25519PrivateKey.generate().private_bytes_raw())
        native[0] &= 248
        native[31] &= 127
        native[31] |= 64
        encryption = x25519.X25519PrivateKey.from_private_bytes(bytes(native))
        cv_oid = alg.CURVE25519_LEGACY.oid
        sub_body = (bytes([4]) + created.to_bytes(4, "big") + bytes([alg.ECDH, len(cv_oid)]) + cv_oid
                    + mpi(b"\x40" + encryption.public_key().public_bytes_raw())
                    + bytes([3, 1, alg.SHA256, alg.AES128]))
        sub_secret = mpi(bytes(native)[::-1])
        primary_algorithm = alg.EDDSA_LEGACY

    primary, _ = _parse_public(primary_body, is_subkey=False)
    subkey, _ = _parse_public(sub_body, is_subkey=True)
    uid_bytes = user_id.encode("utf-8")
    certification = _signature(
        primary_algorithm, signer, primary.fingerprint, 0x13,
        _subpacket(27, b"\x03")                       # certify + sign
        + _subpacket(11, bytes([alg.AES256, alg.AES192, alg.AES128]))
        + _subpacket(21, bytes([alg.SHA256, alg.SHA512, 9]))
        + _subpacket(22, bytes([2, 1, 0]))            # ZLIB, ZIP, none
        + _subpacket(30, b"\x01"),                    # integrity-protected data (MDC)
        _key_hash_part(primary_body) + b"\xb4" + len(uid_bytes).to_bytes(4, "big") + uid_bytes,
        created,
    )
    binding = _signature(
        primary_algorithm, signer, primary.fingerprint, 0x18,
        _subpacket(27, b"\x0c"),                      # encrypt communications + storage
        _key_hash_part(primary_body) + _key_hash_part(sub_body),
        created,
    )
    public_blob = (
        packet(PUBLIC_KEY, primary_body) + packet(USER_ID, uid_bytes) + packet(SIGNATURE, certification)
        + packet(PUBLIC_SUBKEY, sub_body) + packet(SIGNATURE, binding)
    )
    secret_blob = (
        packet(SECRET_KEY, _protect(primary_body, primary_secret, secret_phrase))
        + packet(USER_ID, uid_bytes) + packet(SIGNATURE, certification)
        + packet(SECRET_SUBKEY, _protect(sub_body, sub_secret, secret_phrase))
        + packet(SIGNATURE, binding)
    )
    return GeneratedKey(
        private_key=armor_bytes("PRIVATE KEY BLOCK", secret_blob),
        public_key=armor_bytes("PUBLIC KEY BLOCK", public_blob),
        fingerprint=format_fingerprint(primary.fingerprint),
        user_id=user_id,
    )
