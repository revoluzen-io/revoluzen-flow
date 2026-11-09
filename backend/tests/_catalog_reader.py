"""Read ``frontend/src/templates/catalog.ts`` as data.

The catalog is TypeScript, but each template is an object literal of strings,
numbers, booleans, arrays and objects. The only arithmetic is a step's
``position`` (``X_STEP * 2``, ``Y_BASE - 160``), which no test here cares
about; the reader keeps such a value as an ``Expr`` (its source text) instead
of evaluating it. Anything else it cannot follow stops with a message that says
where, so a catalog change this reader does not understand fails loudly
instead of being skipped.

Used by ``test_template_steps_match_nodes.py``. The Data Quality test keeps its
own, stricter reader of ``data_quality`` params.
"""
from __future__ import annotations

import pathlib
import re
from dataclasses import dataclass, field
from typing import Any

CATALOG = pathlib.Path(__file__).parents[2] / "frontend" / "src" / "templates" / "catalog.ts"

_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")
_IDENT = re.compile(r"[A-Za-z_$][\w$]*")
_ESCAPES = {"n": "\n", "t": "\t", "r": "\r"}


class Expr(str):
    """Source text of a value the reader does not evaluate, e.g. ``X_STEP * 2``."""


def _skip(src: str, i: int) -> int:
    while i < len(src):
        if src[i] in " \t\r\n":
            i += 1
        elif src.startswith("//", i):
            end = src.find("\n", i)
            i = len(src) if end < 0 else end
        elif src.startswith("/*", i):
            i = src.index("*/", i) + 2
        else:
            break
    return i


def _string(src: str, i: int) -> tuple[str, int]:
    quote, out, i = src[i], [], i + 1
    while src[i] != quote:
        if src[i] == "\\":
            if src[i + 1] == "u":
                out.append(chr(int(src[i + 2:i + 6], 16)))
                i += 6
            else:
                out.append(_ESCAPES.get(src[i + 1], src[i + 1]))
                i += 2
        else:
            out.append(src[i])
            i += 1
    return "".join(out), i + 1


def _expr(src: str, i: int) -> tuple[Expr, int]:
    """Text up to the next ``,``, ``}`` or ``]`` that is not inside brackets or a string."""
    start, depth = i, 0
    while i < len(src):
        ch = src[i]
        if ch in "'\"":
            _, i = _string(src, i)
            continue
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            if depth == 0:
                break
            depth -= 1
        elif ch == "," and depth == 0:
            break
        i += 1
    text = src[start:i].strip()
    if not text:
        raise AssertionError(f"catalog.ts: expected a value at {src[start:start + 60]!r}")
    return Expr(text), i


def _value(src: str, i: int) -> tuple[Any, int]:
    i = _skip(src, i)
    ch = src[i]
    if ch in "'\"":
        return _string(src, i)
    if ch == "[":
        items, i = [], _skip(src, i + 1)
        while src[i] != "]":
            item, i = _value(src, i)
            items.append(item)
            i = _skip(src, i)
            if src[i] == ",":
                i = _skip(src, i + 1)
        return items, i + 1
    if ch == "{":
        obj, i = {}, _skip(src, i + 1)
        while src[i] != "}":
            if src[i] in "'\"":
                key, i = _string(src, i)
            else:
                m = _IDENT.match(src, i)
                assert m, f"catalog.ts: cannot read an object key at {src[i:i + 40]!r}"
                key, i = m.group(0), m.end()
            i = _skip(src, i)
            assert src[i] == ":", f"catalog.ts: expected ':' after key {key!r}"
            obj[key], i = _value(src, i + 1)
            i = _skip(src, i)
            if src[i] == ",":
                i = _skip(src, i + 1)
        return obj, i + 1
    m = _NUMBER.match(src, i)
    if m:
        after = _skip(src, m.end())
        if src[after] in ",}]":
            text = m.group(0)
            return (float(text) if "." in text else int(text)), m.end()
    for word, val in (("true", True), ("false", False), ("null", None)):
        if src.startswith(word, i) and not (src[i + len(word):i + len(word) + 1].isalnum()):
            return val, i + len(word)
    return _expr(src, i)


@dataclass(frozen=True)
class Step:
    id: str
    type: str
    label: str
    params: dict

    def __str__(self) -> str:
        return f"{self.id} {self.type} \"{self.label}\""


@dataclass(frozen=True)
class Template:
    id: str
    name: str
    tagline: str
    description: str
    complexity: str
    category: str
    tags: list
    steps: list[Step]
    connections: list[dict]
    parameters: list[dict] = field(default_factory=list)


def read_catalog(path: pathlib.Path = CATALOG) -> list[Template]:
    src = path.read_text(encoding="utf-8")
    marker = re.search(r"export const TEMPLATE_CATALOG[^=]*=\s*", src)
    assert marker, "catalog.ts: TEMPLATE_CATALOG not found"
    raw, _ = _value(src, marker.end())
    assert isinstance(raw, list) and raw, "catalog.ts: TEMPLATE_CATALOG is not a list of templates"
    templates: list[Template] = []
    for t in raw:
        steps = [
            Step(id=s["id"], type=s["type"], label=s.get("label", ""), params=s.get("params", {}))
            for s in t["steps"]
        ]
        templates.append(Template(
            id=t["id"], name=t["name"], tagline=t.get("tagline", ""),
            description=t.get("description", ""), complexity=t.get("complexity", ""),
            category=t.get("category", ""), tags=t.get("tags", []), steps=steps,
            connections=t["connections"], parameters=t.get("parameters", []),
        ))
    return templates
