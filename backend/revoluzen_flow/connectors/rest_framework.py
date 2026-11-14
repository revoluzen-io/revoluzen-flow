"""
Declarative REST connector framework.

A single node class (RestApiSourceNode) reads a JSON manifest describing
auth, base URL, streams (endpoints), and pagination — then executes the
selected stream and returns rows as a DuckDB relation.

This unlocks ~300 SaaS connectors with one Python class. Each new SaaS
integration is a ~40-line JSON file in `manifests/`.

Manifest schema (top-level):
{
  "id": "salesforce",                    // unique connector id, used as palette item
  "name": "Salesforce",                  // human label
  "description": "Salesforce CRM via REST API",
  "category": "saas",                    // palette grouping
  "base_url": "https://{instance}.salesforce.com",
  "auth": {
    "type": "oauth2" | "bearer" | "api_key" | "basic" | "none",
    // type=oauth2: token_url, client_id_param, client_secret_param, refresh_token_param
    // type=bearer: token_param
    // type=api_key: header_name (or query_param), key_param
    // type=basic: username_param, password_param
    "header_name": "Authorization",
    "header_template": "Bearer {token}"
  },
  "params": [
    {"name": "instance", "label": "Instance", "required": true},
    {"name": "access_token", "label": "Access Token", "required": true, "secret": true}
  ],
  "streams": [
    {
      "name": "accounts",
      "label": "Accounts",
      "path": "/services/data/v59.0/query",
      "method": "GET",
      "query": {"q": "SELECT Id, Name FROM Account LIMIT 200"},
      "data_path": "records",            // dot-path into JSON to find row array
      "pagination": {
        "type": "cursor",                // page_number | offset_limit | cursor | link_header | none
        "next_url_path": "nextRecordsUrl",
        "max_pages": 100
      }
    }
  ]
}

A stream can also talk XML, which is how SOAP services run:
  "response_format": "xml"               // the reply is XML; data_path is a row path such as //order
  "body_format": "xml",                  // body_text is XML; {param} values are escaped, and an
  "body_text": "<a><b>{value}</b></a>"   // element marked <!--Optional:--> is dropped when blank
("body_format": "form" sends form fields the same way.)
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, TYPE_CHECKING

# Stage 2.5b: duckdb only used for type annotations on _rows_to_relation
# and execute() returns.
if TYPE_CHECKING:
    import duckdb

from revoluzen_flow.common.http_reply import BODY_CONTENT_TYPES, accept_header, fault_in_reply
from revoluzen_flow.common.safe_xml import (
    SoapFaultError,
    XmlError,
    XmlReply,
    parse_xml,
    raise_for_soap_fault,
    render_xml_template,
    xml_placeholders,
)
from revoluzen_flow.ir.schema import StepType
from revoluzen_flow.nodes.base import BaseNode, ExecutionContext
from revoluzen_flow.nodes.registry import register
from revoluzen_flow.security.ssrf import API_SOURCE_ALLOW_PRIVATE_ENV, check_url

logger = logging.getLogger(__name__)


class PaginationLimitReached(RuntimeError):
    """A stream hit its ``max_pages`` cap while the API still had more pages.

    Raised instead of returning the partial result so a pipeline never
    reports success on silently truncated data. A stream that wants a
    sample can opt in with ``pagination.on_max_pages = "stop"``.
    """


# What to do when a stream reaches ``max_pages`` and the API says there is more:
#   error — raise PaginationLimitReached (default; no silent data loss)
#   stop  — keep the pages read so far, log a warning (sampling, previews)
VALID_ON_MAX_PAGES = ("error", "stop")


def _origin(url: str) -> tuple[str, str | None, int | None]:
    parsed = urllib.parse.urlsplit(url)
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Credentials in API URLs are not allowed")
    return parsed.scheme.lower(), parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)


class _SsrfGuardedRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Re-validate every redirect target against the SSRF policy.

    urllib follows redirects transparently, so checking only the initial
    URL would let a public host 302 us into the private network.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        if _origin(req.full_url) != _origin(newurl):
            raise ValueError("Cross-origin API redirects are not allowed")
        check_url(newurl, allow_private_env=API_SOURCE_ALLOW_PRIVATE_ENV)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_GUARDED_OPENER = urllib.request.build_opener(_SsrfGuardedRedirectHandler)


def _observe_response(url: str, headers: dict[str, str]) -> None:
    """Hand a live response's headers to Source Watch's passive detector.

    Imported lazily: `source_watch` needs `_GUARDED_OPENER` from this module
    for its scheduled probes, so a module-level import here would be circular.

    Swallows everything. This sits on the hot path of every connector fetch,
    and a telemetry-shaped side concern must never be the reason a customer's
    pipeline fails — including when `source_watch` is absent entirely, as in a
    trimmed-down deployment.
    """
    try:
        from .source_watch import observe_response

        observe_response(url, headers)
    except Exception:  # noqa: BLE001
        pass


_MANIFEST_DIR = os.path.join(os.path.dirname(__file__), "manifests")

# 2026-06-18 — writable, user-added connector store. Lives under the data dir
# (NOT the packaged read-only manifests/), so an admin/lead can add a connector
# in-app and have it persist + load without filesystem access or a restart.
# Resolved the same way Database resolves its path so the two agree.
_USER_DATA_DIR = os.environ.get("REVOLUZEN_FLOW_DATA_DIR") or os.path.join(os.getcwd(), "data")
_USER_MANIFEST_DIR = os.path.join(_USER_DATA_DIR, "connectors", "manifests")

_MANIFEST_CACHE: dict[str, "RestConnectorManifest"] = {}
# Ids that came from the user store (so the API/UI can mark them deletable and
# the loader never lets a user manifest shadow a shipped connector).
_USER_MANIFEST_IDS: set[str] = set()

# Connector ids are used as filenames + palette keys — keep them safe.
_SAFE_ID_RE = re.compile(r"[^a-z0-9_]+")


# ─────────────────────────── Manifest model ───────────────────────────

@dataclass
class RestConnectorManifest:
    id: str
    name: str
    description: str = ""
    category: str = "saas"
    # Confidence tier shown in the picker (certified | beta | community |
    # generated). Auto-set to "certified" at load when a <id>.v2.json cert
    # manifest ships alongside; an explicit `tier` in the JSON wins.
    tier: str = "beta"
    base_url: str = ""
    auth: dict[str, Any] = field(default_factory=dict)
    params: list[dict[str, Any]] = field(default_factory=list)
    streams: list[dict[str, Any]] = field(default_factory=list)
    headers: dict[str, str] = field(default_factory=dict)
    # 2026-05-23 (T1): manifest-level defaults merged into every stream's
    # query and headers before the per-stream values are applied. Lets
    # an OData manifest set `$format=json` + `sap-client={client}` once
    # instead of repeating it on every stream. Stream-level keys win
    # on conflict — defaults are a fallback, not an override.
    default_query: dict[str, Any] = field(default_factory=dict)
    default_headers: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RestConnectorManifest":
        return cls(
            id=data["id"],
            name=data.get("name", data["id"]),
            description=data.get("description", ""),
            category=data.get("category", "saas"),
            tier=data.get("tier", "beta"),
            base_url=data.get("base_url", ""),
            auth=data.get("auth", {}),
            params=data.get("params", []),
            streams=data.get("streams", []),
            headers=data.get("headers", {}),
            default_query=data.get("default_query", {}),
            default_headers=data.get("default_headers", {}),
        )

    def stream(self, name: str) -> dict[str, Any] | None:
        for s in self.streams:
            if s.get("name") == name:
                return s
        return None


def load_manifests(force: bool = False) -> dict[str, RestConnectorManifest]:
    """Scan the manifests/ directory and cache parsed manifests.

    Skips `*.v2.json` files — those follow the F0.1 spec consumed by the
    cert matrix endpoint (`revoluzen_flow.api.cert_matrix`), not this runtime
    loader. The runtime SaaS Connector node uses the v1 manifest format;
    a connector with both `<name>.json` (v1) and `<name>.v2.json` (v2 cert
    sample) ships only the v1 at runtime today. The v1→v2 runtime
    consumer is the post-1.0 connector roadmap.
    """
    if _MANIFEST_CACHE and not force:
        return _MANIFEST_CACHE
    _MANIFEST_CACHE.clear()
    _USER_MANIFEST_IDS.clear()

    # 1) Packaged (shipped) manifests. A `<id>.v2.json` cert sibling marks the
    #    connector CERTIFIED unless the v1 JSON pins an explicit tier.
    if os.path.isdir(_MANIFEST_DIR):
        certified_ids = {
            fn[: -len(".v2.json")]
            for fn in os.listdir(_MANIFEST_DIR)
            if fn.endswith(".v2.json")
        }
        for fname in sorted(os.listdir(_MANIFEST_DIR)):
            _load_manifest_file(_MANIFEST_DIR, fname, certified_ids, user_added=False)

    # 2) User-added manifests (writable store). Always Beta; never allowed to
    #    shadow a shipped connector id.
    if os.path.isdir(_USER_MANIFEST_DIR):
        for fname in sorted(os.listdir(_USER_MANIFEST_DIR)):
            _load_manifest_file(_USER_MANIFEST_DIR, fname, set(), user_added=True)

    return _MANIFEST_CACHE


def _load_manifest_file(
    directory: str, fname: str, certified_ids: set[str], *, user_added: bool
) -> None:
    """Parse one v1 manifest file into the cache. v2 cert files are skipped
    (different schema, read by the cert-matrix endpoint). Never raises."""
    if not fname.endswith(".json") or fname.endswith(".v2.json"):
        return
    fpath = os.path.join(directory, fname)
    try:
        with open(fpath, "r", encoding="utf-8") as f:
            data = json.load(f)
        if data.get("hidden"):
            return
        manifest = RestConnectorManifest.from_dict(data)
        if user_added:
            # A user manifest must never hijack a built-in connector id.
            if manifest.id in _MANIFEST_CACHE:
                return
            manifest.tier = "beta"  # user-added is always Beta — honest signal
            _USER_MANIFEST_IDS.add(manifest.id)
        elif "tier" not in data and manifest.id in certified_ids:
            manifest.tier = "certified"
        _MANIFEST_CACHE[manifest.id] = manifest
    except Exception as e:  # noqa: BLE001 — one bad file never breaks the rest
        print(f"[connectors] Failed to load {fname}: {e}")


# ─────────────────────── User-added connector store ───────────────────────

def _safe_connector_id(raw: str) -> str:
    return _SAFE_ID_RE.sub("_", str(raw or "").strip().lower()).strip("_")


def is_user_manifest(connector_id: str) -> bool:
    """True if this connector id was added by a user (deletable)."""
    load_manifests()
    return connector_id in _USER_MANIFEST_IDS


def user_manifest_ids() -> set[str]:
    load_manifests()
    return set(_USER_MANIFEST_IDS)


def save_user_manifest(data: dict[str, Any]) -> "RestConnectorManifest":
    """Persist a v1 runtime manifest to the writable user store as a Beta
    connector and refresh the cache so it is usable immediately (no restart).

    Raises ValueError on a malformed manifest or an id that collides with a
    shipped (built-in) connector."""
    if not isinstance(data, dict):
        raise ValueError("manifest must be a JSON object")
    cid = _safe_connector_id(data.get("id") or "")
    if not cid:
        raise ValueError("manifest 'id' is required (letters, digits, underscore)")
    normalized = {**data, "id": cid, "tier": "beta"}
    # Validate shape — RestConnectorManifest.from_dict raises if id is absent.
    manifest = RestConnectorManifest.from_dict(normalized)
    if not manifest.base_url and not manifest.streams:
        raise ValueError("manifest needs a base_url and at least one stream")
    if not manifest.streams:
        raise ValueError("manifest needs at least one stream")
    # Refuse to shadow a built-in connector (but allow overwriting an existing
    # user one — that's an edit).
    load_manifests()
    if cid in _MANIFEST_CACHE and cid not in _USER_MANIFEST_IDS:
        raise ValueError(f"'{cid}' is a built-in connector id — choose a different id")

    os.makedirs(_USER_MANIFEST_DIR, exist_ok=True)
    path = os.path.join(_USER_MANIFEST_DIR, f"{cid}.json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(normalized, f, indent=2)
    os.replace(tmp, path)
    load_manifests(force=True)  # live — appears in the picker on next fetch
    return manifest


def delete_user_manifest(connector_id: str) -> bool:
    """Remove a user-added connector. Returns False if it wasn't a user one."""
    cid = _safe_connector_id(connector_id)
    path = os.path.join(_USER_MANIFEST_DIR, f"{cid}.json")
    if not os.path.isfile(path):
        return False
    os.remove(path)
    load_manifests(force=True)
    return True


def list_manifests() -> list[RestConnectorManifest]:
    return list(load_manifests().values())


def get_manifest(connector_id: str) -> RestConnectorManifest | None:
    return load_manifests().get(connector_id)


def _connector_options() -> list[dict]:
    """Connector-picker options carrying a confidence tier. Non-certified tiers
    are suffixed into the label so the signal shows even in a plain <select>
    (a richer card picker can read the `tier` field directly)."""
    opts: list[dict] = []
    for m in list_manifests():
        tier = (getattr(m, "tier", "beta") or "beta").lower()
        label = m.name if tier == "certified" else f"{m.name} · {tier}"
        opts.append({"value": m.id, "label": label, "tier": tier})
    return opts


# ─────────────────────────── Template helpers ───────────────────────────

_TEMPLATE_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


def _interpolate(s: Any, env: dict[str, Any]) -> Any:
    """Replace `{name}` placeholders in strings using env values."""
    if not isinstance(s, str):
        return s
    def repl(m):
        key = m.group(1)
        val = env.get(key, "")
        return str(val) if val is not None else ""
    return _TEMPLATE_RE.sub(repl, s)


def _interpolate_dict(d: dict[str, Any], env: dict[str, Any]) -> dict[str, Any]:
    return {k: _interpolate(v, env) for k, v in (d or {}).items()}


def _deep_interpolate(v: Any, env: dict[str, Any]) -> Any:
    """Recursive template substitution — used for request bodies.

    Headers and query strings are flat key→string, so `_interpolate_dict`
    suffices for them. Bodies, however, can be nested: OpenAI's
    `messages: [{"role": "user", "content": "{prompt}"}]` would lose the
    `{prompt}` substitution under shallow interpolation. This walks
    dicts + lists + scalars and substitutes every string leaf.
    """
    if isinstance(v, str):
        return _interpolate(v, env)
    if isinstance(v, dict):
        return {k: _deep_interpolate(x, env) for k, x in v.items()}
    if isinstance(v, list):
        return [_deep_interpolate(x, env) for x in v]
    return v


def _dot_get(obj: Any, path: str) -> Any:
    """Walk dot-path; supports list traversal by returning first match.

    2026-05-25 — also handles keys that *literally contain a dot*. The
    motivating case is OData v4 pagination, whose continuation field is
    ``@odata.nextLink`` (one key, with a dot inside). Previously the
    split-on-dot logic looked for nested ``@odata → nextLink``, didn't
    find it, and v4 stopped after page 1.

    Resolution order at each step:
      1. Literal key lookup against the current dict (handles
         ``@odata.nextLink``, ``schema.field``, etc.).
      2. Fall through to dot-path traversal (handles nested ``d.__next``,
         ``data.cursor.next_id``, ``results.0.id``).

    If the literal key matches, we return immediately — no further
    traversal — so a real nested path like ``d.__next`` (where ``d``
    exists as its own key) keeps working.
    """
    if not path:
        return obj
    # Literal-key fast path: if the full path is itself a key in the
    # current object, return that value verbatim.
    if isinstance(obj, dict) and path in obj:
        return obj[path]
    cur = obj
    for part in path.split("."):
        if cur is None:
            return None
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list) and part.isdigit():
            idx = int(part)
            cur = cur[idx] if 0 <= idx < len(cur) else None
        else:
            return None
    return cur


# ─────────────────────────── Auth ───────────────────────────

def _build_auth_headers(manifest: RestConnectorManifest, params: dict[str, Any]) -> dict[str, str]:
    auth = manifest.auth or {}
    atype = (auth.get("type") or "none").lower()
    headers: dict[str, str] = {}

    if atype == "openapi":
        from revoluzen_flow.connectors.openapi_security import build_security
        return build_security(auth, params)[0]

    if atype == "none":
        return headers

    if atype == "bearer":
        token_param = auth.get("token_param", "access_token")
        token = params.get(token_param) or ""
        header_name = auth.get("header_name", "Authorization")
        template = auth.get("header_template", "Bearer {token}")
        headers[header_name] = template.replace("{token}", str(token))
        return headers

    if atype == "api_key":
        key_param = auth.get("key_param", "api_key")
        key_value = params.get(key_param) or ""
        if auth.get("header_name"):
            header_name = auth["header_name"]
            template = auth.get("header_template", "{key}")
            headers[header_name] = template.replace("{key}", str(key_value))
        # query_param handled in URL builder
        return headers

    if atype == "basic":
        u = str(params.get(auth.get("username_param", "username"), ""))
        p = str(params.get(auth.get("password_param", "password"), ""))
        token = base64.b64encode(f"{u}:{p}".encode()).decode()
        headers["Authorization"] = f"Basic {token}"
        return headers

    if atype == "oauth2":
        # If access token already provided, use it. Otherwise refresh.
        token = params.get(auth.get("access_token_param", "access_token"))
        if not token and auth.get("token_url"):
            token = _oauth2_refresh(manifest, params)
        headers["Authorization"] = f"Bearer {token or ''}"
        return headers

    return headers


def _oauth2_refresh(manifest: RestConnectorManifest, params: dict[str, Any]) -> str | None:
    """Best-effort refresh-token flow. Returns new access_token or None."""
    auth = manifest.auth or {}
    token_url = _interpolate(auth.get("token_url", ""), params)
    if not token_url:
        return None
    # This flow is grant_type=refresh_token, so it can't run without a refresh
    # token. Bail BEFORE the SSRF check / network so a credential-less call
    # (e.g. `tools/test_connector.py --dry-run`, which builds an auth-header
    # preview with no params) returns cleanly instead of crashing the SSRF
    # gate on an unresolved/templated token_url. A real run that supplies a
    # refresh_token is unaffected and still passes through check_url.
    refresh_token = params.get(auth.get("refresh_token_param", "refresh_token"), "")
    if not refresh_token:
        return None
    # SSRF gate outside the try: a policy block must surface loudly, not
    # degrade into "refresh silently failed".
    check_url(token_url, allow_private_env=API_SOURCE_ALLOW_PRIVATE_ENV)
    body = {
        "grant_type": "refresh_token",
        "client_id": params.get(auth.get("client_id_param", "client_id"), ""),
        "client_secret": params.get(auth.get("client_secret_param", "client_secret"), ""),
        "refresh_token": params.get(auth.get("refresh_token_param", "refresh_token"), ""),
    }
    try:
        data = urllib.parse.urlencode(body).encode()
        req = urllib.request.Request(
            token_url,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with _GUARDED_OPENER.open(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode())
        return payload.get("access_token")
    except Exception:
        return None


# ─────────────────────────── HTTP fetch + pagination ───────────────────────────

class _RawReply(dict):
    """A reply that is not JSON: ``{"raw": text}``, with the original bytes kept
    so an XML reply can be read in the encoding it declares."""

    def __init__(self, text: str, content: bytes):
        super().__init__(raw=text)
        self.content = content


def _xml_reply(payload: Any) -> XmlReply:
    """Read a reply as XML. ``{"raw": ...}`` is also what a recorded cassette replays."""
    if isinstance(payload, _RawReply):
        content: bytes | str = payload.content
    elif isinstance(payload, dict) and isinstance(payload.get("raw"), str):
        content = payload["raw"]
    else:
        raise XmlError("This stream expects an XML reply, but the API answered with JSON.")
    root = parse_xml(content)
    raise_for_soap_fault(root)
    return XmlReply(root)


def _rows_in(payload: Any, data_path: str) -> Any:
    if isinstance(payload, XmlReply):
        return payload.rows(data_path or "")
    return _dot_get(payload, data_path) if data_path else payload


def _without_secrets(text: str, manifest: RestConnectorManifest, params: dict[str, Any]) -> str:
    """A server may echo what it was sent, and its words end up in the run log."""
    for param in manifest.params or []:
        value = params.get(param.get("name"))
        if param.get("secret") and isinstance(value, str) and value:
            text = text.replace(value, "[redacted]")
    return text


def _http_request(
    url: str,
    headers: dict[str, str],
    method: str = "GET",
    body: Any = None,
    body_text: str | None = None,
) -> tuple[Any, dict[str, str]]:
    """HTTP request with method + body support.

    2026-06-01 audit upgrade: the original `_http_get` ignored
    `stream.method` entirely (silently sending GET against every
    endpoint) and dropped `stream.body` / `stream.body_text`. That
    broke every manifest stream meant to run as POST/PUT/DELETE —
    OpenAI chat_completions, MongoDB find, Snowflake statements,
    BigQuery query, ClickHouse SQL, Splunk search submit, etc. —
    even though the JSON looked correct.

    This function honors `method` (defaulting to GET) and sends
    exactly one of:
      * `body_text` — raw string payload (text/plain by default; the
        manifest is responsible for setting Content-Type via headers
        when it needs something else like form-encoded).
      * `body` — dict/list → JSON-encoded with
        Content-Type: application/json (only if the manifest didn't
        already set one).

    `_http_get` is preserved as a thin compatibility shim so any
    external caller keeps working.
    """
    method = (method or "GET").upper()
    out_headers = dict(headers)
    data: bytes | None = None

    if body_text is not None and body_text != "":
        data = body_text.encode("utf-8")
        # Don't override Content-Type — caller picks (text/plain,
        # application/x-www-form-urlencoded, etc.). Default only if
        # nothing was set anywhere.
        out_headers.setdefault("Content-Type", "text/plain")
    elif body is not None and body != {} and body != []:
        if isinstance(body, (dict, list)):
            data = json.dumps(body).encode("utf-8")
            out_headers.setdefault("Content-Type", "application/json")
        else:
            data = str(body).encode("utf-8")

    # Every framework fetch funnels through here — initial stream URLs and
    # every pagination follow (including server-supplied absolute next-URLs),
    # so this is the one SSRF choke point. Redirect targets are re-checked
    # by _SsrfGuardedRedirectHandler.
    check_url(url, allow_private_env=API_SOURCE_ALLOW_PRIVATE_ENV)
    req = urllib.request.Request(url, data=data, headers=out_headers, method=method)
    with _GUARDED_OPENER.open(req, timeout=60) as resp:
        resp_content = resp.read()
        resp_body = resp_content.decode("utf-8", errors="replace")
        resp_headers = dict(resp.headers.items())

    # Passive upstream-change detection. Vendors announce breaking changes in
    # RFC 8594 `Deprecation` / `Sunset` headers and echo the serving API
    # version, on every response — addressed to this caller, about the
    # endpoint it just called. Reading them here costs no request and needs no
    # vendor account, which makes it the earliest warning available and the
    # only one that works for a customer's own internal APIs. See
    # `source_watch.observe_response`; it is guaranteed not to raise.
    _observe_response(url, resp_headers)

    try:
        return json.loads(resp_body), resp_headers
    except json.JSONDecodeError:
        return _RawReply(resp_body, resp_content), resp_headers


def _http_get(url: str, headers: dict[str, str]) -> tuple[Any, dict[str, str]]:
    """Back-compat alias — always GET, no body. Prefer `_http_request`."""
    return _http_request(url, headers, method="GET")


# Pagination aliases — natural names found in vendor docs and in many
# pre-existing manifests, mapped to the framework's canonical types.
# Keeps manifests declarative-against-the-vendor instead of forcing
# every manifest author to memorise framework-internal naming.
#
# Aliases recognised:
#   page_token  → cursor       (Microsoft Graph $skiptoken, Google nextPageToken)
#   offset      → offset_limit (most SaaS pagination)
#   page        → page_number  (plain N+1 pagination)
#
# Param-name aliasing matches the wild-style names we've seen:
#   `next_field` / `cursor_field`
#   `param`      / `cursor_param` / `offset_param` / `page_param`
#   `count_param`/ `limit_param`  / `page_size_param`
def _normalize_pagination(p: dict[str, Any] | None) -> dict[str, Any]:
    if not p:
        return {"type": "none"}
    ptype = (p.get("type") or "none").lower()
    if ptype == "page_token":
        return {
            "type": "cursor",
            "cursor_field": p.get("cursor_field") or p.get("next_field", "next_cursor"),
            "cursor_param": p.get("cursor_param") or p.get("param", "cursor"),
            "next_url_path": p.get("next_url_path"),
            "max_pages": p.get("max_pages", 50),
        }
    if ptype == "offset":
        return {
            "type": "offset_limit",
            "offset_param": p.get("offset_param") or p.get("param", "offset"),
            "limit_param": p.get("limit_param") or p.get("count_param", "limit"),
            "page_size": p.get("page_size", 100),
            "max_pages": p.get("max_pages", 50),
        }
    if ptype == "page":
        return {
            "type": "page_number",
            "page_param": p.get("page_param") or p.get("param", "page"),
            "page_size_param": p.get("page_size_param") or p.get("count_param", "per_page"),
            "page_size": p.get("page_size", 100),
            "max_pages": p.get("max_pages", 50),
        }
    return p


def _build_url(base: str, path: str, query: dict[str, Any]) -> str:
    base = (base or "").rstrip("/")
    path = path or ""
    if not path.startswith("/") and not path.startswith("http"):
        path = "/" + path
    full = path if path.startswith("http") else base + path
    if query:
        # Drop empty values
        clean = {k: v for k, v in query.items() if v not in (None, "")}
        if clean:
            sep = "&" if "?" in full else "?"
            full = full + sep + urllib.parse.urlencode(clean)
    return full


def _probe_has_more(
    next_url: str,
    base: str,
    headers: dict[str, str],
    method: str,
    body: Any,
    body_text: str | None,
    data_path: str,
    expects_xml: bool = False,
) -> bool:
    """Fetch the page after the cap and report whether it holds any rows.

    Conservative: if the probe cannot be made or fails, assume there is
    more data, so a truncated read is never reported as complete.
    """
    try:
        if _origin(next_url) != _origin(base):
            return True
        payload, _ = _http_request(next_url, headers, method=method, body=body, body_text=body_text)
        if expects_xml:
            payload = _xml_reply(payload)
    except Exception:  # noqa: BLE001 — any failure means "cannot prove complete"
        return True
    return bool(_rows_in(payload, data_path))


def _template_values(manifest: RestConnectorManifest, params: dict[str, Any]) -> dict[str, Any]:
    """Each declared field's value: what the step says, else the connector's default."""
    return {
        p["name"]: params.get(p["name"]) if params.get(p["name"]) not in (None, "") else p.get("default", "")
        for p in manifest.params or []
        if p.get("name")
    }


def _render_body_text(
    text: str, body_format: str, manifest: RestConnectorManifest, params: dict[str, Any]
) -> str:
    """The text body to send. A value is escaped to suit the format it lands in."""
    if body_format == "xml":
        values = _template_values(manifest, params)
        missing = [
            p.get("label") or p["name"]
            for p in manifest.params or []
            if p.get("required") and p.get("name") in xml_placeholders(text) and values.get(p["name"]) in (None, "")
        ]
        if missing:
            raise ValueError(f"This connector needs a value for: {', '.join(missing)}")
        return render_xml_template(text, values)
    if body_format == "form":
        return _interpolate(text, {k: urllib.parse.quote_plus(str(v)) for k, v in params.items() if v is not None})
    return _interpolate(text, params)


def _execute_stream(
    manifest: RestConnectorManifest,
    stream: dict[str, Any],
    params: dict[str, Any],
    *,
    on_max_pages: str | None = None,
) -> list[dict[str, Any]]:
    """Run a stream and return all collected rows across pages.

    ``on_max_pages`` overrides the stream's ``pagination.on_max_pages``
    (see ``VALID_ON_MAX_PAGES``). Callers pass ``"stop"`` for previews,
    where a partial result is expected.
    """
    base = _interpolate(manifest.base_url, params)
    # Header layering (later overrides earlier):
    #   1. Accept: application/json default
    #   2. manifest.headers              — top-level static headers
    #   3. manifest.default_headers      — T1 (2026-05-23) shared defaults
    #   4. auth-derived headers          — built from params
    #   5. stream.headers                — per-stream overrides win
    expects_xml = str(stream.get("response_format") or "").lower() == "xml"
    headers = {"Accept": accept_header("xml") if expects_xml else "application/json", **(manifest.headers or {})}
    headers.update(_interpolate_dict(manifest.default_headers or {}, params))
    auth = stream.get("auth", manifest.auth) or {}
    if auth.get("type") == "openapi":
        from revoluzen_flow.connectors.openapi_security import build_security
        auth_headers, extra_query = build_security(auth, params)
        headers.update(_interpolate_dict(stream.get("headers", {}), params))
        protected = {key.lower() for key in auth_headers}
        headers = {key: value for key, value in headers.items() if key.lower() not in protected}
        headers.update(auth_headers)
    else:
        headers.update(_build_auth_headers(manifest, params))
        headers.update(_interpolate_dict(stream.get("headers", {}), params))
        extra_query = {}

    # api_key in query string
    if (auth.get("type") or "").lower() == "api_key" and auth.get("query_param"):
        extra_query[auth["query_param"]] = params.get(auth.get("key_param", "api_key"), "")

    path = _interpolate(stream.get("path", ""), params)
    # Query layering (later overrides earlier):
    #   1. manifest.default_query   — T1 (2026-05-23) shared defaults
    #   2. stream.query             — per-stream overrides win
    #   3. extra_query (api_key)    — auth-derived
    query: dict[str, Any] = _interpolate_dict(manifest.default_query or {}, params)
    query.update(_interpolate_dict(stream.get("query", {}), params))
    query.update(extra_query)
    # Strip empties: SAP rejects `sap-client=` with a 400. Manifest authors
    # set `{client}` in the default_query and leave the param blank when
    # the tenant doesn't use multi-client routing.
    query = {k: v for k, v in query.items() if v not in (None, "")}
    data_path = stream.get("data_path", "")

    # Normalize manifest-author-friendly pagination aliases
    # (page_token / offset / page) into the framework's canonical types
    # BEFORE deriving ptype / page_size below. Pre-2026-06-01 these
    # aliases silently fell through to single-page reads — manifests
    # using `page_token` or `offset` were paginating only page 1.
    pagination = _normalize_pagination(stream.get("pagination"))
    ptype = (pagination.get("type") or "none").lower()
    max_pages = int(pagination.get("max_pages", 50))
    # Read from the raw block: the alias normaliser rebuilds the dict and
    # would drop this key for page_token / offset / page streams.
    limit_mode = (
        on_max_pages
        or (stream.get("pagination") or {}).get("on_max_pages")
        or "error"
    ).lower()
    if limit_mode not in VALID_ON_MAX_PAGES:
        raise ValueError(
            f"pagination.on_max_pages must be one of {list(VALID_ON_MAX_PAGES)}, got {limit_mode!r}"
        )

    # Resolve method + body for this stream. Bodies are deep-template-
    # interpolated against the same params dict used for path/query so
    # nested `{prompt}` / `{sql}` / `{search_query}` in JSON arrays
    # (OpenAI chat messages, Druid native queries, etc.) get
    # substituted — not just top-level keys.
    method = (stream.get("method") or "GET").upper()
    body: Any = stream.get("body")
    if body is not None:
        body = _deep_interpolate(body, params)
    body_text = stream.get("body_text")
    body_format = str(stream.get("body_format") or "").lower()
    if body_format == "form" and isinstance(body, dict):
        body_text, body = urllib.parse.urlencode({k: "" if v is None else v for k, v in body.items()}), None
    elif isinstance(body_text, str):
        body_text = _render_body_text(body_text, body_format, manifest, params)
    if body_format in BODY_CONTENT_TYPES and body_text and not any(k.lower() == "content-type" for k in headers):
        headers["Content-Type"] = BODY_CONTENT_TYPES[body_format]

    page_size = int(pagination.get("page_size", 100))

    # Send the page size on the FIRST request too. Without it the API
    # answers page 1 with its own default size (often 10–50). For
    # offset_limit that short page looked like the last one, so the
    # stream stopped after page 1. A size the stream already sets in
    # its query wins and becomes the page size used from here on.
    size_param = None
    if ptype == "offset_limit":
        size_param = pagination.get("limit_param", "limit")
    elif ptype == "page_number":
        size_param = pagination.get("page_size_param", "per_page")
    if size_param:
        explicit = query.get(size_param)
        if explicit is not None and str(explicit).strip().isdigit():
            page_size = int(str(explicit).strip())
        query[size_param] = page_size

    rows: list[dict[str, Any]] = []
    next_url: str | None = _build_url(base, path, query)
    page = 1

    while next_url and page <= max_pages:
        if _origin(next_url) != _origin(base):
            raise ValueError("Cross-origin API endpoints and pagination are not allowed")
        try:
            payload, resp_headers = _http_request(
                next_url, headers, method=method, body=body, body_text=body_text
            )
            if expects_xml:
                payload = _xml_reply(payload)
        except urllib.error.HTTPError as e:
            # A SOAP service explains itself in the body of its 500.
            fault = fault_in_reply(e.read(1024 * 1024), e.headers.get("Content-Type", "")) if expects_xml else None
            if fault:
                raise RuntimeError(_without_secrets(str(SoapFaultError(fault)), manifest, params)) from None
            raise RuntimeError(f"API request failed (HTTP {e.code})") from None
        except SoapFaultError as e:
            raise RuntimeError(_without_secrets(str(e), manifest, params)) from None
        except XmlError as e:
            raise RuntimeError(f"The API's XML reply could not be read. {e}") from None
        except Exception:
            raise RuntimeError("API request failed; check connectivity and connection settings") from None

        chunk = _rows_in(payload, data_path)
        if isinstance(chunk, list):
            rows.extend(x if isinstance(x, dict) else {"value": x} for x in chunk)
        elif isinstance(chunk, dict):
            rows.append(chunk)

        # Compute next page
        if ptype == "none":
            next_url = None
        elif ptype == "page_number":
            page_param = pagination.get("page_param", "page")
            size_param = pagination.get("page_size_param", "per_page")
            if not chunk:
                next_url = None
            else:
                page += 1
                q = dict(query)
                q[page_param] = page
                q[size_param] = page_size
                next_url = _build_url(base, path, q)
        elif ptype == "offset_limit":
            offset_param = pagination.get("offset_param", "offset")
            limit_param = pagination.get("limit_param", "limit")
            if not chunk or (isinstance(chunk, list) and len(chunk) < page_size):
                next_url = None
            else:
                page += 1
                q = dict(query)
                q[offset_param] = (page - 1) * page_size
                q[limit_param] = page_size
                next_url = _build_url(base, path, q)
        elif ptype in ("cursor", "url"):
            # `url`: explicit next-URL follow. The response embeds a full
            # next URL (Twilio's `next_page_uri`, OData v4's
            # `@odata.nextLink`, OData v2's `d.__next`, Salesforce's
            # `nextRecordsUrl`). Same plumbing as cursor-with-next_url_path
            # — the two types differ only in intent: `cursor` may also
            # carry an opaque token while `url` always means a full URL.
            cursor_field = pagination.get("cursor_field", "next_cursor")
            cursor_param = pagination.get("cursor_param", "cursor")
            next_url_path = pagination.get("next_url_path")
            if next_url_path:
                next_relative = _dot_get(payload, next_url_path)
                if next_relative:
                    next_relative = str(next_relative)
                    # Three forms the server can return:
                    #   absolute URL ("https://…")  → follow as-is
                    #   server-relative ("/path")   → replace base's path
                    #   bare path ("entity?…")      → append to base path
                    # urljoin handles all three correctly given a fully-
                    # qualified base. The previous cursor implementation
                    # did string concat which double-prefixed the path
                    # on the server-relative form (T1 fix).
                    if next_relative.startswith(("http://", "https://")):
                        next_url = next_relative
                    else:
                        next_url = urllib.parse.urljoin(
                            base if base.endswith("/") else base + "/",
                            next_relative,
                        )
                else:
                    next_url = None
            elif ptype == "url":
                # url-type without next_url_path is a misconfiguration; stop
                # after first page rather than spin.
                next_url = None
            else:
                cursor = _dot_get(payload, cursor_field)
                if cursor:
                    page += 1
                    q = dict(query)
                    q[cursor_param] = cursor
                    next_url = _build_url(base, path, q)
                else:
                    next_url = None
        elif ptype == "link_header":
            link = resp_headers.get("Link", "") or resp_headers.get("link", "")
            m = re.search(r'<([^>]+)>;\s*rel="next"', link)
            next_url = m.group(1) if m else None
            page += 1
        else:
            next_url = None

    # The loop only exits with a next_url still set when the page cap cut
    # it short. That does not prove there is more data: page_number only
    # learns it is done from an empty page, and offset_limit from a short
    # one, so a stream that ends exactly on the cap still has a next_url.
    # One probe request settles it, so we never fail a complete read.
    if next_url and _probe_has_more(next_url, base, headers, method, body, body_text, data_path, expects_xml):
        msg = (
            f"Stream '{stream.get('name', '?')}' stopped at max_pages={max_pages} "
            f"after {len(rows)} rows, but the API reports more pages. "
            "Raise pagination.max_pages to read everything, or set "
            "pagination.on_max_pages='stop' to accept a partial result."
        )
        if limit_mode == "error":
            raise PaginationLimitReached(msg)
        logger.warning("%s (on_max_pages='stop'; returning partial result)", msg)

    return rows


def _rows_to_relation(conn: duckdb.DuckDBPyConnection, rows: list[dict[str, Any]]) -> duckdb.DuckDBPyRelation:
    if not rows:
        return conn.sql("SELECT NULL AS empty WHERE false")
    # JSON-stringify each row, then read via DuckDB's read_json_auto over a temp file.
    import tempfile
    fd, path = tempfile.mkstemp(suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, default=str) + "\n")
        return conn.sql(f"SELECT * FROM read_json_auto('{path}', format='newline_delimited')")
    finally:
        # DuckDB has read the file lazily — keep until relation is materialized.
        # We rely on caller fetching before deletion; safest to leave for OS temp cleanup.
        pass


# ─────────────────────────── Node ───────────────────────────

# What a connector's secret field may be called in a saved credential, by how the
# field's name ends. Generated connectors name them auth_<scheme>_key and so on.
_CREDENTIAL_KEYS_BY_SUFFIX: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("password", ("password", "pass", "passwd", "pwd")),
    ("username", ("username", "user", "user_name", "login")),
    ("secret", ("client_secret", "secret")),
    ("token", ("token", "access_token", "api_key")),
    ("key", ("api_key", "key", "apikey", "token")),
)


def _apply_saved_credential(manifest: RestConnectorManifest, env: dict[str, Any], ctx: ExecutionContext) -> None:
    """Fill a connector's empty secret fields from the credential the node points at.

    The pipeline stores only which credential to use. A key typed into the node
    would live in the pipeline definition, and every copy of it, in plain sight.
    """
    credential_id = env.pop("credential_id", None)
    if not credential_id:
        return
    store = (ctx.app_state or {}).get("credential_store")
    credential = store.get_raw(credential_id) if store else None
    workflow = getattr(ctx, "_workflow_ref", None)
    workspace = getattr(workflow, "workspace_id", None) or "default"
    if credential is None or (getattr(credential, "workspace_id", None) or "default") != workspace:
        raise ValueError("The saved credential this connector uses no longer exists. Choose another in the step's settings.")
    config = credential.config or {}
    for param in manifest.params or []:
        name = param.get("name")
        if not name or not param.get("secret") or env.get(name):
            continue
        aliases = next((keys for suffix, keys in _CREDENTIAL_KEYS_BY_SUFFIX if name.lower().endswith(suffix)), ())
        source = next((key for key in (name, *aliases) if config.get(key)), None)
        if source:
            env[name] = config[source]
    try:
        store.mark_used(credential_id, workspace_id=workspace)
    except Exception:  # noqa: BLE001 — "last used" must never break a run
        pass


@register(StepType.SAAS_CONNECTOR)
class SaaSConnectorNode(BaseNode):
    """Universal SaaS node — single palette entry that drives any loaded manifest.

    Replaces the 16 individual `rest:*` virtual palette items. The user picks
    a connector from a dropdown (populated from `list_manifests()`), then a
    stream within that connector, then fills in connector-specific params.
    Execution delegates to RestApiSourceNode.
    """

    display_name = "SaaS Connector"
    category = "source"
    description = "Read from a SaaS application, using a built-in connector or one you generated in the API Explorer"

    def execute(self, ctx: ExecutionContext) -> duckdb.DuckDBPyRelation:
        # Just delegate — params shape is identical.
        return RestApiSourceNode(self.params).execute(ctx)

    @staticmethod
    def default_params() -> dict[str, Any]:
        return {"connector_id": "", "stream": ""}

    @staticmethod
    def param_schema() -> list[dict]:
        return [
            {"name": "connector_id", "type": "string", "label": "SaaS Connector", "required": True,
             "options": _connector_options()},
            {"name": "stream", "type": "string", "label": "Stream", "required": True},
        ]


@register(StepType.REST_CONNECTOR)
class RestApiSourceNode(BaseNode):
    """Manifest-driven REST connector. One class, hundreds of SaaS apps."""

    display_name = "REST Connector"
    category = "source"
    description = "Declarative REST/SaaS connector — selects a manifest and stream"

    def execute(self, ctx: ExecutionContext) -> duckdb.DuckDBPyRelation:
        connector_id = self.params.get("connector_id")
        if not connector_id:
            raise ValueError("connector_id is required (id of the manifest to run)")
        manifest = get_manifest(connector_id)
        if not manifest:
            raise ValueError(f"Unknown connector '{connector_id}'. Available: {[m.id for m in list_manifests()]}")
        stream_name = self.params.get("stream") or (manifest.streams[0]["name"] if manifest.streams else None)
        if not stream_name:
            raise ValueError(f"Connector '{connector_id}' has no streams defined")
        stream = manifest.stream(stream_name)
        if not stream:
            raise ValueError(f"Stream '{stream_name}' not found in connector '{connector_id}'")

        # Merge connector-level params (excluding internals).
        env = {k: v for k, v in self.params.items() if not k.startswith("_")}
        _apply_saved_credential(manifest, env, ctx)
        # A preview is a sample by design; a real run must not truncate silently.
        rows = _execute_stream(
            manifest, stream, env,
            on_max_pages="stop" if getattr(ctx, "preview_mode", False) else None,
        )
        return _rows_to_relation(ctx.conn, rows)

    @staticmethod
    def default_params() -> dict[str, Any]:
        return {"connector_id": "", "stream": ""}

    @staticmethod
    def param_schema() -> list[dict]:
        return [
            {"name": "connector_id", "type": "string", "label": "Connector", "required": True,
             "options": _connector_options()},
            {"name": "stream", "type": "string", "label": "Stream", "required": True},
        ]
