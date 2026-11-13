"""OpenPGP for Flow: decrypting incoming files, encrypting outgoing ones,
checking a partner's signature and signing our own files.

Built on the ``cryptography`` package (Apache-2.0 or BSD), which Flow already
depends on; no other PGP library or GnuPG install is needed. Scope:

* Keys: version 4 and version 6 (RFC 9580). RSA, Elgamal, ECDH (Curve25519,
  NIST P-256/384/521, Brainpool), X25519 and X448 for encryption; RSA, DSA,
  ECDSA, EdDSA, Ed25519 and Ed448 for signatures. Secret keys protected the
  way GnuPG exports them (iterated and salted S2K, AES or the older ciphers)
  or with AEAD and Argon2 (RFC 9580).
* Messages: public-key and passphrase session keys (versions 3 to 6);
  integrity-protected data version 1 (MDC checked), GnuPG's OCB packet and
  version 2 (RFC 9580: OCB, GCM, EAX); ZIP / ZLIB / BZip2 compression;
  armored or binary.
* Signatures: a partner's signature on a file (one-pass signed, inside the
  encryption or on its own) is checked against their public key; files
  can be signed with our private key. Binary and text signatures, version 4
  and 6.
* Writing stays with what every tool reads: SEIPD version 1 with AES-256 to
  version 4 keys. Encrypting to version 6 keys is not supported yet.
"""

from revoluzen_flow.openpgp.algorithms import PgpError
from revoluzen_flow.openpgp.keys import Key, read_keys, summary
from revoluzen_flow.openpgp.message import DecryptResult, decrypt_file, decrypt_stream, encrypt_file, encrypt_stream
from revoluzen_flow.openpgp.signatures import SignatureCheck, Signer

ENCRYPTED_EXTENSIONS = (".pgp", ".gpg", ".asc")

__all__ = [
    "PgpError", "Key", "read_keys", "summary", "DecryptResult", "decrypt_file", "decrypt_stream",
    "encrypt_file", "encrypt_stream", "SignatureCheck", "Signer", "ENCRYPTED_EXTENSIONS",
]
