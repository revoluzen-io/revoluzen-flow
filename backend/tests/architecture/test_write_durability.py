"""Ratchet: mutating SQL must go through a committing path.

`Database.execute()` and `Database.execute_with_retry()` do not commit. The
connection runs at sqlite3's default `isolation_level=''`, so the first
INSERT/UPDATE/DELETE opens an implicit transaction that stays open until
something commits on that same (thread-local) connection.

Measured 2026-10-23, before the fix: `POST /api/collaboration/{id}/comments`
returned HTTP 200, a second connection could not see the row, and the row
was gone after an abrupt process exit. The server acknowledged a write it
then lost.

This test fails when a NEW function issues mutating SQL through a
non-committing method without committing. Use one of:

    db.execute_write(sql, params)                 # one statement, durable
    db.execute_write_with_retry(sql, params)      # ... with lock retry
    with db.transaction() as tx: ...              # several, atomic

The baseline below is the set that existed at the freeze and has not been
converted yet -- mostly schema migrations and internal helpers where the
caller owns the transaction. Shrinking it is welcome; growing it needs a
reason in the entry.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / "revoluzen_flow"

MUTATING = re.compile(r"^\s*(INSERT|UPDATE|DELETE|REPLACE)\b", re.I)
NON_COMMITTING = {"execute", "executemany", "execute_with_retry"}
DB_RECEIVERS = {"_db", "db", "self._db", "self.db", "self._database", "database"}

# Modules whose `execute` is a DuckDB / driver handle, not storage.Database.
SKIP_DIRS = {"nodes", "engine", "compile", "connectors", "extraction"}

# Frozen 2026-10-23 and EMPTY: the conversion pass covered every site this
# detector can see, so there is nothing to grandfather. Keep it empty. If a
# genuinely caller-managed transaction needs an exemption, add it here with
# a one-line reason rather than loosening the detector.
#
# "Zero" is bounded by what the detector looks at: a literal (or f-string)
# INSERT/UPDATE/DELETE/REPLACE passed to execute / executemany /
# execute_with_retry on a receiver that looks like a Database handle,
# outside SKIP_DIRS. SQL built dynamically, or issued through a handle
# named something else, is not covered — this is a ratchet, not a proof.
BASELINE: frozenset[str] = frozenset()


def _receiver(attr: ast.Attribute) -> str:
    v = attr.value
    if isinstance(v, ast.Name):
        return v.id
    if isinstance(v, ast.Attribute) and isinstance(v.value, ast.Name):
        return f"{v.value.id}.{v.attr}"
    return ""


def _sql(call: ast.Call) -> str | None:
    if not call.args:
        return None
    a = call.args[0]
    if isinstance(a, ast.Constant) and isinstance(a.value, str):
        return a.value
    if isinstance(a, ast.JoinedStr):
        return "".join(
            v.value for v in a.values
            if isinstance(v, ast.Constant) and isinstance(v.value, str)
        )
    return None


class _Scan(ast.NodeVisitor):
    def __init__(self) -> None:
        self.writes: list[int] = []
        self.commits = 0
        self.ctx = 0

    def visit_Call(self, node: ast.Call) -> None:
        fn = node.func
        if isinstance(fn, ast.Attribute):
            if fn.attr == "commit":
                self.commits += 1
            elif fn.attr in NON_COMMITTING and _receiver(fn) in DB_RECEIVERS:
                sql = _sql(node)
                if sql and MUTATING.match(sql):
                    self.writes.append(node.lineno)
        self.generic_visit(node)

    def visit_With(self, node: ast.With) -> None:
        for item in node.items:
            if "transaction" in ast.dump(item.context_expr):
                self.ctx += 1
        self.generic_visit(node)


def _offenders() -> dict[str, list[int]]:
    out: dict[str, list[int]] = {}
    for path in sorted(ROOT.rglob("*.py")):
        rel = path.relative_to(ROOT)
        if rel.parts and rel.parts[0] in SKIP_DIRS:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            scan = _Scan()
            for child in fn.body:
                scan.visit(child)
            if scan.writes and not scan.commits and not scan.ctx:
                out[f"{rel.as_posix()}::{fn.name}"] = scan.writes
    return out


def test_no_new_uncommitted_writes():
    found = _offenders()
    new = {k: v for k, v in found.items() if k not in BASELINE}
    assert not new, (
        "Mutating SQL with no commit — the caller can be told the write "
        "succeeded and then lose it on an abrupt exit. Use "
        "db.execute_write(...), db.execute_write_with_retry(...), or "
        "`with db.transaction()`:\n  "
        + "\n  ".join(f"{k} (line {v[0]})" for k, v in sorted(new.items()))
    )


def test_baseline_not_stale():
    """Informational: baseline entries that have since been converted."""
    gone = set(BASELINE) - set(_offenders())
    if gone:
        print(f"[write-durability] {len(gone)} baseline entries now safe — trim them.")
