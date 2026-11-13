"""PGP keys kept in Flow's Credentials.

A PGP credential holds an armored private key and its passphrase (to decrypt
files sent to you and to sign files you send), or a partner's armored public
key (to encrypt files for them and to check their signatures), or both.
Credentials are encrypted at rest and masked in every API reply
(``credentials/sensitive_fields.py``): ``private_key`` and ``passphrase`` are
secret by name, and ``public_key`` ends in ``_key``.

Pipelines store only the credential's id. The key and passphrase are read
when the step runs, used in memory, and never written to a log, a run record
or a version.
"""

from __future__ import annotations

from typing import Any

from revoluzen_flow.openpgp.algorithms import PgpError
from revoluzen_flow.openpgp.keys import Key, read_keys

CREDENTIAL_TYPE = "pgp"

_PRIVATE_FIELDS = ("private_key", "pgp_private_key", "secret_key", "private_key_block")
_PUBLIC_FIELDS = ("public_key", "pgp_public_key", "public_keys", "recipient_key", "recipient_public_key",
                  "partner_public_key")
_PASSPHRASE_FIELDS = ("passphrase", "pgp_passphrase", "password", "key_passphrase")


def _first(config: dict[str, Any], names: tuple[str, ...]) -> str:
    for name in names:
        value = config.get(name)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def load_credential(app_state: dict[str, Any] | None, credential_id: str,
                    workspace_id: str | None) -> dict[str, Any]:
    """The credential's decrypted settings, scoped to the pipeline's workspace."""
    if not credential_id:
        raise PgpError("Choose the PGP key credential in the step's settings.")
    store = (app_state or {}).get("credential_store")
    if store is None:
        raise PgpError("Credentials are not available on this server.")
    credential = store.get_raw(credential_id, workspace_id=workspace_id or "default")
    if credential is None:
        raise PgpError("The PGP key credential this step uses no longer exists. Choose another in the step's settings.")
    return dict(credential.config or {})


def decryption_keys(config: dict[str, Any]) -> tuple[list[Key], str]:
    private = _first(config, _PRIVATE_FIELDS)
    if not private:
        raise PgpError(
            "The credential has no private key. To decrypt, add the field 'private_key' with your "
            "armored private key (-----BEGIN PGP PRIVATE KEY BLOCK-----)."
        )
    keys = [key for key in read_keys(private) if key.has_secret]
    if not keys:
        raise PgpError("The credential's 'private_key' holds a public key only. Decrypting needs the private key.")
    return keys, _first(config, _PASSPHRASE_FIELDS)


def encryption_keys(config: dict[str, Any]) -> list[Key]:
    public = _first(config, _PUBLIC_FIELDS)
    if public:
        return read_keys(public)
    private = _first(config, _PRIVATE_FIELDS)
    if private:
        # Encrypting to your own key, for example for an archive copy.
        return read_keys(private)
    raise PgpError(
        "The credential has no public key. To encrypt for a partner, add the field 'public_key' with "
        "their armored public key (-----BEGIN PGP PUBLIC KEY BLOCK-----)."
    )


def verification_keys(config: dict[str, Any]) -> list[Key]:
    """The sender's public key(s), to check the signature on a file they sent."""
    public = _first(config, _PUBLIC_FIELDS)
    if public:
        return read_keys(public)
    private = _first(config, _PRIVATE_FIELDS)
    if private:
        # Checking a file you signed yourself, for example an archive copy.
        return read_keys(private)
    raise PgpError(
        "The credential has no public key. To check a sender's signature, add the field 'public_key' with "
        "the sender's armored public key (-----BEGIN PGP PUBLIC KEY BLOCK-----)."
    )


def signing_keys(config: dict[str, Any]) -> tuple[list[Key], str]:
    """Your private key and its passphrase, to sign the files you send."""
    private = _first(config, _PRIVATE_FIELDS)
    if not private:
        raise PgpError(
            "The credential has no private key. To sign files, add the field 'private_key' with your "
            "armored private key (-----BEGIN PGP PRIVATE KEY BLOCK-----) and its passphrase."
        )
    keys = [key for key in read_keys(private) if key.has_secret]
    if not keys:
        raise PgpError("The credential's 'private_key' holds a public key only. Signing needs the private key.")
    return keys, _first(config, _PASSPHRASE_FIELDS)
