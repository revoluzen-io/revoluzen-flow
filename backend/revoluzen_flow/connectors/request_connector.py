"""A connector made from one request someone has tried.

Used for XML APIs and SOAP services, where the working request is itself the
best description of the connector: the same address, headers and body, and a
row path for the reply. Nothing secret is written into the connector. Sign-in
becomes fields to fill in, or a saved credential, when the connector is used.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qsl, urlsplit
from xml.etree import ElementTree as ET

from revoluzen_flow.common.safe_xml import find_elements, xml_placeholders

__all__ = ["SENSITIVE_NAME", "manifest_from_request"]

SENSITIVE_NAME = re.compile(r"password|passwd|pwd|secret|token|authorization|api.?key|cookie", re.I)

_NOT_SAVED_HEADERS = frozenset({
    "host", "content-length", "transfer-encoding", "connection",
    "proxy-authorization", "accept-encoding", "authorization", "cookie",
})
_LITERAL_ELEMENT = re.compile(r"<((?:[\w.\-]+:)?([\w.\-]+))((?:\s[^<>]*)?)>([^<>]+)</\1>")
_ONLY_PLACEHOLDER = re.compile(r"\{[a-zA-Z_][a-zA-Z0-9_]*\}")

_AUTH_FIELDS: dict[str, tuple[dict[str, Any], list[tuple[str, str]]]] = {
    "none": ({"type": "none"}, []),
    "basic": (
        {"type": "basic", "username_param": "auth_username", "password_param": "auth_password"},
        [("auth_username", "Username"), ("auth_password", "Password")],
    ),
    "bearer": ({"type": "bearer", "token_param": "auth_token"}, [("auth_token", "Bearer token")]),
    "api_key": ({"type": "api_key", "key_param": "auth_key"}, [("auth_key", "API key")]),
    "api_key_query": ({"type": "api_key", "key_param": "auth_key"}, [("auth_key", "API key")]),
}


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (text or "").strip().lower()).strip("_")


def _lift_literal_secrets(body: str, taken: set[str], warnings: list[str]) -> tuple[str, list[str]]:
    """A password typed into the envelope must not be saved in the connector.
    Each one becomes a field, filled in when the connector runs."""
    lifted: list[str] = []

    def swap(match: re.Match[str]) -> str:
        whole, local, attributes, value = match.groups()
        if not SENSITIVE_NAME.search(local) or not value.strip() or _ONLY_PLACEHOLDER.fullmatch(value.strip()):
            return match.group(0)
        name = re.sub(r"\W", "_", local, flags=re.ASCII)
        number = 2
        while name in taken:
            name = f"{local}_{number}"
            number += 1
        taken.add(name)
        lifted.append(name)
        warnings.append(f"The value in <{whole}> looks like a secret, so it was not saved. It is now a field to fill in.")
        return f"<{whole}{attributes}>{{{name}}}</{whole}>"

    return _LITERAL_ELEMENT.sub(swap, body), lifted


def manifest_from_request(
    *,
    connector_id: str,
    display_name: str | None,
    category: str,
    url: str,
    method: str,
    headers: dict[str, str],
    query: dict[str, str],
    body: str,
    body_type: str,
    fields: list[dict[str, Any]],
    row_path: str,
    auth_type: str,
    key_name: str,
    operation: str = "",
) -> tuple[dict[str, Any], list[str]]:
    """A runnable connector with one stream, and anything the person should know about it."""
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username is not None or parts.password is not None:
        raise ValueError("Use an HTTP(S) address without a username or password in it.")
    if row_path.strip():
        find_elements(ET.Element("row"), row_path)  # Refuses a path that cannot work.

    warnings: list[str] = []
    saved_query: dict[str, str] = {}
    for name, value in [*parse_qsl(parts.query, keep_blank_values=True), *query.items()]:
        if SENSITIVE_NAME.search(name):
            warnings.append(f"The query parameter {name} looks like a secret, so it was not saved.")
        else:
            saved_query[name] = value

    saved_headers: dict[str, str] = {}
    for name, value in headers.items():
        if name.lower() in _NOT_SAVED_HEADERS or SENSITIVE_NAME.search(name):
            warnings.append(f"The header {name} was not saved. Sign-in is set when the connector is used.")
        else:
            saved_headers[name] = value

    auth, auth_fields = _AUTH_FIELDS[auth_type]
    auth = dict(auth)
    if auth_type == "api_key":
        auth["header_name"] = key_name
    elif auth_type == "api_key_query":
        auth["query_param"] = key_name

    params: list[dict[str, Any]] = [
        {"name": name, "label": label, "required": True, "secret": True} for name, label in auth_fields
    ]
    taken = {param["name"] for param in params}

    text = body if method not in ("GET", "HEAD", "OPTIONS") else ""
    described = {field["name"]: field for field in fields}
    if text and body_type == "xml":
        text, lifted = _lift_literal_secrets(text, taken | set(xml_placeholders(text)), warnings)
        for name in xml_placeholders(text):
            field = described.get(name, {})
            param: dict[str, Any] = {
                "name": name,
                "label": field.get("label") or name,
                "required": name in lifted or bool(field.get("required", name not in described)),
            }
            if SENSITIVE_NAME.search(name) or name in lifted:
                param["secret"] = True
            elif field.get("default") not in (None, ""):
                param["default"] = field["default"]
            if name not in taken or name in lifted:
                taken.add(name)
                params.append(param)

    label = operation or parts.path.rstrip("/").rsplit("/", 1)[-1] or parts.hostname
    stream: dict[str, Any] = {
        "name": _slug(label) or "request",
        "label": label[:60],
        "path": parts.path or "/",
        "method": method,
        "response_format": "xml",
    }
    if saved_query:
        stream["query"] = saved_query
    if saved_headers:
        stream["headers"] = saved_headers
    if row_path.strip():
        stream["data_path"] = row_path.strip()
    if text:
        stream["body_format"] = body_type
        stream["body_text"] = text

    return {
        "id": _slug(connector_id),
        "name": (display_name or "").strip() or connector_id,
        "description": f"{operation} operation" if operation else f"{method} {parts.path or '/'}",
        "category": category,
        "tier": "generated",
        "base_url": f"{parts.scheme}://{parts.netloc}",
        "auth": auth,
        "params": params,
        "streams": [stream],
    }, warnings
