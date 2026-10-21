"""Plugin System API — manage and discover installed plugins.

Authorization (2026-10-21)
--------------------------
A plugin is arbitrary Python executed inside the Flow process: the manager
adds the plugin directory to ``sys.path`` and calls
``spec.loader.exec_module`` (see ``revoluzen_flow/plugins/__init__.py``).
There is no signature check, no allowlist and no sandbox, so **loading a
plugin is equivalent to running code as the server**.

This router previously carried no authorization at all — only the
``REVOLUZEN_FLOW_ENABLE_PLUGINS`` feature flag, which defaults ON like every
other flag. That made ``POST /api/plugins/reload`` a privilege-escalation
path rather than a convenience: anyone who could reach the endpoint and get a
file onto the host could execute code in the server process.
``test_anonymous_access_blocked.py`` exists to catch a router added without
guards, and this one was never added to it.

So:

* the whole router requires authentication;
* the three mutations — reload, enable, disable — require **admin**, because
  each of them decides what code runs.

Reads stay authenticated-but-unprivileged: listing installed plugins and
fetching the scaffold template disclose no secrets, and developers writing a
plugin need the scaffold.

This is authorization only. It does not make plugin loading *safe* — that
needs signing, an allowlist, or out-of-process execution, none of which exist
yet. Admin-only is the floor, not the fix.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from revoluzen_flow.auth.deps import require_auth, require_min_rank

# Mutations decide which code the server executes, so they are admin-only.
_ADMIN = Depends(require_min_rank("admin"))

router = APIRouter(
    prefix="/api/plugins",
    tags=["plugins"],
    dependencies=[Depends(require_auth)],
)


def _get_manager(request: Request):
    # Stage 2: feature flag guard. Gated by REVOLUZEN_FLOW_ENABLE_PLUGINS.
    from revoluzen_flow.feature_flags import require
    from revoluzen_flow.main import app_state
    require("plugins")
    return app_state["plugin_manager"]


@router.get("")
async def list_plugins(request: Request):
    """List all installed plugins with status."""
    mgr = _get_manager(request)
    return mgr.list_plugins()


@router.post("/reload", dependencies=[_ADMIN])
async def reload_plugins(request: Request):
    """Re-discover and reload all plugins. Admin — this executes code."""
    mgr = _get_manager(request)
    return mgr.load_all()


@router.post("/{plugin_id}/enable", dependencies=[_ADMIN])
async def enable_plugin(plugin_id: str, request: Request):
    """Admin — enabling a plugin is what puts its code in the process."""
    mgr = _get_manager(request)
    mgr.enable_plugin(plugin_id)
    return {"status": "enabled", "plugin_id": plugin_id}


@router.post("/{plugin_id}/disable", dependencies=[_ADMIN])
async def disable_plugin(plugin_id: str, request: Request):
    """Admin — disabling is a containment action during an incident."""
    mgr = _get_manager(request)
    mgr.disable_plugin(plugin_id)
    return {"status": "disabled", "plugin_id": plugin_id}


@router.get("/scaffold")
async def get_scaffold(request: Request):
    """Get a scaffold template for creating a new plugin."""
    mgr = _get_manager(request)
    return mgr.get_scaffold()
