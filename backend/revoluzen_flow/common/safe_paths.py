"""Filesystem path guards for untrusted, caller-supplied names.

Written 2026-10-23 after an unauthenticated probe wrote a file *outside*
the data directory through ``POST /api/upload``: the handler joined
``UploadFile.filename`` straight onto ``data_dir``, and a browser is free
to put anything in that field. ``filename="../escaped.csv"`` landed in the
data directory's parent.

Two helpers, deliberately separate because they answer different questions:

``safe_basename``  - "what should I call this file?" Collapses any path the
                     caller sent down to a single, inert filename.
``resolve_under``  - "is this path inside the directory I meant?" The final
                     containment check, done on the *resolved* path so
                     symlinks and ``..`` cannot smuggle a write out.

Use both on any write whose name comes from a request. ``resolve_under``
alone is not enough -- it would reject the traversal rather than defuse it,
turning a normal filename with a stray slash into a 400.

NOT for read paths that are legitimately absolute. A pipeline source node
pointing at ``D:\\exports\\sales.csv`` is the product working as intended,
so confining *reads* to the data directory would break it. The control for
those is authentication and role, not path shape.
"""

from __future__ import annotations

import ntpath
import os
import posixpath

__all__ = ["UnsafePathError", "safe_basename", "resolve_under"]


class UnsafePathError(ValueError):
    """A caller-supplied path escaped, or could not be reduced to, a safe name."""


# Reserved device names on Windows; writing to any of these opens a device,
# not a file, whatever extension is appended.
_WINDOWS_RESERVED = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}


def safe_basename(name: str | None, *, default: str = "upload.dat") -> str:
    """Reduce a caller-supplied filename to one inert path component.

    Handles both separator conventions regardless of host OS -- a Windows
    client can send ``a/b.csv`` and a POSIX client can send ``a\\b.csv``, and
    on the wrong host only one of those is a separator. Strips any drive or
    UNC prefix, drops every directory component, and rejects what is left if
    it is empty, a dot entry, or a Windows device name.

        >>> safe_basename("../../etc/passwd")
        'passwd'
        >>> safe_basename(r"C:\\Windows\\Temp\\x.csv")
        'x.csv'
        >>> safe_basename("..")
        Traceback (most recent call last):
        UnsafePathError: ...
    """
    if not name:
        return default

    # Strip a drive / UNC prefix under either convention before splitting,
    # so "C:x.csv" (drive-relative, no separator) cannot survive as a name.
    candidate = name.strip().replace("\x00", "")
    candidate = ntpath.splitdrive(candidate)[1]
    candidate = posixpath.splitdrive(candidate)[1]

    # Take the last component under BOTH separator conventions.
    candidate = candidate.replace("\\", "/").rstrip("/")
    candidate = candidate.rsplit("/", 1)[-1]
    candidate = candidate.strip().strip(".") if candidate in (".", "..") else candidate.strip()

    if not candidate or candidate in (".", ".."):
        raise UnsafePathError(f"{name!r} does not contain a usable filename")
    if os.path.splitext(candidate)[0].lower() in _WINDOWS_RESERVED:
        raise UnsafePathError(f"{candidate!r} is a reserved device name")
    return candidate


def resolve_under(root: str | os.PathLike[str], *parts: str) -> str:
    """Join ``parts`` onto ``root`` and assert the result stays inside it.

    The comparison is done with :func:`os.path.realpath` on both sides, so a
    symlink inside ``root`` that points out of it is caught too. ``root``
    itself is accepted; anything above or beside it is not.

    Raises :class:`UnsafePathError` rather than returning a flag, because
    every caller of this wants to abort, and a boolean invites being ignored.
    """
    root_real = os.path.realpath(str(root))
    target_real = os.path.realpath(os.path.join(root_real, *parts))
    if target_real != root_real and not target_real.startswith(root_real + os.sep):
        raise UnsafePathError(
            f"path escapes its root: {os.path.join(*parts) if parts else ''!r}"
        )
    return target_real
