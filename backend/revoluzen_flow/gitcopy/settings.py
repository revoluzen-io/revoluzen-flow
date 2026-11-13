"""Where published pipelines are copied to, and how Flow signs in there.

One repository for the instance, kept in the ``settings`` table. The access
token is encrypted with the master key, like every other secret, and is never
sent back to the browser. An address that carries a password is refused, so a
token can only ever be in the one place it is protected.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import re
import socket
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

from revoluzen_flow.security.encryptor import _ENC_PREFIX

logger = logging.getLogger(__name__)

SETTINGS_ROW = "git_copy"

_BRANCH = re.compile(r"^(?!-)(?!.*\.\.)(?!.*//)(?!.*\.lock$)[A-Za-z0-9._/-]{1,200}(?<![./])$")
_FOLDER_PART = re.compile(r"^[A-Za-z0-9._ -]{1,80}$")


class GitSettingsError(ValueError):
    """The settings cannot be used. The message is written for the person."""


@dataclass
class GitSettings:
    enabled: bool = False
    repo_url: str = ""
    branch: str = "main"
    folder: str = "flow-pipelines"
    username: str = ""
    token: str = ""
    updated_at: str = ""
    updated_by: str = ""

    @property
    def ready(self) -> bool:
        return self.enabled and bool(self.repo_url) and bool(self.token)


def _addresses(host: str) -> list[Any] | None:
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError:
        return None
    return [ipaddress.ip_address(info[4][0]) for info in infos]


def check_repo_url(url: str) -> str:
    """The address, tidied, or :class:`GitSettingsError` saying what is wrong.

    A company's own Git server is usually on its private network, so private
    addresses are allowed here, unlike for pipeline steps. This is an admin
    setting and git only ever speaks the Git protocol to it. Link-local
    addresses, which include cloud metadata services, are never allowed.

    Only http(s). A path on this server's disk is refused on purpose: git
    reaches one by starting a shell, which on Windows can pile up without end,
    and the address alone would decide where on the server Flow writes.
    """
    url = (url or "").strip()
    if not url:
        raise GitSettingsError("Enter the address of the Git repository.")
    if any(ord(c) <= 32 or ord(c) == 127 for c in url):
        raise GitSettingsError("The repository address cannot contain spaces.")
    if url.startswith("-"):
        raise GitSettingsError("That is not a repository address.")
    parsed = urlsplit(url)
    scheme = parsed.scheme.lower()
    if scheme not in ("https", "http") or not parsed.hostname:
        raise GitSettingsError("Use the https address of the repository, such as https://github.com/acme/flow-pipelines.git.")
    if parsed.username is not None or parsed.password is not None:
        raise GitSettingsError("Take the name and token out of the address, and put the token in the Access token box.")
    if parsed.query or parsed.fragment:
        raise GitSettingsError("Use the plain address of the repository, with nothing after a ? or #.")
    addresses = _addresses(parsed.hostname)
    for address in addresses or []:
        if address.is_link_local or address.is_multicast or address.is_reserved or address.is_unspecified:
            raise GitSettingsError("That address is not allowed.")
    if scheme == "http" and not (addresses and all(a.is_private or a.is_loopback for a in addresses)):
        raise GitSettingsError(
            "Plain http would send the access token unprotected across the internet. Use the https address."
        )
    return url


def check_branch(branch: str) -> str:
    branch = (branch or "").strip() or "main"
    if not _BRANCH.match(branch):
        raise GitSettingsError("That is not a valid branch name.")
    return branch


def check_folder(folder: str) -> str:
    """A path inside the repository, or empty for its top level."""
    folder = (folder or "").strip().replace("\\", "/").strip("/")
    if not folder:
        return ""
    parts = folder.split("/")
    if any(p in (".", "..", ".git") or not _FOLDER_PART.match(p) for p in parts):
        raise GitSettingsError("The folder can only use letters, numbers, spaces, dots, dashes and underscores.")
    return "/".join(parts)


def _token_for_storage(token: str, encryptor) -> str:
    if not token or encryptor is None:
        return token
    return encryptor.encrypt_value(token)


def _token_from_storage(stored: str, encryptor) -> str:
    if not stored or not stored.startswith(_ENC_PREFIX) or encryptor is None:
        return stored or ""
    try:
        return encryptor.decrypt_value(stored)
    except Exception:  # noqa: BLE001 - the value must never reach a log
        logger.warning("The saved Git access token cannot be read with this install's master key.")
        return ""


def read_settings(db, encryptor) -> GitSettings:
    row = db.fetchone("SELECT data FROM settings WHERE id = ?", (SETTINGS_ROW,))
    if not row or not row["data"]:
        return GitSettings()
    try:
        data = json.loads(row["data"])
    except (TypeError, ValueError):
        return GitSettings()
    if not isinstance(data, dict):
        return GitSettings()
    return GitSettings(
        enabled=bool(data.get("enabled")),
        repo_url=str(data.get("repo_url") or ""),
        branch=str(data.get("branch") or "main"),
        folder=str(data.get("folder") or ""),
        username=str(data.get("username") or ""),
        token=_token_from_storage(str(data.get("token") or ""), encryptor),
        updated_at=str(data.get("updated_at") or ""),
        updated_by=str(data.get("updated_by") or ""),
    )


def merged_settings(current: GitSettings, changes: dict[str, Any]) -> GitSettings:
    """``current`` with ``changes`` applied and checked. A blank token keeps the
    saved one, so a form that was never sent the token can still be saved."""
    enabled = bool(changes.get("enabled", current.enabled))
    repo_url = str(changes.get("repo_url", current.repo_url) or "").strip()
    token = str(changes.get("token") or "").strip()
    if changes.get("clear_token"):
        kept_token = token
    else:
        kept_token = token or current.token
    result = GitSettings(
        enabled=enabled,
        repo_url=check_repo_url(repo_url) if repo_url or enabled else "",
        branch=check_branch(str(changes.get("branch", current.branch) or "")),
        folder=check_folder(str(changes.get("folder", current.folder) or "")),
        username=str(changes.get("username", current.username) or "").strip()[:200],
        token=kept_token,
        updated_at=current.updated_at,
        updated_by=current.updated_by,
    )
    if result.enabled and not result.token:
        raise GitSettingsError("Enter an access token that can write to this repository.")
    return result


def write_settings(db, encryptor, settings: GitSettings, actor: str) -> GitSettings:
    now = datetime.now(timezone.utc).isoformat()
    settings.updated_at = now
    settings.updated_by = actor
    data = {
        "enabled": settings.enabled,
        "repo_url": settings.repo_url,
        "branch": settings.branch,
        "folder": settings.folder,
        "username": settings.username,
        "token": _token_for_storage(settings.token, encryptor),
        "updated_at": now,
        "updated_by": actor,
    }
    db.execute_write(
        "INSERT OR REPLACE INTO settings (id, data, created_at) VALUES (?, ?, ?)",
        (SETTINGS_ROW, json.dumps(data), now),
    )
    return settings


def public_settings(settings: GitSettings) -> dict[str, Any]:
    """The settings as the API returns them: never the token itself."""
    return {
        "enabled": settings.enabled,
        "repo_url": settings.repo_url,
        "branch": settings.branch,
        "folder": settings.folder,
        "username": settings.username,
        "token_set": bool(settings.token),
        "updated_at": settings.updated_at,
        "updated_by": settings.updated_by,
    }
