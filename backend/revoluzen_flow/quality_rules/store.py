"""Shared quality rule sets: named lists of Data Quality rules that many
pipelines use (2026-11-11).

A rule set is written once ("customer record": email not empty and shaped
like an address, customer_id not empty, country one of a list) and picked
in any Data Quality step. The step runs the set's rules as they are at run
time, so fixing a set fixes every pipeline that uses it, published ones
included, from their next run. That is the point of sharing, and also why:

* every save keeps the rules it replaced, with who saved them and when
  (``history``, the last ``HISTORY_LIMIT`` versions), and a run records
  which version of each set it checked;
* a set a saved pipeline uses cannot be deleted (``rule_set_users``);
* rules are checked with the same ``dq_rule_problems`` the step uses, so a
  set that saves is a set that runs.

Rules have the Data Quality step's shape, ``{column, op, value}``.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable

from pydantic import BaseModel, Field

HISTORY_LIMIT = 20
NAME_MAX = 80


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class RuleSet(BaseModel):
    id: str = Field(default_factory=lambda: "rs_" + uuid.uuid4().hex[:12])
    workspace_id: str = "default"
    name: str
    description: str = ""
    rules: list[dict[str, Any]] = Field(default_factory=list)
    version: int = 1
    # Earlier versions, newest first: {version, rules, description, saved_at, saved_by}.
    history: list[dict[str, Any]] = Field(default_factory=list)
    created_at: str = Field(default_factory=_now)
    created_by: str = ""
    updated_at: str = Field(default_factory=_now)
    updated_by: str = ""


class RuleSetProblem(ValueError):
    """A rule set that cannot be saved; ``problems`` says why, one line each."""

    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("; ".join(problems))


def rule_set_problems(name: str, rules: Any) -> list[str]:
    """What stops this set from being saved: empty when it can be."""
    from revoluzen_flow.nodes.quality import dq_rule_problems

    problems: list[str] = []
    clean = (name or "").strip()
    if not clean:
        problems.append("the rule set needs a name")
    elif len(clean) > NAME_MAX:
        problems.append(f"the name is longer than {NAME_MAX} characters")
    if not isinstance(rules, list) or not rules:
        problems.append("the rule set needs at least one rule")
    else:
        problems.extend(dq_rule_problems(rules))
    return problems


class RuleSetStore:
    """Rule sets in the app database, one row per set."""

    def __init__(self, db):
        self.db = db
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS quality_rule_sets (
                id           TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL DEFAULT 'default',
                name         TEXT NOT NULL,
                data         TEXT NOT NULL
            )
        """)
        self.db.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_quality_rule_sets_name
            ON quality_rule_sets(workspace_id, name)
        """)
        try:
            self.db.conn.commit()  # Database.execute() does not commit
        except Exception:  # noqa: BLE001
            pass

    def _save(self, rs: RuleSet) -> RuleSet:
        self.db.execute_write(
            "INSERT OR REPLACE INTO quality_rule_sets (id, workspace_id, name, data) VALUES (?, ?, ?, ?)",
            (rs.id, rs.workspace_id, rs.name, json.dumps(rs.model_dump(mode="json"))),
        )
        return rs

    def _load(self, rows: Iterable[Any]) -> list[RuleSet]:
        out = []
        for r in rows:
            raw = r["data"] if isinstance(r, dict) else r[0]
            try:
                out.append(RuleSet(**json.loads(raw)))
            except Exception:  # noqa: BLE001 — one unreadable row does not hide the rest
                continue
        return out

    def list(self, workspace_id: str) -> list[RuleSet]:
        return self._load(self.db.fetchall(
            "SELECT data FROM quality_rule_sets WHERE workspace_id = ? ORDER BY name COLLATE NOCASE",
            (workspace_id,),
        ))

    def get(self, set_id: str, workspace_id: str | None = None) -> RuleSet | None:
        found = self._load(self.db.fetchall("SELECT data FROM quality_rule_sets WHERE id = ?", (set_id,)))
        if not found:
            return None
        if workspace_id is not None and found[0].workspace_id != workspace_id:
            return None
        return found[0]

    def get_by_name(self, workspace_id: str, name: str) -> RuleSet | None:
        found = self._load(self.db.fetchall(
            "SELECT data FROM quality_rule_sets WHERE workspace_id = ? AND name = ?",
            (workspace_id, name.strip()),
        ))
        return found[0] if found else None

    def create(self, workspace_id: str, name: str, rules: list[dict[str, Any]], *, description: str = "", by: str = "") -> RuleSet:
        problems = rule_set_problems(name, rules)
        if not problems and self.get_by_name(workspace_id, name) is not None:
            problems = [f'a rule set named "{name.strip()}" already exists']
        if problems:
            raise RuleSetProblem(problems)
        return self._save(RuleSet(
            workspace_id=workspace_id, name=name.strip(), description=description.strip(),
            rules=rules, created_by=by, updated_by=by,
        ))

    def update(
        self, rs: RuleSet, *, rules: list[dict[str, Any]], name: str | None = None,
        description: str | None = None, by: str = "",
    ) -> RuleSet:
        new_name = (name if name is not None else rs.name).strip()
        problems = rule_set_problems(new_name, rules)
        clash = self.get_by_name(rs.workspace_id, new_name)
        if not problems and clash is not None and clash.id != rs.id:
            problems = [f'a rule set named "{new_name}" already exists']
        if problems:
            raise RuleSetProblem(problems)
        new_description = (description if description is not None else rs.description).strip()
        if rules == rs.rules and new_name == rs.name and new_description == rs.description:
            return rs  # nothing changed: no new version
        if rules != rs.rules or new_description != rs.description:
            rs.history = ([{
                "version": rs.version, "rules": rs.rules, "description": rs.description,
                "saved_at": rs.updated_at, "saved_by": rs.updated_by,
            }] + list(rs.history))[:HISTORY_LIMIT]
            rs.version += 1
        rs.rules = rules
        rs.name = new_name
        rs.description = new_description
        rs.updated_at = _now()
        rs.updated_by = by
        return self._save(rs)

    def delete(self, set_id: str) -> None:
        self.db.execute_write("DELETE FROM quality_rule_sets WHERE id = ?", (set_id,))


def stamp_rule_set_versions(workflow: Any, app_state: dict | None) -> None:
    """Write the version of each shared rule set a Data Quality step uses into
    that step's params for this run (``_rule_set_versions``, {id: version}).

    Two readers: the step cache ("Rerun from here") hashes a step's params,
    so an edited set makes the step, and what follows it, run again instead
    of reusing an output checked by the old rules; and the run's copy of the
    pipeline then says which versions it checked. A missing set is stamped
    None; the step itself refuses it when it runs.
    """
    store = (app_state or {}).get("quality_rule_set_store")
    if store is None:
        return
    for step in getattr(workflow, "steps", None) or []:
        step_type = getattr(getattr(step, "type", None), "value", getattr(step, "type", None))
        params = getattr(step, "params", None)
        if step_type != "data_quality" or not isinstance(params, dict):
            continue
        ids = params.get("rule_sets") or []
        if not isinstance(ids, list) or not ids:
            params.pop("_rule_set_versions", None)
            continue
        versions: dict[str, int | None] = {}
        for set_id in ids:
            rs = store.get(str(set_id))
            versions[str(set_id)] = rs.version if rs is not None else None
        params["_rule_set_versions"] = versions


def _uses(definition: dict[str, Any], set_id: str) -> bool:
    for step in definition.get("steps") or []:
        if not isinstance(step, dict) or step.get("type") != "data_quality":
            continue
        ids = (step.get("params") or {}).get("rule_sets") or []
        if isinstance(ids, list) and set_id in ids:
            return True
    return False


def rule_set_users(workflow_store: Any, workspace_id: str, set_id: str) -> list[dict[str, Any]]:
    """The saved pipelines whose newest or published version uses this set:
    ``[{id, name, published}]``. ``published`` is True when the version that
    runs unattended uses it."""
    out: list[dict[str, Any]] = []
    if workflow_store is None:
        return out
    for row in workflow_store.list_all(workspace_id=workspace_id):
        pipeline_id = row.get("id") or ""
        in_live = False
        if row.get("published_version") is not None:
            try:
                live = workflow_store.live_version(pipeline_id, workspace_id=workspace_id)
                in_live = live is not None and _uses(live.workflow.model_dump(mode="json"), set_id)
            except Exception:  # noqa: BLE001
                in_live = False
        in_draft = False
        try:
            newest = workflow_store.get(pipeline_id, workspace_id=workspace_id)
            in_draft = newest is not None and _uses(newest.workflow.model_dump(mode="json"), set_id)
        except Exception:  # noqa: BLE001
            in_draft = False
        if in_live or in_draft:
            out.append({"id": pipeline_id, "name": row.get("name") or pipeline_id, "published": in_live})
    return out


__all__ = [
    "HISTORY_LIMIT", "RuleSet", "RuleSetProblem", "RuleSetStore",
    "rule_set_problems", "rule_set_users", "stamp_rule_set_versions",
]
