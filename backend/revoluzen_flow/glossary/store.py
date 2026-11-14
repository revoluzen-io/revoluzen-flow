"""Business glossary: a workspace's own terms and metrics (2026-11-11).

"Active customer", "net revenue", "churned": words a business uses with a
meaning of its own. Written down here once, by a person, they are what the
Copilot reads when someone asks for a pipeline in those words. Without
them, it guesses, and a guess at "net revenue" is a wrong number that looks
right.

Each entry is a **term** (a thing: "active customer") or a **metric** (a
number: "net revenue"), with a definition in plain words, the other names
people use for it, and optionally the SQL a person wrote for it (a metric's
expression, a term's condition) and where it lives ("crm.accounts.status").

The Copilot receives only the entries a request mentions (``match_terms``),
by name or by another name, so a long glossary costs nothing until it is
used. Definitions are written by people, never read from data: no row value
goes to the model from here.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field

KINDS = ("term", "metric")
NAME_MAX = 80
DEFINITION_MAX = 2000
SQL_MAX = 2000
MATCH_LIMIT = 10
RENDER_MAX_CHARS = 3000


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class GlossaryTerm(BaseModel):
    id: str = Field(default_factory=lambda: "gt_" + uuid.uuid4().hex[:12])
    workspace_id: str = "default"
    name: str
    kind: str = "term"
    definition: str
    synonyms: list[str] = Field(default_factory=list)
    sql: str = ""
    where: str = ""
    owner: str = ""
    created_at: str = Field(default_factory=_now)
    created_by: str = ""
    updated_at: str = Field(default_factory=_now)
    updated_by: str = ""


class GlossaryProblem(ValueError):
    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("; ".join(problems))


def _clean_synonyms(values: Any) -> list[str]:
    out: list[str] = []
    for v in values or []:
        s = str(v).strip()
        if s and s.lower() not in {o.lower() for o in out}:
            out.append(s[:NAME_MAX])
    return out[:20]


def term_problems(name: str, kind: str, definition: str, sql: str) -> list[str]:
    problems = []
    if not (name or "").strip():
        problems.append("a term needs a name")
    elif len(name.strip()) > NAME_MAX:
        problems.append(f"the name is longer than {NAME_MAX} characters")
    if kind not in KINDS:
        problems.append("kind must be term or metric")
    if not (definition or "").strip():
        problems.append("a term needs a definition in plain words")
    elif len(definition) > DEFINITION_MAX:
        problems.append(f"the definition is longer than {DEFINITION_MAX} characters")
    if len(sql or "") > SQL_MAX:
        problems.append(f"the SQL is longer than {SQL_MAX} characters")
    return problems


class GlossaryStore:
    """Glossary entries in the app database, one row each."""

    def __init__(self, db):
        self.db = db
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS glossary_terms (
                id           TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL DEFAULT 'default',
                name         TEXT NOT NULL COLLATE NOCASE,
                data         TEXT NOT NULL
            )
        """)
        self.db.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_glossary_terms_name
            ON glossary_terms(workspace_id, name)
        """)
        try:
            self.db.conn.commit()  # Database.execute() does not commit
        except Exception:  # noqa: BLE001
            pass

    def _save(self, t: GlossaryTerm) -> GlossaryTerm:
        self.db.execute_write(
            "INSERT OR REPLACE INTO glossary_terms (id, workspace_id, name, data) VALUES (?, ?, ?, ?)",
            (t.id, t.workspace_id, t.name, json.dumps(t.model_dump(mode="json"))),
        )
        return t

    def _load(self, rows) -> list[GlossaryTerm]:
        out = []
        for r in rows:
            try:
                out.append(GlossaryTerm(**json.loads(r["data"] if isinstance(r, dict) else r[0])))
            except Exception:  # noqa: BLE001
                continue
        return out

    def list(self, workspace_id: str) -> list[GlossaryTerm]:
        return self._load(self.db.fetchall(
            "SELECT data FROM glossary_terms WHERE workspace_id = ? ORDER BY name COLLATE NOCASE",
            (workspace_id,),
        ))

    def get(self, term_id: str, workspace_id: str | None = None) -> GlossaryTerm | None:
        found = self._load(self.db.fetchall("SELECT data FROM glossary_terms WHERE id = ?", (term_id,)))
        if not found or (workspace_id is not None and found[0].workspace_id != workspace_id):
            return None
        return found[0]

    def _clash(self, workspace_id: str, name: str, own_id: str | None) -> bool:
        rows = self.db.fetchall(
            "SELECT id FROM glossary_terms WHERE workspace_id = ? AND name = ? COLLATE NOCASE",
            (workspace_id, name.strip()),
        )
        return any((r["id"] if isinstance(r, dict) else r[0]) != own_id for r in rows)

    def save(self, workspace_id: str, fields: dict[str, Any], *, by: str = "", term: GlossaryTerm | None = None) -> GlossaryTerm:
        name = str(fields.get("name", term.name if term else "")).strip()
        kind = str(fields.get("kind", term.kind if term else "term")).strip().lower()
        definition = str(fields.get("definition", term.definition if term else "")).strip()
        sql = str(fields.get("sql", term.sql if term else "") or "").strip()
        problems = term_problems(name, kind, definition, sql)
        if not problems and self._clash(workspace_id, name, term.id if term else None):
            problems = [f'"{name}" is already in the glossary']
        if problems:
            raise GlossaryProblem(problems)
        t = term or GlossaryTerm(workspace_id=workspace_id, name=name, definition=definition, created_by=by)
        t.name, t.kind, t.definition, t.sql = name, kind, definition, sql
        t.synonyms = _clean_synonyms(fields.get("synonyms", t.synonyms))
        t.where = str(fields.get("where", t.where) or "").strip()[:300]
        t.owner = str(fields.get("owner", t.owner) or "").strip()[:120]
        t.updated_at, t.updated_by = _now(), by
        return self._save(t)

    def delete(self, term_id: str) -> None:
        self.db.execute_write("DELETE FROM glossary_terms WHERE id = ?", (term_id,))


def _pattern(phrase: str) -> re.Pattern[str]:
    # Words may be joined by spaces, _ or -, and the last may take a plural
    # ending: "active customers" mentions "Active customer".
    words = [re.escape(w) for w in phrase.lower().split()]
    return re.compile(r"(?<![a-z0-9_])" + r"[\s_-]+".join(words) + r"(?:e?s)?(?![a-z0-9_])")


def match_terms(terms: list[GlossaryTerm], text: str, limit: int = MATCH_LIMIT) -> list[GlossaryTerm]:
    """The entries ``text`` mentions by name or another name, in the order
    they first appear. "net revenue", "Net_Revenue" and "net-revenue" all
    match an entry named "Net revenue"; "revenue" alone does not."""
    hay = (text or "").lower()
    if not hay.strip():
        return []
    found: list[tuple[int, GlossaryTerm]] = []
    for t in terms:
        positions = []
        for phrase in [t.name, *t.synonyms]:
            if not phrase.strip():
                continue
            m = _pattern(phrase).search(hay)
            if m:
                positions.append(m.start())
        if positions:
            found.append((min(positions), t))
    found.sort(key=lambda p: p[0])
    return [t for _, t in found[:limit]]


def render_for_model(terms: list[GlossaryTerm], max_chars: int = RENDER_MAX_CHARS) -> str:
    """The matched entries as a block for a model's prompt; empty for none."""
    if not terms:
        return ""
    lines = []
    for t in terms:
        parts = [f"- {t.name} ({t.kind}): {t.definition}"]
        if t.sql:
            parts.append(f"  SQL as defined here: {t.sql}")
        if t.where:
            parts.append(f"  Where it lives: {t.where}")
        if t.synonyms:
            parts.append(f"  Also called: {', '.join(t.synonyms)}")
        lines.append("\n".join(parts))
    body = "\n".join(lines)
    if len(body) > max_chars:
        body = body[:max_chars].rsplit("\n", 1)[0] + "\n- (more terms matched; ask with lookup_business_terms)"
    return (
        "BUSINESS TERMS DEFINED IN THIS WORKSPACE. The request uses these words; use these meanings, "
        "this SQL and these columns, not a meaning of your own. If a definition does not fit the data, "
        "say so instead of changing it.\n" + body
    )


def glossary_block(workspace_id: str | None, text: str, app_state: dict | None = None) -> str:
    """The prompt block for the entries ``text`` mentions; empty when none or
    when the glossary cannot be read. Never raises."""
    try:
        if app_state is None:
            from revoluzen_flow.main import app_state as _state  # type: ignore
            app_state = _state
        store = (app_state or {}).get("glossary_store")
        if store is None:
            return ""
        return render_for_model(match_terms(store.list(workspace_id or "default"), text))
    except Exception:  # noqa: BLE001
        return ""


__all__ = [
    "GlossaryProblem", "GlossaryStore", "GlossaryTerm", "KINDS",
    "glossary_block", "match_terms", "render_for_model", "term_problems",
]
