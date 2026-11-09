"""The Settings toggle for credential expiry warnings reaches the notifier.

The config endpoint keeps only the keys it knows, so a toggle whose key was missing
there would look saved and be dropped, and the warnings would keep coming.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tests.conftest_fixtures_v2 import (  # noqa: F401
    data_dir, db_fixture, app_v2, client, admin_token, authed_client,
)


@pytest.fixture
def warn(authed_client):
    """Send a warning through the real notifier, with its channels recorded."""
    from revoluzen_flow.main import app_state

    notifier = app_state["approval_notifier"]
    store = app_state["notification_store"]
    admin_id = notifier._get_admin_users()[0]["id"]
    original = notifier._send_all_channels
    notifier._send_all_channels = lambda *a, **k: None
    expires_at = datetime.now(timezone.utc) + timedelta(days=5)

    def send(credential_id: str) -> bool:
        notifier.on_credential_expiry(credential_id, "Partner login", expires_at, 5, "7")
        rows = store.list_for_user(admin_id, unread_only=False, limit=100)
        return any((r.get("metadata") or {}).get("credential_id") == credential_id for r in rows)

    yield send
    notifier._send_all_channels = original
    authed_client.put("/api/notifications/config", json={"notify_on_credential_expiry": True})


def test_it_is_on_by_default(authed_client):
    config = authed_client.get("/api/notifications/config").json()
    assert config["notify_on_credential_expiry"] is True


def test_saving_it_off_stops_the_warnings(authed_client, warn):
    assert warn("setting-on")

    saved = authed_client.put("/api/notifications/config", json={"notify_on_credential_expiry": False})
    assert saved.status_code == 200, saved.text
    assert saved.json()["notify_on_credential_expiry"] is False
    assert authed_client.get("/api/notifications/config").json()["notify_on_credential_expiry"] is False
    assert not warn("setting-off")


def test_turning_it_back_on_starts_them_again(authed_client, warn):
    authed_client.put("/api/notifications/config", json={"notify_on_credential_expiry": False})
    assert not warn("setting-back")
    authed_client.put("/api/notifications/config", json={"notify_on_credential_expiry": True})
    assert warn("setting-back")
