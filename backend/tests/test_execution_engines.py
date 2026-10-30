"""Tests for the execution-engine protocol and registry.

The protocol is OSS and the maintained Beam/Spark engines are Enterprise
(edition-matrix.md), so what matters here is that the socket behaves
predictably for a plugin this repo cannot see: registration is explicit,
an unknown engine is an error rather than a silent downgrade, and an
engine can say what it cannot run BEFORE a run starts.
"""

from __future__ import annotations

import pytest

from revoluzen_flow.engine.engines import (
    DEFAULT_ENGINE_ID,
    DuckDBEngine,
    EngineCapabilities,
    EngineNotFoundError,
    EnginePlan,
    ExecutionEngine,
    get_engine,
    list_engines,
    register_engine,
)
from revoluzen_flow.ir.schema import Step, StepType, Workflow


@pytest.fixture
def registry_guard():
    """Restore the process-wide registry so tests cannot leak engines."""
    from revoluzen_flow.engine import engines as mod

    before = dict(mod._ENGINES)
    yield
    mod._ENGINES.clear()
    mod._ENGINES.update(before)


class _FakeEngine:
    """A stand-in for an Enterprise engine — runs nothing, covers little."""

    engine_id = "fake"
    display_name = "Fake"

    def __init__(self, unsupported=("http_request",)):
        self._unsupported = set(unsupported)
        self.ran = False

    def capabilities(self):
        return EngineCapabilities(distributed=True, streaming=True,
                                  supports_preview=False)

    def supports(self, step_type):
        if step_type in self._unsupported:
            return False, f"no distributed equivalent for {step_type}"
        return True, ""

    def plan(self, workflow):
        plan = EnginePlan(engine_id=self.engine_id)
        for s in workflow.steps:
            st = getattr(s.type, "value", str(s.type))
            ok, why = self.supports(st)
            (plan.supported.append(s.id) if ok
             else plan.unsupported.append((s.id, why)))
        return plan

    def execute_workflow(self, workflow, **kwargs):
        self.ran = True
        return "ran"


def _wf(*types: StepType) -> Workflow:
    return Workflow(
        id="wf-engines",
        name="engines",
        steps=[Step(id=f"s{i}", type=t, params={}) for i, t in enumerate(types)],
        connections=[],
    )


# ── the default ──────────────────────────────────────────────────────

def test_duckdb_is_registered_by_default():
    e = get_engine()
    assert e.engine_id == DEFAULT_ENGINE_ID
    assert isinstance(e, DuckDBEngine)


def test_duckdb_satisfies_the_protocol():
    # runtime_checkable only verifies method presence, which is exactly
    # the guarantee a plugin author needs from the published Protocol.
    assert isinstance(DuckDBEngine(), ExecutionEngine)


def test_duckdb_is_not_distributed_and_says_so():
    caps = DuckDBEngine().capabilities()
    assert caps.distributed is False
    assert caps.supports_preview is True


def test_duckdb_supports_a_real_registered_node():
    ok, why = DuckDBEngine().supports(StepType.FILTER.value)
    assert ok, why


def test_duckdb_rejects_an_unknown_step_type():
    ok, why = DuckDBEngine().supports("not_a_real_node")
    assert not ok
    assert "not_a_real_node" in why


# ── registry behaviour ───────────────────────────────────────────────

def test_unknown_engine_raises_rather_than_falling_back(registry_guard):
    # The important half: silently running a pipeline authored for a
    # cluster on one in-process node would look like success and behave
    # like a capacity incident.
    with pytest.raises(EngineNotFoundError) as exc:
        get_engine("beam")
    assert "beam" in str(exc.value)
    assert DEFAULT_ENGINE_ID in str(exc.value)  # names what IS available


def test_registering_a_duplicate_id_is_refused(registry_guard):
    register_engine(_FakeEngine())
    with pytest.raises(ValueError, match="already registered"):
        register_engine(_FakeEngine())


def test_duplicate_can_be_replaced_explicitly(registry_guard):
    first = _FakeEngine()
    register_engine(first)
    second = _FakeEngine()
    register_engine(second, replace=True)
    assert get_engine("fake") is second


def test_engine_without_an_id_is_refused(registry_guard):
    class Nameless:
        engine_id = ""

    with pytest.raises(ValueError, match="engine_id"):
        register_engine(Nameless())


def test_a_third_party_engine_becomes_reachable(registry_guard):
    register_engine(_FakeEngine())
    assert get_engine("fake").display_name == "Fake"
    assert {e.engine_id for e in list_engines()} >= {DEFAULT_ENGINE_ID, "fake"}


# ── coverage reporting ───────────────────────────────────────────────

def test_plan_reports_runnable_when_every_step_is_covered(registry_guard):
    plan = _FakeEngine(unsupported=()).plan(_wf(StepType.FILTER, StepType.JOIN))
    assert plan.runnable
    assert len(plan.supported) == 2
    assert "all 2 steps supported" in plan.describe()


def test_plan_names_the_steps_an_engine_cannot_run(registry_guard):
    # http_request is the realistic shape of this: a side-effecting node
    # with no distributed equivalent, sitting in an otherwise portable
    # pipeline.
    plan = _FakeEngine(unsupported=("http_request",)).plan(
        _wf(StepType.FILTER, StepType.HTTP_REQUEST))
    assert not plan.runnable
    assert [sid for sid, _ in plan.unsupported] == ["s1"]
    # The operator has to be able to act on this, so the reason travels
    # with it rather than a bare boolean.
    assert "no distributed equivalent for http_request" in plan.describe()


def test_plan_does_not_execute(registry_guard):
    engine = _FakeEngine()
    engine.plan(_wf(StepType.FILTER))
    assert engine.ran is False, "plan() must answer the coverage question only"


def test_duckdb_plan_covers_a_normal_pipeline():
    plan = DuckDBEngine().plan(_wf(StepType.FILTER, StepType.JOIN, StepType.AGGREGATE))
    assert plan.runnable, plan.describe()
