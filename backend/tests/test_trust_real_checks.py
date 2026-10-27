"""Security & Diagnostics — the baseline checks must actually check.

Every row on that page used to be hard-coded `not_checked` with no
timestamp and no evidence, including controls whose implementations live
in this repository. The page was declining to inspect its own machinery,
and nothing else can: an external reviewer cannot read our credential
rows to confirm they are ciphertext.

The rule these tests enforce is the one that makes the page worth having:
a check reports `verified` ONLY from something it observed, and says what
it did not cover. Reporting a pass on an empty set, or on a config read
standing in for enforcement, is the failure mode — so those cases are
pinned as `not_checked`, not as success.
"""
from __future__ import annotations

import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from revoluzen_flow.api.trust import _security_baseline  # noqa: E402
from revoluzen_flow.api.trust_checks import (  # noqa: E402
    REQUIRED_SECURITY_HEADERS, check_audit_retention,
    check_credential_encryption, check_master_key_permissions,
    check_security_headers,
)

VALID_STATUSES = {"verified", "configured", "not_checked", "failed"}


class _FakeDb:
    def __init__(self, rows=None, one=None, raises=False):
        self._rows, self._one, self._raises = rows or [], one, raises

    def fetchall(self, *_a, **_k):
        if self._raises:
            raise RuntimeError("boom")
        return self._rows

    def fetchone(self, *_a, **_k):
        if self._raises:
            raise RuntimeError("boom")
        return self._one


class TestCredentialEncryption:
    def test_all_encrypted_is_verified(self):
        r = check_credential_encryption(_FakeDb(rows=[
            {"id": "a", "data": 'ENC:v1:gAAAAA'}, {"id": "b", "data": 'ENC:v1:gBBBBB'},
        ]))
        assert r["status"] == "verified"
        assert r["checked_at"] is not None
        assert "2/2" in (r["evidence"] or "")

    def test_a_plaintext_row_fails(self):
        r = check_credential_encryption(_FakeDb(rows=[
            {"id": "a", "data": 'ENC:v1:gAAAAA'}, {"id": "b", "data": '{"password": "hunter2"}'},
        ]))
        assert r["status"] == "failed"
        assert "1 row(s) are not encrypted" in r["detail"]

    def test_empty_store_is_not_a_pass(self):
        # The trap this page exists to avoid: a green tick earned by having
        # nothing to check. An install with no credentials has not proven
        # that it encrypts them.
        r = check_credential_encryption(_FakeDb(rows=[]))
        assert r["status"] == "not_checked"
        assert r["checked_at"] is None
        assert "nothing to inspect" in r["detail"]

    def test_unreadable_table_is_not_a_failure(self):
        # Learning nothing is not a finding — `failed` would wrongly accuse
        # the install of storing plaintext.
        r = check_credential_encryption(_FakeDb(raises=True))
        assert r["status"] == "not_checked"

    def test_no_db_handle_is_not_a_failure(self):
        assert check_credential_encryption(None)["status"] == "not_checked"

    def test_scope_is_stated(self):
        r = check_credential_encryption(_FakeDb(rows=[{"id": "a", "data": "ENC:v1:x"}]))
        assert "Does not verify key strength" in r["detail"]


class TestMasterKeyPermissions:
    def test_reports_a_status_and_names_its_limits(self):
        r = check_master_key_permissions()
        assert r["status"] in VALID_STATUSES
        # On Windows, NTFS ACLs are not inspected and the row must say so
        # rather than claim a pass from a stat() that means nothing there.
        if os.name != "posix" and r["status"] == "not_checked":
            assert "ACL" in r["detail"] or "nothing to inspect" in r["detail"]

    @pytest.mark.skipif(os.name != "posix", reason="POSIX mode bits only")
    def test_world_readable_key_fails(self, tmp_path, monkeypatch):
        key = tmp_path / "secret.key"
        key.write_text("x")
        key.chmod(0o644)
        monkeypatch.setenv("REVOLUZEN_FLOW_MASTER_KEY_FILE", str(key))
        r = check_master_key_permissions()
        assert r["status"] == "failed"

    @pytest.mark.skipif(os.name != "posix", reason="POSIX mode bits only")
    def test_locked_down_key_is_verified(self, tmp_path, monkeypatch):
        key = tmp_path / "secret.key"
        key.write_text("x")
        key.chmod(0o600)
        monkeypatch.setenv("REVOLUZEN_FLOW_MASTER_KEY_FILE", str(key))
        assert check_master_key_permissions()["status"] == "verified"


class TestAuditRetention:
    def test_rows_present_is_configured_not_verified(self):
        # Counting rows does not prove every auditable action was recorded,
        # which is the claim that matters — so this must never say verified.
        r = check_audit_retention(_FakeDb(one={"n": 12, "oldest": "2026-10-01T00:00:00Z"}))
        assert r["status"] == "configured"
        assert "does NOT" in r["detail"].lower() or "not" in r["detail"].lower()

    def test_empty_table_is_not_checked(self):
        r = check_audit_retention(_FakeDb(one={"n": 0, "oldest": None}))
        assert r["status"] == "not_checked"

    def test_unreadable_is_not_checked(self):
        assert check_audit_retention(_FakeDb(raises=True))["status"] == "not_checked"


class TestSecurityHeaders:
    async def test_all_headers_present_is_verified(self, monkeypatch):
        class _Resp:
            status_code = 200
            headers = {h: "x" for h in REQUIRED_SECURITY_HEADERS}

        class _Client:
            def __init__(self, *a, **k): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def get(self, _url): return _Resp()

        import httpx
        monkeypatch.setattr(httpx, 'AsyncClient', _Client)
        r = await check_security_headers('http://localhost:8000')
        assert r["status"] == "verified"
        assert r["checked_at"] is not None

    async def test_a_missing_header_fails_and_names_it(self, monkeypatch):
        class _Resp:
            status_code = 200
            headers = {h: "x" for h in REQUIRED_SECURITY_HEADERS if h != "x-frame-options"}

        class _Client:
            def __init__(self, *a, **k): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def get(self, _url): return _Resp()

        import httpx
        monkeypatch.setattr(httpx, 'AsyncClient', _Client)
        r = await check_security_headers('http://localhost:8000')
        assert r["status"] == "failed"
        assert "x-frame-options" in r["detail"]

    async def test_an_unreachable_server_is_not_checked(self, monkeypatch):
        class _Client:
            def __init__(self, *a, **k): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def get(self, _url): raise OSError("refused")

        import httpx
        monkeypatch.setattr(httpx, 'AsyncClient', _Client)
        r = await check_security_headers('http://localhost:8000')
        # A probe that did not complete tells us nothing about the control.
        assert r["status"] == "not_checked"
        assert r["checked_at"] is None

    async def test_hsts_is_not_required(self):
        # The middleware emits HSTS only over HTTPS, so demanding it on an
        # HTTP loopback probe would report a failure that is correct
        # behaviour.
        assert "strict-transport-security" not in REQUIRED_SECURITY_HEADERS


class TestTheBaselineShape:
    def test_every_row_has_the_contract_shape(self):
        for row in _security_baseline():
            assert set(row) >= {"key", "label", "status", "detail", "checked_at", "evidence"}
            assert row["status"] in VALID_STATUSES
            assert row["detail"], f"{row['key']} must state its scope"

    def test_unverifiable_controls_stay_not_checked(self):
        # Reading a config and calling it verified is the overclaim this
        # page exists to avoid. These three must not drift into a pass
        # without something that actually exercises them.
        by_key = {r["key"]: r for r in _security_baseline()}
        for key in ("sql_input_sanitization", "rate_limiting", "data_at_rest_encryption"):
            assert by_key[key]["status"] == "not_checked"
            assert by_key[key]["checked_at"] is None

    def test_the_real_checks_are_present(self):
        by_key = {r["key"]: r for r in _security_baseline()}
        for key in ("credential_encryption", "master_key_perms", "audit_log"):
            assert key in by_key

    def test_a_not_checked_row_never_claims_a_timestamp(self):
        for row in _security_baseline():
            if row["status"] == "not_checked":
                assert row["checked_at"] is None, (
                    f"{row['key']} says not_checked but carries a checked_at"
                )
