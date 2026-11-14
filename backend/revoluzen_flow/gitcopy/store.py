"""One row for each copy to make, and how it went.

The table is this store's own, created here like the Execution Contract and
gateway tables. It is a work list and a status, not the record of what was
published: that is the pipeline's versions and lifecycle events.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

#: Minutes to wait after each failed attempt. After the last one, a row waits
#: for someone to press Try again.
RETRY_MINUTES = (1, 5, 15, 60, 60, 240, 240, 1440)
#: A row claimed for longer than this belonged to a server that stopped.
STUCK_AFTER_MINUTES = 15


def _now() -> datetime:
    return datetime.now(timezone.utc)


class GitCopyStore:
    def __init__(self, db):
        self.db = db
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS pipeline_git_copies (
                id              TEXT PRIMARY KEY,
                workspace_id    TEXT NOT NULL DEFAULT 'default',
                workflow_id     TEXT NOT NULL,
                version         INTEGER,
                event           TEXT NOT NULL,
                actor_name      TEXT NOT NULL DEFAULT '',
                actor_email     TEXT NOT NULL DEFAULT '',
                note            TEXT NOT NULL DEFAULT '',
                status          TEXT NOT NULL,
                attempts        INTEGER NOT NULL DEFAULT 0,
                commit_sha      TEXT NOT NULL DEFAULT '',
                commit_url      TEXT NOT NULL DEFAULT '',
                error           TEXT NOT NULL DEFAULT '',
                next_attempt_at TEXT,
                created_at      TEXT NOT NULL,
                updated_at      TEXT NOT NULL
            )
        """)
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS idx_git_copies_workflow "
            "ON pipeline_git_copies(workflow_id, created_at)"
        )
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS idx_git_copies_status "
            "ON pipeline_git_copies(status, next_attempt_at)"
        )
        self.db.commit()

    def add(
        self,
        *,
        workflow_id: str,
        version: int | None,
        workspace_id: str,
        event: str,
        actor_name: str = "",
        actor_email: str = "",
        note: str = "",
    ) -> dict[str, Any]:
        now = _now().isoformat()
        row_id = uuid.uuid4().hex
        self.db.execute_write(
            "INSERT INTO pipeline_git_copies (id, workspace_id, workflow_id, version, event, "
            "actor_name, actor_email, note, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
            (row_id, workspace_id, workflow_id, version, event, actor_name or "", actor_email or "", note or "", now, now),
        )
        return self.get(row_id)

    def get(self, row_id: str) -> dict[str, Any] | None:
        return self.db.fetchone("SELECT * FROM pipeline_git_copies WHERE id = ?", (row_id,))

    def release_stuck(self) -> None:
        cutoff = (_now() - timedelta(minutes=STUCK_AFTER_MINUTES)).isoformat()
        self.db.execute_write(
            "UPDATE pipeline_git_copies SET status = 'pending', next_attempt_at = NULL "
            "WHERE status = 'working' AND updated_at < ?",
            (cutoff,),
        )

    def _next_due(self) -> dict[str, Any] | None:
        """Oldest first, so commits land in the order things happened."""
        return self.db.fetchone(
            "SELECT * FROM pipeline_git_copies "
            "WHERE status = 'pending' OR (status = 'failed' AND next_attempt_at IS NOT NULL AND next_attempt_at <= ?) "
            "ORDER BY created_at, id LIMIT 1",
            (_now().isoformat(),),
        )

    def has_due(self) -> bool:
        return self._next_due() is not None

    def claim_next(self) -> dict[str, Any] | None:
        """Take the next copy to make. Two workers cannot take the same one."""
        while True:
            row = self._next_due()
            if row is None:
                return None
            claimed = self.db.execute_write(
                "UPDATE pipeline_git_copies SET status = 'working', attempts = attempts + 1, updated_at = ? "
                "WHERE id = ? AND status IN ('pending', 'failed')",
                (_now().isoformat(), row["id"]),
            )
            if claimed.rowcount:
                return self.get(row["id"])

    def finish(self, row_id: str, status: str, *, commit_sha: str = "", commit_url: str = "") -> None:
        self.db.execute_write(
            "UPDATE pipeline_git_copies SET status = ?, commit_sha = ?, commit_url = ?, error = '', "
            "next_attempt_at = NULL, updated_at = ? WHERE id = ?",
            (status, commit_sha, commit_url, _now().isoformat(), row_id),
        )

    def fail(self, row_id: str, error: str, *, retry: bool = True) -> None:
        row = self.get(row_id)
        attempts = int(row["attempts"]) if row else 1
        next_attempt = None
        if retry and attempts <= len(RETRY_MINUTES):
            next_attempt = (_now() + timedelta(minutes=RETRY_MINUTES[attempts - 1])).isoformat()
        self.db.execute_write(
            "UPDATE pipeline_git_copies SET status = 'failed', error = ?, next_attempt_at = ?, updated_at = ? WHERE id = ?",
            (error[:1000], next_attempt, _now().isoformat(), row_id),
        )

    def retry(self, row_id: str) -> None:
        self.db.execute_write(
            "UPDATE pipeline_git_copies SET status = 'pending', attempts = 0, next_attempt_at = NULL, "
            "updated_at = ? WHERE id = ? AND status = 'failed'",
            (_now().isoformat(), row_id),
        )

    def latest_for(self, workflow_id: str, workspace_id: str) -> dict[str, Any] | None:
        return self.db.fetchone(
            "SELECT * FROM pipeline_git_copies WHERE workflow_id = ? AND workspace_id = ? "
            "ORDER BY created_at DESC, id DESC LIMIT 1",
            (workflow_id, workspace_id),
        )

    def recent(self, workspace_id: str, limit: int = 20) -> list[dict[str, Any]]:
        return self.db.fetchall(
            "SELECT * FROM pipeline_git_copies WHERE workspace_id = ? ORDER BY created_at DESC, id DESC LIMIT ?",
            (workspace_id, limit),
        )
