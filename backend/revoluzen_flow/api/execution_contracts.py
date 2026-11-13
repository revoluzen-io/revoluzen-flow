"""Execution Contracts API — the promise, and the receipts it produces.

Distinct from ``api/contracts.py``, which is **Schema** Contracts
(expected columns + drift). These two have confusingly similar names and
do different jobs; this one is the declarative "what must be true about
this run" primitive described in ``contracts/schema.py``.

Why this file exists: the verifier, the chain and the run-path entry
point were all written and tested, and then nothing ever called them.
There was no router and no table, so the feature was unreachable. The
executor now verifies at the end of each run; this exposes the result.

Every route that names a pipeline first checks that the pipeline is in the
caller's workspace. Contracts are keyed by pipeline id alone, so without
that check anyone who learned an id could read, replace or delete another
workspace's contract (fixed 2026-11-11).
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request

from revoluzen_flow.auth.deps import current_user_optional, current_workspace_id, require_min_rank
from revoluzen_flow.contracts.schema import ExecutionContract

router = APIRouter(
    prefix="/api/execution-contracts",
    tags=["execution-contracts"],
    dependencies=[Depends(current_workspace_id)],
)

# Changing what a pipeline is held to is an author's act, as editing the
# pipeline is (api/workflows.py uses the same rank).
_AUTHOR = Depends(require_min_rank("developer"))


def _store(_request: Request):
    # Imported inside the function, as the other API modules do: at
    # module-import time main.py is still being constructed.
    from revoluzen_flow.main import app_state  # type: ignore

    store = app_state.get("contract_store")
    if store is None:
        raise HTTPException(503, "contract store unavailable")
    return store


def _pipeline(workflow_id: str, workspace_id: str):
    """The pipeline's newest version, if it is in this workspace; else 404.

    The same answer whether the pipeline is missing or belongs to another
    workspace, so an id from elsewhere tells the caller nothing.
    """
    from revoluzen_flow.main import app_state  # type: ignore

    wf_store = app_state.get("store")
    if wf_store is None:
        raise HTTPException(503, "pipeline store unavailable")
    v = wf_store.get(workflow_id, workspace_id=workspace_id)
    if not v:
        raise HTTPException(404, "Pipeline not found")
    return v


@router.get("")
async def contract_summary(
    request: Request,
    workspace_id: str = Depends(current_workspace_id),
):
    """How many guarantees each pipeline here is held to.

    Declared before ``/{workflow_id}`` so the empty path is matched as
    itself. Exists so the Pipelines table can show a contract chip per
    row from one request instead of one per pipeline.
    """
    return {"counts": _store(request).guarantee_counts(workspace_id)}


@router.get("/{workflow_id}")
async def get_contract(
    workflow_id: str,
    request: Request,
    workspace_id: str = Depends(current_workspace_id),
):
    """The contract this pipeline is held to, or null."""
    _pipeline(workflow_id, workspace_id)
    c = _store(request).get_contract(workflow_id)
    return {"workflow_id": workflow_id, "contract": c.model_dump() if c else None}


@router.put("/{workflow_id}", dependencies=[_AUTHOR])
async def put_contract(
    workflow_id: str,
    contract: ExecutionContract,
    request: Request,
    workspace_id: str = Depends(current_workspace_id),
):
    """Declare what must be true. Takes effect on the next run."""
    _pipeline(workflow_id, workspace_id)
    if not contract.guarantees:
        # A contract with no guarantees verifies nothing and would report
        # every run as "verified" — worse than having none, because it
        # looks like assurance.
        raise HTTPException(400, "a contract needs at least one guarantee")
    _store(request).set_contract(workflow_id, contract, workspace_id=workspace_id)
    return {"workflow_id": workflow_id, "guarantees": len(contract.guarantees)}


@router.post("/{workflow_id}/from-spec", dependencies=[_AUTHOR])
async def contract_from_pipeline_spec(
    workflow_id: str,
    request: Request,
    workspace_id: str = Depends(current_workspace_id),
    user=Depends(current_user_optional),
):
    """Make the contract from the pipeline's spec. Takes effect on the next run.

    Every acceptance criterion that names a check becomes a guarantee. The
    guarantees an earlier pass made from the spec are replaced; ones written
    by hand in the Contract editor are kept. Criteria a person checks, and
    criteria missing a setting, are listed in the reply instead of being
    turned into clauses that would measure nothing or fail every run.
    """
    from revoluzen_flow.contracts.from_spec import SpecHasNoChecks, contract_from_spec

    v = _pipeline(workflow_id, workspace_id)
    wf = v.workflow
    store = _store(request)
    try:
        contract, summary = contract_from_spec(
            workflow_id=workflow_id,
            workflow_name=wf.name,
            goal=wf.business_purpose,
            spec=wf.spec,
            spec_version=v.version,
            existing=store.get_contract(workflow_id),
        )
    except SpecHasNoChecks as exc:
        raise HTTPException(400, str(exc)) from None
    store.set_contract(workflow_id, contract, workspace_id=workspace_id)
    try:
        from revoluzen_flow.audit import audit_action

        audit_action(
            action="contract.from_spec", resource_type="workflow", resource_id=workflow_id,
            actor=getattr(user, "email", None) or "user",
            workspace_id=workspace_id,
            details={"spec_version": v.version, "from_spec": summary["from_spec"], "kept": summary["kept"]},
        )
    except Exception:  # noqa: BLE001 — the contract is saved; the record is best-effort
        pass
    return {
        "workflow_id": workflow_id,
        "contract": contract.model_dump(),
        **summary,
        "made_at": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/{workflow_id}/status")
async def contract_status(
    workflow_id: str,
    request: Request,
    workspace_id: str = Depends(current_workspace_id),
):
    """Where the pipeline stands against its contract, for the editor and
    the publish dialog: the contract's size, whether it still matches the
    spec, and the last verdict on the newest version as it stands now."""
    from revoluzen_flow.contracts.gate import last_verdict_for
    from revoluzen_flow.ir.spec import spec_digest

    v = _pipeline(workflow_id, workspace_id)
    wf = v.workflow
    store = _store(request)
    contract = store.get_contract(workflow_id)
    if contract is None:
        return {"workflow_id": workflow_id, "contract": None}
    current_spec = spec_digest(wf.business_purpose, wf.spec)
    verdict = last_verdict_for(store, workflow_id, wf, contract)
    return {
        "workflow_id": workflow_id,
        "contract": {
            "guarantees": len(contract.guarantees),
            "from_spec": sum(1 for g in contract.guarantees if g.criterion),
            "spec_version": contract.spec_version,
            # The spec has been edited since the contract was made from it.
            "behind_spec": bool(contract.spec_digest) and contract.spec_digest != current_spec,
        },
        "last_verdict": verdict,
    }


@router.delete("/{workflow_id}", dependencies=[_AUTHOR])
async def delete_contract(
    workflow_id: str,
    request: Request,
    workspace_id: str = Depends(current_workspace_id),
):
    _pipeline(workflow_id, workspace_id)
    _store(request).delete_contract(workflow_id)
    return {"status": "deleted"}


@router.get("/{workflow_id}/receipts")
async def list_receipts(
    workflow_id: str,
    request: Request,
    limit: int = 50,
    workspace_id: str = Depends(current_workspace_id),
):
    """Receipts for this pipeline, newest first."""
    _pipeline(workflow_id, workspace_id)
    rs = _store(request).list_receipts(workflow_id, limit=limit)
    return {
        "workflow_id": workflow_id,
        "count": len(rs),
        "receipts": [r.model_dump() for r in rs],
    }


def _receipt_in_workspace(request: Request, run_id: str, workspace_id: str):
    """The receipt for a run, if that run's pipeline is in this workspace."""
    store = _store(request)
    r = store.get_receipt_for_run(run_id)
    if r is None:
        raise HTTPException(404, "no receipt for this run")
    owner = store.workflow_for_run(run_id)
    if owner:
        try:
            _pipeline(owner, workspace_id)
        except HTTPException:
            raise HTTPException(404, "no receipt for this run") from None
    return r


@router.get("/run/{run_id}/receipt")
async def receipt_for_run(
    run_id: str,
    request: Request,
    workspace_id: str = Depends(current_workspace_id),
):
    """The receipt for one run — what the execution page shows."""
    return _receipt_in_workspace(request, run_id, workspace_id).model_dump()


@router.get("/run/{run_id}/receipt.md")
async def receipt_markdown(
    run_id: str,
    request: Request,
    workspace_id: str = Depends(current_workspace_id),
):
    """The same receipt as a document, for pasting into evidence.

    A receipt nobody can hand to an auditor is not doing its job, and
    ``render_markdown()`` already existed on the model unused.
    """
    from fastapi.responses import PlainTextResponse

    r = _receipt_in_workspace(request, run_id, workspace_id)
    return PlainTextResponse(r.render_markdown(), media_type="text/markdown")


@router.get("/{workflow_id}/chain")
async def verify_chain(
    workflow_id: str,
    request: Request,
    workspace_id: str = Depends(current_workspace_id),
):
    """Walk the receipt chain and report whether it has been tampered with.

    This is the half OSS can prove. Detecting that a receipt was edited,
    reordered or inserted needs only the hashes; proving WHO issued one
    needs signing, which is Revoluzen Flow Enterprise.
    """
    from revoluzen_flow.contracts.chain import verify_chain as _verify

    _pipeline(workflow_id, workspace_id)
    receipts = _store(request).chain(workflow_id)
    result = _verify(receipts)
    payload = result.model_dump() if hasattr(result, "model_dump") else dict(result)
    payload["receipt_count"] = len(receipts)
    return payload
