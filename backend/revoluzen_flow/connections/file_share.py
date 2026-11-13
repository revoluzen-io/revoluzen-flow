"""Windows file shares (SMB): Test connection, browsing, the File step's
reading and writing, and the file-arrival trigger's look at a folder.

A saved connection of type ``smb`` names a server, a share and, if wanted, a
folder inside the share, and holds the sign-in: domain, user name and
password (the password is encrypted like every connection secret). It works
the same on Windows, Linux and Docker, because Flow speaks SMB 2 and 3 itself
through the ``smbprotocol`` package (MIT). It never uses the server's mapped
drives or the account Flow runs under; a Local Folder connection with a
``\\\\server\\share`` path still does that on a Windows server.

Reading copies the file, or every file whose name matches ``*`` and ``?``,
into a private temporary folder, where the File step reads it with any format
(fixed-width, COBOL copybook, EBCDIC, PGP-encrypted ...). Writing copies the
finished file under a temporary name and then renames it, so a program
watching the folder never picks up half a file.

Each operation opens its own connection and closes it at the end.
``smbclient`` otherwise keeps one process-wide pool and reuses a session for
any later call with the same user name, so two connections that share a user
name but not a password could ride on one sign-in.
"""

from __future__ import annotations

import errno
import itertools
import logging
import os
import re
import secrets
import shutil
import socket
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterator

logger = logging.getLogger(__name__)

CONNECTION_TYPE = "smb"
DEFAULT_PORT = 445
TEST_TIMEOUT = 10  # seconds to connect for Test connection and browsing
RUN_TIMEOUT = 30  # seconds to connect when a run reads or writes
LIST_LIMIT = 500  # entries shown when browsing or testing a folder
_COPY_CHUNK = 1024 * 1024

# NTSTATUS codes a server answers with (MS-ERREF 2.3.1).
_SIGN_IN_REFUSED = frozenset({
    0xC000006D,  # LOGON_FAILURE
    0xC000006A,  # WRONG_PASSWORD
    0xC000006E,  # ACCOUNT_RESTRICTION
    0xC000006F,  # INVALID_LOGON_HOURS
    0xC0000070,  # INVALID_WORKSTATION
    0xC0000071,  # PASSWORD_EXPIRED
    0xC0000072,  # ACCOUNT_DISABLED
    0xC0000193,  # ACCOUNT_EXPIRED
    0xC0000224,  # PASSWORD_MUST_CHANGE
    0xC0000234,  # ACCOUNT_LOCKED_OUT
})
_NO_SUCH_SHARE = frozenset({0xC00000CC})  # BAD_NETWORK_NAME
_DENIED = frozenset({0xC0000022})  # ACCESS_DENIED
_NOT_FOUND = frozenset({0xC0000034, 0xC000003A, 0xC0000225})  # OBJECT_NAME/OBJECT_PATH_NOT_FOUND, NOT_FOUND
_IN_USE = frozenset({0xC0000043})  # SHARING_VIOLATION
_FULL = frozenset({0xC000007F})  # DISK_FULL
_BAD_NAME = frozenset({0xC0000033, 0xC000003B})  # OBJECT_NAME_INVALID, OBJECT_PATH_SYNTAX_BAD
_IS_FOLDER = frozenset({0xC00000BA})  # FILE_IS_A_DIRECTORY

_WILDCARD = re.compile(r"[*?]")
_DRIVE = re.compile(r"^[A-Za-z]:")


class ShareError(ValueError):
    """A problem with the share, in words for the person on screen. Never
    carries the password."""


@dataclass(frozen=True)
class Location:
    """Where a connection points: ``\\\\server\\share`` and a folder in it."""

    server: str
    share: str
    folder: str = ""  # inside the share, joined with backslashes; "" is the top
    port: int = DEFAULT_PORT

    def unc(self, inner: str = "") -> str:
        """``\\\\server\\share\\inner``, where ``inner`` is a path inside the share."""
        root = f"\\\\{self.server}\\{self.share}"
        return f"{root}\\{inner}" if inner else root


# ── Reading the connection's settings ──

def _parts(value: Any) -> list[str]:
    """Path segments, whichever slash was typed."""
    return [
        part.strip() for part in str(value or "").replace("/", "\\").split("\\")
        if part.strip() and part.strip() != "."
    ]


def _climb(base: list[str], parts: list[str], shown: str) -> list[str]:
    """``base`` followed by ``parts``, with ``..`` applied; never above the top of the share."""
    out = list(base)
    for part in parts:
        if part == "..":
            if not out:
                raise ShareError(f"'{shown}' goes above the top of the share.")
            out.pop()
        else:
            out.append(part)
    return out


def _port(value: Any) -> int:
    if value in (None, ""):
        return DEFAULT_PORT
    try:
        port = int(str(value).strip())
    except ValueError:
        raise ShareError(f"The port '{value}' is not a number. Windows file sharing uses 445.") from None
    if not 0 < port < 65536:
        raise ShareError(f"The port {port} is out of range. Windows file sharing uses 445.")
    return port


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def _as_utc(value: Any) -> datetime | None:
    """A time from the server (``smbprotocol`` gives UTC datetimes)."""
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def location(config: dict[str, Any]) -> Location:
    """The connection's server, share and folder.

    A path copied from File Explorer (``\\\\fileserver01\\finance\\incoming``)
    into Server is split into server, share and folder when Share is empty.
    """
    server = _parts(config.get("host") or config.get("server"))
    share = _parts(config.get("share"))
    if len(server) > 1 and not share:
        server, share = server[:1], server[1:]
    if not server:
        raise ShareError("Give the server's name, for example fileserver01.")
    if len(server) > 1:
        raise ShareError(
            "Server takes only the server's name, for example fileserver01. "
            "The share and the folder have boxes of their own."
        )
    if not share:
        raise ShareError("Give the share's name: the part after the server in \\\\server\\share.")
    folder_text = config.get("base_folder") or config.get("folder") or ""
    folder = _climb([], share[1:] + _parts(folder_text), str(folder_text))
    return Location(server=server[0], share=share[0], folder="\\".join(folder), port=_port(config.get("port")))


def _inside(loc: Location, shown: str) -> list[str]:
    """The segments inside the share of a path typed as ``shown``, by the
    rules of :func:`inner_path`. An empty list is the top of the share."""
    text = shown.replace("/", "\\")
    if _DRIVE.match(text):
        raise ShareError(
            f"'{shown}' is a path on a computer, not on the share. Give the path inside the share, "
            "for example daily\\CUSTMAST.DAT."
        )
    if text.startswith("\\\\"):
        parts = _parts(text)
        if len(parts) < 2 or parts[0].lower() != loc.server.lower() or parts[1].lower() != loc.share.lower():
            raise ShareError(
                f"'{shown}' is not on {loc.unc()}. Give a path inside the connection's share, "
                "or save a connection for that share."
            )
        return _climb([], parts[2:], shown)
    if text.startswith("\\"):
        return _climb([], _parts(text), shown)
    return _climb(_parts(loc.folder), _parts(text), shown)


def inner_path(loc: Location, path: Any) -> str:
    """The step's path as a path inside the share.

    A relative path starts in the connection's folder; one starting with a
    single backslash or slash starts at the top of the share. A full
    ``\\\\server\\share\\...`` path must name the connection's own server and
    share, so the saved sign-in is never sent to another server.
    """
    shown = str(path or "").strip()
    if not shown:
        raise ShareError("Give the file's path on the share, for example daily\\CUSTMAST.DAT.")
    inner = _inside(loc, shown)
    if not inner:
        raise ShareError(f"'{shown}' is a folder. Give the file's name too.")
    return "\\".join(inner)


def inner_folder(loc: Location, folder: Any) -> str:
    """A folder on the share, read by the same rules as a file's path. Empty
    means the connection's own folder; ``""`` comes back for the top of the
    share."""
    shown = str(folder or "").strip()
    if not shown:
        return loc.folder
    if _WILDCARD.search(shown):
        raise ShareError("* and ? work in file names only, not in the folder.")
    return "\\".join(_inside(loc, shown))


def _split_pattern(inner: str) -> tuple[str, str | None]:
    """``(folder, name pattern)`` when the name has ``*`` or ``?``, else ``(inner, None)``."""
    folder, _, name = inner.rpartition("\\")
    if _WILDCARD.search(folder):
        raise ShareError("* and ? work in the file's name only, not in folder names.")
    return (folder, name) if _WILDCARD.search(name) else (inner, None)


def _name_matcher(pattern: str):
    """Match names the way Windows does: ``*`` and ``?``, any case. ``*.*``
    is the Windows habit for every file."""
    if pattern == "*.*":
        pattern = "*"
    regex = "".join(".*" if ch == "*" else "." if ch == "?" else re.escape(ch) for ch in pattern)
    return re.compile(rf"\A{regex}\Z", re.IGNORECASE | re.DOTALL).match


def _account(config: dict[str, Any]) -> str:
    user = str(config.get("username") or config.get("user") or "").strip()
    if not user:
        raise ShareError("Give the user name and password of an account that can open the share.")
    domain = str(config.get("domain") or "").strip().strip("\\")
    if domain and "\\" not in user and "@" not in user:
        return f"{domain}\\{user}"
    return user


# ── Errors in plain words ──

def _status(exc: BaseException) -> int | None:
    for name in ("ntstatus", "status"):
        value = getattr(exc, name, None)
        if isinstance(value, int) and not isinstance(value, bool):
            return value & 0xFFFFFFFF
    return None


def _missing(exc: BaseException) -> bool:
    return (
        _status(exc) in _NOT_FOUND
        or isinstance(exc, FileNotFoundError)
        or getattr(exc, "errno", None) == errno.ENOENT
    )


def explain(exc: BaseException, loc: Location, inner: str = "") -> str:
    """One plain sentence for what went wrong at ``inner`` on the share."""
    status = _status(exc)
    where = loc.unc(inner)
    kind = type(exc).__name__
    module = type(exc).__module__ or ""
    text = str(exc)
    if (status in _SIGN_IN_REFUSED or kind == "SMBAuthenticationError"
            or module.startswith("spnego") or "credential" in kind.lower()):
        return (
            f"{loc.server} refused the sign-in. Check the domain, user name and password, "
            "and that the account is not locked or expired."
        )
    if status in _NO_SUCH_SHARE:
        return f"{loc.server} has no share called '{loc.share}'. The share is the part after the server in \\\\server\\share."
    if status in _DENIED or isinstance(exc, PermissionError):
        return f"The account has no permission to use {where}."
    if _missing(exc):
        return f"{where} was not found."
    if status in _IS_FOLDER or getattr(exc, "errno", None) == errno.EISDIR or isinstance(exc, IsADirectoryError):
        return f"{where} is a folder. Give a file's name, or a name pattern such as *.dat."
    if status in _IN_USE:
        return f"{where} is open in another program. Try again once that program has finished with it."
    if status in _FULL:
        return f"The share is full, so {where} could not be written."
    if status in _BAD_NAME:
        return f"The share does not accept the name {where}. Windows names cannot contain < > : \" | ? or *."
    if "encrypt" in text.lower():
        return (
            f"{loc.server} cannot encrypt the connection, which needs SMB 3. Turn off "
            "'Require encryption', or ask for SMB 3 on the server."
        )
    if (isinstance(exc, (TimeoutError, ConnectionError, socket.gaierror))
            or text.startswith("Failed to connect to") or kind == "SMBConnectionClosed"
            or "timeout" in text.lower()):
        return (
            f"Could not reach {loc.server} on port {loc.port}. Check the server's name, and that this "
            "computer can reach it: Windows file sharing uses TCP port 445."
        )
    return f"{where}: {text or kind}"


# ── One signed-in connection ──

def _client():
    """The ``smbclient`` module, which ships with Flow (``smbprotocol``)."""
    try:
        import smbclient
    except ImportError:
        from revoluzen_flow.connections.addons import driver_message

        raise ShareError(driver_message("Windows file share", "smbprotocol")) from None
    return smbclient


class _Session:
    """A connection to the server with the saved sign-in, closed when the
    work is done."""

    def __init__(self, config: dict[str, Any], timeout: float) -> None:
        self.location = location(config)
        username = _account(config)
        self.client = _client()
        self._cache: dict[str, Any] = {}
        self.options: dict[str, Any] = {
            "username": username,
            "password": str(config.get("password") or ""),
            "port": self.location.port,
            "connection_timeout": timeout,
            "connection_cache": self._cache,
        }
        if _truthy(config.get("encrypt")):
            self.options["encrypt"] = True

    def __enter__(self) -> _Session:
        try:
            with self.explained(""):
                self.client.register_session(self.location.server, **self.options)
        except BaseException:
            self.close()
            raise
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    def close(self) -> None:
        try:
            self.client.reset_connection_cache(fail_on_error=False, connection_cache=self._cache)
        except Exception:  # noqa: BLE001 - closing must not hide the result
            logger.debug("Windows file share: closing the connection failed", exc_info=True)

    @contextmanager
    def explained(self, inner: str) -> Iterator[None]:
        try:
            yield
        except ShareError:
            raise
        except Exception as exc:  # noqa: BLE001 - smbprotocol, pyspnego and socket errors
            logger.debug("Windows file share error at %s", self.location.unc(inner), exc_info=True)
            raise ShareError(explain(exc, self.location, inner)) from None

    def entries(self, folder: str, pattern: str = "*", limit: int | None = None) -> list[dict[str, Any]]:
        """Name, kind and size of what is in ``folder``."""
        out: list[dict[str, Any]] = []
        with self.explained(folder):
            listing = self.client.scandir(self.location.unc(folder), search_pattern=pattern, **self.options)
            try:
                for entry in itertools.islice(listing, limit):
                    is_dir = bool(entry.is_dir())
                    size = None
                    if not is_dir:
                        try:
                            size = int(entry.stat().st_size)
                        except Exception:  # noqa: BLE001 - the size is only shown
                            size = None
                    out.append({"name": entry.name, "kind": "directory" if is_dir else "file", "size": size})
            finally:
                close = getattr(listing, "close", None)
                if callable(close):
                    close()
        return out

    def files(self, folder: str, limit: int | None = None) -> list[dict[str, Any]]:
        """The files directly in ``folder``, each with its size and last
        modified time. Both come with the listing itself (SMB directory
        entries carry them), so a folder of thousands of files costs a few
        requests, not one per file. Folders are left out, and so are links,
        which could lead to another server."""
        out: list[dict[str, Any]] = []
        with self.explained(folder):
            listing = self.client.scandir(self.location.unc(folder), search_pattern="*", **self.options)
            try:
                for entry in listing:
                    if entry.is_symlink() or entry.is_dir(follow_symlinks=False):
                        continue
                    info = entry.smb_info
                    out.append({
                        "name": entry.name,
                        "path": self.location.unc(f"{folder}\\{entry.name}" if folder else entry.name),
                        "size": int(info.end_of_file or 0),
                        "modified": _as_utc(info.last_write_time),
                    })
                    if limit is not None and len(out) >= limit:
                        break
            finally:
                close = getattr(listing, "close", None)
                if callable(close):
                    close()
        return out

    def writing(self, inner: str) -> bool:
        """Whether another program still has the file open to write it.

        This opens the file the way the File step does (others may read it,
        not write it), which the server refuses while a writer has it open.
        A Windows server's folder listing can show a file's final size and
        time before the program copying it in has finished, so this is the
        check that the file is complete. Other problems are left for the run
        to report."""
        where = self.location.unc(inner)
        try:
            with self.client.open_file(where, mode="rb", share_access="r", **self.options):
                return False
        except Exception as exc:  # noqa: BLE001 - only a sharing refusal means "still being written"
            if _status(exc) in _IN_USE:
                return True
            logger.debug("Windows file share: could not open %s to check it", where, exc_info=True)
            return False

    def fetch(self, inner: str, target: str) -> int:
        """Copy one file from the share to ``target``. Other programs may read
        it meanwhile but not write it, so a file still being written is
        refused instead of read half-way."""
        with self.explained(inner):
            with self.client.open_file(self.location.unc(inner), mode="rb", share_access="r", **self.options) as source, \
                    open(target, "wb") as out:
                shutil.copyfileobj(source, out, _COPY_CHUNK)
                return out.tell()

    def ensure_folder(self, folder: str) -> None:
        with self.explained(folder):
            try:
                self.client.stat(self.location.unc(folder), **self.options)
                return
            except Exception as exc:  # noqa: BLE001 - only "not there" means create it
                if not _missing(exc):
                    raise
            self.client.makedirs(self.location.unc(folder), exist_ok=True, **self.options)

    def put(self, local_file: str, inner: str) -> None:
        with self.explained(inner):
            with open(local_file, "rb") as source, \
                    self.client.open_file(self.location.unc(inner), mode="wb", **self.options) as out:
                shutil.copyfileobj(source, out, _COPY_CHUNK)

    def replace(self, inner: str, target: str) -> None:
        with self.explained(target):
            self.client.replace(self.location.unc(inner), self.location.unc(target), **self.options)

    def remove_quietly(self, inner: str) -> None:
        try:
            self.client.remove(self.location.unc(inner), **self.options)
        except Exception:  # noqa: BLE001 - the first error is the one to report
            logger.warning("Windows file share: could not remove the unfinished copy %s", self.location.unc(inner))


# ── What the rest of Flow calls ──

def download(config: dict[str, Any], path: str, work_dir: str, *, timeout: float = RUN_TIMEOUT) -> list[str]:
    """Copy the file the step names, or every file matching ``*`` and ``?``
    in its name, from the share into ``work_dir``. Returns the local copies
    in name order."""
    loc = location(config)
    inner = inner_path(loc, path)
    folder, pattern = _split_pattern(inner)
    with _Session(config, timeout) as session:
        if pattern is None:
            wanted = [inner]
        else:
            matches = _name_matcher(pattern)
            names = sorted(
                (e["name"] for e in session.entries(folder, pattern)
                 if e["kind"] == "file" and matches(e["name"])),
                key=str.lower,
            )
            if not names:
                raise ShareError(f"No file in {loc.unc(folder)} matches '{pattern}'.")
            wanted = [f"{folder}\\{name}" if folder else name for name in names]
        copies: list[str] = []
        for index, name in enumerate(wanted):
            extension = os.path.splitext(name.rpartition("\\")[2])[1]
            target = os.path.join(work_dir, f"download_{index}{extension}")
            size = session.fetch(name, target)
            logger.info("File: copied %s (%d bytes) from the file share", loc.unc(name), size)
            copies.append(target)
    return copies


def upload(config: dict[str, Any], local_file: str, path: str, *, timeout: float = RUN_TIMEOUT) -> str:
    """Copy a finished local file to the share, replacing a file of the same
    name. It is written under a temporary name next to the target and renamed
    when complete. Returns the file's full path on the share."""
    loc = location(config)
    inner = inner_path(loc, path)
    if _WILDCARD.search(inner):
        raise ShareError("* and ? cannot be part of the name of a file to write.")
    folder = inner.rpartition("\\")[0]
    partial = f"{inner}.{secrets.token_hex(4)}.partial"
    with _Session(config, timeout) as session:
        if folder:
            session.ensure_folder(folder)
        try:
            session.put(local_file, partial)
            session.replace(partial, inner)
        except BaseException:
            session.remove_quietly(partial)
            raise
    return loc.unc(inner)


def list_folder(config: dict[str, Any], folder: str = "", *, timeout: float = TEST_TIMEOUT,
                limit: int = LIST_LIMIT) -> tuple[str, list[dict[str, Any]]]:
    """``(where, entries)`` for a folder of the share: the connection's folder,
    or ``folder`` inside it. Folders come first, then files, by name."""
    loc = location(config)
    inner = inner_path(loc, folder) if str(folder or "").strip() else loc.folder
    with _Session(config, timeout) as session:
        found = session.entries(inner, limit=limit)
    found.sort(key=lambda e: (e["kind"] != "directory", e["name"].lower()))
    return loc.unc(inner), found


def list_files(config: dict[str, Any], folder: str = "", *, timeout: float = RUN_TIMEOUT,
               limit: int | None = None) -> tuple[str, list[dict[str, Any]]]:
    """``(where, files)``: the files directly in a folder of the share, for the
    file-arrival trigger. ``folder`` is read like a step's path (empty is the
    connection's folder, a leading ``\\`` the top of the share). Each file has
    ``name``, ``path`` (its full ``\\\\server\\share\\...`` path, which the
    File step reads with this connection), ``size`` and ``modified`` (UTC)."""
    loc = location(config)
    inner = inner_folder(loc, folder)
    with _Session(config, timeout) as session:
        return loc.unc(inner), session.files(inner, limit=limit)


def in_use(config: dict[str, Any], path: str, *, timeout: float = RUN_TIMEOUT) -> bool:
    """Whether another program still has the file at ``path`` open to write
    it (see ``_Session.writing``)."""
    loc = location(config)
    inner = inner_path(loc, path)
    with _Session(config, timeout) as session:
        return session.writing(inner)


def check(config: dict[str, Any], *, timeout: float = TEST_TIMEOUT) -> dict[str, Any]:
    """Sign in and list the connection's folder: what Test connection does."""
    where, found = list_folder(config, timeout=timeout)
    return {
        "where": where,
        "entries": len(found),
        "more": len(found) >= LIST_LIMIT,
    }
