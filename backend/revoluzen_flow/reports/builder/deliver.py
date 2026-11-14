"""Sends a report, and records whether it worked.

The sending itself is ``alerts.notifier.NotificationService``: the same mail
server settings and the same webhook posting as alert rules, so a mail server
fixed in Settings is fixed here too.
"""

from __future__ import annotations

import logging
import smtplib
import socket
from datetime import datetime, timezone
from typing import Any

from revoluzen_flow.alerts.notifier import NotificationService
from revoluzen_flow.security.ssrf import SsrfBlockedError, check_url

from .collect import build_report, failed_run_count, scrub
from .models import DeliveryTarget, ReportDelivery, ReportRecipient, SavedReport
from .render import render_chat, render_html, render_pdf, render_text, report_subject
from .schedule import describe_schedule, due_occurrence

logger = logging.getLogger("revoluzen_flow.reports.builder")

#: Lets a report post to a webhook on the private network, for example a chat
#: server inside the company. Off unless the operator turns it on.
WEBHOOK_ALLOW_PRIVATE_ENV = "REVOLUZEN_FLOW_REPORT_WEBHOOK_ALLOW_PRIVATE"

_CHANNEL_NAMES = {"slack": "Slack", "teams": "Teams", "webhook": "Webhook"}
_SETTINGS_KEYS = {"slack": "slack_webhook", "teams": "teams_webhook"}


def plain_error(exc: Exception) -> str:
    """What went wrong, for someone reading the delivery history."""
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return "The mail server refused the sign-in. Check the user and password in Settings > Notifications."
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return "The mail server refused every recipient."
    if isinstance(exc, smtplib.SMTPSenderRefused):
        return "The mail server refused the sender address. Check 'From' in Settings > Notifications."
    if isinstance(exc, (ConnectionRefusedError, socket.gaierror, socket.timeout, TimeoutError)):
        return "Could not reach the mail server. Check the host and port in Settings > Notifications."
    if isinstance(exc, smtplib.SMTPException):
        return f"The mail server reported a problem: {scrub(exc, 160)}"
    if isinstance(exc, SsrfBlockedError):
        return f"That address is not allowed: {exc}"
    return scrub(exc, 200) or type(exc).__name__


def mail_server_ready(notifier: NotificationService | None = None) -> bool:
    return bool((notifier or NotificationService())._load_smtp_config().get("host"))


def _notification_settings() -> dict:
    from revoluzen_flow.api.notifications import _read_config

    return _read_config()


def channel_ready() -> dict[str, bool]:
    """Which channels could send right now, for the designer to say so."""
    settings = _notification_settings()
    return {
        "email": mail_server_ready(),
        "slack_settings": bool(settings.get("slack_webhook")),
        "teams_settings": bool(settings.get("teams_webhook")),
    }


def _webhook_url(recipient: ReportRecipient, settings: dict) -> str:
    if recipient.target:
        return recipient.target
    key = _SETTINGS_KEYS.get(recipient.type)
    url = str(settings.get(key) or "") if key else ""
    if not url:
        name = _CHANNEL_NAMES[recipient.type]
        raise ValueError(f"No {name} webhook is set here or in Settings > Notifications.")
    return url


def _post(notifier: NotificationService, recipient: ReportRecipient, url: str, result: dict) -> None:
    check_url(url, allow_private_env=WEBHOOK_ALLOW_PRIVATE_ENV)
    if recipient.type == "slack":
        notifier._post_json(url, {"text": render_chat(result)})
    elif recipient.type == "teams":
        notifier._post_json(url, {
            "@type": "MessageCard",
            "@context": "http://schema.org/extensions",
            "summary": report_subject(result),
            "sections": [{
                "activityTitle": result["name"],
                "activitySubtitle": result["period"]["label"],
                "text": render_chat(result).replace("\n", "<br>"),
                "markdown": True,
            }],
        })
    else:
        notifier._post_json(url, {"event": "report", "report": result})


def _send(
    notifier: NotificationService,
    report: SavedReport,
    recipients: list[ReportRecipient],
    result: dict,
) -> list[DeliveryTarget]:
    targets: list[DeliveryTarget] = []

    addresses = [r.target for r in recipients if r.type == "email"]
    if addresses:
        footer = describe_schedule(report.schedule, report.timezone) if report.schedule.enabled else ""
        attachments: list[tuple[str, bytes, str]] = []
        failed_to_attach = ""
        if report.attach_pdf:
            try:
                safe = "".join(c if c.isalnum() or c in "-_ " else "_" for c in report.name).strip() or "report"
                attachments.append((f"{safe}.pdf", render_pdf(result), "application/pdf"))
            except Exception:
                logger.exception("The PDF for report %s could not be made", report.id)
                failed_to_attach = "Sent without the PDF, which could not be made."
        try:
            refused = notifier.send_email_message(
                addresses, report_subject(result),
                render_text(result, footer), render_html(result, footer), attachments,
            )
        except Exception as exc:  # noqa: BLE001 — recorded, shown on the page
            error = plain_error(exc)
            targets.extend(DeliveryTarget(type="email", target=a, status="failed", error=error) for a in addresses)
        else:
            for address in addresses:
                if address in refused:
                    code = refused[address][0] if isinstance(refused[address], tuple) else ""
                    targets.append(DeliveryTarget(type="email", target=address, status="failed", error=f"The mail server refused this address ({code})."))
                else:
                    targets.append(DeliveryTarget(type="email", target=address, status="delivered", error=failed_to_attach))

    chat = [r for r in recipients if r.type != "email"]
    if chat:
        settings = _notification_settings()
        for recipient in chat:
            name = _CHANNEL_NAMES[recipient.type]
            try:
                _post(notifier, recipient, _webhook_url(recipient, settings), result)
            except Exception as exc:  # noqa: BLE001 — recorded, shown on the page
                targets.append(DeliveryTarget(type=recipient.type, target=name, status="failed", error=plain_error(exc)))
            else:
                targets.append(DeliveryTarget(type=recipient.type, target=name, status="delivered"))
    return targets


def _overall(targets: list[DeliveryTarget]) -> str:
    reached = sum(1 for t in targets if t.status == "delivered")
    if reached == len(targets) and targets:
        return "delivered"
    return "partial" if reached else "failed"


def _owner_notice(app_state: dict, report: SavedReport, delivery: ReportDelivery) -> None:
    """Tell someone in the app when a scheduled send did not fully work.

    A schedule nobody is watching fails in silence otherwise. Goes to whoever
    made the report, or to the admins if that person has gone.
    """
    store = app_state.get("notification_store")
    users = app_state.get("user_store")
    if store is None or users is None:
        return
    from revoluzen_flow.notifications.models import Notification

    all_users = users.list_users() or []
    owner = [u["id"] for u in all_users if u.get("id") == report.created_by and u.get("is_active", True)]
    admins = [u["id"] for u in all_users if u.get("role") in ("super_admin", "admin") and u.get("is_active", True)]
    title = (
        f"Report only partly delivered: {report.name}" if delivery.status == "partial"
        else f"Report not delivered: {report.name}"
    )
    for user_id in owner or admins:
        store.create(Notification(
            user_id=user_id, type="system", title=title, message=delivery.detail,
            link_type="report", link_id=report.id,
        ))


def deliver(
    app_state: dict,
    report: SavedReport,
    trigger: str,
    *,
    requested_by: str = "",
    only_to: list[str] | None = None,
    now: datetime | None = None,
) -> ReportDelivery:
    """Make the report and send it. Never raises: the outcome is the record."""
    store = app_state["report_store"]
    now = now or datetime.now(timezone.utc)
    delivery = ReportDelivery(
        report_id=report.id, report_name=report.name, workspace_id=report.workspace_id,
        trigger=trigger, requested_by=requested_by, started_at=now,
    )
    recipients = (
        [ReportRecipient(type="email", target=a) for a in only_to]
        if only_to is not None else list(report.recipients)
    )
    snapshot: dict | None = None
    try:
        if trigger == "schedule" and report.schedule.send_when == "failures" and not failed_run_count(
            app_state, report, report.workspace_id, now,
        ):
            delivery.status = "skipped"
            delivery.detail = "Nothing to send: no runs failed in this period."
        elif not recipients:
            delivery.status = "failed"
            delivery.detail = "This report has no recipients."
        else:
            snapshot = build_report(app_state, report, report.workspace_id, now)
            delivery.period = snapshot["period"]["label"]
            delivery.targets = _send(NotificationService(), report, recipients, snapshot)
            delivery.status = _overall(delivery.targets)
            delivery.detail = _summary(delivery)
    except Exception as exc:  # noqa: BLE001 — a report must never break the caller
        logger.exception("Report %s could not be delivered", report.id)
        delivery.status = "failed"
        delivery.detail = f"The report could not be made: {plain_error(exc)}"

    delivery.finished_at = datetime.now(timezone.utc)
    try:
        store.add_delivery(delivery, snapshot)
        store.record_outcome(report.id, delivery.status, delivery.detail if delivery.status != "delivered" else "", trigger, now)
    except Exception:
        logger.exception("The delivery of report %s could not be recorded", report.id)
    if trigger == "schedule" and delivery.status in ("failed", "partial"):
        try:
            _owner_notice(app_state, report, delivery)
        except Exception:
            logger.warning("Could not tell anyone about report %s", report.id, exc_info=True)
    return delivery


def _summary(delivery: ReportDelivery) -> str:
    total = len(delivery.targets)
    reached = sum(1 for t in delivery.targets if t.status == "delivered")
    if delivery.status == "delivered":
        return f"Delivered to {reached} recipient{'s' if reached != 1 else ''}."
    first = next((t.error for t in delivery.targets if t.status == "failed" and t.error), "")
    if delivery.status == "partial":
        return f"Delivered to {reached} of {total}. {first}".strip()
    return first or "Nothing could be delivered."


# ── What the scheduler calls ────────────────────────────────────────────


def due_reports(app_state: dict, now: datetime) -> list[tuple[SavedReport, datetime]]:
    store = app_state.get("report_store")
    if store is None:
        return []
    due: list[tuple[SavedReport, datetime]] = []
    for report, state in store.list_scheduled():
        occurrence = due_occurrence(report.schedule, report.timezone, state, report.schedule_changed_at, now)
        if occurrence is not None:
            due.append((report, occurrence))
    return due


def send_scheduled(app_state: dict, report_id: str, occurrence: datetime) -> ReportDelivery | None:
    """One scheduled send. The report is read again in case it just changed."""
    store = app_state["report_store"]
    report = store.get(report_id)
    if report is None or not report.schedule.enabled:
        return None
    now = datetime.now(timezone.utc)
    store.begin_occurrence(report_id, occurrence.isoformat(), now)
    delivery = deliver(app_state, report, "schedule", requested_by="schedule", now=now)
    store.finish_occurrence(report_id, delivery.status)
    return delivery
