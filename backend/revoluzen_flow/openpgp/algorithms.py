"""OpenPGP algorithm numbers and the primitives behind them.

Everything cryptographic comes from the ``cryptography`` package. This module
only maps OpenPGP's algorithm numbers (RFC 9580) to it, and implements the two
OpenPGP-specific constructions that are not in it: the string-to-key function
that turns a passphrase into a key, and the ECDH key-derivation step.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


class PgpError(ValueError):
    """A PGP file or key cannot be used. The message says why in plain words."""


# ── Public-key algorithms ──

RSA = 1
RSA_ENCRYPT_ONLY = 2
RSA_SIGN_ONLY = 3
ELGAMAL = 16
DSA = 17
ECDH = 18
ECDSA = 19
EDDSA_LEGACY = 22
X25519 = 25
X448 = 26
ED25519 = 27
ED448 = 28

PUBLIC_KEY_NAMES = {
    RSA: "RSA", RSA_ENCRYPT_ONLY: "RSA", RSA_SIGN_ONLY: "RSA (sign only)", ELGAMAL: "Elgamal",
    DSA: "DSA", ECDH: "ECDH", ECDSA: "ECDSA", EDDSA_LEGACY: "EdDSA", X25519: "X25519",
    X448: "X448", ED25519: "Ed25519", ED448: "Ed448",
}
ENCRYPTION_ALGORITHMS = {RSA, RSA_ENCRYPT_ONLY, ELGAMAL, ECDH, X25519, X448}
SIGNING_ALGORITHMS = {RSA, RSA_SIGN_ONLY, DSA, ECDSA, EDDSA_LEGACY, ED25519, ED448}


# ── Curves, by their OID bytes as they appear in key packets ──

@dataclass(frozen=True)
class Curve:
    name: str
    oid: bytes
    size: int  # bytes per coordinate / key


CURVE25519_LEGACY = Curve("Curve25519", bytes.fromhex("2b060104019755010501"), 32)
ED25519_LEGACY = Curve("Ed25519", bytes.fromhex("2b06010401da470f01"), 32)
NIST_P256 = Curve("NIST P-256", bytes.fromhex("2a8648ce3d030107"), 32)
NIST_P384 = Curve("NIST P-384", bytes.fromhex("2b81040022"), 48)
NIST_P521 = Curve("NIST P-521", bytes.fromhex("2b81040023"), 66)
BRAINPOOL_P256 = Curve("Brainpool P-256", bytes.fromhex("2b2403030208010107"), 32)
BRAINPOOL_P384 = Curve("Brainpool P-384", bytes.fromhex("2b240303020801010b"), 48)
BRAINPOOL_P512 = Curve("Brainpool P-512", bytes.fromhex("2b240303020801010d"), 64)

CURVES = {c.oid: c for c in (
    CURVE25519_LEGACY, ED25519_LEGACY, NIST_P256, NIST_P384, NIST_P521,
    BRAINPOOL_P256, BRAINPOOL_P384, BRAINPOOL_P512,
)}


def ec_curve(curve: Curve):
    """The ``cryptography`` curve object for a NIST or Brainpool curve."""
    from cryptography.hazmat.primitives.asymmetric import ec

    table = {
        NIST_P256.oid: ec.SECP256R1, NIST_P384.oid: ec.SECP384R1, NIST_P521.oid: ec.SECP521R1,
        BRAINPOOL_P256.oid: ec.BrainpoolP256R1, BRAINPOOL_P384.oid: ec.BrainpoolP384R1,
        BRAINPOOL_P512.oid: ec.BrainpoolP512R1,
    }
    if curve.oid not in table:
        raise PgpError(f"The {curve.name} curve is not supported here.")
    return table[curve.oid]()


# ── Symmetric ciphers ──

AES128, AES192, AES256 = 7, 8, 9
SYMMETRIC_NAMES = {
    1: "IDEA", 2: "TripleDES", 3: "CAST5", 4: "Blowfish", 7: "AES-128", 8: "AES-192",
    9: "AES-256", 10: "Twofish", 11: "Camellia-128", 12: "Camellia-192", 13: "Camellia-256",
}
_KEY_SIZES = {1: 16, 2: 24, 3: 16, 4: 16, 7: 16, 8: 24, 9: 32, 11: 16, 12: 24, 13: 32}


def _legacy_cipher(name: str):
    try:
        from cryptography.hazmat.decrepit.ciphers import algorithms as decrepit
        return getattr(decrepit, name)
    except (ImportError, AttributeError):
        return getattr(algorithms, name, None)


def key_size(algorithm: int) -> int:
    if algorithm not in _KEY_SIZES:
        raise PgpError(f"The cipher {SYMMETRIC_NAMES.get(algorithm, algorithm)} is not supported.")
    return _KEY_SIZES[algorithm]


def block_size(algorithm: int) -> int:
    return 8 if algorithm in (1, 2, 3, 4) else 16


def cipher_algorithm(algorithm: int, key: bytes):
    if algorithm in (AES128, AES192, AES256):
        return algorithms.AES(key)
    if algorithm in (11, 12, 13):
        camellia = _legacy_cipher("Camellia")
        if camellia is None:
            raise PgpError("The Camellia cipher is not available on this server.")
        return camellia(key)
    legacy = {1: "IDEA", 2: "TripleDES", 3: "CAST5", 4: "Blowfish"}.get(algorithm)
    if legacy:
        cls = _legacy_cipher(legacy)
        if cls is None:
            raise PgpError(f"The old {legacy} cipher is not available on this server.")
        return cls(key)
    raise PgpError(f"The cipher {SYMMETRIC_NAMES.get(algorithm, algorithm)} is not supported.")


def _cfb_mode(iv: bytes):
    # Newer cryptography releases keep CFB with the older modes; use it from
    # there when present so no deprecation warning is raised.
    try:
        from cryptography.hazmat.decrepit.ciphers.modes import CFB
    except ImportError:
        CFB = modes.CFB  # noqa: N806
    return CFB(iv)


def cfb(algorithm: int, key: bytes, iv: bytes | None = None, *, encrypt: bool = False):
    """A CFB encryptor or decryptor (full-block feedback, OpenPGP's mode)."""
    iv = iv if iv is not None else b"\x00" * block_size(algorithm)
    cipher = Cipher(cipher_algorithm(algorithm, key), _cfb_mode(iv))
    return cipher.encryptor() if encrypt else cipher.decryptor()


# ── Hashes ──

HASH_NAMES = {1: "md5", 2: "sha1", 3: "ripemd160", 8: "sha256", 9: "sha384", 10: "sha512",
              11: "sha224", 12: "sha3_256", 14: "sha3_512"}
HASH_LABELS = {1: "MD5", 2: "SHA-1", 3: "RIPEMD-160", 8: "SHA-256", 9: "SHA-384", 10: "SHA-512",
               11: "SHA-224", 12: "SHA3-256", 14: "SHA3-512"}
HASH_SIZES = {1: 16, 2: 20, 3: 20, 8: 32, 9: 48, 10: 64, 11: 28, 12: 32, 14: 64}
# Collisions can be made for these, so a signature on a file that uses one
# proves nothing (RFC 9580 section 9.5). Key self-signatures made with SHA-1
# are still read, as OpenPGP.js and GnuPG do, because forging one needs far
# more than a collision.
WEAK_HASHES = {1, 2, 3}
SHA256 = 8
SHA384 = 9
SHA512 = 10


def prehashed(algorithm: int):
    """The ``cryptography`` hash object for a hash algorithm number."""
    from cryptography.hazmat.primitives import hashes

    table = {2: hashes.SHA1, 8: hashes.SHA256, 9: hashes.SHA384, 10: hashes.SHA512, 11: hashes.SHA224,
             12: hashes.SHA3_256, 14: hashes.SHA3_512}
    if algorithm not in table:
        raise PgpError(f"Hash algorithm {HASH_LABELS.get(algorithm, algorithm)} is not supported for signatures.")
    return table[algorithm]()


def new_hash(algorithm: int):
    name = HASH_NAMES.get(algorithm)
    if name is None:
        raise PgpError(f"Hash algorithm {algorithm} is not supported.")
    try:
        return hashlib.new(name)
    except ValueError:
        raise PgpError(f"The {name.upper()} hash is not available on this server.") from None


# ── String-to-key: turning a passphrase into a key ──

@dataclass
class S2K:
    kind: int
    hash_algorithm: int = 2
    salt: bytes = b""
    count: int = 0
    argon2: tuple[int, int, int] | None = None  # (passes, parallelism, memory exponent)
    gnu_mode: int = 0  # 1 = no secret part (stub), 2 = on a smartcard


def read_s2k(data: bytes, position: int) -> tuple[S2K, int]:
    if position >= len(data):
        raise PgpError("The key is cut short (its passphrase settings are missing).")
    kind = data[position]
    position += 1
    if kind == 0:
        return S2K(0, data[position]), position + 1
    if kind == 1:
        return S2K(1, data[position], data[position + 1:position + 9]), position + 9
    if kind == 3:
        coded = data[position + 9]
        count = (16 + (coded & 15)) << ((coded >> 4) + 6)
        return S2K(3, data[position], data[position + 1:position + 9], count), position + 10
    if kind == 4:
        salt = data[position:position + 16]
        passes, lanes, memory = data[position + 16], data[position + 17], data[position + 18]
        return S2K(4, salt=salt, argon2=(passes, lanes, memory)), position + 19
    if kind == 101:
        # GnuPG extension: hash octet, "GNU", mode (1 = no secret, 2 = smartcard).
        if data[position + 1:position + 4] != b"GNU":
            raise PgpError("The key uses an unknown passphrase scheme (101 without GNU).")
        mode = data[position + 4]
        if mode == 2:
            serial_length = data[position + 5] if position + 5 < len(data) else 0
            return S2K(101, gnu_mode=2), position + 6 + serial_length
        return S2K(101, gnu_mode=1), position + 5
    raise PgpError(f"The key uses passphrase scheme {kind}, which is not supported.")


def derive_key(s2k: S2K, passphrase: bytes, size: int) -> bytes:
    if s2k.kind == 4:
        try:
            from cryptography.hazmat.primitives.kdf.argon2 import Argon2id
        except ImportError:
            raise PgpError("This key's passphrase uses Argon2, which needs a newer cryptography package.") from None
        passes, lanes, memory_exponent = s2k.argon2 or (1, 1, 21)
        if memory_exponent > 22:
            raise PgpError("This key's passphrase setting asks for more than 4 GiB of memory; it is refused.")
        return Argon2id(salt=s2k.salt, length=size, iterations=passes, lanes=lanes,
                        memory_cost=1 << memory_exponent).derive(passphrase)
    if s2k.kind not in (0, 1, 3):
        raise PgpError("This key has no passphrase-protected secret part to unlock.")
    out = b""
    preload = 0
    while len(out) < size:
        digest = new_hash(s2k.hash_algorithm)
        digest.update(b"\x00" * preload)
        material = s2k.salt + passphrase
        if s2k.kind == 3:
            total = max(s2k.count, len(material))
            full, rest = divmod(total, len(material))
            # Hash the salted passphrase over and over until `total` octets.
            block = material * max(1, min(full, 4096))
            while full >= 4096:
                digest.update(block)
                full -= 4096
            if full:
                digest.update(material * full)
            digest.update(material[:rest])
        else:
            digest.update(material)
        out += digest.digest()
        preload += 1
    return out[:size]


# ── ECDH key derivation (RFC 6637 / RFC 9580 section 11.5) ──

ANONYMOUS_SENDER = b"Anonymous Sender    "


def ecdh_kek(shared: bytes, curve: Curve, kdf_hash: int, kek_algorithm: int, fingerprint: bytes) -> bytes:
    param = (
        bytes([len(curve.oid)]) + curve.oid + bytes([ECDH, 3, 1, kdf_hash, kek_algorithm])
        + ANONYMOUS_SENDER + fingerprint
    )
    digest = new_hash(kdf_hash)
    digest.update(b"\x00\x00\x00\x01" + shared + param)
    return digest.digest()[:key_size(kek_algorithm)]


def checksum(data: bytes) -> bytes:
    return (sum(data) & 0xFFFF).to_bytes(2, "big")
