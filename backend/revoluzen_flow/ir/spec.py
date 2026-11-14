"""The intent spec: what a pipeline was asked to do, in its requester's terms.

A pipeline's steps say what it does. Its spec says what it is FOR: who
answers for the result, who uses it, where the data comes from, how to tell
that it worked, and what should change in the business once it does. The
spec is saved on the pipeline, so every version carries the spec it was
built to, and the Git copy made at publish carries it too.

The acceptance criteria are where the spec touches the engine. A criterion
that names a check becomes a guarantee in the pipeline's Execution Contract
(``contracts/from_spec.py``), so "every order appears exactly once" is
verified on every run instead of only being written down. A criterion with
no check is one a person judges, and the contract says so rather than
pretending to cover it.

The goal is the pipeline's business purpose (``Workflow.business_purpose``),
which publishing already requires, so the spec does not hold a second copy.

Deliberately not part of :func:`revoluzen_flow.ir.versioning.definition_hash`:
editing the spec changes what the pipeline is held to, not what it runs, so
it neither invalidates a passing test nor counts as an unpublished change.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from pydantic import BaseModel, Field

# The checks a criterion can name: each is a guarantee the run can actually
# prove from its own output. `all_steps_succeeded`, `destination_verified`
# and `checkpoint_enabled` exist in the contract too, but they are facts
# about the run, not requirements a requester states, so the spec editor
# leaves them to the contract.
#
# `needs` lists the settings without which the guarantee could never pass
# (the verifier fails a misconfigured clause closed). A criterion missing one
# is reported as incomplete and stays out of the contract.
SPEC_CHECKS: dict[str, dict[str, Any]] = {
    "no_empty_output": {
        "label": "Never empty",
        "needs": [],
    },
    "row_count_min": {
        "label": "At least this many rows",
        "needs": ["min_rows"],
    },
    "row_count_max": {
        "label": "At most this many rows",
        "needs": ["max_rows"],
    },
    "row_count_variance": {
        "label": "Close to an expected number of rows",
        "needs": ["expected_rows", "variance_pct"],
    },
    "schema_contains": {
        "label": "Has these columns",
        "needs": ["columns"],
    },
    "no_duplicate_key": {
        "label": "One row per key",
        "needs": ["key"],
    },
    "freshness_max_age": {
        "label": "Newest row no older than",
        "needs": ["time_column", "max_age_seconds"],
    },
}


def _short_id() -> str:
    return uuid.uuid4().hex[:8]


class SpecCriterion(BaseModel):
    """One way to tell the pipeline worked.

    ``text`` is the requester's own sentence and is what the receipt shows.
    ``check`` names the guarantee that proves it (a key of ``SPEC_CHECKS``),
    or is empty when a person has to judge it. The remaining fields are that
    guarantee's settings, named as the Execution Contract names them.
    """

    id: str = Field(default_factory=_short_id)
    text: str = ""
    check: str = ""

    min_rows: int | None = None
    max_rows: int | None = None
    expected_rows: int | None = None
    variance_pct: float | None = None  # 0.1 == within 10%
    columns: list[str] | None = None
    key: str | None = None
    time_column: str | None = None
    max_age_seconds: int | None = None
    # Which step's output to measure. Empty = the contract's default, which
    # is the pipeline's last step.
    output_step: str | None = None

    def missing(self) -> list[str]:
        """The settings this criterion's check needs and does not have."""
        meta = SPEC_CHECKS.get(self.check)
        if meta is None:
            return []
        return [f for f in meta["needs"] if getattr(self, f) in (None, "", [])]


class PipelineSpec(BaseModel):
    """Who a pipeline is for and how to tell it did its job."""

    # Who answers for the result: the person a broken report goes to, which
    # is not always the person who built the pipeline.
    business_owner: str = ""
    # Who or what reads the output: a dashboard, a team, another system.
    consumers: str = ""
    # Where the data comes from, as the requester names it ("the ERP orders
    # table"), not as a connection id.
    sources: list[str] = Field(default_factory=list)
    acceptance: list[SpecCriterion] = Field(default_factory=list)
    # What should change in the business when this works.
    outcome: str = ""
    updated_at: str | None = None
    updated_by: str | None = None

    def is_empty(self) -> bool:
        return not (
            self.business_owner.strip() or self.consumers.strip()
            or any(s.strip() for s in self.sources)
            or self.acceptance or self.outcome.strip()
        )


def spec_problems(spec: PipelineSpec) -> list[str]:
    """What is wrong with a spec someone is about to save, in words.

    Only things that would make the spec mean something other than what it
    says: a check name the contract does not know, or two criteria with one
    id. A criterion missing a setting is allowed, because it is reported
    where the contract is made and the author may fill it in later.
    """
    problems: list[str] = []
    seen: set[str] = set()
    for i, c in enumerate(spec.acceptance, start=1):
        if c.check and c.check not in SPEC_CHECKS:
            problems.append(f"criterion {i}: '{c.check}' is not a check Flow can run")
        if c.id in seen:
            problems.append(f"criterion {i}: its id '{c.id}' is used twice")
        seen.add(c.id)
        if c.variance_pct is not None and c.variance_pct < 0:
            problems.append(f"criterion {i}: the tolerance cannot be negative")
        for name in ("min_rows", "max_rows", "expected_rows", "max_age_seconds"):
            value = getattr(c, name)
            if value is not None and value < 0:
                problems.append(f"criterion {i}: {name.replace('_', ' ')} cannot be negative")
    return problems


def spec_digest(goal: str, spec: PipelineSpec | None) -> str | None:
    """A fingerprint of what the pipeline was asked to do.

    Covers the goal and every field a person wrote, not who saved it or
    when, so re-saving an unchanged spec keeps its digest. None when there
    is no spec and no goal, i.e. nothing to name.
    """
    goal = (goal or "").strip()
    if spec is None and not goal:
        return None
    body = (spec or PipelineSpec()).model_dump(mode="json", exclude={"updated_at", "updated_by"})
    body["goal"] = goal
    text = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


__all__ = [
    "SPEC_CHECKS",
    "PipelineSpec",
    "SpecCriterion",
    "spec_digest",
    "spec_problems",
]
