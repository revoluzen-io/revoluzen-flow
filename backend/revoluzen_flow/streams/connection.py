"""The saved connection a streaming step reads, with its sign-in merged in."""

from __future__ import annotations

from typing import Any


def saved_connection(ctx: Any, connection_id: str, who: str) -> tuple[dict, str]:
    """``(settings, connection type)`` of the step's saved connection.

    Looked up within the run's workspace (and environment, where the edition
    keeps one), so a step can never use another workspace's connection.
    """
    connection_id = str(connection_id or "").strip()
    if not connection_id:
        raise ValueError(f"{who}: pick a saved connection.")
    from revoluzen_flow.nodes import db_source

    getter = db_source._get_connection_config
    workspace_of = getattr(db_source, "workspace_from_ctx", None)
    environment_of = getattr(db_source, "environment_from_ctx", None)
    if workspace_of is not None:
        result = getter(
            connection_id,
            caller_ws=workspace_of(ctx),
            environment=environment_of(ctx) if environment_of is not None else None,
        )
    else:
        # The run's own stores when it was given them, else the server's.
        app_state = getattr(ctx, "app_state", None) or {}
        workspace_id = getattr(ctx, "workspace_id", None) or None
        if app_state.get("connection_store") is not None:
            result = getter(connection_id, app_state=app_state, workspace_id=workspace_id)
        else:
            result = getter(connection_id, workspace_id=workspace_id)
    if not result:
        raise ValueError(f"{who}: the saved connection '{connection_id}' was not found.")
    config, conn_type = result
    return dict(config or {}), str(conn_type or "").lower()


def connection_of_workflow(workflow: Any, connection_id: str) -> tuple[dict, str] | None:
    """A saved connection looked up for clean-up work outside a run (deleting
    a pipeline), within the pipeline's own workspace. None when not found."""
    from revoluzen_flow.nodes import db_source

    workspace_id = str(getattr(workflow, "workspace_id", None) or "default")
    getter = db_source._get_connection_config
    try:
        if getattr(db_source, "workspace_from_ctx", None) is not None:
            result = getter(connection_id, caller_ws=workspace_id)
        else:
            result = getter(connection_id, workspace_id=workspace_id)
    except Exception:  # noqa: BLE001 — clean-up is best effort
        return None
    if not result:
        return None
    config, conn_type = result
    return dict(config or {}), str(conn_type or "").lower()
