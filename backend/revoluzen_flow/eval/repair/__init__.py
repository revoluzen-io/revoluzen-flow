"""Repair evaluation: can the Copilot fix a broken pipeline, and can we prove it?

The rest of ``revoluzen_flow.eval`` checks the planner, the sanitizer and routing
without calling a model. This package runs real broken pipelines through the real
agent loop, then runs whatever the agent drafted on the real engine and compares
the output with answers computed in plain Python.

    python -m revoluzen_flow.eval.repair --provider oracle --gate

See ``docs/repair-evaluation.md`` for what the scores mean and how to read them.
"""
