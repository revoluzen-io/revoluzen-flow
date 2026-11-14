"""Saved reports and the log of what went out."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from .models import DeliveryState, ReportDelivery, SavedReport

logger = logging.getLogger("revoluzen_flow.reports.builder")

#: What the API shows in place of a stored webhook address, and what the form
#: sends back to mean "keep it". The same placeholder Connections use.
SECRET_PLACEHOLDER = "•" * 8

#: A webhook address is enough to post into someone's channel, so it is
#: encrypted at rest and never sent back to the browser.
SECRET_TARGET_TYPES = frozenset({"slack", "teams", "webhook"})

KEEP_DELIVERIES = 100
MAX_SNAPSHOT_BYTES = 512 * 1024


def _time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def masked(report: SavedReport) -> dict[str, Any]:
    """A saved report as the API returns it."""
    data = report.model_dump(mode="json")
    for recipient in data["recipients"]:
        if recipient["type"] in SECRET_TARGET_TYPES and recipient["target"]:
            recipient["target"] = SECRET_PLACEHOLDER
    return data


class ReportStore:
    def __init__(self, db=None):
        self._db = db

    def set_db(self, db) -> None:
        self._db = db

    @staticmethod
    def _encryptor():
        from revoluzen_flow.state import try_get_encryptor

        return try_get_encryptor()

    def _to_disk(self, report: SavedReport) -> str:
        data = report.model_dump(mode="json")
        encryptor = self._encryptor()
        if encryptor is not None:
            for recipient in data["recipients"]:
                if recipient["type"] in SECRET_TARGET_TYPES and recipient["target"]:
                    recipient["target"] = encryptor.encrypt_value(recipient["target"])
        return json.dumps(data, default=str)

    def _from_disk(self, raw: str) -> SavedReport:
        data = json.loads(raw)
        encryptor = self._encryptor()
        if encryptor is not None:
            for recipient in data.get("recipients") or []:
                target = recipient.get("target") or ""
                if recipient.get("type") in SECRET_TARGET_TYPES and target.startswith("ENC:"):
                    try:
                        recipient["target"] = encryptor.decrypt_value(target)
                    except RuntimeError:
                        # The master key changed. The send will say the address
                        # is unusable; the value is never logged.
                        recipient["target"] = ""
                        logger.warning("Report %s: a stored webhook address could not be decrypted", data.get("id"))
        return SavedReport(**data)

    @staticmethod
    def _state(row: dict) -> DeliveryState:
        return DeliveryState(
            occurrence=row.get("occurrence") or "",
            occurrence_status=row.get("occurrence_status") or "",
            attempts=int(row.get("attempts") or 0),
            last_attempt_at=_time(row.get("last_attempt_at")),
            last_sent_at=_time(row.get("last_sent_at")),
            last_status=row.get("last_status") or "",
            last_error=row.get("last_error") or "",
            last_trigger=row.get("last_trigger") or "",
        )

    # ── Saved reports ───────────────────────────────────────────────────

    def create(self, report: SavedReport) -> SavedReport:
        self._db.execute_write(
            "INSERT INTO saved_reports (id, workspace_id, name, schedule_enabled, data, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                report.id, report.workspace_id, report.name,
                1 if report.schedule.enabled else 0, self._to_disk(report),
                report.created_at.isoformat(), report.updated_at.isoformat(),
            ),
        )
        return report

    def update(self, report: SavedReport) -> SavedReport:
        """Save the report's definition. The delivery columns are left alone."""
        report.updated_at = datetime.now(timezone.utc)
        self._db.execute_write(
            "UPDATE saved_reports SET name = ?, schedule_enabled = ?, data = ?, updated_at = ? "
            "WHERE id = ? AND workspace_id = ?",
            (
                report.name, 1 if report.schedule.enabled else 0, self._to_disk(report),
                report.updated_at.isoformat(), report.id, report.workspace_id,
            ),
        )
        return report

    def get(self, report_id: str, workspace_id: str | None = None) -> SavedReport | None:
        sql = "SELECT data FROM saved_reports WHERE id = ?"
        params: list[Any] = [report_id]
        if workspace_id is not None:
            sql += " AND workspace_id = ?"
            params.append(workspace_id)
        row = self._db.fetchone(sql, tuple(params))
        return self._from_disk(row["data"]) if row else None

    def state(self, report_id: str) -> DeliveryState:
        row = self._db.fetchone("SELECT * FROM saved_reports WHERE id = ?", (report_id,))
        return self._state(row) if row else DeliveryState()

    def list_all(self, workspace_id: str) -> list[tuple[SavedReport, DeliveryState]]:
        rows = self._db.fetchall(
            "SELECT * FROM saved_reports WHERE workspace_id = ? ORDER BY LOWER(name)",
            (workspace_id,),
        )
        return [(self._from_disk(r["data"]), self._state(r)) for r in rows]

    def list_scheduled(self) -> list[tuple[SavedReport, DeliveryState]]:
        """Every report with a schedule turned on, in every workspace."""
        rows = self._db.fetchall("SELECT * FROM saved_reports WHERE schedule_enabled = 1")
        out: list[tuple[SavedReport, DeliveryState]] = []
        for row in rows:
            try:
                out.append((self._from_disk(row["data"]), self._state(row)))
            except Exception:  # one bad row must not stop every other report
                logger.warning("Saved report %s could not be read", row.get("id"), exc_info=True)
        return out

    def name_taken(self, workspace_id: str, name: str, exclude_id: str | None = None) -> bool:
        sql = "SELECT 1 AS present FROM saved_reports WHERE workspace_id = ? AND LOWER(name) = LOWER(?)"
        params: list[Any] = [workspace_id, name]
        if exclude_id:
            sql += " AND id != ?"
            params.append(exclude_id)
        return self._db.fetchone(sql + " LIMIT 1", tuple(params)) is not None

    def delete(self, report_id: str, workspace_id: str) -> bool:
        with self._db.transaction() as tx:
            removed = tx.execute(
                "DELETE FROM saved_reports WHERE id = ? AND workspace_id = ?",
                (report_id, workspace_id),
            ).rowcount
            if removed:
                tx.execute(
                    "DELETE FROM report_deliveries WHERE report_id = ? AND workspace_id = ?",
                    (report_id, workspace_id),
                )
        return removed > 0

    # ── How the schedule stands ─────────────────────────────────────────

    def begin_occurrence(self, report_id: str, occurrence: str, at: datetime) -> None:
        """Note an attempt before it starts. It counts as failed until it is
        finished, so a crash in the middle leaves it able to be tried again."""
        self._db.execute_write(
            "UPDATE saved_reports SET "
            "attempts = CASE WHEN occurrence = ? THEN attempts + 1 ELSE 1 END, "
            "occurrence = ?, occurrence_status = 'failed', last_attempt_at = ? WHERE id = ?",
            (occurrence, occurrence, at.isoformat(), report_id),
        )

    def finish_occurrence(self, report_id: str, status: str) -> None:
        self._db.execute_write(
            "UPDATE saved_reports SET occurrence_status = ? WHERE id = ?",
            (status, report_id),
        )

    def record_outcome(self, report_id: str, status: str, error: str, trigger: str, at: datetime) -> None:
        reached = status in ("delivered", "partial")
        self._db.execute_write(
            "UPDATE saved_reports SET last_status = ?, last_error = ?, last_trigger = ?, last_attempt_at = ?, "
            "last_sent_at = CASE WHEN ? THEN ? ELSE last_sent_at END WHERE id = ?",
            (status, error, trigger, at.isoformat(), 1 if reached else 0, at.isoformat(), report_id),
        )

    # ── What went out ───────────────────────────────────────────────────

    def add_delivery(self, delivery: ReportDelivery, snapshot: dict | None) -> None:
        body = json.dumps(snapshot, default=str) if snapshot is not None else None
        if body is not None and len(body) > MAX_SNAPSHOT_BYTES:
            body = None
        with self._db.transaction() as tx:
            tx.execute(
                "INSERT INTO report_deliveries (id, report_id, workspace_id, status, triggered_by, started_at, data, snapshot) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    delivery.id, delivery.report_id, delivery.workspace_id, delivery.status,
                    delivery.trigger, delivery.started_at.isoformat(),
                    json.dumps(delivery.model_dump(mode="json"), default=str), body,
                ),
            )
            tx.execute(
                "DELETE FROM report_deliveries WHERE report_id = ? AND id NOT IN ("
                "SELECT id FROM report_deliveries WHERE report_id = ? ORDER BY started_at DESC LIMIT ?)",
                (delivery.report_id, delivery.report_id, KEEP_DELIVERIES),
            )

    def list_deliveries(self, report_id: str, workspace_id: str, limit: int = 50) -> list[dict]:
        rows = self._db.fetchall(
            "SELECT data, snapshot IS NOT NULL AS has_snapshot FROM report_deliveries "
            "WHERE report_id = ? AND workspace_id = ? ORDER BY started_at DESC LIMIT ?",
            (report_id, workspace_id, max(1, min(limit, KEEP_DELIVERIES))),
        )
        return [{**json.loads(r["data"]), "has_snapshot": bool(r["has_snapshot"])} for r in rows]

    def get_snapshot(self, report_id: str, delivery_id: str, workspace_id: str) -> dict | None:
        row = self._db.fetchone(
            "SELECT snapshot FROM report_deliveries WHERE id = ? AND report_id = ? AND workspace_id = ?",
            (delivery_id, report_id, workspace_id),
        )
        if not row or not row["snapshot"]:
            return None
        return json.loads(row["snapshot"])


# ── Which versions of Flow have run here ────────────────────────────────
#
# Nothing used to record this: the Environment section could say what version
# is running but not when it arrived, and "new build" had no entry for Flow
# itself. One small list in _meta, added to at startup when the version changes.

_APP_VERSIONS_KEY = "app_version_history"
_MAX_APP_VERSIONS = 50


def read_app_versions(db) -> list[dict]:
    """Oldest first. The first entry is when recording began, not an upgrade."""
    row = db.fetchone("SELECT value FROM _meta WHERE key = ?", (_APP_VERSIONS_KEY,))
    if not row:
        return []
    try:
        history = json.loads(row["value"])
    except (TypeError, ValueError):
        return []
    return history if isinstance(history, list) else []


def note_app_version(db, version: str, now: datetime | None = None) -> bool:
    """Record this version if it is not the one that ran last. True if added."""
    history = read_app_versions(db)
    if history and history[-1].get("version") == version:
        return False
    at = now or datetime.now(timezone.utc)
    history.append({"version": version, "first_seen_at": at.isoformat()})
    db.execute_write(
        "INSERT OR REPLACE INTO _meta (key, value) VALUES (?, ?)",
        (_APP_VERSIONS_KEY, json.dumps(history[-_MAX_APP_VERSIONS:])),
    )
    return True
