# Execution engines — the OSS / Enterprise split

**Status:** protocol implemented 2026-10-29 (`backend/revoluzen_flow/engine/engines.py`).
Beam and Spark engines designed, not built.

Apache Hop ships `hop-engines-spark` and `hop-engines-beam` as installable
plugins. Revoluzen Flow runs on DuckDB, in process, with no way to swap that.
This page is how that changes and where the line falls.

---

## The line

`edition-matrix.md` already decided it, and this follows that rule rather than
inventing a new one:

> the **execution-engine protocol**, the connector SDK and the JDBC registry
> are OSS; the maintained Databricks, dbt, SAP and Teradata integrations built
> on them are Enterprise.

| | Edition | Why |
|---|---|---|
| `ExecutionEngine` protocol | **OSS** | It is the mechanism. Boundary Rule 3 says Plus extends Flow *through* public entry points — a socket that only exists in Enterprise is not one. |
| Engine registry | **OSS** | Same. |
| DuckDB engine | **OSS** | The default. Gating it would gate running a pipeline at all. |
| Per-step coverage (`supports`, `plan`) | **OSS** | Correctness, not scale: it stops a run that would fail halfway. "Never gate correctness." |
| Engine selector in the UI | **OSS** | Choosing between installed engines is not an Enterprise act. |
| **Maintained Beam engine** | **Enterprise** | A maintained integration with someone else's runtime. |
| **Maintained Spark engine** | **Enterprise** | Same. |
| Cluster credentials, per-engine quotas, cross-team policy | **Enterprise** | Administration at scale. |

Nothing about Beam or Spark exists in this repository, including stubs
(Boundary Rule 2). An Enterprise package imports `register_engine` and calls it.

---

## Why the seam is the pipeline, not the node

The intuitive boundary is `BaseNode.execute`. It is the wrong one:

```python
def execute(self, ctx: ExecutionContext) -> duckdb.DuckDBPyRelation:
```

`ExecutionContext` hands every node a live `duckdb.DuckDBPyConnection`, so all
~105 node implementations build DuckDB SQL and return DuckDB relations. An
engine boundary there means reimplementing every node, per engine, forever.

> ⚠ `types/__init__.py` says the substrate "can change without rewriting every
> connector or transform". That is true of the **type system** and false of the
> **node contract**. Read it as intent, not as a description of the code — it is
> the sentence most likely to make someone under-estimate this work.

The seam that does work is the pipeline graph. `ir/schema.py` is explicit:

> It is engine-agnostic — stores operations, not implementations.

So an engine consumes the IR and compiles it, never touching `BaseNode`. This
is how Hop's Beam engine works too: it translates the pipeline description
rather than running Hop's own transforms on Spark.

```
          ┌─ WorkflowExecutor ──────────── DuckDB  (OSS, default)
IR graph ─┤
          └─ ExecutionEngine.execute_workflow ──── Beam / Spark  (Enterprise)
                     compiles IR, never sees BaseNode
```

What stays shared, in `WorkflowExecutor`, for every engine: topological
ordering, retries, resume, step logging, sample capture, lineage export. An
engine is asked only to run a validated graph and report what happened.

---

## Coverage is what decides whether this is usable

No alternative engine will cover all ~105 step types. `http_request`,
`send_email`, `slack_notify`, the AI nodes and the managed-table sinks have no
Beam equivalent. An engine that accepts a pipeline and dies at step 7 of 12 is
worse than one that refuses.

So `supports(step_type) -> (bool, reason)` and `plan(workflow) -> EnginePlan`
are part of the protocol, not optional extras. `plan()` must not execute
anything; it answers "what would break?" before a run, and the reason travels
with each unsupported step so the operator can act:

```
beam cannot run 2 of 11 steps:
  - notify: no distributed equivalent for slack_notify
  - s7:     no distributed equivalent for http_request
```

**An unknown engine is an error, never a downgrade.** `get_engine("beam")` with
Beam not installed raises rather than falling back to DuckDB — quietly running
a pipeline authored for a cluster on one in-process node would look like
success and behave like a capacity incident.

---

## What an Enterprise engine implements

```python
from revoluzen_flow.engine.engines import (
    EngineCapabilities, EnginePlan, register_engine,
)

class BeamEngine:
    engine_id = "beam"
    display_name = "Apache Beam"

    def capabilities(self):
        return EngineCapabilities(distributed=True, streaming=True,
                                  supports_preview=False)

    def supports(self, step_type): ...
    def plan(self, workflow): ...
    def execute_workflow(self, workflow, **kwargs): ...

register_engine(BeamEngine())
```

`ExecutionEngine` is a `runtime_checkable` `Protocol`, so there is no base class
to inherit and no import from Enterprise into OSS.

---

## Sequencing, and the open question

**Beam before Spark.** Beam is itself a portability layer, so one translator
reaches Dataflow, Flink and Spark. Hop ships both because Spark-native suits
installs already running Spark; that is a second-order want.

**The open question is whether this is needed at all.** DuckDB in-process is
fast to a single-node ceiling, and local-first is the product's whole argument.
Past that ceiling the Enterprise answer today is the containerized worker pool,
with cross-machine scaling marked *designed*. A Beam engine is a different
answer to the same question and a much larger one.

Build it when a real workload has exceeded the single-node ceiling — not
because a competitor's marketplace lists two engines. The protocol is worth
having either way: it is small, it makes the coverage question answerable, and
it means the answer to "can I bring my own engine?" stops being "no".
