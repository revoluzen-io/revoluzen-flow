"""File-system sandbox for the DuckDB connections that run pipeline SQL.

Why this exists
---------------
A pipeline step can carry arbitrary SQL (SQL Transform, filters, derived
columns, data-quality rules...). DuckDB SQL can read and write files:
``read_text('/path/secret.key')``, ``read_blob('revoluzen_flow.db')``,
``COPY (...) TO '/anywhere'``. The 2026-11-13 pen test used exactly that to
read the master encryption key and the app database, then decrypt every
stored credential offline.

Reading host files from a pipeline is a documented author capability (the
threat model's M1: a file source may point at any absolute path). What it
must never reach is the instance's own secrets, because on a server with
several workspaces those belong to everyone. So this module does not take
file access away; it takes away the secrets:

* DuckDB runs with ``enable_external_access=false`` and an allowlist
  (``allowed_directories`` / ``allowed_paths``), then ``lock_configuration``
  so pipeline SQL cannot turn it back on.
* The allowlist is: the data dir's own sub-folders (uploads, managed tables,
  outputs, spill...), user data files sitting in the data dir root, the
  DuckDB spill dir, the system temp dir, and every file / folder / bucket a
  step of THIS pipeline names in its parameters.
* Anything that is, or contains, a protected location is dropped from the
  allowlist: the master key file, the app's SQLite databases and their WAL
  / journal files, backups, the config home (``~/.revoluzen-flow``), the
  bootstrap password file. A step that names one of those simply fails with
  DuckDB's "Permission Error".

When it applies
---------------
``REVOLUZEN_FLOW_SQL_SANDBOX`` = 1/0 forces it on or off. Unset, it follows
the security mode: on in ``server`` mode (several people share the box),
off in ``local`` mode (the one user already owns the key file). DuckDB's
own guarantees were probed on 2.0.0: traversal (``../``), case changes,
backslashes, globbing, ATTACH, INSTALL and http are all refused outside the
allowlist; spilling to ``temp_directory`` keeps working.
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)

_REMOTE_SCHEMES = ("s3", "s3a", "s3n", "gs", "gcs", "az", "azure", "abfs", "abfss", "r2", "hf", "http", "https")
_REMOTE_RE = re.compile(r"^(?P<scheme>[a-z0-9]+)://(?P<host>[^/?#]+)", re.IGNORECASE)
_GLOB_CHARS = set("*?[{")
_DATA_EXTS = {
    ".csv", ".tsv", ".txt", ".json", ".jsonl", ".ndjson", ".parquet", ".pq",
    ".xlsx", ".xls", ".xml", ".avro", ".orc", ".arrow", ".feather", ".gz", ".zst",
}
# data_dir sub-folders that hold copies of the app database or secrets.
_PROTECTED_SUBDIRS = {"backups", "migration_backups", "secrets", "keys"}
# data_dir sub-folders runs read and write. Allowed even before they exist:
# a run creates tables/<ws>/<schema>/<table>/ the first time it loads one.
_DATA_SUBDIRS = ("tables", "outputs", "uploads", "samples", "cache", "staging",
                 "files", "exports", "step_cache", "sandbox", "duckdb_spill", "extraction")
# File-name rules for things in the data dir root (and anywhere else).
_PROTECTED_SUFFIXES = (".db", ".db-wal", ".db-shm", ".db-journal", ".sqlite", ".sqlite3",
                       ".sqlite-wal", ".sqlite-shm", ".key", ".pem", ".p12", ".pfx")
_PROTECTED_NAMES = {"initial_admin_password.txt", ".env", "storage_settings.json", "secret.key",
                    "setup_token.txt"}

# Extensions a step type needs DuckDB to have loaded before the lock goes on
# (INSTALL / LOAD of a new extension is refused once external access is off).
_STEP_EXTENSIONS = (
    ("adls", "azure"), ("azure", "azure"), ("gcs", "httpfs"), ("s3", "httpfs"),
)
_SCHEME_EXTENSIONS = {"s3": "httpfs", "s3a": "httpfs", "s3n": "httpfs", "gs": "httpfs",
                      "gcs": "httpfs", "r2": "httpfs", "http": "httpfs", "https": "httpfs",
                      "hf": "httpfs", "az": "azure", "azure": "azure", "abfs": "azure",
                      "abfss": "azure"}


def sandbox_enabled() -> bool:
    raw = os.environ.get("REVOLUZEN_FLOW_SQL_SANDBOX", "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    from revoluzen_flow import runtime_config
    return bool(runtime_config.IS_SERVER_MODE)


# ── path helpers ─────────────────────────────────────────────────────────

def _abs(p: str | os.PathLike) -> str:
    """Absolute and normalised, original case kept (this is what DuckDB sees)."""
    return os.path.normpath(os.path.abspath(os.path.expanduser(str(p))))


def _norm(p: str | os.PathLike) -> str:
    """Comparison form: absolute, normalised, case-folded where the OS is."""
    return os.path.normcase(_abs(p))


def _within(child: str, parent: str) -> bool:
    """True when ``child`` is ``parent`` or lies under it (both normalised)."""
    return child == parent or child.startswith(parent.rstrip(os.sep) + os.sep)


def _protected_name(name: str) -> bool:
    low = name.lower()
    return (
        low in _PROTECTED_NAMES
        or low.endswith(_PROTECTED_SUFFIXES)
        or "password" in low
        or "secret" in low
    )


def protected_locations(data_dir: str | None) -> list[str]:
    """Normalised files and folders pipeline SQL must never reach."""
    out: set[str] = set()
    try:
        from revoluzen_flow.security.encryptor import master_key_path
        out.add(_norm(master_key_path()))
    except Exception:  # noqa: BLE001 — never let the sandbox crash a run
        pass
    out.add(_norm(Path.home() / ".revoluzen-flow"))
    config_home = os.environ.get("REVOLUZEN_FLOW_CONFIG_HOME", "").strip()
    if config_home:
        out.add(_norm(config_home))
    if data_dir:
        root = _norm(data_dir)
        out.add(_norm(os.path.join(root, "secret.key")))
        for name in _PROTECTED_SUBDIRS:
            out.add(_norm(os.path.join(root, name)))
        try:
            for entry in os.scandir(root):
                if entry.is_file() and _protected_name(entry.name):
                    out.add(_norm(entry.path))
        except OSError:
            pass
    return sorted(out)


def is_protected_file(path: str, data_dir: str | None) -> bool:
    """True when ``path`` is one of the instance's secrets (always enforced,
    sandbox on or off). For code that reads a user-chosen file outside a
    pipeline run, e.g. the Copilot inspecting a file's columns: a CSV
    "header" read from the master key IS the key."""
    if not path:
        return False
    return _touches_protected(_norm(path), protected_locations(data_dir), is_dir=os.path.isdir(path))


def _touches_protected(candidate: str, protected: Iterable[str], is_dir: bool) -> bool:
    # Only the explicit list. File-name heuristics ("secret" / "password" in
    # the name, .db / .key suffixes) are applied when that list is built, and
    # only to the data dir root where the app keeps its own files; applied to
    # every declared path they blocked ordinary user data such as
    # "secret_santa.csv".
    for p in protected:
        if candidate == p or _within(candidate, p):
            return True              # the candidate is (inside) a protected place
        if is_dir and _within(p, candidate):
            return True              # allowing this folder would expose a protected file
    return False


def _string_params(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _string_params(v)
    elif isinstance(value, (list, tuple, set)):
        for v in value:
            yield from _string_params(v)


def _declared_locations(workflow: Any, data_dir: str | None):
    """(local_dirs, local_files, remote_prefixes, extensions) a pipeline names."""
    dirs: set[str] = set()
    files: set[str] = set()
    remote: set[str] = set()
    exts: set[str] = set()
    steps = getattr(workflow, "steps", None) or []
    for step in steps:
        stype = str(getattr(step, "type", "") or "").lower()
        for needle, ext in _STEP_EXTENSIONS:
            if needle in stype:
                exts.add(ext)
        params = getattr(step, "params", None) or {}
        for raw in _string_params(params):
            s = raw.strip()
            if not s or len(s) > 2048 or "\n" in s:
                continue
            m = _REMOTE_RE.match(s)
            if m:
                scheme = m.group("scheme").lower()
                if scheme in _REMOTE_SCHEMES:
                    remote.add(f"{scheme}://{m.group('host')}/")
                    if scheme in _SCHEME_EXTENSIONS:
                        exts.add(_SCHEME_EXTENSIONS[scheme])
                continue
            looks_like_path = (
                os.path.isabs(s) or "/" in s or "\\" in s
                or os.path.splitext(s)[1].lower() in _DATA_EXTS
            )
            # SQL text and expressions are not paths; a quote, parenthesis or
            # semicolon means this string is code, not a location.
            if not looks_like_path or any(ch in s for ch in "'\"();"):
                continue
            if os.path.isabs(s) or not data_dir:
                path = s
            else:
                # Reads resolve data_dir -> cwd -> repo root (nodes/_path_utils);
                # writes go under data_dir. Allow both so the node opens exactly
                # the file the sandbox expects.
                from revoluzen_flow.nodes._path_utils import resolve_input_path
                resolved = resolve_input_path(s, data_dir)
                path = os.path.join(data_dir, s)
                if resolved and _abs(resolved) != _abs(path) and not any(ch in s for ch in _GLOB_CHARS):
                    files.add(_abs(resolved))
            if any(ch in path for ch in _GLOB_CHARS):
                cut = min(path.find(ch) for ch in _GLOB_CHARS if ch in path)
                dirs.add(_abs(os.path.dirname(path[:cut]) or "."))
            elif os.path.isdir(path):
                dirs.add(_abs(path))
            else:
                files.add(_abs(path))
                # DuckDB's COPY ... TO writes "tmp_<name>" beside the target
                # and renames it, so a sink needs that twin allowed too.
                files.add(_abs(os.path.join(os.path.dirname(path), "tmp_" + os.path.basename(path))))
    return dirs, files, remote, exts


def allowlist(workflow: Any = None, data_dir: str | None = None) -> tuple[list[str], list[str], list[str]]:
    """(allowed_directories, allowed_paths, extensions_to_preload)."""
    protected = protected_locations(data_dir)
    dirs: set[str] = set()
    files: set[str] = set()

    if data_dir:
        for name in _DATA_SUBDIRS:
            dirs.add(_abs(os.path.join(data_dir, name)))
    if data_dir and os.path.isdir(data_dir):
        try:
            for entry in os.scandir(data_dir):
                if entry.is_dir():
                    dirs.add(_abs(entry.path))
                elif entry.is_file() and os.path.splitext(entry.name)[1].lower() in _DATA_EXTS:
                    files.add(_abs(entry.path))
        except OSError:
            pass
    try:
        from revoluzen_flow import runtime_config
        dirs.add(_abs(runtime_config.DUCKDB_TEMP_DIRECTORY))
    except Exception:  # noqa: BLE001
        pass
    dirs.add(_abs(tempfile.gettempdir()))

    d_dirs, d_files, remote, exts = _declared_locations(workflow, data_dir)
    dirs |= d_dirs
    files |= d_files

    allowed_dirs = sorted(d for d in dirs if not _touches_protected(_norm(d), protected, is_dir=True))
    allowed_files = sorted(f for f in files if not _touches_protected(_norm(f), protected, is_dir=False))
    dropped = (dirs - set(allowed_dirs)) | (files - set(allowed_files))
    if dropped:
        logger.info("sql_sandbox: kept %d protected location(s) out of the allowlist", len(dropped))

    def _duck(p: str, trailing: bool) -> str:
        p = p.replace("\\", "/")
        return p.rstrip("/") + "/" if trailing else p

    return (
        [_duck(d, True) for d in allowed_dirs] + sorted(remote),
        [_duck(f, False) for f in allowed_files],
        sorted(exts),
    )


def _sql_list(items: list[str]) -> str:
    return "[" + ", ".join("'" + i.replace("'", "''") + "'" for i in items) + "]"


def apply(conn, workflow: Any = None, data_dir: str | None = None, *, force: bool = False) -> bool:
    """Lock ``conn`` down. Returns True when the sandbox was applied.

    Call right after opening the connection and BEFORE any pipeline SQL runs.
    ``force`` applies it regardless of the security mode (used for scratch
    connections that should never touch files).
    """
    if not (force or sandbox_enabled()):
        return False
    allowed_dirs, allowed_files, exts = allowlist(workflow, data_dir)
    for ext in exts:
        try:
            conn.execute(f"LOAD {ext}")
        except Exception:  # noqa: BLE001
            try:
                conn.execute(f"INSTALL {ext}")
                conn.execute(f"LOAD {ext}")
            except Exception as exc:  # noqa: BLE001 — the node reports it
                logger.warning("sql_sandbox: could not preload DuckDB extension %s (%s)", ext, exc)
    if allowed_dirs:
        conn.execute(f"SET allowed_directories={_sql_list(allowed_dirs)}")
    if allowed_files:
        conn.execute(f"SET allowed_paths={_sql_list(allowed_files)}")
    conn.execute("SET enable_external_access=false")
    conn.execute("SET lock_configuration=true")
    return True


def require_single_statement(conn, sql: str, what: str = "This SQL") -> None:
    """Refuse SQL that is more than one statement.

    User SQL is spliced into a larger statement (``CREATE TEMP TABLE x AS
    {expr}``, ``EXPLAIN {expr}``). DuckDB runs every statement in the string,
    so ``SELECT 1; COPY (...) TO '/path'`` wrote a file in the pen test.
    DuckDB's own splitter is used, so semicolons inside strings and comments
    are not miscounted. Always enforced, sandbox on or off.
    """
    try:
        n = len(conn.extract_statements(sql))
    except Exception:  # noqa: BLE001 — a parse error surfaces when it runs
        return
    if n > 1:
        raise ValueError(f"{what} must be a single SQL statement (found {n}).")


def lock_scratch(conn) -> bool:
    """Sandbox a scratch connection (validation / previews) when the sandbox
    is enabled: no file or network access at all."""
    return apply(conn, None, None) if sandbox_enabled() else False
