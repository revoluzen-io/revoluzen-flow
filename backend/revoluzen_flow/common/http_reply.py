"""What an HTTP reply is, and which rows are in it.

Used by the HTTP Request step and the API Explorer. A reply is judged by its
Content-Type and then by its first bytes, because plenty of servers label XML
as text/plain, or send everything as application/octet-stream.
"""

from __future__ import annotations

import csv
import io
import json
import re
from typing import Any

from revoluzen_flow.common.safe_xml import (
    XmlError,
    local_name,
    looks_like_xml,
    parse_xml,
    raise_for_soap_fault,
    rows_from_xml,
    soap_fault,
)

__all__ = [
    "BODY_CONTENT_TYPES",
    "accept_header",
    "decode_text",
    "describe_binary",
    "fault_in_reply",
    "html_title",
    "media_type",
    "parse_csv",
    "reply_kind",
    "rows_from_reply",
]

BODY_CONTENT_TYPES = {
    "json": "application/json",
    "xml": "application/xml",
    "form": "application/x-www-form-urlencoded",
    "text": "text/plain; charset=utf-8",
}

_ACCEPT = {
    "auto": "application/json, application/xml;q=0.9, text/xml;q=0.9, */*;q=0.5",
    "json": "application/json",
    "xml": "application/xml, text/xml;q=0.9",
    "text": "*/*",
}

_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "PNG image"),
    (b"\xff\xd8\xff", "JPEG image"),
    (b"GIF87a", "GIF image"),
    (b"GIF89a", "GIF image"),
    (b"%PDF-", "PDF document"),
    (b"PK\x03\x04", "ZIP archive or Office document"),
    (b"\x1f\x8b", "gzip-compressed data"),
    (b"\xd0\xcf\x11\xe0", "older Office document"),
    (b"PAR1", "Parquet file"),
)

_BINARY_FAMILIES = ("image/", "audio/", "video/", "font/")
_BINARY_TYPES = frozenset({
    "application/pdf", "application/zip", "application/gzip", "application/x-gzip", "application/x-tar",
})
_BOMS: tuple[tuple[bytes, str], ...] = (
    (b"\xef\xbb\xbf", "utf-8-sig"),
    (b"\xff\xfe\x00\x00", "utf-32"),
    (b"\x00\x00\xfe\xff", "utf-32"),
    (b"\xff\xfe", "utf-16"),
    (b"\xfe\xff", "utf-16"),
)


def media_type(content_type: str) -> str:
    return (content_type or "").split(";", 1)[0].strip().lower()


def _charset(content_type: str) -> str:
    match = re.search(r"charset=[\"']?([\w.\-:]+)", content_type or "", re.I)
    return match.group(1) if match else ""


def decode_text(raw: bytes, content_type: str = "") -> str:
    """Text from bytes: byte-order mark first, then the declared charset, then UTF-8."""
    for mark, encoding in _BOMS:
        if raw.startswith(mark):
            return raw.decode(encoding, errors="replace")
    try:
        return raw.decode(_charset(content_type) or "utf-8", errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


def _text_head(raw: bytes) -> str | None:
    """The start of the reply as text, or None when it is not text at all."""
    head = raw[:4096]
    for mark, encoding in _BOMS:
        if head.startswith(mark):
            return head.decode(encoding, errors="replace").lstrip("﻿ \t\r\n")
    if b"\x00" in head:
        return None
    return head.decode("utf-8", errors="replace").lstrip()


def reply_kind(content_type: str, raw: bytes) -> str:
    """One of: empty, binary, json, html, xml, csv, text."""
    if not raw:
        return "empty"
    media = media_type(content_type)
    if any(raw.startswith(magic) for magic, _ in _MAGIC):
        return "binary"
    if media.startswith(_BINARY_FAMILIES) and not media.endswith("+xml"):
        return "binary"
    if media in _BINARY_TYPES:
        return "binary"
    head = _text_head(raw)
    if head is None:
        return "binary"
    lowered = head.lower()
    if media in ("text/html", "application/xhtml+xml") or lowered.startswith(("<!doctype html", "<html")):
        return "html"
    if media == "application/json" or media.endswith("+json"):
        return "json"
    if media in ("application/xml", "text/xml") or media.endswith("+xml"):
        return "xml"
    if media in ("text/csv", "application/csv", "text/tab-separated-values"):
        return "csv"
    if head.startswith(("{", "[")):
        return "json"
    if looks_like_xml(head.encode("utf-8", errors="replace")):
        return "xml"
    return "text"


def describe_binary(content_type: str, raw: bytes) -> str:
    """Plain words for binary data, from its first bytes when those are conclusive."""
    for magic, description in _MAGIC:
        if raw.startswith(magic):
            return description
    return media_type(content_type) or "binary data"


def html_title(text: str) -> str:
    match = re.search(r"<title[^>]*>(.*?)</title>", text[:65536], re.I | re.S)
    return " ".join(match.group(1).split())[:200] if match else ""


def parse_csv(text: str, content_type: str = "") -> list[dict[str, Any]]:
    """CSV or tab-separated text as rows, with the first line as the column names."""
    if media_type(content_type) == "text/tab-separated-values":
        delimiter = "\t"
    else:
        try:
            delimiter = csv.Sniffer().sniff(text[:8192], delimiters=",;\t|").delimiter
        except csv.Error:
            delimiter = ","
    return [
        {str(key): value for key, value in row.items() if key is not None}
        for row in csv.DictReader(io.StringIO(text), delimiter=delimiter)
    ]


def accept_header(reply_format: str) -> str:
    return _ACCEPT.get(reply_format, _ACCEPT["auto"])


def fault_in_reply(raw: bytes, content_type: str) -> dict[str, str] | None:
    """The SOAP fault in an error reply, when there is one to read."""
    if reply_kind(content_type, raw) != "xml":
        return None
    try:
        return soap_fault(parse_xml(raw))
    except XmlError:
        return None


def _json_rows(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        for key in ("data", "results", "items", "records", "rows", "values"):
            if key in value and isinstance(value[key], list):
                return value[key]
        return [value]
    return [{"response": str(value)}]


def _xml_rows(raw: bytes, row_path: str) -> list[dict[str, Any]]:
    root = parse_xml(raw)
    raise_for_soap_fault(root)
    return rows_from_xml(root, row_path)


def rows_from_reply(
    raw: bytes,
    content_type: str,
    *,
    reply_format: str = "auto",
    row_path: str = "",
    label: str = "HTTP Request",
) -> list[dict[str, Any]]:
    """The rows in a reply.

    ``auto`` follows what the reply says it is. A web page or a file is an
    error rather than a row, since that is nearly always a wrong address or a
    login page standing in for the data. Choosing ``text`` keeps either one as
    a single row, if that really is what is wanted.
    """
    fmt = (reply_format or "auto").lower()
    if fmt == "text":
        return [{"response": decode_text(raw, content_type)}] if raw else []
    if fmt == "json":
        if not raw:
            return []
        try:
            return _json_rows(json.loads(decode_text(raw, content_type)))
        except ValueError:
            raise ValueError(f"{label}: expected JSON, but the reply is not valid JSON.") from None
    if fmt == "xml":
        if not raw:
            return []
        return _xml_rows(raw, row_path)

    kind = reply_kind(content_type, raw)
    if kind == "empty":
        return []
    if kind == "binary":
        raise ValueError(f"{label}: the reply is a file ({describe_binary(content_type, raw)}), not data this step can read.")
    if kind == "html":
        raise ValueError(
            f"{label}: the reply is a web page, not data. Check the address and sign-in, "
            "or set the reply format to Plain text to keep the page as text."
        )
    if kind == "json":
        try:
            return _json_rows(json.loads(decode_text(raw, content_type)))
        except ValueError:
            if media_type(content_type) == "application/json" or media_type(content_type).endswith("+json"):
                raise ValueError(f"{label}: the reply says it is JSON, but it is not valid JSON.") from None
            return [{"response": decode_text(raw, content_type)}]
    if kind == "xml":
        declared = media_type(content_type)
        try:
            root = parse_xml(raw)
        except XmlError:
            if declared in ("application/xml", "text/xml") or declared.endswith("+xml"):
                raise
            # It only started with "<". Nothing claimed it was XML.
            return [{"response": decode_text(raw, content_type)}]
        if local_name(root.tag).lower() == "html":
            raise ValueError(f"{label}: the reply is a web page, not data. Check the address and sign-in.")
        raise_for_soap_fault(root)
        return rows_from_xml(root, row_path)
    return [{"response": decode_text(raw, content_type)}]
