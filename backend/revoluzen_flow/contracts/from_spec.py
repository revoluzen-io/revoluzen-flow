"""Make a pipeline's Execution Contract from its intent spec.

The spec (``ir/spec.py``) says how to tell a pipeline worked, in the
requester's words. This turns every criterion that names a check into a
guarantee, so each run is judged against the requirement it was built to
and its receipt names that requirement's version.

What it deliberately does NOT do:

* **Drop guarantees someone wrote by hand.** A guarantee with no
  ``criterion`` came from the Contract editor, not the spec, and is kept.
  Making the contract from the spec again replaces only the guarantees an
  earlier pass made from it.
* **Invent a check.** A criterion with no check is one a person judges.
  It is counted and named in the result ("checked by people"), never
  turned into a guarantee that would pass or fail on something it does not
  measure.
* **Add a clause that could never pass.** A criterion missing a setting
  its check needs (a key, a number) would fail every run, because the
  verifier fails a misconfigured clause closed. It is reported as
  incomplete and left out until someone fills it in.
"""

from __future__ import annotations

from typing import Any

from revoluzen_flow.ir.spec import SPEC_CHECKS, PipelineSpec, SpecCriterion, spec_digest

from .schema import ExecutionContract, Guarantee, GuaranteeType

# Settings copied from a criterion onto its guarantee, under the same names.
_SETTINGS = (
    "min_rows", "max_rows", "expected_rows", "variance_pct",
    "columns", "key", "time_column", "max_age_seconds",
)


class SpecHasNoChecks(ValueError):
    """The spec names no check the contract could hold, and there is no
    hand-written guarantee to keep. A contract with no guarantees would
    report every run as verified, so none is made."""


def guarantee_from_criterion(c: SpecCriterion) -> Guarantee:
    """The guarantee a complete, checkable criterion becomes."""
    meta = SPEC_CHECKS[c.check]
    settings: dict[str, Any] = {}
    for name in _SETTINGS:
        value = getattr(c, name)
        if value not in (None, "", []):
            settings[name] = value
    return Guarantee(
        type=GuaranteeType(c.check),
        # The requester's sentence is what the receipt shows first. Without
        # one, the check's plain name, never the raw enum.
        description=(c.text or "").strip() or meta["label"],
        output_step=(c.output_step or None),
        criterion=c.id,
        **settings,
    )


def contract_from_spec(
    *,
    workflow_id: str,
    workflow_name: str,
    goal: str,
    spec: PipelineSpec | None,
    spec_version: int | None,
    existing: ExecutionContract | None,
) -> tuple[ExecutionContract, dict[str, Any]]:
    """The contract the spec asks for, and a summary of how it was made.

    Raises :class:`SpecHasNoChecks` when the result would hold no guarantee.
    The summary says how many guarantees came from the spec, how many
    hand-written ones were kept, which criteria a person checks, and which
    are incomplete and why.
    """
    spec = spec or PipelineSpec()
    made: list[Guarantee] = []
    by_people: list[dict[str, str]] = []
    incomplete: list[dict[str, Any]] = []

    for i, c in enumerate(spec.acceptance, start=1):
        text = (c.text or "").strip()
        if not c.check:
            if text:
                by_people.append({"criterion": c.id, "text": text})
            continue
        if c.check not in SPEC_CHECKS:
            incomplete.append({
                "criterion": c.id, "number": i, "text": text,
                "reason": f"'{c.check}' is not a check Flow can run",
            })
            continue
        missing = c.missing()
        if missing:
            incomplete.append({
                "criterion": c.id, "number": i, "text": text,
                "reason": "needs " + ", ".join(m.replace("_", " ") for m in missing),
            })
            continue
        made.append(guarantee_from_criterion(c))

    kept = [g for g in (existing.guarantees if existing else []) if not g.criterion]
    if not made and not kept:
        raise SpecHasNoChecks(
            "None of the acceptance criteria names a check Flow can run, so "
            "there is nothing to put in the contract. Pick how each criterion "
            "is checked, or leave it to a person."
        )

    contract = ExecutionContract(
        id=(existing.id if existing else f"{workflow_id}-contract"),
        name=((existing.name if existing else "") or f"{workflow_name} contract"),
        input=existing.input if existing else None,
        expected=existing.expected if existing else None,
        transformation=existing.transformation if existing else None,
        output=existing.output if existing else None,
        default_output_step=existing.default_output_step if existing else None,
        guarantees=[*made, *kept],
        spec_version=spec_version,
        spec_digest=spec_digest(goal, spec),
    )
    summary = {
        "from_spec": len(made),
        "kept": len(kept),
        "checked_by_people": by_people,
        "incomplete": incomplete,
        "spec_version": spec_version,
        "spec_digest": contract.spec_digest,
    }
    return contract, summary


__all__ = ["SpecHasNoChecks", "contract_from_spec", "guarantee_from_criterion"]
