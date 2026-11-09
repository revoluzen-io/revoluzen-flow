"""A credential that's about to lapse tells someone, without anyone opening a page."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from revoluzen_flow.credentials.expiry import check_credential_expiry, expiry_band
from revoluzen_flow.notifications.service import ApprovalNotifier
from revoluzen_flow.notifications.store import NotificationStore
from revoluzen_flow.storage.database import Database

NOW = datetime(2026, 11, 4, 9, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    "delta,band",
    [
        (timedelta(days=30), None),
        (timedelta(days=8), None),
        (timedelta(days=7), "7"),
        (timedelta(days=5), "7"),
        (timedelta(hours=20), "1"),
        (timedelta(hours=-1), "expired"),
        (timedelta(days=-40), "expired"),
    ],
)
def test_bands(delta, band):
    got = expiry_band(NOW + delta, NOW)
    assert (got[0] if got else None) == band


class _Mailer(ApprovalNotifier):
    """The real notifier, with the channels recorded instead of sent."""

    def __init__(self, store, users, db):
        super().__init__(store, users, db)
        self.sent = []

    def _send_all_channels(self, config, subject, body, email_recipients, event_type):
        self.sent.append({"subject": subject, "body": body, "to": email_recipients, "event": event_type})


@pytest.fixture
def world(tmp_path):
    db = Database(str(tmp_path / "flow.db"))
    store = NotificationStore(db)
    users = SimpleNamespace(list_users=lambda: [
        {"id": "a1", "email": "admin@example.test", "role": "admin", "is_active": True},
        {"id": "v1", "email": "viewer@example.test", "role": "viewer", "is_active": True},
    ])
    notifier = _Mailer(store, users, db)
    credentials = [{"id": "c1", "name": "Partner login", "expires_at": (NOW + timedelta(days=5)).isoformat()}]
    state = {"credential_store": SimpleNamespace(list_all=lambda: credentials), "approval_notifier": notifier}
    return SimpleNamespace(state=state, store=store, notifier=notifier, credentials=credentials)


def test_warns_admins_on_the_bell_and_by_email(world):
    assert check_credential_expiry(world.state, NOW) == 1
    rows = world.store.list_for_user("a1", unread_only=False, limit=10)
    assert [r["title"] for r in rows] == ["Credential expiring soon: Partner login"]
    assert "in 5 days" in rows[0]["message"]
    assert rows[0]["link_type"] == "credentials"
    assert world.notifier.sent[0]["to"] == ["admin@example.test"]
    assert world.store.list_for_user("v1", unread_only=False, limit=10) == []


def test_each_warning_goes_out_once(world):
    check_credential_expiry(world.state, NOW)
    check_credential_expiry(world.state, NOW + timedelta(hours=1))
    assert len(world.store.list_for_user("a1", unread_only=False, limit=10)) == 1
    assert len(world.notifier.sent) == 1


def test_warns_again_a_day_ahead_and_on_expiry(world):
    check_credential_expiry(world.state, NOW)
    check_credential_expiry(world.state, NOW + timedelta(days=4, hours=12))
    check_credential_expiry(world.state, NOW + timedelta(days=6))
    titles = [r["title"] for r in world.store.list_for_user("a1", unread_only=False, limit=10)]
    assert len(titles) == 3
    assert "Credential expired: Partner login" in titles


def test_renewing_starts_the_warnings_again(world):
    check_credential_expiry(world.state, NOW)
    world.credentials[0]["expires_at"] = (NOW + timedelta(days=6)).isoformat()
    check_credential_expiry(world.state, NOW)
    assert len(world.store.list_for_user("a1", unread_only=False, limit=10)) == 2


def test_no_date_and_far_off_dates_stay_quiet(world):
    world.credentials[:] = [
        {"id": "c2", "name": "No date", "expires_at": None},
        {"id": "c3", "name": "Next year", "expires_at": (NOW + timedelta(days=300)).isoformat()},
        {"id": "c4", "name": "Bad date", "expires_at": "soon"},
    ]
    assert check_credential_expiry(world.state, NOW) == 0
    assert world.notifier.sent == []


def test_the_message_never_carries_a_secret(world):
    world.credentials[0]["config"] = {"password": "synthetic-secret"}
    check_credential_expiry(world.state, NOW)
    row = world.store.list_for_user("a1", unread_only=False, limit=10)[0]
    assert "synthetic-secret" not in row["message"] + str(row.get("metadata"))
    assert "synthetic-secret" not in world.notifier.sent[0]["body"]
