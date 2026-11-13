"""The publish gate: a version known to break its contract does not go live.

Publishing already requires a passing test of the exact version. A test is a
sample run, though, and a sample cannot be held to a row-count promise, so a
pipeline could pass its test, be run for real, break its contract, and still
be published as it stood. This closes that.

The rule is narrow on purpose. It looks only at verdicts on exactly the
definition being published, judged against exactly the contract as it
stands now:

* A failed verdict on that definition and that contract refuses the publish.
* A newer passing verdict on the same pair lets it through.
* A verdict on an older definition does not count. The author changed the
  pipeline, so the old failure says nothing about the fix, and refusing it
  would block the very change that repairs the break.
* A verdict against an older contract does not count either. The promise
  changed, so the old verdict answered a different question.
* No verdict at all lets it through. Most versions are published after a
  test, before any full run; the receipt of the first real run will judge
  them.

Receipts written before these fields existed carry neither fingerprint, so
they never match, and the gate cannot refuse anything on their account.
"""

from __future__ import annotations

from typing import Any

from revoluzen_flow.ir.versioning import definition_hash

from .schema import ExecutionContract
from .verify import contract_digest

# How far back to look for a verdict on the version being published.
_RECEIPTS_SCANNED = 200


class ContractBroken(Exception):
    """Publishing refused: this exact version broke this exact contract."""

    def __init__(self, verdict: dict[str, Any]):
        self.verdict = verdict
        failed = verdict.get("failed") or []
        names = "; ".join(failed[:5]) + ("; …" if len(failed) > 5 else "")
        super().__init__(
            f"This version broke its contract on its last run ({verdict.get('run_id')}): "
            f"{names}. Fix the pipeline or change the contract, run it again, then publish."
        )


def last_verdict_for(store: Any, workflow_id: str, workflow: Any, contract: ExecutionContract) -> dict[str, Any] | None:
    """The newest verdict on this definition against this contract, or None."""
    want_pipeline = definition_hash(workflow)
    want_contract = contract_digest(contract)
    for r in store.list_receipts(workflow_id, limit=_RECEIPTS_SCANNED):  # newest first
        pipeline = r.provenance.pipeline_hash if r.provenance else None
        if pipeline != want_pipeline or r.contract_digest != want_contract:
            continue
        return {
            "run_id": r.run_id,
            "verdict": r.verdict,
            "verified_at": r.verified_at,
            "failed": [c.description or c.type.value for c in r.clauses if not c.passed],
            "spec_version": r.spec_version,
        }
    return None


def check_publish(store: Any, workflow_id: str, workflow: Any) -> dict[str, Any] | None:
    """Raise :class:`ContractBroken` when the version about to go live last
    broke its contract. Returns the verdict it found (or None) otherwise."""
    if store is None:
        return None
    contract = store.get_contract(workflow_id)
    if contract is None:
        return None
    verdict = last_verdict_for(store, workflow_id, workflow, contract)
    if verdict is not None and verdict["verdict"] == "failed":
        raise ContractBroken(verdict)
    return verdict


__all__ = ["ContractBroken", "check_publish", "last_verdict_for"]
