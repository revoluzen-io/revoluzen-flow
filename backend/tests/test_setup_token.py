"""First-run setup token on server-mode installs (2026-11-13 pen test).

Before: on an empty server the first /register from ANYONE became
super_admin, so whoever reached the port first owned the instance. Now that
first account needs a one-time token printed in the server log and saved as
SETUP_TOKEN.txt in the data folder. Local (laptop) mode is unchanged.
"""

from __future__ import annotations

import os
import sys
from types import ModuleType

import pytest
from fastapi import HTTPException

sys.modules.setdefault("psutil", ModuleType("psutil"))

from revoluzen_flow import runtime_config  # noqa: E402
from revoluzen_flow.api import auth as auth_api  # noqa: E402
from revoluzen_flow.auth.models import RegisterRequest  # noqa: E402
from revoluzen_flow.auth.store import UserStore  # noqa: E402

STRONG = "Setup-Token-Test-7!pw"


@pytest.fixture
def empty_instance(_revoluzen_flow_test_db, monkeypatch, tmp_path):
    from revoluzen_flow.main import app_state
    monkeypatch.delenv("REVOLUZEN_FLOW_BOOTSTRAP_ADMIN", raising=False)
    store = UserStore(db=_revoluzen_flow_test_db)
    assert store.list_users() == []
    monkeypatch.setitem(app_state, "db", _revoluzen_flow_test_db)
    monkeypatch.setitem(app_state, "user_store", store)
    monkeypatch.setitem(app_state, "data_dir", str(tmp_path))
    monkeypatch.setattr(runtime_config, "IS_SERVER_MODE", True)
    return store, tmp_path


def _register(**kw):
    import asyncio
    body = RegisterRequest(email=kw.pop("email", "operator@corp.test"), password=STRONG, name="Op", **kw)
    return asyncio.run(auth_api.register(body))


def test_the_policy_asks_for_a_token_only_on_an_empty_server(empty_instance, monkeypatch):
    import asyncio
    assert asyncio.run(auth_api.signup_policy())["setup_token_required"] is True
    monkeypatch.setattr(runtime_config, "IS_SERVER_MODE", False)
    assert asyncio.run(auth_api.signup_policy())["setup_token_required"] is False


@pytest.mark.parametrize("token", ["", "wrong-token-value-that-is-long-enough"])
def test_the_first_account_is_refused_without_the_right_token(empty_instance, token):
    store, _ = empty_instance
    with pytest.raises(HTTPException) as exc:
        _register(setup_token=token)
    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == "setup_token_required"
    assert store.list_users() == []


def test_the_token_creates_the_operator_and_is_then_spent(empty_instance):
    store, data_dir = empty_instance
    token = auth_api.ensure_setup_token(str(data_dir))
    path = data_dir / auth_api.SETUP_TOKEN_FILENAME
    assert token and path.read_text().strip() == token
    assert auth_api.ensure_setup_token(str(data_dir)) == token  # stable across restarts

    result = _register(setup_token=token)
    assert result["user"]["role"] == "super_admin"
    assert not path.exists()
    assert auth_api.ensure_setup_token(str(data_dir)) is None


def test_local_mode_needs_no_token(empty_instance, monkeypatch):
    monkeypatch.setattr(runtime_config, "IS_SERVER_MODE", False)
    result = _register()
    assert result["user"]["role"] == "super_admin"


def test_the_token_file_is_never_listed_or_readable_by_pipelines(empty_instance):
    _, data_dir = empty_instance
    auth_api.ensure_setup_token(str(data_dir))
    from revoluzen_flow.main import _is_listable_data_file
    from revoluzen_flow.security import sql_sandbox
    assert _is_listable_data_file(auth_api.SETUP_TOKEN_FILENAME) is False
    token_path = os.path.join(str(data_dir), auth_api.SETUP_TOKEN_FILENAME)
    assert sql_sandbox.is_protected_file(token_path, str(data_dir))
