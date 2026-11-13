"""
Universal File Source / Sink — one node per direction, format auto-detected.

Why a single node instead of CSV/JSON/Parquet/Excel/XML tiles?
  Most users just want "read this file". The format is already encoded in
  the filename. Asking them to pick the right tile out of five is friction.
  This node sniffs the extension, dispatches to the right reader/writer,
  and only surfaces format-specific options when relevant.

Format detection rules (extension → handler):
  .csv  .tsv                       → DuckDB read_csv_auto
  .json .ndjson .jsonl             → DuckDB read_json_auto
  .parquet .pq                     → DuckDB read_parquet
  .xlsx .xls                       → openpyxl → DuckDB
  .xml                             → ElementTree → DuckDB
  (anything else)                  → user must override format explicitly

Formats for mainframe and older systems (November 2026), chosen in Format
because their extension says nothing (``.dat``, ``.txt``, no extension):
  fixed_width                      → legacy_files.fixed_width
  copybook                         → legacy_files.copybook (COBOL records)
and EBCDIC code pages in Encoding, which also apply to CSV / TSV.

A file ending in .pgp, .gpg or .asc is decrypted first when "Decrypt with
PGP" is on (key and passphrase from a saved credential, see
``openpgp/credentials.py``), and its format is read from the name without
that ending. The File sink can encrypt what it writes the same way.

Files on an FTP / SFTP server or a Windows file share (SMB) are copied to a
private temporary folder and read there; the File sink writes to a Windows
file share the same way (``connections/file_share.py``).

Power users who want zero ambiguity can still use the dedicated CSV /
JSON / Parquet / Excel / XML tiles — those nodes remain registered.
The default palette just hides them in favour of this universal tile.
"""

from __future__ import annotations

import glob
import itertools
import json
import logging
import os
import shutil
import tempfile
import uuid
from typing import Any, TYPE_CHECKING

# Stage 2.5b: duckdb is referenced only in type annotations — node
# execute() return types and helper signatures. Runtime work goes
# through ctx.conn (owned by ExecutionContext).
if TYPE_CHECKING:
    import duckdb

from revoluzen_flow.ir.schema import StepType
from revoluzen_flow.nodes.base import BaseNode, ExecutionContext
from revoluzen_flow.nodes.registry import register

logger = logging.getLogger(__name__)

# Read by Flow's own readers (legacy_files/), never handed to DuckDB as a path.
_LEGACY_FORMATS = ("fixed_width", "copybook")
_ENCRYPTED_EXTENSIONS = (".pgp", ".gpg", ".asc")
# Encodings DuckDB's CSV reader takes as they are; any other is converted to
# UTF-8 first (until November 2026 the Encoding setting was not used at all).
_NATIVE_CSV_ENCODINGS = {"", "utf-8", "utf8", "utf_8", "utf-8-sig", "ascii"}


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def _plain_name(path: str) -> str:
    """The name without a .pgp / .gpg / .asc ending: ``orders.csv.pgp`` → ``orders.csv``."""
    lower = path.lower()
    for ending in _ENCRYPTED_EXTENSIONS:
        if lower.endswith(ending):
            return path[: -len(ending)]
    return path


def _needs_transcoding(encoding: str) -> bool:
    return (encoding or "").strip().lower() not in _NATIVE_CSV_ENCODINGS


# ── Extension → format mapping ──

_EXT_FORMAT = {
    ".csv":     "csv",
    ".tsv":     "tsv",
    ".txt":     "csv",      # treat as CSV with user-provided delimiter
    ".json":    "json",
    ".ndjson":  "ndjson",
    ".jsonl":   "ndjson",
    ".parquet": "parquet",
    ".pq":      "parquet",
    ".xlsx":    "excel",
    ".xls":     "excel",
    ".xml":     "xml",
    # 2026-06-01: columnar-format readers — both go through pyarrow then
    # land in DuckDB as a relation via `conn.from_arrow()`. ORC has no
    # new dependency (pyarrow is already in requirements); Avro requires
    # the `fastavro` package (also added to requirements.txt).
    ".avro":    "avro",
    ".orc":     "orc",
}


def _detect_format(file_path: str, override: str = "") -> str:
    """Return one of csv/tsv/json/ndjson/parquet/excel/xml/orc/avro, or the
    explicit override (which also allows fixed_width and copybook)."""
    if override and override != "auto":
        return override
    ext = os.path.splitext(_plain_name(file_path))[1].lower()
    fmt = _EXT_FORMAT.get(ext)
    if not fmt:
        raise ValueError(
            f"File node: cannot detect format for '{file_path}' (extension '{ext}'). "
            f"Set the Format field explicitly. Supported: {sorted(set(_EXT_FORMAT.values()))}. "
            "Fixed-width text and COBOL copybook records are always chosen in Format."
        )
    return fmt


def _resolve(file_path: str, data_dir: str) -> str:
    """Resolve a file_path for WRITES. Always joins relative paths with
    `data_dir` so sinks land deterministically — never redirects to a
    pre-existing file elsewhere. Read-path resolution uses
    `_resolve_read` below (which has a project-root fallback for
    sample-pack pipelines)."""
    if not file_path:
        raise ValueError("File node: file_path is required")
    if not os.path.isabs(file_path):
        file_path = os.path.join(data_dir, file_path)
    return file_path


def _sql_text(value: Any) -> str:
    """A value for a single-quoted DuckDB literal: quotes doubled, so a
    secret containing one cannot end the statement."""
    return str(value).replace("'", "''")


def _s3_settings(config: dict) -> dict[str, Any]:
    """DuckDB S3 settings from an S3 or MinIO connection's fields.

    The Connections form saves ``access_key``, ``secret_key``, ``region``
    and ``endpoint_url`` (with its scheme, e.g. ``http://minio:9000``),
    plus an ``ssl`` switch for MinIO. DuckDB wants the endpoint as
    host[:port] and plain HTTP said with ``s3_use_ssl``.
    """
    settings: dict[str, Any] = {}
    key = config.get("access_key") or config.get("access_key_id") or config.get("key", "")
    secret = config.get("secret_key") or config.get("secret_access_key") or config.get("secret", "")
    if key and secret:
        settings["s3_access_key_id"] = key
        settings["s3_secret_access_key"] = secret
    if config.get("region"):
        settings["s3_region"] = config["region"]
    endpoint = str(config.get("endpoint_url") or config.get("endpoint") or "").strip()
    if endpoint:
        scheme, sep, host = endpoint.partition("://")
        if not sep:
            scheme, host = "", endpoint
        settings["s3_endpoint"] = host.rstrip("/")
        settings["s3_url_style"] = "path"
        if scheme.lower() in ("http", "https"):
            settings["s3_use_ssl"] = scheme.lower() == "https"
        elif "ssl" in config:
            ssl = config["ssl"]
            settings["s3_use_ssl"] = ssl if isinstance(ssl, bool) else str(ssl).strip().lower() in ("true", "1", "yes", "on")
    return settings


def _resolve_read(file_path: str, data_dir: str) -> str:
    """Resolve a file_path for READS. Delegates to the shared helper so
    sample-pack project-relative paths (e.g.
    `samples/free-api-pipelines/data/orders.csv`) work without the
    data_dir-doubling workaround. See `_path_utils.py` for the full
    rationale (2026-05-26 fix)."""
    if not file_path:
        raise ValueError("File node: file_path is required")
    from revoluzen_flow.nodes._path_utils import resolve_input_path
    return resolve_input_path(file_path, data_dir)


# ── Source ──

DEV_SAMPLE_ROWS = 1000  # limit in dev mode; full_run bypasses

# Avro is read in bounded batches so peak Python-heap is one batch, not the
# whole file (see FileSourceNode._read_avro). Process-global counter gives each
# read a collision-free DuckDB temp-table name.
_AVRO_BATCH_ROWS = 50_000
_AVRO_TMP_SEQ = itertools.count()


@register(StepType.FILE_SOURCE)
class FileSourceNode(BaseNode):
    """Read any supported file format. Format detected from extension.

    Enterprise features:
      - Connection-based file access (S3, SFTP, etc.) — resolves path via connection
      - Multi-file glob patterns (data/*.csv)
      - Encoding selection (utf-8, latin-1, cp1252, etc.)
      - Compression (auto-detected: .gz, .bz2, .zst, .xz)
      - Dev sample limit (DEV_SAMPLE_ROWS) — bypassed in full_run mode
      - File not found → clear error with path
    """
    display_name = "File"
    category = "source"
    description = "Read a file — format detected automatically from the file extension"

    def execute(self, ctx: ExecutionContext) -> duckdb.DuckDBPyRelation:
        raw_path = self.params.get("file_path") or self.params.get("path") or ""
        connection_id = self.params.get("connection_id", "")

        # Connection-based path resolution
        from_storage = False
        remote = None
        if connection_id:
            remote = self._remote_connection(ctx, connection_id)
            if remote is None:
                raw_path, from_storage = self._resolve_via_connection(ctx, connection_id, raw_path)

        # An S3 / MinIO connection hands back an s3:// URL that DuckDB reads
        # itself; it is not a file on this machine to look for. Only such a
        # connection takes this branch, so a URL typed into the step is never
        # fetched from the server. An FTP / SFTP connection's path is a path
        # on that server, downloaded below.
        if from_storage or remote is not None:
            file_path = raw_path
        # Glob patterns are passed straight through (DuckDB handles them)
        elif "*" not in raw_path:
            file_path = _resolve_read(raw_path, ctx.data_dir)
            if not os.path.isfile(file_path):
                raise ValueError(f"File: not found: {file_path}")
        else:
            file_path = _resolve_read(raw_path, ctx.data_dir)

        fmt = _detect_format(file_path, self.params.get("format", "auto"))
        encoding = self.params.get("encoding", "utf-8")
        decrypt = _truthy(self.params.get("pgp_decrypt"))
        if not decrypt:
            self._refuse_encrypted(file_path, local=not (from_storage or remote))

        special = (
            decrypt or remote is not None or fmt in _LEGACY_FORMATS
            or (fmt in ("csv", "tsv") and _needs_transcoding(encoding))
        )
        if special:
            rel = self._read_special(ctx, file_path, fmt, encoding, decrypt=decrypt,
                                     from_storage=from_storage, remote=remote)
        else:
            rel = self._read_by_format(ctx, file_path, fmt, encoding)

        # Dev sample limit. A Sample run's explicit cap wins over this
        # node's default so every source reads the same number of rows;
        # an operator-set `sample_rows` still wins over both.
        limit = self._read_limit(ctx)
        if limit:
            file_src_sample = ctx.register_scoped("__file_src_sample", rel)
            rel = ctx.conn.sql(f"SELECT * FROM {file_src_sample} LIMIT {limit}")

        return rel

    def _read_limit(self, ctx: ExecutionContext) -> int | None:
        """Rows to read in a sample or editor run; ``None`` in a full run."""
        if ctx.full_run:
            return None
        preview_cap = getattr(ctx, "sample_row_limit", None)
        # The editor's File form saves its Dev Sample Limit as dev_sample_rows;
        # until November 2026 only sample_rows was read, so the box did nothing.
        chosen = self.params.get("dev_sample_rows")
        if chosen in (None, ""):
            chosen = self.params.get("sample_rows")
        try:
            limit = int(chosen if chosen not in (None, "") else (preview_cap or DEV_SAMPLE_ROWS))
        except (TypeError, ValueError):
            limit = DEV_SAMPLE_ROWS
        return limit if limit > 0 else None

    @staticmethod
    def _refuse_encrypted(file_path: str, *, local: bool) -> None:
        """Reading a PGP file without decrypting it gives nonsense; say so."""
        lower = file_path.lower()
        looks_encrypted = lower.endswith((".pgp", ".gpg"))
        if not looks_encrypted and local and lower.endswith(".asc") and os.path.isfile(file_path):
            with open(file_path, "rb") as head:
                looks_encrypted = head.read(64).lstrip().startswith(b"-----BEGIN PGP MESSAGE")
        if looks_encrypted:
            raise ValueError(
                f"File: '{os.path.basename(file_path)}' is PGP-encrypted. Turn on 'Decrypt with PGP' "
                "and choose the credential that holds your private key."
            )

    # ── Mainframe formats, EBCDIC text, PGP and FTP / SFTP ──

    def _read_special(self, ctx: ExecutionContext, file_path: str, fmt: str, encoding: str, *,
                      decrypt: bool, from_storage: bool, remote) -> "duckdb.DuckDBPyRelation":
        """Bring the file(s) to a private temporary folder when needed, decrypt
        them, and read them with any format. The result is a DuckDB table, so
        the temporary files can go as soon as this returns."""
        work = tempfile.mkdtemp(prefix="flow_file_")
        try:
            paths = self._local_inputs(ctx, file_path, work, from_storage=from_storage, remote=remote)
            if decrypt:
                paths = self._decrypt_inputs(ctx, paths, file_path, work)
            return self._read_local(ctx, paths, fmt, encoding, work)
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def _local_inputs(self, ctx: ExecutionContext, file_path: str, work: str, *,
                      from_storage: bool, remote) -> list[str]:
        suffix = os.path.splitext(file_path)[1]
        if remote is not None:
            from revoluzen_flow.nodes import _remote_files

            config, conn_type = remote
            if conn_type == "smb":
                # A Windows file share: the file, or every file matching * and ?
                # in its name, is copied here first (connections/file_share.py).
                from revoluzen_flow.connections import file_share

                try:
                    return file_share.download(config, file_path, work)
                except file_share.ShareError as exc:
                    raise ValueError(f"File: {exc}") from None
            target = os.path.join(work, f"download{suffix}")
            _remote_files.download(config, conn_type, file_path, target)
            return [target]
        if from_storage:
            if "*" in file_path:
                urls = [row[0] for row in ctx.conn.execute("SELECT file FROM glob(?)", [file_path]).fetchall()]
            else:
                urls = [file_path]
            if not urls:
                raise ValueError(f"File: no files match '{file_path}'.")
            paths = []
            for index, url in enumerate(sorted(urls)):
                row = ctx.conn.execute("SELECT content FROM read_blob(?)", [url]).fetchone()
                target = os.path.join(work, f"download_{index}{os.path.splitext(url)[1]}")
                with open(target, "wb") as out:
                    out.write(bytes(row[0]) if row and row[0] is not None else b"")
                paths.append(target)
            return paths
        if "*" in file_path:
            matches = sorted(p for p in glob.glob(file_path) if os.path.isfile(p))
            if not matches:
                raise ValueError(f"File: no files match '{file_path}'.")
            return matches
        return [file_path]

    def _decrypt_inputs(self, ctx: ExecutionContext, paths: list[str], file_path: str,
                        work: str) -> list[str]:
        from revoluzen_flow.openpgp import PgpError, decrypt_file
        from revoluzen_flow.openpgp.credentials import decryption_keys, load_credential, verification_keys

        workflow = getattr(ctx, "_workflow_ref", None)
        workspace = getattr(workflow, "workspace_id", None) or "default"
        try:
            config = load_credential(ctx.app_state, str(self.params.get("pgp_credential_id") or ""), workspace)
            keys, passphrase = decryption_keys(config)
        except PgpError as exc:
            raise ValueError(f"File: {exc}") from None
        # "Check the sender's signature": the file must be signed with the
        # sender's key, or the run stops (openpgp/signatures.py).
        verify_keys = None
        if _truthy(self.params.get("pgp_verify")):
            sender_id = str(self.params.get("pgp_verify_credential_id") or "")
            if not sender_id:
                raise ValueError("File: choose the credential with the sender's public key, or turn off "
                                 "'Check the sender's signature'.")
            try:
                verify_keys = verification_keys(load_credential(ctx.app_state, sender_id, workspace))
            except PgpError as exc:
                raise ValueError(f"File: the sender's public key: {exc}") from None
        inner_suffix = os.path.splitext(_plain_name(file_path))[1]
        plain_paths = []
        for index, path in enumerate(paths):
            target = os.path.join(work, f"plain_{index}{inner_suffix}")
            try:
                result = decrypt_file(path, target, keys, passphrase, verify_keys=verify_keys)
            except PgpError as exc:
                raise ValueError(f"File: could not read '{os.path.basename(path)}': {exc}") from None
            if result.signature is not None:
                signed = (f", signature checked: signed by {result.signature.signer} with key "
                          f"{result.signature.fingerprint} ({result.signature.hash_name})")
            elif result.signed_by:
                signed = f", signed by {', '.join(result.signed_by)} (signature not checked)"
            else:
                signed = ""
            logger.info(
                "File: %s %s (%d bytes)%s%s",
                "decrypted" if result.encrypted else "read signed file", os.path.basename(path),
                result.bytes_written, f" with key {result.key_id}" if result.key_id else "", signed,
            )
            plain_paths.append(target)
        return plain_paths

    def _copybook_text(self, ctx: ExecutionContext) -> str:
        text = str(self.params.get("copybook") or "")
        if text.strip():
            return text
        path = str(self.params.get("copybook_path") or "").strip()
        if not path:
            raise ValueError(
                "File: a COBOL copybook is needed to read these records. Paste it in Copybook, "
                "or give the path of the copybook file."
            )
        resolved = _resolve_read(path, ctx.data_dir)
        if not os.path.isfile(resolved):
            raise ValueError(f"File: the copybook file was not found: {resolved}")
        with open(resolved, "rb") as handle:
            data = handle.read()
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            return data.decode("latin-1")

    def _read_local(self, ctx: ExecutionContext, paths: list[str], fmt: str, encoding: str,
                    work: str) -> "duckdb.DuckDBPyRelation":
        limit = self._read_limit(ctx)
        if fmt == "fixed_width":
            from revoluzen_flow.legacy_files.fixed_width import read_fixed_width

            rel, info = read_fixed_width(ctx.conn, paths, self.params, limit=limit)
            if info.get("bad_values"):
                logger.info("File: %d fixed-width values could not be read and were left empty", info["bad_values"])
            return rel
        if fmt == "copybook":
            from revoluzen_flow.legacy_files.copybook import CopybookError, read_copybook_files

            try:
                rel, info = read_copybook_files(ctx.conn, paths, self._copybook_text(ctx), self.params, limit=limit)
            except CopybookError as exc:
                raise ValueError(f"File: {exc}") from None
            if info.get("bad_values"):
                logger.info("File: %d copybook values could not be read and were left empty", info["bad_values"])
            if info.get("unmatched_redefines"):
                logger.info(
                    "File: %d records matched no REDEFINES rule; their REDEFINES fields are empty",
                    info["unmatched_redefines"],
                )
            return rel
        if fmt in ("csv", "tsv") and _needs_transcoding(encoding):
            paths = [self._to_utf8(path, encoding, work, index, limit) for index, path in enumerate(paths)]
        relations = [self._read_by_format(ctx, path, fmt, "utf-8") for path in paths]
        return self._materialize(ctx, relations)

    def _to_utf8(self, path: str, encoding: str, work: str, index: int, limit: int | None) -> str:
        """Convert a text file in another code page (EBCDIC, Latin-1...) to UTF-8
        for DuckDB's CSV reader. A sample run converts only the lines it needs."""
        from revoluzen_flow.legacy_files import codepages
        from revoluzen_flow.legacy_files.records import iter_records

        name = codepages.normalise(encoding, default="utf-8")
        target = os.path.join(work, f"utf8_{index}.csv")
        lines_wanted = None if limit is None else limit + 1 + int(self.params.get("skip_rows") or 0)
        with open(path, "rb") as source, open(target, "w", encoding="utf-8", newline="") as out:
            if codepages.single_byte(name):
                decode = codepages.decoder(name)
                for _number, _offset, record in iter_records(
                    source, "lines", ebcdic=codepages.is_ebcdic(name), limit=lines_wanted,
                ):
                    out.write(decode(record))
                    out.write("\n")
            else:
                import io

                text = io.TextIOWrapper(source, encoding=name, errors="replace", newline="")
                for number, line in enumerate(text, start=1):
                    out.write(line)
                    if lines_wanted is not None and number >= lines_wanted:
                        break
        return target

    @staticmethod
    def _materialize(ctx: ExecutionContext, relations: list) -> "duckdb.DuckDBPyRelation":
        """Copy file scans into one table before their temporary files go."""
        table = f"__file_read_{uuid.uuid4().hex[:16]}"
        names = []
        for index, relation in enumerate(relations):
            name = f"{table}_{index}"
            relation.create(name)
            names.append(f'SELECT * FROM "{name}"')
        ctx.conn.execute(f'CREATE TEMP TABLE "{table}" AS ' + " UNION ALL BY NAME ".join(names))
        for index in range(len(relations)):
            ctx.conn.execute(f'DROP TABLE IF EXISTS "{table}_{index}"')
        return ctx.conn.table(table)

    def _remote_connection(self, ctx: ExecutionContext, connection_id: str):
        """``(config, type)`` when the connection is FTP / FTPS / SFTP or a
        Windows file share (SMB), whose files are copied here to be read; else
        ``None``."""
        if not ctx.app_state.get("connection_store"):
            return None
        from revoluzen_flow.nodes._remote_files import REMOTE_TYPES
        from revoluzen_flow.nodes.db_source import _get_connection_config

        workflow = getattr(ctx, "_workflow_ref", None)
        workspace = getattr(workflow, "workspace_id", None) if workflow is not None else None
        resolved = _get_connection_config(connection_id, ctx.app_state, workspace_id=workspace)
        if resolved is None:
            return None
        config, conn_type = resolved
        kind = str(conn_type or "").lower()
        if kind in REMOTE_TYPES or kind == "smb":
            return config, kind
        return None

    def _read_by_format(self, ctx: ExecutionContext, file_path: str,
                        fmt: str, encoding: str) -> duckdb.DuckDBPyRelation:
        """Dispatch to the right reader based on format."""
        if fmt in ("csv", "tsv"):
            delimiter = "\t" if fmt == "tsv" else self.params.get("delimiter", ",")
            null_str = self.params.get("null_string", "")
            quote_char = self.params.get("quote_char", '"')
            kwargs: dict[str, Any] = {
                "delimiter": delimiter,
                "header": self.params.get("header", True),
            }
            if null_str:
                kwargs["nullstr"] = null_str
            if quote_char and quote_char != '"':
                kwargs["quote"] = quote_char
            return ctx.conn.read_csv(file_path, **kwargs)
        if fmt == "json":
            return ctx.conn.read_json(file_path, format="array")
        if fmt == "ndjson":
            return ctx.conn.read_json(file_path, format="newline_delimited")
        if fmt == "parquet":
            return ctx.conn.read_parquet(file_path)
        if fmt == "excel":
            return self._read_excel(ctx.conn, file_path)
        if fmt == "xml":
            return self._read_xml(ctx.conn, file_path)
        if fmt == "orc":
            return self._read_orc(ctx.conn, file_path)
        if fmt == "avro":
            return self._read_avro(ctx.conn, file_path)
        raise ValueError(f"File: unsupported format '{fmt}'")

    def _read_orc(self, conn, file_path: str) -> "duckdb.DuckDBPyRelation":
        """Read an Apache ORC file via pyarrow → DuckDB relation.

        ORC is a columnar format common in Hadoop / Hive / Spark
        outputs. pyarrow ships native ORC support; we read the whole
        file into an Arrow Table and let DuckDB take it as a relation
        — same end-state as `read_parquet` so all downstream nodes
        see uniform DuckDB semantics.

        Windows note: pyarrow.orc's C++ backend requires an IANA tz
        database. Linux/Mac have one at `/usr/share/zoneinfo`; on
        Windows we point TZDIR at the `tzdata` PyPI package's bundled
        copy. Without this the first ORC read errors with
        "IANA time zone database is unavailable".

        Raises a clear, actionable error if pyarrow.orc / tzdata can't
        be loaded.
        """
        # Windows tz-database shim — set TZDIR before the first ORC
        # call. Idempotent: respects TZDIR if the operator already set
        # it (e.g. to a system zoneinfo path in a container).
        if not os.environ.get("TZDIR"):
            try:
                import tzdata  # type: ignore  # noqa: WPS433
                os.environ["TZDIR"] = os.path.join(
                    os.path.dirname(tzdata.__file__), "zoneinfo"
                )
            except ImportError:
                # tzdata is in requirements.txt; if it's missing the
                # operator skipped the install. Fall through and let
                # pyarrow's own error surface — the message below
                # tells them how to fix it.
                pass
        try:
            import pyarrow.orc as _orc  # noqa: WPS433
        except ImportError as exc:  # pragma: no cover - shipping pyarrow
            raise ValueError(
                "ORC reader: pyarrow.orc is not available. "
                "Install / upgrade pyarrow: `pip install -U pyarrow`."
            ) from exc
        try:
            table = _orc.read_table(file_path)
        except Exception as exc:
            msg = str(exc)
            if "time zone" in msg.lower() or "tzdir" in msg.lower():
                raise ValueError(
                    "ORC reader: IANA tz database is required by "
                    "pyarrow.orc but isn't available. Install the "
                    "`tzdata` package (`pip install tzdata`) or set "
                    "TZDIR to a system zoneinfo directory. Both are "
                    "listed in backend/requirements.txt."
                ) from exc
            raise
        return conn.from_arrow(table)

    def _read_avro(self, conn, file_path: str) -> "duckdb.DuckDBPyRelation":
        """Read an Apache Avro file via fastavro → Arrow → DuckDB, in bounded
        batches.

        Avro is row-oriented + schema-embedded. `fastavro.reader` yields records
        lazily; we accumulate them in fixed-size batches (`_AVRO_BATCH_ROWS`),
        convert each batch to a pyarrow Table, and append it into a DuckDB temp
        table. Peak Python-heap is therefore one batch — not the whole file —
        and the temp table is governed by the executor's DuckDB ``memory_limit``
        + spill-to-disk, so a multi-GB Avro file no longer materialises entirely
        in RAM (the previous ``list(reader)`` did, outside that cap).

        The Arrow schema is pinned from the first batch so an optional field that
        is all-null in a later batch doesn't drift its inferred type and break
        the append. (A first batch of 50k rows is a representative sample; the
        rare all-null-in-first-batch case is the one residual edge.)

        Raises a clear error pointing at the install fix if `fastavro`
        isn't on PATH.
        """
        try:
            import fastavro  # noqa: WPS433
        except ImportError as exc:
            raise ValueError(
                "Avro reader: `fastavro` package is not installed. "
                "Install it: `pip install fastavro>=1.9`. "
                "It's also listed in backend/requirements.txt for fresh installs."
            ) from exc
        try:
            import pyarrow as pa
        except ImportError as exc:  # pragma: no cover - pyarrow is core
            raise ValueError("pyarrow is required for Avro reading") from exc

        tmp = f"__avro_{next(_AVRO_TMP_SEQ)}"
        batch: list[dict] = []
        state = {"schema": None, "created": False}

        def _flush() -> None:
            if not batch:
                return
            if state["schema"] is None:
                table = pa.Table.from_pylist(batch)
                state["schema"] = table.schema
            else:
                table = pa.Table.from_pylist(batch, schema=state["schema"])
            conn.register("__avro_batch", table)
            try:
                if not state["created"]:
                    conn.execute(f'CREATE TEMP TABLE "{tmp}" AS SELECT * FROM __avro_batch')
                    state["created"] = True
                else:
                    conn.execute(f'INSERT INTO "{tmp}" SELECT * FROM __avro_batch')
            finally:
                conn.unregister("__avro_batch")
            batch.clear()

        with open(file_path, "rb") as f:
            for record in fastavro.reader(f):
                batch.append(record)
                if len(batch) >= _AVRO_BATCH_ROWS:
                    _flush()
            _flush()

        if not state["created"]:
            # Empty file — return a zero-row, zero-column relation so
            # downstream nodes get a valid (if empty) cursor.
            return conn.sql("SELECT * FROM (VALUES (NULL)) AS t(col) WHERE 1=0")

        return conn.sql(f'SELECT * FROM "{tmp}"')

    def _resolve_via_connection(self, ctx: ExecutionContext,
                                connection_id: str, path: str) -> tuple[str, bool]:
        """Resolve file path using a connection (S3, SFTP, local share, etc.).

        Returns the path and whether it is an object-storage URL for DuckDB
        to read. The connection is read the way every other step reads one:
        secrets decrypted, any linked credential merged. (Until November 2026
        this called ``.get()`` on the Connection object and always failed.)
        """
        if not ctx.app_state.get("connection_store"):
            return path, False

        from revoluzen_flow.nodes.db_source import _get_connection_config
        resolved = _get_connection_config(connection_id, ctx.app_state)
        if resolved is None:
            raise ValueError(f"File: connection '{connection_id}' not found")
        config, conn_type = resolved

        # S3-style: s3://bucket/prefix + path, read by DuckDB itself
        if conn_type in ("s3", "minio"):
            bucket = config.get("bucket", "")
            prefix = str(config.get("prefix") or "").strip("/")
            for name, value in _s3_settings(config).items():
                if isinstance(value, bool):
                    ctx.conn.sql(f"SET {name}={'true' if value else 'false'}")
                else:
                    ctx.conn.sql(f"SET {name}='{_sql_text(value)}'")
            path = path.lstrip("/")
            s3_path = f"s3://{bucket}/{prefix}/{path}" if prefix else f"s3://{bucket}/{path}"
            return s3_path, True

        # Local/network share: base_path + path
        base_path = config.get("base_path") or config.get("root") or config.get("path", "")
        if base_path:
            return os.path.join(base_path, path), False

        return path, False

    # ── Excel via openpyxl (delegated to ExcelSourceNode helper if available) ──
    def _read_excel(self, conn, file_path: str) -> duckdb.DuckDBPyRelation:
        try:
            import openpyxl  # noqa: F401
        except ImportError:
            raise ValueError(
                "Excel format requires openpyxl: pip install openpyxl"
            )
        from revoluzen_flow.nodes.sources import ExcelSourceNode  # local import avoids cycle
        sheet = self.params.get("sheet_name", "")
        return ExcelSourceNode._read_with_openpyxl(
            conn, file_path, sheet,
            int(self.params.get("header_row", 1)),
            int(self.params.get("skip_rows", 0)),
        )

    # ── XML via ElementTree (delegated to XmlSourceNode if present) ──
    def _read_xml(self, conn, file_path: str) -> duckdb.DuckDBPyRelation:
        from revoluzen_flow.nodes.sources import XmlSourceNode
        node = XmlSourceNode({
            "file_path": file_path,
            "row_xpath": self.params.get("row_xpath", "//record"),
            "namespaces": self.params.get("namespaces", ""),
        })
        # XmlSourceNode expects an absolute path already-resolved
        return node.execute(type("X", (), {"conn": conn, "data_dir": ""})())

    @staticmethod
    def default_params() -> dict[str, Any]:
        return {
            "file_path": "",
            "connection_id": "",
            "format": "auto",
            "encoding": "utf-8",
            "delimiter": ",",
            "header": True,
            "null_string": "",
            "quote_char": '"',
            "sample_rows": DEV_SAMPLE_ROWS,
            "sheet_name": "",
            "header_row": 1,
            "skip_rows": 0,
            "row_xpath": "//record",
            "namespaces": "",
            # Mainframe and older files (November 2026)
            "fixed_columns": [],
            "fixed_widths": "",
            "fixed_names": "",
            "record_format": "",
            "record_length": 0,
            "skip_lines_start": 0,
            "skip_lines_end": 0,
            "text_trim": "",
            "empty_as_null": True,
            "bad_value_rule": "stop",
            "decimal_separator": ".",
            "copybook": "",
            "copybook_path": "",
            "code_page": "cp037",
            "redefines_rules": [],
            "binary_byte_order": "big",
            "float_format": "ibm",
            "column_names": "underscores",
            "pgp_decrypt": False,
            "pgp_credential_id": "",
            "pgp_verify": False,
            "pgp_verify_credential_id": "",
        }

    @staticmethod
    def param_schema() -> list[dict]:
        from revoluzen_flow.legacy_files.codepages import EBCDIC_CODE_PAGES
        from revoluzen_flow.legacy_files.fixed_width import COLUMN_TYPES, TRIM_RULES
        from revoluzen_flow.legacy_files.records import RECORD_FORMATS

        ebcdic = [value for value, _ in EBCDIC_CODE_PAGES]
        legacy = {"format": ["fixed_width", "copybook"]}
        return [
            # Connection tab
            {"name": "connection_id", "type": "connection_picker", "label": "Connection",
             "tab": "Source",
             "description": "Optional: S3, a local or network folder, an FTP / SFTP server, or a Windows file "
                            "share (SMB). Leave empty for local files."},
            {"name": "file_path", "type": "file", "label": "File Path", "required": True,
             "tab": "Source",
             "placeholder": "data/input.csv  (or .json, .parquet, .xlsx, .xml, .orc, .avro, .dat, .pgp)",
             "description": "Relative to connection root (if set) or data directory. Glob patterns supported (*.csv). "
                            "With an FTP / SFTP connection, the path on the server. With a Windows file share, "
                            "the path inside the connection's folder; * and ? in the name read every matching file."},
            {"name": "format", "type": "select", "label": "Format Override",
             "options": ["auto", "csv", "tsv", "json", "ndjson", "parquet", "excel", "xml", "orc", "avro",
                         "fixed_width", "copybook"],
             "default": "auto", "tab": "Source",
             "description": "Leave on 'auto' unless the extension is missing or wrong. Fixed-width text and "
                            "COBOL copybook records (mainframe files) are always chosen here."},
            {"name": "encoding", "type": "select", "label": "Encoding",
             "options": ["utf-8", "utf-8-sig", "latin-1", "cp1252", "iso-8859-1", "ascii", "utf-16", *ebcdic],
             "default": "utf-8", "tab": "Source",
             "description": "File text encoding. The cp codes are EBCDIC code pages for mainframe text "
                            "(037 US and Canada, 1140 with the euro sign, 1047 z/OS UNIX, 500 International, "
                            "273 Germany)."},
            # PGP
            {"name": "pgp_decrypt", "type": "boolean", "label": "Decrypt with PGP", "default": False,
             "tab": "Source",
             "description": "The file is PGP-encrypted (.pgp, .gpg or .asc). It is decrypted in a private "
                            "temporary folder and then read with the format above."},
            {"name": "pgp_credential_id", "type": "credential_picker", "label": "PGP key credential",
             "tab": "Source", "show_when": {"pgp_decrypt": [True]},
             "description": "A saved credential with your private key (private_key) and its passphrase."},
            {"name": "pgp_verify", "type": "boolean", "label": "Check the sender's signature", "default": False,
             "tab": "Source", "show_when": {"pgp_decrypt": [True]},
             "description": "Stop the run unless the file was signed with the sender's key and has not been "
                            "changed since. Files that are signed but not encrypted are read this way too."},
            {"name": "pgp_verify_credential_id", "type": "credential_picker", "label": "Sender's public key",
             "tab": "Source", "show_when": {"pgp_verify": [True]},
             "description": "A saved credential with the sender's public key (public_key)."},
            # Fixed-width text
            {"name": "fixed_columns", "type": "json", "label": "Columns", "tab": "Fixed-width",
             "show_when": {"format": ["fixed_width"]},
             "description": "Each column's name, start (from 1), length and type: "
                            + ", ".join(label for _, label in COLUMN_TYPES) + "."},
            {"name": "fixed_widths", "type": "text", "label": "Or: list of widths", "tab": "Fixed-width",
             "placeholder": "8, 30, 12", "show_when": {"format": ["fixed_width"]},
             "description": "Used when Columns is empty. Every column is read as text."},
            {"name": "fixed_names", "type": "text", "label": "Names for the widths", "tab": "Fixed-width",
             "placeholder": "customer_id, name, balance", "show_when": {"format": ["fixed_width"]}},
            {"name": "skip_lines_start", "type": "number", "label": "Lines to skip at the start",
             "default": 0, "tab": "Fixed-width", "show_when": {"format": ["fixed_width"]},
             "description": "Header lines, skipped in every file."},
            {"name": "skip_lines_end", "type": "number", "label": "Lines to skip at the end",
             "default": 0, "tab": "Fixed-width", "show_when": {"format": ["fixed_width"]},
             "description": "Trailer lines, skipped in every file."},
            {"name": "decimal_separator", "type": "select", "label": "Decimal point", "options": [".", ","],
             "default": ".", "tab": "Fixed-width", "show_when": {"format": ["fixed_width"]}},
            # COBOL copybook records
            {"name": "copybook", "type": "code", "label": "Copybook", "tab": "Copybook",
             "show_when": {"format": ["copybook"]},
             "description": "The COBOL record layout (01 and 05 levels with PIC clauses), pasted here."},
            {"name": "copybook_path", "type": "text", "label": "Or: copybook file", "tab": "Copybook",
             "placeholder": "layouts/CUSTMAST.cpy", "show_when": {"format": ["copybook"]}},
            {"name": "code_page", "type": "select", "label": "Code page",
             "options": [*ebcdic, "latin-1", "cp1252", "utf-8"], "default": "cp037", "tab": "Copybook",
             "show_when": {"format": ["copybook"]},
             "description": "EBCDIC for files copied from a mainframe in binary; latin-1 for files already "
                            "converted to ASCII."},
            {"name": "redefines_rules", "type": "json", "label": "REDEFINES rules", "tab": "Copybook",
             "show_when": {"format": ["copybook"]},
             "description": "Read a REDEFINES part only when a field has a value, for example read "
                            "PAYMENT-INFO when REC-TYPE is P. Without a rule every part is read."},
            {"name": "binary_byte_order", "type": "select", "label": "Binary numbers (COMP)",
             "options": ["big", "little"], "default": "big", "tab": "Copybook",
             "show_when": {"format": ["copybook"]},
             "description": "big = mainframe; little = COMP-5 written on a PC."},
            {"name": "float_format", "type": "select", "label": "Floating point (COMP-1, COMP-2)",
             "options": ["ibm", "ieee"], "default": "ibm", "tab": "Copybook",
             "show_when": {"format": ["copybook"]}},
            {"name": "column_names", "type": "select", "label": "Column names",
             "options": ["underscores", "as_written"], "default": "underscores", "tab": "Copybook",
             "show_when": {"format": ["copybook"]},
             "description": "underscores turns CUST-ID into CUST_ID, which SQL steps can use without quotes."},
            # Both
            {"name": "record_format", "type": "select", "label": "Records",
             "options": [value for value, _ in RECORD_FORMATS], "tab": "Records", "show_when": legacy,
             "description": "; ".join(f"{value} = {label}" for value, label in RECORD_FORMATS)
                            + ". Default: lines for fixed-width text, fixed for copybooks."},
            {"name": "record_length", "type": "number", "label": "Record length (bytes)", "default": 0,
             "tab": "Records", "show_when": legacy,
             "description": "For fixed-length records. 0 = the copybook's length."},
            {"name": "text_trim", "type": "select", "label": "Spaces in text",
             "options": [value for value, _ in TRIM_RULES], "tab": "Records", "show_when": legacy,
             "description": "; ".join(f"{value} = {label}" for value, label in TRIM_RULES) + "."},
            {"name": "empty_as_null", "type": "boolean", "label": "Empty text becomes NULL", "default": True,
             "tab": "Records", "show_when": legacy},
            {"name": "bad_value_rule", "type": "select", "label": "When a value cannot be read",
             "options": ["stop", "empty"], "default": "stop", "tab": "Records", "show_when": legacy,
             "description": "stop = fail the run and say where; empty = leave that value NULL and go on."},
            {"name": "sample_rows", "type": "number", "label": "Dev Sample Limit",
             "default": DEV_SAMPLE_ROWS, "tab": "Source",
             "description": "Max rows in dev mode (0 = no limit). Ignored in Full Run."},
            # CSV/TSV options
            {"name": "delimiter", "type": "select", "label": "Delimiter",
             "options": [",", ";", "\\t", "|"], "default": ",", "tab": "CSV Options",
             "show_when": {"format": ["auto", "csv", "tsv"]}},
            {"name": "header", "type": "boolean", "label": "Has Header Row", "default": True,
             "tab": "CSV Options",
             "show_when": {"format": ["auto", "csv", "tsv"]}},
            {"name": "null_string", "type": "text", "label": "NULL String",
             "placeholder": "NA, null, N/A", "tab": "CSV Options",
             "show_when": {"format": ["auto", "csv", "tsv"]},
             "description": "String values to interpret as NULL."},
            {"name": "quote_char", "type": "text", "label": "Quote Character",
             "default": '"', "tab": "CSV Options",
             "show_when": {"format": ["auto", "csv", "tsv"]}},
            # Excel options
            {"name": "sheet_name", "type": "text", "label": "Sheet Name",
             "placeholder": "Sheet1 — leave blank for first sheet",
             "tab": "Excel Options",
             "show_when": {"format": ["auto", "excel"]}},
            {"name": "header_row", "type": "number", "label": "Header Row", "default": 1,
             "tab": "Excel Options",
             "show_when": {"format": ["auto", "excel"]}},
            {"name": "skip_rows", "type": "number", "label": "Skip Rows", "default": 0,
             "tab": "Excel Options",
             "show_when": {"format": ["auto", "excel"]}},
            # XML options
            {"name": "row_xpath", "type": "text", "label": "Row XPath",
             "default": "//record", "tab": "XML Options",
             "show_when": {"format": ["auto", "xml"]}},
            {"name": "namespaces", "type": "text", "label": "Namespaces JSON",
             "placeholder": '{"ns": "http://example.com/ns"}',
             "tab": "XML Options",
             "show_when": {"format": ["auto", "xml"]}},
        ]


# ── Sink ──

@register(StepType.FILE_SINK)
class FileSinkNode(BaseNode):
    """Write any supported file format. Writer chosen by extension."""
    display_name = "File"
    category = "destination"
    description = "Write a file — format chosen automatically from the file extension"

    def execute(self, ctx: ExecutionContext) -> duckdb.DuckDBPyRelation:
        upstream = self.params.get("_input_step_ids") or []
        if not upstream:
            raise ValueError("File Sink: needs an upstream node")

        rel = ctx.get_input(upstream[0])
        if rel is None:
            raise ValueError(f"File Sink: upstream '{upstream[0]}' has no result")

        raw_path = self.params.get("file_path") or self.params.get("path") or ""
        share = self._file_share(ctx)
        if share is None:
            file_path = _resolve(raw_path, ctx.data_dir)
            os.makedirs(os.path.dirname(file_path) or ".", exist_ok=True)
        else:
            # A path inside the Windows file share's folder, written with
            # forward slashes so the name reads the same on every server.
            file_path = str(raw_path).strip().replace("\\", "/")
            if not file_path:
                raise ValueError("File Sink: give the path of the file to write on the share.")

        encrypt = _truthy(self.params.get("pgp_encrypt"))
        armor = _truthy(self.params.get("pgp_armor"))
        if encrypt and _plain_name(file_path) == file_path:
            # The encrypted file says what it is: orders.csv -> orders.csv.pgp
            file_path += ".asc" if armor else ".pgp"
        fmt = _detect_format(file_path, self.params.get("format", "auto"))
        name = os.path.basename(file_path) if share is None else file_path.rsplit("/", 1)[-1]
        if share is None:
            self._produce(ctx, rel, file_path, fmt, name, encrypt=encrypt, armor=armor)
            return rel

        # Written (and encrypted) in a private folder, then copied to the share
        # under a temporary name and renamed, so only the finished file appears.
        from revoluzen_flow.connections import file_share

        work = tempfile.mkdtemp(prefix="flow_share_")
        try:
            local = os.path.join(work, "upload" + os.path.splitext(name)[1])
            self._produce(ctx, rel, local, fmt, name, encrypt=encrypt, armor=armor)
            try:
                written = file_share.upload(share, local, file_path)
            except file_share.ShareError as exc:
                raise ValueError(f"File Sink: {exc}") from None
            logger.info("File Sink: wrote %s", written)
        finally:
            shutil.rmtree(work, ignore_errors=True)
        return rel

    def _file_share(self, ctx: ExecutionContext) -> dict[str, Any] | None:
        """The connection's settings when this step writes to a Windows file
        share (SMB); ``None`` when it writes on this server. Other connection
        types were never used by this step and still write here."""
        connection_id = str(self.params.get("connection_id") or "").strip()
        if not connection_id or not ctx.app_state.get("connection_store"):
            return None
        from revoluzen_flow.nodes.db_source import _get_connection_config

        workflow = getattr(ctx, "_workflow_ref", None)
        workspace = getattr(workflow, "workspace_id", None) if workflow is not None else None
        resolved = _get_connection_config(connection_id, ctx.app_state, workspace_id=workspace)
        if resolved is None:
            # Not writing on this server instead: the file would land somewhere
            # nobody looks.
            raise ValueError(f"File Sink: connection '{connection_id}' not found")
        config, conn_type = resolved
        return config if str(conn_type or "").lower() == "smb" else None

    def _produce(self, ctx: ExecutionContext, rel: "duckdb.DuckDBPyRelation", file_path: str, fmt: str,
                 name: str, *, encrypt: bool, armor: bool) -> None:
        """Write ``rel`` to ``file_path``, PGP-encrypted when asked. ``name``
        is the file's name at its destination."""
        if not encrypt:
            self._write(ctx, rel, file_path, fmt)
            return

        from revoluzen_flow.openpgp import PgpError, encrypt_file
        from revoluzen_flow.openpgp.credentials import encryption_keys, load_credential

        workflow = getattr(ctx, "_workflow_ref", None)
        workspace = getattr(workflow, "workspace_id", None) or "default"
        signer = self._signer(ctx, workspace)
        try:
            recipients = encryption_keys(
                load_credential(ctx.app_state, str(self.params.get("pgp_credential_id") or ""), workspace)
            )
        except PgpError as exc:
            raise ValueError(f"File Sink: {exc}") from None
        inner_name = _plain_name(name)
        work = tempfile.mkdtemp(prefix="flow_out_")
        try:
            # Written in a private folder under a fixed name, then encrypted;
            # only the encrypted file reaches the destination.
            plain_path = os.path.join(work, "plain" + os.path.splitext(inner_name)[1])
            self._write(ctx, rel, plain_path, fmt)
            try:
                used = encrypt_file(plain_path, file_path, recipients, armor=armor, filename=inner_name,
                                    signer=signer)
                if signer is not None:
                    logger.info("File Sink: signed %s with key %s", inner_name, signer.fingerprint)
            except PgpError as exc:
                raise ValueError(f"File Sink: could not encrypt: {exc}") from None
            logger.info("File Sink: wrote %s encrypted to key %s", name, ", ".join(used))
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def _write(self, ctx: ExecutionContext, rel: "duckdb.DuckDBPyRelation", file_path: str, fmt: str) -> None:
        # Excel/XML get special treatment because DuckDB COPY TO doesn't ship a writer.
        if fmt == "csv":
            delim = self.params.get("delimiter", ",")
            header = "true" if self.params.get("header", True) else "false"
            ctx.conn.execute(
                f"COPY ({rel.sql_query()}) TO '{file_path}' "
                f"(FORMAT CSV, DELIMITER '{delim}', HEADER {header})"
            )
        elif fmt == "tsv":
            ctx.conn.execute(
                f"COPY ({rel.sql_query()}) TO '{file_path}' "
                f"(FORMAT CSV, DELIMITER E'\\t', HEADER true)"
            )
        elif fmt == "parquet":
            compression = self.params.get("compression", "snappy")
            ctx.conn.execute(
                f"COPY ({rel.sql_query()}) TO '{file_path}' "
                f"(FORMAT PARQUET, COMPRESSION '{compression}')"
            )
        elif fmt in ("json", "ndjson"):
            array_mode = "ARRAY true" if fmt == "json" else "ARRAY false"
            ctx.conn.execute(
                f"COPY ({rel.sql_query()}) TO '{file_path}' (FORMAT JSON, {array_mode})"
            )
        elif fmt == "excel":
            self._write_excel(rel, file_path)
        elif fmt == "xml":
            self._write_xml(rel, file_path)
        else:
            raise ValueError(f"File Sink: unsupported format '{fmt}'")

    def _write_excel(self, rel: duckdb.DuckDBPyRelation, file_path: str) -> None:
        try:
            import openpyxl
        except ImportError:
            raise ValueError("Excel sink requires openpyxl: pip install openpyxl")
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = self.params.get("sheet_name") or "Sheet1"
        cols = rel.columns
        ws.append(cols)
        for row in rel.fetchall():
            ws.append(list(row))
        wb.save(file_path)

    def _write_xml(self, rel: duckdb.DuckDBPyRelation, file_path: str) -> None:
        import xml.etree.ElementTree as ET
        root_tag = self.params.get("root_tag", "data")
        row_tag = self.params.get("row_tag", "record")
        root = ET.Element(root_tag)
        cols = rel.columns
        for row in rel.fetchall():
            r = ET.SubElement(root, row_tag)
            for col, val in zip(cols, row):
                el = ET.SubElement(r, col)
                el.text = "" if val is None else str(val)
        ET.ElementTree(root).write(file_path, encoding="utf-8", xml_declaration=True)

    def _signer(self, ctx: ExecutionContext, workspace: str):
        """With "Sign with your key" on: our key, unlocked, to sign the file
        before it is encrypted (openpgp/signatures.py). ``None`` otherwise."""
        if not _truthy(self.params.get("pgp_sign")):
            return None
        from revoluzen_flow.openpgp import PgpError, Signer
        from revoluzen_flow.openpgp.credentials import load_credential, signing_keys

        credential_id = str(self.params.get("pgp_sign_credential_id") or "")
        if not credential_id:
            raise ValueError("File Sink: choose the credential with your private key for signing, or turn off "
                             "'Sign with your key'.")
        try:
            keys, passphrase = signing_keys(load_credential(ctx.app_state, credential_id, workspace))
            return Signer(keys, passphrase)
        except PgpError as exc:
            raise ValueError(f"File Sink: your signing key: {exc}") from None

    @staticmethod
    def default_params() -> dict[str, Any]:
        return {
            "file_path": "",
            "connection_id": "",
            "format": "auto",
            "delimiter": ",",
            "header": True,
            "compression": "snappy",
            "sheet_name": "",
            "root_tag": "data",
            "row_tag": "record",
            "pgp_encrypt": False,
            "pgp_credential_id": "",
            "pgp_armor": False,
            "pgp_sign": False,
            "pgp_sign_credential_id": "",
        }

    @staticmethod
    def param_schema() -> list[dict]:
        return [
            {"name": "connection_id", "type": "connection_picker", "label": "Connection",
             "description": "Optional: a Windows file share (SMB) to write to. Leave empty to write on the "
                            "Flow server."},
            {"name": "file_path", "type": "text", "label": "Output File", "required": True,
             "placeholder": "out/result.parquet  (or .csv, .json, .xlsx, .xml)",
             "description": "Writer is chosen from the extension. With a Windows file share, the path inside "
                            "the connection's folder; the file appears only once it is complete."},
            {"name": "pgp_encrypt", "type": "boolean", "label": "Encrypt with PGP", "default": False,
             "description": "Encrypt the file for a partner. It is written in a private temporary folder, "
                            "encrypted, and only the encrypted file is saved (.pgp is added to the name)."},
            {"name": "pgp_credential_id", "type": "credential_picker", "label": "PGP key credential",
             "show_when": {"pgp_encrypt": [True]},
             "description": "A saved credential with the partner's public key (public_key)."},
            {"name": "pgp_armor", "type": "boolean", "label": "Armored text (.asc)", "default": False,
             "show_when": {"pgp_encrypt": [True]},
             "description": "Write -----BEGIN PGP MESSAGE----- text instead of binary."},
            {"name": "pgp_sign", "type": "boolean", "label": "Sign with your key", "default": False,
             "show_when": {"pgp_encrypt": [True]},
             "description": "Sign the file before it is encrypted, so the partner can check it came from you "
                            "and was not changed."},
            {"name": "pgp_sign_credential_id", "type": "credential_picker", "label": "Your signing key",
             "show_when": {"pgp_sign": [True]},
             "description": "A saved credential with your private key (private_key) and its passphrase."},
            {"name": "format", "type": "select", "label": "Format Override",
             "options": ["auto", "csv", "tsv", "json", "ndjson", "parquet", "excel", "xml", "orc", "avro"],
             "default": "auto"},
            {"name": "delimiter", "type": "select", "label": "Delimiter (CSV only)",
             "options": [",", ";", "\\t", "|"], "default": ",",
             "show_when": {"format": ["auto", "csv"]}},
            {"name": "header", "type": "boolean", "label": "Write Header (CSV only)", "default": True,
             "show_when": {"format": ["auto", "csv", "tsv"]}},
            {"name": "compression", "type": "select", "label": "Compression (Parquet only)",
             "options": ["snappy", "zstd", "gzip", "uncompressed"], "default": "snappy",
             "show_when": {"format": ["auto", "parquet"]}},
            {"name": "sheet_name", "type": "text", "label": "Sheet Name (Excel only)",
             "default": "Sheet1", "show_when": {"format": ["auto", "excel"]}},
            {"name": "root_tag", "type": "text", "label": "Root Tag (XML only)",
             "default": "data", "show_when": {"format": ["auto", "xml"]}},
            {"name": "row_tag", "type": "text", "label": "Row Tag (XML only)",
             "default": "record", "show_when": {"format": ["auto", "xml"]}},
        ]
