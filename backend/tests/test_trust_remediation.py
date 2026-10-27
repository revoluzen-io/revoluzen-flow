"""The "Fix this" button: POST /api/trust/remediate/{control_key}.

The rule this endpoint has to honour is the same one the rest of the
Security & Diagnostics page honours: **report what was observed, not what
was attempted.** A fix that runs without error but leaves the ACL wrong
must still come back `failed`. These tests pin that, because the easy
implementation — return success when the subprocess exits 0 — would be
wrong in exactly the case that matters.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from revoluzen_flow.api import trust

pytestmark = pytest.mark.unit


# ── The fixable-control registry ─────────────────────────────────────

def test_only_registered_controls_are_fixable():
    """The UI renders a button per `can_auto_fix`; that flag comes from here."""
    assert set(trust._REMEDIATIONS) == {"master_key_perms"}


def test_rows_advertise_auto_fix_only_when_failed(monkeypatch):
    """A passing control must never offer to be 'fixed'."""
    monkeypatch.setattr(trust, "_UNVERIFIED_CONTROLS", [])
    monkeypatch.setattr(
        trust, "_security_baseline",
        trust._security_baseline,  # keep the real one; stub its checks below
    )
    import revoluzen_flow.api.trust_checks as checks
    monkeypatch.setattr(checks, "check_credential_encryption", lambda db: {
        "key": "credential_encryption", "status": "verified", "label": "x",
        "detail": "", "checked_at": None, "evidence": None})
    monkeypatch.setattr(checks, "check_audit_retention", lambda db: {
        "key": "audit_log", "status": "configured", "label": "x",
        "detail": "", "checked_at": None, "evidence": None})
    monkeypatch.setattr(checks, "check_master_key_permissions", lambda: {
        "key": "master_key_perms", "status": "verified", "label": "x",
        "detail": "", "checked_at": None, "evidence": None})

    rows = {r["key"]: r for r in trust._security_baseline()}
    assert rows["master_key_perms"]["can_auto_fix"] is False, (
        "a verified control must not offer a fix"
    )

    # Now the same control, failing — the only state that earns a button.
    monkeypatch.setattr(checks, "check_master_key_permissions", lambda: {
        "key": "master_key_perms", "status": "failed", "label": "x",
        "detail": "", "checked_at": None, "evidence": None})
    rows = {r["key"]: r for r in trust._security_baseline()}
    assert rows["master_key_perms"]["can_auto_fix"] is True


def test_unknown_control_is_rejected():
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        trust.remediate_control("sql_injection", _FakeRequest(), user=None)
    assert exc.value.status_code == 404


# ── The outcome comes from the re-check, not from the fixer ──────────

class _FakeRequest:
    base_url = "http://127.0.0.1:8000/"
    headers: dict = {}
    cookies: dict = {}


def _stub_baseline(monkeypatch, status: str):
    monkeypatch.setattr(
        trust, "_security_baseline",
        lambda: [{"key": "master_key_perms", "label": "Master key permissions",
                  "status": status, "detail": "d", "checked_at": None,
                  "evidence": None, "can_auto_fix": status == "failed"}],
    )


def test_response_reports_the_recheck_not_the_attempt(monkeypatch):
    """The fixer claims it ran; the re-check says it did not work.

    This is the negative control for the whole endpoint. If the response
    said `verified` here, the UI would show green over a key that is
    still readable by everyone.
    """
    monkeypatch.setattr(trust, "_REMEDIATIONS", {
        "master_key_perms": lambda: (True, "Reset the DACL."),
    })
    monkeypatch.setattr(trust, "current_workspace_id", lambda request: "default")
    _stub_baseline(monkeypatch, "failed")

    out = trust.remediate_control("master_key_perms", _FakeRequest(), user=None)
    assert out["attempted"] is True            # the fixer ran
    assert out["control"]["status"] == "failed"  # and it did not help


def test_successful_fix_reports_the_new_status(monkeypatch):
    monkeypatch.setattr(trust, "_REMEDIATIONS", {
        "master_key_perms": lambda: (True, "Reset the DACL."),
    })
    monkeypatch.setattr(trust, "current_workspace_id", lambda request: "default")
    _stub_baseline(monkeypatch, "verified")

    out = trust.remediate_control("master_key_perms", _FakeRequest(), user=None)
    assert out["control"]["status"] == "verified"
    assert out["control"]["can_auto_fix"] is False


def test_fixer_exception_does_not_500(monkeypatch):
    """A broken fix must degrade to a message, not a stack trace."""
    def _boom():
        raise RuntimeError("icacls missing")

    monkeypatch.setattr(trust, "_REMEDIATIONS", {"master_key_perms": _boom})
    monkeypatch.setattr(trust, "current_workspace_id", lambda request: "default")
    _stub_baseline(monkeypatch, "failed")

    out = trust.remediate_control("master_key_perms", _FakeRequest(), user=None)
    assert out["attempted"] is False
    assert "RuntimeError" in out["message"]
    assert out["control"]["status"] == "failed"


# ── The master-key fixer itself ──────────────────────────────────────

def test_missing_key_file_is_not_an_attempt(monkeypatch, tmp_path):
    missing = tmp_path / "secret.key"
    monkeypatch.setattr(
        "revoluzen_flow.security.encryptor.master_key_path", lambda: missing
    )
    attempted, message = trust._remediate_master_key_perms()
    assert attempted is False
    assert "nothing to fix" in message


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode bits")
def test_posix_fixer_tightens_the_mode(monkeypatch, tmp_path):
    key = tmp_path / "secret.key"
    key.write_text("x")
    key.chmod(0o644)
    monkeypatch.setattr(
        "revoluzen_flow.security.encryptor.master_key_path", lambda: key
    )
    attempted, _msg = trust._remediate_master_key_perms()
    assert attempted is True
    assert (key.stat().st_mode & 0o777) == 0o600
    assert (Path(key).parent.stat().st_mode & 0o777) == 0o700


@pytest.mark.skipif(os.name != "nt", reason="NTFS DACL")
def test_windows_fixer_calls_harden_file(monkeypatch, tmp_path):
    key = tmp_path / "secret.key"
    key.write_text("x")
    monkeypatch.setattr(
        "revoluzen_flow.security.encryptor.master_key_path", lambda: key
    )
    seen: list[Path] = []
    monkeypatch.setattr(
        "revoluzen_flow.security.win_acl.harden_file",
        lambda p: (seen.append(p), True)[1],
    )
    attempted, message = trust._remediate_master_key_perms()
    assert attempted is True
    assert seen == [key]
    assert str(key) in message


@pytest.mark.skipif(os.name != "nt", reason="NTFS DACL")
def test_windows_fixer_actually_removes_exposure(tmp_path, monkeypatch):
    """End-to-end on a real file: harden it, then read the real ACL back.

    Skips rather than fails when pywin32 is absent — without it `read_acl`
    returns None and there is nothing to assert against.
    """
    from revoluzen_flow.security.win_acl import read_acl

    key = tmp_path / "secret.key"
    key.write_text("x")
    if read_acl(key) is None:
        pytest.skip("pywin32 unavailable; the ACL cannot be read back")

    monkeypatch.setattr(
        "revoluzen_flow.security.encryptor.master_key_path", lambda: key
    )
    attempted, _msg = trust._remediate_master_key_perms()
    assert attempted is True

    acl = read_acl(key)
    assert acl is not None
    assert acl["exposed"] == [], f"still exposed to {acl['exposed']}"
