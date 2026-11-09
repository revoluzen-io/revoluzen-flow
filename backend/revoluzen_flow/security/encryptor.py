"""
Encryptor — Fernet-backed encryption for credentials + AI provider API keys.

Replaces the previous Plus-gated encryption: from May 4 2026, every
install (Free OR Plus) encrypts credential blobs and provider API keys
at rest. The trust posture, COMPLIANCE.md, and product knowledge can
honestly say "encrypted at rest" without contradiction with the code.

## What this is

Standard `cryptography.fernet.Fernet`:
  * Cipher:  AES-128-CBC for confidentiality
  * MAC:     HMAC-SHA256 for integrity (authenticated encryption)
  * Format:  URL-safe base64 over `version | timestamp | iv | ciphertext | hmac`
  * Key:     32 bytes (256 bits), URL-safe base64 encoded

Fernet is the recommended high-level interface in the `cryptography`
library — it's hard to misuse, it's not subject to the AEAD nonce-reuse
foot-guns of raw GCM, and the format is versioned so we can rotate
without breaking old ciphertexts.

## Master key

One 32-byte symmetric key per install, stored at `~/.revoluzen-flow/secret.key`
(or `$REVOLUZEN_FLOW_DATA_DIR/secret.key` if `REVOLUZEN_FLOW_DATA_DIR` is set). The
file is created on first startup if missing; subsequent starts re-read
it. POSIX permissions are verified — Revoluzen Flow refuses to start if the
key file is world-readable on a POSIX filesystem (fail-closed, no
fallback).

## Migration from plaintext

Two legacy formats existed on installs predating May 4 2026:
  * Credentials store: raw dicts, no marker (`{"password": "abc123"}`)
  * AI config store: `PLAIN:<plaintext>` sentinel

Both are tolerated on READ. The very next WRITE re-encrypts. Operators
running an admin migration script (see `docs/admin/encrypt_existing.md`)
can force re-encryption of every stored credential without an edit.

## Sentinel format

Encrypted values are stored with a versioned prefix:
  * Single value:  `ENC:v1:<fernet_token>`
  * Whole config:  the JSON-serialised dict where every sensitive value
                   is `ENC:v1:...`. Non-sensitive fields stay plaintext
                   (host, port, database name) so an operator looking
                   at the SQLite blob can still recognise the row.

## Sensitive field names

The credential `encrypt_config` only encrypts fields whose key matches
this allowlist. Adding a new sensitive field name here makes it
encrypted on the next save without a migration:

    password, secret, key, token, private_key, api_key, apikey,
    client_secret, passphrase, access_token, refresh_token, sasl_password

Database `host`, `port`, `database`, `user`/`username` stay plaintext —
they're not secrets and operators need to read them in DB tooling for
incident response.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import secrets
import stat
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)


_ENC_PREFIX = "ENC:v1:"
_PLAIN_PREFIX = "PLAIN:"   # legacy AI config

_SENSITIVE_FIELD_NAMES = frozenset({
    "password", "secret", "key", "token", "private_key",
    "api_key", "apikey", "client_secret", "passphrase",
    "access_token", "refresh_token", "sasl_password",
    "private_key_pem", "service_account_json",
    "secret_access_key", "aws_secret_access_key", "session_token",
    "account_key", "sas_token", "connection_string",
})


# ─────────────────────────────────────────────────────────────────────
# Master key management
# ─────────────────────────────────────────────────────────────────────


def master_key_path() -> Path:
    """Resolve the path of the symmetric master key file.

    Order:
      1. `REVOLUZEN_FLOW_MASTER_KEY_FILE` env var (operator override)
      2. `<REVOLUZEN_FLOW_DATA_DIR>/secret.key`
      3. `~/.revoluzen-flow/secret.key`
    """
    explicit = os.environ.get("REVOLUZEN_FLOW_MASTER_KEY_FILE", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    data_dir = os.environ.get("REVOLUZEN_FLOW_DATA_DIR", "").strip()
    if data_dir:
        return Path(data_dir).expanduser() / "secret.key"
    return Path.home() / ".revoluzen-flow" / "secret.key"


#: Paths whose ACL this process has already settled. Callers construct an
#: Encryptor freely (`Encryptor.from_master_key()` is not cached), and
#: reading an NTFS DACL through pywin32 on every one of those would be a
#: pointless cost — the answer cannot change without someone editing the
#: ACL out from under a running process.
_WINDOWS_ACL_SETTLED: set[str] = set()


def _harden_existing_key_windows(path: Path) -> None:
    """Lock down a key file that already exists, at load time.

    Creation has hardened the key since 2026-10-26, but that only ever
    helped NEW installs. An install created before then — or one whose
    data directory was later moved to a shared location — kept its
    inherited ACL until someone opened Settings → Security & Diagnostics
    and pressed "Fix this". Most operators never will, and a key that
    decrypts every stored credential should not be waiting on a page
    visit. So the load path repairs it.

    This does NOT refuse to start the way the POSIX branch does, and the
    difference is deliberate. On POSIX the app owns the file it created
    and `chmod 600` essentially cannot fail, so refusing is safe. On
    Windows the app may legitimately lack the right to rewrite the DACL
    (a service account that is not the owner, a locked-down share), and
    refusing there would turn a hardening improvement into an outage for
    installs that work today. When hardening does not take, the process
    starts, logs loudly, and the Security & Diagnostics page still shows
    the control as failed with the exact command to run.
    """
    key = str(path)
    if key in _WINDOWS_ACL_SETTLED:
        return
    try:
        from revoluzen_flow.security.win_acl import harden_file, read_acl

        acl = read_acl(path)
        if acl is None:
            # pywin32 missing — we cannot tell, so we do not touch the
            # file. Hardening blind could strip an ACE the deployment
            # needs, and the page already reports this as `not_checked`.
            _WINDOWS_ACL_SETTLED.add(key)
            return
        if not acl["exposed"]:
            _WINDOWS_ACL_SETTLED.add(key)
            return

        exposed = ", ".join(label for _sid, label in acl["exposed"][:4])
        logger.warning(
            "Revoluzen Flow: master key %s is readable by %s. Tightening its ACL.",
            path, exposed,
        )
        harden_file(path)

        after = read_acl(path)
        if after is not None and not after["exposed"]:
            logger.info(
                "Revoluzen Flow: master key %s restricted to SYSTEM, "
                "Administrators and the owner.", path,
            )
        else:
            still = after["exposed"] if after else []
            logger.error(
                "Revoluzen Flow: could NOT restrict master key %s — still "
                "readable by %s. Fix it on Settings -> Security & Diagnostics, "
                "or run: icacls \"%s\" /inheritance:r /grant:r "
                "\"*S-1-5-18:(F)\" \"*S-1-5-32-544:(F)\"",
                path, ", ".join(lbl for _s, lbl in still[:4]) or "other principals",
                path,
            )
    except Exception:  # noqa: BLE001 — hardening must never block startup
        logger.debug("Windows key hardening skipped", exc_info=True)
    finally:
        _WINDOWS_ACL_SETTLED.add(key)


def _verify_perms(path: Path) -> None:
    """Refuse world-readable key files on POSIX; repair them on Windows.

    The two platforms differ because what the app can guarantee differs —
    see `_harden_existing_key_windows` for why Windows repairs instead of
    refusing.
    """
    if os.name == "nt":
        _harden_existing_key_windows(path)
        return
    if os.name != "posix":
        return
    try:
        mode = path.stat().st_mode
    except OSError as exc:
        raise RuntimeError(
            f"Cannot stat master key file {path}: {exc}"
        ) from exc
    # World/group readable bits set?
    if mode & (stat.S_IRGRP | stat.S_IROTH | stat.S_IWGRP | stat.S_IWOTH):
        raise RuntimeError(
            f"Master key file {path} is group/world readable "
            f"(mode={oct(mode & 0o777)}). Refuse to start. "
            f"Run: chmod 600 {path}"
        )


def load_or_create_master_key() -> bytes:
    """Read the master key, generating one on first run.

    Returns the 32-byte URL-safe-base64 key Fernet expects. Creates the
    parent directory with mode 700 if missing. The key file itself is
    chmod 600 on POSIX.
    """
    path = master_key_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        try:
            os.chmod(path.parent, 0o700)
        except OSError:
            # Best-effort — some filesystems (FAT32, ACL-only) reject chmod.
            pass

    if path.is_file():
        _verify_perms(path)
        key = path.read_bytes().strip()
        # Sanity: Fernet keys are 44 chars (32 bytes base64). Fail loud if
        # someone wrote a different file at this path.
        if len(key) < 32:
            raise RuntimeError(
                f"Master key file {path} is too short to be a Fernet key. "
                f"Either restore a backup or delete the file to regenerate."
            )
        return key

    # First run — generate and persist.
    key = Fernet.generate_key()
    path.write_bytes(key)
    hardened = "unrestricted"
    if os.name == "posix":
        try:
            os.chmod(path, 0o600)
            hardened = "mode 600"
        except OSError:
            pass
    elif os.name == "nt":
        # Windows got no hardening at all until 2026-10-26: the key was
        # written with whatever ACL it inherited from the data directory,
        # so pointing REVOLUZEN_FLOW_DATA_DIR at a shared location handed
        # the key that decrypts every stored credential to whoever could
        # read that directory — silently, because nothing checked either.
        from revoluzen_flow.security.win_acl import harden_file
        if harden_file(path):
            hardened = "restricted to SYSTEM, Administrators and the owner"
    logger.info(
        "Revoluzen Flow: created new master key at %s (%s). "
        "Back this file up — without it, encrypted credentials are unrecoverable.",
        path, hardened,
    )
    return key


# ─────────────────────────────────────────────────────────────────────
# Encryptor
# ─────────────────────────────────────────────────────────────────────


class Encryptor:
    """Fernet-based credential + API-key encryptor.

    Construct via `Encryptor.from_master_key()` — the loader handles
    file location, permission check, and first-run generation.
    """

    def __init__(self, key: bytes) -> None:
        try:
            self._fernet = Fernet(key)
        except (TypeError, ValueError, base64.binascii.Error) as exc:
            raise RuntimeError(
                f"Invalid Fernet key: {exc}. Master key file may be "
                f"corrupt; restore from backup."
            ) from exc

    # ── Construction helpers ────────────────────────────────────────

    @classmethod
    def from_master_key(cls) -> "Encryptor":
        """Load (or create) the master key and return an Encryptor."""
        return cls(load_or_create_master_key())

    # ── Single-value API ────────────────────────────────────────────

    def encrypt_value(self, plaintext: str) -> str:
        """Return `ENC:v1:<token>`. Empty input returns empty."""
        if not plaintext:
            return ""
        token = self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")
        return f"{_ENC_PREFIX}{token}"

    def decrypt_value(self, ciphertext: str) -> str:
        """Reverse of encrypt_value. Tolerates legacy formats:
          * `ENC:v1:<token>`  → decrypt
          * `PLAIN:<value>`   → return value (legacy AI config sentinel)
          * empty             → empty
          * other             → return as-is (treat as plaintext for
                                 backward compatibility with very old
                                 OSS installs that had no encryption)
        """
        if not ciphertext:
            return ""
        if ciphertext.startswith(_ENC_PREFIX):
            try:
                value = self._decrypt_token(ciphertext)
            except InvalidToken as exc:
                raise RuntimeError(
                    "Decryption failed — master key may have changed since "
                    "this credential was saved. Restore the original "
                    "secret.key or re-enter the credential."
                ) from exc
            # Earlier versions encrypted a stored secret again each time its
            # credential was used or edited, so a row can hold several layers.
            # Reading through them repairs those rows; the next save stores one.
            while value.startswith(_ENC_PREFIX):
                try:
                    value = self._decrypt_token(value)
                except InvalidToken:
                    break  # a secret that merely starts with the prefix
            return value
        if ciphertext.startswith(_PLAIN_PREFIX):
            return ciphertext[len(_PLAIN_PREFIX):]
        # Untyped plaintext — only happens on installs that pre-date this
        # encryptor entirely. Return as-is; the next save will encrypt.
        return ciphertext

    def _decrypt_token(self, value: str) -> str:
        return self._fernet.decrypt(value[len(_ENC_PREFIX):].encode("ascii")).decode("utf-8")

    # ── Config-dict API ─────────────────────────────────────────────

    def encrypt_config(self, config: dict[str, Any]) -> dict[str, Any]:
        """Return a copy of `config` with sensitive fields encrypted in
        place. Non-sensitive fields (host, port, database, user) are
        passed through unchanged so operators can identify rows in
        DB tooling without decrypting."""
        if not isinstance(config, dict):
            return config
        out: dict[str, Any] = {}
        for k, v in config.items():
            if self._is_sensitive_field(k) and isinstance(v, str) and v:
                out[k] = self.encrypt_value(v)
            elif isinstance(v, dict):
                out[k] = self.encrypt_config(v)
            else:
                out[k] = v
        return out

    def decrypt_config(self, config: dict[str, Any]) -> dict[str, Any]:
        """Reverse of encrypt_config. Tolerant of mixed plaintext +
        ciphertext rows produced during the migration window."""
        if not isinstance(config, dict):
            return config
        out: dict[str, Any] = {}
        for k, v in config.items():
            if self._is_sensitive_field(k) and isinstance(v, str) and v:
                out[k] = self.decrypt_value(v)
            elif isinstance(v, dict):
                out[k] = self.decrypt_config(v)
            else:
                out[k] = v
        return out

    # ── Helpers ─────────────────────────────────────────────────────

    @staticmethod
    def _is_sensitive_field(name: str) -> bool:
        if not isinstance(name, str):
            return False
        return name.lower() in _SENSITIVE_FIELD_NAMES


# ─────────────────────────────────────────────────────────────────────
# Module-level convenience for tests + scripts
# ─────────────────────────────────────────────────────────────────────


def fresh_encryptor_for_tests() -> Encryptor:
    """Create an Encryptor with an ephemeral key (not persisted). Useful
    in unit tests that need encryption without touching the user's home dir."""
    return Encryptor(secrets.token_bytes(32) and Fernet.generate_key())
