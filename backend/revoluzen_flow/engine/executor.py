"""
Workflow Executor — runs a validated IR through DuckDB.

Topologically sorts steps, executes each via its registered node,
and collects results with preview data.
"""

from __future__ import annotations

import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable

# Stage 2.5b: duckdb is RUNTIME-USED in this file (_open_duckdb calls
# duckdb.connect). The runtime import lives inside that function so a
# caller who imports the module but never runs a workflow doesn't pay
# the duckdb load cost. TYPE_CHECKING keeps annotations resolvable.
if TYPE_CHECKING:
    import duckdb

from revoluzen_flow.common.duckdb_compat import fetch_one, fetch_scalar
from revoluzen_flow import runtime_config
from revoluzen_flow.ir.schema import (
    Workflow, StepRunResult, StepErrorType, WorkflowRunResult, Step, StepType,
)


def classify_error(exc: BaseException | str | None) -> StepErrorType:
    """Bucket a raised exception (or error string) into ``StepErrorType``.

    PR 7. Cheap pattern-match against the exception type / message so the
    UI can render a useful "Failure Reason" badge without every node
    annotating its raises. Nodes that DO know their error category can
    still set ``error_type`` explicitly on the StepRunResult they
    populate — that takes precedence (this helper is only called when
    error_type wasn't already set).
    """
    if exc is None:
        return StepErrorType.UNKNOWN
    name = type(exc).__name__.lower() if isinstance(exc, BaseException) else "string"
    msg = str(exc).lower()

    # Timeouts — watchdog-fired or driver-level.
    if "timeout" in msg or name.endswith("timeout") or "timed out" in msg:
        return StepErrorType.TIMEOUT
    # Memory pressure from DuckDB or below.
    if "out of memory" in msg or "memory_limit" in msg or "exceeded memory" in msg:
        return StepErrorType.DUCKDB_OOM
    # Auth — 401/403, "unauthorized", expired tokens.
    if (
        "401" in msg or "403" in msg
        or "unauthorized" in msg or "forbidden" in msg
        or "credential" in msg or "expired" in msg
        or "auth" in msg and "failed" in msg
    ):
        return StepErrorType.CREDENTIAL_EXPIRED
    # Network — connection refused, DNS, TLS, generic httpx/urllib errors.
    if (
        "connection" in msg or "dns" in msg or "tls" in msg or "ssl" in msg
        or "connectionerror" in name or "timeout" in name
        or "name or service" in msg or "host" in msg and "unreach" in msg
    ):
        return StepErrorType.NETWORK_ERROR
    # User config — missing required fields, bad enum values, etc.
    if (
        "required" in msg or "missing" in msg
        or "invalid" in msg or "not allowed" in msg
        or name in {"valueerror", "keyerror", "validationerror", "typeerror"}
    ):
        return StepErrorType.INVALID_CONFIG
    return StepErrorType.UNKNOWN
from revoluzen_flow.ir.validator import validate_workflow
from revoluzen_flow.nodes.base import ExecutionContext
from revoluzen_flow.nodes.registry import get_registry
from revoluzen_flow.engine.preview import preview_relation
from revoluzen_flow.engine.step_cache import StepCache
from revoluzen_flow.engine.checkpoint_store import get_checkpoint_store
from revoluzen_flow.expression.resolver import resolve_expressions, ExpressionError

# Sandbox-isolation hooks. Defaults are inert no-ops; an optional
# extension may override these symbols at runtime.
SANDBOX_DEFAULT_ROW_LIMIT = 100_000


class SandboxIsolationError(Exception):
    """Raised when a sandbox-isolation check fails."""


def inject_sandbox_limit(*_args, **_kwargs):
    """Default no-op."""


def rewrite_for_sandbox(*_args, **_kwargs):
    """Default no-op — returns None to signal no rewrite was applied."""
    return None


# Both hooks above are replaced by an optional extension. Callers need to
# know which they have, because the difference is not cosmetic: with the
# extension, a sample run rewrites every sink to a scratch namespace and
# genuinely exercises the write; without it, the hooks rewrite NOTHING, so
# asking for `sandbox_namespace` buys no isolation at all and the run
# writes to the real destinations. That is exactly what `safety_mode=
# "sample"` did in OSS — it set a sandbox namespace and a 100-row limit,
# both of which the no-ops discarded, so "Sample" wrote to production
# sinks and read every row.
inject_sandbox_limit.is_default_noop = True
rewrite_for_sandbox.is_default_noop = True


def sandbox_rewrites_available() -> bool:
    """True when a real sandbox extension has replaced the no-op hooks."""
    return not (
        getattr(inject_sandbox_limit, "is_default_noop", False)
        and getattr(rewrite_for_sandbox, "is_default_noop", False)
    )

logger = logging.getLogger(__name__)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _has_template(value: Any) -> bool:
    """True if a params tree contains a ``{{ ... }}`` expression anywhere.

    Deliberately cheap and conservative: it walks strings, lists and dicts
    and stops at the first ``{{``. A false positive only costs the old
    behaviour for that one step; a false negative would skip resolution, so
    the check is on the raw text rather than anything cleverer.

    Used to avoid snapshotting every upstream result into Python when no
    step param needs it — see the call site in ``_execute_step``.
    """
    if isinstance(value, str):
        return "{{" in value
    if isinstance(value, dict):
        return any(_has_template(v) for v in value.values())
    if isinstance(value, (list, tuple, set)):
        return any(_has_template(v) for v in value)
    return False


def _open_duckdb(workflow=None, data_dir: str | None = None) -> duckdb.DuckDBPyConnection:
    """Open an in-memory DuckDB connection with runtime guardrails applied.

    Why this wrapper exists: a single runaway GROUP BY or JOIN can hash
    itself into gigabytes of RAM on DuckDB's default settings, and because
    Revoluzen Flow runs embedded inside the FastAPI process, OOM means the whole
    backend dies — login, API, scheduler, everything. Setting memory_limit
    forces DuckDB to spill intermediate state to disk (``temp_directory``)
    instead of allocating forever, which is slower but survivable.

    We swallow errors from SET statements because older DuckDB builds may
    not know every pragma; a missing pragma must not stop the pipeline.
    """
    import duckdb  # method-scoped (Stage 2.5b)
    conn = duckdb.connect(":memory:")
    try:
        os.makedirs(runtime_config.DUCKDB_TEMP_DIRECTORY, exist_ok=True)
    except OSError as exc:
        logger.warning(
            "executor: could not create duckdb temp dir %s (%s) — "
            "DuckDB will fall back to system temp",
            runtime_config.DUCKDB_TEMP_DIRECTORY, exc,
        )
    try:
        conn.execute(f"SET memory_limit='{runtime_config.DUCKDB_MEMORY_LIMIT}'")
    except Exception as exc:  # noqa: BLE001 — DuckDB version drift
        logger.warning("executor: could not set memory_limit (%s)", exc)
    try:
        conn.execute(
            f"SET temp_directory='{runtime_config.DUCKDB_TEMP_DIRECTORY}'"
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("executor: could not set temp_directory (%s)", exc)

    # Thread cap — prevent DuckDB from stealing every core on the box.
    # 0 means "let DuckDB decide" (dev default). In prod we cap at half
    # the cores so FastAPI / scheduler / WebSocket stay responsive.
    if runtime_config.DUCKDB_THREADS > 0:
        try:
            conn.execute(f"SET threads={runtime_config.DUCKDB_THREADS}")
        except Exception as exc:  # noqa: BLE001
            logger.warning("executor: could not set threads (%s)", exc)

    # Disable insertion-order preservation — lets DuckDB parallelise scans
    # on Parquet/CSV without maintaining row order.  Order is irrelevant
    # for analytical transforms (GROUP BY, JOIN, DISTINCT).
    try:
        _order = "true" if runtime_config.DUCKDB_PRESERVE_ORDER else "false"
        conn.execute(f"SET preserve_insertion_order={_order}")
    except Exception as exc:  # noqa: BLE001
        logger.warning("executor: could not set preserve_insertion_order (%s)", exc)

    # File-system sandbox for pipeline SQL (on in server mode): the pipeline's
    # own files stay readable, the master key and the app database do not.
    # Applied last, after every SET above, because it locks the configuration.
    from revoluzen_flow.security import sql_sandbox
    sql_sandbox.apply(conn, workflow, data_dir)
    return conn


def _relation_to_schema(rel) -> dict:
    """Distil a DuckDB relation into a wire-friendly {columns: [...]} dict.

    Used by ``WorkflowExecutor.get_step_input_schema`` — every value is
    JSON-serialisable so the frontend can render type chips without a
    second round-trip.
    """
    if rel is None:
        return {"columns": []}
    try:
        cols = list(rel.columns)
        types = [str(t) for t in rel.types]
    except Exception:  # noqa: BLE001 — defensive: relation can be in any state
        return {"columns": []}
    return {
        "columns": [
            {"name": c, "type": t}
            for c, t in zip(cols, types)
        ],
    }


class WorkflowExecutor:
    """Execute a workflow IR using DuckDB."""

    # Default cap on TOTAL retry attempts across all steps in a single
    # workflow run. Prevents a flapping pipeline from re-trying forever
    # when many steps each have generous per-step retries (e.g. 12 steps
    # × 3 retries each = 36 retry attempts in the worst case).
    # Override via WorkflowExecutor.workflow_retry_budget on the instance,
    # or per-run via the corresponding Workflow IR field if/when added.
    DEFAULT_WORKFLOW_RETRY_BUDGET = 10

    def __init__(
        self,
        data_dir: str = ".",
        app_state: dict | None = None,
        on_event: "Callable[[dict[str, Any]], None] | None" = None,
    ):
        self.data_dir = data_dir
        self.app_state = app_state or {}
        # 2026-10-25 — optional per-step progress hook.
        #
        # Live run progress existed only on the WebSocket-initiated path,
        # through the separate RealtimeExecutor. The Run button goes
        # through REST and this executor, so nothing ever reported a step
        # starting or finishing: the canvas sat idle for the whole run and
        # then showed the results all at once. Instrumented at 60ms, zero
        # visual transitions on any node.
        #
        # Putting the hook HERE rather than swapping the REST path over to
        # RealtimeExecutor is deliberate: that class is not a subclass, it
        # is a parallel implementation, and it does not carry this one's
        # caching, retry-budget or resume behaviour. One hook on the real
        # execution path also means the scheduler and CLI stream too,
        # rather than just the one caller that asked for it.
        #
        # Never let a listener break a run: `_emit` swallows everything.
        self.on_event = on_event
        self.registry = get_registry()
        # Populated per-run by execute_workflow / execute_step / execute_step_resume
        self._cache: StepCache | None = None
        self._effective_hashes: dict[str, str] = {}
        # What each side-effecting step did to its destination this run.
        # Declared here so the single-step entry points, which never go
        # through execute_workflow's reset, still have a list to append to.
        self._blast_radius: list[Any] = []
        self._cached_step_ids: set[str] = set()
        # Workflow-level retry tracking — counts total retry attempts across
        # ALL steps in the current run. Reset at the start of each
        # execute_workflow call. When the count reaches workflow_retry_budget,
        # _run_step_with_settings stops retrying and falls through to the
        # on_error policy as if the step exhausted its own budget.
        self.workflow_retry_budget = self.DEFAULT_WORKFLOW_RETRY_BUDGET
        self._workflow_retries_used = 0
        # The running workflow's time limit, when it has one, and its
        # cancellation token. Set and cleared by execute_workflow; the step
        # retry loop reads the token to stop retrying a stopped run.
        self._run_limit = None
        self._run_token = None

    def _emit(self, event_type: str, data: dict[str, Any]) -> None:
        """Report progress to an optional listener.

        Deliberately total: a listener that raises, blocks on a full
        queue, or was handed a half-dead WebSocket must not take a
        pipeline run down with it. Observability is never worth a failed
        run, and a run that fails only when someone is watching it is the
        worst kind of bug to chase.
        """
        cb = self.on_event
        if cb is None:
            return
        try:
            cb({"type": event_type, "timestamp": _utc_now_iso(), **data})
        except Exception:  # noqa: BLE001 — see docstring
            logger.debug("on_event listener raised for %s", event_type, exc_info=True)

    def execute_workflow(
        self,
        workflow: Workflow,
        preview_limit: int = 50,
        full_run: bool = False,
        sandbox_namespace: str | None = None,
        sandbox_row_limit: int = SANDBOX_DEFAULT_ROW_LIMIT,
        parameter_values: dict | None = None,
        run_id: str | None = None,
        resume_from_run_id: str | None = None,
        execution_code: str | None = None,
        preview_mode: bool = False,
        sample_row_limit: int | None = None,
        time_limit_seconds: float | None = None,
    ) -> WorkflowRunResult:
        """Execute all steps in topological order.

        A run is stopped and marked failed once it has gone on longer than
        its time limit: the pipeline's own "Maximum Running Time"
        (``metadata.execution_settings``) or ``time_limit_seconds``, whichever
        is shorter. A published endpoint passes its timeout here. See
        ``engine/run_time_limit.py``.

        When ``full_run`` is False (default), source nodes limit to
        ``DEV_SAMPLE_ROWS`` in dev mode for fast iteration.  The frontend
        "Run Full" button passes ``full_run=True`` to load everything.

        ``preview_mode`` makes the run a PREVIEW: the transforms execute
        so every step has inspectable output, but any node with an
        external side effect short-circuits instead of firing it — see
        the R8b block in ``_run_node_once``. This is what the editor's
        Sample button passes (``safety_mode=sample``). Before it was
        wired up, Sample differed from a full run only by a row cap: it
        still wrote to every sink, sent every email and fired every HTTP
        request, and the UI had to tell the operator to go and deactivate
        those nodes by hand before previewing anything.

        Not applied when ``sandbox_namespace`` is set: that path already
        rewrites every sink to a scratch namespace, which exercises the
        write safely and is strictly better than not running it.

        ``sandbox_namespace`` switches the run into PROD-Sandbox mode (PR10):
        every destination is rewritten to point at the scratch namespace
        and every source gets a row-limit hint capped at ``sandbox_row_limit``.
        See ``DESIGN_PROD_SANDBOX.md`` for the load-bearing invariants.
        Default ``None`` = normal DEV/PROD execution; nothing rewritten.
        """
        # ── One-time execution-code gate (Phase 7) ──
        # No-op unless REVOLUZEN_FLOW_REQUIRE_EXECUTION_CODE is set. When on, reaching
        # the executor requires a fresh, single-use, run-bound code minted by
        # an authorized initiation path — so a stolen token can't drive the
        # executor directly. Failure mirrors the capability/param gates below:
        # one synthetic step records the cause for the run-history UI.
        # The pre-commit contract gate runs deep inside _run_node_once,
        # where only the step is in scope. ExecutionContext has no
        # workflow_id and widening its signature would touch every node,
        # so the id is parked on the executor for the duration of the run.
        self._workflow_id = workflow.id
        # Reset per run, for the same reason: the gate appends to it from
        # deep in the step loop, and a list carried over from the last
        # run would attribute the previous run's damage to this receipt.
        self._blast_radius = []
        # What this run was asked to run, fingerprinted before the run adds
        # its own wiring to the step params. The contract receipt records it,
        # and the publish gate uses it to find the verdict on exactly this
        # definition (contracts/gate.py).
        try:
            from revoluzen_flow.ir.versioning import definition_hash as _definition_hash
            self._definition_hash = _definition_hash(workflow)
        except Exception:  # noqa: BLE001 — a fingerprint never stops a run
            self._definition_hash = None

        try:
            from revoluzen_flow.security.execution_codes import enforce_execution_code
            enforce_execution_code(
                execution_code,
                workspace_id=getattr(workflow, "workspace_id", None) or "default",
                pipeline_id=workflow.id,
                action="run",
            )
        except PermissionError as _auth_exc:
            return WorkflowRunResult(
                workflow_id=workflow.id,
                status="error",
                step_results={
                    "authorization": StepRunResult(
                        step_id="authorization",
                        status="error",
                        error=str(_auth_exc),
                        error_type=StepErrorType.INVALID_CONFIG,
                    )
                },
            )

        # ── Runtime capability gate (audit I1 — 2026-05-22) ──
        # validate_capabilities() already runs at save time, but the
        # connection's capability list can change between save and
        # run (admin revokes write, capabilities edited, etc.). Run
        # the same gate at dispatch time so a source step pointed at
        # a now-write-only connection raises before any IO happens.
        # Failure mode mirrors the sandbox-rewrite branch below —
        # one synthetic step records the cause for the run-history UI.
        try:
            from revoluzen_flow.ir.validator import validate_capabilities as _vc
            from revoluzen_flow.main import app_state
            conn_store = app_state.get("connection_store")
            if conn_store is not None:
                workspace_id = getattr(workflow, "workspace_id", None) or "default"
                cap_errors = _vc(
                    workflow,
                    lambda cid: conn_store.get(cid, workspace_id=workspace_id),
                )
                hard = [e for e in cap_errors if e.severity == "error"]
                if hard:
                    return WorkflowRunResult(
                        workflow_id=workflow.id,
                        status="error",
                        step_results={
                            "capability_check": StepRunResult(
                                step_id="capability_check",
                                status="error",
                                error="; ".join(e.message for e in hard[:3]),
                                error_type=StepErrorType.INVALID_CONFIG,
                            )
                        },
                    )
        except Exception:
            # Best-effort — never block a run because the gate itself
            # failed (e.g. test environment without a real store). Save-
            # time validation already happened; this is a defence layer.
            pass

        # ── PROD Sandbox rewrite (PR10 step 3) ──
        # Apply isolation hooks BEFORE any other processing. If the
        # rewrite raises (unknown sink connector, unsafe destination),
        # fail the whole run with a clear error rather than executing
        # part of the graph against real prod targets.
        if sandbox_namespace is not None:
            try:
                workflow = self._apply_sandbox_rewrites(
                    workflow, sandbox_namespace, sandbox_row_limit,
                )
            except SandboxIsolationError as exc:
                return WorkflowRunResult(
                    workflow_id=workflow.id,
                    status="error",
                    step_results={
                        "sandbox_isolation": StepRunResult(
                            step_id="sandbox_isolation",
                            status="error",
                            error=f"Sandbox refused: {exc}",
                            error_type=StepErrorType.INVALID_CONFIG,
                        )
                    },
                )

        # ── Parameter resolution ──────────────────────────────────────────
        # Substitute ${param.<name>} placeholders in step params with the
        # resolved values from `parameter_values` (caller override) or each
        # parameter's default. Failures fall through to the placeholder text
        # so step-level validation can flag the bad reference.
        if (workflow.parameters or parameter_values is not None):
            try:
                from revoluzen_flow.engine.parameters import resolve_workflow_parameters
                workflow = resolve_workflow_parameters(workflow, parameter_values or {})
            except Exception as e:
                return WorkflowRunResult(
                    workflow_id=workflow.id,
                    status="error",
                    step_results={
                        "parameter_resolution": StepRunResult(
                            step_id="parameter_resolution",
                            status="error",
                            error=f"Parameter resolution failed: {e}",
                            error_type=StepErrorType.INVALID_CONFIG,
                        )
                    },
                )

        self._workflow_ref = workflow
        errors = validate_workflow(workflow)
        if any(e.severity == "error" for e in errors):
            return WorkflowRunResult(
                workflow_id=workflow.id,
                status="error",
                step_results={
                    "validation": StepRunResult(
                        step_id="validation",
                        status="error",
                        error="; ".join(e.message for e in errors),
                        error_type=StepErrorType.VALIDATION_FAILED,
                    )
                },
            )

        run_result = WorkflowRunResult(
            workflow_id=workflow.id,
            status="running",
        )

        # Generate run_id eagerly so we can pass it to ExecutionContext.
        # Connection pool (if installed in app_state) uses this to scope
        # cached driver connections to the current run.
        if not run_id:
            import uuid as _uuid
            run_id = _uuid.uuid4().hex[:12]

        conn = _open_duckdb(workflow, self.data_dir)
        # A sandbox run already redirects every sink to a scratch
        # namespace, so there is nothing to protect the operator from and
        # skipping would only make the preview less faithful.
        preview_mode = bool(preview_mode) and sandbox_namespace is None
        # A run that writes to real destinations reads everything. Capping is
        # for runs that don't write; scheduled runs, backfills and Replay never
        # asked for a full run, so they used to write partial data.
        full_run = bool(full_run) or (not preview_mode and sandbox_namespace is None)
        ctx = ExecutionContext(
            conn=conn, data_dir=self.data_dir, full_run=full_run,
            app_state=self.app_state, run_id=run_id,
            preview_mode=preview_mode,
            sample_row_limit=sample_row_limit,
        )
        ctx.sandboxed = sandbox_namespace is not None
        ctx.node_labels = {s.id: (s.label or s.type.value) for s in workflow.steps}
        # Z33 (2026-05-23) — expose workflow id + scaffold metadata +
        # per-step params to nodes (so a sink can read its upstream
        # Wrangler's recipe without us threading the full IR object).
        # Test paths that build the context directly skip this and the
        # nodes fall back to default behavior.
        ctx.workflow_id = getattr(workflow, "id", None)
        ctx._workflow_ref = workflow
        ctx.workflow_metadata = dict(getattr(workflow, "metadata", {}) or {})
        ctx.step_params = {s.id: dict(getattr(s, "params", {}) or {}) for s in workflow.steps}

        # Reset workflow-level retry budget at the start of each run. The
        # counter is shared across every step's retry loop (see
        # `_run_step_with_settings`) so a flapping pipeline can't burn
        # unbounded retry attempts even if individual steps have generous
        # per-step max_retries values.
        self._workflow_retries_used = 0

        # Build execution order (topological sort)
        order = self._topological_sort(workflow)

        # Build input map: step_id -> list of source step_ids
        input_map = self._build_input_map(workflow)

        # Set up per-workflow step cache so each success writes a parquet
        # checkpoint the "Rerun from here" feature can read later.
        self._stamp_shared_rule_sets(workflow)
        self._cache = StepCache(self.data_dir, workflow.id)
        self._effective_hashes = self._cache.compute_effective_hashes(workflow, input_map)
        self._cached_step_ids = set()

        # Sprint 1 / Gate 1: persist per-step outcomes to pipeline_checkpoints
        # so a failed run can be resumed from the first non-success step.
        # Independent of StepCache (which is content-hash keyed for "Rerun
        # from here"). The store is best-effort — a write failure here
        # NEVER fails the run itself.
        if not run_id:
            import uuid as _uuid
            run_id = _uuid.uuid4().hex[:12]
        self._run_id = run_id
        self._checkpoint_store = get_checkpoint_store()

        # Sprint A: workflow-level resume. When a prior run failed mid-way,
        # the caller passes its run_id here; we pre-populate ctx with the
        # parquet snapshots of every successful step from that run so the
        # main loop naturally re-enters at the first non-success step.
        # Stale snapshots are silently skipped — those steps re-execute.
        resume_skip: set[str] = set()
        if resume_from_run_id:
            try:
                prior_ok = self._checkpoint_store.successful_step_ids(resume_from_run_id)
            except Exception:  # noqa: BLE001
                prior_ok = set()
            for step in order:
                if step.id not in prior_ok:
                    continue
                rel = self._cache.load_relation(
                    ctx.conn, step.id, self._effective_hashes.get(step.id, ""),
                )
                if rel is None:
                    continue
                ctx.set_result(step.id, rel)
                self._cached_step_ids.add(step.id)
                resume_skip.add(step.id)
                logger.info(
                    "resume: loaded snapshot for step %s from run %s",
                    step.id, resume_from_run_id,
                )

        t0 = time.perf_counter()
        self._emit("workflow_started", {
            "workflow_id": workflow.id,
            "total_steps": len(order),
        })

        # Pre-scan: build a map of retry_handler step_id → upstream step_ids
        # so the main loop can apply retry logic to upstream nodes.
        retry_targets = self._find_retry_targets(workflow, order, input_map)

        # Every run gets a cancellation token, so Stop can reach it, and its
        # DuckDB connection is registered on the token, so Stop or the time
        # limit interrupts a query in flight instead of waiting it out.
        # A run stopped while it was still queued arrives already cancelled
        # and ends at the first step boundary, before any work.
        from revoluzen_flow.engine.cancellation import create_token
        self._run_token = create_token(run_id)
        _interrupt = getattr(conn, "interrupt", None)
        if callable(_interrupt) and not self._run_token.is_cancelled:
            self._run_token.register_cancel_callback(_interrupt)
        else:
            _interrupt = None

        # The run's time limit, when it has one. Armed just before the first
        # step, so time spent waiting in the pool's queue does not count.
        from revoluzen_flow.engine.run_time_limit import (
            TIME_LIMIT_STEP_ID,
            RunTimeLimit,
            configured_limit_seconds,
            effective_limit_seconds,
        )
        _limit_s = effective_limit_seconds(configured_limit_seconds(workflow), time_limit_seconds)
        run_result.time_limit_seconds = _limit_s
        self._run_limit = RunTimeLimit(run_id, _limit_s).start() if _limit_s else None

        for step_index, step in enumerate(order):
            # E3.1 (2026-06-08, docs/design/executor-maturity-1.2.md) —
            # cancellation check at the step boundary. If a
            # CancellationToken for this run has been cancelled, stop
            # before starting the next step and mark the run cancelled
            # (distinct from "error"). Driver-level interruption of a
            # query already in flight happens separately via callbacks
            # registered on the token (see register_connection_cancel).
            # Guarded + additive: no token for the run = no behaviour
            # change, so every existing test path is unaffected.
            if run_id:
                try:
                    from revoluzen_flow.engine.cancellation import get_token
                    _tok = get_token(run_id)
                    if _tok is not None and _tok.is_cancelled:
                        if self._run_limit is not None and self._run_limit.is_reached:
                            # No step is running to carry the reason, so the
                            # run gets a result of its own, like "validation".
                            run_result.step_results[TIME_LIMIT_STEP_ID] = StepRunResult(
                                step_id=TIME_LIMIT_STEP_ID,
                                status="error",
                                error=self._run_limit.message_between_steps(),
                                error_type=StepErrorType.TIMEOUT,
                            )
                            run_result.status = "error"
                            run_result.stopped_by_time_limit = True
                            run_result.stop_message = self._run_limit.message_between_steps()
                            logger.info("run %s stopped at its time limit before step %s",
                                        run_id, step.id)
                        else:
                            run_result.status = "cancelled"
                            run_result.stop_message = (
                                f"{self._stopped_by(_tok)} before "
                                f"“{ctx.node_labels.get(step.id, step.id)}” started, "
                                f"so it and the steps after it did not run."
                            )
                            logger.info("run %s cancelled at step boundary (%s)",
                                        run_id, step.id)
                        break
                except Exception:  # noqa: BLE001 — never break the loop over cancel plumbing
                    pass

            if step.type == StepType.RETRY_HANDLER:
                # Retry handlers don't execute themselves — they wrap upstream
                # nodes. The wrapping was already applied when the upstream
                # node ran (see retry_targets below). Record a success result.
                upstream_ids = input_map.get(step.id, [])
                upstream_ok = all(
                    run_result.step_results.get(uid, StepRunResult(step_id=uid, status="error")).status == "success"
                    for uid in upstream_ids
                )
                if upstream_ids and upstream_ok:
                    # Pass through upstream result
                    first_up = upstream_ids[0]
                    rel = ctx._results.get(first_up)
                    if rel is not None:
                        ctx.set_result(step.id, rel)
                    preview = {}
                    if rel is not None and preview_limit > 0:
                        preview = preview_relation(rel, limit=preview_limit)
                    run_result.step_results[step.id] = StepRunResult(
                        step_id=step.id,
                        status="success",
                        row_count=preview.get("total_rows", 0),
                        columns=preview.get("columns", []),
                        sample_data=preview.get("sample_data", []),
                        schema_info=preview.get("schema_info", []),
                        duration_ms=0.0,
                    )
                else:
                    on_exhausted = step.params.get("on_exhausted", "fail")
                    if on_exhausted == "skip":
                        empty = ctx.conn.sql("SELECT NULL AS empty WHERE false")
                        ctx.set_result(step.id, empty)
                        run_result.step_results[step.id] = StepRunResult(
                            step_id=step.id, status="skipped",
                            error="Upstream failed after retries — skipped per on_exhausted=skip",
                        )
                    else:
                        upstream_err = "; ".join(
                            run_result.step_results.get(uid, StepRunResult(step_id=uid, status="error", error="unknown")).error or "unknown"
                            for uid in upstream_ids
                            if run_result.step_results.get(uid, StepRunResult(step_id=uid, status="error")).status != "success"
                        )
                        run_result.step_results[step.id] = StepRunResult(
                            step_id=step.id, status="error",
                            error=f"All retries exhausted: {upstream_err}",
                            error_type=StepErrorType.UPSTREAM_FAILED,
                        )
                        run_result.status = "error"
                        break
                continue

            # Check if this step has a retry handler attached downstream.
            # If so, override _settings with the retry handler's params.
            retry_cfg = retry_targets.get(step.id)
            if retry_cfg:
                settings = step.params.get("_settings") or {}
                settings = {**settings,
                    "retry_on_fail": True,
                    "max_retries": int(retry_cfg.get("max_retries", 3)),
                    "retry_delay_ms": int(float(retry_cfg.get("delay_seconds", 2)) * 1000),
                    "retry_strategy": "exponential" if float(retry_cfg.get("backoff_multiplier", 2.0)) > 1.0 else "fixed",
                    "on_error": "continue" if retry_cfg.get("on_exhausted") == "skip" else "stop",
                }
                step = Step(id=step.id, type=step.type, label=step.label, params={**step.params, "_settings": settings})

            self._emit("step_started", {
                "workflow_id": workflow.id,
                "step_id": step.id,
                "step_type": step.type.value if hasattr(step.type, "value") else str(step.type),
            })

            if step.id in resume_skip:
                rel = ctx._results.get(step.id)
                preview = preview_relation(rel, limit=preview_limit) if rel is not None and preview_limit > 0 else {}
                step_result = StepRunResult(
                    step_id=step.id,
                    status="success",
                    row_count=preview.get("total_rows", 0),
                    columns=preview.get("columns", []),
                    sample_data=preview.get("sample_data", []),
                    schema_info=preview.get("schema_info", []),
                    duration_ms=0.0,
                    error="resumed-from-snapshot",
                )
            else:
                step_result = self._execute_step(step, ctx, input_map, preview_limit)
            if self._stopped_by_run_limit(step_result):
                # Say why, rather than DuckDB's bare "Interrupted!", and stop
                # the run even when the step is set to continue on error.
                step_result = step_result.model_copy(update={
                    "status": "error",
                    "error": self._run_limit.message_during_step(),
                    "error_type": StepErrorType.TIMEOUT,
                })
                run_result.stopped_by_time_limit = True
                run_result.stop_message = self._run_limit.message_during_step()
            elif self._stopped_mid_step(step_result):
                # Someone pressed Stop while this step ran. The run ends as
                # cancelled, not failed: no failure alert, and the record
                # says who stopped it.
                who = self._stopped_by(self._run_token)
                step_result = step_result.model_copy(update={
                    "status": "error",
                    "error": f"{who} while this step was running.",
                    "error_type": StepErrorType.CANCELLED,
                })
                run_result.status = "cancelled"
                run_result.stop_message = (
                    f"{who} while “{ctx.node_labels.get(step.id, step.id)}” was running."
                )
            run_result.step_results[step.id] = step_result

            self._emit(
                "step_error" if step_result.status == "error" else "step_completed",
                {
                    "workflow_id": workflow.id,
                    "step_id": step.id,
                    "status": step_result.status,
                    "row_count": step_result.row_count,
                    "duration_ms": step_result.duration_ms,
                    "error": step_result.error,
                },
            )

            # Persist step output capture for the historical replay viewer
            # (Executions → lineage node click). Best-effort: a write hiccup
            # here NEVER aborts the pipeline. Realtime executor has the same
            # hook for WS-driven runs; this is the sync / scheduled path.
            try:
                step_output_store = (self.app_state or {}).get("step_output_store")
                if step_output_store is not None:
                    from revoluzen_flow.engine.step_output_store import schema_from_sample, MAX_SAMPLE_ROWS as _MAX_CAP
                    step_type = step.type.value if hasattr(step.type, "value") else str(step.type)
                    # Re-preview against the registered relation if the
                    # live StepRunResult sample was capped below the OSS
                    # cap (typical: caller asks for 50, OSS cap is 100,
                    # we want the full 100 in the drawer).
                    sample_data = list(getattr(step_result, "sample_data", []) or [])
                    schema_info = list(getattr(step_result, "schema_info", []) or [])
                    if (
                        step_result.status == "success"
                        and len(sample_data) < _MAX_CAP
                        and getattr(step_result, "row_count", 0) > len(sample_data)
                    ):
                        rel = ctx._results.get(step.id)
                        if rel is not None:
                            capture_preview = preview_relation(rel, limit=_MAX_CAP)
                            sample_data = capture_preview.get("sample_data", [])
                            schema_info = capture_preview.get("schema_info", [])
                    step_output_store.record(
                        execution_id=self._run_id,
                        step_id=step.id,
                        step_index=step_index,
                        step_type=step_type,
                        label=step.label or step.id,
                        status=step_result.status,
                        row_count=int(getattr(step_result, "row_count", 0) or 0),
                        sample_rows=sample_data,
                        schema=schema_from_sample(sample_data, schema_info),
                    )
            except Exception:  # noqa: BLE001 — capture never breaks a run
                logger.warning("step_output capture failed for run=%s step=%s",
                               self._run_id, step.id, exc_info=True)

            # Sprint 1: record the outcome in pipeline_checkpoints. Wrapped
            # in try/except so a checkpoint store glitch never breaks an
            # otherwise-successful run.
            try:
                if step_result.status == "success":
                    self._checkpoint_store.mark_success(
                        workflow_id=workflow.id,
                        run_id=self._run_id,
                        step_id=step.id,
                        rows_out=getattr(step_result, "row_count", None) or 0,
                        duration_ms=int(getattr(step_result, "duration_ms", 0) or 0),
                        output_ref=self._cache.parquet_path(step.id) if self._cache else None,
                    )
                elif step_result.status == "skipped":
                    self._checkpoint_store.mark_skipped(
                        workflow_id=workflow.id,
                        run_id=self._run_id,
                        step_id=step.id,
                        reason=getattr(step_result, "error", "") or "",
                    )
                elif step_result.status == "error":
                    self._checkpoint_store.mark_failed(
                        workflow_id=workflow.id,
                        run_id=self._run_id,
                        step_id=step.id,
                        error_summary=getattr(step_result, "error", "") or "unknown error",
                        duration_ms=int(getattr(step_result, "duration_ms", 0) or 0),
                    )
            except Exception:  # noqa: BLE001 — never fail run on checkpoint write
                logger.warning("checkpoint write failed for run=%s step=%s",
                               self._run_id, step.id, exc_info=True)

            # ``skipped`` (set when on_error=continue) lets the workflow keep
            # going. Only a hard ``error`` (on_error=stop, the default) aborts.
            if step_result.status == "error":
                if run_result.status != "cancelled":
                    run_result.status = "error"
                break

        # Disarm the limit and the interrupt before cleanup, so the queries
        # below are never interrupted by a timer or a late Stop.
        if self._run_limit is not None:
            self._run_limit.stop()
            self._run_limit = None
        if _interrupt is not None:
            self._run_token.unregister_cancel_callback(_interrupt)
        self._run_token = None

        elapsed = (time.perf_counter() - t0) * 1000

        # A cancelled run stays cancelled. It used to fall through to
        # "success" here, and commit the incremental-load cursors below as if
        # every row had been loaded.
        if run_result.status not in ("error", "cancelled"):
            run_result.status = "success"
            # Persist buffered incremental-sync watermarks ONLY now that the
            # whole run (sources AND sinks) has succeeded. A source that
            # advanced its cursor at read-time would skip rows whose sink then
            # failed — silent data loss. No-op unless a db_source ran.
            # Only for a run that really loaded something: a Sample (preview) or
            # sandbox run reads a capped number of rows and writes nothing real,
            # and its newest value would send the next real run past rows that
            # were never loaded.
            if not (getattr(ctx, "preview_mode", False) or getattr(ctx, "sandboxed", False)):
                self._commit_sync_cursors(ctx)
        # Whatever was not saved above stays unsaved, and queue messages the
        # run took but did not confirm go back to their queue (streams/holds.py).
        self._release_pending_reads(ctx)

        run_result.completed_at = datetime.now(timezone.utc)
        run_result.duration_ms = round(elapsed, 2)
        self._emit("workflow_completed", {
            "workflow_id": workflow.id,
            "status": run_result.status,
            "duration_ms": run_result.duration_ms,
        })

        # Release any pooled DB connections borrowed by this run. The
        # pool lives in app_state and is OPTIONAL — if not installed,
        # this is a no-op. Per `DESIGN_CONNECTION_POOLING.md` D-002 +
        # the load-bearing safety net section.
        try:
            pool = self.app_state.get("connection_pool")
            if pool is not None and run_id:
                pool.release_run(run_id)
        except Exception:  # noqa: BLE001 — never break run cleanup
            logger.warning("connection_pool.release_run failed", exc_info=True)

        # Drop this run's cancellation token. Tokens are created lazily
        # (connector cancel callbacks, the /cancel endpoint) and nothing
        # else removes them — without this the registry grows by one
        # entry per run for the life of the process.
        if run_id:
            try:
                from revoluzen_flow.engine.cancellation import clear_token
                clear_token(run_id)
            except Exception:  # noqa: BLE001 — never break run cleanup
                pass

        # L2.2 (2026-06-08, docs/design/lineage-1.2.md) — auto-export
        # this run's lineage to an OpenLineage endpoint (Marquez /
        # DataHub) when REVOLUZEN_FLOW_LINEAGE_OPENLINEAGE_URL is configured.
        # Best-effort: lineage is observational, so a telemetry endpoint
        # being down must never fail a data run. The exporter itself
        # never raises; this guard catches setup errors (no store, bad
        # config) too.
        self._maybe_export_openlineage(run_id)

        # Steward ingestion (2026-06-17) — feed the volume-anomaly /
        # node-empty-output / schema-drift detectors from this run so they
        # stop being permanently empty. Out-of-band + best-effort: a recorder
        # bug must never fail a data run (Steward rule #2). FULL, non-sandbox
        # runs only — sampled DEV / scratch sandbox row counts would poison
        # the baselines.
        if full_run and not sandbox_namespace:
            try:
                from revoluzen_flow.steward.ingest import record_run as _steward_record_run
                _steward_record_run(self.app_state, workflow, run_result)
            except Exception:  # noqa: BLE001 — never break run cleanup
                logger.warning("steward ingestion failed for run=%s", run_id, exc_info=True)

        # A contract describes what reached the destination. A preview or
        # sandbox run wrote nothing there, on capped rows, so judging it would
        # raise false alerts and add a false receipt to the chain.
        if not (ctx.preview_mode or ctx.sandboxed):
            self._maybe_verify_contract(
                workflow, run_result, run_id, ctx=ctx, resumed_from=resume_from_run_id,
            )

        conn.close()
        return run_result

    def _alert_contract_failed(self, workflow, run_id, receipt) -> None:
        """Tell someone. A verdict nobody sees is a diary entry.

        ``on_failure`` cannot cover this: the run succeeded. That is the
        whole situation an Execution Contract exists for, so it fires on
        its own condition, ``on_contract_failed``.

        Never raises — alerting is downstream of the data being correct.
        """
        try:
            from revoluzen_flow.alerts.models import AlertCondition
            from revoluzen_flow.alerts.notifier import NotificationService

            store = self.app_state.get("alert_store") if self.app_state else None
            if store is None:
                return

            # list_rules_by_workflow returns dicts, not AlertRule objects.
            from revoluzen_flow.alerts.models import AlertRule

            wanted = {AlertCondition.ON_CONTRACT_FAILED.value, AlertCondition.ON_ANY.value}
            rules = []
            for raw in (store.list_rules_by_workflow(workflow.id) or []):
                conds = set(raw.get("conditions") or [])
                if raw.get("condition"):
                    conds.add(raw["condition"])
                if raw.get("enabled", True) and (conds & wanted):
                    try:
                        rules.append(AlertRule(**raw))
                    except Exception:  # noqa: BLE001 — skip a malformed rule
                        continue
            if not rules:
                return

            failed = [c for c in receipt.clauses if not c.passed]
            detail = "; ".join(
                f"{c.description or c.type}: {c.detail}" for c in failed)
            notifier = NotificationService()
            for rule in rules:
                notifier.send(rule, {
                    "workflow_name": workflow.name,
                    "execution_id": run_id or "",
                    # The run really did succeed. Saying otherwise here
                    # would make the alert contradict the run history.
                    "status": "success",
                    "triggered_condition": "on_contract_failed",
                    "error_message": (
                        f"Execution Contract NOT verified — "
                        f"{len(failed)} of {len(receipt.clauses)} guarantees failed. "
                        f"{detail}"
                    ),
                })
        except Exception:  # noqa: BLE001 — alerting never breaks a run
            logger.warning("contract-failure alert dispatch failed", exc_info=True)

    def _precommit_gate(self, step, ctx, input_map) -> None:
        """Refuse a write that would break the contract, before it happens.

        Also the only place the blast radius can be recorded. Both halves
        of "replaced with 0 rows, was 3" exist together for exactly this
        instant: once the sink runs, the old count is gone and the best
        anyone can say afterwards is the half that is left.

        Only runs for nodes that have an external side effect, and only
        when the pipeline has a contract with at least one pre-commit
        checkable guarantee — so a pipeline without a contract pays one
        dict lookup.

        ``ContractBlockedWrite`` is allowed to propagate: it is the whole
        point. Every OTHER failure in here is swallowed, because a gate
        that breaks runs through its own bugs is worse than no gate.
        """
        if getattr(ctx, "preview_mode", False):
            return  # nothing is about to be written
        try:
            from revoluzen_flow.contracts.precommit import (
                PRECOMMIT_CHECKABLE, ContractBlockedWrite, check_precommit,
            )
        except Exception:  # noqa: BLE001
            return

        try:
            store = self.app_state.get("contract_store") if self.app_state else None
            if store is None:
                return
            wf_id = getattr(ctx, "workflow_id", None) or getattr(self, "_workflow_id", None)
            if not wf_id:
                return
            contract = store.get_contract(wf_id)
            if contract is None:
                return

            from revoluzen_flow.ir.node_metadata import side_effect_class_for
            if not side_effect_class_for(step.type.value):
                return  # pure transform: nothing to protect

            inputs = [ctx._results[u] for u in input_map.get(step.id, [])
                      if u in ctx._results]
            if not inputs:
                return
            rel = inputs[0]
            # `count()` returns a relation; DuckDB 2.0 removed fetchone() on
            # relations, so read it through the compat helper.
            row_count = int(fetch_scalar(rel.count("*"), default=0))
            columns = list(getattr(rel, "columns", []) or [])
            # Deliberately NOT behind the checkable-guarantee test below.
            # A contract of only post-run guarantees blocks nothing, which
            # is exactly the case where the write lands and the operator
            # most needs to be told what it did.
            gateable = any(g.type in PRECOMMIT_CHECKABLE
                           for g in contract.guarantees)
        except ContractBlockedWrite:
            raise
        except Exception:  # noqa: BLE001 — a gate bug must not fail a run
            logger.warning("pre-commit contract gate skipped", exc_info=True)
            return

        violations = check_precommit(
            contract, row_count=row_count, columns=columns,
            step_id=step.id) if gateable else []
        radius = self._record_blast_radius(
            step, ctx, rows_written=row_count, blocked=bool(violations))

        if violations:
            logger.warning(
                "Execution Contract BLOCKED write at %s: %s%s",
                step.id, "; ".join(str(v) for v in violations),
                f" [{radius.summary()}]" if radius else "",
            )
            raise ContractBlockedWrite(
                step.id, violations, step_label=getattr(step, "label", None),
                row_count=row_count, columns=columns,
            )

    def _record_blast_radius(self, step, ctx, *, rows_written: int,
                             blocked: bool):
        """Note what this write did, or would have done, to its target.

        Best effort by design. A destination whose current size cannot be
        read records ``rows_before=None`` — explicitly "not known", never
        0, because a zero here would read as "the table was already
        empty" and quietly excuse the very loss this is meant to surface.
        """
        try:
            from revoluzen_flow.contracts.schema import BlastRadius
            from revoluzen_flow.datastore.paths import safe_schema_or_table_name

            params = getattr(step, "params", None) or {}
            destination, mode, rows_before = "", "", None

            if step.type.value in ("local_table_sink", "local_table"):
                schema = safe_schema_or_table_name(
                    params.get("schema_name") or "default")
                table = safe_schema_or_table_name(
                    params.get("table_name") or "", fallback="")
                mode = str(params.get("mode") or "replace").lower()
                if table:
                    destination = f"{schema}.{table}"
                    rows_before = self._existing_row_count(ctx, schema, table)
            else:
                # Other sinks: we can still name the target and the mode,
                # which is most of the value. The prior size lives in a
                # system we do not own.
                destination = str(
                    params.get("table") or params.get("path")
                    or params.get("url") or params.get("target") or "")
                mode = str(params.get("mode") or params.get("write_mode") or "").lower()

            radius = BlastRadius(
                step_id=step.id, destination=destination, mode=mode,
                rows_before=rows_before, rows_written=rows_written,
                blocked=blocked,
            )
            self._blast_radius.append(radius)
            return radius
        except Exception:  # noqa: BLE001 — never fail a run to describe it
            logger.debug("blast radius not recorded", exc_info=True)
            return None

    def _existing_row_count(self, ctx, schema: str, table: str) -> int | None:
        """Rows currently in a managed table, or None if unreadable."""
        try:
            from revoluzen_flow.datastore.store import DataStore

            db = self.app_state.get("db") if self.app_state else None
            if db is None:
                return None
            ws = getattr(ctx, "workspace_id", None) or "default"
            found = DataStore(db).find_table_by_name(ws, schema, table)
            if found is None:
                return None  # does not exist yet: nothing to destroy
            count = getattr(found, "row_count", None)
            return None if count is None else int(count)
        except Exception:  # noqa: BLE001
            return None

    def _maybe_verify_contract(
        self, workflow, run_result, run_id: str | None, *, ctx=None, resumed_from: str | None = None,
    ) -> None:
        """Check the run against its Execution Contract and store the receipt.

        No contract for this pipeline means nothing to verify, and that is
        the common case — this is opt-in per pipeline, not a tax on every
        run.

        Deliberately the LAST thing before the connection closes: the
        verifier reads the finished WorkflowRunResult, so it has to see
        the run's final state including step statuses and row counts.

        Never raises. A verification problem must not turn a successful
        run into a failed one — the receipt records the verdict, it does
        not decide whether the data moved.
        """
        try:
            store = self.app_state.get("contract_store") if self.app_state else None
            if store is None:
                return
            contract = store.get_contract(workflow.id)
            if contract is None:
                return

            from revoluzen_flow.contracts.facts import destination_confirmed, measure_contract_facts
            from revoluzen_flow.contracts.service import verify_run

            upstream: dict[str, list[str]] = {}
            for edge in workflow.connections:
                upstream.setdefault(edge.to_step, []).append(edge.from_step)

            # Duplicate keys and the age of a time column need the rows, so
            # they are measured here while the run's relations still exist.
            # Before this, both guarantees failed every run for lack of a fact.
            facts: dict = {}
            if ctx is not None:
                facts = measure_contract_facts(
                    contract, run_result, dict(getattr(ctx, "_results", {}) or {}), ctx.conn,
                )
            radius = list(getattr(self, "_blast_radius", []) or [])
            # A full run with the step cache on writes every finished step's
            # output, which is what Resume reads back: that run can be
            # resumed. Said here so "Run was resumable" is a measured fact,
            # not a clause that failed every run for lack of one.
            checkpointed = None
            if ctx is not None:
                checkpointed = bool(
                    getattr(self, "_cache", None) is not None
                    and getattr(self, "_effective_hashes", None)
                    and getattr(ctx, "full_run", False)
                )

            receipt = verify_run(
                contract,
                run_result,
                run_id=run_id,
                upstream=upstream,
                labels={s.id: s.label for s in workflow.steps if s.label},
                # Chain onto this pipeline's previous receipt so the
                # sequence can later be walked for tampering.
                prev_hash=store.last_receipt_hash(workflow.id),
                trigger=getattr(run_result, "trigger", None) or "api",
                # What this run did to its destinations, captured by the
                # gate while the before-and-after both still existed.
                blast_radius=radius,
                destination_verified=destination_confirmed(radius),
                resumed_from=resumed_from,
                checkpoint_enabled=(True if resumed_from else checkpointed),
                duplicate_key_counts=facts.get("duplicate_key_counts"),
                column_age_seconds=facts.get("column_age_seconds"),
                unmeasured=facts.get("unmeasured"),
                # Which definition ran, as fingerprinted when the run began.
                # The publish gate reads it to find the last verdict on
                # exactly the version about to go live.
                pipeline_hash=getattr(self, "_definition_hash", None),
            )
            store.record(workflow.id, receipt)
            logger.info(
                "Execution Contract %s for run %s: %s (%d clauses)",
                contract.id, run_id, receipt.verdict, len(receipt.clauses),
            )
            if not receipt.verified:
                self._alert_contract_failed(workflow, run_id, receipt)
        except Exception:  # noqa: BLE001 — a receipt never fails a run
            logger.warning("Execution Contract verification failed", exc_info=True)

    def _maybe_export_openlineage(self, run_id: str | None) -> None:
        """Export the run's runtime lineage to an OpenLineage HTTP
        endpoint if REVOLUZEN_FLOW_LINEAGE_OPENLINEAGE_URL is set + a lineage
        store is available. No-op otherwise. Never raises."""
        if not run_id:
            return
        try:
            import os
            url = os.environ.get("REVOLUZEN_FLOW_LINEAGE_OPENLINEAGE_URL", "").strip()
            if not url:
                return
            store = self.app_state.get("lineage_store")
            if store is None:
                return
            from revoluzen_flow.lineage.openlineage import OpenLineageHTTPExporter
            summary = OpenLineageHTTPExporter(url).export_run(run_id, store)
            logger.info(
                "OpenLineage export for run %s: posted=%s failed=%s",
                run_id, summary.get("posted"), summary.get("failed"),
            )
        except Exception:  # noqa: BLE001 — observational; never break a run
            logger.warning("OpenLineage auto-export failed", exc_info=True)

    def _commit_sync_cursors(self, ctx) -> None:
        """Persist the incremental-sync watermarks buffered during a successful
        run. Called only when the run finished ``success`` — on error/cancel the
        buffer is discarded so cursors never advance past unsunk rows."""
        pending = getattr(ctx, "pending_sync_cursors", None)
        if not pending:
            return
        try:
            from revoluzen_flow.engine.sync_state_store import sync_state_store
        except Exception:  # noqa: BLE001
            return
        for state in pending:
            try:
                # Streaming sources (2026-11-13) hand over something that saves
                # itself: a read position saved only if no other run moved it
                # (streams/positions.py), or queue messages to confirm
                # (streams/holds.py).
                commit = getattr(state, "commit", None)
                if callable(commit):
                    commit()
                else:
                    sync_state_store.upsert(state)
            except Exception as exc:  # noqa: BLE001 — one bad cursor won't fail the run
                logger.warning(
                    "cursor commit failed for step %s: %s",
                    getattr(state, "step_id", "?"), exc,
                )
        ctx.pending_sync_cursors = []

    @staticmethod
    def _release_pending_reads(ctx) -> None:
        """Drop the read positions a run did not save, and give queue messages
        it did not confirm back to their queue (``release``)."""
        pending = getattr(ctx, "pending_sync_cursors", None)
        if not pending:
            return
        for item in pending:
            release = getattr(item, "release", None)
            if callable(release):
                try:
                    release()
                except Exception:  # noqa: BLE001 — never break run cleanup
                    logger.warning("could not give back the messages of step %s",
                                   getattr(item, "step_id", "?"), exc_info=True)
        ctx.pending_sync_cursors = []

    def execute_step(self, workflow: Workflow, step_id: str, preview_limit: int = 50) -> StepRunResult:
        """Execute a single step (and its dependencies) for preview."""
        trace = self.execute_step_trace(workflow, step_id, preview_limit=preview_limit)
        return trace.step_results.get(
            step_id,
            StepRunResult(step_id=step_id, status="error", error="Step not found"),
        )

    def execute_step_trace(self, workflow: Workflow, step_id: str, preview_limit: int = 50) -> WorkflowRunResult:
        """Execute a step and every upstream dependency, returning all results.

        This backs the UI's "Test Node" behavior. A user testing a
        downstream transform expects the source and every intermediate step
        to run first, and the preview panel should show those upstream
        outputs as executed. ``execute_step`` keeps its legacy selected-step
        return shape by delegating to this trace method.
        """
        started = time.time()
        # Resolve ${param.x} against the pipeline's DECLARED defaults — exactly
        # what the editor Run does (no caller overrides) — so a parameterized
        # step previews correctly via Test Node instead of sending a literal
        # "${param.x}" into the SQL and failing with a parser error.
        if getattr(workflow, "parameters", None):
            try:
                from revoluzen_flow.engine.parameters import resolve_workflow_parameters
                workflow = resolve_workflow_parameters(workflow, {})
            except Exception:
                logger.warning(
                    "execute_step_trace: parameter resolution failed; previewing with raw ${param} refs",
                    exc_info=True,
                )
        run_result = WorkflowRunResult(workflow_id=workflow.id, status="running")
        self._workflow_ref = workflow
        step = next((s for s in workflow.steps if s.id == step_id), None)
        if not step:
            run_result.status = "error"
            run_result.completed_at = datetime.now(timezone.utc)
            run_result.duration_ms = round((time.time() - started) * 1000, 1)
            run_result.step_results[step_id] = StepRunResult(
                step_id=step_id,
                status="error",
                error="Step not found",
            )
            return run_result

        conn = _open_duckdb(workflow, self.data_dir)
        try:
            ctx = ExecutionContext(
                conn=conn,
                data_dir=self.data_dir,
                app_state=self.app_state,
                preview_mode=True,
            )
            ctx.node_labels = {s.id: (s.label or s.type.value) for s in workflow.steps}
            input_map = self._build_input_map(workflow)

            self._stamp_shared_rule_sets(workflow)
            self._cache = StepCache(self.data_dir, workflow.id)
            self._effective_hashes = self._cache.compute_effective_hashes(workflow, input_map)
            self._cached_step_ids = set()

            # Execute all dependencies first. Use the same preview_limit as
            # the target so Input tabs can truthfully show upstream sample
            # data after Test Node.
            deps = self._get_dependencies(workflow, step_id)
            for dep_step in deps:
                dep_result = self._execute_step(dep_step, ctx, input_map, preview_limit=preview_limit)
                run_result.step_results[dep_step.id] = dep_result
                if dep_result.status == "error":
                    run_result.status = "error"
                    run_result.step_results[step_id] = StepRunResult(
                        step_id=step_id,
                        status="error",
                        error=f"Dependency {dep_step.id} failed: {dep_result.error}",
                    )
                    return run_result

            result = self._execute_step(step, ctx, input_map, preview_limit)
            run_result.step_results[step.id] = result
            run_result.status = "success" if result.status == "success" else "error"
            return run_result
        finally:
            run_result.completed_at = datetime.now(timezone.utc)
            run_result.duration_ms = round((time.time() - started) * 1000, 1)
            try:
                conn.close()
            except Exception:
                logger.warning(
                    "execute_step_trace: failed closing DuckDB connection",
                    exc_info=True,
                )

    def execute_workflow_resume(
        self,
        workflow: Workflow,
        run_id: str,
        preview_limit: int = 50,
        full_run: bool = True,
        parameter_values: dict | None = None,
    ) -> WorkflowRunResult:
        """Resume a previously-failed workflow run.

        Sprint A exit gate: a 10M-row sync killed mid-flight via kill -9 must
        pick up at the first non-success step on rerun. We do that by passing
        the prior `run_id` as ``resume_from_run_id`` — the main loop loads
        cached snapshots for steps that succeeded last time and re-executes
        only the rest.

        The new run gets its own fresh ``run_id``. Successful resumed steps
        get fresh checkpoint rows in this run too, so a future resume can
        chain off this one without going back to the original failure.
        """
        if not run_id:
            raise ValueError("run_id is required for execute_workflow_resume")
        from revoluzen_flow.security.execution_codes import mint_for_run
        return self.execute_workflow(
            workflow=workflow,
            preview_limit=preview_limit,
            full_run=full_run,
            parameter_values=parameter_values,
            resume_from_run_id=run_id,
            execution_code=mint_for_run(workflow, job_run_id=run_id),
        )

    def execute_step_resume(
        self, workflow: Workflow, step_id: str, preview_limit: int = 50,
    ) -> StepRunResult:
        """Rerun a step using cached upstream outputs when they are still fresh.

        This is the "Rerun from failed" path. For every dependency,
        if its effective hash matches the cached entry the parquet is loaded
        directly; otherwise the dep is re-executed and its cache is refreshed
        (which in turn invalidates anything downstream that depended on it).
        The target step always runs fresh.
        """
        self._workflow_ref = workflow
        step = next((s for s in workflow.steps if s.id == step_id), None)
        if not step:
            return StepRunResult(step_id=step_id, status="error", error="Step not found")

        conn = _open_duckdb(workflow, self.data_dir)
        # Not a preview: the target step runs for real and may write.
        ctx = ExecutionContext(conn=conn, data_dir=self.data_dir, full_run=True, app_state=self.app_state)
        ctx.node_labels = {s.id: (s.label or s.type.value) for s in workflow.steps}
        input_map = self._build_input_map(workflow)

        cache = StepCache(self.data_dir, workflow.id)
        effective = cache.compute_effective_hashes(workflow, input_map)
        self._cache = cache
        self._effective_hashes = effective
        self._cached_step_ids = set()

        deps = self._get_dependencies(workflow, step_id)
        used_cache: list[str] = []
        for dep_step in deps:
            rel = cache.load_relation(conn, dep_step.id, effective.get(dep_step.id, ""))
            if rel is not None:
                ctx.set_result(dep_step.id, rel)
                self._cached_step_ids.add(dep_step.id)
                used_cache.append(dep_step.id)
                logger.info("resume: loaded cached output for step %s", dep_step.id)
                continue
            # Stale or missing → execute. The normal write path refreshes
            # the manifest so later deps can trust it.
            dep_result = self._execute_step(dep_step, ctx, input_map, preview_limit=0)
            if dep_result.status == "error":
                conn.close()
                return StepRunResult(
                    step_id=step_id, status="error",
                    error=f"Dependency {dep_step.id} failed: {dep_result.error}",
                )

        result = self._execute_step(step, ctx, input_map, preview_limit)
        conn.close()
        if used_cache:
            logger.info(
                "resume: step %s ran with %d cached dependencies", step_id, len(used_cache),
            )
        return result

    # ──────────────────────────────────────────────────────────────────────
    # Schema-only execution (PR 1 — Schema Propagation Loop)
    #
    # Returns the column schema flowing INTO `step_id` without materialising
    # rows. The ConfigPanel / Data Wrangler use this so column-name dropdowns
    # always reflect the live, post-transformation column list — fixing the
    # silent "broken pipeline" class of bugs where a Rename step renames a
    # column and the downstream Typecast dropdown still shows the old name.
    #
    # Strategy: run the upstream subgraph with preview_limit=0 (nodes still
    # execute and produce DuckDBPyRelation handles, but no rows are
    # materialised by `preview_relation`). Then read `.columns` / `.types`
    # off each input relation. For sources (no upstream), return the
    # source's own output schema so its own ConfigPanel can populate
    # column-name pickers too.
    # ──────────────────────────────────────────────────────────────────────

    def get_step_input_schema(self, workflow: Workflow, step_id: str) -> dict:
        """Return the column schemas feeding into `step_id`.

        Shape:
            {
                "step_id": "<id>",
                "is_source": false,
                "inputs": [
                    {
                        "upstream_step_id": "<id>",
                        "schema": {
                            "columns": [{"name": "...", "type": "..."}],
                        },
                    },
                    ...
                ],
                "self_schema": {"columns": [...]}      # only for sources
            }

        Sources (no upstream) execute themselves with preview_limit=0 so
        their own output schema is returned under ``self_schema``.

        Runs in preview mode, as Test Node does. This is called just to fill the
        column pickers when someone opens a step, so it must never have an effect
        outside the run. It used to run the upstream steps for real: opening a step
        downstream of a sink wrote that sink's file, table or upload every time.
        """
        # The same declared defaults Test Node uses, so a ${param.x} upstream
        # doesn't reach the SQL as literal text.
        if getattr(workflow, "parameters", None):
            try:
                from revoluzen_flow.engine.parameters import resolve_workflow_parameters
                workflow = resolve_workflow_parameters(workflow, {})
            except Exception:
                logger.warning(
                    "get_step_input_schema: parameter resolution failed; using raw ${param} refs",
                    exc_info=True,
                )
        self._workflow_ref = workflow
        step = next((s for s in workflow.steps if s.id == step_id), None)
        if not step:
            raise ValueError(f"Step not found: {step_id}")

        conn = _open_duckdb(workflow, self.data_dir)
        try:
            ctx = ExecutionContext(
                conn=conn,
                data_dir=self.data_dir,
                app_state=self.app_state,
                preview_mode=True,
            )
            ctx.node_labels = {s.id: (s.label or s.type.value) for s in workflow.steps}
            input_map = self._build_input_map(workflow)

            # StepCache + retry tracker must be initialised because _execute_step
            # reads them. We don't persist anything from this run — schema
            # lookups are read-only against any data they touch.
            self._stamp_shared_rule_sets(workflow)
            self._cache = StepCache(self.data_dir, workflow.id)
            try:
                self._effective_hashes = self._cache.compute_effective_hashes(
                    workflow, input_map,
                )
            except Exception:  # noqa: BLE001 — cache miss is fine
                self._effective_hashes = {}
            self._cached_step_ids = set()
            self._workflow_retries_used = 0

            upstream_step_ids = input_map.get(step_id, [])

            # Execute every dependency in topological order. preview_limit=0
            # short-circuits row materialisation; we only need the relation's
            # column/type metadata. Errors propagate — a broken upstream
            # means we genuinely can't know the schema.
            for dep_step in self._get_dependencies(workflow, step_id):
                dep_result = self._execute_step(
                    dep_step, ctx, input_map, preview_limit=0,
                )
                if dep_result.status == "error":
                    return {
                        "step_id": step_id,
                        "is_source": not upstream_step_ids,
                        "inputs": [],
                        "error": (
                            f"Upstream step {dep_step.id} "
                            f"({dep_step.label or dep_step.type.value}) "
                            f"failed: {dep_result.error}"
                        ),
                    }

            if not upstream_step_ids:
                # Source node — run the source itself with preview_limit=0
                # so its own ConfigPanel can use the result. (Filter,
                # Rename, etc. inside the source's params don't apply at
                # this point — but type pickers and column-name pickers
                # do, e.g. "primary key column".)
                result = self._execute_step(
                    step, ctx, input_map, preview_limit=0,
                )
                if result.status == "error":
                    return {
                        "step_id": step_id,
                        "is_source": True,
                        "inputs": [],
                        "error": f"Source failed: {result.error}",
                    }
                rel = ctx.get_input(step.id)
                return {
                    "step_id": step_id,
                    "is_source": True,
                    "inputs": [],
                    "self_schema": _relation_to_schema(rel),
                }

            inputs = []
            for up_id in upstream_step_ids:
                rel = ctx.get_input(up_id)
                inputs.append({
                    "upstream_step_id": up_id,
                    "upstream_label": ctx.node_labels.get(up_id, up_id),
                    "schema": _relation_to_schema(rel),
                })
            return {
                "step_id": step_id,
                "is_source": False,
                "inputs": inputs,
            }
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

    def _persist_to_cache(
        self, step: Step, ctx: ExecutionContext,
        input_map: dict[str, list[str]], row_count: int,
    ) -> None:
        """Write the just-computed step output to the on-disk cache."""
        if self._cache is None or not self._effective_hashes:
            return
        if step.id in self._cached_step_ids:
            # We loaded this step from cache — no need to re-write the same bytes.
            return
        # The cache key is the step's config, not its rows. A capped result or
        # a skipped side effect's placeholder would be reused as the real
        # output by Resume and "Rerun from here", and written to destinations.
        if (
            not getattr(ctx, "full_run", False)
            or getattr(ctx, "preview_mode", False)
            or getattr(ctx, "sandboxed", False)
        ):
            return
        rel = ctx._results.get(step.id)
        if rel is None:
            return
        eff = self._effective_hashes.get(step.id, "")
        if not eff:
            return
        upstream_hashes = [
            self._effective_hashes.get(uid, "") for uid in input_map.get(step.id, [])
        ]
        try:
            self._cache.write(step, ctx.conn, rel, eff, upstream_hashes, row_count)
        except Exception as exc:  # noqa: BLE001 — cache is best-effort
            logger.warning("step_cache: write failed for %s: %s", step.id, exc)

    def _execute_step(
        self, step: Step, ctx: ExecutionContext,
        input_map: dict[str, list[str]], preview_limit: int,
    ) -> StepRunResult:
        """Execute a single step.

        Honors the per-node Settings tab fields stored under ``params._settings``:
          * ``deactivated``    — bypass this node; pass upstream through unchanged
          * ``timeout_sec``    — hard wall-clock cap on execution
          * ``retry_on_fail``  — retry loop driven by max_retries / retry_strategy
          * ``max_retries``    — number of retries (default 0)
          * ``retry_delay_ms`` — base delay between retries
          * ``retry_strategy`` — fixed | linear | exponential
          * ``on_error``       — stop | continue | continue_error_output
        """
        t0 = time.perf_counter()
        settings = (step.params.get("_settings") or {}) if isinstance(step.params, dict) else {}

        # Deactivated = stop the chain at this node. The node itself is
        # skipped and — because nothing is
        # written to ctx._results — any downstream step that tries to read
        # its input will hit the "upstream produced no data" guard below
        # and be skipped too. That gives the user the same visual outcome
        # as physically disconnecting the edge.
        if settings.get("deactivated"):
            return StepRunResult(
                step_id=step.id, status="skipped",
                error="Node is deactivated",
                duration_ms=round((time.perf_counter() - t0) * 1000, 2),
            )

        # If this step declares upstream dependencies but none of them
        # produced a result, we're in the shadow of a deactivated/skipped
        # ancestor. Skip instead of letting the node raise a confusing
        # "no input data" error from deep inside its execute().
        upstream_ids = input_map.get(step.id, [])
        if upstream_ids and not any(uid in ctx._results for uid in upstream_ids):
            return StepRunResult(
                step_id=step.id, status="skipped",
                error="Skipped — an upstream node is deactivated or was skipped",
                duration_ms=round((time.perf_counter() - t0) * 1000, 2),
            )

        timeout_sec = self._coerce_int(settings.get("timeout_sec"), 0)
        retry_on_fail = bool(settings.get("retry_on_fail"))
        max_retries = self._coerce_int(settings.get("max_retries"), 0) if retry_on_fail else 0
        retry_delay_ms = self._coerce_int(settings.get("retry_delay_ms"), 1000)
        retry_strategy = (settings.get("retry_strategy") or "fixed").lower()
        on_error = (settings.get("on_error") or "stop").lower()
        # 2026-05-30 audit: the two Settings-tab toggles that were
        # previously UI-only — now honoured at runtime.
        #
        #   execute_once — if the operator set it and this step has
        #     already produced a result in this run (e.g. inside a
        #     ForEach loop body that re-invokes the same step ID),
        #     short-circuit with the cached relation.
        #   always_output — when the step's relation comes back empty,
        #     synthesise a single-row marker so downstream sinks /
        #     alerts always fire even when the loop body is skipped
        #     (the relation is empty).
        execute_once = bool(settings.get("execute_once"))
        always_output = bool(settings.get("always_output"))

        if execute_once:
            existing = ctx._results.get(step.id)
            if existing is not None:
                preview = preview_relation(existing, limit=preview_limit) if preview_limit > 0 else {}
                return StepRunResult(
                    step_id=step.id,
                    status="success",
                    row_count=preview.get("total_rows", 0),
                    columns=preview.get("columns", []),
                    sample_data=preview.get("sample_data", []),
                    schema_info=preview.get("schema_info", []),
                    duration_ms=round((time.perf_counter() - t0) * 1000, 2),
                    error="execute_once: returning cached result from prior invocation",
                )

        last_error: Exception | None = None
        attempt = 0
        attempts_total = max_retries + 1

        while attempt < attempts_total:
            attempt += 1
            attempt_t0 = time.perf_counter()
            try:
                relation = self._run_node_once(step, ctx, input_map, timeout_sec)
                # always_output: rewrite an empty relation into a single
                # marker row so downstream sinks / alerts still fire.
                # We pick the cheapest probe (COUNT(*) on the relation)
                # rather than materialising the full result.
                if always_output and relation is not None:
                    try:
                        ctx.conn.register("__always_out_probe", relation)
                        n = fetch_scalar(ctx.conn.sql("SELECT COUNT(*) FROM __always_out_probe"))
                        if n == 0:
                            cols = ", ".join(
                                f"NULL AS \"{c}\"" for c in (relation.columns or [])
                            )
                            extra = "TRUE AS _always_output_marker"
                            select_list = f"{cols}, {extra}" if cols else extra
                            relation = ctx.conn.sql(f"SELECT {select_list}")
                    except Exception:  # noqa: BLE001 — best-effort
                        # If the probe fails (relation already closed,
                        # column metadata unavailable), leave the
                        # original relation untouched.
                        pass
                ctx.set_result(step.id, relation)
                elapsed = (time.perf_counter() - t0) * 1000

                preview = preview_relation(relation, limit=preview_limit) if preview_limit > 0 else {}
                self._persist_to_cache(step, ctx, input_map, preview.get("total_rows", 0))
                # L1.1 (2026-06-08) — emit runtime lineage event for
                # this successful step. Helper is a no-op when the
                # lineage store isn't configured, so this is safe for
                # every test path. See ExecutionContext.emit_lineage_step_run.
                ctx.emit_lineage_step_run(
                    step_id=step.id,
                    step_label=getattr(step, "label", "") or "",
                    step_type=(step.type.value if hasattr(step.type, "value") else str(step.type)),
                    columns_out=preview.get("columns", []),
                    rows_out=preview.get("total_rows", 0),
                    started_at=t0,
                    completed_at=time.time(),
                )
                return StepRunResult(
                    step_id=step.id,
                    status="success",
                    row_count=preview.get("total_rows", 0),
                    columns=preview.get("columns", []),
                    sample_data=preview.get("sample_data", []),
                    schema_info=preview.get("schema_info", []),
                    duration_ms=round(elapsed, 2),
                    skipped_reason=self._preview_skip_reason(step, ctx),
                    read_error=preview.get("read_error") or None,
                )
            except Exception as e:  # noqa: BLE001 — we want to handle every failure
                last_error = e
                # E2.1 (2026-06-08, docs/design/executor-maturity-1.2.md):
                # consult the workflow's RetryPolicy BEFORE the per-step
                # retry loop decides to schedule another attempt. When
                # the policy is enabled AND the failure's class isn't in
                # `retry_on` (e.g. data_quality / user_input failures),
                # short-circuit to the error-return path instead of
                # wasting retries on something a retry won't fix.
                #
                # Default policy is disabled, which makes `should_retry`
                # return True for every input - so workflows without a
                # declared policy keep current behaviour unchanged.
                # Wrapped in try/except: a policy-resolution bug must
                # never break a workflow's existing retry contract.
                try:
                    from revoluzen_flow.engine.failure_class import classify_error as _fc_classify
                    from revoluzen_flow.engine.retry_policy import (
                        resolve_workflow_policy as _resolve_policy,
                        should_retry as _should_retry,
                    )
                    _policy = _resolve_policy(getattr(self, "_workflow_ref", None))
                    if _policy.enabled:
                        _fc = _fc_classify(e)
                        if not _should_retry(_policy, _fc.value, attempt=attempt):
                            break  # short-circuit; failure won't change between attempts
                except Exception:
                    pass  # policy is observational; never break the per-step retry contract
                # A stopped run (Stop, or past its time limit) starts no more
                # attempts: they would only start work that has been ruled
                # out, and DuckDB's interrupt is one shot, so a retried query
                # would run to the end.
                if self._run_token is not None and self._run_token.is_cancelled:
                    break
                if attempt >= attempts_total:
                    break
                # Workflow-level retry budget — if the run as a whole has
                # already burned its budget, stop retrying THIS step too.
                # The step then falls through to its on_error policy as if
                # its per-step retries were exhausted.
                if self._workflow_retries_used >= self.workflow_retry_budget:
                    last_error = RuntimeError(
                        f"Workflow retry budget exhausted "
                        f"({self.workflow_retry_budget} retries across all steps). "
                        f"Original error on step '{step.id}': {e}"
                    )
                    break
                self._workflow_retries_used += 1
                # Compute back-off
                delay_ms = retry_delay_ms
                if retry_strategy == "linear":
                    delay_ms = retry_delay_ms * attempt
                elif retry_strategy == "exponential":
                    delay_ms = retry_delay_ms * (2 ** (attempt - 1))
                if self._run_token is not None:
                    # Wakes early on Stop or the time limit, so a long
                    # back-off cannot hold a stopped run open.
                    if self._run_token.wait(delay_ms / 1000.0):
                        break
                else:
                    time.sleep(max(0.0, delay_ms / 1000.0))

        # All attempts failed — apply on_error policy
        elapsed = (time.perf_counter() - t0) * 1000
        err_msg = str(last_error) if last_error else "unknown error"
        # PR 7: bucket the failure into a known reason code so the UI
        # can render a coloured badge + "Fix" CTA. classify_error()
        # falls through to UNKNOWN when nothing matches.
        err_type = classify_error(last_error)

        # E1.1 (2026-06-08, docs/design/executor-maturity-1.2.md) - the
        # broader retry-policy classification. Distinct from `err_type`
        # (the executor's narrow taxonomy) - this is the wider
        # "is this retryable?" category the retry policy (E2) will
        # consult. Wrapped in try/except so a classifier hiccup never
        # masks the underlying error.
        fclass: str | None = None
        try:
            from revoluzen_flow.engine.failure_class import classify_error as _classify_fc
            fc = _classify_fc(last_error) if last_error is not None else None
            if fc is not None:
                fclass = fc.value
        except Exception:
            fclass = None

        if on_error in ("continue", "continue_error_output"):
            # Treat as soft-failure: register an empty relation so downstream
            # nodes can still run, but mark this step as failed-but-skipped.
            try:
                empty_rel = ctx.conn.sql("SELECT NULL AS empty WHERE false")
                ctx.set_result(step.id, empty_rel)
            except Exception:
                pass
            return StepRunResult(
                step_id=step.id,
                status="error" if on_error == "continue_error_output" else "skipped",
                error=err_msg,
                error_type=err_type if on_error == "continue_error_output" else None,
                failure_class=fclass if on_error == "continue_error_output" else None,
                duration_ms=round(elapsed, 2),
            )

        # A write the contract refused keeps what it would have written, so
        # the run's receipt judges the other guarantees on that data.
        refused_rows, refused_columns = 0, []
        try:
            from revoluzen_flow.contracts.precommit import ContractBlockedWrite
            if isinstance(last_error, ContractBlockedWrite):
                refused_rows = int(last_error.row_count or 0)
                refused_columns = list(last_error.columns or [])
        except Exception:  # noqa: BLE001 — never mask the step's own error
            pass

        # Default: stop the workflow on this error
        return StepRunResult(
            step_id=step.id,
            status="error",
            row_count=refused_rows,
            columns=refused_columns,
            error=err_msg,
            error_type=err_type,
            failure_class=fclass,
            duration_ms=round(elapsed, 2),
        )

    # Reasons a step is skipped without failing. A limit that passes at that
    # moment did not stop such a step, so its result keeps its own message.
    _NON_FAILURE_SKIPS = (
        "Node is deactivated",
        "Skipped — an upstream node is deactivated or was skipped",
    )

    @staticmethod
    def _stopped_by(token) -> str:
        """Who or what stopped the run, as the start of a sentence."""
        reason = (getattr(token, "reason", "") or "").strip()
        return reason or "Stopped"

    def _stopped_mid_step(self, step_result: StepRunResult) -> bool:
        """True when this step failed because someone stopped the run while
        it ran: its query was interrupted, a driver cancelled, or a retry was
        cut short. The time limit is reported separately (see below)."""
        token = self._run_token
        if token is None or not token.is_cancelled:
            return False
        if self._run_limit is not None and self._run_limit.is_reached:
            return False
        if step_result.status == "error":
            return True
        return (
            step_result.status == "skipped"
            and bool(step_result.error)
            and step_result.error not in self._NON_FAILURE_SKIPS
        )

    def _stopped_by_run_limit(self, step_result: StepRunResult) -> bool:
        """True when this step failed because the run's time limit passed.

        The step boundary check stops the run before a step starts once the
        limit has passed, so a failure here means the limit expired while the
        step ran: DuckDB was interrupted, a driver cancelled, or a retry was
        cut short. A step set to continue on error reports that as skipped."""
        limit = self._run_limit
        if limit is None or not limit.is_reached:
            return False
        if step_result.status == "error":
            return True
        return (
            step_result.status == "skipped"
            and bool(step_result.error)
            and step_result.error not in self._NON_FAILURE_SKIPS
        )

    @staticmethod
    def _coerce_int(value, default: int) -> int:
        try:
            if value is None or value == "":
                return default
            return int(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _preview_skip_reason(step: Step, ctx: ExecutionContext) -> str | None:
        """Why this step's side effect did not fire, or None if it did.

        A short-circuited sink still reports `success` with its rows —
        downstream really did receive that data — so without this the
        operator could not tell a write that happened from one that was
        skipped. The UI renders it as "skipped (sample)".
        """
        if not getattr(ctx, "preview_mode", False):
            return None
        try:
            from revoluzen_flow.ir.node_metadata import side_effect_class_for
            cls_label = side_effect_class_for(step.type.value)
        except Exception:  # noqa: BLE001 — never break a run over a label
            return None
        if cls_label is None:
            return None
        return "sample"

    def _run_node_once(
        self, step: Step, ctx: ExecutionContext,
        input_map: dict[str, list[str]], timeout_sec: int,
    ) -> "duckdb.DuckDBPyRelation":
        """Single execution attempt for a node, with optional wall-clock timeout.

        The timeout uses a worker thread because DuckDB releases the GIL during
        long queries.  When ``timeout_sec`` is 0 (default) we run inline with
        no overhead.

        R8b (2026-05-30) — Preview-mode short-circuit. When the
        executor was dispatched with ``ctx.preview_mode=True``, any
        node whose `SIDE_EFFECT_CLASS` is non-null short-circuits:
        passthrough sinks return the input relation unchanged;
        transforming/terminal nodes emit a one-row "would-have-done"
        marker. The real side effect (write/send/publish) is skipped.
        Pure transforms always run because they only mutate the
        in-process relation, which is what preview mode wants to
        inspect. This is the executor-side blanket — individual nodes
        can still implement their own preview-mode handling (e.g.
        send_email's per-recipient count) and that handling runs
        normally because the node-level execute() is never reached.
        """
        # ── Execution Contract pre-commit gate ───────────────────────
        # Before a node with an external side effect fires, check the
        # guarantees that are answerable from the data about to be
        # written. A `replace`-mode sink handed zero rows truncates the
        # destination; verifying afterwards records that loss instead of
        # preventing it. Raising here means the write never happens and
        # the destination keeps what it had.
        self._precommit_gate(step, ctx, input_map)

        if getattr(ctx, "preview_mode", False):
            try:
                from revoluzen_flow.ir.node_metadata import side_effect_class_for
                cls_label = side_effect_class_for(step.type.value)
            except Exception:  # noqa: BLE001 — metadata lookup must never break exec
                cls_label = None
            if cls_label == "passthrough":
                # Sinks: hand the upstream relation back unchanged.
                inputs = [ctx._results[u]
                          for u in input_map.get(step.id, [])
                          if u in ctx._results]
                if inputs:
                    return inputs[0]
                return ctx.conn.sql("SELECT TRUE AS _preview_passthrough WHERE FALSE")
            if cls_label in ("transforming", "terminal"):
                # Actions / terminal: emit a one-row marker so downstream
                # nodes still see SOMETHING and the run-replay viewer
                # records that this step was visited.
                step_label = step.label or step.type.value
                step_label_safe = step_label.replace("'", "''")
                # X4 (2026-05-30) — ask the node class for a specific
                # preview message ("would write 50 rows to /tmp/x.csv")
                # so the run log is observable. Falls back to the generic
                # message when the node hasn't implemented preview_message.
                input_row_count = 0
                try:
                    inputs = [ctx._results[u]
                              for u in input_map.get(step.id, [])
                              if u in ctx._results]
                    if inputs:
                        ctx.conn.register("__preview_count_probe", inputs[0])
                        row = fetch_one(ctx.conn.sql(
                            "SELECT COUNT(*) FROM __preview_count_probe"
                        ))
                        input_row_count = int(row[0]) if row else 0
                except Exception:  # noqa: BLE001 — observability, never fail
                    pass
                custom_msg: str | None = None
                try:
                    node_cls = self.registry.get(step.type)
                    fn = getattr(node_cls, "preview_message", None)
                    if callable(fn):
                        custom_msg = fn(step.params or {}, input_row_count)
                except Exception:  # noqa: BLE001
                    custom_msg = None
                message = custom_msg or "side effect skipped (preview run)"
                message_safe = message.replace("'", "''")
                return ctx.conn.sql(
                    f"SELECT '{step_label_safe}' AS step, "
                    f"'preview_mode' AS status, "
                    f"'{message_safe}' AS message"
                )
        node_cls = self.registry.get(step.type)
        # Inject input step IDs into params for the node to use.
        # ``_step_id`` is also injected so sink nodes can scope the
        # idempotency dedup store to (pipeline_id, sink_step_id, hash)
        # without us threading the IR Step through to every executor.
        # Pre-existing references like the warehouse-sink drift event's
        # ``self.params.get("_step_id", "")`` already assumed this key
        # would be present when needed; v30 (sink idempotency, 2026-05-27)
        # is what actually makes it true.
        raw_params = {
            **step.params,
            "_input_step_ids": input_map.get(step.id, []),
            "_step_id": step.id,
        }
        # For transform nodes, also inject node label map so they can register named tables
        if step.type.value == "transform":
            label_map = {}
            for s in (self._workflow_ref.steps if hasattr(self, '_workflow_ref') else []):
                label_map[s.id] = s.label or s.type.value
            raw_params["_node_labels"] = label_map

        # Resolve {{ ... }} expressions against upstream node results.
        #
        # `results_as_rows()` pulls EVERY upstream result fully into Python
        # dicts. Calling it unconditionally — as this did until 2026-10-23 —
        # makes a run quadratic in the number of steps, because step N
        # fetches the complete output of steps 1..N-1 all over again.
        #
        # Measured on a 120,000-row source: a 3-step chain pulled 360,000
        # row-dicts, a 9-step chain pulled 4,320,000 (120k x sum(0..8)).
        # Wall clock went 0.74 s -> 6.4 s for the same input. Most of that
        # work was for nothing: the great majority of steps contain no
        # `{{ }}` template at all, and the snapshot was discarded unused.
        #
        # So: only pay for the snapshot when a template is actually present.
        # `resolve_expressions` is a no-op on params with no templates, so
        # passing an empty mapping is equivalent for those steps.
        try:
            upstream_ids = input_map.get(step.id, [])
            results_rows = ctx.results_as_rows() if _has_template(raw_params) else {}
            current_item: dict | None = None
            if upstream_ids:
                first_rows = results_rows.get(upstream_ids[0], [])
                current_item = first_rows[0] if first_rows else None
            params = resolve_expressions(
                raw_params,
                ctx_results=results_rows,
                node_labels=ctx.node_labels,
                item=current_item,
                item_index=0,
                vars_=ctx.vars,
            )
        except ExpressionError as ee:
            raise RuntimeError(f"Expression error in '{ee.expression}': {ee}") from ee

        node = node_cls(params)

        # ── Central branch routing (2026-06-11 multi-output) ──────────────
        # When this step consumes a non-`output` branch port of an upstream
        # node (e.g. the True branch of an if_condition, a named case of a
        # switch, a conditional_split output), transparently route + strip
        # the `_split_output` tag so the node sees ONLY its branch's rows
        # through the normal get_input(s) path. This makes branch routing
        # work for every node type, not just the few that opted into
        # get_routed_inputs.
        #
        # STRICT NO-OP guard: we only build the override when at least one
        # incoming port is a real branch (not '' / 'output'). Every existing
        # single-output pipeline skips this entirely and behaves byte-for-byte
        # as before.
        ports = params.get("_input_step_ports") or []
        has_branch_port = any(
            isinstance(p, (list, tuple)) and len(p) >= 2 and str(p[1]) not in ("", "output")
            for p in ports
        )
        if has_branch_port:
            override: dict = {}
            for entry in ports:
                try:
                    from_sid, from_port = entry[0], entry[1]
                except Exception:
                    continue
                # C1 (2026-06-15) heterogeneous multi-output: if the upstream
                # registered a distinct relation for this port (different
                # schema, e.g. a profile report), hand it over directly. Only
                # when there's no named output do we fall back to the
                # `_split_output` row-subset filter (conditional_split, DQ
                # reject, dual-output dedup, …). The two mechanisms coexist.
                named = ctx.get_named_output(from_sid, from_port)
                if named is not None:
                    override[from_sid] = named
                    continue
                rel = ctx._results.get(from_sid)
                if rel is not None:
                    override[from_sid] = ctx.route_relation(rel, from_port)
            ctx._routed_override = override
        else:
            ctx._routed_override = None

        # Scope every node's internal DuckDB view / temp-table names by this
        # step id so two nodes of the SAME type never collide on a shared
        # hardcoded name (see ExecutionContext.scoped_name / register_scoped).
        ctx.current_step_id = step.id

        try:
            if timeout_sec and timeout_sec > 0:
                import threading
                holder: dict = {}

                def _runner():
                    try:
                        holder["rel"] = node.execute(ctx)
                    except Exception as exc:
                        holder["err"] = exc

                t = threading.Thread(target=_runner, daemon=True)
                t.start()
                t.join(timeout=timeout_sec)
                if t.is_alive():
                    # Best effort: we can't kill the thread, but we surface a clear error.
                    raise TimeoutError(
                        f"Step '{step.id}' exceeded timeout of {timeout_sec}s"
                    )
                if "err" in holder:
                    raise holder["err"]
                return holder["rel"]

            return node.execute(ctx)
        finally:
            ctx._routed_override = None

    def _apply_sandbox_rewrites(
        self,
        workflow: Workflow,
        scratch_namespace: str,
        row_limit: int,
    ) -> Workflow:
        """Return a deep-copied workflow with sandbox isolation applied.

        Walks every step ONCE; sources get row-limit injection, destinations
        get scratch-namespace rewrite, everything else passes through. The
        original workflow is never mutated — it stays intact for the deploy
        path that runs after sandbox approval.

        Raises ``SandboxIsolationError`` (caller catches) if any sink has
        no safe rewrite path. Failing fast here is invariant I2.
        """
        rewritten_steps = []
        for step in workflow.steps:
            # Source: inject row limit. Destination: rewrite to scratch.
            # Pass-through for everything else (transforms, control flow).
            #
            # `or <previous>` is load-bearing, not defensive padding. Both
            # hooks are declared above as inert no-ops that return None —
            # "None signals no rewrite was applied" — and only an optional
            # extension replaces them. Without the fallback this loop
            # appended those Nones as the workflow's steps, so EVERY step
            # became None and the next `step.id` raised
            # "'NoneType' object has no attribute 'id'".
            #
            # That made safety_mode="sample" a guaranteed HTTP 500 on any
            # install without the extension — i.e. all of OSS — and sample
            # is exactly what the editor's Run button sends
            # (`?full_run=false&safety_mode=sample`). The product's primary
            # run action could not work. Reproduced 2026-10-23 on a
            # three-step CSV pipeline: live mode 200, sample mode 500.
            limited = inject_sandbox_limit(step, max_rows=row_limit) or step
            rewritten = rewrite_for_sandbox(limited, scratch_namespace) or limited
            rewritten_steps.append(rewritten)

        return workflow.model_copy(update={"steps": rewritten_steps})

    def _stamp_shared_rule_sets(self, workflow: Workflow) -> None:
        """Put the current version of each shared rule set into the Data
        Quality steps that use one (quality_rules/store.py), before the step
        cache hashes the steps: an edited set is then a changed step."""
        try:
            from revoluzen_flow.quality_rules.store import stamp_rule_set_versions
            stamp_rule_set_versions(workflow, self.app_state)
        except Exception:  # noqa: BLE001 — the step reads the sets itself; this only keys the cache
            logger.warning("could not stamp shared rule set versions", exc_info=True)

    def _build_input_map(self, workflow: Workflow) -> dict[str, list[str]]:
        """Map each step to its input step IDs.

        R6 (2026-05-30) — also stamps `_input_step_ports` onto each
        downstream step's params as ``[(from_step_id, from_port,
        to_port), ...]``. This lets branching downstreams (e.g. a
        step consuming the True branch of an if_condition) self-filter
        their input based on port semantics without forcing an
        executor-level branch dispatcher.

        Existing behaviour preserved: nodes that don't read the new
        param see no change in input data.
        """
        result: dict[str, list[str]] = {s.id: [] for s in workflow.steps}
        port_map: dict[str, list[tuple[str, str, str]]] = {s.id: [] for s in workflow.steps}
        # Per-edge table-name aliases keyed by (to_step -> {from_step: alias}).
        # Lets a multi-input node (SQL Transform) register each incoming relation
        # under a user-chosen name instead of the sanitized upstream label.
        alias_map: dict[str, dict[str, str]] = {s.id: {} for s in workflow.steps}
        step_by_id = {s.id: s for s in workflow.steps}
        for conn in workflow.connections:
            if conn.to_step in result:
                result[conn.to_step].append(conn.from_step)
                port_map[conn.to_step].append((
                    conn.from_step,
                    getattr(conn, "from_port", "output") or "output",
                    getattr(conn, "to_port", "input") or "input",
                ))
                _alias = (getattr(conn, "alias", None) or "").strip()
                if _alias:
                    alias_map[conn.to_step][conn.from_step] = _alias
        # Stamp the port metadata onto each step's params for nodes that
        # want to read it. Mirror of how _input_step_ids is injected.
        for sid, ports in port_map.items():
            step = step_by_id.get(sid)
            if step is not None:
                step.params["_input_step_ports"] = ports
                aliases = alias_map.get(sid) or {}
                if aliases:
                    step.params["_input_step_aliases"] = aliases
        return result

    def _topological_sort(self, workflow: Workflow) -> list[Step]:
        """Sort steps in dependency order."""
        step_map = {s.id: s for s in workflow.steps}
        in_degree: dict[str, int] = {s.id: 0 for s in workflow.steps}
        adjacency: dict[str, list[str]] = {s.id: [] for s in workflow.steps}

        for conn in workflow.connections:
            if conn.from_step in adjacency and conn.to_step in in_degree:
                adjacency[conn.from_step].append(conn.to_step)
                in_degree[conn.to_step] += 1

        queue = [sid for sid, deg in in_degree.items() if deg == 0]
        order = []

        while queue:
            sid = queue.pop(0)
            order.append(step_map[sid])
            for neighbor in adjacency[sid]:
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    queue.append(neighbor)

        return order

    def _find_retry_targets(
        self, workflow: Workflow, order: list[Step],
        input_map: dict[str, list[str]],
    ) -> dict[str, dict]:
        """Find upstream steps that have a retry_handler attached downstream.

        Returns a dict of upstream_step_id → retry params from the handler.
        This lets the executor apply retry logic *around* the upstream step
        instead of treating the retry_handler as a separate execution unit.
        """
        targets: dict[str, dict] = {}
        for step in order:
            if step.type != StepType.RETRY_HANDLER:
                continue
            upstream_ids = input_map.get(step.id, [])
            for uid in upstream_ids:
                targets[uid] = {
                    "max_retries": step.params.get("max_retries", 3),
                    "delay_seconds": step.params.get("delay_seconds", 2),
                    "backoff_multiplier": step.params.get("backoff_multiplier", 2.0),
                    "on_exhausted": step.params.get("on_exhausted", "fail"),
                }
                logger.info(
                    "retry_handler: wrapping step '%s' with retries=%d delay=%ss",
                    uid, targets[uid]["max_retries"], targets[uid]["delay_seconds"],
                )
        return targets

    def _get_dependencies(self, workflow: Workflow, step_id: str) -> list[Step]:
        """Get all transitive dependencies of a step, in execution order."""
        step_map = {s.id: s for s in workflow.steps}
        input_map = self._build_input_map(workflow)

        visited = set()
        order = []

        def dfs(sid: str):
            if sid in visited:
                return
            visited.add(sid)
            for dep_id in input_map.get(sid, []):
                dfs(dep_id)
            if sid != step_id:
                order.append(step_map[sid])

        dfs(step_id)
        return order
