"""Persistence for Execution Contracts and the receipts they produce.

The verifier (``contracts/verify.py``) and the run-path entry point
(``contracts/service.py``) were written, tested and then never wired to
anything — no router, no caller, no table. This is the missing third
piece: somewhere to keep the contract a pipeline is held to, and the
receipts it accumulates.

Two tables, deliberately separate:

* ``execution_contracts`` — one current contract per workflow. The
  promise, edited by a human, versioned only by overwrite.
* ``contract_verifications`` — append-only receipts. Never updated,
  never deleted by the run path; a receipt that could be edited after
  the fact is not evidence.

The chain lives here rather than in the verifier: ``prev_hash`` on a new
receipt is the ``receipt_hash`` of the previous one for the same
contract, so ``contracts/chain.py`` can later walk the sequence and
detect a tampered, reordered or inserted entry.
"""

from __future__ import annotations

import json
from typing import Any

from revoluzen_flow.contracts.schema import ContractVerification, ExecutionContract

__all__ = ["ContractStore"]


class ContractStore:
    """SQLite-backed storage for contracts and their receipts."""

    def __init__(self, db):
        self.db = db
        self._ensure_tables()

    def _ensure_tables(self) -> None:
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS execution_contracts (
                workflow_id  TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL DEFAULT 'default',
                contract     TEXT NOT NULL,
                updated_at   TEXT NOT NULL
            )
        """)
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS contract_verifications (
                receipt_hash TEXT PRIMARY KEY,
                workflow_id  TEXT NOT NULL,
                contract_id  TEXT NOT NULL,
                run_id       TEXT NOT NULL,
                verdict      TEXT NOT NULL,
                prev_hash    TEXT,
                verified_at  TEXT NOT NULL,
                receipt      TEXT NOT NULL
            )
        """)
        self.db.execute("""
            CREATE INDEX IF NOT EXISTS idx_contract_verif_workflow
            ON contract_verifications(workflow_id, verified_at)
        """)
        self.db.execute("""
            CREATE INDEX IF NOT EXISTS idx_contract_verif_run
            ON contract_verifications(run_id)
        """)
        self._commit()

    def _commit(self) -> None:
        # Only for the DDL above. Database.execute() does NOT commit on
        # this codebase, so every WRITE here goes through execute_write()
        # instead — the sanctioned durable form, which the architecture
        # guard in tests/architecture/test_write_durability.py enforces.
        try:
            self.db.conn.commit()
        except Exception:
            pass

    # ── contracts ────────────────────────────────────────────────────

    def set_contract(self, workflow_id: str, contract: ExecutionContract,
                     workspace_id: str = "default") -> None:
        from datetime import datetime, timezone

        self.db.execute_write(
            "INSERT OR REPLACE INTO execution_contracts "
            "(workflow_id, workspace_id, contract, updated_at) VALUES (?,?,?,?)",
            (workflow_id, workspace_id, contract.model_dump_json(),
             datetime.now(timezone.utc).isoformat()),
        )

    def get_contract(self, workflow_id: str) -> ExecutionContract | None:
        rows = self.db.fetchall(
            "SELECT contract FROM execution_contracts WHERE workflow_id = ?",
            (workflow_id,),
        )
        if not rows:
            return None
        raw = rows[0]["contract"] if isinstance(rows[0], dict) else rows[0][0]
        return ExecutionContract(**json.loads(raw))

    def guarantee_counts(self, workspace_id: str = "default") -> dict[str, int]:
        """workflow_id -> number of guarantees, for every contract here.

        The Pipelines table shows a contract chip per row. Asking
        ``get_contract`` once per pipeline is an N+1 that grows with the
        list and reparses every contract to count a list; one query
        answers the whole page.
        """
        rows = self.db.fetchall(
            "SELECT workflow_id, contract FROM execution_contracts "
            "WHERE COALESCE(workspace_id, 'default') = ?",
            (workspace_id,),
        )
        out: dict[str, int] = {}
        for r in rows:
            wid = r["workflow_id"] if isinstance(r, dict) else r[0]
            raw = r["contract"] if isinstance(r, dict) else r[1]
            try:
                out[wid] = len(json.loads(raw).get("guarantees") or [])
            except Exception:  # noqa: BLE001 — one bad row is not the page
                out[wid] = 0
        return out

    def delete_contract(self, workflow_id: str) -> None:
        self.db.execute_write(
            "DELETE FROM execution_contracts WHERE workflow_id = ?", (workflow_id,))

    # ── receipts ─────────────────────────────────────────────────────

    def last_receipt_hash(self, workflow_id: str) -> str | None:
        """The hash to chain the next receipt onto."""
        rows = self.db.fetchall(
            "SELECT receipt_hash FROM contract_verifications "
            "WHERE workflow_id = ? ORDER BY verified_at DESC, rowid DESC LIMIT 1",
            (workflow_id,),
        )
        if not rows:
            return None
        return rows[0]["receipt_hash"] if isinstance(rows[0], dict) else rows[0][0]

    def record(self, workflow_id: str, receipt: ContractVerification) -> None:
        """Append a receipt. Never overwrites: a receipt is evidence."""
        self.db.execute_write(
            "INSERT OR IGNORE INTO contract_verifications "
            "(receipt_hash, workflow_id, contract_id, run_id, verdict, "
            " prev_hash, verified_at, receipt) VALUES (?,?,?,?,?,?,?,?)",
            (receipt.receipt_hash, workflow_id, receipt.contract_id,
             receipt.run_id, receipt.verdict, receipt.prev_hash,
             receipt.verified_at, receipt.model_dump_json()),
        )

    def list_receipts(self, workflow_id: str, limit: int = 50) -> list[ContractVerification]:
        rows = self.db.fetchall(
            "SELECT receipt FROM contract_verifications WHERE workflow_id = ? "
            "ORDER BY verified_at DESC, rowid DESC LIMIT ?",
            (workflow_id, limit),
        )
        out = []
        for r in rows:
            raw = r["receipt"] if isinstance(r, dict) else r[0]
            out.append(ContractVerification(**json.loads(raw)))
        return out

    def get_receipt_for_run(self, run_id: str) -> ContractVerification | None:
        rows = self.db.fetchall(
            "SELECT receipt FROM contract_verifications WHERE run_id = ? LIMIT 1",
            (run_id,),
        )
        if not rows:
            return None
        raw = rows[0]["receipt"] if isinstance(rows[0], dict) else rows[0][0]
        return ContractVerification(**json.loads(raw))

    def workflow_for_run(self, run_id: str) -> str | None:
        """Which pipeline a run's receipt belongs to, so a reader can be
        checked against that pipeline's workspace."""
        rows = self.db.fetchall(
            "SELECT workflow_id FROM contract_verifications WHERE run_id = ? LIMIT 1",
            (run_id,),
        )
        if not rows:
            return None
        return rows[0]["workflow_id"] if isinstance(rows[0], dict) else rows[0][0]

    def chain(self, workflow_id: str) -> list[ContractVerification]:
        """Oldest-first, the order ``contracts/chain.py`` expects."""
        return list(reversed(self.list_receipts(workflow_id, limit=1000)))
