"""OpenAPI / Swagger -> Revoluzen Flow REST connector manifest (DRAFT).

Turns an OpenAPI 3 or Swagger 2 spec into a draft manifest for the
manifest-driven REST framework (``rest_framework.py``), so the long tail of
REST APIs becomes a paste-a-spec operation instead of a hand-written
connector. This is the scaling lesson: a connector FACTORY, not hand-written
connectors.

The output is a DRAFT — the auth params, stream `data_path`, and pagination
usually need a human pass + a live test before it ships. Pure function, no
network: the caller fetches the spec (SSRF-guarded) and passes the parsed
dict here. The emitted auth blocks match ``_build_auth_headers`` exactly.
"""
from __future__ import annotations

import re
from typing import Any

from revoluzen_flow.connectors.openapi_security import normalize_security


def _slug(s: str) -> str:
    out = re.sub(r"[^a-z0-9]+", "_", (s or "").strip().lower()).strip("_")
    return out or "api"


def _auth_from_spec(spec: dict) -> tuple[dict, list[dict]]:
    """Declarations alone do not require authentication; security does."""
    return normalize_security(spec, spec.get("security", []))


def _base_url(spec: dict) -> str:
    servers = spec.get("servers")
    if isinstance(servers, list) and servers and isinstance(servers[0], dict) and servers[0].get("url"):
        return str(servers[0]["url"]).rstrip("/")
    host = spec.get("host")
    if host:
        scheme = (spec.get("schemes") or ["https"])[0]
        return f"{scheme}://{host}{spec.get('basePath', '') or ''}".rstrip("/")
    return ""


# Fields that describe a page of results rather than a record.
_ENVELOPE_FIELDS = frozenset({
    "total", "total_count", "totalcount", "count", "size", "length",
    "page", "page_size", "pagesize", "per_page", "perpage", "page_count", "pagecount",
    "limit", "offset", "next", "next_page", "nextpage", "previous", "prev",
    "cursor", "next_cursor", "nextcursor", "has_more", "hasmore",
    "links", "_links", "meta", "metadata", "paging", "pagination",
    "status", "success", "message", "error", "errors",
})


def _resolve(spec: dict, schema: Any) -> dict:
    """Follow local $refs, so a schema defined under components reads like an inline one."""
    for _ in range(8):
        ref = schema.get("$ref") if isinstance(schema, dict) else None
        if not isinstance(ref, str) or not ref.startswith("#/"):
            break
        node: Any = spec
        for part in ref[2:].split("/"):
            node = node.get(part.replace("~1", "/").replace("~0", "~")) if isinstance(node, dict) else None
        schema = node
    return schema if isinstance(schema, dict) else {}


def _rows_path(spec: dict, operation: dict) -> str:
    """Where the records are in a successful reply, when the spec makes that clear.

    Many APIs wrap their list, as in {"items": [...], "total": 6}. With no path,
    that whole reply becomes one row. It only counts as a wrapper when one list
    sits beside nothing but paging details: {"id": 7, "lines": [...]} is a single
    record that happens to contain a list, and stays one row.
    """
    responses = operation.get("responses")
    reply = _resolve(spec, (responses or {}).get("200")) if isinstance(responses, dict) else {}
    content = reply.get("content") if isinstance(reply.get("content"), dict) else {}
    media = content.get("application/json") or next(iter(content.values()), {})
    schema = _resolve(spec, (media or {}).get("schema") or reply.get("schema"))

    path: list[str] = []
    for _ in range(3):
        properties = schema.get("properties")
        if schema.get("type") == "array" or not isinstance(properties, dict):
            break
        body = {name: _resolve(spec, prop) for name, prop in properties.items() if name.lower() not in _ENVELOPE_FIELDS}
        if len(body) != 1:
            return ""
        name, schema = next(iter(body.items()))
        path.append(name)
    return ".".join(path) if schema.get("type") == "array" else ""


def manifest_from_openapi(
    spec: Any, *, connector_id: str | None = None,
    base_url: str | None = None, max_streams: int = 50,
) -> dict:
    """Parse an OpenAPI 3 / Swagger 2 spec into a draft manifest dict.

    GET operations become read streams (the framework is source-focused).
    Raises ValueError on a non-object spec.
    """
    if not isinstance(spec, dict):
        raise ValueError("OpenAPI spec must be a JSON object")
    info = spec.get("info") or {}
    title = str(info.get("title") or "API")
    auth, params = _auth_from_spec(spec)
    parameter_map = {p["name"]: p for p in params}

    streams: list[dict] = []
    seen: set[str] = set()
    for path, methods in (spec.get("paths") or {}).items():
        if not isinstance(methods, dict):
            continue
        get = methods.get("get")
        if not isinstance(get, dict):
            continue
        base = get.get("operationId") or path.strip("/").replace("/", "_") or "root"
        name = _slug(base)
        if name in seen:
            name = _slug(f"{base}_{path}")
        if not name or name in seen:
            continue
        seen.add(name)
        operation_auth, operation_params = normalize_security(spec, get.get("security", spec.get("security", [])))
        parameter_map.update({p["name"]: p for p in operation_params})
        streams.append({
            "name": name,
            "label": str(get.get("summary") or name)[:60],
            "path": path,
            "method": "GET",
            "auth": operation_auth,
            # Pagination is left for the human pass — the spec rarely says how an API pages.
            **({"data_path": rows} if (rows := _rows_path(spec, get)) else {}),
        })
        if len(streams) >= max_streams:
            break

    return {
        "id": _slug(connector_id or title),
        "name": title,
        "description": str(info.get("description") or "")[:200],
        "category": "saas",
        "tier": "generated",  # never auto-ships at Certified — needs review + test
        "base_url": (base_url or _base_url(spec)).rstrip("/"),
        "auth": auth,
        "params": list(parameter_map.values()),
        "streams": streams,
    }
