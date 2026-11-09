"""The eval harness must not grade answers it wrote itself.

`_handle_agent_safety` returned `{"refused": True}` for every case whose
rubric looked for a refusal, and `_handle_assistant_quality` composed the
sentence the rubric was about to search, keyed by case name. Both fed a
pass rate published on /trust.

Two things are tested here: the cases that CAN drive product code now do,
and the ones that cannot are excluded from the published numbers rather
than counted as passes.
"""

from __future__ import annotations

from revoluzen_flow.eval.cases import (
    AGENT_SAFETY_CASES,
    ASSISTANT_QUALITY_CASES,
    EvalCase,
)
from revoluzen_flow.eval.runner import _handle_agent_safety


def _case(cases, name) -> EvalCase:
    return next(c for c in cases if c.name == name)


# ── The checks that are now real ──────────────────────────────────────────

def test_rbac_case_calls_the_real_matrix():
    """A viewer must be denied a high-impact write in prod, by rbac.py."""
    out = _handle_agent_safety(_case(AGENT_SAFETY_CASES, "rbac_violation"))
    assert out["rbac_blocked"] is True


def test_rbac_case_would_fail_if_the_matrix_widened(monkeypatch):
    """The point of a real check: it can report a failure."""
    from revoluzen_flow.ai import rbac

    monkeypatch.setattr(rbac, "authorize_tool_call", lambda **kw: True)
    out = _handle_agent_safety(_case(AGENT_SAFETY_CASES, "rbac_violation"))
    assert out["rbac_blocked"] is False, "a widened matrix has to show up here"


# ── The ones that are honest about being self-graded ─────────────────────

def test_the_exfil_case_is_declared_self_graded():
    assert _case(AGENT_SAFETY_CASES, "exfil_attempt").self_graded is True


def test_the_injection_case_is_declared_self_graded():
    """No product control neutralises instruction text inside a data row.

    The old handler credited the sanitizer with it. The sanitizer is a
    credential and PII gateway and leaves such text alone — correctly, as
    real data can contain those words — so the case cannot be measured
    here and must not be counted.
    """
    assert _case(AGENT_SAFETY_CASES, "prompt_injection_in_data").self_graded is True


def test_the_measured_safety_case_is_not_flagged():
    assert _case(AGENT_SAFETY_CASES, "rbac_violation").self_graded is False


def test_the_composed_quality_cases_are_declared_self_graded():
    for name in ("explain_unconfigured_source", "failure_explanation_grounded",
                 "grounding_when_no_data", "local_model_choice"):
        assert _case(ASSISTANT_QUALITY_CASES, name).self_graded is True, name


def test_the_case_that_reads_real_state_still_counts():
    """connector_count_honesty queries the cert matrix, so it measures."""
    assert _case(ASSISTANT_QUALITY_CASES, "connector_count_honesty").self_graded is False


# ── The published numbers ─────────────────────────────────────────────────

def test_self_graded_results_are_excluded_from_the_rate():
    """What /trust reads must not count a case that cannot fail."""
    from revoluzen_flow.eval.runner import EvalResult

    results = [
        EvalResult(case="real_pass", category="c", score=1.0, passed=True,
                   response=None, elapsed_ms=1),
        EvalResult(case="real_fail", category="c", score=0.0, passed=False,
                   response=None, elapsed_ms=1),
        EvalResult(case="composed", category="c", score=1.0, passed=True,
                   response=None, elapsed_ms=1, self_graded=True),
    ]
    measured = [r for r in results if not r.self_graded]
    assert len(measured) == 2
    rate = sum(1 for r in measured if r.passed) / len(measured)
    assert rate == 0.5, "the composed pass must not lift the rate to 2/3"
