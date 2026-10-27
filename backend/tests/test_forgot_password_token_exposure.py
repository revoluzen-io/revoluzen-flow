"""When may /forgot-password echo a live reset token to an anonymous caller?

The endpoint is public and unauthenticated, so a returned token IS an
account-takeover credential handed to whoever asked. The existing rules
were already careful — SMTP configured means email it and never echo —
but the "single user" exemption did not consider network exposure. On an
instance bound to the LAN the sole account is the admin, so "there is
only one account to reset" stops being a reason it is safe and becomes
the reason it is not.

The second thing pinned here is anti-enumeration. The response must look
identical for a registered and an unregistered address, which means the
message may branch on CONFIGURATION but never on whether the user was
found.
"""

from __future__ import annotations

import pytest

from revoluzen_flow.api import auth as auth_api

pytestmark = pytest.mark.unit


class _User:
    def __init__(self, email):
        self.id = "u1"
        self.email = email


class _Store:
    """Knows exactly one user, like a single-operator install."""

    def __init__(self, users):
        self._users = users

    def get_user_by_email(self, email):
        return _User(email) if email in self._users else None

    def list_users(self):
        return list(self._users)


class _Req:
    def __init__(self, ip="127.0.0.1"):
        self.client = type("C", (), {"host": ip})()
        self.headers = {}
        self.url = type("U", (), {"scheme": "http", "netloc": "localhost"})()


@pytest.fixture
def local_install(tmp_path, monkeypatch):
    """One user, no SMTP, and a REAL database.

    The auth queue lives in the settings table, not on disk — without a
    db handle `_read_auth_queue` returns empty lists and
    `_write_auth_queue` silently does nothing, so a test that asserted
    the token was stored would pass or fail for the wrong reason.
    """
    from revoluzen_flow.main import app_state
    from revoluzen_flow.storage.database import Database

    db = Database(str(tmp_path / "test.db"))
    monkeypatch.setitem(app_state, "db", db)
    monkeypatch.setitem(app_state, "data_dir", str(tmp_path))
    monkeypatch.setattr(auth_api, "get_store",
                        lambda: _Store({"solo@local.test"}))
    monkeypatch.setattr(auth_api, "_smtp_is_configured", lambda: False)
    monkeypatch.setattr(auth_api, "_is_plus_active", lambda: False)
    return tmp_path


async def ask(email="solo@local.test", ip="127.0.0.1"):
    body = auth_api.ForgotPasswordRequest(email=email)
    return await auth_api.forgot_password(body, _Req(ip))


# ── The gap this file closes ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_single_user_local_install_still_gets_the_token(local_install, monkeypatch):
    """The affordance that makes a laptop install usable without SMTP."""
    from revoluzen_flow import runtime_config
    monkeypatch.setattr(runtime_config, "IS_SERVER_MODE", False)
    out = await ask()
    assert out["reset_token"], "local single-operator install lost its fallback"
    assert out["expires_at"]


@pytest.mark.asyncio
async def test_single_user_but_network_exposed_withholds_the_token(local_install, monkeypatch):
    """THE fix. One account on a LAN-bound instance is the admin account —
    handing its reset token to an anonymous caller is takeover."""
    from revoluzen_flow import runtime_config
    monkeypatch.setattr(runtime_config, "IS_SERVER_MODE", True)
    out = await ask()
    assert out["reset_token"] is None, "a LAN-exposed instance echoed a takeover token"
    assert out["expires_at"] is None


@pytest.mark.asyncio
async def test_withheld_message_does_not_promise_a_link(local_install, monkeypatch):
    """The old copy said "use it below" even when nothing was returned,
    leaving the user with no link and no next step."""
    from revoluzen_flow import runtime_config
    monkeypatch.setattr(runtime_config, "IS_SERVER_MODE", True)
    msg = (await ask())["message"]
    assert "use it below" not in msg.lower()


@pytest.mark.asyncio
async def test_withheld_message_points_somewhere_that_exists(local_install, monkeypatch):
    """It used to say "ask them to share it from Admin -> Auth Queue".

    There is no Admin page and no Auth Queue screen in this edition —
    `getAuthQueue` exists in the API client but nothing renders it, and
    the only CLI reset is the DEV-only `seed-admin`. So the one branch
    that fires for a LAN-bound single-operator install with no SMTP sent
    people to a screen they cannot open. The advice has to be something
    this edition can actually do.
    """
    from revoluzen_flow import runtime_config
    monkeypatch.setattr(runtime_config, "IS_SERVER_MODE", True)
    msg = (await ask())["message"]
    lowered = msg.lower()
    assert "auth queue" not in lowered, "points at a screen this edition does not have"
    assert "smtp" in lowered or "inline" in lowered, (
        f"no route the operator can actually take: {msg!r}"
    )


# ── Anti-enumeration ─────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("server_mode", [False, True])
async def test_message_is_identical_for_unknown_emails(local_install, monkeypatch, server_mode):
    """A registered and an unregistered address must be indistinguishable
    from the response text alone."""
    from revoluzen_flow import runtime_config
    monkeypatch.setattr(runtime_config, "IS_SERVER_MODE", server_mode)
    known = await ask("solo@local.test")
    unknown = await ask("nobody@elsewhere.test")
    assert known["message"] == unknown["message"], (
        "the message differs by whether the account exists — that enumerates users"
    )
    assert known["queued"] == unknown["queued"] is True


@pytest.mark.asyncio
async def test_unknown_email_never_yields_a_token(local_install, monkeypatch):
    from revoluzen_flow import runtime_config
    monkeypatch.setattr(runtime_config, "IS_SERVER_MODE", False)
    assert (await ask("nobody@elsewhere.test"))["reset_token"] is None


# ── SMTP wins regardless of mode ─────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("server_mode", [False, True])
async def test_smtp_configured_never_echoes_the_token(local_install, monkeypatch, server_mode):
    from revoluzen_flow import runtime_config
    monkeypatch.setattr(runtime_config, "IS_SERVER_MODE", server_mode)
    monkeypatch.setattr(auth_api, "_smtp_is_configured", lambda: True)
    monkeypatch.setattr(auth_api, "_send_reset_email", lambda **kw: True)
    out = await ask()
    assert out["reset_token"] is None
    assert "email" in out["message"].lower()


@pytest.mark.asyncio
async def test_failed_smtp_send_does_not_fall_back_to_echoing(local_install, monkeypatch):
    """A broken relay must not downgrade the posture."""
    from revoluzen_flow import runtime_config
    monkeypatch.setattr(runtime_config, "IS_SERVER_MODE", False)
    monkeypatch.setattr(auth_api, "_smtp_is_configured", lambda: True)
    monkeypatch.setattr(auth_api, "_send_reset_email", lambda **kw: False)
    assert (await ask())["reset_token"] is None


# ── The token still exists for the admin to hand over ────────────────

@pytest.mark.asyncio
async def test_token_is_still_minted_and_queued_when_withheld(local_install, monkeypatch):
    """Withholding from the RESPONSE must not break the reset itself —
    the admin reads it from the Auth Queue."""
    from revoluzen_flow import runtime_config
    monkeypatch.setattr(runtime_config, "IS_SERVER_MODE", True)
    await ask()
    queue = auth_api._read_auth_queue()
    tokens = [t for t in queue["reset_tokens"] if t.get("email") == "solo@local.test"]
    assert len(tokens) == 1, "no token was stored for the admin to pass on"
    assert tokens[0]["token"]
    assert tokens[0]["used"] is False
