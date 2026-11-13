"""Settings > Add-ons: drivers an admin adds from inside the app (2026-11-11).

See ``connections/addon_installer.py`` for what may be installed, where it
goes and why it cannot replace anything Revoluzen Flow ships.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel

from revoluzen_flow.auth.deps import require_admin
from revoluzen_flow.connections import addon_installer as installer

router = APIRouter(prefix="/api/addons", tags=["addons"])


def _data_dir() -> str:
    from revoluzen_flow.main import app_state

    data_dir = app_state.get("data_dir")
    if not data_dir:
        raise HTTPException(503, "Revoluzen Flow is still starting.")
    return str(data_dir)


def _actor(user) -> str:
    try:
        from revoluzen_flow.audit import actor_for
        return str(actor_for(user) or "")
    except Exception:  # noqa: BLE001
        return ""


def _audit(action: str, package: str, user=None, actor: str = "", **details) -> None:
    try:
        from revoluzen_flow.audit import audit_action
        audit_action(
            action=action, resource_type="addon", resource_id=package,
            actor=actor or _actor(user), details=details,
        )
    except Exception:  # noqa: BLE001 — never block the screen on the audit log
        pass


class InstallRequest(BaseModel):
    accept_licence: bool = False
    from_uploads: bool = False


@router.get("")
def addon_status(_admin=Depends(require_admin)):
    """The installable drivers, which are installed, and any install running."""
    return installer.status(_data_dir())


@router.post("/{package}/install", status_code=202)
def install_addon(package: str, body: InstallRequest, user=Depends(require_admin)):
    data_dir = _data_dir()
    actor = _actor(user)

    def _done(pkg: str, job: dict) -> None:
        _audit("addon.install.finished", pkg, actor=actor, state=job.get("state"),
               version=job.get("version"), source=job.get("source"))

    try:
        job = installer.start_install(
            data_dir, package, accepted=body.accept_licence, from_uploads=body.from_uploads,
            actor=actor, on_done=_done,
        )
    except installer.AddonError as exc:
        raise HTTPException(400, str(exc)) from None
    _audit("addon.install.started", package, actor=actor, source=job["source"], licence_accepted=True)
    return {"started": True, "job": job}


@router.post("/{package}/wheels")
async def upload_wheels(package: str, files: list[UploadFile] = File(...), user=Depends(require_admin)):
    """Keep wheel files for installing on a machine without internet access."""
    data_dir = _data_dir()
    saved: list[str] = []
    try:
        for upload in files:
            data = await upload.read(installer.MAX_WHEEL_BYTES + 1)
            saved.append(installer.save_wheel(data_dir, package, upload.filename or "", data))
    except installer.AddonError as exc:
        raise HTTPException(400, str(exc)) from None
    _audit("addon.wheels.uploaded", package, user=user, files=saved)
    return {"saved": saved}


@router.delete("/{package}/wheels")
def clear_wheels(package: str, user=Depends(require_admin)):
    try:
        installer.clear_wheels(_data_dir(), package)
    except installer.AddonError as exc:
        raise HTTPException(400, str(exc)) from None
    _audit("addon.wheels.cleared", package, user=user)
    return {"cleared": True}
