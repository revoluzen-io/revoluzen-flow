"""Whether an event trigger may start its pipeline now.

Asked before a file is taken or a finished pipeline's event is used up, so a
trigger that cannot run does not consume what it was waiting for:

* the pipeline must be published, and its published version must load. A
  trigger never runs a draft; with no live version it refuses, the way the
  scheduler refuses on ``PublishedVersionMissing``;
* the pipeline must not be paused or archived;
* the pipeline's overlap setting is honoured (engine/overlap.py): with Skip or
  Queue, and a run of it still going, the trigger waits; Cancel Previous stops
  that run and goes ahead.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: A gate answer that should keep the event or file for a later check.
WAIT = "wait"
#: A gate answer for which waiting would not help.
REFUSE = "refuse"


@dataclass(frozen=True)
class GateResult:
    version: Any = None
    kind: str = ""
    message: str = ""

    @property
    def ok(self) -> bool:
        return self.version is not None and not self.kind


def check_trigger_can_run(app_state: dict, row: dict) -> GateResult:
    from revoluzen_flow.engine.overlap import check_overlap
    from revoluzen_flow.ir.governance import is_deactivated
    from revoluzen_flow.ir.versioning import PublishedVersionMissing, published_pin

    store = app_state.get("store")
    workflow_id = row.get("workflow_id") or ""
    workspace_id = row.get("workspace_id") or "default"
    if store is None:
        return GateResult(kind=WAIT, message="The pipeline store is not ready yet.")
    latest = store.get(workflow_id, workspace_id=workspace_id)
    if latest is None:
        return GateResult(kind=REFUSE, message="The pipeline this trigger starts no longer exists.")
    name = latest.workflow.name or workflow_id
    if published_pin(latest) is None:
        return GateResult(
            kind=REFUSE,
            message=f'"{name}" is not published, so the trigger did not start it. Publish it to let the trigger run it.',
        )
    status = getattr(getattr(latest.workflow, "status", None), "value", None)
    if status == "archived" or is_deactivated(latest.workflow):
        return GateResult(kind=REFUSE, message=f'"{name}" is paused, so the trigger did not start it.')
    try:
        live = store.live_version(workflow_id, workspace_id=workspace_id)
    except PublishedVersionMissing as exc:
        return GateResult(kind=REFUSE, message=str(exc))
    if live is None:
        return GateResult(kind=REFUSE, message="The pipeline this trigger starts no longer exists.")
    overlap = check_overlap(live.workflow, app_state.get("execution_store"), workflow_id, workspace_id)
    if overlap is not None:
        return GateResult(
            kind=WAIT,
            message=f'Waiting: a run of "{name}" is still going and its overlap setting is {overlap.policy}.',
        )
    return GateResult(version=live)


def declared_parameter(version: Any, name: str) -> bool:
    return any(p.name == name for p in (getattr(version.workflow, "parameters", None) or []))
