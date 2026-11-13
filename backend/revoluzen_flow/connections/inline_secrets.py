"""Passwords and keys typed straight into a connection's settings.

A connection can take its password from a saved Credential or hold it in its
own settings. Credentials have been encrypted at rest since May 2026; a
connection's own settings were not, and the API sent them back to the browser
as they were typed. This module is the one place that decides which settings
are secret and what happens to them:

* On disk, every secret value is ``ENC:v1:<token>``, made by the same
  encryptor and master key as Credentials. Host, port, database and the other
  settings stay readable, so a row can still be recognised in DB tooling.
* On the server, ``ConnectionStore.get`` hands back the real value, because
  that is what drivers need: the run paths, the connection testers, the
  connection pool and report execution.
* In API responses the value is replaced by ``SECRET_PLACEHOLDER``. An edit
  that sends the placeholder back keeps the stored value; any other value
  replaces it.

A setting is secret when its name is one the encryptor protects for
Credentials, or one the Connections form shows as a password or a key.
``tests/test_connection_secrets.py`` reads the form and fails if a password
field is added there without being added here. A setting whose value holds a
PEM private key is secret whatever its name (2026-11-12), so a key pasted
into the wrong box, or a certificate file pasted whole with its key, is not
kept in clear or sent back.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Callable

from revoluzen_flow.security.encryptor import _ENC_PREFIX, _SENSITIVE_FIELD_NAMES

logger = logging.getLogger(__name__)


#: What an API response shows in place of a stored secret, and what the edit
#: form sends back to mean "keep the stored value".
SECRET_PLACEHOLDER = "\u2022" * 8

#: Fields the Connections form shows as a password or a key whose names the
#: encryptor's list does not already cover. ``credentials_json`` is a text
#: area, but it holds a service account's private key; so does Kafka's
#: ``ssl_key_pem`` (the client certificate's key, with ``ssl_key_password``).
_FORM_SECRET_FIELDS = frozenset({
    "secret_key", "hmac_secret", "custom_header_value",
    "personal_access_token", "integration_token", "api_token",
    "consumer_secret", "token_secret", "auth_token", "app_key",
    "routing_key", "webhook_url", "credentials_json",
    "ssl_key_pem", "ssl_key_password",
})

#: A PEM private key (PKCS#8, RSA, EC, OpenSSH, PGP) anywhere in a value.
_PRIVATE_KEY_TEXT = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----")


def holds_private_key(value: Any) -> bool:
    """Whether a setting's value holds a PEM private key, whatever its name."""
    return isinstance(value, str) and bool(_PRIVATE_KEY_TEXT.search(value))

#: Secret whatever the connection type.
SECRET_FIELDS: frozenset[str] = _SENSITIVE_FIELD_NAMES | _FORM_SECRET_FIELDS

#: Secret for one connection type only. A MongoDB URI carries the password
#: (``mongodb://user:password@host``) where a Neo4j bolt URI does not, and the
#: form treats only Salesforce's client id as a secret.
SECRET_FIELDS_BY_TYPE: dict[str, frozenset[str]] = {
    "mongodb": frozenset({"uri"}),
    "salesforce": frozenset({"client_id"}),
}

#: Settings whose values are names, never secrets. ``credential_field_map``
#: says which credential key fills which setting, as in
#: ``{"password": "db_password"}``.
_NAME_MAPS = frozenset({"credential_field_map"})


def secret_fields(conn_type: str | None) -> frozenset[str]:
    """Every setting name that holds a secret for this connection type."""
    return SECRET_FIELDS | SECRET_FIELDS_BY_TYPE.get((conn_type or "").lower(), frozenset())


def is_secret_field(name: Any, conn_type: str | None = None) -> bool:
    return isinstance(name, str) and name.lower() in secret_fields(conn_type)


def _map_secrets(
    config: Any,
    conn_type: str | None,
    change: Callable[[str, str], str],
    *,
    stored_too: bool = False,
    top: bool = True,
) -> Any:
    """Return a copy of ``config`` with ``change`` applied to every non-empty
    secret string. Nested settings are walked too, as the encryptor does for
    Credentials; the type-specific names apply at the top level only. A value
    that holds a PEM private key is a secret whatever its name.

    ``stored_too`` also takes any value already stored encrypted, whatever
    its name, so reading and masking never depend on the list of names
    staying as it was when the value was saved (a name leaving the list,
    or a connection changing type).
    """
    if not isinstance(config, dict):
        return config
    names = secret_fields(conn_type) if top else SECRET_FIELDS
    out: dict[str, Any] = {}
    for key, value in config.items():
        if key in _NAME_MAPS:
            out[key] = value
        elif isinstance(value, dict):
            out[key] = _map_secrets(value, conn_type, change, stored_too=stored_too, top=False)
        elif isinstance(value, str) and value and (
            (isinstance(key, str) and key.lower() in names)
            or (stored_too and value.startswith(_ENC_PREFIX))
            or holds_private_key(value)
        ):
            out[key] = change(key, value)
        else:
            out[key] = value
    return out


# ── At rest ─────────────────────────────────────────────────────────────


def encrypt_config(config: dict[str, Any], conn_type: str | None, encryptor) -> dict[str, Any]:
    """Encrypt every secret for saving. With no encryptor (the test-only
    ``REVOLUZEN_FLOW_DISABLE_ENCRYPTION=1``) the config is saved as it is,
    the same as Credentials.

    Values arrive decrypted, because ``ConnectionStore`` only ever saves what
    ``get`` handed out. Each one is encrypted once more; a secret that merely
    starts with ``ENC:v1:`` therefore survives, and ``decrypt_value`` reads
    through any extra layer an older bug may have left.
    """
    if encryptor is None:
        return dict(config or {})
    return _map_secrets(config or {}, conn_type, lambda _key, value: encryptor.encrypt_value(value))


def decrypt_config(
    config: dict[str, Any],
    conn_type: str | None,
    encryptor,
    *,
    connection_id: str = "?",
) -> dict[str, Any]:
    """Decrypt every secret for server-side use.

    A value the master key cannot read (the key file was replaced, or the
    database came from another install) is kept as stored, so restoring the
    original ``secret.key`` brings it back. It is never logged.
    """
    if encryptor is None:
        return dict(config or {})

    def _one(key: str, value: str) -> str:
        if not value.startswith(_ENC_PREFIX):
            return value  # stored before encryption; the startup pass encrypts it
        try:
            return encryptor.decrypt_value(value)
        except RuntimeError:
            logger.warning(
                "Connection %s: the stored %r cannot be decrypted with this "
                "install's master key and was left as stored. Restore the "
                "original secret.key, or enter the value again on the "
                "Connections page.", connection_id, key,
            )
            return value

    return _map_secrets(config or {}, conn_type, _one, stored_too=True)


def encrypt_plaintext_secrets(
    config: dict[str, Any],
    conn_type: str | None,
    encryptor,
) -> tuple[dict[str, Any], int]:
    """Encrypt the secrets still stored as typed. Returns the new config and
    how many values were encrypted.

    Used by the pass that runs at every server start, so it leaves alone any
    value that is already ``ENC:v1:`` (a second run changes nothing) and
    checks that each new token decrypts to the value it replaces.
    """
    encrypted = 0

    def _one(key: str, value: str) -> str:
        nonlocal encrypted
        if value.startswith(_ENC_PREFIX):
            return value
        token = encryptor.encrypt_value(value)
        if encryptor.decrypt_value(token) != value:
            raise RuntimeError(f"the encrypted {key!r} did not read back as stored")
        encrypted += 1
        return token

    return _map_secrets(config or {}, conn_type, _one), encrypted


# ── In API responses ────────────────────────────────────────────────────


def mask_config(config: dict[str, Any], conn_type: str | None) -> dict[str, Any]:
    """Replace every stored secret with ``SECRET_PLACEHOLDER``. An empty
    value stays empty, so the form can tell "not set" from "stored"."""
    return _map_secrets(config or {}, conn_type, lambda _key, _value: SECRET_PLACEHOLDER, stored_too=True)


def masked_connection(data: dict[str, Any]) -> dict[str, Any]:
    """A connection as the API returns it: everything but the secrets."""
    out = dict(data)
    out["config"] = mask_config(out.get("config") or {}, out.get("type"))
    return out


def connection_response(connection) -> dict[str, Any]:
    """``model_dump`` for a response body, with the secrets masked."""
    return masked_connection(connection.model_dump(mode="json"))


def keep_unchanged_secrets(incoming: dict[str, Any], stored: dict[str, Any]) -> dict[str, Any]:
    """Put the stored value back wherever an edit sent the placeholder.

    The edit form shows a stored secret as ``SECRET_PLACEHOLDER`` and sends it
    back untouched unless the user replaced it. A placeholder with nothing
    stored behind it is dropped rather than saved as if it were the password.
    Every other value, including an empty one, is what the user asked for.
    """
    if not isinstance(incoming, dict):
        return incoming
    stored = stored if isinstance(stored, dict) else {}
    out: dict[str, Any] = {}
    for key, value in incoming.items():
        before = stored.get(key)
        if value == SECRET_PLACEHOLDER:
            if before not in (None, ""):
                out[key] = before
        elif isinstance(value, dict) and key not in _NAME_MAPS:
            out[key] = keep_unchanged_secrets(value, before if isinstance(before, dict) else {})
        else:
            out[key] = value
    return out


def drop_placeholders(config: dict[str, Any]) -> dict[str, Any]:
    """For a new connection or an unsaved test, a placeholder stands for
    nothing: there is no stored value to keep."""
    return keep_unchanged_secrets(config or {}, {})
