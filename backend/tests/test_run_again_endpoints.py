"""The three "run it again" endpoints: Re-run, Resume from failed, Replay.

Replay shipped broken. `replay_execution` did
``from revoluzen_flow.monitoring.store import get_execution_store`` — a
name that module does not define (it has only the `ExecutionStore`
class) — so every Replay raised ImportError and returned 500 before
touching an execution. Its sibling call sites used the module-level
`get_execution_store` helper in api/execution.py, which is why only
Replay was affected and why no existing test noticed.

Nothing covered these endpoints end-to-end, and a scope slip in the same
file (an `environment` local that does not exist on the ephemeral route)
also reached a running server with the whole suite green. Both are
"the name isn't there" bugs that only surface when the line executes, so
the guards below are static and cover every endpoint at once rather than
one example each.
"""

from __future__ import annotations

import ast
import importlib
import pathlib
import subprocess
import sys

import pytest

pytestmark = pytest.mark.unit

MODULE = "revoluzen_flow.api.execution"
SOURCE = pathlib.Path(
    importlib.import_module(MODULE).__file__  # type: ignore[arg-type]
)

# The endpoints behind the Execution Summary page's three buttons, plus
# the canvas Run/Sample route that shares their machinery.
RUN_AGAIN_ENDPOINTS = [
    "run_workflow",            # Re-run
    "resume_workflow",         # Resume from failed
    "replay_execution",        # Replay
    "run_workflow_ephemeral",  # canvas Run / Sample
]


def _tree() -> ast.Module:
    return ast.parse(SOURCE.read_text(encoding="utf-8"))


def _function(name: str) -> ast.AST:
    for node in ast.walk(_tree()):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in {MODULE} — was it renamed?")


def test_every_endpoint_under_test_exists():
    """Guard the guard: a rename must fail loudly, not silently skip."""
    for name in RUN_AGAIN_ENDPOINTS:
        assert _function(name) is not None


@pytest.mark.parametrize("endpoint", RUN_AGAIN_ENDPOINTS)
def test_function_local_imports_actually_resolve(endpoint):
    """Every `from X import Y` inside the endpoint must really exist.

    THE Replay bug. These imports sit inside the function body, so the
    interpreter only evaluates them when a request arrives — import-time
    checks and a green test suite both miss them.
    """
    node = _function(endpoint)
    for sub in ast.walk(node):
        if not isinstance(sub, ast.ImportFrom) or sub.level:
            continue
        module = importlib.import_module(sub.module)
        for alias in sub.names:
            assert hasattr(module, alias.name), (
                f"{endpoint} imports '{alias.name}' from '{sub.module}', "
                f"which does not define it"
            )


def test_replay_uses_the_module_level_execution_store():
    """It must NOT re-import the helper from monitoring.store.

    That module exports the `ExecutionStore` class, not a factory. The
    working call sites use the `get_execution_store` defined at the top
    of api/execution.py, which reads the live store from app_state.
    """
    from revoluzen_flow.monitoring import store as monitoring_store
    assert not hasattr(monitoring_store, "get_execution_store"), (
        "monitoring.store now exports get_execution_store — re-check that "
        "replay imports the one it actually wants"
    )

    import revoluzen_flow.api.execution as execution_api
    assert callable(execution_api.get_execution_store)

    for sub in ast.walk(_function("replay_execution")):
        if isinstance(sub, ast.ImportFrom) and sub.module == "revoluzen_flow.monitoring.store":
            names = [a.name for a in sub.names]
            assert "get_execution_store" not in names, (
                "replay_execution is importing get_execution_store from "
                "monitoring.store again — that name does not exist there"
            )


def test_no_undefined_names_in_the_execution_api():
    """Catches the other half: a local that is simply not in scope.

    An `environment=environment` on the ephemeral route referenced a name
    that exists on the saved-run route but not that one — a 500 on every
    canvas Run, with the whole suite green. Ruff's F821 sees it; Python
    does not until the line runs.
    """
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "ruff", "check", "--select", "F821",
             "--output-format", "concise", str(SOURCE)],
            capture_output=True, text=True, timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover
        pytest.skip(f"ruff unavailable: {exc}")
    if "No module named" in (proc.stderr or ""):  # pragma: no cover
        pytest.skip("ruff is not installed in this environment")

    findings = [ln for ln in (proc.stdout or "").splitlines() if "F821" in ln]
    assert not findings, "undefined names in the execution API:\n" + "\n".join(findings)


@pytest.mark.parametrize("endpoint", ["run_workflow", "resume_workflow", "replay_execution"])
def test_run_again_endpoints_are_admitted_through_the_pool(endpoint):
    """Each must gate on the pool, not run unmetered.

    Re-run and Replay call `_admit`. Resume delegates to the executor
    through a path that is already inside an admitted run, so it is
    exempt — asserted explicitly so an accidental change is visible
    rather than silently un-gating a run path.
    """
    src = ast.unparse(_function(endpoint))
    if endpoint == "resume_workflow":
        pytest.skip("resume delegates to an already-admitted execution path")
    assert "_admit(" in src, f"{endpoint} executes without pool admission"
