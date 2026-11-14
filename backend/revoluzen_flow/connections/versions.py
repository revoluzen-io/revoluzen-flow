"""A connection's version history.

Every change to a connection's settings keeps a version: its name, type,
settings, linked credential, project, environment, roles, description and
tags, with who changed it and when. A save that changes none of these (a
connection test writing its result, say) keeps none.

Secrets are never kept. Each password or key is stored as a keyed fingerprint
(``FP:v1:<hex>``, from ``Encryptor.fingerprint``), so the history can say
"password changed" and nothing more. Restoring an older version therefore
keeps today's password unless the person restoring types the old one in.

The newest ``CONNECTION_VERSION_RETENTION`` versions are kept, and the first
one always.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any

from .inline_secrets import _map_secrets  # the one walk over secret settings

logger = logging.getLogger(__name__)

#: What a version records about a connection.
VERSIONED_FIELDS = (
    "name", "type", "config", "credential_id", "project_id",
    "environment", "description", "tags", "capabilities",
)

#: How a secret's fingerprint is stored in a version's settings.
FP_PREFIX = "FP:v1:"

KINDS = ("saved", "restored")

_LABELS = {
    "name": "name", "type": "type", "credential_id": "linked credential",
    "project_id": "project", "environment": "environment",
    "description": "description", "tags": "tags", "capabilities": "roles",
}


def fingerprint(value: str, encryptor) -> str:
    """``FP:v1:<hex>`` for a secret. Without an encryptor (the test-only
    ``REVOLUZEN_FLOW_DISABLE_ENCRYPTION=1``) the fingerprint is an unkeyed
    hash, as weak as that mode's at-rest storage."""
    if encryptor is not None and hasattr(encryptor, "fingerprint"):
        return FP_PREFIX + encryptor.fingerprint(value)
    return FP_PREFIX + hashlib.sha256(b"unkeyed\x00" + (value or "").encode("utf-8")).hexdigest()


def snapshot(connection, encryptor) -> dict[str, Any]:
    """What a version keeps of a connection (from ``ConnectionStore.get``,
    so secrets arrive decrypted and leave as fingerprints)."""
    data = connection.model_dump(mode="json")
    snap = {k: data.get(k) for k in VERSIONED_FIELDS}
    snap["config"] = _map_secrets(
        snap.get("config") or {}, snap.get("type"),
        lambda _key, value: value if value.startswith(FP_PREFIX) else fingerprint(value, encryptor),
    )
    return snap


def _canonical(snap: dict[str, Any]) -> str:
    return json.dumps(snap, sort_keys=True, default=str, separators=(",", ":"))


def same(a: dict[str, Any] | None, b: dict[str, Any] | None) -> bool:
    """Whether two snapshots record the same connection."""
    return a is not None and b is not None and _canonical(a) == _canonical(b)


def _flat(value: Any, prefix: str = "") -> dict[str, Any]:
    """Settings as dotted keys, so nested settings compare one by one."""
    if not isinstance(value, dict):
        return {prefix: value}
    out: dict[str, Any] = {}
    for k, v in value.items():
        key = f"{prefix}.{k}" if prefix else str(k)
        if isinstance(v, dict) and v:
            out.update(_flat(v, key))
        else:
            out[key] = v
    return out


def _label(key: str) -> str:
    if key in _LABELS:
        return _LABELS[key]
    last = key.split(".")[-1]
    return last.replace("_", " ").strip().lower() or key


def changed_settings(before: dict[str, Any] | None, after: dict[str, Any]) -> list[str]:
    """The settings that differ between two snapshots, as people read them
    ("host", "password", "linked credential")."""
    if before is None:
        return []
    out: list[str] = []
    for field in VERSIONED_FIELDS:
        if field == "config":
            a, b = _flat(before.get("config") or {}), _flat(after.get("config") or {})
            for key in list(a) + [k for k in b if k not in a]:
                if a.get(key) != b.get(key):
                    out.append(_label(key))
        elif before.get(field) != after.get(field):
            out.append(_label(field))
    seen: set[str] = set()
    return [x for x in out if not (x in seen or seen.add(x))]


def describe(before: dict[str, Any] | None, after: dict[str, Any]) -> str:
    """A note for a version: "Host and password changed"."""
    names = changed_settings(before, after)
    if not names:
        return "Saved"
    if len(names) > 3:
        names = names[:3] + [f"{len(names) - 3} more"]
    text = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
    return text[:1].upper() + text[1:] + " changed"


class ConnectionVersions:
    """The ``connection_versions`` table."""

    def __init__(self, db):
        self._db = db

    def latest(self, connection_id: str) -> dict[str, Any] | None:
        row = self._db.fetchone(
            "SELECT version, data FROM connection_versions WHERE connection_id = ? ORDER BY version DESC LIMIT 1",
            (connection_id,),
        )
        if row is None:
            return None
        return {"version": row["version"], "data": json.loads(row["data"])}

    def _insert(self, connection, number: int, snap: dict[str, Any], created_by: str,
                note: str, kind: str, created_at: str | None) -> None:
        self._db.execute_write(
            "INSERT INTO connection_versions (connection_id, version, workspace_id, data, created_by, "
            "change_summary, kind, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                connection.id, number, getattr(connection, "workspace_id", None) or "default",
                json.dumps(snap, default=str), created_by, note[:200],
                kind if kind in KINDS else "saved",
                created_at or datetime.now(timezone.utc).isoformat(),
            ),
        )

    def record(
        self,
        connection,
        encryptor,
        *,
        before: dict[str, Any] | None = None,
        before_at: str | None = None,
        created_by: str = "user",
        note: str = "",
        kind: str = "saved",
    ) -> int | None:
        """Keep a version when the connection differs from its newest one.
        Returns the new version number, or None when nothing changed.

        ``before`` is the snapshot taken just before this save. A connection
        made before version history existed has no versions; its first change
        keeps ``before`` as version 1 (author unknown, dated ``before_at``,
        when it was last saved), so that change can be compared and undone.
        """
        snap = snapshot(connection, encryptor)
        latest = self.latest(connection.id)
        if latest is None and before is not None:
            if _canonical(before) == _canonical(snap):
                return None  # nothing changed, and no history to add to yet
            self._insert(connection, 1, before, "", "As it was when version history began", "saved", before_at)
            latest = {"version": 1, "data": before}
        if latest is not None and _canonical(latest["data"]) == _canonical(snap):
            return None
        number = (latest["version"] + 1) if latest else 1
        self._insert(
            connection, number, snap, created_by or "user",
            note or (describe(latest["data"], snap) if latest else "Created"),
            kind, None,
        )
        self._prune(connection.id)
        return number

    def _prune(self, connection_id: str) -> int:
        from revoluzen_flow import runtime_config
        keep = runtime_config.CONNECTION_VERSION_RETENTION_COUNT
        if keep <= 0:
            return 0
        rows = self._db.fetchall(
            "SELECT version FROM connection_versions WHERE connection_id = ? ORDER BY version DESC",
            (connection_id,),
        )
        numbers = [r["version"] for r in rows]
        drop = [n for n in numbers[keep:] if n != 1]
        if not drop:
            return 0
        marks = ",".join("?" for _ in drop)
        self._db.execute_write(
            f"DELETE FROM connection_versions WHERE connection_id = ? AND version IN ({marks})",
            (connection_id, *drop),
        )
        return len(drop)

    def list(self, connection_id: str) -> list[dict[str, Any]]:
        """Every kept version, newest first."""
        rows = self._db.fetchall(
            "SELECT version, data, created_by, change_summary, kind, created_at FROM connection_versions "
            "WHERE connection_id = ? ORDER BY version DESC",
            (connection_id,),
        )
        return [
            {
                "version": r["version"],
                "data": json.loads(r["data"]),
                # Empty for the version kept from before the history began:
                # who made that one is not known.
                "created_by": r["created_by"] or "",
                "change_summary": r["change_summary"] or "",
                "kind": r["kind"] or "saved",
                "created_at": r["created_at"] or "",
            }
            for r in rows
        ]

    def get(self, connection_id: str, version: int) -> dict[str, Any] | None:
        row = self._db.fetchone(
            "SELECT data FROM connection_versions WHERE connection_id = ? AND version = ?",
            (connection_id, version),
        )
        return json.loads(row["data"]) if row else None

    def delete_all(self, connection_id: str) -> None:
        self._db.execute_write("DELETE FROM connection_versions WHERE connection_id = ?", (connection_id,))


def public_settings(snap: dict[str, Any]) -> dict[str, Any]:
    """A version's settings as the API returns them: each secret becomes
    ``{"secret": true, "tag": "<12 hex>"}``. Two versions with the same tag
    hold the same secret; the value itself is not kept anywhere."""
    def _walk(value: Any) -> Any:
        if isinstance(value, dict):
            return {k: _walk(v) for k, v in value.items()}
        if isinstance(value, str) and value.startswith(FP_PREFIX):
            return {"secret": True, "tag": value[len(FP_PREFIX):len(FP_PREFIX) + 12]}
        return value
    out = dict(snap)
    out["config"] = _walk(snap.get("config") or {})
    return out


def restore_config(
    target: dict[str, Any],
    current: dict[str, Any],
    current_snap: dict[str, Any],
    typed: dict[str, Any],
    path: str = "",
) -> tuple[dict[str, Any], list[str], list[str]]:
    """The settings to save when restoring version ``target`` (a snapshot's
    config) over ``current`` (the connection's decrypted config).

    A secret the version shares with today's settings keeps today's value. A
    secret that differed takes the value the person typed for it, if any,
    else today's value. Returns the settings plus two lists of dotted setting
    names: secrets that kept today's value although the version had another
    (``kept``), and secrets the version had that cannot be restored because
    nothing is stored today and nothing was typed (``missing``).
    """
    out: dict[str, Any] = {}
    kept: list[str] = []
    missing: list[str] = []
    for key, value in (target or {}).items():
        dotted = f"{path}.{key}" if path else str(key)
        now = (current or {}).get(key)
        now_snap = (current_snap or {}).get(key)
        if isinstance(value, dict):
            sub, k, m = restore_config(
                value,
                now if isinstance(now, dict) else {},
                now_snap if isinstance(now_snap, dict) else {},
                typed,
                dotted,
            )
            out[key] = sub
            kept += k
            missing += m
        elif isinstance(value, str) and value.startswith(FP_PREFIX):
            if now_snap == value and isinstance(now, str):
                out[key] = now
            elif isinstance(typed.get(dotted), str) and typed[dotted]:
                out[key] = typed[dotted]
            elif isinstance(now, str) and now:
                out[key] = now
                kept.append(dotted)
            else:
                missing.append(dotted)
        else:
            out[key] = value
    return out, kept, missing
