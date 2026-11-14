"""What the event triggers remember between checks.

Three tables, made here like the gateway and Git copy tables, so they need no
schema version:

* ``trigger_files``: each file a file-arrival trigger has seen, by path and
  version. The primary key is what makes "never two runs for one file" hold:
  a run starts only after its row was inserted, and a second insert of the same
  file does nothing. Rows outlive restarts and a change of server.
* ``trigger_state``: per trigger, which folder settings the record of existing
  files was taken for. Changing the folder or the pattern takes a new one.
* ``trigger_events``: a finished pipeline waiting to start an after-pipeline
  trigger. One row per trigger: if it finishes again before the trigger could
  start, the newer finish replaces the older, so the trigger runs once more,
  not once per finish.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

#: A file that has left its folder is forgotten this long afterwards. If it
#: comes back after that, it counts as a new arrival.
FORGET_GONE_AFTER = timedelta(days=30)


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class ChainEvent:
    trigger_id: str
    workspace_id: str
    upstream_id: str
    upstream_name: str
    execution_id: str
    status: str
    chain: list[str]
    received_at: str


class TriggerStore:
    def __init__(self, db):
        self.db = db
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS trigger_files (
                trigger_id   TEXT NOT NULL,
                path         TEXT NOT NULL,
                version      TEXT NOT NULL,
                size         INTEGER,
                modified_at  TEXT,
                status       TEXT NOT NULL,
                execution_id TEXT NOT NULL DEFAULT '',
                run_status   TEXT NOT NULL DEFAULT '',
                seen_at      TEXT NOT NULL,
                started_at   TEXT,
                finished_at  TEXT,
                gone_since   TEXT,
                PRIMARY KEY (trigger_id, path, version)
            )
        """)
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS trigger_state (
                trigger_id      TEXT PRIMARY KEY,
                watch_signature TEXT NOT NULL,
                baseline_at     TEXT NOT NULL
            )
        """)
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS trigger_events (
                trigger_id    TEXT PRIMARY KEY,
                workspace_id  TEXT NOT NULL,
                upstream_id   TEXT NOT NULL,
                upstream_name TEXT NOT NULL DEFAULT '',
                execution_id  TEXT NOT NULL DEFAULT '',
                status        TEXT NOT NULL,
                chain         TEXT NOT NULL DEFAULT '[]',
                received_at   TEXT NOT NULL
            )
        """)
        self.db.commit()

    # ── Files ────────────────────────────────────────────────────────────

    def known_versions(self, trigger_id: str) -> set[tuple[str, str]]:
        rows = self.db.fetchall(
            "SELECT path, version FROM trigger_files WHERE trigger_id = ?", (trigger_id,),
        )
        return {(r["path"], r["version"]) for r in rows}

    def mark_existing(self, trigger_id: str, files: Iterable[Any]) -> int:
        """Remember files that were already there, so they never start a run."""
        now = _now().isoformat()
        count = 0
        with self.db.transaction() as tx:
            for f in files:
                cur = tx.execute(
                    "INSERT OR IGNORE INTO trigger_files (trigger_id, path, version, size, modified_at, "
                    "status, seen_at) VALUES (?, ?, ?, ?, ?, 'existing', ?)",
                    (trigger_id, f.path, f.version, f.size, _iso(f.modified), now),
                )
                count += cur.rowcount or 0
        return count

    def claim(self, trigger_id: str, f: Any) -> bool:
        """Take one file for a run. False if it was already taken, by this
        server or any other: the row exists, so the run must not start."""
        now = _now().isoformat()
        cur = self.db.execute_write(
            "INSERT OR IGNORE INTO trigger_files (trigger_id, path, version, size, modified_at, "
            "status, seen_at, started_at) VALUES (?, ?, ?, ?, ?, 'started', ?, ?)",
            (trigger_id, f.path, f.version, f.size, _iso(f.modified), now, now),
        )
        return bool(cur.rowcount)

    def release(self, trigger_id: str, path: str, version: str) -> None:
        """Give back a file whose run never started, so the next check tries again."""
        self.db.execute_write(
            "DELETE FROM trigger_files WHERE trigger_id = ? AND path = ? AND version = ? "
            "AND status = 'started' AND execution_id = ''",
            (trigger_id, path, version),
        )

    def finish(self, trigger_id: str, path: str, version: str, run_status: str, execution_id: str) -> None:
        self.db.execute_write(
            "UPDATE trigger_files SET status = 'done', run_status = ?, execution_id = ?, finished_at = ? "
            "WHERE trigger_id = ? AND path = ? AND version = ?",
            (run_status or "", execution_id or "", _now().isoformat(), trigger_id, path, version),
        )

    def note_listing(self, trigger_id: str, present_paths: set[str]) -> None:
        """Mark the remembered files that have left the folder, clear the mark on
        ones that are back, and forget those gone for a long time."""
        now = _now()
        rows = self.db.fetchall(
            "SELECT path, version, gone_since FROM trigger_files WHERE trigger_id = ?", (trigger_id,),
        )
        gone_now, back = [], []
        for row in rows:
            if row["path"] in present_paths:
                if row["gone_since"]:
                    back.append((trigger_id, row["path"], row["version"]))
            elif not row["gone_since"]:
                gone_now.append((now.isoformat(), trigger_id, row["path"], row["version"]))
        cutoff = (now - FORGET_GONE_AFTER).isoformat()
        with self.db.transaction() as tx:
            if gone_now:
                tx.executemany(
                    "UPDATE trigger_files SET gone_since = ? WHERE trigger_id = ? AND path = ? AND version = ?",
                    gone_now,
                )
            if back:
                tx.executemany(
                    "UPDATE trigger_files SET gone_since = NULL WHERE trigger_id = ? AND path = ? AND version = ?",
                    back,
                )
            tx.execute(
                "DELETE FROM trigger_files WHERE trigger_id = ? AND gone_since IS NOT NULL AND gone_since < ? "
                "AND status != 'started'",
                (trigger_id, cutoff),
            )

    def recent_files(self, trigger_id: str, limit: int = 20) -> list[dict]:
        return self.db.fetchall(
            "SELECT path, version, size, modified_at, status, execution_id, run_status, seen_at, started_at, "
            "finished_at FROM trigger_files WHERE trigger_id = ? AND status != 'existing' "
            "ORDER BY COALESCE(started_at, seen_at) DESC LIMIT ?",
            (trigger_id, limit),
        )

    # ── The record of what was already there ─────────────────────────────

    def baseline_signature(self, trigger_id: str) -> str | None:
        row = self.db.fetchone("SELECT watch_signature FROM trigger_state WHERE trigger_id = ?", (trigger_id,))
        return row["watch_signature"] if row else None

    def set_baseline(self, trigger_id: str, signature: str) -> None:
        self.db.execute_write(
            "INSERT OR REPLACE INTO trigger_state (trigger_id, watch_signature, baseline_at) VALUES (?, ?, ?)",
            (trigger_id, signature, _now().isoformat()),
        )

    # ── After-pipeline events ────────────────────────────────────────────

    def put_event(
        self,
        *,
        trigger_id: str,
        workspace_id: str,
        upstream_id: str,
        upstream_name: str,
        execution_id: str,
        status: str,
        chain: list[str],
    ) -> None:
        self.db.execute_write(
            "INSERT OR REPLACE INTO trigger_events (trigger_id, workspace_id, upstream_id, upstream_name, "
            "execution_id, status, chain, received_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (trigger_id, workspace_id, upstream_id, upstream_name or "", execution_id or "", status,
             json.dumps(list(chain or [])), _now().isoformat()),
        )

    def pending_events(self) -> list[ChainEvent]:
        rows = self.db.fetchall("SELECT * FROM trigger_events ORDER BY received_at, trigger_id")
        events = []
        for row in rows:
            try:
                chain = json.loads(row.get("chain") or "[]")
            except ValueError:
                chain = []
            events.append(ChainEvent(
                trigger_id=row["trigger_id"], workspace_id=row["workspace_id"], upstream_id=row["upstream_id"],
                upstream_name=row.get("upstream_name") or "", execution_id=row.get("execution_id") or "",
                status=row["status"], chain=[str(c) for c in chain if c], received_at=row["received_at"],
            ))
        return events

    def take_event(self, event: ChainEvent) -> bool:
        """Remove an event to act on it. False if another server took it, or a
        newer finish replaced it in the meantime (that one is acted on next)."""
        cur = self.db.execute_write(
            "DELETE FROM trigger_events WHERE trigger_id = ? AND received_at = ? AND execution_id = ?",
            (event.trigger_id, event.received_at, event.execution_id),
        )
        return bool(cur.rowcount)

    def drop_event(self, trigger_id: str) -> None:
        self.db.execute_write("DELETE FROM trigger_events WHERE trigger_id = ?", (trigger_id,))

    # ── Housekeeping ─────────────────────────────────────────────────────

    def forget_trigger(self, trigger_id: str) -> None:
        with self.db.transaction() as tx:
            for table in ("trigger_files", "trigger_state", "trigger_events"):
                tx.execute(f"DELETE FROM {table} WHERE trigger_id = ?", (trigger_id,))

    def forget_deleted_triggers(self) -> None:
        """Drop what deleted triggers left behind."""
        with self.db.transaction() as tx:
            for table in ("trigger_files", "trigger_state", "trigger_events"):
                tx.execute(f"DELETE FROM {table} WHERE trigger_id NOT IN (SELECT id FROM schedules)")


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


_STORES: dict[int, TriggerStore] = {}


def trigger_store(app_state: dict) -> TriggerStore | None:
    """The store for the running app's database, built on first use."""
    db = app_state.get("db") if isinstance(app_state, dict) else None
    if db is None:
        return None
    store = app_state.get("trigger_store")
    if isinstance(store, TriggerStore) and store.db is db:
        return store
    store = _STORES.get(id(db))
    if store is None or store.db is not db:
        store = TriggerStore(db)
        _STORES[id(db)] = store
    app_state["trigger_store"] = store
    return store
