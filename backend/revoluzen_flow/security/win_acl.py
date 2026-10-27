"""Windows ACL handling for the master key file.

The encryptor locks the key down on POSIX — `chmod 600` on the file,
`0700` on its directory, and `_verify_perms` refuses to start when the
file is group- or world-readable. On Windows it did none of that: the
key was written with whatever ACL it inherited from the data directory
and nothing ever looked. Point `REVOLUZEN_FLOW_DATA_DIR` at a shared
drive, `C:\\ProgramData`, or a container bind mount and the key that
decrypts every stored credential inherits that directory's permissions
silently. Observed in development: a key file carrying
`PS\\CodexSandboxUsers: Modify`, every ACE inherited, nothing explicit.

Two halves, deliberately using different mechanisms:

* **Hardening** goes through `icacls` with `*<SID>` syntax. It ships with
  every Windows install, needs no dependency, and SIDs avoid the trap
  that sinks name-based approaches — "Administrators" is "Administratoren"
  on a German host, while S-1-5-32-544 is S-1-5-32-544 everywhere.

* **Reading** uses pywin32, because it returns SIDs directly. There is no
  `icacls` flag that prints SIDs reliably, and parsing its localised
  output to decide a security question would be worse than not checking.
  Without pywin32 the caller reports `not_checked` rather than guessing.
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

# Well-known SIDs — identical on every Windows install and in every
# language. https://learn.microsoft.com/windows-server/identity/ad-ds/manage/understand-security-identifiers
SID_LOCAL_SYSTEM = "S-1-5-18"
SID_ADMINISTRATORS = "S-1-5-32-544"

#: Principals allowed to reach the master key. The file's owner is added
#: at check time — it is the account the service runs as.
ALWAYS_ALLOWED_SIDS = frozenset({SID_LOCAL_SYSTEM, SID_ADMINISTRATORS})

# Access-mask bits that let a principal READ the key. GENERIC_ALL and
# FILE_ALL_ACCESS imply read, so they count too.
_FILE_READ_DATA = 0x0001
_GENERIC_READ = 0x80000000
_GENERIC_ALL = 0x10000000
_FILE_ALL_ACCESS = 0x001F01FF
_READ_BITS = _FILE_READ_DATA | _GENERIC_READ | _GENERIC_ALL | _FILE_ALL_ACCESS

_ACCESS_ALLOWED_ACE_TYPE = 0


def current_user_sid() -> str | None:
    """SID of the account this process runs as, or None if unavailable."""
    try:
        import win32api
        import win32security
        name = win32api.GetUserNameEx(win32api.NameSamCompatible)
        sid, _domain, _type = win32security.LookupAccountName(None, name)
        return win32security.ConvertSidToStringSid(sid)
    except Exception:  # noqa: BLE001 — absence is handled by the caller
        return None


def harden_file(path: Path) -> bool:
    """Restrict `path` to SYSTEM, Administrators and the current user.

    Breaks inheritance (`/inheritance:r`) and replaces the DACL
    (`/grant:r`), which is what makes this equivalent to `chmod 600`
    rather than merely additive. Best-effort: returns False and logs
    rather than raising, because failing to tighten permissions must
    never stop the application from starting — that would turn a
    hardening improvement into an outage.

    Only the FILE is hardened, not its directory. The data directory also
    holds the database and is often shared with a service account, so
    locking it to three principals could break a working deployment; the
    POSIX path can afford `0700` there because it owns that directory by
    convention.
    """
    if os.name != "nt":
        return False
    grants = [f"*{SID_LOCAL_SYSTEM}:(F)", f"*{SID_ADMINISTRATORS}:(F)"]
    me = current_user_sid()
    if me:
        grants.append(f"*{me}:(F)")
    try:
        proc = subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", *grants],
            capture_output=True, text=True, timeout=15, check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not harden master key ACL on %s: %s", path, exc)
        return False
    if proc.returncode != 0:
        logger.warning(
            "icacls could not harden %s (exit %s): %s",
            path, proc.returncode, (proc.stderr or proc.stdout or "").strip()[:200],
        )
        return False
    logger.info("Master key ACL restricted to SYSTEM, Administrators and the owner: %s", path)
    return True


def read_acl(path: Path) -> dict | None:
    """Who can read `path`, by SID.

    Returns ``{"owner": sid, "allowed": [...], "exposed": [(sid, name)]}``
    where `exposed` lists principals that are NOT the owner, SYSTEM or
    Administrators and can still read the file. Returns None when the ACL
    cannot be read at all — the caller must then report `not_checked`,
    never a pass.
    """
    if os.name != "nt":
        return None
    try:
        import win32security
    except Exception:  # noqa: BLE001
        return None
    try:
        sd = win32security.GetFileSecurity(
            str(path),
            win32security.OWNER_SECURITY_INFORMATION | win32security.DACL_SECURITY_INFORMATION,
        )
        owner_sid = win32security.ConvertSidToStringSid(sd.GetSecurityDescriptorOwner())
        dacl = sd.GetSecurityDescriptorDacl()
    except Exception as exc:  # noqa: BLE001
        logger.debug("Could not read ACL for %s: %s", path, exc)
        return None

    if dacl is None:
        # A NULL DACL grants everyone full control — the worst case, and
        # emphatically not "no information".
        return {"owner": owner_sid, "allowed": [], "exposed": [("NULL-DACL", "Everyone")]}

    allowed_sids = set(ALWAYS_ALLOWED_SIDS) | {owner_sid}
    allowed: list[str] = []
    exposed: list[tuple[str, str]] = []
    for i in range(dacl.GetAceCount()):
        try:
            (ace_type, _flags), mask, sid = dacl.GetAce(i)
        except Exception:  # noqa: BLE001 — skip ACEs we cannot parse
            continue
        if ace_type != _ACCESS_ALLOWED_ACE_TYPE:
            continue
        if not (mask & _READ_BITS):
            continue
        sid_str = win32security.ConvertSidToStringSid(sid)
        if sid_str in allowed_sids:
            allowed.append(sid_str)
            continue
        try:
            name, domain, _ = win32security.LookupAccountSid(None, sid)
            label = f"{domain}\\{name}" if domain else name
        except Exception:  # noqa: BLE001 — an unresolvable SID is still exposure
            label = sid_str
        exposed.append((sid_str, label))
    return {"owner": owner_sid, "allowed": allowed, "exposed": exposed}
