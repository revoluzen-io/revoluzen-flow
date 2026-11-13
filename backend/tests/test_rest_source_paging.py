"""REST (API) Source paging and incremental runs, against a local server (2026-11-11).

Found live on 2026-11-08 and reproduced here before the fix:
  * offset paging repeated one offset, so long lists came back with the
    same rows again and again;
  * a page shorter than Page Size ended the reading, so an API whose own
    page size is smaller than Page Size gave only its first page;
  * a reply that is a bare JSON list was never paged past the first page;
  * the step form shows "page_number" under Operation = Paginate without
    saving it, so a step left on that default read one page;
  * an incremental run with nothing new returned a table without the usual
    columns, so the next step failed.
"""

from __future__ import annotations

import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import duckdb
import pytest

from revoluzen_flow.engine import sync_state_store as cursor_store
from revoluzen_flow.engine.executor import WorkflowExecutor
from revoluzen_flow.ir.schema import Step, StepConnection, StepType, Workflow
from revoluzen_flow.nodes.activities import ApiSourceNode
from revoluzen_flow.nodes.base import ExecutionContext

RECORDS = [{"id": i, "name": f"n{i}"} for i in range(250)]
CHANGES = [
    {"id": 1, "updated_at": "2026-01-01", "amount": 10.5},
    {"id": 2, "updated_at": "2026-01-02", "amount": 20.0},
    {"id": 3, "updated_at": "2026-01-03", "amount": 30.25},
]


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # keep test output quiet
        pass

    def _reply(self, payload) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        parts = urllib.parse.urlsplit(self.path)
        q = dict(urllib.parse.parse_qsl(parts.query))
        offset = int(q.get("offset", 0))
        # The API's own page size is 50, and it never sends more than 50.
        limit = min(int(q.get("limit", 50)), 50)
        if parts.path == "/items":
            self._reply({"data": RECORDS[offset:offset + limit], "total": len(RECORDS)})
        elif parts.path == "/list":
            self._reply(RECORDS[offset:offset + limit])
        elif parts.path == "/pages":
            page = int(q.get("page", 1))
            per = 30
            self._reply({"items": RECORDS[(page - 1) * per:page * per]})
        elif parts.path == "/same":
            self._reply({"data": RECORDS[:10]})
        elif parts.path == "/changes":
            since = q.get("since", "")
            self._reply({"data": [c for c in CHANGES if c["updated_at"] > since]})
        else:
            self.send_response(404)
            self.end_headers()


@pytest.fixture
def server(monkeypatch):
    monkeypatch.setenv("REVOLUZEN_FLOW_API_SOURCE_ALLOW_PRIVATE", "1")
    http = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=http.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{http.server_address[1]}"
    finally:
        http.shutdown()
        http.server_close()


def _read(**params) -> list[dict]:
    ctx = ExecutionContext(conn=duckdb.connect(), full_run=True)
    rel = ApiSourceNode({"max_retries": 0, **params}).execute(ctx)
    columns = rel.columns
    return [dict(zip(columns, row)) for row in rel.fetchall()]


def _ids(rows: list[dict]) -> list[int]:
    return sorted(int(r["id"]) for r in rows)


# ── Paging ────────────────────────────────────────────────────────────────

def test_offset_paging_reads_every_row_once(server):
    rows = _read(url=f"{server}/items", pagination="offset_limit", page_size=100, data_key="data")
    assert _ids(rows) == list(range(250))


def test_offset_paging_of_a_bare_list(server):
    rows = _read(url=f"{server}/list", pagination="offset", page_size=100)
    assert _ids(rows) == list(range(250))


def test_page_paging_when_the_api_pages_smaller_than_page_size(server):
    # The API sends 30 per page; Page Size says 100. A short first page used
    # to end the reading after 30 rows.
    rows = _read(url=f"{server}/pages", pagination="page_number", page_size=100)
    assert _ids(rows) == list(range(250))


def test_the_form_default_paging_style_is_used(server):
    # Operation = Paginate with the style left on the shown default.
    rows = _read(url=f"{server}/pages", operation="paginate", page_size=100)
    assert _ids(rows) == list(range(250))


def test_operation_fetch_reads_one_page_even_with_a_style_left_behind(server):
    rows = _read(url=f"{server}/pages", operation="fetch", pagination="page_number")
    assert _ids(rows) == list(range(30))


def test_an_api_that_ignores_the_page_parameter_stops_instead_of_repeating(server):
    with pytest.raises(ValueError, match="same page twice"):
        _read(url=f"{server}/same", pagination="page_number", page_size=10)


def test_max_pages_still_caps_the_reading(server):
    rows = _read(url=f"{server}/items", pagination="offset", page_size=50, max_pages=2, data_key="data")
    assert _ids(rows) == list(range(100))


# ── Incremental runs ──────────────────────────────────────────────────────

WORKFLOW = "wf-rest-incremental"


@pytest.fixture
def store(_revoluzen_flow_test_db, monkeypatch):
    monkeypatch.setattr(cursor_store.sync_state_store, "_db", _revoluzen_flow_test_db)
    return cursor_store.sync_state_store


def _pipeline(server: str, out) -> Workflow:
    source = Step(id="src", type=StepType.API_SOURCE, params={
        "url": f"{server}/changes?since={{cursor}}", "data_key": "data", "max_retries": 0,
        "sync_mode": "incremental", "cursor_response_field": "updated_at",
    })
    keep = Step(id="keep", type=StepType.FILTER, params={"condition": "amount > 0"})
    write = Step(id="out", type=StepType.DESTINATION, params={"connector_type": "csv", "file_path": str(out)})
    return Workflow(
        id=WORKFLOW, name="rest incremental", steps=[source, keep, write],
        connections=[StepConnection(from_step="src", to_step="keep"), StepConnection(from_step="keep", to_step="out")],
    )


def test_an_incremental_run_with_nothing_new_keeps_the_columns(server, store, tmp_path):
    executor = WorkflowExecutor(data_dir=str(tmp_path), app_state={"data_dir": str(tmp_path)})
    first = executor.execute_workflow(_pipeline(server, tmp_path / "first.csv"))
    assert first.status == "success", first.step_results
    assert store.get(WORKFLOW, "src").last_cursor == "2026-01-03"

    second = executor.execute_workflow(_pipeline(server, tmp_path / "second.csv"))
    # The Filter reads `amount`. Before the fix it failed: the empty result
    # had a single column called "empty".
    assert second.status == "success", second.step_results
    assert second.step_results["src"].row_count == 0
    assert "amount" in second.step_results["src"].columns


def test_the_store_keeps_and_resets_the_columns(store):
    store.save_columns("wf-cols", "s1", [["id", "BIGINT"], ["amount", "DOUBLE"]])
    assert store.get_columns("wf-cols", "s1") == [["id", "BIGINT"], ["amount", "DOUBLE"]]
    store.reset("wf-cols", "s1")
    assert store.get_columns("wf-cols", "s1") is None
