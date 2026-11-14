"""Makes the copies in the background.

Publishing must not wait for a Git server, and must not fail because one is
down. So a publish adds a row and returns, one worker thread works through the
rows in order, and a failed copy is tried again later. The Pipelines page shows
how each one went.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from typing import Any

from revoluzen_flow.gitcopy.client import GitFailed, GitRepo, WillNotRetry, commit_page
from revoluzen_flow.gitcopy.files import build_bundle, write_bundle
from revoluzen_flow.gitcopy.settings import GitSettings, read_settings
from revoluzen_flow.gitcopy.store import GitCopyStore

logger = logging.getLogger(__name__)

RETRY_CHECK_SECONDS = 300

_lock = threading.Lock()
_worker: threading.Thread | None = None
_run_again = False
_store_lock = threading.Lock()


def _app_state() -> dict:
    from revoluzen_flow.main import app_state
    return app_state


def queue() -> GitCopyStore:
    """The store for the current database, made the first time it is needed."""
    state = _app_state()
    with _store_lock:
        store = state.get("git_copy_store")
        if store is None or store.db is not state["db"]:
            store = GitCopyStore(state["db"])
            state["git_copy_store"] = store
        return store


def current_settings() -> GitSettings:
    state = _app_state()
    return read_settings(state["db"], state.get("encryptor"))


def working_directory() -> str:
    return os.path.join(_app_state()["data_dir"], "git-copy", "work")


def public_row(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "id": row["id"],
        "workflow_id": row["workflow_id"],
        "version": row["version"],
        "event": row["event"],
        "status": row["status"],
        "commit_sha": row["commit_sha"],
        "commit_url": row["commit_url"],
        "error": row["error"],
        "actor_name": row["actor_name"],
        "note": row["note"],
        "attempts": row["attempts"],
        "will_retry_at": row["next_attempt_at"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def enqueue(
    *,
    workflow_id: str,
    version: int | None,
    workspace_id: str,
    event: str,
    actor_name: str = "",
    actor_email: str = "",
    note: str = "",
) -> dict[str, Any]:
    """Add a copy to make. ``{"status": "off"}`` when Git copy is not set up."""
    if not current_settings().ready:
        return {"status": "off"}
    row = queue().add(
        workflow_id=workflow_id, version=version, workspace_id=workspace_id,
        event=event, actor_name=actor_name, actor_email=actor_email, note=note,
    )
    kick()
    return {"status": "queued", "id": row["id"]}


def kick() -> None:
    """Make sure a worker is going. Only ever one, because they would share a
    working copy."""
    global _worker, _run_again
    with _lock:
        if _worker is not None and _worker.is_alive():
            _run_again = True
            return
        _run_again = False
        _worker = threading.Thread(target=_drain, name="revoluzen_flow-git-copy", daemon=True)
        _worker.start()


def _drain() -> None:
    global _run_again
    try:
        while True:
            row = queue().claim_next()
            if row is None:
                with _lock:
                    if not _run_again:
                        return
                    _run_again = False
                continue
            _make_copy(row)
    except Exception:  # noqa: BLE001 — a worker must end quietly; the retry loop starts another
        logger.exception("Git copy worker stopped")


def _make_copy(row: dict[str, Any]) -> None:
    store = queue()
    try:
        settings = current_settings()
        if not settings.ready:
            raise GitFailed("Copying to Git is switched off, or the access token is missing.")
        state = _app_state()
        workflow_store = state["store"]
        workspace_id = row["workspace_id"]
        view = (
            workflow_store.version_as_pipeline(row["workflow_id"], row["version"], workspace_id=workspace_id)
            if row["version"] is not None
            else workflow_store.get(row["workflow_id"], workspace_id=workspace_id)
        )
        if view is None:
            store.finish(row["id"], "skipped")
            return
        _match, fingerprint, _recomputed = workflow_store.verify_version_hash(
            row["workflow_id"], view.version, workspace_id=workspace_id,
        )
        bundle = build_bundle(
            view,
            root_folder=settings.folder,
            workspace_id=workspace_id,
            event=row["event"],
            actor_name=row["actor_name"],
            actor_email=row["actor_email"],
            note=row["note"],
            fingerprint=fingerprint,
            app_state=state,
        )
        repo = GitRepo(working_directory(), settings)
        sha = repo.save(
            lambda directory: write_bundle(bundle, directory, settings.folder),
            bundle.message,
            bundle.author,
        )
    except WillNotRetry as exc:
        logger.warning("Git copy of %s stopped: %s", row["workflow_id"], exc)
        store.fail(row["id"], str(exc), retry=False)
    except GitFailed as exc:
        logger.warning("Git copy of %s failed: %s", row["workflow_id"], exc)
        store.fail(row["id"], str(exc))
    except Exception:  # noqa: BLE001 — the text of an unexpected error could hold anything
        logger.exception("Git copy of %s failed", row["workflow_id"])
        store.fail(row["id"], "Something went wrong while saving to Git. The server log has the details.")
    else:
        if sha is None:
            store.finish(row["id"], "unchanged")
        else:
            store.finish(row["id"], "saved", commit_sha=sha, commit_url=commit_page(settings.repo_url, sha))


def retry(row_id: str) -> None:
    queue().retry(row_id)
    kick()


def save_all_published(workspace_id: str, actor_name: str = "", actor_email: str = "") -> int:
    """Queue a copy of every published pipeline in a workspace, so a repository
    connected after the fact holds what is already live."""
    store = queue()
    workflow_store = _app_state()["store"]
    count = 0
    for row in workflow_store.list_all(workspace_id=workspace_id):
        if row.get("published_version") is None:
            continue
        store.add(
            workflow_id=row["id"], version=row["published_version"], workspace_id=workspace_id,
            event="snapshot", actor_name=actor_name, actor_email=actor_email,
            note="First copy of what was already published.",
        )
        count += 1
    if count:
        kick()
    return count


def _start_if_due() -> None:
    store = queue()
    store.release_stuck()
    if current_settings().ready and store.has_due():
        kick()


async def retry_loop() -> None:
    """Pick up failed copies when they are due, and any left by a restart."""
    await asyncio.sleep(60)
    while True:
        try:
            await asyncio.to_thread(_start_if_due)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("Git copy retry check failed (will try again): %s", exc)
        await asyncio.sleep(RETRY_CHECK_SECONDS)
