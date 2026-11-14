"""SQLite-backed workflow store with versioning.

Every save that changes a pipeline adds a version, marked with the kind of
save that made it: 'saved' (Save, Import, a rename...), 'auto' (auto-save and
the save before a run), 'restored' (an older version saved again) or 'review'
(submitted, approved, rejected, deployed).

A published pipeline has one version that is live. Schedules, endpoints and
every other unattended run read it through ``live_version``. Saving adds a newer
version beside it, a draft, and nothing that runs on its own changes until
someone publishes again. ``save`` is the one place a pipeline is written, so
that is where this is enforced.

Version retention, after every save (see ``_prune_versions``): the current
version, named versions, published versions, the one deployed to PROD and
review steps are always kept. Of the rest, the newest ``VERSION_RETENTION_COUNT``
saves and the newest ``AUTOSAVE_RETENTION_COUNT`` auto-saves are kept, and the
pipeline's removed count goes up by however many go.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from copy import deepcopy

from revoluzen_flow import runtime_config
from .schema import Workflow, WorkflowVersion, PipelineStatus
from .step_secrets import fill_stored_secrets, open_parameters_for_hash, open_steps_for_hash, seal_workflow

logger = logging.getLogger(__name__)


def compute_workflow_hash(workflow: Workflow) -> str:
    """SHA-256 hex digest of a workflow's structural content.

    Memory-cheap: dumps the model with ``sort_keys=True`` so the bytes
    fed into the hasher are deterministic regardless of dict order, then
    releases the intermediate string as soon as ``hashlib`` has consumed
    it. No long-lived buffers.

    Intentionally excludes *mutable* bookkeeping fields that change on
    every save even when the pipeline itself is unchanged:
      - ``updated_at`` — stamped on every write
      - ``deployed_at`` / ``deployed_by`` / ``deployed_version``
      - ``published_at`` / ``published_by``
      - ``rollback_from``
      - ``test_results``

    What remains is the *actual pipeline definition* — steps, connections,
    configs, schedule, connections, etc. Two saves that only flip the
    deployed_version pointer will produce the same hash, which is the
    correct semantics for "did the content change".
    """
    # model_dump gets us a plain dict without datetime-in-JSON pain.
    d = workflow.model_dump(mode="json")
    # Strip bookkeeping / lifecycle fields. None of these describe "what
    # the pipeline does" — they track *state* and change via update_status
    # without re-running save(). If we hashed them we'd get false-positive
    # mismatches on every rollback of a pipeline that's been published or
    # re-deployed since its last structural save.
    for k in (
        "status",
        "updated_at", "deployed_at", "deployed_by", "deployed_version",
        "published_at", "published_by", "rollback_from", "test_results",
        "published_version", "has_unpublished_changes",
    ):
        d.pop(k, None)
    # Over the value of a secret typed into a step, never its encrypted form:
    # encrypting gives different bytes each time, and every version saved
    # before step secrets were encrypted has to go on matching its hash.
    open_steps_for_hash(d.get("steps"))
    open_parameters_for_hash(d.get("parameters"))
    # sort_keys=True makes the byte stream deterministic. default=str
    # catches any stray datetime the model_dump didn't serialize.
    payload = json.dumps(d, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


VERSION_KINDS = ("saved", "auto", "restored", "review")


def version_kind(value: str | None) -> str:
    """One of VERSION_KINDS; anything else counts as an ordinary save."""
    kind = (value or "").strip().lower()
    return kind if kind in VERSION_KINDS else "saved"


# What a pipeline does, as opposed to what it is called and where it is kept.
# The run settings in metadata["execution_settings"] count too.
_DEFINITION_FIELDS = ("steps", "connections", "parameters", "retry_policy")


def definition_hash(workflow: Workflow) -> str:
    """SHA-256 of what a pipeline *does*: its steps, how they connect, its
    parameters and its run settings.

    Narrower than :func:`compute_workflow_hash`, which covers the whole record.
    Renaming a pipeline, moving it, editing its README, switching it off, an
    approval note and dragging a node somewhere tidier all leave this as it
    was, so none of them count as changes waiting to be published.
    """
    d = workflow.model_dump(mode="json")
    steps = d.get("steps") or []
    # Over the value of a secret, never its encrypted form, for the same
    # reason as compute_workflow_hash.
    open_steps_for_hash(steps)
    open_parameters_for_hash(d.get("parameters"))
    payload = {
        "steps": sorted(
            ({k: v for k, v in s.items() if k not in ("position", "proposed_by")} for s in steps),
            key=lambda s: str(s.get("id")),
        ),
        "connections": sorted(
            d.get("connections") or [],
            key=lambda c: json.dumps(c, sort_keys=True, default=str),
        ),
        "parameters": d.get("parameters") or [],
        "retry_policy": d.get("retry_policy"),
        "execution_settings": (d.get("metadata") or {}).get("execution_settings") or {},
    }
    text = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def published_pin(current: WorkflowVersion) -> int | None:
    """The live version number of a pipeline, given its newest version, or
    None when it is not published.

    A pipeline published before ``published_version`` existed has none. Until
    then the newest version was what ran, so it is still the live one.
    """
    wf = current.workflow
    if wf.published_version is not None:
        return wf.published_version
    return current.version if wf.status == PipelineStatus.PUBLISHED else None


class WorkflowStore:
    """Versioned workflow store backed by SQLite."""

    def __init__(self, db=None):
        self._db = db

    def set_db(self, db):
        self._db = db

    def save(
        self,
        workflow: Workflow,
        change_summary: str = "",
        created_by: str = "user",
        kind: str = "saved",
    ) -> WorkflowVersion:
        """Save a new version of a workflow.

        On the **first** save of a workflow (version 1) — whether from
        the create endpoint, an import, a duplicate, an autosave on a
        brand-new draft, or any programmatic path — the name is
        auto-suffixed if another workflow in the same workspace
        already uses it. Subsequent saves (updates) don't rewrite the
        name; once a workflow has been saved, its name is stable
        unless the caller explicitly changes it.
        """
        # Get current max version
        row = self._db.fetchone(
            "SELECT MAX(version) as max_v FROM workflow_versions WHERE workflow_id = ?",
            (workflow.id,),
        )
        version_num = (row["max_v"] or 0) + 1 if row else 1

        # ── Store-layer name rules (May 18 2026, supersedes May 6) ─────
        # On the FIRST save of a new workflow only — the single chokepoint
        # every create path goes through (POST /workflows, POST
        # /templates/import, agent apply_pipeline_draft, autosave-on-new,
        # any direct programmatic save).
        #
        # Rule 1 (locked 2026-05-09): reject placeholder names so the
        # workflows list never silently collects anonymous rows. The
        # frontend's `requireNamedWorkflow` helper enforces this client-
        # side, but defense-in-depth here catches every non-UI path
        # (agent apply_pipeline_draft, CLI imports, future integrations).
        # Without this guard, the backend used to auto-suffix "Untitled
        # Pipeline" → "Untitled Pipeline (2)" etc. and produce the "five
        # Untitled rows" pattern the user keeps catching.
        #
        # Rule 2: dedupe real names by auto-suffixing — unchanged.
        if version_num == 1:
            raw_name = (getattr(workflow, "name", None) or "").strip()
            if not raw_name or raw_name.lower() == "untitled pipeline":
                raise ValueError(
                    "Pipeline name is required and cannot be the placeholder "
                    "'Untitled Pipeline'. Give it a descriptive name before saving."
                )
            try:
                from revoluzen_flow.common.unique_name import ensure_unique_name
                ws_id = workflow.workspace_id or "default"
                existing_names = {
                    w.get("name", "") for w in self.list_all(workspace_id=ws_id)
                }
                workflow.name = ensure_unique_name(raw_name, existing_names)
            except ValueError:
                raise
            except Exception:  # noqa: BLE001 — never fail a save on the dedupe helper
                pass

        workflow.updated_at = datetime.now(timezone.utc)
        # Every write of a pipeline comes through here, so this is where a
        # secret typed into a step is encrypted and a kept one is restored.
        # Done on the caller's object: whatever it does next, such as run the
        # pipeline, must not hold the value as typed.
        fill_stored_secrets(workflow, self, workflow.workspace_id or "default")
        seal_workflow(workflow)
        self._carry_publication(workflow, version_num)
        version = WorkflowVersion(
            version=version_num,
            workflow=deepcopy(workflow),
            created_by=created_by,
            change_summary=change_summary,
        )

        data = version.model_dump(mode="json")
        # 2026-05-22: migrate legacy step types in the serialised blob
        # too. Belt-and-braces with the read-side migrate in get() —
        # this one ensures the on-disk row never grows new legacy
        # types from API callers that bypass the route handlers
        # (templates import, agent apply_pipeline_draft, programmatic
        # saves). Mutates in place; safe because ``data`` is a fresh
        # dict from model_dump().
        from revoluzen_flow.ir.migrations import migrate_legacy_node_types
        wf_blob = data.get("workflow")
        if isinstance(wf_blob, dict):
            migrate_legacy_node_types(wf_blob)
        # workspace_id denormalised into the row so list_all can filter
        # on an index. The Workflow model carries it through the JSON
        # blob too, so either source is a single point of truth.
        ws_id = workflow.workspace_id or "default"
        # Content hash (v15+) — SHA-256 over the structural fields only.
        # Cheap compute, stored so rollback can detect tamper / corruption
        # and admins can prove "this deployed version exactly matches the
        # one that was approved on date X."
        content_hash = compute_workflow_hash(workflow)
        self._db.execute(
            "INSERT OR REPLACE INTO workflow_versions (workflow_id, version, data, created_by, change_summary, created_at, workspace_id, content_hash, kind) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (workflow.id, version_num, json.dumps(data, default=str), created_by, change_summary, version.created_at.isoformat(), ws_id, content_hash, version_kind(kind)),
        )
        self._db.commit()

        # Prune old versions beyond retention limit
        self._prune_versions(workflow.id)

        # 2026-05-23 (Y12 + Z25): invalidate the storage AND connection
        # lineage caches so both surfaces reflect the new pipeline
        # references immediately on the operator's next render.
        # Best-effort — never block a successful workflow save on cache
        # cleanup. Credential cache is keyed on the connection store
        # (not pipelines), so workflow saves don't need to invalidate it.
        try:
            from revoluzen_flow.datastore.usage import (
                invalidate as _invalidate_usage,
                invalidate_connection_usage as _invalidate_conn_usage,
            )
            _invalidate_usage(ws_id)
            _invalidate_conn_usage(ws_id)
        except Exception:  # noqa: BLE001
            pass

        return version

    def _carry_publication(self, workflow: Workflow, version_num: int) -> None:
        """Keep a save from changing what is live.

        Every write of a pipeline comes through ``save``, including writers
        that build the pipeline afresh and know nothing of its lifecycle, such
        as a Copilot change. So this is where a published pipeline stays
        published, at the version it was published at. Only Publish, Deploy,
        Rollback and Unpublish (``update_status``) move that.
        """
        previous = self.get(workflow.id) if version_num > 1 else None
        pin = published_pin(previous) if previous is not None else None
        if pin is None:
            workflow.published_version = None
            workflow.has_unpublished_changes = False
            return
        workflow.status = previous.workflow.status
        workflow.published_at = previous.workflow.published_at
        workflow.published_by = previous.workflow.published_by
        workflow.published_version = pin
        frozen = previous if pin == previous.version else self.get(workflow.id, pin)
        workflow.has_unpublished_changes = (
            frozen is None or definition_hash(workflow) != definition_hash(frozen.workflow)
        )

    def stored_versions_newest_first(self, workflow_id: str, workspace_id: str):
        """Yield ``{"steps": {id: settings}, "parameters": {name: default}}``
        for each stored version of a pipeline in one workspace, newest first.
        The newest nearly always answers, so older versions are read only when
        the caller keeps going."""
        sql = (
            "SELECT data FROM workflow_versions WHERE workflow_id = ? AND workspace_id = ? "
            "ORDER BY version DESC LIMIT ? OFFSET ?"
        )
        offset = 0
        page = 1
        while True:
            rows = self._db.fetchall(sql, (workflow_id, workspace_id, page, offset))
            if not rows:
                return
            for row in rows:
                stored = json.loads(row["data"]).get("workflow") or {}
                yield {
                    "steps": {s.get("id"): s.get("params") for s in stored.get("steps") or [] if isinstance(s, dict)},
                    "parameters": {
                        p.get("name"): p.get("default") for p in stored.get("parameters") or [] if isinstance(p, dict)
                    },
                }
            offset += len(rows)
            page = 50

    def _version_rows(self, workflow_id: str) -> list[dict]:
        """Every version of a pipeline, oldest first, without the full IR:
        number, kind, name, author, note, time, fingerprint, and the
        lifecycle fields the version history and the keeping rules read.

        ``published`` marks the version a /publish was made on: publishing
        stamps ``published_at`` on the newest row in place, and later saves
        carry that value forward, so the first row with a new value is the
        one that was published.
        """
        rows = self._db.fetchall(
            "SELECT version, kind, version_name, created_by, change_summary, created_at, content_hash, "
            "json_extract(data, '$.workflow.published_at') AS published_at, "
            "json_extract(data, '$.workflow.deployed_version') AS deployed_version, "
            "json_extract(data, '$.workflow.published_version') AS published_version, "
            "json_extract(data, '$.workflow.status') AS status, "
            "json_array_length(json_extract(data, '$.workflow.steps')) AS step_count "
            "FROM workflow_versions WHERE workflow_id = ? ORDER BY version ASC",
            (workflow_id,),
        )
        out: list[dict] = []
        previous_published = None
        for r in rows:
            published_at = r["published_at"]
            out.append({
                "version": r["version"],
                "kind": version_kind(r["kind"]),
                "name": r["version_name"] or "",
                "created_by": r["created_by"] or "user",
                "change_summary": r["change_summary"] or "",
                "created_at": r["created_at"] or "",
                "content_hash": r["content_hash"] or "",
                "step_count": r["step_count"] or 0,
                "published": bool(published_at) and published_at != previous_published,
                "published_at": published_at if (published_at and published_at != previous_published) else None,
                "deployed_version": r["deployed_version"],
                "published_version": r["published_version"],
                "status": r["status"] or "draft",
            })
            previous_published = published_at
        return out

    def _prune_versions(self, workflow_id: str) -> int:
        """Remove the versions the keeping rules no longer need.

        Always kept while the pipeline exists:
          - the current (newest) version
          - named versions
          - published versions, the live one, and the one deployed to PROD
          - review steps (submitted, approved, rejected, deployed)
        Of the rest, the newest ``VERSION_RETENTION_COUNT`` saves and the
        newest ``AUTOSAVE_RETENTION_COUNT`` auto-saves are kept; a limit of 0
        or less keeps every version of that kind. Whatever goes is added to
        the pipeline's removed count, which the version history shows.

        Returns the number of versions removed.
        """
        keep_saved = runtime_config.VERSION_RETENTION_COUNT
        keep_auto = runtime_config.AUTOSAVE_RETENTION_COUNT
        rows = self._version_rows(workflow_id)
        if not rows:
            return 0
        newest = rows[-1]
        keep: set[int] = {newest["version"]}
        deployed_v = newest.get("deployed_version")
        if isinstance(deployed_v, int):
            keep.add(deployed_v)
        if isinstance(newest.get("published_version"), int):
            keep.add(newest["published_version"])
        saved_seen = auto_seen = 0
        for r in reversed(rows):  # newest first
            v = r["version"]
            if r["name"] or r["published"] or r["kind"] == "review":
                keep.add(v)
            elif r["kind"] == "auto":
                auto_seen += 1
                if keep_auto <= 0 or auto_seen <= keep_auto:
                    keep.add(v)
            else:
                saved_seen += 1
                if keep_saved <= 0 or saved_seen <= keep_saved:
                    keep.add(v)

        to_delete = [r["version"] for r in rows if r["version"] not in keep]
        if not to_delete:
            return 0

        placeholders = ",".join("?" for _ in to_delete)
        self._db.execute(
            f"DELETE FROM workflow_versions WHERE workflow_id = ? AND version IN ({placeholders})",
            (workflow_id, *to_delete),
        )
        try:
            self._db.execute(
                "INSERT INTO workflow_version_removals (workflow_id, removed, last_removed_at) "
                "VALUES (?, ?, ?) ON CONFLICT(workflow_id) DO UPDATE SET "
                "removed = removed + excluded.removed, last_removed_at = excluded.last_removed_at",
                (workflow_id, len(to_delete), datetime.now(timezone.utc).isoformat()),
            )
        except Exception:  # noqa: BLE001 — the count is a note, never block a save on it
            logger.warning("version retention: could not record removals for %s", workflow_id, exc_info=True)
        self._db.commit()
        logger.info(
            "version retention: removed %d version(s) of workflow %s (keeping %d saves, %d auto-saves, deployed=%s)",
            len(to_delete), workflow_id, keep_saved, keep_auto, deployed_v,
        )
        return len(to_delete)

    def removed_count(self, workflow_id: str) -> int:
        """How many versions of this pipeline the keeping rules have removed."""
        try:
            row = self._db.fetchone(
                "SELECT removed FROM workflow_version_removals WHERE workflow_id = ?",
                (workflow_id,),
            )
        except Exception:  # noqa: BLE001 — a database from before v34
            return 0
        return int(row["removed"] or 0) if row else 0

    def set_version_name(self, workflow_id: str, version: int, name: str) -> bool:
        """Name a version, or clear its name with ''. Named versions are never
        removed by the keeping rules. Returns False when there is no such
        version. The name is not part of the pipeline, so its fingerprint
        does not change."""
        cursor = self._db.execute(
            "UPDATE workflow_versions SET version_name = ? WHERE workflow_id = ? AND version = ?",
            ((name or "").strip(), workflow_id, version),
        )
        self._db.commit()
        return cursor.rowcount > 0

    def get(
        self,
        workflow_id: str,
        version: int | None = None,
        workspace_id: str | None = None,
    ) -> WorkflowVersion | None:
        """Get a specific version (or latest) of a workflow.

        When ``workspace_id`` is provided, the row is only returned if
        it belongs to that workspace. A caller that passes the wrong
        workspace gets None back, which the API layer should translate
        to 404 (NOT 403) so the existence of the workflow doesn't leak
        across tenant boundaries.
        """
        if version is None:
            if workspace_id is None:
                row = self._db.fetchone(
                    "SELECT data FROM workflow_versions WHERE workflow_id = ? ORDER BY version DESC LIMIT 1",
                    (workflow_id,),
                )
            else:
                row = self._db.fetchone(
                    "SELECT data FROM workflow_versions WHERE workflow_id = ? AND workspace_id = ? ORDER BY version DESC LIMIT 1",
                    (workflow_id, workspace_id),
                )
        else:
            if workspace_id is None:
                row = self._db.fetchone(
                    "SELECT data FROM workflow_versions WHERE workflow_id = ? AND version = ?",
                    (workflow_id, version),
                )
            else:
                row = self._db.fetchone(
                    "SELECT data FROM workflow_versions WHERE workflow_id = ? AND version = ? AND workspace_id = ?",
                    (workflow_id, version, workspace_id),
                )
        if row is None:
            return None
        data = json.loads(row["data"])
        # 2026-05-22: rewrite legacy step types (csv_source / db_sink /
        # webhook_trigger / etc.) into the modern generic shape before
        # the Pydantic model reconstructs them. Keeps old saved
        # workflows runnable without forcing a re-save. See
        # ``revoluzen_flow.ir.migrations.migrate_legacy_node_types`` for the
        # full remap policy + per-step warning log.
        from revoluzen_flow.ir.migrations import migrate_legacy_node_types
        wf_blob = data.get("workflow")
        if isinstance(wf_blob, dict):
            migrate_legacy_node_types(wf_blob)
        return WorkflowVersion(**data)

    def get_latest_workflow(self, workflow_id: str) -> Workflow | None:
        """Get the latest workflow object."""
        v = self.get(workflow_id)
        return v.workflow if v else None

    def version_as_pipeline(
        self,
        workflow_id: str,
        version: int,
        workspace_id: str | None = None,
    ) -> WorkflowVersion | None:
        """What a pipeline is with the definition of an earlier version.

        The steps, connections, parameters and run settings are that version's.
        What the pipeline is called, where it is kept, whether it is switched
        on and who owns it are the newest version's, because those belong to
        the pipeline rather than to any one version of it. None when the
        pipeline or the version does not exist.
        """
        latest = self.get(workflow_id, workspace_id=workspace_id)
        if latest is None:
            return None
        if version == latest.version:
            return latest
        frozen = self.get(workflow_id, version, workspace_id=workspace_id)
        if frozen is None:
            return None
        update = {field: getattr(frozen.workflow, field) for field in _DEFINITION_FIELDS}
        metadata = dict(latest.workflow.metadata or {})
        settings = (frozen.workflow.metadata or {}).get("execution_settings")
        if settings is None:
            metadata.pop("execution_settings", None)
        else:
            metadata["execution_settings"] = settings
        update["metadata"] = metadata
        return WorkflowVersion(
            version=version,
            workflow=latest.workflow.model_copy(update=update),
            created_by=frozen.created_by,
            created_at=frozen.created_at,
            change_summary=frozen.change_summary,
        )

    def live_version(
        self,
        workflow_id: str,
        workspace_id: str | None = None,
    ) -> WorkflowVersion | None:
        """The version a run that nobody is watching must use.

        For a published pipeline that is the published version, whatever has
        been saved since. For any other pipeline it is the newest version.
        Returns None when there is no such pipeline. Raises
        :class:`PublishedVersionMissing` rather than quietly running a draft
        if the published version cannot be found.
        """
        latest = self.get(workflow_id, workspace_id=workspace_id)
        if latest is None:
            return None
        pin = published_pin(latest)
        if pin is None or pin == latest.version:
            return latest
        live = self.version_as_pipeline(workflow_id, pin, workspace_id=workspace_id)
        if live is None:
            raise PublishedVersionMissing(latest.workflow.name or workflow_id, pin)
        return live

    def list_all(self, workspace_id: str | None = None) -> list[dict]:
        """List all workflows with latest version info.

        When ``workspace_id`` is provided the result is restricted to
        that workspace — used by the workspace-scoped API router to
        enforce the tenant boundary. When None (legacy path / admin
        tooling), every workflow in the database is returned.
        """
        # Get latest version per workflow, optionally scoped to a
        # single workspace. The filter lives on the OUTER wv row
        # rather than the subquery so a workflow whose old versions
        # predate the workspace_id back-fill still gets counted by
        # the latest (back-filled) row.
        # Pulled in v15: also surface the latest version's content_hash so
        # listings (Admin Deployments tab, dashboards) can display the
        # signed-artifact short prefix without a second round-trip per row.
        # Read the same canonical records as Execution History, never the
        # optional engine logger. Scope both subqueries to the workflow tenant.
        last_run_sql = (
            "(SELECT started_at FROM executions "
            " WHERE workflow_id = wv.workflow_id AND workspace_id = wv.workspace_id "
            " ORDER BY julianday(started_at) DESC, id DESC LIMIT 1) AS last_run, "
            "(SELECT status FROM executions "
            " WHERE workflow_id = wv.workflow_id AND workspace_id = wv.workspace_id "
            " ORDER BY julianday(started_at) DESC, id DESC LIMIT 1) AS last_run_status"
        )

        def _run(select_extra: str) -> list:
            if workspace_id is None:
                return self._db.fetchall(f"""
                    SELECT wv.data, wv.content_hash, {select_extra}
                    FROM workflow_versions wv
                    INNER JOIN (
                        SELECT workflow_id, MAX(version) as max_v
                        FROM workflow_versions GROUP BY workflow_id
                    ) latest ON wv.workflow_id = latest.workflow_id AND wv.version = latest.max_v
                """)
            return self._db.fetchall(
                f"""
                SELECT wv.data, wv.content_hash, {select_extra}
                FROM workflow_versions wv
                INNER JOIN (
                    SELECT workflow_id, MAX(version) as max_v
                    FROM workflow_versions GROUP BY workflow_id
                ) latest ON wv.workflow_id = latest.workflow_id AND wv.version = latest.max_v
                WHERE wv.workspace_id = ?
                """,
                (workspace_id,),
            )

        rows = _run(last_run_sql)
        result = []
        for row in rows:
            data = json.loads(row["data"])
            wf = data.get("workflow", {})
            result.append({
                "id": wf.get("id", ""),
                "name": wf.get("name", ""),
                "description": wf.get("description", ""),
                # Documentation fields (self-documenting pipelines). Surfaced
                # here so listing consumers — the inventory report's "what &
                # why", the Pipelines page — get them without a per-row fetch.
                # `tags` was already read by the report but never emitted here,
                # so it was silently empty; fixed alongside.
                "business_purpose": wf.get("business_purpose", "") or "",
                "readme": wf.get("readme", "") or "",
                "tags": wf.get("tags", []) or [],
                "project_id": wf.get("project_id", "default"),
                "folder_id": wf.get("folder_id"),
                "version": data.get("version", 1),
                "step_count": len(wf.get("steps", [])),
                "status": wf.get("status", "draft"),
                "updated_at": wf.get("updated_at", ""),
                "created_at": wf.get("created_at", ""),
                "deployed_version": wf.get("deployed_version"),
                "deployed_at": wf.get("deployed_at"),
                "deployed_by": wf.get("deployed_by"),
                "rollback_from": wf.get("rollback_from"),
                "published_at": wf.get("published_at"),
                "published_by": wf.get("published_by"),
                # The live version (the newest one on a pipeline published
                # before the field existed), and whether a draft sits beside it.
                "published_version": wf.get("published_version") or (
                    data.get("version", 1) if wf.get("status") == PipelineStatus.PUBLISHED.value else None
                ),
                "has_unpublished_changes": bool(wf.get("has_unpublished_changes")),
                # 2026-05-28 — owner_id / owner_name expose the
                # workflow's creator so the Pipelines page Author
                # column has data. Both fields already live on the
                # workflow schema (see ir/schema.py:277-278); they
                # were just dropped on the way out of list_all.
                "owner_id": wf.get("owner_id", ""),
                "owner_name": wf.get("owner_name", ""),
                # 2026-05-28 — Paused-pipeline badge on the Pipelines
                # page reads these. Defaults TRUE (matches the
                # behaviour in schema.py:313-314 — a workflow is
                # active by default in both environments).
                "is_active_dev": wf.get("is_active_dev", True),
                "is_active_prod": wf.get("is_active_prod", True),
                # Latest version's hash. Empty string on legacy rows
                # (pre-v15) where no hash was stored at save time.
                "content_hash": row["content_hash"] or "",
                # 2026-05-28 — last_run + last_run_status from the JOIN.
                # NULL on a workflow that's never been run; the frontend
                # TimeAgo component renders that as "—" which is correct
                # ("no runs yet"), distinct from the previous "always —
                # because the field never made it across the wire".
                "last_run": row["last_run"] or "",
                "last_run_status": row["last_run_status"] or "",
                # Surface metadata so the Pipelines listing can render the
                # priority chip without fetching each workflow individually.
                # Kept as the full dict (small — typically a handful of
                # keys) rather than just metadata.priority so future
                # metadata-aware UI surfaces don't need another schema
                # change. (May 11 2026.)
                "metadata": wf.get("metadata") or {},
            })
        return result

    def list_all_full(self, workspace_id: str | None = None) -> list[dict]:
        """List all workflows with their **full** workflow JSON, not just
        the listing-summary shape that ``list_all()`` returns.

        Z43 (2026-05-23) — the lineage scanners
        (``compute_workspace_usage`` for Storage USED BY, and
        ``compute_connection_usage`` for Connections USED BY) need the
        full ``steps`` array of every workflow so they can find
        connection_id / local_table_source / local_table_sink references.
        ``list_all()`` strips that out (returns ``step_count`` instead)
        so both scanners had been silently returning empty results.

        Returns a list of dicts shaped like::

            { "id": ..., "name": ..., "workspace_id": ..., "project_id": ...,
              "metadata": {...}, "status": ..., "version": ...,
              "steps": [ {id, type, params, ...}, ... ],
              "connections": [ ... ],
              "parameters": [ ... ] }

        i.e. the same ``data["workflow"]`` blob the SQL row stores, with
        the wrapper-level ``version`` and ``workspace_id`` flattened in
        so the caller doesn't need to dig through the wrapper. The
        version reflects whichever max_v row was selected — same as
        list_all().
        """
        if workspace_id is None:
            rows = self._db.fetchall("""
                SELECT wv.data, wv.workspace_id, wv.version FROM workflow_versions wv
                INNER JOIN (
                    SELECT workflow_id, MAX(version) as max_v
                    FROM workflow_versions GROUP BY workflow_id
                ) latest ON wv.workflow_id = latest.workflow_id AND wv.version = latest.max_v
            """)
        else:
            rows = self._db.fetchall("""
                SELECT wv.data, wv.workspace_id, wv.version FROM workflow_versions wv
                INNER JOIN (
                    SELECT workflow_id, MAX(version) as max_v
                    FROM workflow_versions GROUP BY workflow_id
                ) latest ON wv.workflow_id = latest.workflow_id AND wv.version = latest.max_v
                WHERE wv.workspace_id = ?
                """,
                (workspace_id,),
            )
        out: list[dict] = []
        for row in rows:
            try:
                data = json.loads(row["data"])
            except (TypeError, ValueError):
                # Corrupt row — skip rather than blow up the whole scan.
                continue
            wf = data.get("workflow") or {}
            if not isinstance(wf, dict):
                continue
            # Flatten wrapper metadata into the workflow dict so callers
            # have one consistent shape. Don't clobber a workflow that
            # already carries these fields (defensive — the IR layer
            # should always wrap, but tests pass bare dicts).
            wf.setdefault("workspace_id", row["workspace_id"])
            wf.setdefault("version", row["version"])
            out.append(wf)
        return out

    def get_versions(self, workflow_id: str) -> list[dict]:
        """List all versions of a workflow, oldest first: number, author,
        time, note, step count and fingerprint (content_hash, empty on rows
        from before v15), plus since v34 the kind of save, the name, whether
        it was the published version, whether it is the one deployed to PROD,
        and whether it is the live one."""
        rows = self._version_rows(workflow_id)
        if not rows:
            return []
        deployed_v = rows[-1].get("deployed_version")
        live_v = rows[-1].get("published_version")
        if live_v is None and rows[-1].get("status") == PipelineStatus.PUBLISHED.value:
            live_v = rows[-1]["version"]
        result = []
        for r in rows:
            result.append({
                "version": r["version"],
                "created_by": r["created_by"],
                "created_at": r["created_at"],
                "change_summary": r["change_summary"],
                "step_count": r["step_count"],
                "content_hash": r["content_hash"],
                "kind": r["kind"],
                "name": r["name"],
                "published": r["published"],
                "published_at": r["published_at"],
                "deployed": isinstance(deployed_v, int) and r["version"] == deployed_v,
                "live": isinstance(live_v, int) and r["version"] == live_v,
            })
        return result

    def verify_version_hash(
        self,
        workflow_id: str,
        version: int,
        workspace_id: str | None = None,
    ) -> tuple[bool, str, str]:
        """Re-compute the hash of a stored version and compare to the
        stored ``content_hash`` column.

        Returns ``(match, stored_hash, recomputed_hash)``.

        Semantics:
          * stored_hash == ""        → legacy row (pre-v15); caller should
            treat as "no verification possible" and proceed (returns
            ``(True, "", recomputed_hash)`` so the match flag is forgiving).
          * stored_hash == recomputed → OK, content is intact.
          * stored_hash != recomputed → TAMPER/CORRUPTION detected.
            Caller (e.g. rollback endpoint) should refuse to proceed.

        Memory: one row read, one hash computed, both released on return.
        """
        row = self._db.fetchone(
            "SELECT data, content_hash FROM workflow_versions WHERE workflow_id = ? AND version = ?"
            + (" AND workspace_id = ?" if workspace_id else ""),
            (workflow_id, version, workspace_id) if workspace_id else (workflow_id, version),
        )
        if row is None:
            return (False, "", "")
        stored = row["content_hash"] or ""
        data = json.loads(row["data"])
        wv = WorkflowVersion(**data)
        recomputed = compute_workflow_hash(wv.workflow)
        if stored == "":
            # Legacy row — can't verify. Forgive so rollback of pre-v15
            # versions isn't blocked; a warning is logged by the caller.
            return (True, "", recomputed)
        return (stored == recomputed, stored, recomputed)

    def diff(self, workflow_id: str, v1: int, v2: int) -> dict | None:
        """Compute a simple diff between two versions."""
        ver1 = self.get(workflow_id, v1)
        ver2 = self.get(workflow_id, v2)
        if not ver1 or not ver2:
            return None

        w1 = ver1.workflow
        w2 = ver2.workflow

        steps1 = {s.id: s for s in w1.steps}
        steps2 = {s.id: s for s in w2.steps}

        added = [s.id for s in w2.steps if s.id not in steps1]
        removed = [sid for sid in steps1 if sid not in steps2]
        modified = []
        for sid in steps1:
            if sid in steps2:
                if steps1[sid].model_dump() != steps2[sid].model_dump():
                    modified.append(sid)

        return {
            "from_version": v1,
            "to_version": v2,
            "added_steps": added,
            "removed_steps": removed,
            "modified_steps": modified,
            "connections_changed": (
                [c.model_dump() for c in w1.connections] != [c.model_dump() for c in w2.connections]
            ),
        }

    def update_status(
        self,
        workflow_id: str,
        status: PipelineStatus,
        test_results: dict | None = None,
        published_by: str | None = None,
        deployed_version: int | None = None,
        rollback_from: int | None = None,
        restoring: bool = False,
    ) -> WorkflowVersion | None:
        """Update the status of the latest workflow version in-place.

        Publishing makes the newest version live, or ``deployed_version`` when
        it is given. ``restoring`` is for a test putting back the status it
        found: a published pipeline that was only tested has not been published
        again, and the changes being tested must not go live.
        """
        latest = self.get(workflow_id)
        if not latest:
            return None
        pin_before = published_pin(latest)
        latest.workflow.status = status
        latest.workflow.updated_at = datetime.now(timezone.utc)
        if test_results is not None:
            latest.workflow.test_results = test_results
        if status == PipelineStatus.PUBLISHED and not restoring:
            latest.workflow.published_at = datetime.now(timezone.utc)
            if published_by:
                latest.workflow.published_by = published_by
            pin = deployed_version if deployed_version is not None else latest.version
            latest.workflow.published_version = pin
            frozen = latest if pin == latest.version else self.get(workflow_id, pin)
            latest.workflow.has_unpublished_changes = (
                frozen is None or definition_hash(latest.workflow) != definition_hash(frozen.workflow)
            )
        elif status == PipelineStatus.TESTING and pin_before is not None:
            # While it is being tested the status says "testing", so the
            # live version has to be written down.
            latest.workflow.published_version = pin_before
        elif status in (PipelineStatus.DRAFT, PipelineStatus.ARCHIVED):
            latest.workflow.published_version = None
            latest.workflow.has_unpublished_changes = False
        # Deployment tracking
        if deployed_version is not None:
            latest.workflow.deployed_version = deployed_version
            latest.workflow.deployed_at = datetime.now(timezone.utc)
            latest.workflow.deployed_by = published_by
        if rollback_from is not None:
            latest.workflow.rollback_from = rollback_from

        # Update in place
        data = latest.model_dump(mode="json")
        self._db.execute(
            "UPDATE workflow_versions SET data = ? WHERE workflow_id = ? AND version = ?",
            (json.dumps(data, default=str), workflow_id, latest.version),
        )
        self._db.commit()
        return latest

    def delete(self, workflow_id: str) -> bool:
        """Delete a workflow and all its versions, permanently.

        Nothing keeps a copy. The API's normal delete does not call this: it
        moves the pipeline to the Bin with :meth:`trash`, and this is what
        ``DELETE /api/workflows/{id}?permanent=true`` and internal callers use.
        """
        cursor = self._db.execute(
            "DELETE FROM workflow_versions WHERE workflow_id = ?",
            (workflow_id,),
        )
        try:
            self._db.execute("DELETE FROM workflow_version_removals WHERE workflow_id = ?", (workflow_id,))
        except Exception:  # noqa: BLE001 — a database from before v34
            pass
        self._db.commit()
        return cursor.rowcount > 0

    # ── The Bin: deleted pipelines that can still be restored ───────────
    #
    # A deleted pipeline's version rows move from workflow_versions into
    # workflow_trash (same columns, same primary key), with one
    # workflow_trash_meta row beside them for the Bin list. Nothing that
    # reads workflow_versions has to know the Bin exists: a binned pipeline
    # is simply absent, exactly as it was when delete() dropped the rows.
    # Schedules, the step cache, CDC slots and the audit trail are handled
    # one layer up, in revoluzen_flow/ir/workflow_bin.py.

    _VERSION_COLUMNS = (
        "workflow_id, version, data, created_by, change_summary, "
        "created_at, workspace_id, content_hash, kind, version_name"
    )
    # "leave the folder as it was" for restore_from_trash(folder_id=...),
    # because None already means "no folder".
    KEEP = object()

    def _invalidate_usage(self, ws_id: str | None) -> None:
        """Drop the Storage / Connections usage caches after pipelines appear
        or disappear. Best-effort, like the same call in save()."""
        try:
            from revoluzen_flow.datastore.usage import (
                invalidate as _invalidate_usage,
                invalidate_connection_usage as _invalidate_conn_usage,
            )
            _invalidate_usage(ws_id)
            _invalidate_conn_usage(ws_id)
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def _trash_entry(row: dict, include_schedules: bool = False) -> dict:
        entry = {
            "id": row["workflow_id"],
            "name": row["name"],
            "workspace_id": row["workspace_id"],
            "project_id": row["project_id"],
            "folder_id": row["folder_id"],
            "status_before": row["status_before"],
            "deleted_at": row["deleted_at"],
            "deleted_by_id": row["deleted_by_id"],
            "deleted_by_name": row["deleted_by_name"],
            "version_count": row["version_count"],
        }
        if include_schedules:
            try:
                schedules = json.loads(row["schedules"] or "[]")
            except (TypeError, ValueError):
                schedules = []
            entry["schedules"] = schedules if isinstance(schedules, list) else []
        return entry

    def trash(
        self,
        workflow_id: str,
        workspace_id: str | None = None,
        deleted_by_id: str = "",
        deleted_by_name: str = "",
        schedules: list[dict] | None = None,
    ) -> dict | None:
        """Move a pipeline and all its versions into the Bin.

        ``schedules`` is a snapshot of the schedules the caller is about to
        remove with the pipeline, kept so a restore can put them back.
        Returns the new Bin entry, or None when there is no such pipeline
        (in that workspace).
        """
        latest = self.get(workflow_id, workspace_id=workspace_id)
        if latest is None:
            return None
        wf = latest.workflow
        ws_id = getattr(wf, "workspace_id", None) or "default"
        status = getattr(wf, "status", None)
        status_before = getattr(status, "value", status) or "draft"
        cols = self._VERSION_COLUMNS
        with self._db.transaction() as tx:
            # One pipeline, one place: never leave a stale Bin copy of the same id.
            tx.execute("DELETE FROM workflow_trash WHERE workflow_id = ?", (workflow_id,))
            tx.execute("DELETE FROM workflow_trash_meta WHERE workflow_id = ?", (workflow_id,))
            moved = tx.execute(
                f"INSERT INTO workflow_trash ({cols}) "
                f"SELECT {cols} FROM workflow_versions WHERE workflow_id = ?",
                (workflow_id,),
            ).rowcount
            tx.execute(
                "INSERT INTO workflow_trash_meta (workflow_id, workspace_id, name, "
                "project_id, folder_id, status_before, deleted_at, deleted_by_id, "
                "deleted_by_name, version_count, schedules) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    workflow_id, ws_id, wf.name or "", wf.project_id or "default",
                    wf.folder_id, str(status_before),
                    datetime.now(timezone.utc).isoformat(),
                    deleted_by_id or "", deleted_by_name or "", moved,
                    json.dumps(schedules or [], default=str),
                ),
            )
            tx.execute("DELETE FROM workflow_versions WHERE workflow_id = ?", (workflow_id,))
        self._invalidate_usage(ws_id)
        return self.get_trash_entry(workflow_id)

    def get_trash_entry(
        self,
        workflow_id: str,
        workspace_id: str | None = None,
        include_schedules: bool = False,
    ) -> dict | None:
        """One Bin entry, or None. Scoped to ``workspace_id`` when given, so
        another workspace's pipeline looks like it does not exist."""
        sql = "SELECT * FROM workflow_trash_meta WHERE workflow_id = ?"
        params: list = [workflow_id]
        if workspace_id is not None:
            sql += " AND workspace_id = ?"
            params.append(workspace_id)
        row = self._db.fetchone(sql, tuple(params))
        return self._trash_entry(row, include_schedules) if row else None

    def list_trash(self, workspace_id: str | None = None) -> list[dict]:
        """The Bin, newest deletion first. Scoped to a workspace when given."""
        sql = "SELECT * FROM workflow_trash_meta"
        params: tuple = ()
        if workspace_id is not None:
            sql += " WHERE workspace_id = ?"
            params = (workspace_id,)
        sql += " ORDER BY deleted_at DESC"
        return [self._trash_entry(r) for r in self._db.fetchall(sql, params)]

    def restore_from_trash(
        self,
        workflow_id: str,
        workspace_id: str | None = None,
        *,
        name: str | None = None,
        project_id: str | None = None,
        folder_id=KEEP,
        restored_by: str = "user",
    ) -> dict | None:
        """Move a binned pipeline back, as a **draft**.

        ``name`` / ``project_id`` / ``folder_id`` let the caller correct what
        no longer fits (a name that is taken, a project that was deleted);
        left out, the pipeline keeps what it had. Returns the Bin entry it
        came from (with its schedule snapshot), or None when it is not in
        the Bin. Raises :class:`WorkflowIdInUse` if a live pipeline already
        has this id.

        The status change is made on the binned copy *before* it moves, so
        even if the version note below failed the pipeline would come back
        as a draft, never as published.
        """
        entry = self.get_trash_entry(workflow_id, workspace_id, include_schedules=True)
        if entry is None:
            return None
        if self._db.fetchone(
            "SELECT 1 AS present FROM workflow_versions WHERE workflow_id = ? LIMIT 1",
            (workflow_id,),
        ):
            raise WorkflowIdInUse(workflow_id)
        latest_row = self._db.fetchone(
            "SELECT version, data FROM workflow_trash WHERE workflow_id = ? "
            "ORDER BY version DESC LIMIT 1",
            (workflow_id,),
        )
        if latest_row is None:
            # A Bin entry with no versions behind it can never be restored.
            self._delete_trash_ids([workflow_id])
            return None
        data = json.loads(latest_row["data"])
        blob = data.get("workflow")
        if not isinstance(blob, dict):
            blob = {}
        blob["status"] = PipelineStatus.DRAFT.value
        blob["published_version"] = None
        blob["has_unpublished_changes"] = False
        if name is not None:
            blob["name"] = name
        if project_id is not None:
            blob["project_id"] = project_id
        if folder_id is not WorkflowStore.KEEP:
            blob["folder_id"] = folder_id
        data["workflow"] = blob
        cols = self._VERSION_COLUMNS
        with self._db.transaction() as tx:
            tx.execute(
                "UPDATE workflow_trash SET data = ? WHERE workflow_id = ? AND version = ?",
                (json.dumps(data, default=str), workflow_id, latest_row["version"]),
            )
            tx.execute(
                f"INSERT INTO workflow_versions ({cols}) "
                f"SELECT {cols} FROM workflow_trash WHERE workflow_id = ?",
                (workflow_id,),
            )
            tx.execute("DELETE FROM workflow_trash WHERE workflow_id = ?", (workflow_id,))
            tx.execute("DELETE FROM workflow_trash_meta WHERE workflow_id = ?", (workflow_id,))
        # A version of its own, so Version history shows the restore.
        restored = self.get(workflow_id)
        if restored is not None:
            try:
                self.save(
                    restored.workflow,
                    change_summary="Restored from the Bin",
                    created_by=restored_by,
                )
            except Exception:  # noqa: BLE001 — it is already back, as a draft
                logger.warning(
                    "restored %s from the Bin but could not add the version note",
                    workflow_id, exc_info=True,
                )
        self._invalidate_usage(entry["workspace_id"])
        return entry

    def _delete_trash_ids(self, ids: list[str]) -> None:
        if not ids:
            return
        with self._db.transaction() as tx:
            for start in range(0, len(ids), 500):
                chunk = ids[start:start + 500]
                marks = ",".join("?" for _ in chunk)
                tx.execute(f"DELETE FROM workflow_trash WHERE workflow_id IN ({marks})", tuple(chunk))
                tx.execute(f"DELETE FROM workflow_trash_meta WHERE workflow_id IN ({marks})", tuple(chunk))
                tx.execute(f"DELETE FROM workflow_version_removals WHERE workflow_id IN ({marks})", tuple(chunk))

    def purge_trash(self, workflow_id: str, workspace_id: str | None = None) -> dict | None:
        """Remove one pipeline from the Bin for good. Returns its entry, or
        None when it was not there (in that workspace)."""
        entry = self.get_trash_entry(workflow_id, workspace_id)
        if entry is None:
            return None
        self._delete_trash_ids([workflow_id])
        return entry

    def purge_trash_all(self, workspace_id: str | None = None) -> list[dict]:
        """Empty the Bin (of one workspace when given). Returns what was removed."""
        entries = self.list_trash(workspace_id)
        self._delete_trash_ids([e["id"] for e in entries])
        return entries

    def purge_trash_older_than(self, cutoff_iso: str) -> list[dict]:
        """Remove every Bin entry deleted before ``cutoff_iso`` (an ISO UTC
        timestamp). Returns what was removed."""
        rows = self._db.fetchall(
            "SELECT * FROM workflow_trash_meta WHERE deleted_at < ? ORDER BY deleted_at",
            (cutoff_iso,),
        )
        entries = [self._trash_entry(r) for r in rows]
        self._delete_trash_ids([e["id"] for e in entries])
        return entries


class WorkflowIdInUse(Exception):
    """A live pipeline already has the id of the one being restored."""


class PublishedVersionMissing(Exception):
    """A pipeline's published version cannot be found. Nothing may run in its
    place: the newest version may be an untested draft."""

    def __init__(self, name: str, version: int):
        self.pipeline_name = name
        self.version = version
        super().__init__(
            f'Version {version} of "{name}", the published one, cannot be found, so it '
            "was not run. Publish the pipeline again."
        )
