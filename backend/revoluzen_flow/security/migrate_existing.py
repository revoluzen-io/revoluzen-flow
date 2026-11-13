"""
One-shot migration: re-encrypt every credential + AI provider API key.

Why this exists: pre-1.0 OSS Free installs stored credentials and AI
provider API keys in plaintext on disk. The May 4 2026 fix made
encryption always-on, but EXISTING rows are still in their legacy form
(raw dict for credentials, `PLAIN:<value>` sentinel for AI config).

The Encryptor's read path tolerates these legacy formats — the next
SAVE re-encrypts them. But operators upgrading don't necessarily edit
every credential, so plaintext can linger for months.

This script walks every row in `credentials`, `user_ai_config`, and
`workspace_ai_config` and forces a re-save through the always-on
encryptor. After it runs, every secret is `ENC:v1:<token>` form.

It also encrypts the passwords and keys typed straight into a
connection's settings (`connections`), which were stored as typed until
November 2026. For credentials it follows `credentials/sensitive_fields.py`:
every field whose name says it holds a secret, and every field marked
sensitive, not just the encryptor's short list of names. Both of those
also run on their own at every server start (`encrypt_connection_secrets`,
`encrypt_credential_secrets`), so operators do not have to run this script
for them.

It also encrypts secrets typed straight into a pipeline step's settings or
given as a secret parameter's default (`ir/step_secrets.py`): every stored
version, the Bin and old run records, and removes them from user templates.
A secret parameter's value saved on a schedule or backfill is encrypted too. That too runs at server start
(`encrypt_pipeline_step_secrets`). The start-up pass stops after a short time
on old run records and carries on at the next start; this script does them
all at once.

## Usage

    python -m revoluzen_flow.security.migrate_existing                    # in-place
    python -m revoluzen_flow.security.migrate_existing --dry-run          # report only
    python -m revoluzen_flow.security.migrate_existing --backup-dir ./bak # write JSONL backup first

## Safety

- **Always takes a snapshot first** unless `--no-backup` is passed
  explicitly. Snapshot is a JSONL file under `<data_dir>/migration_backups/`
  with each row's raw pre-migration JSON. Lets the operator restore if
  something goes wrong.
- **Idempotent.** Re-running on an already-migrated install is a no-op
  (every row reads as `ENC:v1:`, the encrypted value, and re-encrypts
  to a different token but with the same plaintext — safe). The
  connections pass goes further: it never touches a value that is
  already `ENC:v1:`, so a second run writes nothing at all.
- **Per-row try/except.** A single corrupt row doesn't abort the run;
  it's logged and skipped. Final report counts successes + failures.
- **Refuses to run without a master key.** If `~/.revoluzen-flow/secret.key`
  doesn't exist, the script generates one — but warns the operator.
- **Refuses to run on a write-locked DB.** Acquires SQLite's exclusive
  lock for the duration; concurrent Revoluzen Flow processes block until the
  migration finishes.

## What it does NOT touch

- A pipeline's stored fingerprint (`content_hash`). It is over the value
  of a secret rather than its encrypted form, so it stays valid.
- Logged prompts in the execution history. Historical runs may have logged
  sanitized prompts that still contain `PLAIN:` strings — those are
  intentionally not re-written so audit replay stays accurate.
- The RAG vector store. Embeddings of public docs aren't secrets.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────


def _resolve_db_path() -> Path:
    """Mirror the resolution main.py does."""
    data_dir = os.environ.get("REVOLUZEN_FLOW_DATA_DIR", "").strip()
    if data_dir:
        return Path(data_dir).expanduser() / "revoluzen_flow.db"
    return Path("data") / "revoluzen_flow.db"


def _backup_dir() -> Path:
    data_dir = os.environ.get("REVOLUZEN_FLOW_DATA_DIR", "data")
    return Path(data_dir).expanduser() / "migration_backups"


def _open_db(db_path: Path) -> sqlite3.Connection:
    if not db_path.is_file():
        raise FileNotFoundError(
            f"Revoluzen Flow database not found at {db_path}. Set REVOLUZEN_FLOW_DATA_DIR "
            f"to point at the correct data directory."
        )
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


# ─────────────────────────────────────────────────────────────────────
# Per-table workers
# ─────────────────────────────────────────────────────────────────────


def _migrate_credentials(
    conn: sqlite3.Connection, encryptor, *, dry_run: bool, backup_writer,
) -> tuple[int, int, int]:
    """Re-encrypt every row in `credentials`. Returns (scanned, updated, failed)."""
    from revoluzen_flow.credentials.sensitive_fields import decrypt_config, encrypt_config

    scanned = updated = failed = 0
    rows = conn.execute("SELECT id, data FROM credentials").fetchall()
    for row in rows:
        scanned += 1
        try:
            blob = json.loads(row["data"]) if row["data"] else {}
            backup_writer({"table": "credentials", "id": row["id"], "data": blob})

            config = blob.get("config") or {}
            # Decrypt-then-re-encrypt to canonicalise format. Every value
            # stored as ENC:v1: is read back (extra layers included); plaintext
            # passes through. Then every secret is encrypted once: by name, or
            # because the person marked the field sensitive (sensitive_fields).
            decrypted = decrypt_config(config, encryptor)
            re_encrypted = encrypt_config(decrypted, blob.get("sensitive_fields"), encryptor)
            if config == re_encrypted:
                continue  # already canonical; no write needed
            blob["config"] = re_encrypted
            if not dry_run:
                conn.execute(
                    "UPDATE credentials SET data = ? WHERE id = ?",
                    (json.dumps(blob, default=str), row["id"]),
                )
            updated += 1
        except Exception as exc:  # noqa: BLE001
            failed += 1
            logger.warning("credentials row id=%s failed: %s", row["id"], exc)
    return scanned, updated, failed


def _encrypt_plaintext_rows(
    conn, table: str, encrypt_blob, *, dry_run: bool, backup_writer,
) -> tuple[int, int, int]:
    """Encrypt the secrets still stored as typed in `table`. Returns
    (scanned, updated, failed).

    `conn` is anything with `.execute()`: the CLI's sqlite3 connection, or
    the Database inside `transaction()` at server start. `encrypt_blob(row)`
    returns the new config and how many values it encrypted. Unlike the
    credential canonicalisation above, this re-encrypts nothing: a value
    that is already `ENC:v1:` is left as it is, so running twice writes
    nothing the second time. Each new token is checked to decrypt to the
    value it replaces before the row is written. A row that fails is left
    untouched and logged by id only, never with its contents.
    """
    scanned = updated = failed = 0
    try:
        rows = conn.execute(f"SELECT id, data FROM {table}").fetchall()
    except sqlite3.OperationalError:
        return 0, 0, 0  # no such table on this install
    for row in rows:
        scanned += 1
        try:
            blob = json.loads(row["data"]) if row["data"] else {}
            config = blob.get("config")
            if not isinstance(config, dict):
                continue
            encrypted, count = encrypt_blob(blob)
            if not count:
                continue
            backup_writer({"table": table, "id": row["id"], "data": blob})
            blob["config"] = encrypted
            if not dry_run:
                conn.execute(
                    f"UPDATE {table} SET data = ? WHERE id = ?",
                    (json.dumps(blob, default=str), row["id"]),
                )
            updated += 1
        except Exception as exc:  # noqa: BLE001
            failed += 1
            logger.warning("%s row id=%s failed: %s", table, row["id"], type(exc).__name__)
    return scanned, updated, failed


def _migrate_connections(
    conn, encryptor, *, dry_run: bool, backup_writer,
) -> tuple[int, int, int]:
    """Encrypt the secrets typed into each connection's settings."""
    from revoluzen_flow.connections.inline_secrets import encrypt_plaintext_secrets

    return _encrypt_plaintext_rows(
        conn, "connections",
        lambda blob: encrypt_plaintext_secrets(blob["config"], blob.get("type"), encryptor),
        dry_run=dry_run, backup_writer=backup_writer,
    )


def _encrypt_credential_rows(
    conn, encryptor, *, dry_run: bool, backup_writer,
) -> tuple[int, int, int]:
    """Encrypt the credential secrets still stored as typed: fields whose name
    says they hold a secret, and fields the person marked sensitive. Before
    November 2026 only the encryptor's short list of names was encrypted."""
    from revoluzen_flow.credentials.sensitive_fields import encrypt_plaintext_secrets

    return _encrypt_plaintext_rows(
        conn, "credentials",
        lambda blob: encrypt_plaintext_secrets(blob["config"], blob.get("sensitive_fields"), encryptor),
        dry_run=dry_run, backup_writer=backup_writer,
    )


def _no_backup(_payload: dict) -> None:
    """The startup pass keeps no copy: a backup would hold the very
    passwords it is encrypting, in clear, on disk."""


def _startup_pass(db, encryptor, worker, what: str) -> tuple[int, int, int]:
    """One transaction, so a crash part-way leaves every row as it was and the
    next start simply runs again. Does nothing when encryption is disabled
    (REVOLUZEN_FLOW_DISABLE_ENCRYPTION=1, test-only)."""
    if encryptor is None:
        return 0, 0, 0
    with db.transaction() as tx:
        scanned, updated, failed = worker(tx, encryptor, dry_run=False, backup_writer=_no_backup)
    if updated:
        logger.info("Revoluzen Flow: encrypted the passwords and keys stored in %d %s.", updated, what)
    if failed:
        logger.warning(
            "Revoluzen Flow: %d %s could not be encrypted and were left as they "
            "were; the next start tries again.", failed, what,
        )
    return scanned, updated, failed


def encrypt_connection_secrets(db, encryptor) -> tuple[int, int, int]:
    """Server-start pass: encrypt any connection secret still stored as typed.

    Called from the lifespan after the automatic database backup. Safe to run
    at every start: once every value is encrypted it reads the rows and
    writes nothing.
    """
    return _startup_pass(db, encryptor, _migrate_connections, "connection(s)")


def encrypt_credential_secrets(db, encryptor) -> tuple[int, int, int]:
    """Server-start pass for saved credentials, with the same guarantees as
    `encrypt_connection_secrets`."""
    return _startup_pass(db, encryptor, _encrypt_credential_rows, "credential(s)")


# Marker row in `settings`, so later starts do not read every stored version.
_SECRET_MIGRATIONS_ID = "secret_migrations"
_RUN_RECORD_BATCH = 200
_RUN_RECORD_SECONDS = 20.0


def _encrypt_once_each(encryptor):
    """An encrypt function that gives one secret one token, and checks each
    token reads back as the value it replaces. Across the versions of a
    pipeline this keeps a secret that never changed looking unchanged, to the
    version compare and to the step cache."""
    tokens: dict[str, str] = {}

    def encrypt(value: str) -> str:
        token = tokens.get(value)
        if token is None:
            token = encryptor.encrypt_value(value)
            if encryptor.decrypt_value(token) != value:
                raise RuntimeError("an encrypted step setting did not read back as stored")
            tokens[value] = token
        return token

    return encrypt


def _migrate_pipeline_versions(
    conn, table: str, encryptor, *, dry_run: bool, backup_writer,
) -> tuple[int, int, int]:
    """Encrypt the secrets typed into the steps of every stored version in
    `table`. One pipeline at a time, so memory stays bounded. A value already
    `ENC:v1:` is left as it is, so running twice writes nothing the second
    time. A row that fails is left untouched and logged by id only."""
    from revoluzen_flow.ir.step_secrets import seal_dumped_workflow

    scanned = updated = failed = 0
    try:
        ids = [r["workflow_id"] for r in conn.execute(f"SELECT DISTINCT workflow_id FROM {table}").fetchall()]
    except sqlite3.OperationalError:
        return 0, 0, 0  # no such table on this install
    for workflow_id in ids:
        encrypt = _encrypt_once_each(encryptor)
        rows = conn.execute(
            f"SELECT version, data FROM {table} WHERE workflow_id = ? ORDER BY version",
            (workflow_id,),
        ).fetchall()
        for row in rows:
            scanned += 1
            try:
                blob = json.loads(row["data"])
                workflow = blob.get("workflow") if isinstance(blob, dict) else None
                if not isinstance(workflow, dict):
                    continue
                if not seal_dumped_workflow(workflow, encrypt):
                    continue
                backup_writer({"table": table, "id": f"{workflow_id}:{row['version']}", "data": row["data"]})
                if not dry_run:
                    conn.execute(
                        f"UPDATE {table} SET data = ? WHERE workflow_id = ? AND version = ?",
                        (json.dumps(blob, default=str), workflow_id, row["version"]),
                    )
                updated += 1
            except Exception as exc:  # noqa: BLE001
                failed += 1
                logger.warning(
                    "%s row %s v%s failed: %s", table, workflow_id, row["version"], type(exc).__name__,
                )
    return scanned, updated, failed


def _blank_template_secrets(conn, *, dry_run: bool, backup_writer) -> tuple[int, int, int]:
    """Remove secrets typed into the steps of user templates. Templates are
    for sharing and never carry one; whoever uses the template enters it."""
    from revoluzen_flow.ir.step_secrets import blank_step_secrets

    scanned = updated = failed = 0
    try:
        rows = conn.execute("SELECT id, data FROM user_templates").fetchall()
    except sqlite3.OperationalError:
        return 0, 0, 0
    for row in rows:
        scanned += 1
        try:
            blob = json.loads(row["data"]) if row["data"] else {}
            if not isinstance(blob, dict):
                continue
            steps, blanked = blank_step_secrets(blob.get("steps"))
            if not blanked:
                continue
            backup_writer({"table": "user_templates", "id": row["id"], "data": row["data"]})
            blob["steps"] = steps
            if not dry_run:
                conn.execute(
                    "UPDATE user_templates SET data = ? WHERE id = ?",
                    (json.dumps(blob, default=str), row["id"]),
                )
            updated += 1
        except Exception as exc:  # noqa: BLE001
            failed += 1
            logger.warning("user_templates row id=%s failed: %s", row["id"], type(exc).__name__)
    return scanned, updated, failed


def _encrypt_run_records(
    conn, after: int, limit: int, encryptor, *, dry_run: bool, backup_writer,
) -> tuple[int, int, int, int]:
    """Encrypt the secrets in the pipeline copy kept with each run, and any
    secret parameter value the run was given, for up to `limit` run records
    after rowid `after`. Returns (scanned, updated, failed, last rowid seen)."""
    from revoluzen_flow.ir.step_secrets import seal_dumped_workflow, seal_parameter_values

    scanned = updated = failed = 0
    last = after
    try:
        rows = conn.execute(
            "SELECT rowid AS row_no, id, data FROM executions WHERE rowid > ? ORDER BY rowid LIMIT ?",
            (after, limit),
        ).fetchall()
    except sqlite3.OperationalError:
        return 0, 0, 0, after
    encrypt = _encrypt_once_each(encryptor)
    for row in rows:
        scanned += 1
        last = row["row_no"]
        try:
            blob = json.loads(row["data"])
            if not isinstance(blob, dict):
                continue
            snapshot = blob.get("workflow_snapshot")
            sealed = seal_dumped_workflow(snapshot, encrypt)
            metadata = blob.get("metadata")
            if isinstance(metadata, dict):
                values = metadata.get("parameter_values")
                parameters = snapshot.get("parameters") if isinstance(snapshot, dict) else None
                kept = seal_parameter_values(values, parameters if isinstance(parameters, list) else [], encrypt)
                if kept is not values:
                    metadata["parameter_values"] = kept
                    sealed += 1
            if not sealed:
                continue
            backup_writer({"table": "executions", "id": row["id"], "data": row["data"]})
            if not dry_run:
                conn.execute(
                    "UPDATE executions SET data = ? WHERE id = ?",
                    (json.dumps(blob, default=str), row["id"]),
                )
            updated += 1
        except Exception as exc:  # noqa: BLE001
            failed += 1
            logger.warning("executions row id=%s failed: %s", row["id"], type(exc).__name__)
    return scanned, updated, failed, last


def _declared_parameters(conn, workflow_id: Any, cache: dict) -> list:
    """A pipeline's parameter declarations, newest version first, including a
    pipeline in the Bin. They say which saved values are secret."""
    if workflow_id in cache:
        return cache[workflow_id]
    parameters: list = []
    for table in ("workflow_versions", "workflow_trash"):
        try:
            row = conn.execute(
                f"SELECT json_extract(data, '$.workflow.parameters') AS parameters FROM {table} "
                "WHERE workflow_id = ? ORDER BY version DESC LIMIT 1",
                (workflow_id,),
            ).fetchone()
        except sqlite3.OperationalError:
            continue  # no such table on this install
        if row and row["parameters"]:
            parameters = json.loads(row["parameters"])
            break
    cache[workflow_id] = parameters
    return parameters


def _seal_saved_parameter_values(
    conn, table: str, owner_field: str, values_field: str, encryptor, *, dry_run: bool, backup_writer,
) -> tuple[int, int, int]:
    """Encrypt the secret parameter values saved on each row of `table`, for
    schedules and backfills. A value already `ENC:v1:` is left as it is."""
    from revoluzen_flow.ir.step_secrets import seal_parameter_values

    scanned = updated = failed = 0
    try:
        rows = conn.execute(f"SELECT id, data FROM {table}").fetchall()
    except sqlite3.OperationalError:
        return 0, 0, 0
    cache: dict = {}
    encrypt = _encrypt_once_each(encryptor)
    for row in rows:
        scanned += 1
        try:
            blob = json.loads(row["data"]) if row["data"] else {}
            values = blob.get(values_field) if isinstance(blob, dict) else None
            if not isinstance(values, dict) or not values:
                continue
            kept = seal_parameter_values(values, _declared_parameters(conn, blob.get(owner_field), cache), encrypt)
            if kept is values:
                continue
            backup_writer({"table": table, "id": row["id"], "data": row["data"]})
            blob[values_field] = kept
            if not dry_run:
                conn.execute(
                    f"UPDATE {table} SET data = ? WHERE id = ?",
                    (json.dumps(blob, default=str), row["id"]),
                )
            updated += 1
        except Exception as exc:  # noqa: BLE001
            failed += 1
            logger.warning("%s row id=%s failed: %s", table, row["id"], type(exc).__name__)
    return scanned, updated, failed


def _seal_binned_schedules(conn, encryptor, *, dry_run: bool, backup_writer) -> tuple[int, int, int]:
    """The same for the copy of a pipeline's schedules kept with it in the Bin."""
    from revoluzen_flow.ir.step_secrets import seal_parameter_values

    scanned = updated = failed = 0
    try:
        rows = conn.execute("SELECT workflow_id, schedules FROM workflow_trash_meta").fetchall()
    except sqlite3.OperationalError:
        return 0, 0, 0
    cache: dict = {}
    encrypt = _encrypt_once_each(encryptor)
    for row in rows:
        scanned += 1
        try:
            schedules = json.loads(row["schedules"] or "[]")
            if not isinstance(schedules, list):
                continue
            changed = False
            for schedule in schedules:
                values = schedule.get("parameter_values") if isinstance(schedule, dict) else None
                if not isinstance(values, dict) or not values:
                    continue
                kept = seal_parameter_values(values, _declared_parameters(conn, row["workflow_id"], cache), encrypt)
                if kept is not values:
                    schedule["parameter_values"] = kept
                    changed = True
            if not changed:
                continue
            backup_writer({"table": "workflow_trash_meta", "id": row["workflow_id"], "data": row["schedules"]})
            if not dry_run:
                conn.execute(
                    "UPDATE workflow_trash_meta SET schedules = ? WHERE workflow_id = ?",
                    (json.dumps(schedules, default=str), row["workflow_id"]),
                )
            updated += 1
        except Exception as exc:  # noqa: BLE001
            failed += 1
            logger.warning("workflow_trash_meta row id=%s failed: %s", row["workflow_id"], type(exc).__name__)
    return scanned, updated, failed


def _write_migration_marker(tx, marker: dict) -> None:
    now = datetime.now(timezone.utc).isoformat()
    tx.execute(
        "INSERT INTO settings (id, data, created_at, updated_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET data = excluded.data, updated_at = excluded.updated_at",
        (_SECRET_MIGRATIONS_ID, json.dumps(marker), now, now),
    )


def encrypt_pipeline_step_secrets(
    db, encryptor, *, seconds: float = _RUN_RECORD_SECONDS,
) -> dict[str, int]:
    """Server-start pass for secrets typed into pipeline steps and given as
    the default of a secret parameter.

    Stored versions, the Bin and user templates are done in one transaction,
    so a crash part-way leaves them as they were and the next start runs again.
    Parameter values saved on schedules and backfills are done the same way.
    Run records can be far more numerous and have no retention limit, so they
    are done in short batches for at most `seconds`, and the next start carries
    on where this one stopped. Until then the API masks them by name.

    A marker in `settings` saves later starts from reading every row. Called
    before the scheduler starts, so no new run is being written meanwhile.
    Does nothing when encryption is disabled (test-only).
    """
    report = {"versions": 0, "bin": 0, "templates": 0, "runs": 0, "schedules": 0, "backfills": 0, "failed": 0}
    if encryptor is None:
        return report
    row = db.fetchone("SELECT data FROM settings WHERE id = ?", (_SECRET_MIGRATIONS_ID,))
    marker: dict = json.loads(row["data"]) if row else {}

    if not marker.get("pipeline_secrets_done"):
        with db.transaction() as tx:
            _, report["versions"], failed_versions = _migrate_pipeline_versions(
                tx, "workflow_versions", encryptor, dry_run=False, backup_writer=_no_backup,
            )
            _, report["bin"], failed_bin = _migrate_pipeline_versions(
                tx, "workflow_trash", encryptor, dry_run=False, backup_writer=_no_backup,
            )
            _, report["templates"], failed_templates = _blank_template_secrets(
                tx, dry_run=False, backup_writer=_no_backup,
            )
            report["failed"] += failed_versions + failed_bin + failed_templates
            if not report["failed"]:
                marker["pipeline_secrets_done"] = True
                _write_migration_marker(tx, marker)

    if not marker.get("saved_parameter_values_done"):
        with db.transaction() as tx:
            _, report["schedules"], failed_schedules = _seal_saved_parameter_values(
                tx, "schedules", "workflow_id", "parameter_values", encryptor,
                dry_run=False, backup_writer=_no_backup,
            )
            _, report["backfills"], failed_backfills = _seal_saved_parameter_values(
                tx, "backfill_runs", "pipeline_id", "params_template", encryptor,
                dry_run=False, backup_writer=_no_backup,
            )
            _, binned, failed_binned = _seal_binned_schedules(
                tx, encryptor, dry_run=False, backup_writer=_no_backup,
            )
            report["schedules"] += binned
            failed_saved = failed_schedules + failed_backfills + failed_binned
            report["failed"] += failed_saved
            if not failed_saved:
                marker["saved_parameter_values_done"] = True
                _write_migration_marker(tx, marker)

    if not marker.get("run_secrets_done"):
        deadline = time.monotonic() + seconds
        while True:
            with db.transaction() as tx:
                scanned, updated, failed, after = _encrypt_run_records(
                    tx, int(marker.get("run_secrets_after") or 0), _RUN_RECORD_BATCH,
                    encryptor, dry_run=False, backup_writer=_no_backup,
                )
                report["runs"] += updated
                report["failed"] += failed
                marker["run_secrets_after"] = after
                if scanned < _RUN_RECORD_BATCH:
                    marker["run_secrets_done"] = True
                _write_migration_marker(tx, marker)
            if marker.get("run_secrets_done") or time.monotonic() >= deadline:
                break

    if any(report[key] for key in ("versions", "bin", "templates", "runs", "schedules", "backfills")):
        logger.info(
            "Revoluzen Flow: secrets in pipelines encrypted in %d version(s), %d Bin version(s), "
            "%d run record(s), %d schedule(s) and %d backfill row(s), and removed from %d template(s).",
            report["versions"], report["bin"], report["runs"], report["schedules"],
            report["backfills"], report["templates"],
        )
    if report["failed"]:
        logger.warning(
            "Revoluzen Flow: %d stored pipeline row(s) could not be processed for step "
            "secrets and were left as they were.", report["failed"],
        )
    return report


def _migrate_ai_config(
    conn: sqlite3.Connection, table: str, encryptor, *,
    dry_run: bool, backup_writer,
) -> tuple[int, int, int]:
    """Re-encrypt the `api_key` column on user_ai_config or workspace_ai_config."""
    scanned = updated = failed = 0
    try:
        rows = conn.execute(
            f"SELECT user_id, workspace_id, api_key FROM {table}"
            if table == "user_ai_config"
            else f"SELECT workspace_id, api_key FROM {table}"
        ).fetchall()
    except sqlite3.OperationalError:
        # Table doesn't exist on this install — fine.
        return 0, 0, 0

    for row in rows:
        scanned += 1
        try:
            current = row["api_key"] or ""
            backup_writer({
                "table": table,
                "id": (row["user_id"] if table == "user_ai_config" else row["workspace_id"]),
                "api_key": current,
            })
            if not current:
                continue
            plaintext = encryptor.decrypt_value(current)
            re_encrypted = encryptor.encrypt_value(plaintext) if plaintext else ""
            if current == re_encrypted:
                continue
            if not dry_run:
                if table == "user_ai_config":
                    conn.execute(
                        f"UPDATE {table} SET api_key = ? WHERE user_id = ?",
                        (re_encrypted, row["user_id"]),
                    )
                else:
                    conn.execute(
                        f"UPDATE {table} SET api_key = ? WHERE workspace_id = ?",
                        (re_encrypted, row["workspace_id"]),
                    )
            updated += 1
        except Exception as exc:  # noqa: BLE001
            failed += 1
            logger.warning("%s row failed: %s", table, exc)
    return scanned, updated, failed


# ─────────────────────────────────────────────────────────────────────
# Backup writer
# ─────────────────────────────────────────────────────────────────────


def _make_backup_writer(backup_path: Path | None):
    """Return a function `(payload: dict) -> None` that appends one JSONL
    line to the backup file. None when backup is disabled."""
    if backup_path is None:
        def noop(_payload: dict) -> None: ...
        return noop, None

    backup_path.parent.mkdir(parents=True, exist_ok=True)
    f = open(backup_path, "a", encoding="utf-8")

    def write(payload: dict) -> None:
        f.write(json.dumps(payload, default=str) + "\n")

    return write, f


# ─────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="python -m revoluzen_flow.security.migrate_existing",
        description="Re-encrypt legacy plaintext credentials + AI API keys.",
    )
    p.add_argument("--db", help="Path to revoluzen_flow.db. Defaults to REVOLUZEN_FLOW_DATA_DIR/revoluzen_flow.db.")
    p.add_argument("--dry-run", action="store_true", help="Report what would change; don't write.")
    p.add_argument("--no-backup", action="store_true", help="Skip the JSONL backup snapshot.")
    p.add_argument("--backup-dir", help="Override the default migration_backups directory.")
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    db_path = Path(args.db) if args.db else _resolve_db_path()
    print(f"DB: {db_path}")

    # Set up encryptor — creates the master key file on first run if missing.
    from revoluzen_flow.security.encryptor import Encryptor
    enc = Encryptor.from_master_key()

    # Backup snapshot.
    backup_path: Path | None = None
    if not args.no_backup:
        backup_dir = Path(args.backup_dir) if args.backup_dir else _backup_dir()
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_path = backup_dir / f"migration_{ts}.jsonl"
        print(f"Backup: {backup_path}")

    backup_writer, backup_fh = _make_backup_writer(backup_path)

    conn = _open_db(db_path)
    try:
        cred_s, cred_u, cred_f = _migrate_credentials(
            conn, enc, dry_run=args.dry_run, backup_writer=backup_writer,
        )
        conn_s, conn_u, conn_f = _migrate_connections(
            conn, enc, dry_run=args.dry_run, backup_writer=backup_writer,
        )
        ver_s, ver_u, ver_f = _migrate_pipeline_versions(
            conn, "workflow_versions", enc, dry_run=args.dry_run, backup_writer=backup_writer,
        )
        bin_s, bin_u, bin_f = _migrate_pipeline_versions(
            conn, "workflow_trash", enc, dry_run=args.dry_run, backup_writer=backup_writer,
        )
        tpl_s, tpl_u, tpl_f = _blank_template_secrets(
            conn, dry_run=args.dry_run, backup_writer=backup_writer,
        )
        sch_s, sch_u, sch_f = _seal_saved_parameter_values(
            conn, "schedules", "workflow_id", "parameter_values", enc,
            dry_run=args.dry_run, backup_writer=backup_writer,
        )
        bsc_s, bsc_u, bsc_f = _seal_binned_schedules(
            conn, enc, dry_run=args.dry_run, backup_writer=backup_writer,
        )
        bfl_s, bfl_u, bfl_f = _seal_saved_parameter_values(
            conn, "backfill_runs", "pipeline_id", "params_template", enc,
            dry_run=args.dry_run, backup_writer=backup_writer,
        )
        run_s = run_u = run_f = run_after = 0
        while True:
            scanned, updated, failed, run_after = _encrypt_run_records(
                conn, run_after, _RUN_RECORD_BATCH, enc,
                dry_run=args.dry_run, backup_writer=backup_writer,
            )
            run_s += scanned
            run_u += updated
            run_f += failed
            if scanned < _RUN_RECORD_BATCH:
                break
        user_s, user_u, user_f = _migrate_ai_config(
            conn, "user_ai_config", enc,
            dry_run=args.dry_run, backup_writer=backup_writer,
        )
        ws_s, ws_u, ws_f = _migrate_ai_config(
            conn, "workspace_ai_config", enc,
            dry_run=args.dry_run, backup_writer=backup_writer,
        )

        if not args.dry_run:
            conn.commit()
    finally:
        conn.close()
        if backup_fh is not None:
            backup_fh.close()

    print()
    print("Migration report")
    print("─" * 60)
    print(f"  credentials          scanned={cred_s:>4}  updated={cred_u:>4}  failed={cred_f}")
    print(f"  connections          scanned={conn_s:>4}  updated={conn_u:>4}  failed={conn_f}")
    print(f"  pipeline versions    scanned={ver_s:>4}  updated={ver_u:>4}  failed={ver_f}")
    print(f"  Bin versions         scanned={bin_s:>4}  updated={bin_u:>4}  failed={bin_f}")
    print(f"  user templates       scanned={tpl_s:>4}  updated={tpl_u:>4}  failed={tpl_f}")
    print(f"  schedules            scanned={sch_s:>4}  updated={sch_u:>4}  failed={sch_f}")
    print(f"  Bin schedules        scanned={bsc_s:>4}  updated={bsc_u:>4}  failed={bsc_f}")
    print(f"  backfills            scanned={bfl_s:>4}  updated={bfl_u:>4}  failed={bfl_f}")
    print(f"  run records          scanned={run_s:>4}  updated={run_u:>4}  failed={run_f}")
    print(f"  user_ai_config       scanned={user_s:>4}  updated={user_u:>4}  failed={user_f}")
    print(f"  workspace_ai_config  scanned={ws_s:>4}  updated={ws_u:>4}  failed={ws_f}")
    total_failed = cred_f + conn_f + ver_f + bin_f + tpl_f + sch_f + bsc_f + bfl_f + run_f + user_f + ws_f
    if args.dry_run:
        print("\n  [dry-run] no rows written.")
    if backup_path:
        print(f"\n  Backup snapshot: {backup_path}")
    if total_failed > 0:
        print("\n  ⚠ Some rows failed. Review the warnings above.")
        return 1
    print("\n  ✓ Migration successful." if not args.dry_run else "\n  ✓ Dry-run successful.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
