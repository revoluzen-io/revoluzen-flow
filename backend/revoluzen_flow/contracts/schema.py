"""Execution Contract — a declarative, verifiable promise about a data run.

Tier-1 flagship primitive (see ``docs/positioning-answer-engine.md``). It turns
"pipeline succeeded" into **"the data operation was verified"**: the author
declares the input, the expected shape and a set of *guarantees* BEFORE a run;
after the run, :func:`revoluzen_flow.contracts.verify.verify_contract` checks each
guarantee against the actual :class:`~revoluzen_flow.ir.schema.WorkflowRunResult` and
emits a tamper-evident :class:`ContractVerification` receipt.

Design invariants (deliberate):

* **Deterministic core.** Every guarantee is checked in code, never by an LLM.
  The AI layer may *propose* a contract; it never *decides* whether one passed.
* **Fail-closed.** A guarantee whose required evidence is missing is reported as
  ``passed=False`` with a clear reason — "not proven" is never silently "passed".
* **Receipt, not a log line.** The verdict is a canonical-hash receipt so it can
  be chained (``prev_hash``) and, in Revoluzen Flow Enterprise, Ed25519-signed on top of the same
  shape (same OSS-detects-tamper / Plus-prevents-forgery split as audit export).
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class GuaranteeType(str, Enum):
    """The deterministic checks an Execution Contract can assert."""

    ROW_COUNT_VARIANCE = "row_count_variance"   # output rows within expected ± variance_pct
    ROW_COUNT_MIN = "row_count_min"             # output rows >= min_rows
    ROW_COUNT_MAX = "row_count_max"             # output rows <= max_rows
    NO_EMPTY_OUTPUT = "no_empty_output"         # output rows > 0
    SCHEMA_CONTAINS = "schema_contains"         # output columns superset of `columns`
    NO_DUPLICATE_KEY = "no_duplicate_key"       # 0 duplicate values on `key` (from facts)
    ALL_STEPS_SUCCEEDED = "all_steps_succeeded" # workflow success + no errored step
    CHECKPOINT_ENABLED = "checkpoint_enabled"   # run was checkpointed/resumable (from facts)
    DESTINATION_VERIFIED = "destination_verified"  # destination commit confirmed (from facts)
    FRESHNESS_MAX_AGE = "freshness_max_age"     # source age <= max_age_seconds (from facts)


class Guarantee(BaseModel):
    """One clause of an Execution Contract.

    Only the fields relevant to ``type`` need to be set; the verifier reports a
    misconfigured clause as a (fail-closed) failure rather than raising.
    """

    type: GuaranteeType
    description: str | None = None
    # For row/schema checks: which step's output to inspect. Falls back to the
    # contract's ``default_output_step`` and then to the run's terminal step.
    output_step: str | None = None

    # row-count guarantees
    expected_rows: int | None = None
    variance_pct: float | None = None  # 0.05 == +/- 5%
    min_rows: int | None = None
    max_rows: int | None = None

    # schema guarantee
    columns: list[str] | None = None

    # evidence-backed guarantees (values come from the `facts` dict, not the
    # result object, because the engine computes them out-of-band)
    key: str | None = None            # no_duplicate_key
    max_age_seconds: int | None = None  # freshness_max_age
    # freshness_max_age measured on this column of the output: the age of
    # its newest value. Without it, the clause needs `source_age_seconds`
    # from whoever ran the pipeline.
    time_column: str | None = None
    # The spec criterion this guarantee was made from (ir/spec.py). None for
    # a guarantee someone wrote into the contract by hand, which making the
    # contract from the spec again leaves alone.
    criterion: str | None = None


# Fields added after receipts were already in the field. The receipt hashes
# over the contract, so these are left out of that canonical form while they
# are empty: a receipt made before they existed then recomputes to its
# stored hash. See verify._contract_canonical.
CONTRACT_FIELDS_ADDED_LATER = ("spec_version", "spec_digest")
GUARANTEE_FIELDS_ADDED_LATER = ("time_column", "criterion")


class ExecutionContract(BaseModel):
    """The declared promise for a data operation, authored before the run."""

    id: str
    name: str = ""
    input: str | None = None           # e.g. "Oracle / HR.timesheets"
    expected: str | None = None        # e.g. "~250,000 records"
    transformation: str | None = None  # e.g. "SQL transform v17"
    output: str | None = None          # e.g. "SQL Server / employee_timesheet"
    default_output_step: str | None = None
    guarantees: list[Guarantee] = Field(default_factory=list)
    # When the contract was made from the pipeline's spec: the pipeline
    # version that spec was read from, and the spec's digest. Every receipt
    # copies both, so a verdict names the requirement it was judged against.
    spec_version: int | None = None
    spec_digest: str | None = None


class ClauseResult(BaseModel):
    """The outcome of verifying one :class:`Guarantee`."""

    type: GuaranteeType
    passed: bool
    description: str | None = None
    detail: str = ""
    expected: Any | None = None
    observed: Any | None = None


class NodeRun(BaseModel):
    """Compact per-step execution summary carried on a receipt (provenance)."""

    step_id: str
    status: str = "pending"
    row_count: int = 0
    duration_ms: float = 0


class Provenance(BaseModel):
    """Where a run came from — pins the pipeline version, engine, trigger and a
    per-node execution summary onto the receipt, so a reviewer can answer *which*
    pipeline ran, under what engine, and how it was kicked off."""

    pipeline_hash: str | None = None   # sha256 of the workflow definition that ran
    engine_version: str | None = None
    trigger: str | None = None         # ui | schedule | api | backfill | ...
    nodes: list[NodeRun] = Field(default_factory=list)


class BlastRadius(BaseModel):
    """What a side-effecting step did, or would have done, to its destination.

    A verdict alone does not tell an operator what it cost them. "Contract
    failed: output is empty" and "the table you serve dashboards from was
    replaced with 0 rows, it had 3" are the same event described at two
    very different levels of urgency, and only the second one says whether
    to stop what you are doing.

    Recorded at the pre-commit gate, which is the only moment both numbers
    exist at once: after the write, ``rows_before`` is gone.
    """

    step_id: str
    destination: str = ""          # "default.orders_by_region"
    mode: str = ""                 # replace | append | merge
    rows_before: int | None = None  # None = could not be read, NOT zero
    rows_written: int | None = None  # what landed, or would have
    blocked: bool = False          # True = the gate refused this write

    # The mode is stored as the bare verb the node config uses
    # ("replace"), which reads wrong in a sentence about something that
    # already happened — "the table replace with 0 rows".
    _PAST = {"replace": "replaced", "append": "appended", "merge": "merged"}

    def summary(self) -> str:
        """One line an operator can act on."""
        where = self.destination or self.step_id
        wrote = "?" if self.rows_written is None else str(self.rows_written)
        before = "" if self.rows_before is None else f", was {self.rows_before}"
        if self.blocked:
            # "refused to <infinitive>" — the bare verb is right here.
            kept = ("" if self.rows_before is None
                    else f" — it still has {self.rows_before} row"
                         f"{'' if self.rows_before == 1 else 's'}")
            return (f"refused to {self.mode or 'write'} {where} "
                    f"with {wrote} rows{kept}")
        verb = self._PAST.get(self.mode, self.mode or "written")
        return f"{where} {verb} with {wrote} rows{before}"

    @property
    def destructive(self) -> bool:
        """Did this write destroy rows that were already there?

        Only ``replace`` can; an append that adds nothing is a no-op, not
        a loss, and flagging it would train people to ignore the flag.
        """
        if self.blocked or self.mode != "replace":
            return False
        if self.rows_before is None or self.rows_written is None:
            return False
        return self.rows_written < self.rows_before


class StepAttribution(BaseModel):
    """Which step is the likely cause of a failed guarantee.

    "output is empty" names the symptom at the sink; the cause is almost
    always further upstream, and finding it means reading every step's
    row count by hand. This points at the step where the rows went.

    Deliberately typed as a lead, not a verdict: it is derived from where
    the row count collapsed along the execution order, which is a strong
    signal and not a proof. A step can legitimately filter everything out.
    """

    guarantee: GuaranteeType
    step_id: str                    # the step that dropped the rows
    detail: str                     # "8 -> 0 at ro_filter"
    upstream_step: str | None = None
    rows_in: int | None = None
    rows_out: int | None = None


class ContractVerification(BaseModel):
    """The receipt: a verified/failed verdict over all clauses of a contract."""

    contract_id: str
    run_id: str
    verdict: str  # "verified" | "failed"
    clauses: list[ClauseResult] = Field(default_factory=list)
    verified_at: str
    receipt_hash: str
    prev_hash: str | None = None
    # resume context (ties the Execution Contract to durable/resumable runs)
    resumed_from: str | int | None = None
    # provenance — pipeline version / engine / trigger / per-node summary
    provenance: Provenance | None = None
    # What the run did to its destinations, and which step caused a
    # failure. Both are hashed into the receipt ONLY when non-empty, so
    # receipts written before these fields existed still recompute to
    # their stored hash (see verify._receipt_payload).
    blast_radius: list[BlastRadius] = Field(default_factory=list)
    attribution: list[StepAttribution] = Field(default_factory=list)
    # Which requirement and which contract this verdict was judged against.
    # Hashed into the receipt only when set, like the two lists above.
    # `contract_digest` lets the publish gate tell a verdict on the contract
    # as it stands from one on a contract that has since been changed.
    spec_version: int | None = None
    spec_digest: str | None = None
    contract_digest: str | None = None

    @property
    def verified(self) -> bool:
        return self.verdict == "verified"

    def render_markdown(self) -> str:
        """A human/enterprise-legible 'the data operation was verified' block."""
        icon = "✓" if self.verified else "✗"
        head = f"### Execution Contract {icon} {self.verdict.upper()}"
        lines = [head, "", f"- run: `{self.run_id}`  ", f"- verified_at: {self.verified_at}  "]
        if self.resumed_from is not None:
            lines.append(f"- resumed_from: {self.resumed_from}  ")
        if self.provenance:
            p = self.provenance
            if p.pipeline_hash:
                lines.append(f"- pipeline: `{p.pipeline_hash[:12]}`  ")
            if p.engine_version:
                lines.append(f"- engine: {p.engine_version}  ")
            if p.trigger:
                lines.append(f"- trigger: {p.trigger}  ")
        if self.spec_digest:
            version = f"version {self.spec_version}, " if self.spec_version is not None else ""
            lines.append(f"- spec: {version}`{self.spec_digest[:12]}`  ")
        lines.append("")
        causes = {a.guarantee: a for a in self.attribution}
        for c in self.clauses:
            mark = "✓" if c.passed else "✗"
            label = c.description or c.type.value
            extra = f" ({c.detail})" if c.detail else ""
            lines.append(f"- {mark} {label}{extra}")
            cause = None if c.passed else causes.get(c.type)
            if cause:
                lines.append(f"    - likely cause: {cause.detail}")
        lines.append("")
        if self.blast_radius:
            lines.append("**Effect on destinations**")
            lines.append("")
            for b in self.blast_radius:
                lines.append(f"- {b.summary()}")
            lines.append("")
        lines.append(f"receipt: `{self.receipt_hash[:16]}...`")
        return "\n".join(lines)
