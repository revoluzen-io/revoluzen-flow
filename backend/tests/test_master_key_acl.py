"""Master key file protection on Windows.

The encryptor locked the key down on POSIX (`chmod 600`, plus a refusal
to start when it is group/world readable) and did nothing at all on
Windows: the key was written with whatever ACL it inherited from the data
directory, and no check ever looked. Point `REVOLUZEN_FLOW_DATA_DIR` at a
shared drive or a container bind mount and the key that decrypts every
stored credential silently inherits that directory's permissions.

This was not hypothetical — a development key file was found carrying
`PS\\CodexSandboxUsers: Modify`, every ACE inherited, nothing explicit.

These tests are Windows-only by nature. On POSIX they skip, and the
POSIX mode-bit behaviour is covered in test_trust_real_checks.py.
"""
from __future__ import annotations

import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows ACL behaviour")

from revoluzen_flow.security.win_acl import (  # noqa: E402
    ALWAYS_ALLOWED_SIDS, SID_ADMINISTRATORS, SID_LOCAL_SYSTEM,
    harden_file, read_acl,
)


def _has_pywin32() -> bool:
    try:
        import win32security  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


needs_pywin32 = pytest.mark.skipif(
    not _has_pywin32(), reason="pywin32 not installed; the reader degrades to not_checked"
)


class TestWellKnownSids:
    def test_sids_are_the_locale_independent_constants(self):
        # The whole reason this module uses SIDs rather than names:
        # "Administrators" is "Administratoren" on a German host, while
        # S-1-5-32-544 is the same everywhere.
        assert SID_LOCAL_SYSTEM == "S-1-5-18"
        assert SID_ADMINISTRATORS == "S-1-5-32-544"
        assert ALWAYS_ALLOWED_SIDS == {"S-1-5-18", "S-1-5-32-544"}


@needs_pywin32
class TestReadAcl:
    def test_hardened_file_has_no_exposed_principals(self, tmp_path):
        key = tmp_path / "secret.key"
        key.write_bytes(b"k")
        assert harden_file(key) is True
        acl = read_acl(key)
        assert acl is not None
        assert acl["exposed"] == [], f"unexpected exposure: {acl['exposed']}"
        # Owner + SYSTEM + Administrators.
        assert len(acl["allowed"]) >= 2

    def test_a_broadly_granted_file_is_reported_as_exposed(self, tmp_path):
        import subprocess
        key = tmp_path / "secret.key"
        key.write_bytes(b"k")
        harden_file(key)
        # Grant Users (S-1-5-32-545) read — the shape of the real-world
        # problem, where an inherited ACL lets a whole group read the key.
        subprocess.run(["icacls", str(key), "/grant", "*S-1-5-32-545:(R)"],
                       capture_output=True, check=False, timeout=15)
        acl = read_acl(key)
        assert acl is not None
        assert acl["exposed"], "a group with read access must be reported"
        assert any("S-1-5-32-545" in sid or "User" in name for sid, name in acl["exposed"])

    def test_missing_file_reads_as_none(self, tmp_path):
        assert read_acl(tmp_path / "nope.key") is None


class TestTheCheckReportsIt:
    @needs_pywin32
    def test_exposed_key_makes_the_control_fail(self, tmp_path, monkeypatch):
        import subprocess
        from revoluzen_flow.api.trust_checks import check_master_key_permissions
        key = tmp_path / "secret.key"
        key.write_bytes(b"k")
        harden_file(key)
        subprocess.run(["icacls", str(key), "/grant", "*S-1-5-32-545:(R)"],
                       capture_output=True, check=False, timeout=15)
        monkeypatch.setenv("REVOLUZEN_FLOW_MASTER_KEY_FILE", str(key))
        r = check_master_key_permissions()
        assert r["status"] == "failed"
        assert r["checked_at"] is not None
        assert "beyond the owner" in r["detail"]

    @needs_pywin32
    def test_hardened_key_verifies(self, tmp_path, monkeypatch):
        from revoluzen_flow.api.trust_checks import check_master_key_permissions
        key = tmp_path / "secret.key"
        key.write_bytes(b"k")
        assert harden_file(key) is True
        monkeypatch.setenv("REVOLUZEN_FLOW_MASTER_KEY_FILE", str(key))
        r = check_master_key_permissions()
        assert r["status"] == "verified"
        assert "not share permissions" in r["detail"], "the check must state its scope"

    def test_absent_key_is_not_checked_not_failed(self, tmp_path, monkeypatch):
        # No key yet is not a finding about permissions.
        from revoluzen_flow.api.trust_checks import check_master_key_permissions
        monkeypatch.setenv("REVOLUZEN_FLOW_MASTER_KEY_FILE", str(tmp_path / "absent.key"))
        r = check_master_key_permissions()
        assert r["status"] == "not_checked"
        assert r["checked_at"] is None


class TestCreationHardensTheKey:
    def test_a_newly_created_key_is_not_world_readable(self, tmp_path, monkeypatch):
        """The regression that mattered: a key created inside a directory
        with a broad inherited ACL must not keep that ACL."""
        import subprocess
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        # Make the directory readable by Users so anything created inside
        # inherits that — the real-world shape of the problem.
        subprocess.run(["icacls", str(data_dir), "/grant", "*S-1-5-32-545:(OI)(CI)(R)"],
                       capture_output=True, check=False, timeout=15)
        monkeypatch.setenv("REVOLUZEN_FLOW_MASTER_KEY_FILE", str(data_dir / "secret.key"))
        monkeypatch.delenv("REVOLUZEN_FLOW_MASTER_KEY", raising=False)

        from revoluzen_flow.security.encryptor import load_or_create_master_key
        load_or_create_master_key()

        key = data_dir / "secret.key"
        assert key.exists()
        if _has_pywin32():
            acl = read_acl(key)
            assert acl is not None
            assert acl["exposed"] == [], (
                f"a freshly created key inherited a broad ACL: {acl['exposed']}"
            )
