"""Tells people when a saved credential is about to expire, or has.

The Credentials page warned only whoever happened to open it. A password that lapses
over a weekend fails every pipeline using it on Monday, so the warning goes to the bell
and out over the notification channels, a week ahead, a day ahead, and on expiry.
"""

from __future__ import annotations

import logging
import math
import threading
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

WARN_DAYS = 7


def expiry_band(expires_at: datetime, now: datetime) -> tuple[str, int] | None:
    """Which warning a credential is due, and the days left. None if none is due yet."""
    days_left = math.ceil((expires_at - now).total_seconds() / 86400)
    if expires_at <= now:
        return "expired", days_left
    if days_left <= 1:
        return "1", days_left
    if days_left <= WARN_DAYS:
        return "7", days_left
    return None


def _parse(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def check_credential_expiry(app_state: dict, now: datetime | None = None) -> int:
    """Send any warning that's due. Returns how many credentials are in a warning band."""
    store = app_state.get("credential_store")
    notifier = app_state.get("approval_notifier")
    if store is None or notifier is None:
        return 0
    now = now or datetime.now(timezone.utc)
    due = 0
    for credential in store.list_all():
        expires_at = _parse(credential.get("expires_at"))
        if expires_at is None:
            continue
        band = expiry_band(expires_at, now)
        if band is None:
            continue
        due += 1
        notifier.on_credential_expiry(credential["id"], credential.get("name", ""), expires_at, band[1], band[0])
    return due


def check_in_background(app_state: dict) -> None:
    """The same check off the calling thread: a channel can take seconds to answer."""
    def run() -> None:
        try:
            check_credential_expiry(app_state)
        except Exception:  # noqa: BLE001 — a warning must never break what triggered it
            logger.warning("Credential expiry check failed", exc_info=True)

    threading.Thread(target=run, name="credential-expiry", daemon=True).start()
