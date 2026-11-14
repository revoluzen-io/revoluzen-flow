"""What published pipelines depend on, so it cannot be removed from under them.

A published pipeline runs on its own, so deleting a connection it reads from
breaks it later, when nobody is watching. The delete routes for connections,
credentials, managed tables, uploaded files and pipelines ask here first and
refuse, naming the pipelines.

Only the *live* definition of each published pipeline counts. A connection the
live version uses is protected even if a draft has stopped using it, and one
that only a draft has started using is not.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator

from revoluzen_flow.datastore.usage import _scan_workflow_for_refs

logger = logging.getLogger(__name__)


@dataclass
class PipelineRefs:
    """What one pipeline definition points at."""

    connections: set[str] = field(default_factory=set)
    connection_names: set[str] = field(default_factory=set)
    credentials: set[str] = field(default_factory=set)
    pipelines: set[str] = field(default_factory=set)
    connectors: set[str] = field(default_factory=set)
    tables: set[tuple[str, str]] = field(default_factory=set)
    table_roles: dict[tuple[str, str], str] = field(default_factory=dict)
    files: set[str] = field(default_factory=set)


def _walk(value: Any, found: PipelineRefs) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            name = key.lower() if isinstance(key, str) else ""
            if isinstance(item, str):
                if not item.strip():
                    continue
                if name == "connection_id" or name.endswith("_connection_id"):
                    found.connections.add(item)
                elif name == "credential_id" or name.endswith("_credential_id"):
                    found.credentials.add(item)
                elif name == "connection_name":
                    found.connection_names.add(item)
            else:
                _walk(item, found)
    elif isinstance(value, list):
        for item in value:
            _walk(item, found)


def references(definition: dict[str, Any]) -> PipelineRefs:
    """Everything a pipeline definition (a dumped Workflow) points at."""
    found = PipelineRefs()
    for step in definition.get("steps") or []:
        if not isinstance(step, dict):
            continue
        params = step.get("params")
        _walk(params, found)
        if isinstance(params, dict):
            for key, bucket in (("pipeline_id", found.pipelines), ("connector_id", found.connectors)):
                value = params.get(key)
                if isinstance(value, str) and value.strip():
                    bucket.add(value.strip())
    table_refs, file_paths = _scan_workflow_for_refs(definition)
    for schema, name, role in table_refs:
        key = (schema, name)
        found.tables.add(key)
        if found.table_roles.get(key) != "sink":
            found.table_roles[key] = role
    found.files.update(file_paths)
    return found


def published_definitions(store, workspace_id: str | None) -> Iterator[tuple[str, str, dict[str, Any]]]:
    """(id, name, live definition) of each published pipeline in a workspace,
    or in every workspace with None."""
    for row in store.list_all(workspace_id=workspace_id):
        if row.get("published_version") is None:
            continue
        pipeline_id = row.get("id") or ""
        try:
            live = store.live_version(pipeline_id, workspace_id=workspace_id)
        except Exception:  # noqa: BLE001
            # Its published version is missing or unreadable. Its newest one is
            # the best remaining evidence of what it needs.
            logger.warning("published refs: reading the newest version of %s", pipeline_id, exc_info=True)
            live = store.get(pipeline_id, workspace_id=workspace_id)
        if live is not None:
            yield pipeline_id, row.get("name") or pipeline_id, live.workflow.model_dump(mode="json")


def _matching(store, workspace_id: str | None, test) -> list[dict[str, str]]:
    return [
        {"id": pipeline_id, "name": name}
        for pipeline_id, name, definition in published_definitions(store, workspace_id)
        if test(pipeline_id, references(definition))
    ]


def published_using_connection(store, workspace_id: str, connection_id: str, name: str = "") -> list[dict[str, str]]:
    return _matching(
        store, workspace_id,
        lambda _pid, refs: connection_id in refs.connections or (bool(name) and name in refs.connection_names),
    )


def published_using_credential(
    store,
    workspace_id: str,
    credential_id: str,
    connections: Iterable[Any] = (),
) -> list[dict[str, str]]:
    """Directly, or through a connection that signs in with this credential."""
    through = {
        _field(c, "id") for c in connections
        if _field(c, "credential_id") == credential_id
    }
    names = {
        _field(c, "name") for c in connections
        if _field(c, "credential_id") == credential_id
    }
    return _matching(
        store, workspace_id,
        lambda _pid, refs: (
            credential_id in refs.credentials
            or bool(refs.connections & through)
            or bool(refs.connection_names & names)
        ),
    )


def published_using_table(store, workspace_id: str, schema: str, name: str) -> list[dict[str, str]]:
    key = ((schema or "default").lower(), (name or "").lower())
    return _matching(store, workspace_id, lambda _pid, refs: key in refs.tables)


def published_using_file(store, workspace_id: str, path: str) -> list[dict[str, str]]:
    wanted = (path or "").replace("\\", "/")
    return _matching(store, workspace_id, lambda _pid, refs: wanted in refs.files)


def published_calling_pipeline(store, workspace_id: str, pipeline_id: str) -> list[dict[str, str]]:
    return _matching(
        store, workspace_id,
        lambda pid, refs: pid != pipeline_id and pipeline_id in refs.pipelines,
    )


def published_using_connector(store, connector_id: str) -> list[dict[str, str]]:
    """A saved connector is available to every workspace, so all are checked."""
    return _matching(store, None, lambda _pid, refs: connector_id in refs.connectors)


def _field(item: Any, name: str) -> Any:
    return item.get(name) if isinstance(item, dict) else getattr(item, name, None)


def refusal(thing: str, name: str, pipelines: list[dict[str, str]], action: str = "deleted") -> dict[str, Any]:
    """The body of a 409: what was refused, why, and what to do about it."""
    names = [f'"{p["name"]}"' for p in pipelines]
    shown = names[:3]
    extra = len(names) - len(shown)
    if extra:
        listed = f"{', '.join(shown)} and {extra} more"
    elif len(shown) > 1:
        listed = f"{', '.join(shown[:-1])} and {shown[-1]}"
    else:
        listed = shown[0]
    one = len(names) == 1
    subject = "the published pipeline" if one else f"{len(names)} published pipelines:"
    who = "it" if one else "them"
    return {
        "code": "used_by_published_pipeline",
        "message": (
            f'{thing} "{name}" is used by {subject} {listed}, so it cannot be {action}. '
            f"Publish a version that no longer uses it, or unpublish {who}, then try again."
        ),
        "pipelines": pipelines,
    }


__all__ = [
    "PipelineRefs",
    "published_calling_pipeline",
    "published_definitions",
    "published_using_connection",
    "published_using_connector",
    "published_using_credential",
    "published_using_file",
    "published_using_table",
    "references",
    "refusal",
]
