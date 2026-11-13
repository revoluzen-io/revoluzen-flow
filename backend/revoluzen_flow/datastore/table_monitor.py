"""Table monitors: a managed table's tests, run without anyone pressing Run.

A table's tests used to run only from the Run button in Storage, so a table
that stopped being loaded, or was loaded with duplicates, was found when
someone happened to look. A monitor runs them:

* **after every load** into the table, in the background, so the pipeline
  that wrote it is not slowed down; and/or
* **every N minutes**, which is the only way to catch the table that
  stopped being refreshed at all: no load, so nothing runs after a load.

Each run is kept on the table (``last_test_run``). A failed test becomes a
Steward quality finding, the same surface every other data problem lands
on, and a bell notification.

The periodic loop runs where the scheduler runs (``REVOLUZEN_FLOW_ENABLE_
SCHEDULER``), and only on the instance holding the scheduler's lease when
there is one, so two instances never check the same table twice.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Any

from revoluzen_flow.datastore.table_tests import run_tests_for_table

logger = logging.getLogger("revoluzen_flow.table_monitor")

LOOP_SECONDS = 60
MIN_EVERY_MINUTES = 5

# How each row-level table test reads to the Steward's quality recorder.
_STEWARD_CHECK = {
    "not_null": "not_null",
    "unique": "unique",
    "accepted_values": "accepted_values",
    "relationships": "referential_integrity",
}


def _data_dir(app_state: dict) -> str:
    return str(app_state.get("data_dir") or "")


def _assertion(r: dict[str, Any], label: str, table_rows: int) -> dict[str, Any]:
    """One failed table test as a Steward quality assertion.

    Row checks count the bad rows. Freshness is one miss. A row-count test
    reports the rows the table now holds, as a drop or a jump, because that
    is the number the Steward's volume title prints.
    """
    tt = r.get("test_type")
    message = f"{label}: {r.get('message') or tt}"
    if int(r.get("violations") or 0) < 0:
        # The test itself could not run (a renamed column, a missing table).
        return {"check": "custom", "column": tt or "", "failed_count": 1, "total_rows": 0,
                "message": f"{label}: the {tt} test could not run. {r.get('message') or ''}".strip()}
    if tt == "freshness":
        return {"check": "freshness", "column": "", "failed_count": 1, "total_rows": 0, "message": message}
    if tt == "row_count_anomaly":
        actual = int(r.get("actual_rows") or 0)
        prior = r.get("prior_rows")
        low = actual < int(prior) if prior is not None else "<" in str(r.get("message") or "")
        return {"check": "row_count_min" if low else "row_count_max", "column": "",
                "failed_count": max(actual, 1), "total_rows": 0, "message": message}
    return {"check": _STEWARD_CHECK.get(tt, "custom"), "column": ", ".join(r.get("columns") or []),
            "failed_count": max(int(r.get("violations") or 0), 1), "total_rows": table_rows, "message": message}


def record_test_run(app_state: dict, table: Any, result: dict[str, Any], *, trigger: str, run_id: str | None = None) -> None:
    """Keep the result on the table; report failures to the Steward and the bell."""
    store = app_state.get("datastore")
    summary = {
        "ran_at": result.get("ran_at"),
        "trigger": trigger,
        "run_id": run_id,
        "passed": result.get("passed", 0),
        "failed": result.get("failed", 0),
        "results": result.get("results", []),
    }
    if store is not None:
        try:
            # Re-read: a load may have saved the table since this run began.
            fresh = store.get_table(table.id) or table
            fresh.last_test_run = summary
            store.save_table(fresh, touch=False)  # a test run is not an edit
        except Exception:  # noqa: BLE001
            logger.warning("could not save the test run on table %s", table.id, exc_info=True)

    # A "warn" test's failure stays on the table; a "fail" test (the
    # default) is reported. Severity meant nothing before 2026-11-11.
    failures = [
        r for r in result.get("results", [])
        if not r.get("ok") and str(r.get("severity") or "fail") != "warn"
    ]
    if not failures:
        return
    label = f"{table.schema_name}.{table.name}"
    try:
        from revoluzen_flow.api.steward import _get_quality_finding_store
        from revoluzen_flow.steward.quality import QualityAssertion, QualityCheckReport, record_quality_report

        record_quality_report(
            _get_quality_finding_store(table.workspace_id),
            QualityCheckReport(
                source_signature=f"managed_table:{table.id}",
                source_label=label,
                run_id=run_id or "",
                assertions=[
                    QualityAssertion(**_assertion(r, label, int(getattr(table, "row_count", 0) or 0)))
                    for r in failures
                ],
            ),
            workspace_id=table.workspace_id,
        )
    except Exception:  # noqa: BLE001
        logger.warning("could not record Steward findings for table %s", label, exc_info=True)

    try:
        from revoluzen_flow.notifications.models import Notification

        notes = app_state.get("notification_store")
        users = app_state.get("user_store")
        if notes is not None and users is not None:
            # The admins, as for any unattended failure nobody is watching.
            admins = [
                u["id"] for u in (users.list_users() or [])
                if u.get("id") and u.get("role") in ("super_admin", "admin") and u.get("is_active", True)
            ]
            for uid in admins:
                notes.create(Notification(
                    user_id=uid,
                    type="system",
                    title=f"{label} failed {len(failures)} test{'s' if len(failures) != 1 else ''}",
                    message="; ".join(
                        f"{r.get('test_type')} {', '.join(r.get('columns') or [])}".strip()
                        for r in failures[:4]
                    ),
                    link_type="storage",
                    link_id=table.id,
                    metadata={"table_id": table.id, "workspace_id": table.workspace_id, "trigger": trigger},
                ))
    except Exception:  # noqa: BLE001
        logger.debug("table test notification failed", exc_info=True)


def run_monitor(app_state: dict, table_id: str, workspace_id: str, *, trigger: str, run_id: str | None = None) -> dict[str, Any] | None:
    """Run one table's tests now and record the result. Never raises."""
    store = app_state.get("datastore")
    if store is None:
        return None
    try:
        table = store.get_table(table_id, workspace_id=workspace_id)
        if table is None or not table.tests:
            return None
        result = run_tests_for_table(store, table, workspace_id=workspace_id, data_dir=_data_dir(app_state))
        record_test_run(app_state, table, result, trigger=trigger, run_id=run_id)
        return result
    except Exception:  # noqa: BLE001
        logger.warning("table monitor failed for %s", table_id, exc_info=True)
        return None


def run_in_background(app_state: dict, table_id: str, workspace_id: str, *, trigger: str, run_id: str | None = None) -> None:
    """Run a table's tests off the caller's thread (a sink, mid-run)."""
    threading.Thread(
        target=run_monitor,
        args=(app_state, table_id, workspace_id),
        kwargs={"trigger": trigger, "run_id": run_id},
        daemon=True,
        name=f"revoluzen_flow-table-monitor-{table_id[:8]}",
    ).start()


def due_tables(app_state: dict, now: datetime | None = None) -> list[Any]:
    """Tables with a periodic monitor whose interval has passed."""
    from revoluzen_flow.datastore.models import StorageTable
    import json

    db = app_state.get("db")
    if db is None:
        return []
    now = now or datetime.now(timezone.utc)
    out = []
    for row in db.fetchall("SELECT data FROM storage_tables"):
        try:
            table = StorageTable(**json.loads(row["data"] if isinstance(row, dict) else row[0]))
        except Exception:  # noqa: BLE001
            continue
        every = table.monitor_every_minutes
        if not every or not table.tests:
            continue
        last = (table.last_test_run or {}).get("ran_at")
        try:
            last_dt = datetime.fromisoformat(str(last).replace("Z", "+00:00")) if last else None
        except ValueError:
            last_dt = None
        if last_dt is not None and last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=timezone.utc)
        if last_dt is None or (now - last_dt).total_seconds() >= max(every, MIN_EVERY_MINUTES) * 60:
            out.append(table)
    return out


class TableMonitorLoop:
    """Every minute, run the tests of each table whose interval has passed."""

    def __init__(self, app_state: dict):
        self.app_state = app_state
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _may_run_here(self) -> bool:
        scheduler = self.app_state.get("scheduler")
        holds = getattr(scheduler, "_holds_lease", None)
        if callable(holds):
            try:
                return bool(holds())
            except Exception:  # noqa: BLE001
                return True
        return True

    def tick(self) -> int:
        """One pass. Returns how many tables were checked."""
        if not self._may_run_here():
            return 0
        checked = 0
        for table in due_tables(self.app_state):
            run_monitor(self.app_state, table.id, table.workspace_id, trigger="schedule")
            checked += 1
        return checked

    def _loop(self) -> None:
        while not self._stop.wait(LOOP_SECONDS):
            try:
                self.tick()
            except Exception:  # noqa: BLE001 — the loop outlives a bad pass
                logger.warning("table monitor pass failed", exc_info=True)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, daemon=True, name="revoluzen_flow-table-monitors")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()


__all__ = ["TableMonitorLoop", "due_tables", "record_test_run", "run_in_background", "run_monitor"]
