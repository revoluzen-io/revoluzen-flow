"""Revoluzen Flow AI eval harness — reproducible quality benchmark.

The /trust page surfaces these numbers so customers can verify what we claim.
Every release ships eval results alongside the release artifacts.

Run all evals:
    python -m revoluzen_flow.eval.run

Run a specific category:
    python -m revoluzen_flow.eval.run --category planner
    python -m revoluzen_flow.eval.run --category agent_tools

See docs/eval-harness.md for the spec.
"""

from .runner import run_all, run_category, EvalResult

__all__ = ["run_all", "run_category", "EvalResult"]
