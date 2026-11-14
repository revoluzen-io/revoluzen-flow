"""Bounded, ephemeral API inspection; never creates a connector or saves secrets."""
from __future__ import annotations

import base64
import contextlib
import csv
import http.client
import ipaddress
import json
import os
import re
import socket
import ssl
import time
from typing import Literal
from urllib.parse import parse_qsl, urlencode, urlsplit

import anyio
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from revoluzen_flow.auth.deps import require_min_rank
from revoluzen_flow.common.http_reply import (
    BODY_CONTENT_TYPES,
    accept_header,
    decode_text,
    describe_binary,
    html_title,
    media_type,
    parse_csv,
    reply_kind,
)
from revoluzen_flow.common.safe_xml import (
    XmlError,
    element_to_row,
    find_elements,
    local_name,
    parse_xml,
    pretty_xml,
    render_xml_template,
    row_path_candidates,
    soap_fault,
    xml_structure,
    xml_to_data,
)
from revoluzen_flow.connectors.ai_authoring import parse_spec_text
from revoluzen_flow.connectors.request_connector import SENSITIVE_NAME as SENSITIVE
from revoluzen_flow.connectors.wsdl_import import read_wsdl

router = APIRouter(prefix="/explorer", dependencies=[Depends(require_min_rank("developer"))])
LIMIT = 256 * 1024
SPEC_LIMIT = 32 * 1024 * 1024
WSDL_LIMIT = 4 * 1024 * 1024
REFERENCE_URLS = {
    'stripe': 'https://raw.githubusercontent.com/stripe/openapi/master/openapi/spec3.json',
    'github': 'https://raw.githubusercontent.com/github/rest-api-description/main/descriptions/api.github.com/api.github.com.json',
    'slack': 'https://raw.githubusercontent.com/slackapi/slack-api-specs/master/web-api/slack_web_openapi_v2.json',
    'twilio': 'https://raw.githubusercontent.com/twilio/twilio-oai/main/spec/json/twilio_api_v2010.json',
    'plaid': 'https://raw.githubusercontent.com/plaid/plaid-openapi/master/2020-09-14.yml',
    'digitalocean': 'https://api-engineering.nyc3.cdn.digitaloceanspaces.com/spec-ci/DigitalOcean-public.v2.yaml',
}


class InspectRequest(BaseModel):
    url: str = Field(min_length=1, max_length=2048)
    method: Literal['GET', 'HEAD', 'OPTIONS', 'POST', 'PUT', 'PATCH', 'DELETE'] = 'GET'
    headers: dict[str, str] = Field(default_factory=dict, max_length=40)
    query: dict[str, str] = Field(default_factory=dict, max_length=40)
    body: str = Field(default='', max_length=32768)
    body_type: Literal['json', 'xml', 'form', 'text'] = 'json'
    # For an XML body with {name} placeholders. See render_xml_template.
    body_values: dict[str, str] = Field(default_factory=dict, max_length=300)
    auth_type: Literal['none', 'bearer', 'basic', 'api_key', 'api_key_query'] = 'none'
    token: str = Field(default='', max_length=8192)
    username: str = Field(default='', max_length=512)
    password: str = Field(default='', max_length=8192)
    key_name: str = Field(default='X-API-Key', max_length=128)
    confirm_write: bool = False


def _tls_context() -> ssl.SSLContext:
    """Verifying TLS context with TLS 1.0/1.1 refused.

    `ssl.create_default_context()` verifies certificates and hostnames, but it
    leaves TLS 1.0 and 1.1 permitted on builds whose OpenSSL still offers them.
    The Explorer reaches arbitrary third-party hosts, so pin the floor
    explicitly rather than inherit whatever the platform happens to allow.
    """
    ctx = ssl.create_default_context()
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx

def _target(url):
    parsed = urlsplit(url)
    if parsed.scheme not in ('https', 'http') or not parsed.hostname or parsed.username is not None or parsed.password is not None or parsed.fragment:
        raise ValueError('Use an HTTP(S) URL without embedded credentials or a fragment')
    addresses = socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == 'https' else 80), type=socket.SOCK_STREAM)
    allow_private = os.getenv('REVOLUZEN_FLOW_API_SOURCE_ALLOW_PRIVATE', '').lower() in ('1', 'true', 'yes')
    for entry in addresses:
        ip = ipaddress.ip_address(entry[4][0])
        if ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified or (not allow_private and (ip.is_private or ip.is_loopback)):
            raise ValueError('Target blocked by network policy; internal APIs require REVOLUZEN_FLOW_API_SOURCE_ALLOW_PRIVATE=1')
    if not addresses:
        raise ValueError('No address found')
    return parsed, addresses[0]


def _request_parts(req):
    if sum(len(k) + len(v) for k, v in [*req.headers.items(), *req.query.items()]) > 32768:
        raise ValueError('Headers and query parameters exceed 32 KB')
    if '{' in req.url or '}' in req.url:
        raise ValueError('Replace path and server placeholders with actual values before sending')
    if req.method not in ('GET', 'HEAD', 'OPTIONS') and not req.confirm_write:
        raise ValueError('Confirm this request may modify remote data')
    headers = {'Accept': accept_header('auto'), 'Accept-Encoding': 'identity'}
    for key, value in req.headers.items():
        if not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", key) or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError('Invalid request header')
        if key.lower() in ('host', 'content-length', 'transfer-encoding', 'connection', 'proxy-authorization', 'accept-encoding', 'authorization', 'cookie'):
            raise ValueError('Use authentication fields; transport and cookie headers cannot be overridden')
        headers = {k: v for k, v in headers.items() if k.lower() != key.lower()}
        headers[key] = value
    query = dict(req.query)
    secrets = [req.token, req.password, req.username]
    if req.auth_type == 'basic':
        if not req.username or ':' in req.username:
            raise ValueError('Basic authentication requires a username without a colon')
        encoded = base64.b64encode(f'{req.username}:{req.password}'.encode()).decode()
        headers['Authorization'] = 'Basic ' + encoded
        secrets.append(encoded)
    elif req.auth_type != 'none':
        if not req.token or any(ord(c) < 32 or ord(c) == 127 for c in req.token):
            raise ValueError('Authentication value is missing or invalid')
        if req.auth_type == 'bearer':
            headers['Authorization'] = 'Bearer ' + req.token
        else:
            if not re.fullmatch(r'[A-Za-z0-9_-]+', req.key_name) or req.key_name.lower() in ('host', 'content-length', 'transfer-encoding', 'connection'):
                raise ValueError('Invalid API key name')
            if req.auth_type == 'api_key':
                headers = {k: v for k, v in headers.items() if k.lower() != req.key_name.lower()}
                headers[req.key_name] = req.token
            else:
                query[req.key_name] = req.token
    secrets.extend(req.headers.values())
    secrets.extend(v for k, v in req.query.items() if SENSITIVE.search(k))
    secrets.extend(v for k, v in req.body_values.items() if SENSITIVE.search(k))
    return headers, query, [s for s in secrets if s]


def _redact(value, secrets, depth=0):
    if depth > 16:
        return '[depth limit]'
    if isinstance(value, dict):
        return {k: '[redacted]' if SENSITIVE.search(k) else _redact(v, secrets, depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v, secrets, depth + 1) for v in value[:100]]
    if isinstance(value, str):
        for secret in sorted(secrets, key=len, reverse=True):
            value = value.replace(secret, '[redacted]')
    return value


def _exchange(parsed, address, method, path, headers, body, *, limit=LIMIT, deadline=15):
    """One request to an address already checked. Returns (response, bytes, was_cut_off)."""
    started = time.monotonic()
    conn = http.client.HTTPConnection(parsed.hostname, parsed.port or (443 if parsed.scheme == 'https' else 80), timeout=deadline)
    sock = None
    try:
        # Pin the validated address. TLS still verifies the original hostname.
        family, socktype, proto, _, sockaddr = address
        sock = socket.socket(family, socktype, proto)
        sock.settimeout(deadline)
        sock.connect(sockaddr)
        if parsed.scheme == 'https':
            sock = _tls_context().wrap_socket(sock, server_hostname=parsed.hostname)
        conn.sock = sock
        conn.request(method, path, body=body, headers=headers)
        response = conn.getresponse()
        raw = bytearray()
        while len(raw) <= limit:
            remaining = deadline - (time.monotonic() - started)
            if remaining <= 0:
                raise TimeoutError()
            # http.client closes this object as soon as a server says
            # "Connection: close". The response still reads through its own
            # handle, with the timeout set above, so that isn't a failure.
            with contextlib.suppress(OSError):
                sock.settimeout(remaining)
            chunk = response.read1(min(16384, limit + 1 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
        return response, bytes(raw[:limit]), len(raw) > limit
    finally:
        conn.close()
        if sock is not None:
            sock.close()


def _present_xml(raw, base, text, secrets):
    """An XML reply, ready for each view: formatted text, data, rows and structure."""
    try:
        root = parse_xml(raw)
    except XmlError as exc:
        return {**base, 'kind': 'text', 'text': _redact(text, secrets), 'notice': str(exc)}
    if local_name(root.tag).lower() == 'html':
        return {**base, 'kind': 'html', 'text': _redact(text, secrets), 'title': _redact(html_title(text), secrets)}
    records = [
        {**candidate, 'rows': _redact([element_to_row(e) for e in find_elements(root, candidate['path'])[:100]], secrets)}
        for candidate in row_path_candidates(root)
    ]
    return {
        **base,
        'data': _redact(xml_to_data(root), secrets),
        'text': _redact(pretty_xml(raw, SENSITIVE), secrets),
        'xml': {'records': records, 'structure': xml_structure(root), 'fault': _redact(soap_fault(root), secrets)},
    }


def _present_reply(raw, content_type, truncated, secrets):
    """What came back, described so the page can show it for what it is."""
    kind = reply_kind(content_type, raw)
    base = {'kind': kind, 'content_type': media_type(content_type), 'data': None, 'text': ''}
    if kind == 'empty':
        return base
    if kind == 'binary':
        return {**base, 'binary': describe_binary(content_type, raw)}
    text = decode_text(raw, content_type)
    if truncated:
        return {**base, 'text': _redact(text, secrets)}
    if kind == 'json':
        try:
            return {**base, 'data': _redact(json.loads(text), secrets)}
        except (ValueError, RecursionError):
            return {**base, 'kind': 'text', 'text': _redact(text, secrets)}
    if kind == 'xml':
        return _present_xml(raw, base, text, secrets)
    if kind == 'html':
        return {**base, 'text': _redact(text, secrets), 'title': _redact(html_title(text), secrets)}
    if kind == 'csv':
        try:
            rows = parse_csv(text, content_type)
        except csv.Error:
            return {**base, 'kind': 'text', 'text': _redact(text, secrets)}
        return {**base, 'data': _redact(rows, secrets), 'text': _redact(text, secrets), 'row_count': len(rows)}
    return {**base, 'text': _redact(text, secrets)}


def inspect_request(req: InspectRequest):
    headers, query, secrets = _request_parts(req)
    parsed, address = _target(req.url)
    original_query = parse_qsl(parsed.query, keep_blank_values=True)
    secrets.extend(v for k, v in original_query if SENSITIVE.search(k))
    query_string = urlencode([(k, v) for k, v in original_query if k not in query] + list(query.items()))
    path = (parsed.path or '/') + ('?' + query_string if query_string else '')
    body = render_xml_template(req.body, req.body_values) if req.body and req.body_type == 'xml' and req.body_values else req.body
    if body and not any(k.lower() == 'content-type' for k in headers):
        headers['Content-Type'] = BODY_CONTENT_TYPES[req.body_type]
    started = time.monotonic()
    response, raw, truncated = _exchange(parsed, address, req.method, path, headers, body.encode() if body else None)
    return {'status': response.status, 'elapsed_ms': round((time.monotonic() - started) * 1000),
            'bytes': len(raw), 'truncated': truncated,
            'headers': {k: v for k, v in response.getheaders() if k.lower() in ('content-type', 'date', 'retry-after')},
            'sample_limit': 100,
            **_present_reply(raw, response.getheader('Content-Type') or '', truncated, secrets)}


@router.post('/request')
async def send_request(req: InspectRequest):
    try:
        return await anyio.to_thread.run_sync(inspect_request, req)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    except Exception:
        raise HTTPException(502, 'Request failed. Check the address, network access and TLS certificate trust.') from None


class DiscoverRequest(BaseModel):
    text: str = Field(max_length=2 * 1024 * 1024)


def discover_spec(spec):
    try:
        if not isinstance(spec, dict) or not (spec.get('openapi') or spec.get('swagger')):
            raise ValueError('Not an OpenAPI specification')
        paths = spec.get('paths')
        if not isinstance(paths, dict):
            raise ValueError('Specification must contain paths')
        servers = spec.get('servers') or []
        base = servers[0].get('url', '') if servers else ''
        if not base and spec.get('host'):
            base = (spec.get('schemes') or ['https'])[0] + '://' + spec['host'] + spec.get('basePath', '')
        operations = []
        for path, item in paths.items():
            if not isinstance(path, str) or not path.startswith('/'):
                raise ValueError('Invalid operation path')
            if not isinstance(item, dict):
                continue
            for method, operation in item.items():
                if not isinstance(method, str):
                    raise ValueError('Invalid operation method')
                if method.upper() not in ('GET', 'HEAD', 'OPTIONS', 'POST', 'PUT', 'PATCH', 'DELETE') or not isinstance(operation, dict):
                    continue
                security = operation.get('security', spec.get('security', []))
                if not isinstance(security, list) or any(not isinstance(option, dict) or any(not isinstance(name, str) for name in option) for option in security):
                    raise ValueError('Invalid security requirements')
                operations.append({'path': path, 'method': method.upper(), 'summary': str(operation.get('summary', ''))[:200],
                                   'security': security})
                if len(operations) >= 5000:
                    break
            if len(operations) >= 5000:
                break
        return {'base_url': base, 'operations': operations, 'limit': 5000,
                'limit_reached': len(operations) == 5000}
    except (ValueError, TypeError, AttributeError, IndexError):
        raise HTTPException(400, 'Invalid OpenAPI JSON/YAML specification') from None


@router.post('/discover')
async def discover(req: DiscoverRequest):
    try:
        return await anyio.to_thread.run_sync(lambda: discover_spec(parse_spec_text(req.text)))
    except (ValueError, RecursionError):
        raise HTTPException(400, 'Invalid OpenAPI JSON/YAML specification (maximum 2 MB)') from None


class WsdlRequest(BaseModel):
    text: str = Field(default='', max_length=2 * 1024 * 1024)
    url: str = Field(default='', max_length=2048)
    auth_type: Literal['none', 'bearer', 'basic', 'api_key', 'api_key_query'] = 'none'
    token: str = Field(default='', max_length=8192)
    username: str = Field(default='', max_length=512)
    password: str = Field(default='', max_length=8192)
    key_name: str = Field(default='X-API-Key', max_length=128)


def load_wsdl(req: WsdlRequest):
    """Read a WSDL that was pasted, or fetch it and the files it imports from its own site."""
    if req.text.strip():
        return read_wsdl(req.text)
    if not req.url.strip():
        raise ValueError('Paste a WSDL, or enter its address.')
    start = req.url.strip()
    signed = InspectRequest(url=start, auth_type=req.auth_type, token=req.token, username=req.username,
                            password=req.password, key_name=req.key_name)
    headers, query, _ = _request_parts(signed)
    headers['Accept'] = 'text/xml, application/xml;q=0.9, */*;q=0.5'
    site = urlsplit(start)[:2]

    def fetch(url: str) -> bytes:
        # Sign-in goes with these requests, so a document cannot send them elsewhere.
        if urlsplit(url)[:2] != site:
            raise ValueError('it is on a different site, so it was not fetched')
        parsed, address = _target(url)
        query_string = urlencode(parse_qsl(parsed.query, keep_blank_values=True) + list(query.items()))
        path = (parsed.path or '/') + ('?' + query_string if query_string else '')
        response, raw, truncated = _exchange(parsed, address, 'GET', path, headers, None, limit=WSDL_LIMIT, deadline=30)
        if response.status != 200:
            raise ValueError(f'the server answered HTTP {response.status}. Redirects are not followed.')
        if truncated:
            raise ValueError('it is larger than 4 MB')
        return raw

    return read_wsdl(fetch(start), url=start, fetch=fetch)


@router.post('/wsdl')
async def import_wsdl(req: WsdlRequest):
    try:
        return await anyio.to_thread.run_sync(load_wsdl, req)
    except (ValueError, RecursionError) as exc:
        raise HTTPException(400, str(exc)[:300]) from None
    except Exception:
        raise HTTPException(502, 'The WSDL could not be downloaded. Check the address, network access and TLS certificate trust.') from None


class ReferenceRequest(BaseModel):
    reference: Literal['stripe', 'github', 'slack', 'twilio', 'plaid', 'digitalocean']


def load_reference(reference: str):
    # Only curated public URLs; never forward app credentials or follow redirects.
    url = REFERENCE_URLS[reference]
    parsed, address = _target(url)
    started = time.monotonic()
    conn = http.client.HTTPConnection(parsed.hostname, 443, timeout=30)
    sock = None
    try:
        family, kind, proto, _, sockaddr = address
        sock = socket.socket(family, kind, proto)
        sock.settimeout(30)
        sock.connect(sockaddr)
        sock = _tls_context().wrap_socket(sock, server_hostname=parsed.hostname)
        conn.sock = sock
        conn.request('GET', parsed.path, headers={'Accept-Encoding': 'identity', 'User-Agent': 'Revoluzen Flow-API-Explorer'})
        response = conn.getresponse()
        if response.status != 200:
            raise ValueError(f'Specification source returned HTTP {response.status}. Redirects are not followed.')
        length = response.getheader('Content-Length')
        if length and int(length) > SPEC_LIMIT:
            raise ValueError('Specification exceeds the 32 MB remote import limit')
        raw = bytearray()
        while len(raw) <= SPEC_LIMIT:
            remaining = 30 - (time.monotonic() - started)
            if remaining <= 0:
                raise TimeoutError()
            sock.settimeout(remaining)
            chunk = response.read1(min(65536, SPEC_LIMIT + 1 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
        if len(raw) > SPEC_LIMIT:
            raise ValueError('Specification exceeds the 32 MB remote import limit')
        text = raw.decode('utf-8-sig')
        # Large JSON specs use the dedicated bounded path. YAML retains its tighter
        # shared parser limit because expansion can consume substantially more memory.
        spec = json.loads(text) if text.lstrip().startswith('{') else parse_spec_text(text)
        result = discover_spec(spec)
        return {**result, 'reference': reference, 'source_url': url, 'bytes': len(raw)}
    finally:
        conn.close()
        if sock is not None:
            sock.close()


@router.post('/reference')
async def import_reference(req: ReferenceRequest):
    try:
        return await anyio.to_thread.run_sync(load_reference, req.reference)
    except HTTPException:
        raise
    except (ValueError, RecursionError) as exc:
        raise HTTPException(400, str(exc)[:240]) from None
    except Exception:
        raise HTTPException(502, 'Specification download failed. Check network access and TLS certificate trust, then retry.') from None
