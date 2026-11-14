"""Which values in a saved credential are secret, and how they are kept.

Until November 2026 the Credentials store encrypted only the encryptor's
fixed list of key names, and masked an even shorter list by showing the first
two characters of each value. A field the person named themselves, such as
``db_password`` or ``api_token``, was stored in clear and sent back to the
browser as typed, and the form's "sensitive" switch never reached the server.

A value is secret when either:

* its name says so. That is a name the server always protects
  (``SECRET_FIELDS`` from ``connections/inline_secrets.py``, which includes the
  encryptor's list), or a name containing password, passphrase, secret, token,
  api key, private key, connection string or credentials json, or ending in
  ``_key``, unless it ends in ``_id``, ``_url``, ``_uri``, ``_name``,
  ``_type``, ``_endpoint``, ``_path``, ``_header`` or ``_param``;
* or the person marked it sensitive in the form. The credential keeps those
  field names in ``sensitive_fields``.

Secrets are encrypted at rest (``ENC:v1:``) with the master key, decrypted
only by ``CredentialStore.get_raw`` for server-side use, and shown everywhere
else as ``SECRET_PLACEHOLDER``. A value already stored encrypted is decrypted
and masked whatever its name, so the rules can change without stranding a
value. The Credentials form mirrors the name rule in
``frontend/src/utils/storedSecrets.ts`` to lock its switch;
``tests/test_credential_secrets.py`` holds the two to the same answers.
"""

from __future__ import annotations

from typing import Any, Callable, Iterable

from revoluzen_flow.connections.inline_secrets import SECRET_FIELDS, SECRET_PLACEHOLDER
from revoluzen_flow.security.encryptor import _ENC_PREFIX

_SECRET_WORDS = (
    "password", "passwd", "passphrase", "secret", "token", "apikey", "api_key",
    "private_key", "connection_string", "credentials_json",
)
_NOT_SECRET_ENDINGS = (
    "_id", "_url", "_uri", "_name", "_type", "_endpoint", "_path", "_header", "_param",
)


def field_key(name: Any) -> str:
    """How the form saves a field name: trimmed, lower case, spaces as underscores."""
    return name.strip().lower().replace(" ", "_") if isinstance(name, str) else ""


def name_says_secret(name: Any) -> bool:
    key = field_key(name)
    if not key:
        return False
    if key in SECRET_FIELDS:
        return True
    if key.endswith(_NOT_SECRET_ENDINGS):
        return False
    return key == "key" or key.endswith("_key") or any(word in key for word in _SECRET_WORDS)


def _is_encrypted(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(_ENC_PREFIX)


def marked_fields(marked: Iterable[Any] | None, config: dict[str, Any] | None) -> list[str]:
    """What the credential keeps as ``sensitive_fields``: the names the person
    marked that are still in ``config``, in the form's spelling."""
    present = {field_key(key) for key in (config or {})}
    chosen = {field_key(name) for name in (marked or [])}
    return sorted((chosen & present) - {""})


def sensitive_fields(config: dict[str, Any] | None, marked: Iterable[Any] | None) -> list[str]:
    """Every field the API shows as secret: by its name, marked by the person,
    or already stored encrypted. The form sets its switches from this."""
    chosen = {field_key(name) for name in (marked or [])}
    return sorted(
        key for key, value in (config or {}).items()
        if name_says_secret(key) or field_key(key) in chosen or _is_encrypted(value)
    )


def _map_secrets(
    config: Any,
    chosen: set[str],
    change: Callable[[str, Any], Any],
    *,
    stored_too: bool,
    top: bool = True,
) -> Any:
    """Return a copy of ``config`` with ``change`` applied to each non-empty
    secret value. Nested settings are walked by name only; what the person
    marked applies to the top level, where the form puts every field."""
    if not isinstance(config, dict):
        return config
    out: dict[str, Any] = {}
    for key, value in config.items():
        if isinstance(value, dict):
            out[key] = _map_secrets(value, chosen, change, stored_too=stored_too, top=False)
            continue
        secret = name_says_secret(key) or (top and field_key(key) in chosen)
        if value not in (None, "") and (secret or (stored_too and _is_encrypted(value))):
            out[key] = change(key, value)
        else:
            out[key] = value
    return out


def encrypt_config(config: dict[str, Any], marked: Iterable[Any] | None, encryptor) -> dict[str, Any]:
    """Encrypt every secret text value for saving. Values arrive decrypted
    (the store saves what ``get_raw`` handed out), so each is encrypted once;
    a secret that merely starts with ``ENC:v1:`` survives, because
    ``decrypt_value`` reads through any extra layer. Non-text values are kept
    as they are, the same as the encryptor, and masked on the way out."""
    if encryptor is None:
        return dict(config or {})
    chosen = {field_key(name) for name in (marked or [])}
    return _map_secrets(
        config or {}, chosen,
        lambda _key, value: encryptor.encrypt_value(value) if isinstance(value, str) else value,
        stored_too=False,
    )


def decrypt_config(config: dict[str, Any], encryptor) -> dict[str, Any]:
    """Decrypt every value stored as ``ENC:v1:``, whatever its name. A value
    the master key cannot read raises, as ``Encryptor.decrypt_value`` always
    has for credentials."""
    if encryptor is None:
        return dict(config or {})
    return _map_secrets(
        config or {}, set(),
        lambda _key, value: encryptor.decrypt_value(value) if _is_encrypted(value) else value,
        stored_too=True,
    )


def encrypt_plaintext_secrets(
    config: dict[str, Any],
    marked: Iterable[Any] | None,
    encryptor,
) -> tuple[dict[str, Any], int]:
    """Encrypt the secret text values still stored as typed. Returns the new
    config and how many values were encrypted. For the pass that runs at every
    server start: a value already ``ENC:v1:`` is left alone, so a second run
    changes nothing, and each new token must read back as the value it
    replaces."""
    encrypted = 0
    chosen = {field_key(name) for name in (marked or [])}

    def _one(key: str, value: Any) -> Any:
        nonlocal encrypted
        if not isinstance(value, str) or _is_encrypted(value):
            return value
        token = encryptor.encrypt_value(value)
        if encryptor.decrypt_value(token) != value:
            raise RuntimeError(f"the encrypted {key!r} did not read back as stored")
        encrypted += 1
        return token

    return _map_secrets(config or {}, chosen, _one, stored_too=False), encrypted


def mask_config(config: dict[str, Any], marked: Iterable[Any] | None) -> dict[str, Any]:
    """Replace every stored secret with ``SECRET_PLACEHOLDER``. An empty value
    stays empty, so the form can tell "not set" from "stored"."""
    chosen = {field_key(name) for name in (marked or [])}
    return _map_secrets(config or {}, chosen, lambda _key, _value: SECRET_PLACEHOLDER, stored_too=True)


def credential_response(credential) -> dict[str, Any]:
    """A credential as the API returns it: secrets masked, plus the fields the
    form should show as sensitive."""
    data = credential.model_dump(mode="json")
    config = data.get("config") or {}
    data["config"] = mask_config(config, credential.sensitive_fields)
    data["sensitive_fields"] = sensitive_fields(config, credential.sensitive_fields)
    return data


# What the API showed before November 2026, kept only so that a page opened
# before the upgrade can still save without replacing the real secret.
_LEGACY_MASKED = frozenset({"password", "secret", "secret_access_key", "api_key", "token", "private_key"})


def _legacy_mask(key: str, value: Any) -> Any:
    if isinstance(value, str) and isinstance(key, str) and key.lower() in _LEGACY_MASKED:
        return "***" if len(value) <= 4 else value[:2] + "***"
    return value


def keep_unchanged_secrets(
    incoming: dict[str, Any],
    plain: dict[str, Any],
    stored: dict[str, Any],
) -> dict[str, Any]:
    """Treat a value the edit form was shown as "leave this as it is".

    The form shows a stored secret as ``SECRET_PLACEHOLDER`` and sends it back
    unless the person replaced it; the stored value is kept. A placeholder
    with nothing stored behind it is dropped rather than saved as the secret.
    Pages opened before November 2026 showed a two-character mask or the
    ciphertext; those are recognised too.
    """
    plain = plain or {}
    stored = stored or {}
    kept: dict[str, Any] = {}
    for key, value in (incoming or {}).items():
        if value == SECRET_PLACEHOLDER:
            if plain.get(key) not in (None, ""):
                kept[key] = plain[key]
            continue
        if key in plain and value in (
            stored.get(key), _legacy_mask(key, stored.get(key)), _legacy_mask(key, plain[key]),
        ):
            kept[key] = plain[key]
            continue
        kept[key] = value
    return kept
