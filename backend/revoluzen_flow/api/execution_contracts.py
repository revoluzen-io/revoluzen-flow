"""Execution Contracts API — the promise, and the receipts it produces.

Distinct from ``api/contracts.py``, which is **Schema** Contracts
(expected columns + drift). These two have confusingly similar names and
do different jobs; this one is the declarative "what must be true about
this run" primitive described in ``contracts/schema.py``.

Why this file exists: the verifier, the chain and the run-path entry
point were all written and tested, and then nothing ever called them.
There was no router and no table, so the feature was unreachable. The
executor now verifies at the end of each run; this exposes the result.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from revoluzen_flow.auth.deps import current_workspace_id
from revoluzen_flow.contracts.schema import ExecutionContract

router = APIRouter(
    prefix="/api/execution-contracts",
    tags=["execution-contracts"],
    dependencies=[Depends(current_workspace_id)],
)


def _store(_request: Request):
    # Imported inside the function, as the other API modules do: at
    # module-import time main.py is still being constructed.
    from revoluzen_flow.main import app_state  # type: ignore

    store = app_state.get("contract_store")
    if store is None:
        raise HTTPException(503, "contract store unavailable")
    return store


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
async def get_contract(workflow_id: str, request: Request):
    """The contract this pipeline is held to, or null."""
    c = _store(request).get_contract(workflow_id)
    return {"workflow_id": workflow_id, "contract": c.model_dump() if c else None}


@router.put("/{workflow_id}")
async def put_contract(workflow_id: str, contract: ExecutionContract, request: Request):
    """Declare what must be true. Takes effect on the next run."""
    if not contract.guarantees:
        # A contract with no guarantees verifies nothing and would report
        # every run as "verified" — worse than having none, because it
        # looks like assurance.
        raise HTTPException(400, "a contract needs at least one guarantee")
    _store(request).set_contract(workflow_id, contract)
    return {"workflow_id": workflow_id, "guarantees": len(contract.guarantees)}


@router.delete("/{workflow_id}")
async def delete_contract(workflow_id: str, request: Request):
    _store(request).delete_contract(workflow_id)
    return {"status": "deleted"}


@router.get("/{workflow_id}/receipts")
async def list_receipts(workflow_id: str, request: Request, limit: int = 50):
    """Receipts for this pipeline, newest first."""
    rs = _store(request).list_receipts(workflow_id, limit=limit)
    return {
        "workflow_id": workflow_id,
        "count": len(rs),
        "receipts": [r.model_dump() for r in rs],
    }


@router.get("/run/{run_id}/receipt")
async def receipt_for_run(run_id: str, request: Request):
    """The receipt for one run — what the execution page shows."""
    r = _store(request).get_receipt_for_run(run_id)
    if r is None:
        raise HTTPException(404, "no receipt for this run")
    return r.model_dump()


@router.get("/run/{run_id}/receipt.md")
async def receipt_markdown(run_id: str, request: Request):
    """The same receipt as a document, for pasting into evidence.

    A receipt nobody can hand to an auditor is not doing its job, and
    ``render_markdown()`` already existed on the model unused.
    """
    from fastapi.responses import PlainTextResponse

    r = _store(request).get_receipt_for_run(run_id)
    if r is None:
        raise HTTPException(404, "no receipt for this run")
    return PlainTextResponse(r.render_markdown(), media_type="text/markdown")


@router.get("/{workflow_id}/chain")
async def verify_chain(workflow_id: str, request: Request):
    """Walk the receipt chain and report whether it has been tampered with.

    This is the half OSS can prove. Detecting that a receipt was edited,
    reordered or inserted needs only the hashes; proving WHO issued one
    needs signing, which is Revoluzen Flow Enterprise.
    """
    from revoluzen_flow.contracts.chain import verify_chain as _verify

    receipts = _store(request).chain(workflow_id)
    result = _verify(receipts)
    payload = result.model_dump() if hasattr(result, "model_dump") else dict(result)
    payload["receipt_count"] = len(receipts)
    return payload
