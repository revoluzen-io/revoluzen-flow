"""What goes into the repository for one pipeline, and what must never.

Three files in a folder of their own::

    <folder>/<project>/<pipeline>--<id>/
        pipeline.json    the definition, in the same form as Export, so it can be
                         imported into any Flow to put the pipeline back
        depends-on.json  the connections, credentials, tables, files and
                         pipelines it uses, and its schedules
        README.md        what it is for, who published it, when and why

A Git history cannot be cleaned up afterwards, so what leaves here is checked
twice. Everything is masked on the way in, and then :func:`assert_no_secrets`
refuses the whole copy if anything that looks like a secret is still there.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from revoluzen_flow.connections.inline_secrets import SECRET_PLACEHOLDER, connection_response
from revoluzen_flow.credentials.sensitive_fields import name_says_secret
from revoluzen_flow.gitcopy.client import WillNotRetry
from revoluzen_flow.ir.published_refs import references
from revoluzen_flow.ir.schema import WorkflowVersion
from revoluzen_flow.ir.step_secrets import mask_step_params
from revoluzen_flow.security.encryptor import _ENC_PREFIX

EVENT_TITLES = {
    "published": "Publish",
    "unpublished": "Unpublish",
    "deployed": "Deploy",
    "rolled_back": "Roll back",
    "snapshot": "Copy",
}
_EVENT_STATUS = {
    "published": "Published",
    "unpublished": "Unpublished",
    "deployed": "Published",
    "rolled_back": "Published (rolled back)",
    "snapshot": "Published",
}

# The password in scheme://user:password@host. A reference such as
# ${param.pw} or {{ pw }} is left alone.
_URL_PASSWORD = re.compile(r"(\b[a-zA-Z][a-zA-Z0-9+.\-]*://[^/\s:@'\"]*:)([^/\s@'\"{}$•]+)(@)")
_PRIVATE_KEY = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")


@dataclass
class Bundle:
    """One copy, ready to be written and committed."""

    folder: str
    files: dict[str, str]
    message: str
    author: tuple[str, str]
    pipeline_id: str


def _slug(text: str, fallback: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:60].strip("-")
    return slug or fallback


def _scrub(value: Any) -> Any:
    """A copy with anything encrypted, and any password in an address, masked."""
    if isinstance(value, dict):
        return {key: _scrub(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_scrub(item) for item in value]
    if isinstance(value, str):
        if value.startswith(_ENC_PREFIX):
            return SECRET_PLACEHOLDER
        return _URL_PASSWORD.sub(lambda m: f"{m.group(1)}{SECRET_PLACEHOLDER}{m.group(3)}", value)
    return value


def _mask_by_name(value: Any) -> Any:
    """For connection settings, where masking too much costs nothing: every
    value whose name suggests a secret, unless it is a reference."""
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if isinstance(item, str) and item and name_says_secret(key) and "{{" not in item and "${" not in item:
                out[key] = SECRET_PLACEHOLDER
            else:
                out[key] = _mask_by_name(item)
        return out
    if isinstance(value, list):
        return [_mask_by_name(item) for item in value]
    return value


def assert_no_secrets(files: dict[str, str], message: str) -> None:
    """Refuse the whole copy if a secret is still in it. Stops the copy for
    good rather than retrying, because the same files would fail again."""
    for name, text in {**files, "commit message": message}.items():
        if _ENC_PREFIX in text:
            raise WillNotRetry(f"Stopped: {name} still held an encrypted value, so nothing was sent to Git.")
        if _PRIVATE_KEY.search(text):
            raise WillNotRetry(f"Stopped: {name} held a private key, so nothing was sent to Git.")
        if _URL_PASSWORD.search(text):
            raise WillNotRetry(f"Stopped: {name} held an address with a password, so nothing was sent to Git.")


def pipeline_payload(view: WorkflowVersion, workspace_id: str) -> dict[str, Any]:
    """The definition, in the shape ``POST /workflows/import`` takes."""
    from revoluzen_flow.api.workflows import build_pipeline_export_payload

    pipeline = build_pipeline_export_payload(view.workflow, workspace_id)
    pipeline["metadata"] = mask_step_params(pipeline.get("metadata") or {})
    pipeline["execution_settings"] = mask_step_params(pipeline.get("execution_settings") or {})
    return _scrub({"format_version": 2, "export_type": "pipeline", "pipeline": pipeline})


def _connection_entry(connection_id: str, workspace_id: str, app_state: dict) -> dict[str, Any]:
    store = app_state.get("connection_store")
    connection = store.get(connection_id, workspace_id=workspace_id) if store is not None else None
    if connection is None:
        return {"id": connection_id, "missing": True}
    data = connection_response(connection)
    return {
        "id": connection_id,
        "name": data.get("name") or "",
        "type": data.get("type") or "",
        "settings": _mask_by_name(data.get("config") or {}),
        "credential_id": data.get("credential_id") or None,
    }


def _credential_entry(credential_id: str, workspace_id: str, app_state: dict) -> dict[str, Any]:
    """Its name and kind only. What is inside never leaves Flow."""
    store = app_state.get("credential_store")
    credential = store.get(credential_id, workspace_id=workspace_id) if store is not None else None
    if credential is None:
        return {"id": credential_id, "missing": True}
    return {"id": credential_id, "name": credential.name, "type": credential.type}


def depends_on(view: WorkflowVersion, workspace_id: str, app_state: dict) -> dict[str, Any]:
    """What this pipeline needs in order to run, without any secret."""
    refs = references(view.workflow.model_dump(mode="json"))
    connections = [_connection_entry(c, workspace_id, app_state) for c in sorted(refs.connections)]
    credential_ids = set(refs.credentials) | {
        c["credential_id"] for c in connections if c.get("credential_id")
    }
    pipelines: list[dict[str, Any]] = []
    store = app_state.get("store")
    for pipeline_id in sorted(refs.pipelines):
        called = store.get(pipeline_id, workspace_id=workspace_id) if store is not None else None
        pipelines.append(
            {"id": pipeline_id, "name": called.workflow.name} if called else {"id": pipeline_id, "missing": True}
        )
    schedules: list[dict[str, Any]] = []
    schedule_store = app_state.get("schedule_store")
    if schedule_store is not None:
        keep = ("name", "schedule_type", "cron_expression", "daily_time", "weekly_days", "weekly_time",
                "interval_minutes", "timezone", "enabled")
        # Event triggers are written with their own settings, not the clock's.
        keep_for = {
            "file_arrival": ("name", "schedule_type", "enabled", "file_location", "file_connection_id",
                             "file_folder", "file_pattern", "file_parameter", "file_settle_seconds",
                             "file_poll_seconds"),
            "after_pipeline": ("name", "schedule_type", "enabled", "after_workflow_id", "after_outcome"),
        }
        for schedule in schedule_store.list_by_workflow(view.workflow.id, workspace_id=workspace_id):
            fields = keep_for.get(schedule.get("schedule_type") or "", keep)
            schedules.append({key: schedule[key] for key in fields if schedule.get(key) not in (None, "", [])})
    return _scrub({
        "connections": connections,
        "connection_names": sorted(refs.connection_names),
        "credentials": [_credential_entry(c, workspace_id, app_state) for c in sorted(credential_ids)],
        "tables": [
            {"schema": schema, "name": name, "role": refs.table_roles.get((schema, name), "generic")}
            for schema, name in sorted(refs.tables)
        ],
        "files": sorted(refs.files),
        "pipelines": pipelines,
        "connectors": sorted(refs.connectors),
        "schedules": schedules,
    })


def _cell(text: Any) -> str:
    return str(text if text is not None else "").replace("|", "\\|").replace("\n", " ").strip()


def _readme(
    view: WorkflowVersion,
    *,
    event: str,
    actor: str,
    note: str,
    when: str,
    project: str,
    needs: dict[str, Any],
    fingerprint: str,
) -> str:
    wf = view.workflow
    rows = [
        ("Status", _EVENT_STATUS.get(event, "Published")),
        ("Version", view.version),
        ("By", actor),
        ("When", when),
        ("Why", note or "No note was given."),
        ("Project", project),
        ("Pipeline id", wf.id),
        ("Fingerprint", fingerprint or "not recorded"),
    ]
    lines = [f"# {wf.name}", ""]
    if (wf.business_purpose or "").strip():
        lines += [f"> {wf.business_purpose.strip()}", ""]
    if (wf.description or "").strip():
        lines += [wf.description.strip(), ""]
    lines += ["| | |", "|---|---|"]
    lines += [f"| {key} | {_cell(value)} |" for key, value in rows]
    lines += ["", "## Steps", "", "| # | Step | Type |", "|---|---|---|"]
    lines += [
        f"| {index} | {_cell(step.label or step.id)} | {_cell(getattr(step.type, 'value', step.type))} |"
        for index, step in enumerate(wf.steps, 1)
    ]
    connections = [c.get("name") or c.get("id") for c in needs["connections"]]
    credentials = [c.get("name") or c.get("id") for c in needs["credentials"]]
    tables = [f"{t['schema']}.{t['name']}" for t in needs["tables"]]
    needed = [
        ("Connections", connections), ("Credentials", credentials), ("Managed tables", tables),
        ("Files", needs["files"]), ("Pipelines it runs", [p.get("name") or p.get("id") for p in needs["pipelines"]]),
    ]
    if any(values for _, values in needed):
        lines += ["", "## Depends on", ""]
        lines += [f"- **{label}:** {', '.join(str(v) for v in values)}" for label, values in needed if values]
    lines += [
        "", "## Putting it back", "",
        "In Revoluzen Flow, open Pipelines and import `pipeline.json`. It comes back as a draft.",
        "Passwords and keys are not in this repository, so add the connections and credentials",
        "listed in `depends-on.json` first, then test and publish it.",
        "",
    ]
    if (wf.readme or "").strip():
        lines += ["## Notes from the pipeline", "", wf.readme.strip(), ""]
    return "\n".join(lines)


def build_bundle(
    view: WorkflowVersion,
    *,
    root_folder: str,
    workspace_id: str,
    event: str,
    actor_name: str,
    actor_email: str,
    note: str,
    fingerprint: str,
    app_state: dict,
) -> Bundle:
    """Everything one copy needs, already checked for secrets."""
    wf = view.workflow
    project = ""
    project_store = app_state.get("project_store")
    if wf.project_id and project_store is not None:
        record = project_store.get(wf.project_id)
        project = getattr(record, "name", "") if record else ""
    project = project or ("Default" if wf.project_id in ("", "default", None) else wf.project_id)

    needs = depends_on(view, workspace_id, app_state)
    when = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    actor = f"{actor_name} <{actor_email}>" if actor_email else (actor_name or "unknown")
    files = {
        "pipeline.json": json.dumps(pipeline_payload(view, workspace_id), indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        "depends-on.json": json.dumps(needs, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        "README.md": _readme(
            view, event=event, actor=actor, note=note, when=when,
            project=project, needs=needs, fingerprint=fingerprint,
        ),
    }

    parts = [part for part in (root_folder, "" if workspace_id == "default" else _slug(workspace_id, "workspace")) if part]
    parts += [_slug(project, "project"), f"{_slug(wf.name, 'pipeline')}--{wf.id}"]
    title = EVENT_TITLES.get(event, "Copy")
    message_lines = [
        f'{title} "{wf.name}" version {view.version}'[:120], "",
    ]
    if note:
        message_lines += [note, ""]
    message_lines += [
        f"By: {actor}",
        f"Pipeline id: {wf.id}",
        f"Fingerprint: {fingerprint or 'not recorded'}",
    ]
    message = "\n".join(message_lines) + "\n"
    assert_no_secrets(files, message)
    # A newline or angle bracket would break git's "Name <email>" line.
    author_name = re.sub(r"[<>\r\n]", " ", actor_name or "").strip() or "Revoluzen Flow"
    author_email = re.sub(r"[<>\s]", "", actor_email or "") or "flow@revoluzen.invalid"
    return Bundle(
        folder="/".join(parts),
        files=files,
        message=message,
        author=(author_name, author_email),
        pipeline_id=wf.id,
    )


def write_bundle(bundle: Bundle, working_directory: str, root_folder: str) -> None:
    """Write the files, first removing any older folder for this pipeline, so
    a rename or a move to another project shows in Git as just that."""
    root = os.path.join(working_directory, *root_folder.split("/")) if root_folder else working_directory
    suffix = f"--{bundle.pipeline_id}"
    if os.path.isdir(root):
        for current, directories, _files in os.walk(root):
            if ".git" in directories:
                directories.remove(".git")
            for name in list(directories):
                if name.endswith(suffix):
                    shutil.rmtree(os.path.join(current, name))
                    directories.remove(name)
    target = os.path.join(working_directory, *bundle.folder.split("/"))
    os.makedirs(target, exist_ok=True)
    for name, text in bundle.files.items():
        with open(os.path.join(target, name), "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
