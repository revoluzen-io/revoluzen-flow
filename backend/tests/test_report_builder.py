"""Reports people design and schedule: the numbers in them, when they go out,
whether delivery is recorded honestly, and who can do what."""
import email
import json
import threading
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from revoluzen_flow.monitoring.store import ExecutionRecord, StepLog
from revoluzen_flow.reports.builder.catalog import DATASETS, SECTIONS, TEMPLATES, custom_table_problems
from revoluzen_flow.reports.builder.collect import BUILDERS, build_report, scrub
from revoluzen_flow.reports.builder.models import (
    DeliveryState, ReportDefinition, ReportFilters, ReportRecipient, ReportSchedule, ReportSection, SavedReport,
    TimeRange,
)
from revoluzen_flow.reports.builder.render import render_chat, render_docx, render_html, render_pdf, render_text
from revoluzen_flow.reports.builder.schedule import (
    GRACE, RETRY_GAP, due_occurrence, latest_occurrence, next_occurrence, resolve_window,
)
from revoluzen_flow.reports.builder.store import SECRET_PLACEHOLDER, note_app_version, read_app_versions
from revoluzen_flow.storage.database import SCHEMA_VERSION, Database
from tests.conftest_fixtures_v2 import (  # noqa: F401
    data_dir, db_fixture, app_v2, client, admin_token, authed_client,
)

UTC = timezone.utc
# A Tuesday.
NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)


def weekly(**overrides) -> ReportSchedule:
    return ReportSchedule(**{"enabled": True, "frequency": "weekly", "time": "09:00", "weekly_days": [0], **overrides})


def definition(sections: list[str] | None = None, **overrides) -> ReportDefinition:
    data = {
        "name": "Ops digest",
        "timezone": "UTC",
        "sections": [ReportSection(type=t) for t in (sections or ["run_summary"])],
        **overrides,
    }
    return ReportDefinition(**data)


class TestWhenAReportGoesOut:
    def test_weekly_in_the_reports_own_time_zone(self):
        # Monday 09:00 in Kolkata is 03:30 UTC.
        at = latest_occurrence(weekly(), "Asia/Kolkata", NOW)
        assert at == datetime(2026, 11, 9, 3, 30, tzinfo=UTC)
        assert next_occurrence(weekly(), "Asia/Kolkata", NOW) == datetime(2026, 11, 16, 3, 30, tzinfo=UTC)

    def test_monthly_and_daily(self):
        monthly = ReportSchedule(enabled=True, frequency="monthly", time="06:00", day_of_month=1)
        assert latest_occurrence(monthly, "UTC", NOW) == datetime(2026, 11, 1, 6, 0, tzinfo=UTC)
        assert next_occurrence(monthly, "UTC", NOW) == datetime(2026, 12, 1, 6, 0, tzinfo=UTC)
        daily = ReportSchedule(enabled=True, frequency="daily", time="13:00")
        assert latest_occurrence(daily, "UTC", NOW) == datetime(2026, 11, 9, 13, 0, tzinfo=UTC)
        assert next_occurrence(daily, "UTC", NOW) == datetime(2026, 11, 10, 13, 0, tzinfo=UTC)

    def test_a_send_missed_while_the_server_was_off_still_goes(self):
        schedule = weekly()  # owed Monday 09:00 UTC, now Tuesday noon
        owed = due_occurrence(schedule, "UTC", DeliveryState(), None, NOW)
        assert owed == datetime(2026, 11, 9, 9, 0, tzinfo=UTC)

    def test_a_send_too_old_is_dropped(self):
        late = datetime(2026, 11, 9, 9, 0, tzinfo=UTC) + GRACE["weekly"] + timedelta(minutes=1)
        assert due_occurrence(weekly(), "UTC", DeliveryState(), None, late) is None

    def test_turning_a_schedule_on_is_not_a_request_for_the_last_send(self):
        changed = datetime(2026, 11, 9, 10, 0, tzinfo=UTC)
        assert due_occurrence(weekly(), "UTC", DeliveryState(), changed, NOW) is None

    def test_a_schedule_that_is_off_owes_nothing(self):
        assert due_occurrence(weekly(enabled=False), "UTC", DeliveryState(), None, NOW) is None

    def test_sent_once_per_occurrence(self):
        owed = datetime(2026, 11, 9, 9, 0, tzinfo=UTC).isoformat()
        state = DeliveryState(occurrence=owed, occurrence_status="delivered", attempts=1)
        assert due_occurrence(weekly(), "UTC", state, None, NOW) is None

    def test_a_send_that_reached_some_people_is_never_repeated(self):
        owed = datetime(2026, 11, 9, 9, 0, tzinfo=UTC).isoformat()
        state = DeliveryState(occurrence=owed, occurrence_status="partial", attempts=1)
        assert due_occurrence(weekly(), "UTC", state, None, NOW) is None

    def test_a_failed_send_is_retried_after_a_gap_then_given_up(self):
        owed = datetime(2026, 11, 9, 9, 0, tzinfo=UTC)
        just_now = DeliveryState(occurrence=owed.isoformat(), occurrence_status="failed", attempts=1, last_attempt_at=NOW)
        assert due_occurrence(weekly(), "UTC", just_now, None, NOW + timedelta(minutes=1)) is None
        assert due_occurrence(weekly(), "UTC", just_now, None, NOW + RETRY_GAP) == owed
        exhausted = just_now.model_copy(update={"attempts": 3})
        assert due_occurrence(weekly(), "UTC", exhausted, None, NOW + RETRY_GAP) is None


class TestPeriods:
    def test_rolling(self):
        w = resolve_window(TimeRange(kind="last_7_days"), "UTC", NOW)
        assert (w.start, w.end) == (NOW - timedelta(days=7), NOW)
        assert (w.previous_start, w.previous_end) == (NOW - timedelta(days=14), NOW - timedelta(days=7))

    def test_last_week_is_monday_to_sunday_in_the_reports_zone(self):
        w = resolve_window(TimeRange(kind="last_week"), "Asia/Kolkata", NOW)
        assert w.start == datetime(2026, 11, 1, 18, 30, tzinfo=UTC)  # Mon 2 Nov 00:00 IST
        assert w.end == datetime(2026, 11, 8, 18, 30, tzinfo=UTC)
        assert w.previous_start == datetime(2026, 10, 25, 18, 30, tzinfo=UTC)

    def test_a_running_month_compares_with_the_same_stretch_of_last_month(self):
        w = resolve_window(TimeRange(kind="this_month"), "UTC", NOW)
        assert w.start == datetime(2026, 11, 1, tzinfo=UTC)
        assert w.previous_start == datetime(2026, 10, 1, tzinfo=UTC)
        assert w.previous_end == datetime(2026, 10, 10, 12, 0, tzinfo=UTC)

    def test_custom_includes_the_last_day(self):
        w = resolve_window(TimeRange(kind="custom", start="2026-11-01", end="2026-11-03"), "UTC", NOW)
        assert w.end == datetime(2026, 11, 4, tzinfo=UTC)
        assert "1 Nov 2026 to 3 Nov 2026" in w.label


class TestCatalog:
    def test_every_section_can_be_built(self):
        assert {s["type"] for s in SECTIONS} == set(BUILDERS)

    def test_templates_only_use_sections_that_exist(self):
        known = {s["type"] for s in SECTIONS}
        for template in TEMPLATES:
            assert template["sections"]
            assert {s["type"] for s in template["sections"]} <= known

    def test_the_weekly_template_covers_what_was_asked_for(self):
        weekly_digest = next(t for t in TEMPLATES if t["id"] == "weekly_digest")
        kinds = {s["type"] for s in weekly_digest["sections"]}
        assert {"environment", "usage", "failures", "successes", "new_builds"} <= kinds

    def test_custom_table_rules(self):
        good = {"dataset": "runs", "group_by": ["pipeline"], "columns": ["runs", "failed"]}
        assert custom_table_problems(good) == []
        assert custom_table_problems({**good, "dataset": "nope"})
        assert custom_table_problems({**good, "group_by": ["nope"]})
        assert custom_table_problems({**good, "columns": []})
        assert custom_table_problems({**good, "where": {"status": ["Nope"]}})
        # Without grouping the columns are the row's own fields.
        assert custom_table_problems({"dataset": "runs", "group_by": [], "columns": ["started", "error"]}) == []
        assert custom_table_problems({"dataset": "runs", "group_by": [], "columns": ["runs"]})

    def test_every_dataset_measure_is_implemented(self):
        from revoluzen_flow.reports.builder.collect import _MEASURES

        for key, dataset in DATASETS.items():
            assert {m for m, _, _ in dataset["measures"]} == set(_MEASURES[key])


class TestSafeToSendOut:
    def test_errors_lose_their_secrets(self):
        text = scrub("could not connect to postgres://etl:hunter2@db.example.com/x password=abc123")
        assert "hunter2" not in text
        assert "abc123" not in text

    def test_long_errors_are_shortened(self):
        assert len(scrub("x " * 500, 50)) <= 50


def _sample_result(sections=None) -> dict:
    return {
        "name": "Ops <digest>",
        "description": "",
        "generated_label": "10 Nov 2026 12:00 UTC",
        "period": {"label": "Last 7 days (3 Nov 2026 to 10 Nov 2026)"},
        "filters": [],
        "notes": [],
        "sections": sections or [
            {"id": "a", "type": "run_summary", "title": "Run summary", "blocks": [
                {"kind": "kpis", "items": [{"label": "Failed", "value": "3", "tone": "bad", "detail": "1 the period before"}]},
            ]},
            {"id": "b", "type": "failures", "title": "Failures", "blocks": [{
                "kind": "table",
                "columns": [{"label": "Pipeline", "align": "left"}, {"label": "Failed runs", "align": "right"}],
                "rows": [["Sales <load>", {"text": "3", "tone": "bad"}]],
                "total": 9, "empty": "None.",
            }]},
            {"id": "c", "type": "run_trend", "title": "Runs over time", "blocks": [{
                "kind": "bars",
                "series": [{"key": "success", "label": "Succeeded", "tone": "good"}, {"key": "failed", "label": "Failed", "tone": "bad"}],
                "rows": [{"label": "Mon 2 Nov", "values": {"success": 4, "failed": 1}}],
                "empty": "No runs.", "has_data": True,
            }]},
            {"id": "d", "type": "note", "title": "", "blocks": [{"kind": "text", "text": "Hello <team>", "tone": ""}]},
        ],
    }


class TestRendering:
    def test_html_escapes_everything_it_was_given(self):
        out = render_html(_sample_result())
        assert "Sales &lt;load&gt;" in out
        assert "Ops &lt;digest&gt;" in out
        assert "<load>" not in out
        assert "Hello &lt;team&gt;" in out

    def test_html_uses_no_layout_outlook_drops(self):
        out = render_html(_sample_result())
        assert "display:flex" not in out
        assert "display:grid" not in out

    def test_a_table_cut_short_says_so(self):
        assert "Showing 1 of 9." in render_html(_sample_result())
        assert "Showing 1 of 9." in render_text(_sample_result())

    def test_text_carries_the_numbers(self):
        out = render_text(_sample_result())
        assert "Failed: 3 (1 the period before)" in out
        assert "Sales <load> | 3" in out
        assert "Mon 2 Nov: Succeeded 4, Failed 1" in out

    def test_chat_is_short(self):
        rows = [[f"Pipeline {i}", "1"] for i in range(400)]
        out = render_chat(_sample_result([{"id": "a", "type": "failures", "title": "F", "blocks": [
            {"kind": "table", "columns": [{"label": "P", "align": "left"}, {"label": "N", "align": "right"}],
             "rows": rows, "total": 400, "empty": ""},
            {"kind": "text", "text": "x" * 5000, "tone": ""},
        ]}]))
        assert len(out) <= 2800
        assert "and 395 more" in out

    def test_pdf_and_word_are_real_files(self):
        assert render_pdf(_sample_result()).startswith(b"%PDF")
        assert render_docx(_sample_result()).startswith(b"PK")


# ── With a real database ────────────────────────────────────────────────


@pytest.fixture(scope="module")
def state(client):
    from revoluzen_flow.main import app_state

    return app_state


@pytest.fixture(autouse=True)
def clean_tables(state):
    db = state["db"]
    for table in ("saved_reports", "report_deliveries", "executions", "alert_logs", "lifecycle_events", "workflow_versions"):
        db.execute_write(f"DELETE FROM {table}")
    db.execute_write("DELETE FROM _meta WHERE key = 'app_version_history'")
    yield


def run(state, **kwargs) -> ExecutionRecord:
    defaults = dict(
        workflow_id="wf-a", workflow_name="Sales load", project_id="default", status="success",
        started_at=datetime.now(UTC) - timedelta(hours=2),
        completed_at=datetime.now(UTC) - timedelta(hours=2) + timedelta(seconds=30),
        triggered_by="schedule",
    )
    return state["execution_store"].record(ExecutionRecord(**{**defaults, **kwargs}))


def section_of(result: dict, section_type: str) -> dict:
    return next(s for s in result["sections"] if s["type"] == section_type)


def kpi_values(section: dict) -> dict[str, str]:
    block = next(b for b in section["blocks"] if b["kind"] == "kpis")
    return {i["label"]: i["value"] for i in block["items"]}


def first_table(section: dict) -> dict:
    return next(b for b in section["blocks"] if b["kind"] == "table")


def text_of(cell) -> str:
    return cell["text"] if isinstance(cell, dict) else cell


class TestTheNumbers:
    def test_run_summary_counts_the_period_and_the_one_before(self, state):
        run(state)
        run(state)
        run(state, status="error")
        run(state, status="cancelled")
        run(state, status="running", completed_at=None)
        run(state, started_at=datetime.now(UTC) - timedelta(days=8), completed_at=None, status="error")
        run(state, started_at=datetime.now(UTC) - timedelta(days=30), completed_at=None)

        values = kpi_values(section_of(build_report(state, definition(), "default"), "run_summary"))
        assert values["Runs"] == "5"
        assert values["Succeeded"] == "2"
        assert values["Failed"] == "1"
        assert values["Stopped"] == "1"
        # The Dashboard's rule: runs still going are not in the rate.
        assert values["Success rate"] == "50.0%"
        assert values["Average run time"] == "30.0 s"

        block = next(b for b in section_of(build_report(state, definition(), "default"), "run_summary")["blocks"])
        failed = next(i for i in block["items"] if i["label"] == "Failed")
        assert failed["detail"] == "1 the period before"

    def test_matches_the_dashboard(self, state):
        run(state)
        run(state, status="error")
        run(state, status="success")
        stats = state["execution_store"].get_stats(hours=24, workspace_id="default")
        report = definition(filters=ReportFilters(time_range=TimeRange(kind="last_24_hours")))
        values = kpi_values(section_of(build_report(state, report, "default"), "run_summary"))
        assert values["Runs"] == str(stats["total"])
        assert values["Failed"] == str(stats["failed"])
        assert values["Success rate"] == f"{stats['success_rate']:.1f}%"

    def test_narrowed_by_pipeline_project_and_kind_of_run(self, state):
        run(state, workflow_id="wf-a", project_id="p1")
        run(state, workflow_id="wf-b", project_id="p2")
        run(state, workflow_id="wf-c", project_id="p1", triggered_by="manual")

        def runs(**filters) -> str:
            made = build_report(state, definition(filters=ReportFilters(**filters)), "default")
            return kpi_values(section_of(made, "run_summary"))["Runs"]

        assert runs() == "3"
        assert runs(workflow_ids=["wf-b"]) == "1"
        assert runs(project_ids=["p1"]) == "2"
        assert runs(run_kinds="automatic") == "2"
        assert runs(project_ids=["p1"], run_kinds="automatic") == "1"
        # This edition's runs are all development runs.
        assert runs(environment="dev") == "3"
        assert runs(environment="prod") == "0"

    def test_another_workspaces_runs_never_appear(self, state):
        run(state, workspace_id="other")
        run(state)
        values = kpi_values(section_of(build_report(state, definition(), "default"), "run_summary"))
        assert values["Runs"] == "1"

    def test_failures_group_by_pipeline_and_scrub_the_error(self, state):
        for _ in range(3):
            run(state, status="error", error_message="login failed password=abc123", step_logs=[
                StepLog(step_id="s1", step_name="Read orders", step_type="csv_source", status="error"),
            ])
        run(state, workflow_id="wf-b", workflow_name="Other", status="error", error_message="boom")
        table = first_table(section_of(build_report(state, definition(["failures"]), "default"), "failures"))
        assert text_of(table["rows"][0][0]) == "Sales load"
        assert text_of(table["rows"][0][2]) == "3"
        assert text_of(table["rows"][0][4]) == "Read orders"
        assert "abc123" not in text_of(table["rows"][0][5])
        assert text_of(table["rows"][1][0]) == "Other"

    def test_a_row_limit_is_honoured_and_reported(self, state):
        for i in range(5):
            run(state, workflow_id=f"wf-{i}", workflow_name=f"P{i}", status="error")
        section = ReportSection(type="failures", options={"limit": 2})
        made = build_report(state, definition(sections=None) .model_copy(update={"sections": [section]}), "default")
        table = first_table(section_of(made, "failures"))
        assert len(table["rows"]) == 2
        assert table["total"] == 5

    def test_usage_adds_up_rows_and_delivered_rows(self, state):
        run(state, step_logs=[
            StepLog(step_id="a", step_name="Read", step_type="csv_source", status="success", rows_processed=100),
            StepLog(step_id="b", step_name="Save", step_type="local_table_sink", status="success", rows_processed=90),
        ], metadata={"peak_memory_mb": 512.0, "cpu_seconds": 2.5})
        values = kpi_values(section_of(build_report(state, definition(["usage"]), "default"), "usage"))
        assert values["Rows processed"] == "190"
        assert values["Rows delivered"] == "90"
        assert values["Highest memory"] == "512 MB"
        assert values["CPU time"] == "2.5 s"

    def test_usage_says_when_nothing_was_measured(self, state):
        run(state)
        values = kpi_values(section_of(build_report(state, definition(["usage"]), "default"), "usage"))
        assert values["Highest memory"] == "Not recorded"

    def test_trend_includes_quiet_days(self, state):
        run(state)
        made = build_report(state, definition(["run_trend"]), "default")
        bars = section_of(made, "run_trend")["blocks"][0]
        assert len(bars["rows"]) in (7, 8)
        assert sum(sum(r["values"].values()) for r in bars["rows"]) == 1

    def test_custom_table_grouped(self, state):
        run(state, status="error")
        run(state)
        run(state, workflow_id="wf-b", workflow_name="B")
        section = ReportSection(type="custom_table", title="Mine", options={
            "dataset": "runs", "group_by": ["pipeline"], "columns": ["runs", "failed", "success_rate"],
            "sort_by": "runs", "sort_dir": "desc", "limit": 10,
        })
        made = build_report(state, definition().model_copy(update={"sections": [section]}), "default")
        result = section_of(made, "custom_table")
        assert result["title"] == "Mine"
        table = first_table(result)
        assert [c["label"] for c in table["columns"]] == ["Pipeline", "Runs", "Failed", "Success rate"]
        assert [text_of(c) for c in table["rows"][0]] == ["Sales load", "2", "1", "50.0%"]

    def test_custom_table_one_row_per_run_with_a_filter(self, state):
        run(state, status="error", error_message="bad")
        run(state)
        section = ReportSection(type="custom_table", options={
            "dataset": "runs", "group_by": [], "columns": ["pipeline", "status", "error"],
            "where": {"status": ["Failed"]},
        })
        made = build_report(state, definition().model_copy(update={"sections": [section]}), "default")
        table = first_table(section_of(made, "custom_table"))
        assert [[text_of(c) for c in r] for r in table["rows"]] == [["Sales load", "Failed", "bad"]]

    def test_a_section_that_breaks_does_not_take_the_report_down(self, state):
        run(state)
        with patch.dict(BUILDERS, {"failures": lambda ctx, section: 1 / 0}):
            made = build_report(state, definition(["failures", "run_summary"]), "default")
        assert "could not be made" in section_of(made, "failures")["blocks"][0]["text"]
        assert kpi_values(section_of(made, "run_summary"))["Runs"] == "1"


class TestNewBuilds:
    def _pipeline(self, state, workflow_id="wf-new", name="Fresh pipeline", project_id="default"):
        from revoluzen_flow.ir.schema import PipelineStatus, Step, StepType, Workflow

        workflow = Workflow(
            id=workflow_id, name=name, project_id=project_id, workspace_id="default",
            steps=[Step(id="s1", type=StepType.CSV_SOURCE, label="Read", params={"file_path": "a.csv"})],
        )
        store = state["store"]
        store.save(workflow, change_summary="first", created_by="Priya")
        again = store.get(workflow_id).workflow
        store.save(again, change_summary="added a filter", created_by="Priya")
        store.save(store.get(workflow_id).workflow, change_summary="autosave", kind="auto")
        store.update_status(workflow_id, PipelineStatus.PUBLISHED, published_by="Priya")
        state["lifecycle_store"].add_event(workflow_id, "published", "Published by Priya", workspace_id="default")
        return workflow_id

    def test_counts_real_saves_not_auto_saves(self, state):
        self._pipeline(state)
        # The period ends "now", and everything above happened within the same
        # millisecond or two of it.
        moment = datetime.now(UTC) + timedelta(seconds=1)
        section = section_of(build_report(state, definition(["new_builds"]), "default", moment), "new_builds")
        values = kpi_values(section)
        assert values["New pipelines"] == "1"
        assert values["New versions"] == "1"
        assert values["Published"] == "1"
        table = first_table(section)
        row = [text_of(c) for c in table["rows"][0]]
        assert row[0] == "Fresh pipeline"
        assert row[2] == "2"          # two saved versions, not the auto-save
        assert row[3] == "v2"
        assert "New pipeline" in row[4] and "Published" in row[4]
        assert row[5] == "Priya"

    def test_flow_upgrades_appear_once_there_is_something_to_compare(self, state):
        db = state["db"]
        assert note_app_version(db, "1.0.1", datetime.now(UTC) - timedelta(days=30)) is True
        assert note_app_version(db, "1.0.1") is False
        section = section_of(build_report(state, definition(["new_builds"]), "default"), "new_builds")
        assert not [b for b in section["blocks"] if b["kind"] == "text"]

        assert note_app_version(db, "1.0.2", datetime.now(UTC) - timedelta(days=1)) is True
        assert [v["version"] for v in read_app_versions(db)] == ["1.0.1", "1.0.2"]
        section = section_of(build_report(state, definition(["new_builds"]), "default"), "new_builds")
        notes = [b["text"] for b in section["blocks"] if b["kind"] == "text"]
        assert len(notes) == 1 and "1.0.2" in notes[0]


def test_a_cron_schedule_is_described_by_the_day_it_runs():
    from revoluzen_flow.reports.builder.collect import _describe_pipeline_schedule

    schedule = {"schedule_type": "cron", "cron_expression": "0 9 * * 1", "timezone": "Asia/Kolkata"}
    assert _describe_pipeline_schedule(schedule) == "Every Monday at 09:00 Asia/Kolkata"


class TestEverySectionRuns:
    @pytest.mark.parametrize("section_type", sorted(BUILDERS))
    def test_with_data(self, state, section_type):
        run(state, status="error", error_message="boom")
        run(state)
        options = {}
        if section_type == "note":
            options = {"text": "Hello"}
        if section_type == "custom_table":
            options = {"dataset": "steps", "group_by": ["step"], "columns": ["runs"]}
        section = ReportSection(type=section_type, options=options)
        made = build_report(state, definition().model_copy(update={"sections": [section]}), "default")
        assert "could not be made" not in json.dumps(made)
        # Every renderer copes with what each section produces.
        assert render_html(made)
        assert render_text(made)
        assert render_chat(made)
        assert render_pdf(made).startswith(b"%PDF")
        assert render_docx(made).startswith(b"PK")


class FakeSMTP:
    """Stands in for the mail server and keeps what it was given."""

    sent: list[tuple[str, list[str], str]] = []
    refuse: dict = {}
    fail_with: Exception | None = None

    def __init__(self, host, port, timeout=None):
        self.host = host
        self.port = port

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def starttls(self, context=None):
        pass

    def login(self, user, password):
        pass

    def sendmail(self, sender, recipients, message):
        if FakeSMTP.fail_with is not None:
            raise FakeSMTP.fail_with
        FakeSMTP.sent.append((sender, list(recipients), message))
        return FakeSMTP.refuse


@pytest.fixture
def mail(state):
    """A configured mail server that records instead of sending."""
    FakeSMTP.sent = []
    FakeSMTP.refuse = {}
    FakeSMTP.fail_with = None
    with patch("revoluzen_flow.alerts.notifier.smtplib.SMTP", FakeSMTP), patch(
        "revoluzen_flow.alerts.notifier.NotificationService._load_smtp_config",
        staticmethod(lambda: {"host": "mail.test", "port": 25, "user": "", "password": "", "from_email": "flow@test.invalid", "tls": False}),
    ):
        yield FakeSMTP


def saved(state, **overrides) -> SavedReport:
    data = {
        "name": "Weekly digest",
        "timezone": "UTC",
        "sections": [ReportSection(type="run_summary"), ReportSection(type="failures")],
        "recipients": [ReportRecipient(type="email", target="ops@test.invalid")],
        "schedule": weekly(),
        **overrides,
    }
    return state["report_store"].create(SavedReport(workspace_id="default", created_by="admin", **data))


def body_of(raw: str) -> tuple[email.message.Message, str, str]:
    message = email.message_from_string(raw)
    plain, html = "", ""
    for part in message.walk():
        if part.get_content_type() == "text/plain":
            plain = part.get_payload(decode=True).decode()
        if part.get_content_type() == "text/html":
            html = part.get_payload(decode=True).decode()
    return message, plain, html


class TestDelivery:
    def test_the_email_carries_the_real_numbers(self, state, mail):
        from revoluzen_flow.reports.builder.deliver import deliver

        run(state)
        run(state, status="error", error_message="nope")
        report = saved(state)
        delivery = deliver(state, report, "manual", requested_by="Priya")

        assert delivery.status == "delivered"
        assert [t.target for t in delivery.targets] == ["ops@test.invalid"]
        assert len(mail.sent) == 1
        sender, recipients, raw = mail.sent[0]
        assert recipients == ["ops@test.invalid"]
        message, plain, html = body_of(raw)
        assert message["Subject"].startswith("Weekly digest: Last 7 days")
        assert "Runs: 2" in plain
        assert "Failed: 1" in plain
        assert "Sales load" in html

    def test_recorded_with_what_went_out(self, state, mail):
        from revoluzen_flow.reports.builder.deliver import deliver

        run(state)
        report = saved(state)
        delivery = deliver(state, report, "manual")
        store = state["report_store"]
        log = store.list_deliveries(report.id, "default")
        assert log[0]["status"] == "delivered"
        assert log[0]["has_snapshot"] is True
        snapshot = store.get_snapshot(report.id, delivery.id, "default")
        assert kpi_values(section_of(snapshot, "run_summary"))["Runs"] == "1"
        assert store.state(report.id).last_status == "delivered"
        assert store.state(report.id).last_sent_at is not None

    def test_pdf_attachment(self, state, mail):
        from revoluzen_flow.reports.builder.deliver import deliver

        deliver(state, saved(state, attach_pdf=True), "manual")
        message = email.message_from_string(mail.sent[0][2])
        attachments = [p for p in message.walk() if p.get_content_disposition() == "attachment"]
        assert len(attachments) == 1
        assert attachments[0].get_filename() == "Weekly digest.pdf"
        assert attachments[0].get_payload(decode=True).startswith(b"%PDF")

    def test_some_addresses_refused_is_partial(self, state, mail):
        from revoluzen_flow.reports.builder.deliver import deliver

        mail.refuse = {"bad@test.invalid": (550, b"no such user")}
        report = saved(state, recipients=[
            ReportRecipient(type="email", target="ops@test.invalid"),
            ReportRecipient(type="email", target="bad@test.invalid"),
        ])
        delivery = deliver(state, report, "manual")
        assert delivery.status == "partial"
        assert {t.target: t.status for t in delivery.targets} == {
            "ops@test.invalid": "delivered", "bad@test.invalid": "failed",
        }
        assert state["report_store"].state(report.id).last_sent_at is not None

    def test_mail_server_down_is_recorded_in_plain_words(self, state, mail):
        from revoluzen_flow.reports.builder.deliver import deliver

        mail.fail_with = ConnectionRefusedError("refused")
        report = saved(state)
        delivery = deliver(state, report, "manual")
        assert delivery.status == "failed"
        assert "Could not reach the mail server" in delivery.detail
        store_state = state["report_store"].state(report.id)
        assert store_state.last_status == "failed"
        assert store_state.last_sent_at is None

    def test_no_mail_server_set_up(self, state):
        from revoluzen_flow.reports.builder.deliver import deliver

        with patch(
            "revoluzen_flow.alerts.notifier.NotificationService._load_smtp_config",
            staticmethod(lambda: {"host": "", "port": 25, "user": "", "password": "", "from_email": "", "tls": False}),
        ):
            delivery = deliver(state, saved(state), "manual")
        assert delivery.status == "failed"
        assert "Settings > Notifications" in delivery.detail

    def test_a_subject_cannot_inject_a_header(self, state, mail):
        from revoluzen_flow.reports.builder.deliver import deliver

        report = saved(state)
        report.name = "Bad\r\nBcc: someone@test.invalid"
        deliver(state, report, "manual")
        message = email.message_from_string(mail.sent[0][2])
        assert message["Bcc"] is None

    def test_slack_posts_the_headlines_and_settings_supply_the_webhook(self, state, mail):
        from revoluzen_flow.reports.builder import deliver as module

        run(state)
        posted = []
        with patch.object(module, "_notification_settings", lambda: {"slack_webhook": "https://hooks.slack.test/abc"}), \
             patch.object(module, "check_url", lambda url, **kw: None), \
             patch("revoluzen_flow.alerts.notifier.NotificationService._post_json", staticmethod(lambda url, payload, timeout=10: posted.append((url, payload)))):
            delivery = module.deliver(state, saved(state, recipients=[ReportRecipient(type="slack")]), "manual")
        assert delivery.status == "delivered"
        assert delivery.targets[0].target == "Slack"
        assert posted[0][0] == "https://hooks.slack.test/abc"
        assert "Runs 1" in posted[0][1]["text"]

    def test_slack_with_no_webhook_anywhere_says_so(self, state, mail):
        from revoluzen_flow.reports.builder import deliver as module

        with patch.object(module, "_notification_settings", lambda: {}):
            delivery = module.deliver(state, saved(state, recipients=[ReportRecipient(type="slack")]), "manual")
        assert delivery.status == "failed"
        assert "No Slack webhook" in delivery.detail

    def test_a_webhook_cannot_reach_the_private_network(self, state, mail):
        from revoluzen_flow.reports.builder.deliver import deliver

        posted = []
        with patch("revoluzen_flow.alerts.notifier.NotificationService._post_json", staticmethod(lambda *a, **k: posted.append(a))):
            delivery = deliver(state, saved(state, recipients=[ReportRecipient(type="webhook", target="http://169.254.169.254/x")]), "manual")
        assert delivery.status == "failed"
        assert posted == []

    def test_a_test_send_goes_only_to_the_person_asking(self, state, mail):
        from revoluzen_flow.reports.builder.deliver import deliver

        deliver(state, saved(state), "test", only_to=["me@test.invalid"])
        assert mail.sent[0][1] == ["me@test.invalid"]


class TestScheduledSends:
    def _due_now(self, state, **overrides):
        """A report owed right now: the send time just passed."""
        now = datetime.now(UTC)
        at = now - timedelta(minutes=5)
        schedule = ReportSchedule(enabled=True, frequency="daily", time=at.strftime("%H:%M"), **overrides)
        return saved(state, schedule=schedule)

    def _tick(self, state, scheduler):
        scheduler._check_reports(datetime.now(UTC))
        for thread in list(scheduler._run_threads.values()) or threading.enumerate():
            if thread.name.startswith("revoluzen_flow-report"):
                thread.join(timeout=10)
        deadline = datetime.now(UTC) + timedelta(seconds=10)
        while scheduler._running_jobs and datetime.now(UTC) < deadline:
            pass

    def test_goes_out_once_from_the_existing_scheduler(self, state, mail):
        from revoluzen_flow.scheduling.scheduler import PipelineScheduler

        run(state)
        report = self._due_now(state)
        scheduler = PipelineScheduler()
        self._tick(state, scheduler)
        assert len(mail.sent) == 1
        self._tick(state, scheduler)
        assert len(mail.sent) == 1
        log = state["report_store"].list_deliveries(report.id, "default")
        assert [d["trigger"] for d in log] == ["schedule"]

    def test_only_when_something_failed(self, state, mail):
        from revoluzen_flow.scheduling.scheduler import PipelineScheduler

        run(state)
        report = self._due_now(state, send_when="failures")
        self._tick(state, PipelineScheduler())
        assert mail.sent == []
        log = state["report_store"].list_deliveries(report.id, "default")
        assert log[0]["status"] == "skipped"
        assert "no runs failed" in log[0]["detail"]

    def test_a_failed_send_tells_someone(self, state, mail):
        from revoluzen_flow.scheduling.scheduler import PipelineScheduler

        mail.fail_with = ConnectionRefusedError("refused")
        report = self._due_now(state)
        self._tick(state, PipelineScheduler())
        bell = state["notification_store"].list_for_user("admin")
        titles = [n["title"] for n in bell]
        assert f"Report not delivered: {report.name}" in titles
        match = next(n for n in bell if n["title"] == f"Report not delivered: {report.name}")
        assert match["link_type"] == "report"
        assert match["link_id"] == report.id

    def test_the_watchdog_does_not_write_a_run_for_a_report(self, state):
        from revoluzen_flow.scheduling.scheduler import PipelineScheduler

        scheduler = PipelineScheduler()
        key = "report:abc"
        scheduler._running_jobs[key] = datetime.now(UTC) - timedelta(hours=7)
        scheduler._check_schedules()
        assert key not in scheduler._running_jobs
        assert state["execution_store"].list_all(workspace_id="default") == []


class TestStore:
    def test_webhooks_are_encrypted_at_rest(self, state):
        secret = "https://hooks.slack.test/T000/B000/secret"
        report = saved(state, recipients=[ReportRecipient(type="slack", target=secret)])
        raw = state["db"].fetchone("SELECT data FROM saved_reports WHERE id = ?", (report.id,))["data"]
        assert secret not in raw
        assert state["report_store"].get(report.id).recipients[0].target == secret

    def test_a_workspace_cannot_read_another(self, state):
        report = saved(state)
        assert state["report_store"].get(report.id, workspace_id="other") is None
        assert state["report_store"].delete(report.id, "other") is False

    def test_delivery_log_is_kept_to_a_limit(self, state, monkeypatch):
        from revoluzen_flow.reports.builder import store as module
        from revoluzen_flow.reports.builder.models import ReportDelivery

        monkeypatch.setattr(module, "KEEP_DELIVERIES", 3)
        report = saved(state)
        store = state["report_store"]
        for i in range(5):
            store.add_delivery(ReportDelivery(
                report_id=report.id, status="delivered",
                started_at=datetime.now(UTC) + timedelta(minutes=i),
            ), None)
        assert len(store.list_deliveries(report.id, "default")) == 3

    def test_an_edit_does_not_touch_the_delivery_state(self, state):
        store = state["report_store"]
        report = saved(state)
        at = datetime.now(UTC)
        store.record_outcome(report.id, "delivered", "", "schedule", at)
        report.description = "changed"
        store.update(report)
        assert store.state(report.id).last_status == "delivered"
        assert store.state(report.id).last_sent_at == at


class TestMigration:
    def test_an_older_database_gets_the_tables(self, tmp_path):
        path = tmp_path / "old.db"
        first = Database(str(path))
        first.execute_write("DROP TABLE saved_reports")
        first.execute_write("DROP TABLE report_deliveries")
        first.execute_write("UPDATE _meta SET value = '35' WHERE key = 'schema_version'")
        first.close()
        second = Database(str(path))
        assert second.count("saved_reports") == 0
        assert second.count("report_deliveries") == 0
        assert second.fetchone("SELECT value FROM _meta WHERE key = 'schema_version'")["value"] == str(SCHEMA_VERSION)
        second.close()

    def test_included_in_a_backup(self, state):
        saved(state)
        exported = state["db"].export_all()
        assert len(exported["_tables"]["saved_reports"]) == 1
        assert "report_deliveries" in exported["_tables"]


# ── The API ─────────────────────────────────────────────────────────────


def api_body(**overrides) -> dict:
    data = {
        "name": "From the API",
        "timezone": "UTC",
        "filters": {"time_range": {"kind": "last_7_days"}},
        "sections": [{"type": "run_summary"}],
        "schedule": {"enabled": False},
        "recipients": [],
    }
    data.update(overrides)
    return data


@pytest.fixture(scope="module")
def viewer(app_v2, authed_client):
    """A signed-in viewer in the same workspace."""
    from fastapi.testclient import TestClient

    from revoluzen_flow.auth.models import User
    from revoluzen_flow.main import app_state

    users = app_state["user_store"]
    password = "Viewer-test-" + "x" * 12
    user = User(email="viewer-report@test.invalid", name="Viewer", role="viewer", password_hash=User.hash_password(password))
    users._save_user(user)
    c = TestClient(app_v2, base_url="http://localhost")
    made = c.post("/api/auth/login", json={"email": user.email, "password": password})
    assert made.status_code == 200, made.text
    c.headers["Authorization"] = f"Bearer {made.json()['token']}"
    return c


class TestApi:
    def test_catalog(self, authed_client):
        r = authed_client.get("/api/reports/builder/catalog")
        assert r.status_code == 200
        body = r.json()
        assert {s["type"] for s in body["sections"]} == set(BUILDERS)
        assert body["templates"]
        # Community has one environment, and the page shows that.
        assert [e["value"] for e in body["environments"]] == ["dev"]
        assert set(body["channels"]) == {"email", "slack_settings", "teams_settings"}

    def test_preview_needs_no_name(self, authed_client, state):
        run(state)
        r = authed_client.post("/api/reports/builder/preview", json=api_body(name=""))
        assert r.status_code == 200, r.text
        assert kpi_values(section_of(r.json(), "run_summary"))["Runs"] == "1"

    def test_save_list_edit_delete(self, authed_client):
        created = authed_client.post("/api/reports/saved", json=api_body())
        assert created.status_code == 200, created.text
        report_id = created.json()["id"]
        listed = authed_client.get("/api/reports/saved").json()["reports"]
        assert [r["id"] for r in listed] == [report_id]
        assert listed[0]["delivery"]["last_status"] == ""

        changed = authed_client.put(f"/api/reports/saved/{report_id}", json=api_body(name="Renamed"))
        assert changed.status_code == 200, changed.text
        assert authed_client.get(f"/api/reports/saved/{report_id}").json()["name"] == "Renamed"

        assert authed_client.delete(f"/api/reports/saved/{report_id}").status_code == 200
        assert authed_client.get(f"/api/reports/saved/{report_id}").status_code == 404

    def test_names_are_unique_in_a_workspace(self, authed_client):
        assert authed_client.post("/api/reports/saved", json=api_body(name="Same")).status_code == 200
        assert authed_client.post("/api/reports/saved", json=api_body(name="same")).status_code == 409

    @pytest.mark.parametrize("change, message", [
        ({"name": "  "}, "name"),
        ({"sections": []}, "section"),
        ({"sections": [{"type": "nope"}]}, "Unknown section"),
        ({"timezone": "Mars/Base"}, "time zone"),
        ({"filters": {"time_range": {"kind": "custom", "start": "2026-11-05", "end": "2026-11-01"}}}, "ends before"),
        ({"filters": {"workflow_ids": ["missing"]}}, "no longer exists"),
        ({"recipients": [{"type": "email", "target": "not an address"}]}, "not an email"),
        ({"recipients": [{"type": "webhook", "target": ""}]}, "webhook address"),
        ({"schedule": {"enabled": True}}, "recipient"),
        ({"schedule": {"enabled": True, "time": "25:00"}, "recipients": [{"type": "email", "target": "a@test.invalid"}]}, "HH:MM"),
        ({"schedule": {"enabled": True, "frequency": "monthly", "day_of_month": 31}, "recipients": [{"type": "email", "target": "a@test.invalid"}]}, "1 to 28"),
        ({"schedule": {"enabled": True, "weekly_days": []}, "recipients": [{"type": "email", "target": "a@test.invalid"}]}, "day of the week"),
        ({"schedule": {"enabled": True}, "recipients": [{"type": "email", "target": "a@test.invalid"}],
          "filters": {"time_range": {"kind": "custom", "start": "2026-11-01", "end": "2026-11-05"}}}, "period that moves"),
    ])
    def test_bad_reports_are_refused_in_plain_words(self, authed_client, change, message):
        r = authed_client.post("/api/reports/saved", json=api_body(**change))
        assert r.status_code == 400, r.text
        assert message in json.dumps(r.json())

    def test_a_stored_webhook_is_never_sent_back(self, authed_client, state):
        with patch("revoluzen_flow.api.report_builder.check_url", lambda url, **kw: None):
            created = authed_client.post("/api/reports/saved", json=api_body(
                recipients=[{"type": "slack", "target": "https://hooks.slack.test/keep"}],
            ))
        assert created.status_code == 200, created.text
        body = created.json()
        recipient = body["recipients"][0]
        assert recipient["target"] == SECRET_PLACEHOLDER

        # Sending the bullets back keeps the stored address.
        again = authed_client.put(f"/api/reports/saved/{body['id']}", json=api_body(recipients=[recipient]))
        assert again.status_code == 200, again.text
        stored = state["report_store"].get(body["id"])
        assert stored.recipients[0].target == "https://hooks.slack.test/keep"

    def test_slack_left_empty_needs_one_in_settings(self, authed_client):
        with patch("revoluzen_flow.api.report_builder.channel_ready", lambda: {"slack_settings": False, "teams_settings": False, "email": True}):
            refused = authed_client.post("/api/reports/saved", json=api_body(recipients=[{"type": "slack", "target": ""}]))
        assert refused.status_code == 400
        assert "Slack webhook" in refused.text
        with patch("revoluzen_flow.api.report_builder.channel_ready", lambda: {"slack_settings": True, "teams_settings": False, "email": True}):
            accepted = authed_client.post("/api/reports/saved", json=api_body(recipients=[{"type": "slack", "target": ""}]))
        assert accepted.status_code == 200, accepted.text

    def test_bullets_for_a_new_recipient_are_refused(self, authed_client):
        r = authed_client.post("/api/reports/saved", json=api_body(
            recipients=[{"id": "new", "type": "slack", "target": SECRET_PLACEHOLDER}],
        ))
        assert r.status_code == 400

    def test_a_private_webhook_address_is_refused_when_saving(self, authed_client):
        r = authed_client.post("/api/reports/saved", json=api_body(
            recipients=[{"type": "webhook", "target": "http://127.0.0.1:9/x"}],
        ))
        assert r.status_code == 400
        assert "not allowed" in r.text

    def test_schedule_shows_the_next_send_and_can_be_toggled(self, authed_client):
        created = authed_client.post("/api/reports/saved", json=api_body(
            schedule={"enabled": True, "frequency": "weekly", "time": "09:00", "weekly_days": [0]},
            recipients=[{"type": "email", "target": "a@test.invalid"}],
        )).json()
        assert created["next_send_at"]
        assert created["schedule_text"] == "Every Mon at 09:00 UTC"
        off = authed_client.post(f"/api/reports/saved/{created['id']}/schedule", json={"enabled": False}).json()
        assert off["schedule"]["enabled"] is False
        assert off["next_send_at"] is None

    def test_cannot_turn_on_a_schedule_with_no_recipients(self, authed_client):
        created = authed_client.post("/api/reports/saved", json=api_body()).json()
        r = authed_client.post(f"/api/reports/saved/{created['id']}/schedule", json={"enabled": True})
        assert r.status_code == 400

    def test_changing_the_time_does_not_send_for_a_time_already_past(self, authed_client, state):
        created = authed_client.post("/api/reports/saved", json=api_body(
            schedule={"enabled": True, "frequency": "daily", "time": "00:00"},
            recipients=[{"type": "email", "target": "a@test.invalid"}],
        )).json()
        stored = state["report_store"].get(created["id"])
        assert stored.schedule_changed_at is not None
        from revoluzen_flow.reports.builder.deliver import due_reports

        assert due_reports(state, datetime.now(UTC)) == []

    def test_run_duplicate_and_download(self, authed_client, state):
        run(state)
        created = authed_client.post("/api/reports/saved", json=api_body(
            schedule={"enabled": True, "frequency": "daily", "time": "09:00"},
            recipients=[{"type": "email", "target": "a@test.invalid"}],
        )).json()
        report_id = created["id"]

        made = authed_client.post(f"/api/reports/saved/{report_id}/run")
        assert made.status_code == 200
        assert kpi_values(section_of(made.json(), "run_summary"))["Runs"] == "1"

        copy = authed_client.post(f"/api/reports/saved/{report_id}/duplicate").json()
        assert copy["name"] == "From the API (copy)"
        assert copy["schedule"]["enabled"] is False

        pdf = authed_client.get(f"/api/reports/saved/{report_id}/download?format=pdf")
        assert pdf.status_code == 200
        assert pdf.content.startswith(b"%PDF")
        word = authed_client.get(f"/api/reports/saved/{report_id}/download?format=docx")
        assert word.content.startswith(b"PK")
        assert authed_client.get(f"/api/reports/saved/{report_id}/download?format=exe").status_code == 422

    def test_send_records_a_delivery_and_a_test_goes_to_me(self, authed_client, mail):
        created = authed_client.post("/api/reports/saved", json=api_body(
            recipients=[{"type": "email", "target": "ops@test.invalid"}],
        )).json()
        report_id = created["id"]

        sent = authed_client.post(f"/api/reports/saved/{report_id}/send", json={})
        assert sent.status_code == 200, sent.text
        assert sent.json()["status"] == "delivered"
        assert mail.sent[0][1] == ["ops@test.invalid"]

        authed_client.post(f"/api/reports/saved/{report_id}/send", json={"to_me": True})
        assert mail.sent[1][1] != ["ops@test.invalid"]

        log = authed_client.get(f"/api/reports/saved/{report_id}/deliveries").json()["deliveries"]
        assert [d["trigger"] for d in log] == ["test", "manual"]
        shown = authed_client.get(f"/api/reports/saved/{report_id}/deliveries/{log[1]['id']}")
        assert shown.status_code == 200
        assert shown.json()["name"] == "From the API"

    def test_a_viewer_can_read_but_not_change_or_send(self, authed_client, viewer):
        created = authed_client.post("/api/reports/saved", json=api_body()).json()
        report_id = created["id"]
        assert viewer.get("/api/reports/saved").status_code == 200
        assert viewer.post(f"/api/reports/saved/{report_id}/run").status_code == 200
        assert viewer.post("/api/reports/saved", json=api_body(name="No")).status_code == 403
        assert viewer.put(f"/api/reports/saved/{report_id}", json=api_body()).status_code == 403
        assert viewer.delete(f"/api/reports/saved/{report_id}").status_code == 403
        assert viewer.post(f"/api/reports/saved/{report_id}/send", json={}).status_code == 403
        assert viewer.post(f"/api/reports/saved/{report_id}/schedule", json={"enabled": False}).status_code == 403

    def test_anonymous_callers_are_turned_away(self, client):
        assert client.get("/api/reports/saved").status_code == 401
        assert client.post("/api/reports/builder/preview", json=api_body()).status_code == 401

    def test_another_workspaces_report_is_not_found(self, authed_client, state):
        other = state["report_store"].create(SavedReport(
            name="Theirs", workspace_id="elsewhere", sections=[ReportSection(type="run_summary")],
        ))
        assert authed_client.get(f"/api/reports/saved/{other.id}").status_code == 404
        assert authed_client.post(f"/api/reports/saved/{other.id}/run").status_code == 404
        assert authed_client.delete(f"/api/reports/saved/{other.id}").status_code == 404
