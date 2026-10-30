"""Compute sizing: the overcommit verdict and the per-pipeline rollup.

Two numbers on the Configuration page were computed and then left for
the reader to interpret:

  * `theoretical_max_ram` hardcoded `max_workers * 4`, so raising
    DUCKDB_MEMORY_LIMIT to 8GB still printed a 4GB-per-worker worst
    case — the sentence ignored the very setting it described;
  * peak memory and CPU were recorded per RUN and shown on Run History,
    but never rolled up, so "which pipeline is the expensive one, and
    can I run them together?" meant reading runs one at a time.

The interesting case is the interaction. On the box these were built on,
20 workers x 4GB reads as 80 GB against 15.7 GB of RAM, and the
worst-case-only verdict said "drop to 3 workers" — while measurement
said the heaviest pipeline peaks at 200 MB and all seven together want
623 MB. Serialising a host that could run everything at once is a real
cost paid for a theoretical risk, so the verdict has to prefer the
measurement when it has one.
"""

from __future__ import annotations

import pytest

from revoluzen_flow.api.pool import (
    _assess_capacity,
    _parse_mem_gb,
    _worst_case_phrase,
)

pytestmark = pytest.mark.unit


# ── the limit string is read, not assumed ──────────────────────────────

@pytest.mark.parametrize("text,expected", [
    ("4GB", 4.0),
    ("8gb", 8.0),
    ("512MB", 0.5),
    ("2 GiB", 2.0),
    ("1TB", 1024.0),
    ("16", 16.0),          # bare number = GB, DuckDB's own default unit
])
def test_parse_mem_gb_reads_the_unit(text, expected):
    assert _parse_mem_gb(text) == pytest.approx(expected)


def test_parse_mem_gb_returns_zero_rather_than_guessing():
    # An unreadable limit must disable the verdict, not produce a
    # confident number from a default nobody configured.
    assert _parse_mem_gb("plenty") == 0.0
    assert _parse_mem_gb("") == 0.0
    assert _parse_mem_gb(None) == 0.0


def test_worst_case_phrase_follows_the_configured_limit():
    # The regression: this used to be `max_workers * 4` regardless.
    assert "80.0 GB" in _worst_case_phrase(20, "4GB")
    assert "160.0 GB" in _worst_case_phrase(20, "8GB")
    assert "10.0 GB" in _worst_case_phrase(20, "512MB")


# ── the verdict, without measurements ──────────────────────────────────

def test_fits_in_ram_is_reported_as_ok():
    out = _assess_capacity(max_workers=4, per_worker="2GB",
                           total_ram_gb=32.0, available_ram_gb=20.0)
    assert out["status"] == "ok"
    assert out["remediation"] == []


def test_mild_overcommit_is_flagged_but_not_screamed():
    out = _assess_capacity(max_workers=8, per_worker="2GB",
                           total_ram_gb=12.0, available_ram_gb=6.0)
    assert out["status"] == "overcommitted"
    assert out["severity"] == 1


def test_severe_overcommit_names_the_env_var_to_change():
    out = _assess_capacity(max_workers=20, per_worker="4GB",
                           total_ram_gb=15.7, available_ram_gb=8.0)
    assert out["status"] == "severely_overcommitted"
    assert out["safe_max_workers_at_current_limit"] == 3
    joined = " ".join(out["remediation"])
    assert "REVOLUZEN_FLOW_MAX_CONCURRENT_RUNS=3" in joined


def test_unreadable_inputs_produce_unknown_not_a_number():
    out = _assess_capacity(max_workers=20, per_worker="plenty",
                           total_ram_gb=15.7, available_ram_gb=8.0)
    assert out["status"] == "unknown"
    assert "worst_case_gb" not in out


# ── the verdict, with measurements: the diagnosis inverts ──────────────

def test_measurement_redirects_the_fix_from_concurrency_to_the_ceiling():
    """The case this was built for.

    Same config as `test_severe_overcommit_...` above — the only new
    input is what the pipelines actually used. The advice must change
    from "run fewer at once" to "stop reserving 4GB for a 200 MB job".
    """
    out = _assess_capacity(max_workers=20, per_worker="4GB",
                           total_ram_gb=15.7, available_ram_gb=8.0,
                           observed_peak_mb=200.0, observed_runs=10)

    assert out["status"] == "overprovisioned_ceiling"
    assert out["ceiling_vs_observed"] == pytest.approx(20.5, abs=0.1)

    joined = " ".join(out["remediation"])
    assert "REVOLUZEN_FLOW_DUCKDB_MEMORY_LIMIT=1GB" in joined
    # The crucial half: it must actively tell the operator NOT to take
    # the concurrency cut the worst case alone would have demanded.
    assert "Leave concurrency where it is" in joined
    assert "REVOLUZEN_FLOW_MAX_CONCURRENT_RUNS" not in joined


def test_a_handful_of_runs_is_an_anecdote_not_a_footprint():
    # Four runs must not be enough to overturn the worst case — an
    # under-sampled p95 that lowers the ceiling is how a host gets OOMed
    # by the fifth run.
    out = _assess_capacity(max_workers=20, per_worker="4GB",
                           total_ram_gb=15.7, available_ram_gb=8.0,
                           observed_peak_mb=200.0, observed_runs=4)
    assert out["status"] == "severely_overcommitted"
    assert "observed_peak_mb" not in out


def test_a_pipeline_that_really_uses_the_ceiling_keeps_the_hard_warning():
    # Measurement cuts both ways: when the workload genuinely approaches
    # its allowance there is no headroom to reclaim, and capping
    # concurrency is once again the right advice.
    out = _assess_capacity(max_workers=20, per_worker="4GB",
                           total_ram_gb=15.7, available_ram_gb=8.0,
                           observed_peak_mb=3500.0, observed_runs=50)
    assert out["status"] == "severely_overcommitted"
    assert "REVOLUZEN_FLOW_MAX_CONCURRENT_RUNS=3" in " ".join(out["remediation"])


def test_low_free_memory_is_raised_to_the_top_of_the_advice():
    out = _assess_capacity(max_workers=20, per_worker="4GB",
                           total_ram_gb=15.7, available_ram_gb=1.2)
    assert "1.2 GB is free right now" in out["remediation"][0]


# ── the rollup itself ──────────────────────────────────────────────────

class _StubStore:
    """Stands in for ExecutionStore.list_all with hand-built rows."""

    def __init__(self, rows):
        self._rows = rows

    def list_all(self, limit=100, workspace_id="default"):
        return [r for r in self._rows
                if r.get("workspace_id", "default") == workspace_id][:limit]


def _run(wf, name, *, mem=None, ms=0.0, status="success", ws="default"):
    """One persisted run. Memory lives in metadata, as the in-process
    executor writes it — the top-level column is only filled by the
    subprocess runner, and a rollup that reads only the column reports
    nothing for a default install."""
    return {
        "id": f"{wf}-{mem}-{ms}-{status}",
        "workflow_id": wf,
        "workflow_name": name,
        "workspace_id": ws,
        "status": status,
        "duration_ms": ms,
        "metadata": {} if mem is None else {"peak_memory_mb": mem},
    }


@pytest.fixture
def footprint_of(monkeypatch):
    from revoluzen_flow import main as flow_main
    from revoluzen_flow.api.pool import _compute_footprint

    def run(rows, **kwargs):
        monkeypatch.setitem(flow_main.app_state, "execution_store",
                            _StubStore(rows))
        return _compute_footprint(kwargs.pop("workspace_id", "default"),
                                  **kwargs)

    return run


def test_p95_is_interpolated_not_just_the_maximum(footprint_of):
    """The regression: `s[int(n * p)]` returns the last element for any
    n <= 20 at p95, so the p95 and max columns printed the same number
    and the percentile was worth nothing at the sample sizes a
    self-hosted install actually has."""
    mems = [100, 100, 100, 100, 100, 100, 100, 100, 100, 900]
    out = footprint_of([_run("wf1", "Heavy", mem=m) for m in mems])

    p = out["pipelines"][0]
    assert p["memory_mb"]["max"] == 900
    assert p["memory_mb"]["p95"] < 900
    assert p["memory_mb"]["p95"] > 100


def test_runs_without_a_memory_reading_are_counted_but_not_averaged(
        footprint_of):
    # "We did not measure it" and "it used nothing" are different
    # claims; a zero in the RAM column would assert the second.
    out = footprint_of([
        _run("wf1", "Partly measured", mem=200),
        _run("wf1", "Partly measured"),
        _run("wf1", "Partly measured"),
    ])
    p = out["pipelines"][0]
    assert p["runs_sampled"] == 3
    assert p["memory_samples"] == 1
    assert p["memory_mb"]["avg"] == 200


def test_a_pipeline_with_no_readings_at_all_reports_no_concurrency(
        footprint_of):
    out = footprint_of([_run("wf1", "Unmeasured") for _ in range(5)])
    p = out["pipelines"][0]
    assert p["memory_samples"] == 0
    # Not 0, not a guess — the question is unanswerable for this one.
    assert p["safe_concurrent_copies"] is None


def test_rollup_is_scoped_to_the_workspace(footprint_of):
    out = footprint_of([
        _run("wf1", "Mine", mem=100, ws="default"),
        _run("wf2", "Theirs", mem=900, ws="other-tenant"),
    ])
    assert [p["name"] for p in out["pipelines"]] == ["Mine"]


def test_errors_are_rated_against_runs_not_against_samples(footprint_of):
    out = footprint_of([
        _run("wf1", "Flaky", mem=100, status="error"),
        _run("wf1", "Flaky", mem=100),
        _run("wf1", "Flaky", mem=100),
        _run("wf1", "Flaky", mem=100),
    ])
    assert out["pipelines"][0]["error_rate_pct"] == 25.0


def test_pipelines_are_ordered_by_what_they_cost(footprint_of):
    out = footprint_of([
        _run("wf1", "Small", mem=50),
        _run("wf2", "Huge", mem=5000),
        _run("wf3", "Medium", mem=500),
    ])
    assert [p["name"] for p in out["pipelines"]] == ["Huge", "Medium", "Small"]


def test_total_demand_is_the_sum_of_the_p95s(footprint_of):
    out = footprint_of([
        _run("wf1", "A", mem=100),
        _run("wf2", "B", mem=250),
    ])
    assert out["all_pipelines_p95_mb"] == pytest.approx(350.0)


def test_a_pipeline_over_the_worker_ceiling_is_called_out(footprint_of,
                                                          monkeypatch):
    from revoluzen_flow import runtime_config
    monkeypatch.setattr(runtime_config, "DUCKDB_MEMORY_LIMIT", "1GB")

    out = footprint_of([
        _run("wf1", "Spiller", mem=4000),
        _run("wf2", "Modest", mem=100),
    ])
    by_name = {p["name"]: p for p in out["pipelines"]}
    assert by_name["Spiller"]["exceeds_worker_limit"] is True
    assert by_name["Modest"]["exceeds_worker_limit"] is False
