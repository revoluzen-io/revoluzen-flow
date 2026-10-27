"""The sign-in screen must only mention the bootstrap password file when
that file exists.

The login page told every operator to find the one-time password in
`INITIAL_ADMIN_PASSWORD.txt`. That file is written ONLY when the install
was started with `REVOLUZEN_FLOW_BOOTSTRAP_ADMIN=1` (headless / Docker /
CI) — a normal interactive install creates neither the file nor the
`admin@revoluzen.local` account. So the advice pointed at something that
was never created, and never named the account it was talking about.

`signup-policy` now reports the file's actual presence. The file is the
right signal rather than the account, because the server deletes it the
moment the bootstrap admin rotates their password — so "pending" means
"first run genuinely unfinished", not merely "this install bootstrapped
once, months ago".
"""

from __future__ import annotations

import pytest

from revoluzen_flow.api import auth as auth_api

pytestmark = pytest.mark.unit


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """Point app_state at an empty data dir with no users."""
    from revoluzen_flow.main import app_state
    monkeypatch.setitem(app_state, "data_dir", str(tmp_path))
    monkeypatch.setitem(app_state, "db", None)
    monkeypatch.setitem(app_state, "user_store", None)
    return tmp_path


def write_password_file(d):
    (d / auth_api.BOOTSTRAP_PASSWORD_FILENAME).write_text(
        "Email: admin@revoluzen.local\nPassword: hunter2hunter2\n", encoding="utf-8"
    )


# ── The signal itself ────────────────────────────────────────────────

def test_not_pending_when_the_file_was_never_written(data_dir):
    """The default install. This is the case the old copy got wrong."""
    assert auth_api._bootstrap_password_pending() is False


def test_pending_when_the_file_exists(data_dir):
    write_password_file(data_dir)
    assert auth_api._bootstrap_password_pending() is True


def test_not_pending_after_the_file_is_removed(data_dir):
    """Rotation deletes it — the hint must disappear with it."""
    write_password_file(data_dir)
    assert auth_api._bootstrap_password_pending() is True
    (data_dir / auth_api.BOOTSTRAP_PASSWORD_FILENAME).unlink()
    assert auth_api._bootstrap_password_pending() is False


def test_missing_data_dir_is_not_pending(monkeypatch):
    """A public endpoint must not 500 because app_state is half-built."""
    from revoluzen_flow.main import app_state
    monkeypatch.setitem(app_state, "data_dir", None)
    assert auth_api._bootstrap_password_pending() is False



# ── What the endpoint reports ────────────────────────────────────────

@pytest.mark.asyncio
async def test_policy_hides_the_hint_on_a_normal_install(data_dir):
    out = await auth_api.signup_policy()
    assert out["bootstrap_password_pending"] is False
    assert out["bootstrap_admin_email"] is None, (
        "an install that never bootstrapped must not name an account"
    )


@pytest.mark.asyncio
async def test_policy_names_the_account_while_pending(data_dir, monkeypatch):
    from revoluzen_flow import runtime_config
    monkeypatch.setattr(runtime_config, "IS_SERVER_MODE", False)
    write_password_file(data_dir)

    out = await auth_api.signup_policy()
    assert out["bootstrap_password_pending"] is True
    assert out["bootstrap_admin_email"] == "admin@revoluzen.local", (
        "the operator needs to know WHICH account the password belongs to"
    )


@pytest.mark.asyncio
async def test_server_mode_reveals_nothing_even_while_pending(data_dir, monkeypatch):
    """`signup-policy` is unauthenticated. On a network-exposed install an
    anonymous caller should not learn an account name, nor that a
    one-time password is sitting on disk."""
    from revoluzen_flow import runtime_config
    monkeypatch.setattr(runtime_config, "IS_SERVER_MODE", True)
    write_password_file(data_dir)

    out = await auth_api.signup_policy()
    assert out["bootstrap_password_pending"] is False
    assert out["bootstrap_admin_email"] is None


@pytest.mark.asyncio
async def test_existing_fields_are_unchanged(data_dir):
    """The login page already depends on these two."""
    out = await auth_api.signup_policy()
    assert set(out) >= {"allow_self_registration", "first_user_bootstrap"}
    assert isinstance(out["allow_self_registration"], bool)
    assert isinstance(out["first_user_bootstrap"], bool)
