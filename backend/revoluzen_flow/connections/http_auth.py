"""Signing a web request the way a saved API connection says to.

The REST API connection form offers seven ways to sign in, and its Test button
honours all of them. The pipeline steps did not: an API key in a header, a key
in the query string, a custom header and OAuth client credentials were sent as
a Bearer token, or not at all, so a connection could pass its test and then
fail in a pipeline. Steps that call an API through a saved connection use this
module, so they sign in the way the test did.
"""

from __future__ import annotations

import base64
import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from revoluzen_flow.security.ssrf import check_url

__all__ = [
    "HTTP_CONNECTION_TYPES",
    "connection_base_url",
    "connection_url",
    "request_auth",
    "same_site",
    "ssl_context_for",
    "with_query",
    "without_query",
]

# Connection types that are a web address plus a sign-in.
HTTP_CONNECTION_TYPES = frozenset({
    "rest_api", "graphql",
    "oracle_api", "oracle_fusion", "oracle_bip",
    "sap_s4hana", "sap_successfactors",
})


def connection_base_url(config: dict[str, Any]) -> str:
    return str(config.get("base_url") or config.get("url") or config.get("endpoint") or "").strip()


def _site(url: str) -> tuple[str, str | None, int | None]:
    parts = urllib.parse.urlsplit(url)
    return parts.scheme.lower(), parts.hostname, parts.port or (443 if parts.scheme.lower() == "https" else 80)


def same_site(first: str, second: str) -> bool:
    return _site(first) == _site(second)


def connection_url(base_url: str, url: str) -> str:
    """The address to call: the step's own, read against its connection's.

    A path is added to the connection's address, and ``/`` or nothing means
    that address itself. A full address must be on the connection's site,
    because the connection's sign-in goes with the request.
    """
    base = base_url.strip().rstrip("/")
    text = (url or "").strip()
    if text in ("", "/"):
        return base
    if text.lower().startswith(("http://", "https://")):
        if not same_site(text, base):
            raise ValueError(
                "The address is on a different site from the step's connection, so the "
                "connection's sign-in was not sent. Use a path such as /orders, or the connection's own site."
            )
        return text
    return f"{base}{text}" if text.startswith(("/", "?")) else f"{base}/{text}"


def with_query(url: str, query: dict[str, str]) -> str:
    if not query:
        return url
    return f"{url}{'&' if '?' in url else '?'}{urllib.parse.urlencode(query)}"


def without_query(url: str) -> str:
    """An address fit for a message. A key may be in its query string."""
    return url.split("?", 1)[0]


def ssl_context_for(config: dict[str, Any]) -> ssl.SSLContext | None:
    """None to verify certificates as usual; a context that does not, when the
    connection was saved with Verify SSL turned off."""
    raw = config.get("ssl_verify", True)
    verify = raw.strip().lower() not in ("false", "0", "no", "off") if isinstance(raw, str) else bool(raw)
    if verify:
        return None
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


def _extra_headers(value: Any) -> dict[str, str]:
    """The connection's Custom Headers: one ``Name: Value`` per line."""
    if isinstance(value, dict):
        return {str(name): str(text) for name, text in value.items() if name}
    headers: dict[str, str] = {}
    if isinstance(value, str):
        for line in value.splitlines():
            name, colon, text = line.partition(":")
            if colon and name.strip():
                headers[name.strip()] = text.strip()
    return headers


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


def _client_credentials_token(config: dict[str, Any], allow_private_env: str | None, timeout: int) -> str:
    token_url = str(config.get("token_url") or "").strip()
    client_id = config.get("client_id") or ""
    if not token_url or not client_id:
        raise ValueError("The connection's OAuth 2.0 sign-in needs a token URL and a client ID.")
    check_url(token_url, allow_private_env=allow_private_env)
    form = {"grant_type": "client_credentials", "client_id": client_id, "client_secret": config.get("client_secret") or ""}
    if str(config.get("oauth_scope") or "").strip():
        form["scope"] = str(config["oauth_scope"]).strip()
    request = urllib.request.Request(
        token_url, data=urllib.parse.urlencode(form).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
    )
    handlers: list[Any] = [_RefuseRedirects()]
    context = ssl_context_for(config)
    if context is not None:
        handlers.append(urllib.request.HTTPSHandler(context=context))
    try:
        with urllib.request.build_opener(*handlers).open(request, timeout=timeout) as reply:
            token = json.loads(reply.read().decode("utf-8") or "{}").get("access_token")
    except urllib.error.HTTPError as exc:
        raise ValueError(f"The connection's OAuth 2.0 token request was refused (HTTP {exc.code}).") from None
    except (urllib.error.URLError, OSError, ValueError, AttributeError):
        raise ValueError("The connection's OAuth 2.0 token could not be fetched. Check its token URL.") from None
    if not token:
        raise ValueError("The connection's OAuth 2.0 token reply had no access_token.")
    return str(token)


def request_auth(
    config: dict[str, Any], *, allow_private_env: str | None = None, timeout: int = 30,
) -> tuple[dict[str, str], dict[str, str]]:
    """The headers and query parameters that sign a request for this connection."""
    headers: dict[str, str] = {}
    query: dict[str, str] = {}
    secret = config.get("token") or config.get("api_key") or ""
    kind = str(config.get("auth_type") or "").strip().lower() or ("bearer" if secret else "none")
    if kind == "bearer":
        if config.get("api_key") and not config.get("token"):
            # Connections saved by older forms keep the key, its header and its prefix apart.
            prefix = str(config.get("api_key_prefix", "Bearer") or "")
            headers[str(config.get("api_key_header") or "Authorization")] = f"{prefix} {config['api_key']}".strip()
        elif secret:
            headers["Authorization"] = f"Bearer {secret}"
    elif kind == "api_key":
        if secret:
            prefix = str(config.get("api_key_prefix") or "")
            name = str(config.get("api_key_header") or "").strip() or "Authorization"
            headers[name] = f"{prefix} {secret}".strip()
    elif kind == "api_key_query":
        if secret:
            query[str(config.get("api_key_param") or "").strip() or "api_key"] = str(secret)
    elif kind == "basic":
        user = config.get("username") or config.get("user") or ""
        password = config.get("password") or ""
        if user or password:
            headers["Authorization"] = "Basic " + base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
    elif kind == "oauth2_cc":
        headers["Authorization"] = "Bearer " + _client_credentials_token(config, allow_private_env, timeout)
    elif kind == "custom_header":
        name = str(config.get("custom_header_name") or "").strip()
        if name and config.get("custom_header_value"):
            headers[name] = str(config["custom_header_value"])
    return {**_extra_headers(config.get("headers")), **headers}, query
