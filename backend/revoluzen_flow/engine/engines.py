"""Execution-engine protocol — the socket an alternative engine plugs into.

Revoluzen Flow runs pipelines on DuckDB, in process. That is the right
default: it is fast to a single-node ceiling, needs no cluster, and keeps
the product's local-first promise. Past that ceiling some installs want
Beam or Spark, and `edition-matrix.md` already settled how that splits —

    "the execution-engine protocol, the connector SDK and the JDBC
     registry are OSS; the maintained Databricks, dbt, SAP and Teradata
     integrations built on them are Enterprise."

So this module — the protocol, the registry, and the DuckDB engine that
implements it — is OSS. Maintained Beam/Spark engines are Enterprise and
ship as separate packages that register themselves here. Nothing in this
file knows they exist (Boundary Rule 2: no Plus code in this repo, not
even stubs).

## Why the seam is the pipeline, not the node

The obvious place to put an engine boundary is ``BaseNode.execute``. It
is the wrong place. That method is typed

    def execute(self, ctx: ExecutionContext) -> duckdb.DuckDBPyRelation

and ``ExecutionContext`` hands every node a live DuckDB connection, so
all ~105 node implementations build DuckDB SQL. Swapping engines there
means reimplementing every node per engine, forever.

(``types/__init__.py`` says the substrate "can change without rewriting
every connector or transform". That is true of the *type system* and not
of the *node contract*; read it as the aspiration it is.)

The seam that does work is the pipeline. ``ir/schema.py`` is explicit:
"It is engine-agnostic — stores operations, not implementations." An
engine therefore consumes the IR graph and compiles it, never touching
BaseNode. This is how Hop's Beam engine works too — it translates the
pipeline description rather than running Hop's own transforms on Spark.

## Coverage is the part that decides whether this is usable

No alternative engine will cover all ~105 step types. ``shell``,
``http_request``, the AI nodes and the managed-table sinks have no Beam
equivalent. An engine that accepts a pipeline and fails at step 7 of 12
is worse than one that refuses up front, so ``supports()`` is part of
the protocol rather than an optional nicety, and ``plan()`` exists to
answer "what would break?" before anything runs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:  # pragma: no cover - typing only
    from revoluzen_flow.ir.schema import Workflow
    from revoluzen_flow.monitoring.store import WorkflowRunResult

__all__ = [
    "EnginePlan",
    "EngineCapabilities",
    "ExecutionEngine",
    "EngineNotFoundError",
    "register_engine",
    "get_engine",
    "list_engines",
    "DEFAULT_ENGINE_ID",
]

DEFAULT_ENGINE_ID = "duckdb"


class EngineNotFoundError(LookupError):
    """Raised when a pipeline names an engine that is not registered."""


@dataclass(frozen=True)
class EngineCapabilities:
    """What an engine can and cannot do, declared rather than discovered.

    ``streaming`` and ``distributed`` are advertised so the UI can
    explain *why* someone would switch, instead of presenting a bare
    list of names.
    """

    distributed: bool = False
    streaming: bool = False
    resumable: bool = True
    # Engines that cannot sample cheaply (a cluster submit costs more
    # than the sample saves) tell the editor not to offer the button.
    supports_preview: bool = True


@dataclass
class EnginePlan:
    """The answer to "can this engine run this pipeline?", before it runs.

    ``unsupported`` is the load-bearing field: a non-empty list means the
    caller should refuse and say which steps, not start and fail midway.
    """

    engine_id: str
    supported: list[str] = field(default_factory=list)
    unsupported: list[tuple[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def runnable(self) -> bool:
        return not self.unsupported

    def describe(self) -> str:
        if self.runnable:
            return f"{self.engine_id}: all {len(self.supported)} steps supported"
        lines = [
            f"{self.engine_id} cannot run {len(self.unsupported)} of "
            f"{len(self.supported) + len(self.unsupported)} steps:"
        ]
        lines += [f"  - {sid}: {why}" for sid, why in self.unsupported]
        return "\n".join(lines)


@runtime_checkable
class ExecutionEngine(Protocol):
    """What an engine must provide to be registered.

    Deliberately small. Everything the executor does that is NOT about
    running operations over data — topological ordering, retries, resume,
    lineage export, sample capture, step logging — stays in
    ``WorkflowExecutor`` and is shared by every engine. An engine is
    asked only to run a validated graph and report what happened.
    """

    engine_id: str
    display_name: str

    def capabilities(self) -> EngineCapabilities:
        ...

    def supports(self, step_type: str) -> tuple[bool, str]:
        """``(True, "")`` or ``(False, "<human reason>")``.

        The reason is shown to the operator, so "no Beam equivalent for
        shell execution" beats "unsupported".
        """
        ...

    def plan(self, workflow: "Workflow") -> EnginePlan:
        """Dry-run the coverage question. Must not execute anything."""
        ...

    def execute_workflow(self, workflow: "Workflow", **kwargs) -> "WorkflowRunResult":
        """Run the pipeline. Signature mirrors WorkflowExecutor.execute_workflow."""
        ...


# ── Registry ─────────────────────────────────────────────────────────
# A plain dict rather than entry-point scanning: engines are registered
# explicitly, by the OSS default at import time and by Enterprise
# packages from their plugin hook. Explicit registration means an engine
# cannot appear because something happened to be on sys.path.
_ENGINES: dict[str, ExecutionEngine] = {}


def register_engine(engine: ExecutionEngine, *, replace: bool = False) -> None:
    eid = getattr(engine, "engine_id", "")
    if not eid:
        raise ValueError("engine must define a non-empty engine_id")
    if eid in _ENGINES and not replace:
        raise ValueError(
            f"engine {eid!r} is already registered; pass replace=True to override"
        )
    _ENGINES[eid] = engine


def get_engine(engine_id: str | None = None) -> ExecutionEngine:
    """Return the named engine, or the DuckDB default when unspecified.

    A pipeline that names an engine which is not installed is an error,
    never a silent fall back to DuckDB: quietly running a distributed
    pipeline on one node would look like success and behave like a
    capacity incident.
    """
    eid = engine_id or DEFAULT_ENGINE_ID
    try:
        return _ENGINES[eid]
    except KeyError:
        known = ", ".join(sorted(_ENGINES)) or "none"
        raise EngineNotFoundError(
            f"execution engine {eid!r} is not installed (registered: {known})"
        ) from None


def list_engines() -> list[ExecutionEngine]:
    return [_ENGINES[k] for k in sorted(_ENGINES)]


# ── The OSS default ──────────────────────────────────────────────────
class DuckDBEngine:
    """Today's in-process executor, wearing the protocol.

    Adds no behaviour. ``WorkflowExecutor`` is untouched and still
    constructed exactly as before by every existing caller; this type
    exists so the DuckDB path is reachable through the same interface as
    any other engine, rather than being a special case the registry has
    to work around.
    """

    engine_id = DEFAULT_ENGINE_ID
    display_name = "DuckDB (in-process)"

    def __init__(self, executor_factory=None):
        self._factory = executor_factory

    def capabilities(self) -> EngineCapabilities:
        return EngineCapabilities(
            distributed=False,
            streaming=False,
            resumable=True,
            supports_preview=True,
        )

    def supports(self, step_type: str) -> tuple[bool, str]:
        # The DuckDB engine is the reference implementation: a step type
        # is supported exactly when a node is registered for it, so this
        # never drifts from what the product can actually run.
        from revoluzen_flow.ir.schema import StepType
        from revoluzen_flow.nodes.registry import NodeRegistry

        try:
            NodeRegistry.get(StepType(step_type))
        except Exception:
            return False, f"no node implementation registered for {step_type!r}"
        return True, ""

    def plan(self, workflow: "Workflow") -> EnginePlan:
        plan = EnginePlan(engine_id=self.engine_id)
        for step in workflow.steps:
            stype = getattr(step.type, "value", str(step.type))
            ok, why = self.supports(stype)
            (plan.supported.append(step.id) if ok
             else plan.unsupported.append((step.id, why)))
        return plan

    def execute_workflow(self, workflow: "Workflow", **kwargs) -> "WorkflowRunResult":
        if self._factory is not None:
            return self._factory().execute_workflow(workflow, **kwargs)
        from revoluzen_flow.engine.executor import WorkflowExecutor

        return WorkflowExecutor().execute_workflow(workflow, **kwargs)


register_engine(DuckDBEngine())
