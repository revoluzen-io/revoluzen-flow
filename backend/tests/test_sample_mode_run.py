"""`safety_mode="sample"` must actually run (2026-10-23).

The pipeline editor's Run button calls
``POST /api/execute/workflow/{id}?full_run=false&safety_mode=sample``.
On any install without the sandbox-isolation extension — i.e. all of OSS —
that returned HTTP 500 with ``'NoneType' object has no attribute 'id'``.

The cause was in ``Executor._apply_sandbox_rewrites``. Both isolation hooks
are declared as inert no-ops that return ``None`` ("None signals no rewrite
was applied"), and the loop appended those ``None`` values as the workflow's
steps, so every step became ``None``. The product's primary run action could
not work.

These tests pin the two halves of that: the rewrite helper must preserve
steps when the hooks are inert, and the endpoint must return a successful
run in sample mode.
"""

from __future__ import annotations

import pytest

from revoluzen_flow.engine import executor as ex


# ── the unit that broke ─────────────────────────────────────────────────

def _wf():
    from revoluzen_flow.ir.schema import Workflow
    return Workflow.model_validate({
        "name": "sample-mode",
        "steps": [
            {"id": "s1", "type": "csv_source", "label": "src",
             "params": {"file_path": "x.csv"}, "position": {"x": 0, "y": 0}},
            {"id": "s2", "type": "filter", "label": "f",
             "params": {"mode": "expression", "condition": "1=1"},
             "position": {"x": 1, "y": 0}},
        ],
        "connections": [{"from_step": "s1", "to_step": "s2"}],
    })


def _bare_executor():
    """A WorkflowExecutor shell.

    `_apply_sandbox_rewrites` touches no instance state, and building a real
    executor would pull in a DuckDB connection and the node registry for a
    test about list handling.
    """
    return object.__new__(ex.WorkflowExecutor)


def test_inert_hooks_leave_every_step_intact():
    """With the default no-op hooks, the rewrite is an identity on steps.

    This is the exact assertion that would have caught the crash: before
    the fix, `steps` came back as [None, None].
    """
    wf = _wf()
    out = ex.WorkflowExecutor._apply_sandbox_rewrites(
        _bare_executor(), wf, "sample_ns", 100,
    )
    assert len(out.steps) == len(wf.steps)
    assert all(s is not None for s in out.steps), "a step was replaced by None"
    assert [s.id for s in out.steps] == ["s1", "s2"]


def test_an_extension_can_still_override_the_hooks(monkeypatch):
    """The `or` fallback must not swallow a real rewrite."""
    wf = _wf()

    def fake_rewrite(step, _ns):
        return step.model_copy(update={"label": step.label + "-sandboxed"})

    monkeypatch.setattr(ex, "rewrite_for_sandbox", fake_rewrite)
    out = ex.WorkflowExecutor._apply_sandbox_rewrites(
        _bare_executor(), wf, "sample_ns", 100,
    )
    assert [s.label for s in out.steps] == ["src-sandboxed", "f-sandboxed"]


def test_the_original_workflow_is_not_mutated():
    wf = _wf()
    ex.WorkflowExecutor._apply_sandbox_rewrites(
        _bare_executor(), wf, "sample_ns", 100,
    )
    assert [s.id for s in wf.steps] == ["s1", "s2"]


# ── end to end, through the endpoint the Run button calls ───────────────

PASSWORD = "SampleMode!2026X"


@pytest.fixture
def seeded(tmp_path):
    """A client signed in as the bootstrap admin, with a runnable pipeline.

    Mirrors the fixture in test_auth_change_password.py: conftest enables
    REVOLUZEN_FLOW_BOOTSTRAP_ADMIN, so `admin@revoluzen.local` exists and its
    password can be reset in-place. Self-registration is disabled by default,
    which is why this does not go through /api/auth/register.

    base_url must be a loopback name — the DNS-rebinding middleware rejects
    TestClient's default `testserver` Host before auth is ever consulted.
    """
    import os

    os.environ["REVOLUZEN_FLOW_DATA_DIR"] = str(tmp_path)
    from fastapi.testclient import TestClient
    import revoluzen_flow.main as m
    from revoluzen_flow.auth.models import User

    (tmp_path / "orders.csv").write_text(
        "order_id,amount,status\n1,250,paid\n2,89,paid\n3,1200,cancelled\n",
        encoding="utf-8",
    )
    headers = {}
    with TestClient(m.app, base_url="http://localhost") as c:
        admin = m.app_state["user_store"].get_user("admin")
        admin.password_hash = User.hash_password(PASSWORD)
        m.app_state["user_store"]._save_user(admin)
        tok = c.post("/api/auth/login", json={
            "email": "admin@revoluzen.local", "password": PASSWORD,
        }).json()["token"]
        headers["Authorization"] = f"Bearer {tok}"

        wf = c.post("/api/workflows", headers=headers, json={
            "name": "sample-mode e2e",
            "steps": [
                {"id": "s1", "type": "csv_source", "label": "Orders",
                 "params": {"file_path": "orders.csv"}, "position": {"x": 0, "y": 0}},
                {"id": "s2", "type": "filter", "label": "Paid",
                 "params": {"mode": "expression", "condition": "status = 'paid'"},
                 "position": {"x": 300, "y": 0}},
                # 2026-10-30: was {"schema": ..., "table": ...}. The node
                # reads schema_name/table_name, so both were ignored and the
                # rows went to a managed table literally called "default" —
                # and this test still asserted success, because the sink's
                # "table_name is required" guard could not fire while
                # safe_schema_or_table_name() returned "default" for an empty
                # name. With that fixed the wrong keys now raise, which is
                # what should always have happened.
                {"id": "s3", "type": "local_table_sink", "label": "out",
                 "params": {"schema_name": "default", "table_name": "orders_paid",
                            "mode": "replace"},
                 "position": {"x": 600, "y": 0}},
            ],
            "connections": [{"from_step": "s1", "to_step": "s2"},
                            {"from_step": "s2", "to_step": "s3"}],
        })
        assert wf.status_code < 400, wf.text
        yield c, headers, wf.json()["id"]


@pytest.mark.parametrize("mode", ["live", "sample"])
def test_run_succeeds_in_both_safety_modes(seeded, mode):
    """`sample` is what the editor's Run button sends — it must not 500."""
    c, headers, wid = seeded
    r = c.post(
        f"/api/execute/workflow/{wid}"
        f"?full_run=false&environment=dev&safety_mode={mode}",
        json={}, headers=headers,
    )
    assert r.status_code == 200, f"{mode} mode returned {r.status_code}: {r.text[:300]}"
    body = r.json()
    assert body.get("status") == "success", body.get("status")
    assert len(body.get("step_results") or {}) == 3


def test_sample_mode_reports_per_step_rows_and_timing(seeded):
    """Sample mode must come back with the same per-step detail as live —
    a run that 'works' but reports nothing is not much better than a 500."""
    c, headers, wid = seeded
    body = c.post(
        f"/api/execute/workflow/{wid}?full_run=false&safety_mode=sample",
        json={}, headers=headers,
    ).json()
    results = body["step_results"]
    assert results["s1"]["row_count"] == 3
    assert results["s2"]["row_count"] == 2, "the filter should drop the cancelled row"
    for sid, sr in results.items():
        assert sr.get("duration_ms") is not None, f"{sid} reported no duration"
