"""Reports people design, save and schedule (Insights > Reports).

    GET    /api/reports/builder/catalog        what can go in a report
    POST   /api/reports/builder/preview        a report before it is saved
    GET    /api/reports/saved                  the workspace's reports
    POST   /api/reports/saved                  save a new one
    GET    /api/reports/saved/{id}             one report
    PUT    /api/reports/saved/{id}             change it
    DELETE /api/reports/saved/{id}             remove it and its delivery log
    POST   /api/reports/saved/{id}/duplicate   copy it, with the schedule off
    POST   /api/reports/saved/{id}/schedule    turn the schedule on or off
    POST   /api/reports/saved/{id}/run         make it now, on screen
    POST   /api/reports/saved/{id}/send        send it now
    GET    /api/reports/saved/{id}/download    PDF or Word
    GET    /api/reports/saved/{id}/deliveries  when it went out, and how that went
    GET    /api/reports/saved/{id}/deliveries/{delivery_id}  the report as it went out

Anyone signed in to the workspace can read and view; changing or sending a
report needs at least developer, the same line as alert rules.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel

from revoluzen_flow.auth.deps import current_workspace_id, require_auth, require_min_rank
from revoluzen_flow.reports.builder.catalog import public_catalog, section_problems
from revoluzen_flow.reports.builder.collect import build_report
from revoluzen_flow.reports.builder.deliver import WEBHOOK_ALLOW_PRIVATE_ENV, channel_ready, deliver
from revoluzen_flow.reports.builder.models import (
    ENVIRONMENTS, MAX_DESCRIPTION, MAX_NAME, MAX_RECIPIENTS, MAX_SECTIONS, RECIPIENT_TYPES, RUN_KINDS,
    DeliveryState, ReportDefinition, SavedReport, clean_line, is_email, schedule_problems, time_range_problems,
)
from revoluzen_flow.reports.builder.render import render_docx, render_pdf
from revoluzen_flow.reports.builder.schedule import describe_schedule, is_known_zone, next_occurrence, range_label
from revoluzen_flow.reports.builder.store import SECRET_PLACEHOLDER, SECRET_TARGET_TYPES, ReportStore, masked
from revoluzen_flow.security.ssrf import SsrfBlockedError, check_url

router = APIRouter(prefix="/api/reports", tags=["report designer"], dependencies=[Depends(require_auth)])

_AUTHOR = Depends(require_min_rank("developer"))


def _safe_workspace_id(request: Request) -> str:
    try:
        return current_workspace_id(request)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, "workspace resolve failed") from exc


def _state() -> dict:
    from revoluzen_flow.main import app_state

    return app_state


def _store() -> ReportStore:
    store = _state().get("report_store")
    if store is None:
        raise HTTPException(503, "Reports are not available yet.")
    return store


def _audit(user: Any, action: str, report_id: str, details: dict | None = None) -> None:
    try:
        audit = _state().get("audit_logger")
        if audit:
            audit.log(
                user_id=getattr(user, "id", "") or "",
                user_email=getattr(user, "email", "") or "",
                action=action,
                resource_type="report",
                resource_id=report_id,
                details=details or {},
            )
    except Exception:
        pass


def _can_see_prod(user: Any) -> bool:
    from revoluzen_flow.api.reports import _can_see_prod as can_see

    return can_see(user)


def _tier() -> str:
    from revoluzen_flow.api.reports import _detect_tier

    return _detect_tier()


def _environments(user: Any) -> list[dict[str, str]]:
    """The environments this person can report on. Community has one."""
    if _tier() == "plus" and _can_see_prod(user):
        return [
            {"value": "all", "label": "All environments"},
            {"value": "dev", "label": "Development"},
            {"value": "prod", "label": "Production"},
        ]
    return [{"value": "dev", "label": "Development"}]


def _narrow_environment(definition: ReportDefinition, user: Any) -> None:
    """Someone who may not see production gets development, never an error
    that confirms what production holds. Same rule as the inventory report."""
    if not _can_see_prod(user):
        definition.filters.environment = "dev"


def _problems(body: ReportDefinition, workspace_id: str, user: Any, *, preview: bool = False) -> list[str]:
    """Everything wrong with a report, in words for the person building it."""
    problems: list[str] = []
    body.name = clean_line(body.name)
    body.description = (body.description or "").strip()
    if not preview:
        if not body.name:
            problems.append("Give the report a name.")
        if len(body.name) > MAX_NAME:
            problems.append(f"Keep the name under {MAX_NAME} characters.")
    if len(body.description) > MAX_DESCRIPTION:
        problems.append(f"Keep the description under {MAX_DESCRIPTION} characters.")
    if not is_known_zone(body.timezone):
        problems.append("Choose a time zone this server knows.")
    if not preview and not body.sections:
        problems.append("Add at least one section.")
    if len(body.sections) > MAX_SECTIONS:
        problems.append(f"A report can hold up to {MAX_SECTIONS} sections.")
    for section in body.sections:
        problems.extend(section_problems(section))

    filters = body.filters
    if filters.environment not in ENVIRONMENTS:
        problems.append("Choose an environment.")
    if filters.run_kinds not in RUN_KINDS:
        problems.append("Choose which runs to count.")
    problems.extend(time_range_problems(filters.time_range))

    from revoluzen_flow.projects.acl import assert_project_access

    for project_id in filters.project_ids:
        try:
            assert_project_access(project_id, workspace_id, user, action="report")
        except HTTPException:
            problems.append("One of the chosen projects no longer exists or is not available to you.")
            break
    workflow_store = _state().get("store")
    for workflow_id in filters.workflow_ids:
        if workflow_store is None or workflow_store.get(workflow_id, workspace_id=workspace_id) is None:
            problems.append("One of the chosen pipelines no longer exists.")
            break

    if preview:
        return problems

    problems.extend(schedule_problems(body.schedule))
    if len(body.recipients) > MAX_RECIPIENTS:
        problems.append(f"A report can go to up to {MAX_RECIPIENTS} recipients.")
    for recipient in body.recipients:
        recipient.target = (recipient.target or "").strip()
        if recipient.type not in RECIPIENT_TYPES:
            problems.append("Unknown kind of recipient.")
        elif recipient.type == "email":
            if not is_email(recipient.target):
                problems.append(f"'{recipient.target}' is not an email address.")
        elif recipient.type == "webhook" and not recipient.target:
            problems.append("Enter the webhook address.")
        elif not recipient.target:
            # Slack or Teams left empty: only good if Settings has one to use.
            if not channel_ready()[f"{recipient.type}_settings"]:
                name = recipient.type.capitalize()
                problems.append(f"Enter the {name} webhook address. None is saved in Settings > Notifications.")
        elif recipient.target != SECRET_PLACEHOLDER:
            try:
                check_url(recipient.target, allow_private_env=WEBHOOK_ALLOW_PRIVATE_ENV)
            except SsrfBlockedError as exc:
                problems.append(f"That webhook address is not allowed: {exc}")
    if body.schedule.enabled:
        if not body.recipients:
            problems.append("Add at least one recipient, or turn the schedule off.")
        if filters.time_range.kind == "custom":
            problems.append("A report on a schedule needs a period that moves, such as Last 7 days.")
    return problems


def _check(body: ReportDefinition, workspace_id: str, user: Any, *, preview: bool = False) -> None:
    problems = _problems(body, workspace_id, user, preview=preview)
    if problems:
        raise HTTPException(400, {"message": problems[0], "problems": problems})
    _narrow_environment(body, user)


def _keep_stored_targets(body: ReportDefinition, existing: SavedReport | None) -> None:
    """The form shows a stored webhook as bullets and sends them back."""
    stored = {r.id: r for r in existing.recipients} if existing else {}
    for recipient in body.recipients:
        if recipient.target != SECRET_PLACEHOLDER:
            continue
        previous = stored.get(recipient.id)
        if previous is None or previous.type != recipient.type:
            raise HTTPException(400, "Enter the webhook address again.")
        recipient.target = previous.target


def _schedule_key(definition: ReportDefinition) -> tuple:
    s = definition.schedule
    return (s.enabled, s.frequency, s.time, tuple(s.weekly_days or []), s.day_of_month, definition.timezone)


def _present(report: SavedReport, state: DeliveryState) -> dict[str, Any]:
    data = masked(report)
    upcoming = None
    if report.schedule.enabled:
        upcoming = next_occurrence(report.schedule, report.timezone, datetime.now(timezone.utc))
    return {
        **data,
        "time_range_label": range_label(report.filters.time_range.kind),
        "schedule_text": describe_schedule(report.schedule, report.timezone) if report.schedule.enabled else "",
        "next_send_at": upcoming.isoformat() if upcoming else None,
        "delivery": {
            "last_attempt_at": state.last_attempt_at.isoformat() if state.last_attempt_at else None,
            "last_sent_at": state.last_sent_at.isoformat() if state.last_sent_at else None,
            "last_status": state.last_status,
            "last_error": state.last_error,
            "last_trigger": state.last_trigger,
        },
    }


def _find(report_id: str, workspace_id: str) -> SavedReport:
    report = _store().get(report_id, workspace_id=workspace_id)
    if report is None:
        raise HTTPException(404, "Report not found")
    return report


@router.get("/builder/catalog")
def get_catalog(user=Depends(require_auth)) -> dict[str, Any]:
    return {**public_catalog(), "environments": _environments(user), "channels": channel_ready()}


@router.post("/builder/preview")
def preview_report(
    body: ReportDefinition,
    user=Depends(require_auth),
    workspace_id: str = Depends(_safe_workspace_id),
) -> dict[str, Any]:
    """The report as it stands in the designer, before it is saved."""
    _check(body, workspace_id, user, preview=True)
    body.name = body.name or "Untitled report"
    return build_report(_state(), body, workspace_id)


@router.get("/saved")
def list_reports(workspace_id: str = Depends(_safe_workspace_id)) -> dict[str, Any]:
    return {"reports": [_present(r, s) for r, s in _store().list_all(workspace_id)]}


@router.post("/saved", dependencies=[_AUTHOR])
def create_report(
    body: ReportDefinition,
    user=Depends(require_auth),
    workspace_id: str = Depends(_safe_workspace_id),
) -> dict[str, Any]:
    _check(body, workspace_id, user)
    _keep_stored_targets(body, None)
    store = _store()
    if store.name_taken(workspace_id, body.name):
        raise HTTPException(409, "There is already a report with that name.")
    report = SavedReport(
        **body.model_dump(),
        workspace_id=workspace_id,
        created_by=getattr(user, "id", "") or "",
        created_by_name=getattr(user, "name", "") or getattr(user, "email", "") or "",
        schedule_changed_at=datetime.now(timezone.utc),
    )
    store.create(report)
    _audit(user, "report.create", report.id, {"name": report.name})
    return _present(report, DeliveryState())


@router.get("/saved/{report_id}")
def get_report(report_id: str, workspace_id: str = Depends(_safe_workspace_id)) -> dict[str, Any]:
    report = _find(report_id, workspace_id)
    return _present(report, _store().state(report_id))


@router.put("/saved/{report_id}", dependencies=[_AUTHOR])
def update_report(
    report_id: str,
    body: ReportDefinition,
    user=Depends(require_auth),
    workspace_id: str = Depends(_safe_workspace_id),
) -> dict[str, Any]:
    existing = _find(report_id, workspace_id)
    _check(body, workspace_id, user)
    _keep_stored_targets(body, existing)
    store = _store()
    if store.name_taken(workspace_id, body.name, exclude_id=report_id):
        raise HTTPException(409, "There is already a report with that name.")
    changed = _schedule_key(body) != _schedule_key(existing)
    updated = existing.model_copy(update={
        **{name: getattr(body, name) for name in ReportDefinition.model_fields},
        "schedule_changed_at": datetime.now(timezone.utc) if changed else existing.schedule_changed_at,
    })
    store.update(updated)
    _audit(user, "report.update", report_id, {"name": updated.name})
    return _present(updated, store.state(report_id))


@router.delete("/saved/{report_id}", dependencies=[_AUTHOR])
def delete_report(
    report_id: str,
    user=Depends(require_auth),
    workspace_id: str = Depends(_safe_workspace_id),
) -> dict[str, bool]:
    if not _store().delete(report_id, workspace_id):
        raise HTTPException(404, "Report not found")
    _audit(user, "report.delete", report_id)
    return {"deleted": True}


@router.post("/saved/{report_id}/duplicate", dependencies=[_AUTHOR])
def duplicate_report(
    report_id: str,
    user=Depends(require_auth),
    workspace_id: str = Depends(_safe_workspace_id),
) -> dict[str, Any]:
    source = _find(report_id, workspace_id)
    store = _store()
    from revoluzen_flow.common.unique_name import ensure_unique_name

    names = {r.name for r, _ in store.list_all(workspace_id)}
    now = datetime.now(timezone.utc)
    copy = SavedReport(
        **{name: getattr(source, name) for name in ReportDefinition.model_fields},
        workspace_id=workspace_id,
        created_by=getattr(user, "id", "") or "",
        created_by_name=getattr(user, "name", "") or getattr(user, "email", "") or "",
        schedule_changed_at=now,
    )
    copy.name = ensure_unique_name(f"{source.name} (copy)", names)
    copy.schedule = copy.schedule.model_copy(update={"enabled": False})
    store.create(copy)
    _audit(user, "report.create", copy.id, {"name": copy.name, "copied_from": report_id})
    return _present(copy, DeliveryState())


class ScheduleToggle(BaseModel):
    enabled: bool


@router.post("/saved/{report_id}/schedule", dependencies=[_AUTHOR])
def toggle_schedule(
    report_id: str,
    body: ScheduleToggle,
    user=Depends(require_auth),
    workspace_id: str = Depends(_safe_workspace_id),
) -> dict[str, Any]:
    report = _find(report_id, workspace_id)
    if body.enabled:
        if not report.recipients:
            raise HTTPException(400, "Add at least one recipient first.")
        if report.filters.time_range.kind == "custom":
            raise HTTPException(400, "A report on a schedule needs a period that moves, such as Last 7 days.")
    store = _store()
    if report.schedule.enabled != body.enabled:
        report.schedule = report.schedule.model_copy(update={"enabled": body.enabled})
        report.schedule_changed_at = datetime.now(timezone.utc)
        store.update(report)
        _audit(user, "report.schedule", report_id, {"enabled": body.enabled})
    return _present(report, store.state(report_id))


@router.post("/saved/{report_id}/run")
def run_report(
    report_id: str,
    user=Depends(require_auth),
    workspace_id: str = Depends(_safe_workspace_id),
) -> dict[str, Any]:
    report = _find(report_id, workspace_id)
    _narrow_environment(report, user)
    return build_report(_state(), report, workspace_id)


class SendRequest(BaseModel):
    # Just to the person asking: for checking how it looks.
    to_me: bool = False


@router.post("/saved/{report_id}/send", dependencies=[_AUTHOR])
def send_report(
    report_id: str,
    body: SendRequest,
    user=Depends(require_auth),
    workspace_id: str = Depends(_safe_workspace_id),
) -> dict[str, Any]:
    report = _find(report_id, workspace_id)
    _narrow_environment(report, user)
    only_to = None
    if body.to_me:
        email = getattr(user, "email", "") or ""
        if not is_email(email):
            raise HTTPException(400, "Your account has no email address to send to.")
        only_to = [email]
    actor = getattr(user, "name", "") or getattr(user, "email", "") or ""
    delivery = deliver(
        _state(), report, "test" if body.to_me else "manual",
        requested_by=actor, only_to=only_to,
    )
    _audit(user, "report.send", report_id, {"status": delivery.status, "test": body.to_me})
    return delivery.model_dump(mode="json")


@router.get("/saved/{report_id}/download")
def download_report(
    report_id: str,
    format: str = Query("pdf", pattern="^(pdf|docx)$"),
    user=Depends(require_auth),
    workspace_id: str = Depends(_safe_workspace_id),
) -> Response:
    """The report as a file, with a copy kept in Storage like other reports."""
    report = _find(report_id, workspace_id)
    _narrow_environment(report, user)
    result = build_report(_state(), report, workspace_id)
    from revoluzen_flow.api.reports import _persist_report_to_storage

    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in report.name)[:80] or "report"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    base = f"{safe}-{stamp}"
    if format == "pdf":
        blob, ext, media = render_pdf(result), ".pdf", "application/pdf"
    else:
        blob, ext, media = (
            render_docx(result), ".docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
    object_id, storage_error = _persist_report_to_storage(
        workspace_id, blob, base, ext, "custom", report.filters.environment,
        description=f"Report: {report.name}, {result['period']['label']}",
    )
    headers = {"Content-Disposition": f'attachment; filename="{base}{ext}"'}
    exposed = ["Content-Disposition"]
    if object_id:
        headers["X-Storage-Object-Id"] = object_id
        exposed.append("X-Storage-Object-Id")
    if storage_error:
        headers["X-Storage-Error"] = storage_error[:256]
        exposed.append("X-Storage-Error")
    headers["Access-Control-Expose-Headers"] = ", ".join(exposed)
    return Response(content=blob, media_type=media, headers=headers)


@router.get("/saved/{report_id}/deliveries")
def list_deliveries(
    report_id: str,
    limit: int = 50,
    workspace_id: str = Depends(_safe_workspace_id),
) -> dict[str, Any]:
    _find(report_id, workspace_id)
    return {"deliveries": _store().list_deliveries(report_id, workspace_id, limit)}


@router.get("/saved/{report_id}/deliveries/{delivery_id}")
def get_delivery_snapshot(
    report_id: str,
    delivery_id: str,
    workspace_id: str = Depends(_safe_workspace_id),
) -> dict[str, Any]:
    snapshot = _store().get_snapshot(report_id, delivery_id, workspace_id)
    if snapshot is None:
        raise HTTPException(404, "That copy of the report is not available.")
    return snapshot


__all__ = ["router", "SECRET_TARGET_TYPES"]
