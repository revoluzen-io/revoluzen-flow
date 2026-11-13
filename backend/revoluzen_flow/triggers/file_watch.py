"""Start a run when a new file arrives in a folder.

Every poll (one minute unless set, never less than 30 seconds) the trigger lists
its folder and keeps the files whose names match its pattern. Then:

* **Already there.** When a trigger is saved with a new folder or pattern, the
  files already in the folder are written down as existing and never start a
  run, unless "also run for files already there" is on. A file changed after
  the trigger was saved counts as new.
* **Still arriving.** A file must be seen at least twice with the same size and
  modified time, and keep them for the settle time (one minute unless set),
  before its run starts. A file still being copied in keeps changing, so it
  waits. On a Windows file share the listing may not change while a file is
  copied in, so just before its run starts the file is also opened the way
  the File step opens it; while the program writing it still has it open, it
  waits another settle time.
* **One run per file.** The run starts only after the file is written down in
  ``trigger_files`` by path and version; a second attempt finds it there and
  does nothing. The record survives restarts. A file replaced by a new version
  (new content tag, or new size or time) is a new file and starts its own run.
* **One at a time.** A trigger runs one file at a time, oldest first. Waiting
  files start as soon as the run before them ends. A failed run is not retried:
  replay it from the Executions page.

The file's path reaches the pipeline as the parameter the trigger names, in the
form that location's Source step reads. Nothing about the file is lost if the
pipeline cannot run yet (not published, paused, overlap setting): the files
wait, and the trigger's status says why.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from revoluzen_flow.scheduling.models import ScheduleType
from revoluzen_flow.triggers.file_listing import (
    FoundFile,
    ListingError,
    list_folder,
    still_being_written,
)

logger = logging.getLogger("revoluzen_flow.triggers.file_watch")

TRIGGERED_BY = ScheduleType.FILE_ARRIVAL.value
#: How often what deleted triggers left behind is cleared away.
SWEEP_EVERY = timedelta(hours=1)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def name_matches(name: str, pattern: str) -> bool:
    """Whether a file name fits the pattern. Case does not matter, and several
    patterns can be given separated by semicolons: ``*.csv;*.txt``."""
    patterns = [p.strip().lower() for p in (pattern or "*").split(";") if p.strip()] or ["*"]
    lowered = (name or "").lower()
    return any(fnmatch.fnmatchcase(lowered, p) for p in patterns)


def watch_signature(row: dict) -> str:
    """What the record of existing files depends on: where, and which names."""
    parts = [
        row.get("file_location") or "",
        row.get("file_connection_id") or "",
        (row.get("file_folder") or "").strip(),
        (row.get("file_pattern") or "*").strip().lower(),
    ]
    return hashlib.sha256(json.dumps(parts).encode("utf-8")).hexdigest()


@dataclass
class _Seen:
    version: str
    since: datetime
    checks: int = 1


def _plural(count: int, one: str, many: str | None = None) -> str:
    return f"{count} {one if count == 1 else (many or one + 's')}"


class FileTriggerWatcher:
    """The file-arrival half of the scheduler. One per scheduler."""

    def __init__(
        self,
        scheduler: Any,
        *,
        lister: Callable[..., list[FoundFile]] | None = None,
        clock: Callable[[], datetime] | None = None,
        app_state: dict | None = None,
        writing: Callable[..., bool] | None = None,
    ):
        self._scheduler = scheduler
        self._lister = lister or list_folder
        self._writing = writing or still_being_written
        self._clock = clock or _utc_now
        self._app_state = app_state
        self._seen: dict[str, dict[str, _Seen]] = {}
        self._last_check: dict[str, datetime] = {}
        self._checking: set[str] = set()
        self._lock = threading.Lock()
        self._swept_at: datetime | None = None

    def _state(self) -> dict:
        if self._app_state is not None:
            return self._app_state
        from revoluzen_flow.main import app_state

        return app_state

    # ── When to look ─────────────────────────────────────────────────────

    def due(self, row: dict, now: datetime) -> bool:
        trigger_id = row.get("id") or ""
        if not row.get("enabled") or row.get("schedule_type") != TRIGGERED_BY:
            return False
        with self._lock:
            if trigger_id in self._checking:
                return False
        last = self._last_check.get(trigger_id)
        poll = max(30, int(row.get("file_poll_seconds") or 60))
        return last is None or (now - last).total_seconds() >= poll

    def begin(self, trigger_id: str) -> bool:
        """Claim the right to check this trigger now. False if a check is under way."""
        with self._lock:
            if trigger_id in self._checking:
                return False
            self._checking.add(trigger_id)
            return True

    def end(self, trigger_id: str) -> None:
        with self._lock:
            self._checking.discard(trigger_id)

    def forget(self, trigger_id: str) -> None:
        self._seen.pop(trigger_id, None)
        self._last_check.pop(trigger_id, None)

    # ── One look ─────────────────────────────────────────────────────────

    def check(self, row: dict) -> str:
        """List the folder, note what is new, and start the next run if one is
        due. Returns the status line it saved on the trigger."""
        from revoluzen_flow.triggers.store import trigger_store

        state = self._state()
        trigger_id = row["id"]
        schedule_store = state.get("schedule_store")
        store = trigger_store(state)
        now = self._clock()
        self._last_check[trigger_id] = now
        if schedule_store is None or store is None:
            return ""
        self._sweep(store, now)

        message = self._look(row, state, store, now)
        try:
            schedule_store.record_check(trigger_id, message, now)
        except Exception:  # noqa: BLE001
            logger.debug("Could not save the status of trigger %s", trigger_id, exc_info=True)
        return message

    def _look(self, row: dict, state: dict, store: Any, now: datetime) -> str:
        trigger_id = row["id"]
        pattern = row.get("file_pattern") or "*"
        try:
            listed = self._lister(
                row.get("file_location") or "",
                row.get("file_folder") or "",
                app_state=state,
                workspace_id=row.get("workspace_id") or "default",
                connection_id=row.get("file_connection_id") or "",
            )
        except ListingError as exc:
            return f"Cannot check the folder: {exc}"
        except Exception as exc:  # noqa: BLE001
            logger.warning("File trigger %s could not list its folder", trigger_id, exc_info=True)
            return f"Cannot check the folder: {type(exc).__name__}"

        files = [f for f in listed if name_matches(f.name, pattern)]
        present = {f.path for f in files}

        signature = watch_signature(row)
        if store.baseline_signature(trigger_id) != signature:
            if not row.get("file_include_existing"):
                saved_at = _parse_time(row.get("updated_at")) or _parse_time(row.get("created_at")) or now
                store.mark_existing(
                    trigger_id, [f for f in files if f.modified is None or f.modified <= saved_at],
                )
            store.set_baseline(trigger_id, signature)
            self._seen.pop(trigger_id, None)
        store.note_listing(trigger_id, present)

        known = store.known_versions(trigger_id)
        seen = self._seen.setdefault(trigger_id, {})
        for path in [p for p in seen if p not in present]:
            del seen[path]
        settle = int(row.get("file_settle_seconds") or 0)
        ready: list[FoundFile] = []
        arriving = 0
        for f in files:
            if (f.path, f.version) in known:
                continue
            noted = seen.get(f.path)
            if noted is None or noted.version != f.version:
                seen[f.path] = _Seen(version=f.version, since=now)
                arriving += 1
                continue
            noted.checks += 1
            if noted.checks >= 2 and (now - noted.since).total_seconds() >= settle:
                ready.append(f)
            else:
                arriving += 1
        oldest = datetime.min.replace(tzinfo=timezone.utc)
        ready.sort(key=lambda f: (f.modified or oldest, f.name))

        started = self._start_next(row, ready, state, store, now) if ready else ""
        notes = [started] if started else []
        if arriving:
            notes.append(f"{_plural(arriving, 'file')} still arriving.")
        if not notes:
            notes.append(f"No new files. {_plural(len(files), 'matching file')} in the folder.")
        return " ".join(notes)

    def _start_next(self, row: dict, ready: list[FoundFile], state: dict, store: Any, now: datetime) -> str:
        from revoluzen_flow.triggers.gate import check_trigger_can_run, declared_parameter

        trigger_id = row["id"]
        waiting = _plural(len(ready), "file")
        if self._scheduler.is_trigger_busy(trigger_id):
            return f"{waiting} waiting for the current run to finish."
        gate = check_trigger_can_run(state, row)
        if not gate.ok:
            return f"{gate.message} {waiting} waiting."
        parameter = (row.get("file_parameter") or "").strip()
        if parameter and not declared_parameter(gate.version, parameter):
            return (
                f'The pipeline has no parameter named "{parameter}" to receive the file\'s path. '
                f"Add it in the Editor, or choose another. {waiting} waiting."
            )
        f, writing = self._first_complete(row, ready, state, now)
        if f is None:
            if len(writing) == 1:
                return f"{writing[0].name} is still open in the program writing it, so its run waits."
            return f"{len(writing)} files are still open in the programs writing them, so their runs wait."
        if not store.claim(trigger_id, f):
            return ""  # another server took it; the next look moves on
        options = {
            "triggered_by": TRIGGERED_BY,
            "parameter_overrides": {parameter: f.path} if parameter else {},
            "run_metadata": {
                "file": {
                    **f.describe(),
                    "location": row.get("file_location") or "",
                    "folder": row.get("file_folder") or "",
                },
            },
            "on_finish": lambda status, execution_id: store.finish(trigger_id, f.path, f.version, status, execution_id),
        }
        if not self._scheduler.start_trigger_run(row, **options):
            store.release(trigger_id, f.path, f.version)
            return f"Could not start a run now, as the server is at its run limit. {waiting} waiting."
        more = len(ready) - 1
        return f"Started a run for {f.name}." + (f" {_plural(more, 'more file')} waiting." if more else "")

    def _first_complete(
        self, row: dict, ready: list[FoundFile], state: dict, now: datetime,
    ) -> tuple[FoundFile | None, list[FoundFile]]:
        """The first ready file that no program still has open for writing,
        and the files passed over because one does. Those wait another settle
        time before they are tried again. Only a Windows file share is asked
        (see ``file_listing.still_being_written``)."""
        seen = self._seen.setdefault(row["id"], {})
        writing: list[FoundFile] = []
        for f in ready:
            try:
                busy = self._writing(
                    row.get("file_location") or "",
                    f.path,
                    app_state=state,
                    workspace_id=row.get("workspace_id") or "default",
                    connection_id=row.get("file_connection_id") or "",
                )
            except Exception:  # noqa: BLE001 - the run reads the file and says what is wrong
                logger.debug("File trigger %s could not check whether %s is complete", row["id"], f.path, exc_info=True)
                busy = False
            if not busy:
                return f, writing
            writing.append(f)
            seen[f.path] = _Seen(version=f.version, since=now)
        return None, writing

    def _sweep(self, store: Any, now: datetime) -> None:
        if self._swept_at is not None and now - self._swept_at < SWEEP_EVERY:
            return
        self._swept_at = now
        try:
            store.forget_deleted_triggers()
        except Exception:  # noqa: BLE001
            logger.debug("Could not clear what deleted triggers left", exc_info=True)


def preview_folder(app_state: dict, workspace_id: str, settings: dict, limit: int = 50) -> dict[str, Any]:
    """What a trigger with these settings would see right now, for the screen's
    Check folder button. Starts nothing and writes nothing."""
    location = (settings.get("file_location") or "").strip().lower()
    folder = settings.get("file_folder") or ""
    pattern = settings.get("file_pattern") or "*"
    listed = list_folder(
        location, folder, app_state=app_state, workspace_id=workspace_id,
        connection_id=settings.get("file_connection_id") or "",
    )
    matching = [f for f in listed if name_matches(f.name, pattern)]
    oldest = datetime.min.replace(tzinfo=timezone.utc)
    matching.sort(key=lambda f: (f.modified or oldest, f.name), reverse=True)
    return {
        "total": len(listed),
        "matching": len(matching),
        "files": [f.describe() for f in matching[:limit]],
    }
