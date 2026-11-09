"""An exposed master key is repaired when it is LOADED, not only created.

Creation-time hardening only ever helped new installs. The ones that
actually have an exposed key are the old ones, and they only got fixed if
somebody opened the Security page and pressed "Fix this". These tests pin
the load path: open an existing key whose ACL is wrong and the ACL is
tightened before the key is handed out.

The deliberate asymmetry with POSIX is pinned too — Windows repairs and
continues, POSIX refuses to start — because it is a decision, not an
oversight, and a future reader should see it asserted rather than infer
it from a missing branch.
"""

from __future__ import annotations

import logging
import os
import stat

import pytest

from revoluzen_flow.security import encryptor

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clear_settled_cache():
    """The per-process "already checked" set must not leak between tests."""
    encryptor._WINDOWS_ACL_SETTLED.clear()
    yield
    encryptor._WINDOWS_ACL_SETTLED.clear()


class _FakeAcl:
    """Stand-in for win_acl: reports exposure until harden_file is called."""

    def __init__(self, *, exposed=True, hardening_works=True):
        self._exposed = exposed
        self._works = hardening_works
        self.harden_calls: list = []

    def read_acl(self, path):
        return {"owner": "me",
                "allowed": [],
                "exposed": [("S-1-1-0", "Everyone")] if self._exposed else []}

    def harden_file(self, path):
        self.harden_calls.append(path)
        if self._works:
            self._exposed = False
        return self._works


def _install_fake(monkeypatch, fake):
    monkeypatch.setattr("revoluzen_flow.security.win_acl.read_acl", fake.read_acl)
    monkeypatch.setattr("revoluzen_flow.security.win_acl.harden_file", fake.harden_file)
    monkeypatch.setattr(encryptor.os, "name", "nt")


def test_exposed_existing_key_is_hardened_at_load(monkeypatch, tmp_path):
    key = tmp_path / "secret.key"
    key.write_bytes(b"x" * 44)
    fake = _FakeAcl(exposed=True)
    _install_fake(monkeypatch, fake)

    encryptor._verify_perms(key)

    assert fake.harden_calls == [key], "an exposed key must be hardened on load"


def test_already_tight_key_is_left_alone(monkeypatch, tmp_path):
    """Negative control — no exposure, no rewrite of the DACL."""
    key = tmp_path / "secret.key"
    key.write_bytes(b"x" * 44)
    fake = _FakeAcl(exposed=False)
    _install_fake(monkeypatch, fake)

    encryptor._verify_perms(key)

    assert fake.harden_calls == [], "a correct ACL must not be rewritten"


def test_acl_is_read_once_per_process(monkeypatch, tmp_path):
    key = tmp_path / "secret.key"
    key.write_bytes(b"x" * 44)
    fake = _FakeAcl(exposed=False)
    reads: list = []
    original = fake.read_acl
    fake.read_acl = lambda p: (reads.append(p), original(p))[1]
    _install_fake(monkeypatch, fake)

    for _ in range(5):
        encryptor._verify_perms(key)

    assert len(reads) == 1, f"DACL read {len(reads)} times; should be cached"


def test_failed_hardening_logs_and_does_not_raise(monkeypatch, tmp_path, caplog):
    """The whole point of repairing instead of refusing: startup survives."""
    key = tmp_path / "secret.key"
    key.write_bytes(b"x" * 44)
    fake = _FakeAcl(exposed=True, hardening_works=False)
    _install_fake(monkeypatch, fake)

    with caplog.at_level(logging.ERROR):
        encryptor._verify_perms(key)  # must not raise

    assert fake.harden_calls == [key]
    assert any("could NOT restrict" in r.message or "could NOT restrict" in r.getMessage()
               for r in caplog.records), "a failed repair must be logged loudly"


def test_missing_pywin32_does_not_touch_the_file(monkeypatch, tmp_path):
    """Unknown ACL means hands off — hardening blind could break a deployment."""
    key = tmp_path / "secret.key"
    key.write_bytes(b"x" * 44)
    calls: list = []
    monkeypatch.setattr("revoluzen_flow.security.win_acl.read_acl", lambda p: None)
    monkeypatch.setattr("revoluzen_flow.security.win_acl.harden_file",
                        lambda p: calls.append(p))
    monkeypatch.setattr(encryptor.os, "name", "nt")

    encryptor._verify_perms(key)

    assert calls == []


def test_win_acl_exception_never_blocks_startup(monkeypatch, tmp_path):
    key = tmp_path / "secret.key"
    key.write_bytes(b"x" * 44)

    def _boom(path):
        raise OSError("win32 unavailable")

    monkeypatch.setattr("revoluzen_flow.security.win_acl.read_acl", _boom)
    monkeypatch.setattr(encryptor.os, "name", "nt")

    encryptor._verify_perms(key)  # must not raise


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode bits")
def test_posix_still_refuses_rather_than_repairing(tmp_path):
    """The asymmetry is intentional; assert it so nobody 'fixes' it."""
    key = tmp_path / "secret.key"
    key.write_bytes(b"x" * 44)
    key.chmod(0o644)

    with pytest.raises(RuntimeError, match="Refuse to start"):
        encryptor._verify_perms(key)

    # And the file is untouched — POSIX does not silently repair.
    assert key.stat().st_mode & stat.S_IROTH


@pytest.mark.windows
@pytest.mark.skipif(os.name != "nt", reason="NTFS DACL")
def test_end_to_end_real_file_is_hardened_on_load(tmp_path):
    """No fakes: a real file, the real ACL, read back after the real load."""
    from revoluzen_flow.security.win_acl import read_acl

    key = tmp_path / "secret.key"
    key.write_bytes(b"x" * 44)
    if read_acl(key) is None:
        pytest.skip("pywin32 unavailable; the ACL cannot be read back")

    encryptor._verify_perms(key)

    acl = read_acl(key)
    assert acl is not None
    assert acl["exposed"] == [], f"still exposed to {acl['exposed']}"
