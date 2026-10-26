"""Real checks behind the Security & Diagnostics baseline.

`_security_baseline()` used to return seven rows hard-coded to
``not_checked`` with no ``checked_at`` and no ``evidence`` — nothing was
inspected and no code path could ever report otherwise. The controls it
names are all implemented in this codebase (the encryptor, the security-
headers middleware, the rate limiter, the audit tables), so the page was
declining to look at its own machinery. Nobody else can: an external
reviewer cannot read our credential rows to confirm they are ciphertext.

The rule every check here follows: **assert only what was actually
observed, and say what was not.** A check that reports "Verified" on
weaker evidence than its label implies is worse than "Not checked",
because the whole value of this page is that it does not overclaim. Each
result therefore carries a `detail` naming the limit of what was tested.

Status vocabulary (unchanged):
    verified     — observed directly, this run
    configured   — a setting was found; enforcement was NOT exercised
    not_checked  — no check ran (and why, in `detail`)
    failed       — observed, and wrong
"""

from __future__ import annotations

import logging
import os
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Set by SecurityHeadersMiddleware on every response. HSTS is deliberately
# absent: the middleware only emits it over HTTPS, so requiring it on a
# loopback HTTP probe would report a failure that is correct behaviour.
REQUIRED_SECURITY_HEADERS = (
    "x-content-type-options",
    "x-frame-options",
    "referrer-policy",
    "content-security-policy",
)

# Credentials are stored as `ENC:v1:<fernet token>` (security/encryptor.py).
_ENC_PREFIX = "ENC:v1:"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row(key: str, label: str, status: str, detail: str,
         evidence: str | None = None, checked: bool = True,
         remediation: str | None = None) -> dict[str, Any]:
    """One baseline row.

    `remediation` is the concrete thing an operator types or clicks, and
    it is the field that turns a report into something actionable. It is
    set only on `failed`: a fix offered next to a passing control is
    noise, and one offered next to `not_checked` would imply we know what
    is wrong when we do not.
    """
    return {
        "key": key, "label": label, "status": status, "detail": detail,
        "checked_at": _now() if checked else None,
        "evidence": evidence,
        "remediation": remediation if status == "failed" else None,
    }


async def check_security_headers(base_url: str) -> dict[str, Any]:
    """Issue a loopback request and read the headers off a real response.

    This is the strongest check on the page because it measures the
    deployed server rather than reading configuration: whatever the
    middleware stack actually does to a response is what gets reported.

    Scope it does NOT cover: this observes the response as served by this
    process. A reverse proxy in front of it can add or strip headers, and
    HSTS is excluded entirely because the middleware emits it only over
    HTTPS — demanding it on an HTTP loopback probe would report a failure
    that is in fact correct behaviour.
    """
    key, label = "security_headers", "HTTP security headers"
    try:
        import httpx
    except Exception:  # noqa: BLE001
        return _row(key, label, "not_checked",
                    "httpx is not installed, so no loopback request was made.",
                    checked=False)
    url = base_url.rstrip("/") + "/api/health"
    try:
        async with httpx.AsyncClient(timeout=5.0, follow_redirects=False) as client:
            resp = await client.get(url)
    except Exception as exc:  # noqa: BLE001
        # A failed probe is not a failed control — we learned nothing.
        return _row(key, label, "not_checked",
                    f"Loopback request to {url} did not complete ({type(exc).__name__}), "
                    "so response headers were not observed.",
                    checked=False)

    present = {h for h in REQUIRED_SECURITY_HEADERS if h in resp.headers}
    missing = [h for h in REQUIRED_SECURITY_HEADERS if h not in present]
    detail = (
        f"Observed on a live response from {url}. Covers this process only — "
        "a reverse proxy may add or strip headers. HSTS is not asserted here "
        "because it is emitted only over HTTPS."
    )
    if missing:
        return _row(key, label, "failed",
                    detail + f" Missing: {', '.join(missing)}.",
                    evidence=f"GET {url} → {resp.status_code}",
                    remediation=(
                        "These are added by SecurityHeadersMiddleware. Check it is "
                        "still installed in main.py, and that a reverse proxy in "
                        "front of the app is not stripping response headers."
                    ))
    return _row(key, label, "verified", detail,
                evidence=f"GET {url} → {resp.status_code}; "
                         f"{len(present)}/{len(REQUIRED_SECURITY_HEADERS)} headers present")


def check_credential_encryption(db: Any) -> dict[str, Any]:
    """Read our own credential rows and confirm they are ciphertext.

    Scope it does NOT cover: this proves the stored form carries the
    `ENC:v1:` envelope, not that the key is strong, that the values
    decrypt, or that no secret was written somewhere else.
    """
    key, label = "credential_encryption", "Stored credential encryption"
    if db is None:
        return _row(key, label, "not_checked",
                    "No database handle was available to this check.", checked=False)
    try:
        rows = db.fetchall("SELECT id, data FROM credentials")
    except Exception as exc:  # noqa: BLE001
        return _row(key, label, "not_checked",
                    f"Could not read the credentials table ({type(exc).__name__}).",
                    checked=False)

    total = len(rows)
    if total == 0:
        # Nothing stored is not evidence of encryption. Say so rather than
        # reporting a clean pass on an empty set.
        return _row(key, label, "not_checked",
                    "No credentials are stored, so there was nothing to inspect.",
                    checked=False)

    plaintext = 0
    for r in rows:
        data = r["data"] if hasattr(r, "keys") else r[1]
        if not isinstance(data, str) or _ENC_PREFIX not in data:
            plaintext += 1

    detail = (
        f"Inspected {total} stored credential row(s) for the {_ENC_PREFIX} envelope. "
        "Does not verify key strength, that values decrypt, or that no secret "
        "was written elsewhere."
    )
    if plaintext:
        return _row(key, label, "failed",
                    detail + f" {plaintext} row(s) are not encrypted.",
                    evidence=f"{total - plaintext}/{total} rows carry {_ENC_PREFIX}",
                    remediation=(
                        "Those rows predate encryption. Re-save each affected "
                        "connection so its secrets are written through the "
                        "encryptor, then re-run this check."
                    ))
    return _row(key, label, "verified", detail,
                evidence=f"{total}/{total} rows carry {_ENC_PREFIX}")


def check_master_key_permissions() -> dict[str, Any]:
    """Stat the master key file and report who can read it.

    POSIX only, deliberately. The encryptor's own `_verify_perms` takes the
    same position — on Windows the NTFS ACL is the operator's deployment
    concern and a mode check would be meaningless. Reporting `not_checked`
    there is the honest answer; claiming "verified" from a stat() that
    tells us nothing would not be.
    """
    key, label = "master_key_perms", "Master key permissions"
    try:
        from revoluzen_flow.security.encryptor import master_key_path
        path: Path = master_key_path()
    except Exception as exc:  # noqa: BLE001
        return _row(key, label, "not_checked",
                    f"Could not resolve the master key path ({type(exc).__name__}).",
                    checked=False)

    if not path.exists():
        return _row(key, label, "not_checked",
                    f"No master key file at {path} — nothing to inspect.", checked=False)

    if os.name == "nt":
        from revoluzen_flow.security.win_acl import read_acl
        acl = read_acl(path)
        if acl is None:
            return _row(key, label, "not_checked",
                        f"Key file present at {path}. Its NTFS ACL could not be "
                        "read on this host (pywin32 unavailable), so access was "
                        "not determined.",
                        checked=False)
        detail = (
            f"Read the NTFS ACL of {path}. Covers principals granted read access "
            "by the file's own DACL — not share permissions, not who can reach "
            "the file by other means, and not the containing directory."
        )
        if acl["exposed"]:
            names = ", ".join(label_ for _sid, label_ in acl["exposed"][:4])
            from revoluzen_flow.security.win_acl import (
                SID_ADMINISTRATORS, SID_LOCAL_SYSTEM, current_user_sid,
            )
            me = current_user_sid() or "<your-SID — run: whoami /user>"
            fix = (
                f'icacls "{path}" /inheritance:r /grant:r '
                f'"*{SID_LOCAL_SYSTEM}:(F)" "*{SID_ADMINISTRATORS}:(F)" "*{me}:(F)"'
            )
            return _row(key, label, "failed",
                        detail + f" Readable by {len(acl['exposed'])} principal(s) "
                                 f"beyond the owner, SYSTEM and Administrators: {names}.",
                        evidence=f"exposed: {names}",
                        remediation=fix)
        return _row(key, label, "verified", detail,
                    evidence="DACL grants read only to the owner, SYSTEM and Administrators")

    if os.name != "posix":
        return _row(key, label, "not_checked",
                    f"Key file present at {path}. Permissions on this platform "
                    "are not inspected by this check.",
                    checked=False)

    try:
        mode = path.stat().st_mode
    except OSError as exc:
        return _row(key, label, "not_checked",
                    f"Could not stat {path} ({exc.__class__.__name__}).", checked=False)

    exposed = mode & (stat.S_IRGRP | stat.S_IROTH | stat.S_IWGRP | stat.S_IWOTH)
    detail = (
        f"Read the file mode of {path}. Covers the POSIX permission bits only — "
        "not ownership, ACLs, or who can reach the file by other means."
    )
    if exposed:
        return _row(key, label, "failed",
                    detail + f" Mode is {oct(mode & 0o777)}; group/world can access it.",
                    evidence=f"mode={oct(mode & 0o777)}",
                    remediation=f"chmod 600 {path} && chmod 700 {path.parent}")
    return _row(key, label, "verified", detail, evidence=f"mode={oct(mode & 0o777)}")


def check_audit_retention(db: Any) -> dict[str, Any]:
    """Report what the audit log actually holds.

    Scope it does NOT cover: this counts rows and finds the oldest one. It
    does not establish that every auditable action produced an entry, which
    is the claim an auditor actually cares about and which only event-level
    coverage testing can support.
    """
    key, label = "audit_log", "Audit retention"
    if db is None:
        return _row(key, label, "not_checked",
                    "No database handle was available to this check.", checked=False)
    try:
        row = db.fetchone(
            "SELECT COUNT(*) AS n, MIN(created_at) AS oldest FROM vault_audit_log"
        )
    except Exception as exc:  # noqa: BLE001
        return _row(key, label, "not_checked",
                    f"Could not read the audit table ({type(exc).__name__}).",
                    checked=False)

    n = (row["n"] if hasattr(row, "keys") else row[0]) if row else 0
    oldest = (row["oldest"] if hasattr(row, "keys") else row[1]) if row else None
    detail = (
        "Counted rows in the audit table and read the oldest entry. Does NOT "
        "establish that every auditable action produced an entry — event "
        "coverage needs dedicated testing."
    )
    if not n:
        return _row(key, label, "not_checked",
                    "The audit table is present but empty, so retention has not "
                    "been exercised on this install.", checked=False)
    return _row(key, label, "configured", detail,
                evidence=f"{n} audit row(s); oldest {oldest}")
