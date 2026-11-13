"""Fixes by policy: what Flow may resume on its own, how often, after how long.

When an unattended run fails for a reason a retry can plausibly fix (a
network blip, a source that was down), the fix is usually "run it again
from where it stopped", and someone does that by hand hours later. This
policy lets Flow do it: after ``delay_seconds``, resume the failed run from
its checkpoint, at most ``max_attempts`` times per incident. Every attempt
is recorded on the incident's timeline, with the run it started.

What it will never do:

* Change the pipeline. A resume reruns the published version from its
  checkpoint; nothing about the pipeline is edited.
* Retry a failure a retry cannot fix. Bad data, a broken configuration and
  a crash (``data_quality``, ``user_input``, ``fatal``) are refused here,
  whatever the admin asks for, because resuming them only repeats the same
  failure on the same rows.
* Run unless an admin turned it on. Off by default.

Instance-wide, like the publishing policy (``api/publish_policy.py``), and
stored in the same admin settings row.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field, field_validator

SETTING_KEY = "auto_recover"

# The classes a resume can plausibly fix (engine/failure_class.retry_advisable),
# plus "unknown", which an admin may choose to include.
ALLOWED_CLASSES = ("transient", "dependency", "unknown")
NEVER_CLASSES = ("data_quality", "user_input", "fatal")


class AutoRecoverPolicy(BaseModel):
    enabled: bool = False
    failure_classes: list[str] = Field(default_factory=lambda: ["transient", "dependency"])
    max_attempts: int = Field(default=2, ge=1, le=5)
    delay_seconds: int = Field(default=300, ge=10, le=86_400)

    @field_validator("failure_classes")
    @classmethod
    def _only_retryable(cls, value: list[str]) -> list[str]:
        cleaned = list(dict.fromkeys(str(v).strip().lower() for v in value if str(v).strip()))
        refused = [v for v in cleaned if v in NEVER_CLASSES]
        if refused:
            raise ValueError(
                f"{', '.join(refused)} failures are never resumed automatically: "
                "a resume reruns the same rows with the same settings and fails the same way."
            )
        unknown = [v for v in cleaned if v not in ALLOWED_CLASSES]
        if unknown:
            raise ValueError(f"unknown failure class: {', '.join(unknown)}")
        return cleaned


def _db(app_state: dict | None = None):
    if app_state is None:
        from revoluzen_flow.main import app_state as _state  # type: ignore
        app_state = _state
    return app_state.get("db")


def _admin_settings(db) -> dict[str, Any]:
    try:
        row = db.fetchone("SELECT data FROM settings WHERE id = 'admin_settings'")
        data = json.loads(row["data"]) if row and row["data"] else {}
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 — a missing or bad row reads as "no policy"
        return {}


def load_policy(app_state: dict | None = None) -> AutoRecoverPolicy:
    db = _db(app_state)
    if db is None:
        return AutoRecoverPolicy()
    raw = _admin_settings(db).get(SETTING_KEY)
    if not isinstance(raw, dict):
        return AutoRecoverPolicy()
    try:
        return AutoRecoverPolicy(**raw)
    except Exception:  # noqa: BLE001 — a stored policy that no longer validates is off
        return AutoRecoverPolicy()


def save_policy(policy: AutoRecoverPolicy, app_state: dict | None = None) -> AutoRecoverPolicy:
    db = _db(app_state)
    if db is None:
        raise RuntimeError("settings store not initialized")
    existing = _admin_settings(db)
    existing[SETTING_KEY] = policy.model_dump()
    db.execute(
        "INSERT OR REPLACE INTO settings (id, data, created_at) VALUES ('admin_settings', ?, ?)",
        (json.dumps(existing), datetime.now(timezone.utc).isoformat()),
    )
    # execute() does not commit on this codebase (see api/publish_policy.py).
    db.commit()
    return policy


__all__ = ["ALLOWED_CLASSES", "NEVER_CLASSES", "AutoRecoverPolicy", "load_policy", "save_policy"]
