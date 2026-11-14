"""SQLite-backed credential store with always-on encryption (May 4 2026).

Sensitive config fields are encrypted at rest using Fernet (AES-128-CBC +
HMAC-SHA256) — the encryptor is wired into app_state at startup for both
Free and Plus. The previous Plus-gated path that left OSS Free credentials
in plaintext is gone.

Tolerates legacy plaintext rows on read so existing OSS installs upgrade
without manual migration: the next save re-encrypts each row, and a pass at
server start encrypts what is still in clear.

Which values are secret, and how they are encrypted and masked, is decided
in ``sensitive_fields.py`` (November 2026): by name, by the form's
"sensitive" switch, or because the value is already stored encrypted.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from copy import deepcopy

from revoluzen_flow.connections.inline_secrets import SECRET_PLACEHOLDER

from .models import Credential
from .sensitive_fields import (
    decrypt_config,
    encrypt_config,
    field_key,
    keep_unchanged_secrets,
    marked_fields,
    mask_config,
    sensitive_fields,
)


class CredentialStore:
    """Credential store with secret masking, backed by SQLite.

    Sensitive fields (password, secret, api_key, token, …) are encrypted
    at rest via the always-on Fernet encryptor. Non-sensitive fields
    (host, port, database name) are kept plaintext so operators can
    identify rows in DB tooling without decrypting.
    """

    def __init__(self, db=None):
        self._db = db

    def set_db(self, db):
        self._db = db

    def _get_encryptor(self):
        """Get the always-on encryptor from app_state.

        May 4 2026: removed the `license_mgr.is_plus` gate — credentials
        are now encrypted in OSS Free too, matching what EDITION_MATRIX,
        COMPLIANCE.md, and the trust posture endpoint say.

        Returns None only when REVOLUZEN_FLOW_DISABLE_ENCRYPTION=1 is set (a
        test-only escape hatch) or when app_state hasn't been populated
        yet (e.g. unit tests constructing the store directly). In both
        cases the store falls back to plaintext storage.
        """
        try:
            from revoluzen_flow.main import app_state
            return app_state.get("encryptor")
        except Exception:
            return None

    def _save(self, credential: Credential):
        # Keep only the marked fields still in the config, in the form's spelling.
        credential.sensitive_fields = marked_fields(credential.sensitive_fields, credential.config)
        data = credential.model_dump(mode="json")

        # Revoluzen Flow: encrypt sensitive fields before saving
        encryptor = self._get_encryptor()
        if encryptor:
            data["config"] = encrypt_config(data.get("config", {}), credential.sensitive_fields, encryptor)

        self._db.insert_json(
            "credentials", credential.id, data,
            name=credential.name,
            type=credential.type,
            project_id=credential.project_id,
            workspace_id=credential.workspace_id or "default",
            created_at=credential.created_at.isoformat(),
            updated_at=credential.updated_at.isoformat(),
        )

    def create(self, credential: Credential) -> Credential:
        # A placeholder copied from a masked response has nothing stored
        # behind it on a new credential; never save it as the secret.
        credential.config = keep_unchanged_secrets(credential.config, {}, {})
        self._save(credential)
        return credential

    def get(self, credential_id: str, workspace_id: str | None = None) -> Credential | None:
        """Get a credential — returns None if cross-workspace access is attempted."""
        data = self._db.get_json("credentials", credential_id)
        if data is None:
            return None
        if workspace_id is not None:
            cred_ws = data.get("workspace_id") or "default"
            if cred_ws != workspace_id:
                return None
        return Credential(**data)

    def list_all(
        self,
        cred_type: str | None = None,
        project_id: str | None = None,
        workspace_id: str | None = None,
    ) -> list[dict]:
        items = self._db.list_json("credentials")
        result = []
        for c in items:
            if cred_type and c.get("type") != cred_type:
                continue
            if project_id and c.get("project_id") and c.get("project_id") != project_id:
                continue
            if workspace_id is not None:
                cred_ws = c.get("workspace_id") or "default"
                if cred_ws != workspace_id:
                    continue
            # Mask sensitive config fields, and say which they are so the
            # form can show its switches.
            config = c.get("config") or {}
            c["config"] = mask_config(config, c.get("sensitive_fields"))
            c["sensitive_fields"] = sensitive_fields(config, c.get("sensitive_fields"))
            result.append(c)
        return sorted(result, key=lambda x: x.get("name", ""))

    def get_raw(
        self,
        credential_id: str,
        workspace_id: str | None = None,
    ) -> Credential | None:
        """Get credential with unmasked config (for engine use).

        Revoluzen Flow: decrypts encrypted fields before returning.

        If `workspace_id` is provided, enforces the tenant boundary and
        returns None on cross-workspace access. Engine callsites that
        already have a Connection on hand should pass
        `connection.workspace_id` here so a compromised or misconfigured
        pipeline can't deref a credential from another tenant.
        """
        data = self._db.get_json("credentials", credential_id)
        if data is None:
            return None

        if workspace_id is not None:
            cred_ws = data.get("workspace_id") or "default"
            if cred_ws != workspace_id:
                return None

        # Revoluzen Flow: decrypt every value stored encrypted, whatever its name
        encryptor = self._get_encryptor()
        if encryptor:
            data["config"] = decrypt_config(data.get("config", {}), encryptor)

        return Credential(**data)

    def update(
        self,
        credential_id: str,
        updates: dict,
        workspace_id: str | None = None,
    ) -> Credential | None:
        # Decrypted, because _save encrypts: saving the stored form would
        # encrypt every secret a second time and leave it unreadable.
        credential = self.get_raw(credential_id, workspace_id=workspace_id)
        if not credential:
            return None
        stored = self.get(credential_id, workspace_id=workspace_id)
        kept_secret: set[str] = set()
        for key, value in updates.items():
            # Refuse to cross workspace boundary via an update body —
            # workspace membership of a credential is immutable once set
            # (an admin moving secrets between tenants must delete and
            # recreate, which produces an audit trail).
            if key == "workspace_id":
                continue
            if key == "config" and isinstance(value, dict):
                kept_secret = {k for k, v in value.items() if v == SECRET_PLACEHOLDER and k in credential.config}
                value = self._keep_unchanged_secrets(value, credential.config, stored.config)
            if value is not None and hasattr(credential, key):
                setattr(credential, key, value)
        if kept_secret:
            # A secret kept as it was stays secret, whatever marks came with the
            # edit: the value was never shown, so it is never stored in clear.
            credential.sensitive_fields = sorted(
                set(credential.sensitive_fields) | {field_key(k) for k in kept_secret}
            )
        credential.updated_at = datetime.now(timezone.utc)
        self._save(credential)
        return self.get(credential_id, workspace_id=workspace_id)

    @classmethod
    def _keep_unchanged_secrets(cls, incoming: dict, plain: dict, stored: dict) -> dict:
        """Treat a value the edit form was shown as "leave this as it is".

        The form pre-fills each stored secret with the placeholder the API
        returned. Saving that as it is would replace the real secret with the
        placeholder; see ``sensitive_fields.keep_unchanged_secrets``.
        """
        return keep_unchanged_secrets(incoming, plain, stored)

    def delete(self, credential_id: str, workspace_id: str | None = None) -> bool:
        # Pre-check via get so cross-tenant delete is a no-op + 404
        # rather than silently removing the row from another workspace.
        if workspace_id is not None:
            if not self.get(credential_id, workspace_id=workspace_id):
                return False
        return self._db.delete_row("credentials", credential_id)

    def mark_used(self, credential_id: str, workspace_id: str | None = None) -> None:
        """Update last_used timestamp — scoped when workspace_id provided."""
        # Decrypted for the same reason as update().
        cred = self.get_raw(credential_id, workspace_id=workspace_id)
        if cred:
            cred.last_used = datetime.now(timezone.utc)
            self._save(cred)

    def count(self) -> int:
        return self._db.count("credentials")

    @staticmethod
    def _mask_config(config: dict, marked: list[str] | None = None) -> dict:
        """Replace every secret in ``config`` with the placeholder. No
        character of a secret is ever shown."""
        return mask_config(config, marked)
