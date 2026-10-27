"""Sample runs must not write.

"Sample" differed from a full run by a row cap and nothing else: it wrote
to every sink, sent every email and fired every HTTP request. The UI even
told the operator to go and deactivate their sinks by hand "if you only
want to preview the data path" — which is what a sample run is for.

The machinery to prevent that already existed (`ctx.preview_mode`, and the
R8b short-circuit in `executor._run_node_once`); the Sample button simply
never asked for it, because `safety_mode="sample"` routed to the sandbox
hooks, which are no-ops outside the extension build.

These assert the behaviour end to end: a destination writes nothing, the
transforms still run so the preview has data, and the row cap is real.
"""
from __future__ import annotations

import os
import sys

import duckdb
import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from revoluzen_flow.api.execution import _sample_kwargs  # noqa: E402
from revoluzen_flow.common.duckdb_compat import fetch_scalar  # noqa: E402
from revoluzen_flow.nodes.base import ExecutionContext  # noqa: E402
from revoluzen_flow.nodes.guardrails import cap_rows  # noqa: E402


class TestSafetyModeMapping:
    def test_sample_asks_for_preview_and_a_cap(self):
        # Without the sandbox extension (the OSS case) sample must fall
        # back to preview mode. Asking for a sandbox namespace here bought
        # nothing: the rewrite hooks are no-ops, so the sinks wrote to
        # their real destinations.
        assert _sample_kwargs('sample', 'wf1', 100) == {
            'preview_mode': True, 'sample_row_limit': 100,
        }

    def test_live_asks_for_nothing(self):
        assert _sample_kwargs('live', 'wf1', 100) == {}

    def test_sandbox_build_keeps_using_the_sandbox(self, monkeypatch):
        # With a real extension the sandbox is the better preview — the
        # write happens, just into a scratch namespace — so it must win.
        import revoluzen_flow.engine.executor as ex
        monkeypatch.setattr(ex.rewrite_for_sandbox, 'is_default_noop', False, raising=False)
        monkeypatch.setattr(ex.inject_sandbox_limit, 'is_default_noop', False, raising=False)
        assert _sample_kwargs('sample', 'wf1', 100) == {
            'sandbox_namespace': 'sample_wf1', 'sandbox_row_limit': 100,
        }


class TestTheRowCapIsReal:
    """cap_rows' own sample limit is 1,000,000 AND gated on IS_DEV, so it
    never fired for a file of ordinary size. An explicit cap must."""

    def _rel(self, n=5000):
        return duckdb.connect(':memory:').sql(f'SELECT * FROM range({n}) t(i)')

    def _count(self, relation):
        # `fetch_scalar`, not `.fetchone()[0]`. DuckDB 2.0 removed
        # `Relation.fetchone()` and `duckdb>=1.2.0` has no upper bound, so
        # CI resolves 2.x and these three asserts raised AttributeError
        # while passing locally on 1.x. The production call sites were
        # migrated to the compat helper; these tests were missed.
        return fetch_scalar(relation.count('*'))

    def test_explicit_cap_applies(self):
        assert self._count(cap_rows(self._rel(), label='CSV', full_run=False, limit=100)) == 100

    def test_without_a_cap_a_small_file_is_read_whole(self):
        # Documents the old behaviour that made Sample meaningless on
        # files: 5,000 rows is far under DEV_SAMPLE_ROWS, so nothing was
        # limited and Sample read exactly as much as Run.
        assert self._count(cap_rows(self._rel(), label='CSV', full_run=False)) == 5000

    def test_full_run_ignores_the_cap(self):
        assert self._count(cap_rows(self._rel(), label='CSV', full_run=True, limit=100)) == 5000


class TestContextCarriesTheFlags:
    def test_defaults_are_off(self):
        ctx = ExecutionContext(conn=duckdb.connect(':memory:'))
        assert ctx.preview_mode is False
        assert ctx.sample_row_limit is None

    def test_preview_run_sets_both(self):
        ctx = ExecutionContext(
            conn=duckdb.connect(':memory:'), preview_mode=True, sample_row_limit=100,
        )
        assert ctx.preview_mode is True
        assert ctx.sample_row_limit == 100


class TestASampleRunDoesNotWrite:
    """The point of the whole change, exercised through the real executor."""

    def _workflow(self, out_path: str):
        from revoluzen_flow.ir.schema import Workflow, Step, StepConnection, NodePosition
        wf = Workflow(name='sample-check', project_id='default', workspace_id='default')
        # db_source with no connection_id runs the query against the
        # run's own in-memory DuckDB — a source with no external
        # dependency, which is what this test needs.
        wf.steps.append(Step(
            id='src', type='db_source', label='src',
            params={'query': 'SELECT 1 AS id, 2 AS n'},
            position=NodePosition(x=0, y=0),
        ))
        wf.steps.append(Step(
            id='sink', type='csv_sink', label='write',
            params={'file_path': out_path},
            position=NodePosition(x=200, y=0),
        ))
        wf.connections.append(StepConnection(from_step='src', to_step='sink', alias='src'))
        return wf

    def _run(self, tmp_path, **kwargs):
        from revoluzen_flow.engine.executor import WorkflowExecutor
        out = os.path.join(str(tmp_path), 'out.csv')
        wf = self._workflow(out)
        result = WorkflowExecutor(data_dir=str(tmp_path)).execute_workflow(wf, **kwargs)
        return out, result

    def test_live_run_writes_the_file(self, tmp_path):
        out, result = self._run(tmp_path)
        assert result.step_results['src'].status == 'success'
        assert os.path.exists(out), 'a normal run must still write'

    def test_sample_run_writes_nothing(self, tmp_path):
        out, result = self._run(tmp_path, preview_mode=True, sample_row_limit=100)
        assert not os.path.exists(out), (
            'preview_mode must not write to the destination — this is the '
            'whole promise of a Sample run'
        )

    def test_sample_run_still_produces_upstream_data(self, tmp_path):
        # A preview that skipped the transforms too would be useless: the
        # operator runs Sample precisely to see each step's output.
        _, result = self._run(tmp_path, preview_mode=True, sample_row_limit=100)
        src = result.step_results['src']
        assert src.status == 'success'
        assert src.row_count == 1

    def test_the_skip_is_reported_not_silent(self, tmp_path):
        # A short-circuited sink still reports `success` with its
        # passthrough rows, so without this marker the operator cannot
        # tell a write that happened from one that was skipped.
        _, result = self._run(tmp_path, preview_mode=True, sample_row_limit=100)
        assert result.step_results['sink'].skipped_reason == 'sample'
        assert result.step_results['src'].skipped_reason is None, (
            'a pure source has no side effect to skip'
        )

    def test_a_live_run_marks_nothing_as_skipped(self, tmp_path):
        _, result = self._run(tmp_path)
        assert result.step_results['sink'].skipped_reason is None

    def test_the_cap_reaches_the_source(self, tmp_path):
        # db_source applies its own 1,000-row default; a Sample run's
        # explicit cap has to win, or the run reads ten times what the
        # API said it would.
        from revoluzen_flow.engine.executor import WorkflowExecutor
        from revoluzen_flow.ir.schema import Workflow, Step, NodePosition
        wf = Workflow(name='cap', project_id='default', workspace_id='default')
        wf.steps.append(Step(
            id='src', type='db_source', label='src',
            params={'query': 'SELECT i AS id FROM range(5000) t(i)'},
            position=NodePosition(x=0, y=0),
        ))
        result = WorkflowExecutor(data_dir=str(tmp_path)).execute_workflow(
            wf, preview_mode=True, sample_row_limit=100,
        )
        assert result.step_results['src'].row_count == 100

    def test_the_sink_step_is_still_reported(self, tmp_path):
        # Skipping must be visible. A sink that silently vanished from the
        # run would leave the operator unsure whether it had written.
        _, result = self._run(tmp_path, preview_mode=True, sample_row_limit=100)
        assert 'sink' in result.step_results
