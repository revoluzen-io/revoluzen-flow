"""XML and SOAP, from the parser up to a pipeline that produces rows.

Everything talks to a local test server. The end of this file walks the whole
route a person takes: import a WSDL, try an operation in the API Explorer, save
it as a connector, and run that connector as a pipeline step.
"""

from __future__ import annotations

import json
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import parse_qs

import duckdb
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from revoluzen_flow.api import api_explorer as explorer
from revoluzen_flow.api.connector_authoring import router as authoring_router
from revoluzen_flow.common import safe_xml
from revoluzen_flow.common.http_reply import decode_text, reply_kind, rows_from_reply
from revoluzen_flow.common.safe_xml import (
    MAX_DEPTH,
    SoapFaultError,
    UnsafeXmlError,
    XmlError,
    detect_row_path,
    find_elements,
    parse_xml,
    parse_xml_file,
    pretty_xml,
    render_xml_template,
    row_path_candidates,
    rows_from_xml,
    soap_fault,
    xml_placeholders,
    xml_structure,
    xml_to_data,
)
from revoluzen_flow.connections.http_auth import (
    connection_url,
    request_auth,
    same_site,
    ssl_context_for,
    with_query,
    without_query,
)
from revoluzen_flow.connections.models import CONNECTION_CATEGORIES, CONNECTION_TYPES
from revoluzen_flow.nodes.activities import ApiSourceNode
from revoluzen_flow.connectors import rest_framework as rf
from revoluzen_flow.connectors.request_connector import manifest_from_request
from revoluzen_flow.connectors.wsdl_import import WsdlError, read_wsdl
from revoluzen_flow.nodes.base import ExecutionContext
from revoluzen_flow.nodes.flow_control import HttpRequestNode
from revoluzen_flow.nodes.sources import XmlSourceNode

SOAP_ENV = "http://schemas.xmlsoap.org/soap/envelope/"

ORDERS = (
    b'<?xml version="1.0" encoding="UTF-8"?>'
    b'<orders xmlns="urn:shop"><meta><total>2</total></meta>'
    b'<order id="1"><sku>A&amp;B</sku><customer><name>Asha</name></customer><tag>x</tag><tag>y</tag></order>'
    b'<order id="2"><sku>C</sku><customer><name>Ben</name></customer></order>'
    b"</orders>"
)


def _soap(inner: str) -> bytes:
    return f'<soap:Envelope xmlns:soap="{SOAP_ENV}"><soap:Body>{inner}</soap:Body></soap:Envelope>'.encode()


CUSTOMERS = _soap(
    '<m:GetCustomersResponse xmlns:m="urn:example:customers"><m:customers>'
    "<m:customer><m:id>1</m:id><m:name>Asha</m:name></m:customer>"
    "<m:customer><m:id>2</m:id><m:name>Ben</m:name></m:customer>"
    "</m:customers></m:GetCustomersResponse>"
)
FAULT = _soap("<soap:Fault><faultcode>soap:Client</faultcode><faultstring>Unknown region</faultstring></soap:Fault>")
FAULT_12 = _soap(
    "<soap:Fault><soap:Code><soap:Value>soap:Sender</soap:Value></soap:Code>"
    '<soap:Reason><soap:Text xml:lang="en">Bad value</soap:Text></soap:Reason></soap:Fault>'
)
LAUGHS = (
    b'<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol">'
    b'<!ENTITY lol1 "&lol;&lol;&lol;&lol;"><!ENTITY lol2 "&lol1;&lol1;&lol1;&lol1;">]>'
    b"<lolz>&lol2;</lolz>"
)

WSDL = """<?xml version="1.0"?>
<wsdl:definitions name="Customers" targetNamespace="urn:example:customers"
  xmlns:wsdl="http://schemas.xmlsoap.org/wsdl/" xmlns:soap="http://schemas.xmlsoap.org/wsdl/soap/"
  xmlns:tns="urn:example:customers" xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <wsdl:types>
    <xs:schema targetNamespace="urn:example:customers" elementFormDefault="qualified">
      <xs:element name="GetCustomers">
        <xs:complexType><xs:sequence>
          <xs:element name="region" type="xs:string"/>
          <xs:element name="status" type="xs:string" minOccurs="0"/>
          <xs:element name="paging" type="tns:Paging" minOccurs="0"/>
        </xs:sequence><xs:attribute name="locale" type="xs:string"/></xs:complexType>
      </xs:element>
      <xs:complexType name="Paging"><xs:sequence>
        <xs:element name="limit" type="xs:int"/>
        <xs:element name="next" type="tns:Paging" minOccurs="0"/>
      </xs:sequence></xs:complexType>
    </xs:schema>
  </wsdl:types>
  <wsdl:message name="In"><wsdl:part name="parameters" element="tns:GetCustomers"/></wsdl:message>
  <wsdl:portType name="PT"><wsdl:operation name="GetCustomers">
    <wsdl:documentation>List customers</wsdl:documentation>
    <wsdl:input message="tns:In"/></wsdl:operation></wsdl:portType>
  <wsdl:binding name="B" type="tns:PT"><soap:binding style="document" transport="http://schemas.xmlsoap.org/soap/http"/>
    <wsdl:operation name="GetCustomers"><soap:operation soapAction="urn:example:customers/GetCustomers"/>
      <wsdl:input><soap:body use="literal"/></wsdl:input></wsdl:operation></wsdl:binding>
  <wsdl:service name="CustomerService"><wsdl:port name="P" binding="tns:B"><soap:address location="ENDPOINT"/></wsdl:port></wsdl:service>
</wsdl:definitions>"""


class _Handler(BaseHTTPRequestHandler):
    seen: list = []
    other_port = 0

    def log_message(self, *args):
        pass

    def _send(self, status, content_type, body, **headers):
        self.send_response(status)
        if content_type:
            self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _route(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode() if length else ""
        type(self).seen.append({"method": self.command, "path": self.path, "headers": dict(self.headers), "body": body})
        path = self.path.split("?")[0]
        routes = {
            "/orders": (200, "application/xml; charset=utf-8", ORDERS),
            "/orders-as-text": (200, "text/plain", ORDERS),
            "/json": (200, "application/json", json.dumps({"items": [{"id": 1}]}).encode()),
            "/not-json": (200, "application/json", b"{oops"),
            "/html": (200, "text/html", b"<html><title>Sign in</title><body>login</body></html>"),
            "/png": (200, "image/png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 30),
            "/octet": (200, "application/octet-stream", b"\x00\x01\x02\x03binary"),
            "/text": (200, "text/plain", b"all good"),
            "/csv": (200, "text/csv", b"id,name\n1,Asha\n2,Ben\n"),
            "/empty": (204, "", b""),
            "/laughs": (200, "application/xml", LAUGHS),
            "/broken": (200, "application/xml", b"<a><b></a>"),
            "/fault": (200, "text/xml", FAULT),
            "/fault500": (500, "text/xml", FAULT),
            "/fault12": (500, "application/soap+xml", FAULT_12),
            "/wrong-reply": (200, "application/json", b'{"ok": true}'),
            "/secret": (200, "application/xml", b"<a><row><password>hunter2</password><name>Asha</name><token kind='x'>abc</token></row><row><name>Ben</name></row></a>"),
            "/single": (200, "application/xml", b"<reply><customer><id>7</id><name>Asha</name></customer></reply>"),
        }
        if path == "/redirect-out":
            # Multicast stays blocked even with internal addresses allowed, as they are here.
            self._send(302, "text/plain", b"", Location="http://224.0.0.1/x")
        elif path == "/redirect-here":
            self._send(302, "text/plain", b"", Location=f"http://127.0.0.1:{self.server.server_port}/orders")
        elif path == "/redirect-other-site":
            self._send(302, "text/plain", b"", Location=f"http://127.0.0.1:{type(self).other_port}/orders")
        elif path == "/token":
            form = parse_qs(body)
            ok = form.get("grant_type") == ["client_credentials"] and form.get("client_secret") == ["s3"]
            self._send(200 if ok else 401, "application/json", json.dumps({"access_token": "tok-1"} if ok else {}).encode())
        elif path == "/paged":
            page = int(parse_qs(self.path.partition("?")[2]).get("page", ["1"])[0])
            items = "".join(f"<item><n>{n}</n></item>" for n in ((1, 2), (3, 4))[page - 1]) if page <= 2 else ""
            self._send(200, "application/xml", f"<list>{items}</list>".encode())
        elif path == "/linked":
            page = parse_qs(self.path.partition("?")[2]).get("p", ["1"])[0]
            headers = {"Link": '</linked?p=2>; rel="next"'} if page == "1" else {}
            self._send(200, "application/json", json.dumps({"items": [{"id": int(page)}]}).encode(), **headers)
        elif path == "/linked-away":
            self._send(200, "application/json", b'{"items": [{"id": 1}]}',
                       Link=f'<http://127.0.0.1:{type(self).other_port}/linked?p=2>; rel="next"')
        elif path == "/soap":
            if "BAD" in body:
                self._send(500, "text/xml", FAULT)
            else:
                self._send(200, "text/xml; charset=utf-8", CUSTOMERS)
        elif path == "/service.wsdl":
            text = WSDL.replace("ENDPOINT", f"http://127.0.0.1:{self.server.server_port}/soap")
            self._send(200, "text/xml", text.encode())
        elif path in routes:
            self._send(*routes[path])
        else:
            self._send(404, "text/plain", b"missing")

    do_GET = do_POST = do_PUT = _route


@pytest.fixture
def server(monkeypatch):
    monkeypatch.setenv("REVOLUZEN_FLOW_API_SOURCE_ALLOW_PRIVATE", "1")
    monkeypatch.setenv("REVOLUZEN_FLOW_HTTP_ALLOW_PRIVATE", "1")
    _Handler.seen = []
    # A second server is a different site: same machine, different port.
    servers = [ThreadingHTTPServer(("127.0.0.1", 0), _Handler) for _ in range(2)]
    _Handler.other_port = servers[1].server_port
    threads = [threading.Thread(target=http.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True) for http in servers]
    for thread in threads:
        thread.start()
    yield f"http://127.0.0.1:{servers[0].server_port}"
    for http, thread in zip(servers, threads):
        http.shutdown()
        http.server_close()
        thread.join(timeout=5)


@pytest.fixture
def ctx(tmp_path):
    conn = duckdb.connect()
    yield ExecutionContext(conn=conn, data_dir=str(tmp_path))
    conn.close()


def _rows(relation):
    return [dict(zip(relation.columns, row)) for row in relation.fetchall()]


# ── The reader refuses attacks ──────────────────────────────────────────────


@pytest.mark.parametrize("document", [
    LAUGHS,
    b'<!DOCTYPE a [<!ENTITY x SYSTEM "file:///etc/passwd">]><a>&x;</a>',
    b'<!DOCTYPE a [<!ENTITY % x SYSTEM "http://169.254.169.254/x.dtd"> %x;]><a/>',
    b'<!DOCTYPE a [<!ENTITY x SYSTEM "http://x/y" NDATA n>]><a/>',
    b'<!DOCTYPE a SYSTEM "http://example.com/x.dtd"><a>&undeclared;</a>',
])
def test_entities_are_refused(document):
    with pytest.raises(UnsafeXmlError):
        parse_xml(document)
    with pytest.raises(UnsafeXmlError):
        parse_xml(document.decode(), keep_prefixes=True)


def test_entities_are_refused_in_a_file(tmp_path):
    path = tmp_path / "bad.xml"
    path.write_bytes(LAUGHS)
    with pytest.raises(UnsafeXmlError):
        parse_xml_file(str(path))


def test_nothing_is_fetched_for_a_doctype():
    # A DOCTYPE with no entities is harmless, and real exports have them.
    assert parse_xml('<!DOCTYPE a SYSTEM "http://169.254.169.254/x.dtd"><a>ok</a>').text == "ok"


def test_deep_nesting_is_refused():
    with pytest.raises(UnsafeXmlError):
        parse_xml("<a>" * (MAX_DEPTH + 1) + "</a>" * (MAX_DEPTH + 1))


def test_the_deepest_document_allowed_can_still_be_shown():
    text = "<a>" * MAX_DEPTH + "x" + "</a>" * MAX_DEPTH
    assert pretty_xml(text).count("<a>") == MAX_DEPTH
    assert xml_to_data(parse_xml(text))


def test_not_well_formed_says_where():
    with pytest.raises(XmlError, match="line 1"):
        parse_xml("<a><b></a>")


def test_encodings_follow_the_document():
    assert parse_xml('<?xml version="1.0" encoding="windows-1252"?><a>caf\xe9</a>'.encode("latin-1")).text == "caf\xe9"
    assert parse_xml('<?xml version="1.0" encoding="utf-16"?><a>€</a>'.encode("utf-16")).text == "€"
    # Text already decoded is not re-read in the encoding its declaration names.
    assert parse_xml('<?xml version="1.0" encoding="windows-1252"?><a>€</a>').text == "€"


# ── Row paths and rows ──────────────────────────────────────────────────────


@pytest.mark.parametrize("path, count", [
    ("//order", 2), ("order", 2), ("/orders/order", 2), ("//x:order", 2),
    ("//order[@id='2']", 1), ("/orders/order[1]", 1), ("//order[sku='C']", 1),
    ("//customer/name", 2), ("/orders//name", 2), ("/orders/*", 3), ("//orders", 1),
    ("/wrong/order", 0), ("//missing", 0),
])
def test_row_paths_ignore_namespaces(path, count):
    assert len(find_elements(parse_xml(ORDERS), path)) == count


@pytest.mark.parametrize("path", ["//order/@id", "//order/text()", "//order[", "@id"])
def test_a_row_path_that_cannot_select_elements_is_explained(path):
    with pytest.raises(XmlError, match="row path"):
        find_elements(parse_xml(ORDERS), path)


def test_rows_have_attributes_nested_columns_and_repeats():
    rows = rows_from_xml(parse_xml(ORDERS), "//order")
    assert rows[0] == {"id": "1", "sku": "A&B", "customer_name": "Asha", "tag": '["x", "y"]'}
    assert rows[1] == {"id": "2", "sku": "C", "customer_name": "Ben"}


def test_a_leaf_with_an_attribute_keeps_both():
    assert rows_from_xml(parse_xml('<r><i><p c="USD">10</p></i><i><p c="EUR">9</p></i></r>')) == [
        {"p": "10", "p_c": "USD"}, {"p": "9", "p_c": "EUR"},
    ]


def test_bare_values_become_a_value_column():
    assert rows_from_xml(parse_xml("<ids><id>1</id><id>2</id></ids>")) == [{"value": "1"}, {"value": "2"}]


def test_the_repeating_record_is_found_not_the_repeating_detail():
    document = parse_xml("<o><warn>a</warn><warn>b</warn><i><l/><l/></i><i><l/><l/></i></o>")
    assert detect_row_path(document) == "/o/i"


def test_a_single_record_is_found_inside_its_wrappers():
    assert rows_from_xml(parse_xml("<reply><customer><id>7</id><name>A</name></customer></reply>")) == [{"id": "7", "name": "A"}]


def test_candidates_look_inside_the_soap_body():
    assert row_path_candidates(parse_xml(CUSTOMERS))[0] == {"path": "/Envelope/Body/GetCustomersResponse/customers/customer", "count": 2}
    assert rows_from_xml(parse_xml(CUSTOMERS)) == [{"id": "1", "name": "Asha"}, {"id": "2", "name": "Ben"}]


def test_structure_lists_each_path_once():
    entries = {e["path"]: e for e in xml_structure(parse_xml(ORDERS))}
    assert entries["/orders/order"] == {"path": "/orders/order", "type": "element", "count": 2}
    assert entries["/orders/order/@id"]["type"] == "attribute"
    assert entries["/orders/meta/total"]["type"] == "value"


def test_pretty_keeps_prefixes_and_masks_sensitive_values():
    text = pretty_xml(_soap("<password>hunter2</password><x apikey='k'>1</x>"), explorer.SENSITIVE)
    assert "soap:Envelope" in text and "\n" in text
    assert "hunter2" not in text and "'k'" not in text and '"k"' not in text


def test_soap_faults_in_both_versions():
    assert soap_fault(parse_xml(FAULT)) == {"code": "soap:Client", "message": "Unknown region", "detail": ""}
    assert soap_fault(parse_xml(FAULT_12))["message"] == "Bad value"
    assert soap_fault(parse_xml(CUSTOMERS)) is None
    assert soap_fault(parse_xml(ORDERS)) is None


# ── Filling in an XML body ──────────────────────────────────────────────────


def test_values_are_escaped():
    out = render_xml_template('<a x="{v}"><b>{v}</b></a>', {"v": '</b><admin>1</admin>"&'})
    root = parse_xml(out)
    assert root.get("x") == '</b><admin>1</admin>"&'
    assert root[0].text == '</b><admin>1</admin>"&'
    assert root.find("admin") is None and root[0].find("admin") is None


def test_a_blank_optional_is_left_out_and_a_blank_required_is_not():
    out = render_xml_template("<a><!--Optional:--><b>{b}</b><c>{c}</c></a>", {"b": "", "c": ""})
    root = parse_xml(out)
    assert root.find("b") is None and root.find("c") is not None


def test_an_optional_block_goes_when_everything_in_it_is_blank():
    template = "<a><!--Optional:--><p><l>{l}</l><!--Optional:--><o>{o}</o></p></a>"
    assert parse_xml(render_xml_template(template, {"l": "", "o": ""})).find("p") is None
    assert parse_xml(render_xml_template(template, {"l": "5", "o": ""})).find("p/l").text == "5"


def test_a_blank_placeholder_attribute_is_left_out():
    assert parse_xml(render_xml_template('<a k="{k}" n="1"/>', {"k": ""})).attrib == {"n": "1"}


def test_text_in_braces_with_no_value_stays():
    assert parse_xml(render_xml_template("<a>{other}</a>", {"x": "1"})).text == "{other}"


def test_literal_optional_text_is_kept():
    assert parse_xml(render_xml_template("<a><!--Optional:--><b>fixed</b></a>", {"x": ""})).find("b").text == "fixed"


def test_a_fragment_is_still_filled_in_and_escaped():
    assert render_xml_template("<a>{v}", {"v": "<&"}) == "<a>&lt;&amp;"


def test_placeholders_listed_once_in_order():
    assert xml_placeholders("<a>{b}{a}{b}{1x}</a>") == ["b", "a"]


# ── XML files use the same reader ───────────────────────────────────────────


def test_a_file_reads_by_row_xpath(tmp_path, ctx):
    path = tmp_path / "shop.xml"
    path.write_bytes(ORDERS)
    rows = _rows(XmlSourceNode({"file_path": str(path), "row_xpath": "//order"}).execute(ctx))
    assert [row["sku"] for row in rows] == ["A&B", "C"]


def test_the_untouched_file_default_still_reads_top_level_children(tmp_path, ctx):
    path = tmp_path / "shop.xml"
    path.write_bytes(b"<rows><item><a>1</a></item><item><a>2</a></item></rows>")
    rows = _rows(XmlSourceNode({"file_path": str(path), "row_xpath": "//record"}).execute(ctx))
    assert [row["a"] for row in rows] == ["1", "2"]


def test_a_mistyped_row_path_on_a_file_is_an_error(tmp_path, ctx):
    path = tmp_path / "shop.xml"
    path.write_bytes(ORDERS)
    with pytest.raises(ValueError, match="no elements"):
        XmlSourceNode({"file_path": str(path), "row_xpath": "//nothing"}).execute(ctx)


def test_row_tag_still_works_on_a_file(tmp_path, ctx):
    path = tmp_path / "shop.xml"
    path.write_bytes(ORDERS)
    assert len(_rows(XmlSourceNode({"file_path": str(path), "row_tag": "order"}).execute(ctx))) == 2


def test_a_file_cannot_attack_the_server(tmp_path, ctx):
    path = tmp_path / "bad.xml"
    path.write_bytes(LAUGHS)
    with pytest.raises(UnsafeXmlError):
        XmlSourceNode({"file_path": str(path)}).execute(ctx)


# ── What a reply is ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("content_type, body, kind", [
    ("application/json", b"{}", "json"),
    ("application/vnd.api+json", b"{}", "json"),
    ("text/plain", b'[{"a":1}]', "json"),
    ("application/xml", b"<a/>", "xml"),
    ("text/xml", b"<a/>", "xml"),
    ("application/soap+xml", b"<a/>", "xml"),
    ("image/svg+xml", b"<svg/>", "xml"),
    ("text/plain", b"<a>1</a>", "xml"),
    ("", b'<?xml version="1.0"?><a/>', "xml"),
    ("text/html", b"<p>x</p>", "html"),
    ("text/plain", b"<!DOCTYPE html><html></html>", "html"),
    ("text/csv", b"a,b", "csv"),
    ("text/plain", b"hello", "text"),
    ("application/octet-stream", b"just text", "text"),
    ("application/octet-stream", b"\x00\x01\x02", "binary"),
    ("application/pdf", b"anything", "binary"),
    ("text/plain", b"\x89PNG\r\n\x1a\n", "binary"),
    ("", b"", "empty"),
])
def test_reply_kinds(content_type, body, kind):
    assert reply_kind(content_type, body) == kind


def test_utf16_xml_is_not_taken_for_binary():
    assert reply_kind("", '<?xml version="1.0" encoding="utf-16"?><a/>'.encode("utf-16")) == "xml"


def test_text_uses_the_declared_charset():
    assert decode_text("caf\xe9".encode("latin-1"), "text/plain; charset=iso-8859-1") == "caf\xe9"
    assert decode_text(b"abc", "text/plain; charset=not-a-charset") == "abc"


def test_rows_from_reply_formats():
    assert rows_from_reply(b'{"items":[{"a":1}]}', "application/json") == [{"a": 1}]
    assert rows_from_reply(b"<a><b>1</b><b>2</b></a>", "text/xml", reply_format="auto", row_path="//b") == [{"value": "1"}, {"value": "2"}]
    assert rows_from_reply(b"<a/>", "text/xml", reply_format="text") == [{"response": "<a/>"}]
    assert rows_from_reply(b"", "") == []
    with pytest.raises(ValueError, match="not valid JSON"):
        rows_from_reply(b"<a/>", "text/xml", reply_format="json")
    with pytest.raises(XmlError):
        rows_from_reply(b"nope", "text/plain", reply_format="xml")


def test_something_that_only_starts_with_a_bracket_is_text():
    assert rows_from_reply(b"<b>hello</b> world", "text/plain") == [{"response": "<b>hello</b> world"}]


# ── HTTP Request step ───────────────────────────────────────────────────────


def _http(ctx, **params):
    return _rows(HttpRequestNode(params).execute(ctx))


def test_http_step_reads_xml_into_rows(server, ctx):
    assert _http(ctx, url=f"{server}/orders") == [
        {"id": "1", "sku": "A&B", "customer_name": "Asha", "tag": '["x", "y"]'},
        {"id": "2", "sku": "C", "customer_name": "Ben", "tag": None},
    ]
    assert _Handler.seen[0]["headers"]["Accept"].startswith("application/json, application/xml")


def test_http_step_row_path_picks_the_element(server, ctx):
    assert _http(ctx, url=f"{server}/orders", row_path="//customer") == [{"name": "Asha"}, {"name": "Ben"}]


def test_http_step_reads_xml_labelled_as_text(server, ctx):
    assert len(_http(ctx, url=f"{server}/orders-as-text", row_path="//order")) == 2


def test_http_step_json_is_unchanged(server, ctx):
    assert _http(ctx, url=f"{server}/json") == [{"id": 1}]


def test_http_step_says_when_json_is_broken(server, ctx):
    with pytest.raises(ValueError, match="not valid JSON"):
        _http(ctx, url=f"{server}/not-json")


def test_http_step_empty_reply_is_no_rows(server, ctx):
    assert HttpRequestNode({"url": f"{server}/empty"}).execute(ctx).fetchall() == []


def test_http_step_plain_text_is_one_row(server, ctx):
    assert _http(ctx, url=f"{server}/text") == [{"response": "all good"}]


def test_http_step_web_page_is_an_error_unless_asked_for(server, ctx):
    with pytest.raises(ValueError, match="web page"):
        _http(ctx, url=f"{server}/html")
    assert "Sign in" in _http(ctx, url=f"{server}/html", response_format="text")[0]["response"]


def test_http_step_a_file_is_an_error_that_says_what_it_is(server, ctx):
    with pytest.raises(ValueError, match="PNG image"):
        _http(ctx, url=f"{server}/png")


def test_http_step_refuses_an_entity_attack(server, ctx):
    with pytest.raises(UnsafeXmlError):
        _http(ctx, url=f"{server}/laughs")


def test_http_step_reports_a_fault_sent_with_200(server, ctx):
    with pytest.raises(SoapFaultError, match="Unknown region"):
        _http(ctx, url=f"{server}/fault")


def test_http_step_reports_a_fault_sent_with_500(server, ctx):
    with pytest.raises(ValueError, match="500.*Unknown region"):
        _http(ctx, url=f"{server}/fault500")


@pytest.mark.parametrize("params, content_type", [
    ({"body_type": "xml"}, "application/xml"),
    ({"body_type": "form"}, "application/x-www-form-urlencoded"),
    ({"body_type": "text"}, "text/plain; charset=utf-8"),
    ({"body_type": "json"}, "application/json"),
    ({}, "application/json"),
    # Saved before Body Type existed.
    ({"content_type": "application/xml"}, "application/xml"),
    ({"content_type": "text/plain"}, "text/plain; charset=utf-8"),
])
def test_http_step_sets_the_content_type(server, ctx, params, content_type):
    body = "a=1" if params.get("body_type") == "form" else "<a/>"
    _http(ctx, url=f"{server}/json", method="POST", body=body, **params)
    assert _Handler.seen[0]["headers"]["Content-Type"] == content_type
    assert _Handler.seen[0]["body"] == body


def test_http_step_a_typed_content_type_wins(server, ctx):
    _http(ctx, url=f"{server}/json", method="POST", body="<a/>", body_type="xml",
          headers=[{"key": "content-type", "value": "text/xml"}])
    assert _Handler.seen[0]["headers"]["Content-Type"] == "text/xml"


def test_http_step_form_body_from_json(server, ctx):
    _http(ctx, url=f"{server}/json", method="POST", body='{"q": "a b&c", "n": 1}', body_type="form")
    assert parse_qs(_Handler.seen[0]["body"]) == {"q": ["a b&c"], "n": ["1"]}


def test_http_step_a_form_body_it_cannot_read_is_an_error(server, ctx):
    with pytest.raises(ValueError, match="form body"):
        _http(ctx, url=f"{server}/json", method="POST", body="nonsense", body_type="form")


def _per_row(ctx, values, **params):
    ctx.set_result("src", ctx.conn.sql("SELECT * FROM (VALUES " + ", ".join(f"('{v}')" for v in values) + ") t(name)"))
    return _rows(HttpRequestNode({"per_row": True, "_input_step_ids": ["src"], **params}).execute(ctx))


def test_http_step_per_row_xml_values_are_escaped(server, ctx):
    rows = _per_row(ctx, ["a&b"], url=f"{server}/orders", method="POST", body="<q>{name}</q>", body_type="xml", row_path="//order")
    assert _Handler.seen[0]["body"] == "<q>a&amp;b</q>"
    assert rows[0]["name"] == "a&b" and rows[0]["sku"] == "A&B"


def test_http_step_per_row_form_values_are_encoded(server, ctx):
    _per_row(ctx, ["a&b"], url=f"{server}/json", method="POST", body="who={name}&k=1", body_type="form")
    assert parse_qs(_Handler.seen[0]["body"]) == {"who": ["a&b"], "k": ["1"]}


def test_http_step_per_row_records_a_fault_against_the_row(server, ctx):
    rows = _per_row(ctx, ["x"], url=f"{server}/fault", method="POST", body="<q/>", body_type="xml")
    assert "Unknown region" in rows[0]["_http_error"]


@pytest.mark.parametrize("params, header, value", [
    ({"auth_type": "bearer", "auth_token": "t0k"}, "Authorization", "Bearer t0k"),
    ({"auth_type": "basic", "auth_user": "u", "auth_pass": "p"}, "Authorization", "Basic dTpw"),
    ({"auth_type": "api_key", "api_key_header": "X-Key", "api_key_value": "k1"}, "X-Key", "k1"),
])
def test_http_step_sends_its_sign_in(server, ctx, params, header, value):
    _http(ctx, url=f"{server}/json", **params)
    assert _Handler.seen[0]["headers"][header] == value


def test_http_step_a_typed_header_beats_the_auth_fields(server, ctx):
    _http(ctx, url=f"{server}/json", auth_type="bearer", auth_token="t", headers={"Authorization": "Custom x"})
    assert _Handler.seen[0]["headers"]["Authorization"] == "Custom x"


def test_http_step_no_auth_sends_no_header(server, ctx):
    _http(ctx, url=f"{server}/json", auth_type="none", auth_token="left-over")
    assert "Authorization" not in _Handler.seen[0]["headers"]


def test_http_step_redirect_to_a_blocked_address_is_refused(server, ctx):
    with pytest.raises(ValueError, match="blocked"):
        _http(ctx, url=f"{server}/redirect-out")
    assert len(_Handler.seen) == 1


def test_http_step_follows_a_redirect_on_the_same_site_with_its_sign_in(server, ctx):
    _http(ctx, url=f"{server}/redirect-here", auth_type="bearer", auth_token="t", row_path="//order")
    assert _Handler.seen[1]["headers"]["Authorization"] == "Bearer t"


def test_http_step_does_not_take_its_sign_in_to_another_site(server, ctx):
    _http(ctx, url=f"{server}/redirect-other-site", auth_type="bearer", auth_token="t",
          headers={"X-Api-Key": "k", "X-Trace": "keep"}, row_path="//order")
    assert "Authorization" not in _Handler.seen[1]["headers"]
    assert "X-Api-Key" not in _Handler.seen[1]["headers"]
    assert _Handler.seen[1]["headers"]["X-Trace"] == "keep"


def test_http_step_schema_lists_what_it_reads():
    names = {field["name"] for field in HttpRequestNode.param_schema()}
    assert {"body_type", "response_format", "row_path", "auth_type", "auth_token", "auth_user", "auth_pass", "api_key_header", "api_key_value"} <= names


# ── Connector framework ─────────────────────────────────────────────────────


def _manifest(server, stream, params=()):
    return rf.RestConnectorManifest.from_dict({
        "id": "shop", "name": "Shop", "base_url": server, "params": list(params),
        "streams": [{"name": "s", **stream}],
    })


def _run_stream(server, stream, values=None, params=()):
    manifest = _manifest(server, stream, params)
    return rf._execute_stream(manifest, manifest.streams[0], values or {})


def test_stream_reads_xml_by_row_path(server):
    rows = _run_stream(server, {"path": "/orders", "response_format": "xml", "data_path": "//customer"})
    assert rows == [{"name": "Asha"}, {"name": "Ben"}]
    assert _Handler.seen[0]["headers"]["Accept"].startswith("application/xml")


def test_stream_with_no_path_finds_the_rows(server):
    assert len(_run_stream(server, {"path": "/orders", "response_format": "xml"})) == 2


def test_stream_with_no_format_is_untouched(server):
    assert _run_stream(server, {"path": "/orders"}) == [{"raw": ORDERS.decode()}]


def test_stream_reports_a_fault(server):
    for path in ("/fault", "/fault500"):
        with pytest.raises(RuntimeError, match="Unknown region"):
            _run_stream(server, {"path": path, "response_format": "xml"})


def test_stream_keeps_secrets_out_of_a_fault(server, monkeypatch):
    params = [{"name": "key", "secret": True}]
    monkeypatch.setattr(rf, "_http_request", lambda *a, **k: (rf._RawReply("", _soap(
        "<soap:Fault><faultstring>bad key s3cr3t</faultstring></soap:Fault>")), {}))
    with pytest.raises(RuntimeError) as caught:
        _run_stream(server, {"path": "/x", "response_format": "xml"}, {"key": "s3cr3t"}, params)
    assert "s3cr3t" not in str(caught.value)
    assert "[redacted]" in str(caught.value)


@pytest.mark.parametrize("path, expected", [("/broken", "could not be read"), ("/laughs", "could not be read"), ("/wrong-reply", "could not be read")])
def test_stream_says_when_the_reply_is_not_usable_xml(server, path, expected):
    with pytest.raises(RuntimeError, match=expected):
        _run_stream(server, {"path": path, "response_format": "xml"})


def test_stream_a_recorded_reply_can_be_replayed(server, monkeypatch):
    monkeypatch.setattr(rf, "_http_request", lambda *a, **k: ({"raw": ORDERS.decode()}, {}))
    assert len(_run_stream(server, {"path": "/x", "response_format": "xml", "data_path": "//order"})) == 2


FIELD_PARAMS = [{"name": "v", "label": "Value", "required": True}, {"name": "opt"}]
XML_BODY = "<a><v>{v}</v><!--Optional:--><o>{opt}</o></a>"


def test_stream_fills_in_an_xml_body(server):
    _run_stream(server, {"path": "/orders", "method": "POST", "response_format": "xml", "body_format": "xml", "body_text": XML_BODY},
                {"v": "a&b"}, FIELD_PARAMS)
    assert _Handler.seen[0]["body"] == "<a><v>a&amp;b</v></a>"
    assert _Handler.seen[0]["headers"]["Content-Type"] == "application/xml"


def test_stream_a_required_value_cannot_be_blank(server):
    with pytest.raises(ValueError, match="needs a value for: Value"):
        _run_stream(server, {"path": "/orders", "method": "POST", "response_format": "xml", "body_format": "xml", "body_text": XML_BODY}, {}, FIELD_PARAMS)
    assert _Handler.seen == []


def test_stream_uses_the_connector_default(server):
    params = [{"name": "v", "required": True, "default": "EMEA"}]
    _run_stream(server, {"path": "/orders", "method": "POST", "response_format": "xml", "body_format": "xml", "body_text": "<a>{v}</a>"}, {}, params)
    assert _Handler.seen[0]["body"] == "<a>EMEA</a>"


def test_stream_sends_form_fields(server):
    _run_stream(server, {"path": "/json", "method": "POST", "body_format": "form", "body": {"q": "{v}"}}, {"v": "x"})
    assert _Handler.seen[0]["body"] == "q=x"
    assert _Handler.seen[0]["headers"]["Content-Type"] == "application/x-www-form-urlencoded"


def test_stream_form_text_values_are_encoded(server):
    _run_stream(server, {"path": "/json", "method": "POST", "body_format": "form", "body_text": "q={v}"}, {"v": "a&b"})
    assert _Handler.seen[0]["body"] == "q=a%26b"


def test_stream_plain_body_text_is_unchanged(server):
    _run_stream(server, {"path": "/json", "method": "POST", "body_text": "<a>{v}</a>"}, {"v": "x&y"})
    assert _Handler.seen[0]["body"] == "<a>x&y</a>"
    assert _Handler.seen[0]["headers"]["Content-Type"] == "text/plain"


def test_stream_xml_pagination_reads_dotted_paths(monkeypatch):
    pages = [
        (rf._RawReply("", b"<feed><next>2</next><item><a>1</a></item><item><a>2</a></item></feed>"), {}),
        (rf._RawReply("", b"<feed><item><a>3</a></item><item><a>4</a></item></feed>"), {}),
    ]
    monkeypatch.setattr(rf, "_http_request", lambda *a, **k: pages.pop(0))
    manifest = _manifest("https://api.example.test", {
        "path": "/f", "response_format": "xml", "data_path": "//item",
        "pagination": {"type": "cursor", "cursor_field": "feed.next", "cursor_param": "page"},
    })
    rows = rf._execute_stream(manifest, manifest.streams[0], {})
    assert [row["a"] for row in rows] == ["1", "2", "3", "4"]


# ── API Explorer ────────────────────────────────────────────────────────────


def _inspect(**params):
    return explorer.inspect_request(explorer.InspectRequest(**params))


def test_explorer_xml_reply(server):
    result = _inspect(url=f"{server}/orders")
    assert result["kind"] == "xml" and result["content_type"] == "application/xml"
    assert '<sku>A&amp;B</sku>' in result["text"] and "\n" in result["text"]
    assert result["data"]["orders"]["order"][0]["@id"] == "1"
    first = result["xml"]["records"][0]
    assert first["path"] == "/orders/order" and first["count"] == 2
    assert first["rows"][1]["customer_name"] == "Ben"
    assert {"path": "/orders/order/@id", "type": "attribute", "count": 2} in result["xml"]["structure"]
    assert result["xml"]["fault"] is None


def test_explorer_masks_sensitive_xml_everywhere(server):
    result = _inspect(url=f"{server}/secret")
    assert "hunter2" not in json.dumps(result) and "abc" not in result["text"]
    assert result["xml"]["records"][0]["rows"][0]["password"] == "[redacted]"
    assert result["data"]["a"]["row"][0]["password"] == "[redacted]"


def test_explorer_redacts_a_token_it_sent_when_a_server_echoes_it(server, monkeypatch):
    monkeypatch.setattr(explorer, "_exchange", lambda *a, **k: (_Response("application/xml"), b"<a><b>CANARY</b></a>", False))
    result = _inspect(url=f"{server}/x", auth_type="bearer", token="CANARY")
    assert "CANARY" not in json.dumps(result)


class _Response:
    status = 200

    def __init__(self, content_type):
        self.content_type = content_type

    def getheader(self, name):
        return self.content_type

    def getheaders(self):
        return [("Content-Type", self.content_type)]


def test_explorer_shows_a_fault(server):
    result = _inspect(url=f"{server}/fault500")
    assert result["status"] == 500 and result["xml"]["fault"]["message"] == "Unknown region"


@pytest.mark.parametrize("path, kind", [("/html", "html"), ("/text", "text"), ("/csv", "csv"), ("/png", "binary"), ("/octet", "binary"), ("/empty", "empty"), ("/json", "json")])
def test_explorer_names_each_kind_of_reply(server, path, kind):
    assert _inspect(url=f"{server}{path}")["kind"] == kind


def test_explorer_csv_is_rows_and_text(server):
    result = _inspect(url=f"{server}/csv")
    assert result["data"] == [{"id": "1", "name": "Asha"}, {"id": "2", "name": "Ben"}]
    assert result["text"].startswith("id,name") and result["row_count"] == 2


def test_explorer_html_title_and_source(server):
    result = _inspect(url=f"{server}/html")
    assert result["title"] == "Sign in" and "<body>" in result["text"]


def test_explorer_binary_says_what_it_is_and_shows_no_garbage(server):
    result = _inspect(url=f"{server}/png")
    assert result["binary"] == "PNG image" and result["text"] == "" and result["data"] is None


def test_explorer_an_entity_attack_is_shown_as_text_and_not_read(server):
    result = _inspect(url=f"{server}/laughs")
    assert result["kind"] == "text" and "entities" in result["notice"] and result["data"] is None


def test_explorer_broken_xml_is_shown_as_text(server):
    result = _inspect(url=f"{server}/broken")
    assert result["kind"] == "text" and "not well-formed" in result["notice"] and result["text"] == "<a><b></a>"


def test_explorer_xml_that_is_really_a_web_page(server, monkeypatch):
    monkeypatch.setattr(explorer, "_exchange", lambda *a, **k: (_Response("application/xml"), b"<html><title>T</title></html>", False))
    assert _inspect(url=f"{server}/x")["kind"] == "html"


@pytest.mark.parametrize("body_type, content_type", [
    ("json", "application/json"), ("xml", "application/xml"),
    ("form", "application/x-www-form-urlencoded"), ("text", "text/plain; charset=utf-8"),
])
def test_explorer_body_type_sets_the_content_type(server, body_type, content_type):
    _inspect(url=f"{server}/json", method="POST", confirm_write=True, body="x", body_type=body_type)
    assert _Handler.seen[0]["headers"]["Content-Type"] == content_type


def test_explorer_a_typed_content_type_wins(server):
    _inspect(url=f"{server}/json", method="POST", confirm_write=True, body="x", body_type="xml", headers={"Content-Type": "text/xml"})
    assert _Handler.seen[0]["headers"]["Content-Type"] == "text/xml"


def test_explorer_fills_in_values_escaped(server):
    _inspect(url=f"{server}/json", method="POST", confirm_write=True, body_type="xml",
             body="<a><!--Optional:--><o>{o}</o><v>{v}</v></a>", body_values={"o": "", "v": "x<y"})
    assert _Handler.seen[0]["body"] == "<a><v>x&lt;y</v></a>"


def test_explorer_redacts_a_sensitive_value_if_it_comes_back(server, monkeypatch):
    monkeypatch.setattr(explorer, "_exchange", lambda *a, **k: (_Response("text/plain"), b"got pw-CANARY", False))
    result = _inspect(url=f"{server}/x", method="POST", confirm_write=True, body_type="xml", body="<a>{password}</a>", body_values={"password": "pw-CANARY"})
    assert "pw-CANARY" not in result["text"]


# ── WSDL ────────────────────────────────────────────────────────────────────


def _operation(text=WSDL.replace("ENDPOINT", "https://svc.example.test/soap"), **kwargs):
    return read_wsdl(text, **kwargs)["operations"][0]["soap"]


def test_wsdl_lists_operations():
    result = read_wsdl(WSDL.replace("ENDPOINT", "https://svc.example.test/soap"))
    operation = result["operations"][0]
    assert result["service"] == "CustomerService"
    assert operation["path"] == "GetCustomers" and operation["summary"] == "List customers" and operation["method"] == "POST"
    assert operation["soap"]["endpoint"] == "https://svc.example.test/soap"
    assert operation["soap"]["headers"] == {"Content-Type": "text/xml; charset=utf-8", "SOAPAction": '"urn:example:customers/GetCustomers"'}


def test_wsdl_envelope_is_namespaced_and_well_formed():
    envelope = _operation()["envelope"]
    root = parse_xml(envelope)
    assert root.tag == f"{{{SOAP_ENV}}}Envelope"
    request = root[1][0]
    assert request.tag == "{urn:example:customers}GetCustomers"
    assert request.find("{urn:example:customers}region").text == "{region}"
    assert request.get("locale") == "{locale}"


def test_wsdl_fields_say_what_is_required():
    fields = {field["name"]: field for field in _operation()["fields"]}
    assert fields["region"]["required"] is True and fields["region"]["type"] == "string"
    assert fields["status"]["required"] is False
    # Required inside something optional, so not required overall.
    assert fields["limit"]["required"] is False and fields["limit"]["path"] == "GetCustomers/paging/limit"


def test_wsdl_a_type_that_contains_itself_stops():
    envelope = _operation()["envelope"]
    assert envelope.count("<tns:next>") <= 1 and envelope.count("tns:limit") >= 2


def test_wsdl_envelope_drops_blank_optional_blocks_when_filled_in():
    envelope = _operation()["envelope"]
    fields = {field["name"]: "" for field in _operation()["fields"]}
    out = render_xml_template(envelope, {**fields, "region": "EMEA"})
    root = parse_xml(out)
    assert root[1][0].find("{urn:example:customers}paging") is None
    assert root[1][0].find("{urn:example:customers}region").text == "EMEA"


UNQUALIFIED = WSDL.replace(' elementFormDefault="qualified"', "")


def test_wsdl_unqualified_children_have_no_prefix():
    envelope = _operation(UNQUALIFIED.replace("ENDPOINT", "https://x.test/"))["envelope"]
    assert "<tns:GetCustomers" in envelope and "<region>{region}</region>" in envelope


RPC = WSDL.replace('style="document"', 'style="rpc"').replace(
    '<wsdl:part name="parameters" element="tns:GetCustomers"/>',
    '<wsdl:part name="city" type="xs:string"/><wsdl:part name="zip" type="xs:string"/>',
).replace('<soap:body use="literal"/>', '<soap:body use="encoded" namespace="urn:rpc"/>')


def test_wsdl_rpc_wraps_parts_in_the_operation():
    operation = _operation(RPC.replace("ENDPOINT", "https://x.test/"))
    root = parse_xml(operation["envelope"])
    wrapper = root[1][0]
    assert wrapper.tag == "{urn:rpc}GetCustomers"
    assert [child.tag for child in wrapper] == ["city", "zip"]
    assert any("older encoded style" in warning for warning in operation["warnings"])


SOAP12 = WSDL.replace("http://schemas.xmlsoap.org/wsdl/soap/", "http://schemas.xmlsoap.org/wsdl/soap12/")


def test_wsdl_soap_12_headers_and_envelope():
    operation = _operation(SOAP12.replace("ENDPOINT", "https://x.test/"))
    assert operation["version"] == "1.2"
    assert operation["headers"] == {"Content-Type": 'application/soap+xml; charset=utf-8; action="urn:example:customers/GetCustomers"'}
    assert parse_xml(operation["envelope"]).tag == "{http://www.w3.org/2003/05/soap-envelope}Envelope"


EXTENSION = WSDL.replace(
    '<xs:element name="region" type="xs:string"/>', '<xs:element name="child" type="tns:Child"/>'
).replace(
    '<xs:complexType name="Paging">',
    '<xs:complexType name="Base"><xs:sequence><xs:element name="id" type="xs:int"/></xs:sequence></xs:complexType>'
    '<xs:complexType name="Child"><xs:complexContent><xs:extension base="tns:Base"><xs:sequence>'
    '<xs:element name="extra" type="xs:string"/></xs:sequence></xs:extension></xs:complexContent></xs:complexType>'
    '<xs:simpleType name="Colour"><xs:restriction base="xs:token"/></xs:simpleType>'
    '<xs:complexType name="Paging">',
)


def test_wsdl_extended_types_include_their_base():
    names = [field["name"] for field in _operation(EXTENSION.replace("ENDPOINT", "https://x.test/"))["fields"]]
    assert "id" in names and "extra" in names


def test_wsdl_a_pasted_import_is_reported_not_guessed():
    with_import = WSDL.replace("<wsdl:types>", '<wsdl:import namespace="urn:x" location="more.wsdl"/><wsdl:types>').replace("ENDPOINT", "https://x.test/")
    assert any("more.wsdl" in warning for warning in read_wsdl(with_import)["warnings"])


def test_wsdl_follows_imports_when_it_can():
    schema = (
        '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema" targetNamespace="urn:example:customers" '
        'elementFormDefault="qualified"><xs:element name="GetCustomers"><xs:complexType><xs:sequence>'
        '<xs:element name="from_import" type="xs:string"/></xs:sequence></xs:complexType></xs:element></xs:schema>'
    )
    head, tail = WSDL.split("<xs:schema", 1)[0], WSDL.split("</xs:schema>", 1)[1]
    bare = (
        head + '<xs:schema targetNamespace="urn:w"><xs:import namespace="urn:example:customers" schemaLocation="types.xsd"/></xs:schema>' + tail
    ).replace("ENDPOINT", "soap")
    asked = []

    def fetch(url):
        asked.append(url)
        return schema.encode()

    operation = read_wsdl(bare, url="https://svc.test/a/service.wsdl", fetch=fetch)["operations"][0]["soap"]
    assert asked == ["https://svc.test/a/types.xsd"]
    assert [field["name"] for field in operation["fields"]] == ["from_import"]
    # A relative address is relative to the WSDL.
    assert operation["endpoint"] == "https://svc.test/a/soap"


def test_wsdl_an_import_that_cannot_be_read_is_a_warning():
    with_import = WSDL.replace("<wsdl:types>", '<wsdl:import location="more.wsdl"/><wsdl:types>').replace("ENDPOINT", "https://x.test/")

    def fetch(url):
        raise OSError("nope")

    result = read_wsdl(with_import, url="https://x.test/a.wsdl", fetch=fetch)
    assert any("more.wsdl" in warning for warning in result["warnings"]) and result["operations"]


def test_wsdl_import_cycles_end():
    looped = WSDL.replace("<wsdl:types>", '<wsdl:import location="a.wsdl"/><wsdl:types>').replace("ENDPOINT", "https://x.test/")
    asked = []

    def fetch(url):
        asked.append(url)
        return looped.encode()

    read_wsdl(looped, url="https://x.test/a.wsdl", fetch=fetch)
    assert asked == []  # a.wsdl is the document already being read


def test_wsdl_very_large_operations_are_trimmed_and_say_so():
    many = "".join(f'<xs:element name="f{i}" type="xs:string" minOccurs="0"/>' for i in range(300))
    large = WSDL.replace('<xs:element name="status" type="xs:string" minOccurs="0"/>', many).replace("ENDPOINT", "https://x.test/")
    operation = _operation(large)
    names = [field["name"] for field in operation["fields"]]
    assert "region" in names and len(names) <= 60
    assert any("left out" in warning for warning in operation["warnings"])
    assert len(operation["envelope"]) < explorer.InspectRequest.model_fields["body"].metadata[0].max_length


def test_wsdl_duplicate_field_names_stay_distinct():
    twice = WSDL.replace(
        '<xs:element name="limit" type="xs:int"/>', '<xs:element name="status" type="xs:int"/><xs:element name="limit" type="xs:int"/>'
    ).replace("ENDPOINT", "https://x.test/")
    names = [field["name"] for field in _operation(twice)["fields"]]
    assert len(names) == len(set(names)) and "paging_status" in names


@pytest.mark.parametrize("text, message", [
    ('<description xmlns="http://www.w3.org/ns/wsdl"/>', "WSDL 2.0"),
    ("<project/>", "not a WSDL"),
    ('<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"/>', "not a WSDL"),
    ("<a><b></a>", "could not be read"),
    ('<!DOCTYPE d [<!ENTITY x "1">]><definitions/>', "could not be read"),
    ('<definitions xmlns="http://schemas.xmlsoap.org/wsdl/"/>', "no SOAP operations"),
])
def test_wsdl_refuses_what_it_cannot_use(text, message):
    with pytest.raises(WsdlError, match=message):
        read_wsdl(text)


# ── Fetching a WSDL over the network ────────────────────────────────────────


def _load(**params):
    return explorer.load_wsdl(explorer.WsdlRequest(**params))


def test_explorer_loads_a_wsdl_by_address_with_sign_in(server):
    result = _load(url=f"{server}/service.wsdl", auth_type="bearer", token="t")
    assert result["operations"][0]["soap"]["endpoint"] == f"{server}/soap"
    assert _Handler.seen[0]["headers"]["Authorization"] == "Bearer t"


def test_explorer_loading_a_wsdl_obeys_the_network_rules(monkeypatch):
    monkeypatch.delenv("REVOLUZEN_FLOW_API_SOURCE_ALLOW_PRIVATE", raising=False)
    with pytest.raises(ValueError, match="blocked"):
        _load(url="http://127.0.0.1:9/service.wsdl")


def test_explorer_a_failed_wsdl_download_says_why(server):
    with pytest.raises(ValueError, match="HTTP 404"):
        _load(url=f"{server}/nope.wsdl")


def test_explorer_will_not_carry_sign_in_to_another_site(server):
    text = WSDL.replace("<wsdl:types>", f'<wsdl:import location="http://127.0.0.1:{_Handler.other_port}/service.wsdl"/><wsdl:types>').replace("ENDPOINT", f"{server}/soap")

    def serve(*args, **kwargs):
        return _Response("text/xml"), text.encode(), False

    original = explorer._exchange
    try:
        explorer._exchange = serve
        result = _load(url=f"{server}/service.wsdl", auth_type="bearer", token="t")
    finally:
        explorer._exchange = original
    assert any("different site" in warning for warning in result["warnings"])
    assert _Handler.seen == []


def test_explorer_needs_a_wsdl_to_read():
    with pytest.raises(ValueError, match="Paste a WSDL"):
        _load()


# ── Connector from a request ────────────────────────────────────────────────


def _from_request(**overrides):
    settings = dict(
        connector_id="shop", display_name="Shop", category="saas", url="https://api.example.test/v1/orders",
        method="GET", headers={}, query={}, body="", body_type="json", fields=[], row_path="//order",
        auth_type="none", key_name="X-API-Key",
    )
    return manifest_from_request(**{**settings, **overrides})


def test_connector_repeats_the_request():
    manifest, warnings = _from_request(url="https://api.example.test/v1/orders?limit=5", query={"page": "2"}, headers={"X-Trace": "1"})
    stream = manifest["streams"][0]
    assert manifest["base_url"] == "https://api.example.test"
    assert stream["path"] == "/v1/orders" and stream["method"] == "GET" and stream["query"] == {"limit": "5", "page": "2"}
    assert stream["headers"] == {"X-Trace": "1"} and stream["response_format"] == "xml" and stream["data_path"] == "//order"
    assert manifest["auth"] == {"type": "none"} and manifest["params"] == [] and warnings == []


def test_connector_never_stores_a_secret():
    manifest, warnings = _from_request(
        url="https://api.example.test/x?api_key=CANARY1", query={"session_token": "CANARY2"},
        headers={"X-Auth-Token": "CANARY3", "Cookie": "CANARY4", "Authorization": "CANARY5"},
        method="POST", body_type="xml", body="<a><wsse:Password xmlns:wsse='u'>CANARY6</wsse:Password></a>",
    )
    assert "CANARY" not in json.dumps(manifest)
    assert len(warnings) == 6
    field = next(p for p in manifest["params"] if p["name"] == "Password")
    assert field["secret"] is True and field["required"] is True
    assert "{Password}" in manifest["streams"][0]["body_text"]


@pytest.mark.parametrize("auth_type, auth, params", [
    ("basic", {"type": "basic", "username_param": "auth_username", "password_param": "auth_password"}, ["auth_username", "auth_password"]),
    ("bearer", {"type": "bearer", "token_param": "auth_token"}, ["auth_token"]),
    ("api_key", {"type": "api_key", "key_param": "auth_key", "header_name": "X-K"}, ["auth_key"]),
    ("api_key_query", {"type": "api_key", "key_param": "auth_key", "query_param": "X-K"}, ["auth_key"]),
])
def test_connector_sign_in_becomes_secret_fields(auth_type, auth, params):
    manifest, _ = _from_request(auth_type=auth_type, key_name="X-K")
    assert manifest["auth"] == auth
    assert [p["name"] for p in manifest["params"]] == params
    assert all(p["secret"] for p in manifest["params"])


def test_connector_fields_come_from_the_placeholders():
    manifest, _ = _from_request(
        method="POST", body_type="xml", body="<a>{region}{status}{added}{api_token}</a>",
        fields=[
            {"name": "region", "label": "Region", "required": True, "default": "EMEA"},
            {"name": "status", "label": "Status", "required": False, "default": ""},
            {"name": "not_in_body", "label": "x", "required": True, "default": ""},
            {"name": "api_token", "label": "Token", "required": True, "default": "CANARY"},
        ],
    )
    params = {p["name"]: p for p in manifest["params"]}
    assert params["region"] == {"name": "region", "label": "Region", "required": True, "default": "EMEA"}
    assert params["status"] == {"name": "status", "label": "Status", "required": False}
    assert params["added"]["required"] is True and "not_in_body" not in params
    assert params["api_token"]["secret"] is True and "default" not in params["api_token"]


def test_connector_a_get_saves_no_body():
    manifest, _ = _from_request(method="GET", body="<a/>")
    assert "body_text" not in manifest["streams"][0]


@pytest.mark.parametrize("url", ["ftp://x/y", "https://user:pw@x.test/y", "not a url"])
def test_connector_refuses_bad_addresses(url):
    with pytest.raises(ValueError):
        _from_request(url=url)


def test_connector_refuses_a_row_path_that_cannot_work():
    with pytest.raises(XmlError):
        _from_request(row_path="//order/@id")


def test_from_request_endpoint_returns_a_runnable_draft():
    app = FastAPI()
    app.include_router(authoring_router)
    with TestClient(app) as client:
        reply = client.post("/api/connectors/author/from-request", json={
            "connector_id": "shop", "url": "https://api.example.test/o", "row_path": "//order",
        })
    assert reply.status_code == 200
    body = reply.json()
    assert body["mode"] == "request" and body["validation"]["valid"] is True
    assert body["runtime_manifest"]["streams"][0]["data_path"] == "//order"


# ── The whole route ─────────────────────────────────────────────────────────


@pytest.fixture
def connector_store(tmp_path, monkeypatch):
    monkeypatch.setattr(rf, "_USER_MANIFEST_DIR", str(tmp_path / "manifests"))
    rf.load_manifests(force=True)
    yield
    monkeypatch.undo()
    rf.load_manifests(force=True)


def test_soap_service_tried_saved_and_run_in_a_pipeline(server, ctx, connector_store):
    # Import the WSDL by its address and choose the operation.
    operation = explorer.load_wsdl(explorer.WsdlRequest(url=f"{server}/service.wsdl"))["operations"][0]["soap"]
    fields = operation["fields"]

    # Try it in the Explorer.
    tried = _inspect(
        url=operation["endpoint"], method="POST", confirm_write=True, headers=operation["headers"],
        body=operation["envelope"], body_type="xml",
        body_values={**{field["name"]: "" for field in fields}, "region": "EMEA"},
    )
    assert tried["status"] == 200 and tried["xml"]["fault"] is None
    sent = _Handler.seen[-1]
    assert sent["headers"]["SOAPAction"] == '"urn:example:customers/GetCustomers"'
    assert sent["headers"]["Content-Type"] == "text/xml; charset=utf-8"
    assert "EMEA" in sent["body"] and "paging" not in sent["body"] and "{" not in sent["body"]
    record = tried["xml"]["records"][0]
    assert record["path"].endswith("/customers/customer") and record["count"] == 2

    # Save it as a connector.
    manifest, warnings = manifest_from_request(
        connector_id="customers", display_name="Customers", category="saas", url=operation["endpoint"],
        method="POST", headers=operation["headers"], query={}, body=operation["envelope"], body_type="xml",
        fields=[{**field, "default": "EMEA" if field["name"] == "region" else ""} for field in fields],
        row_path=record["path"], auth_type="none", key_name="X-API-Key", operation=operation["operation"],
    )
    assert warnings == []
    saved = rf.save_user_manifest(manifest)
    assert saved.tier == "beta"

    # Run it as a pipeline step.
    relation = rf.SaaSConnectorNode({"connector_id": "customers", "stream": "getcustomers", "region": "APAC"}).execute(ctx)
    assert _rows(relation) == [{"id": "1", "name": "Asha"}, {"id": "2", "name": "Ben"}]
    assert "APAC" in _Handler.seen[-1]["body"]

    # A fault from the service stops the step and says why.
    with pytest.raises(RuntimeError, match="Unknown region"):
        rf.SaaSConnectorNode({"connector_id": "customers", "stream": "getcustomers", "region": "BAD"}).execute(ctx)


def test_xml_api_tried_saved_and_run_in_a_pipeline(server, ctx, connector_store):
    tried = _inspect(url=f"{server}/orders", query={"status": "open"})
    assert tried["kind"] == "xml"
    path = tried["xml"]["records"][0]["path"]

    manifest, _ = manifest_from_request(
        connector_id="shop_orders", display_name="Orders", category="saas", url=f"{server}/orders",
        method="GET", headers={}, query={"status": "open"}, body="", body_type="json", fields=[],
        row_path=path, auth_type="none", key_name="X-API-Key",
    )
    rf.save_user_manifest(manifest)

    rows = _rows(rf.SaaSConnectorNode({"connector_id": "shop_orders", "stream": "orders"}).execute(ctx))
    assert [row["sku"] for row in rows] == ["A&B", "C"]
    assert _Handler.seen[-1]["path"] == "/orders?status=open"


def test_a_saved_soap_connector_signs_in_from_a_saved_credential(server, ctx, connector_store):
    operation = explorer.load_wsdl(explorer.WsdlRequest(url=f"{server}/service.wsdl"))["operations"][0]["soap"]
    manifest, _ = manifest_from_request(
        connector_id="signed", display_name="Signed", category="saas", url=operation["endpoint"], method="POST",
        headers=operation["headers"], query={}, body=operation["envelope"], body_type="xml",
        fields=[{**field, "default": ""} for field in operation["fields"]], row_path="//customer",
        auth_type="basic", key_name="X-API-Key", operation=operation["operation"],
    )
    rf.save_user_manifest(manifest)

    credential = SimpleNamespace(config={"username": "u", "password": "p"}, workspace_id="default")
    ctx.app_state = {"credential_store": SimpleNamespace(get_raw=lambda *a, **k: credential, mark_used=lambda *a, **k: None)}
    node = rf.SaaSConnectorNode({"connector_id": "signed", "stream": "getcustomers", "region": "EMEA", "credential_id": "c1"})
    assert len(_rows(node.execute(ctx))) == 2
    assert _Handler.seen[-1]["headers"]["Authorization"] == "Basic dTpw"


# ── Steps that call an API through a saved connection ───────────────────────


def _with_connection(ctx, config, kind="rest_api", workspace="default"):
    connection = SimpleNamespace(config=dict(config), type=kind, credential_id=None, workspace_id=workspace)

    def get(connection_id, workspace_id=None):
        return None if workspace_id is not None and workspace_id != connection.workspace_id else connection

    ctx.app_state = {"connection_store": SimpleNamespace(get=get), "credential_store": None}
    ctx._workflow_ref = SimpleNamespace(workspace_id="default")
    return ctx


@pytest.mark.parametrize("config, headers, query", [
    ({}, {}, {}),
    ({"auth_type": "none", "token": "t"}, {}, {}),
    ({"auth_type": "bearer", "token": "t"}, {"Authorization": "Bearer t"}, {}),
    ({"token": "t"}, {"Authorization": "Bearer t"}, {}),
    # Saved by older forms: the key, its header and its prefix kept apart.
    ({"api_key": "k", "api_key_header": "X-K", "api_key_prefix": "Token"}, {"X-K": "Token k"}, {}),
    ({"auth_type": "api_key", "token": "k", "api_key_header": "X-Key"}, {"X-Key": "k"}, {}),
    ({"auth_type": "api_key", "token": "k"}, {"Authorization": "k"}, {}),
    ({"auth_type": "api_key_query", "token": "k", "api_key_param": "key"}, {}, {"key": "k"}),
    ({"auth_type": "basic", "username": "u", "password": "p"}, {"Authorization": "Basic dTpw"}, {}),
    ({"auth_type": "custom_header", "custom_header_name": "X-Auth", "custom_header_value": "v"}, {"X-Auth": "v"}, {}),
    ({"auth_type": "bearer", "token": "t", "headers": "X-Team: data\nAuthorization: loses"}, {"X-Team": "data", "Authorization": "Bearer t"}, {}),
])
def test_connection_sign_in_as_the_form_offers_it(config, headers, query):
    assert request_auth(config) == (headers, query)


def test_connection_oauth_client_credentials(server):
    config = {"auth_type": "oauth2_cc", "token_url": f"{server}/token", "client_id": "c", "client_secret": "s3"}
    assert request_auth(config, allow_private_env="REVOLUZEN_FLOW_HTTP_ALLOW_PRIVATE") == ({"Authorization": "Bearer tok-1"}, {})
    with pytest.raises(ValueError, match=r"refused \(HTTP 401\)"):
        request_auth({**config, "client_secret": "wrong"}, allow_private_env="REVOLUZEN_FLOW_HTTP_ALLOW_PRIVATE")
    with pytest.raises(ValueError, match="token URL and a client ID"):
        request_auth({"auth_type": "oauth2_cc"})


@pytest.mark.parametrize("url, expected", [
    ("", "https://api.example.test/v1"),
    ("/", "https://api.example.test/v1"),
    ("/orders", "https://api.example.test/v1/orders"),
    ("orders?x=1", "https://api.example.test/v1/orders?x=1"),
    ("?x=1", "https://api.example.test/v1?x=1"),
    ("https://api.example.test/v2/x", "https://api.example.test/v2/x"),
])
def test_connection_address_and_step_address(url, expected):
    assert connection_url("https://api.example.test/v1/", url) == expected


@pytest.mark.parametrize("url", ["https://elsewhere.test/v1", "http://api.example.test/v1", "https://api.example.test:8443/v1", "https://{host}/x"])
def test_connection_sign_in_never_goes_to_another_site(url):
    with pytest.raises(ValueError, match="different site"):
        connection_url("https://api.example.test/v1", url)


def test_connection_helpers():
    assert with_query("https://x.test/a?b=1", {"key": "k y"}) == "https://x.test/a?b=1&key=k+y"
    assert without_query("https://x.test/a?key=secret") == "https://x.test/a"
    assert same_site("https://x.test/a", "https://x.test:443/b")
    assert ssl_context_for({}) is None and ssl_context_for({"ssl_verify": True}) is None
    assert ssl_context_for({"ssl_verify": "false"}).verify_mode == ssl.CERT_NONE


def test_http_step_uses_its_saved_connection(server, ctx):
    _with_connection(ctx, {"base_url": server, "auth_type": "bearer", "token": "conn-token"})
    rows = _http(ctx, url="/orders", connection_id="c1", row_path="//customer")
    assert rows == [{"name": "Asha"}, {"name": "Ben"}]
    assert _Handler.seen[0]["path"] == "/orders" and _Handler.seen[0]["headers"]["Authorization"] == "Bearer conn-token"


def test_http_step_slash_means_the_connection_address(server, ctx):
    _with_connection(ctx, {"base_url": f"{server}/orders"})
    assert len(_http(ctx, url="/", connection_id="c1", row_path="//order")) == 2
    assert _Handler.seen[0]["path"] == "/orders"


def test_http_step_sign_in_on_the_step_wins(server, ctx):
    _with_connection(ctx, {"base_url": server, "auth_type": "bearer", "token": "conn-token"})
    _http(ctx, url="/json", connection_id="c1", auth_type="basic", auth_user="u", auth_pass="p")
    assert _Handler.seen[0]["headers"]["Authorization"] == "Basic dTpw"


def test_http_step_key_in_the_query_stays_out_of_messages(server, ctx):
    _with_connection(ctx, {"base_url": server, "auth_type": "api_key_query", "token": "k3y-secret", "api_key_param": "key"})
    with pytest.raises(ValueError) as caught:
        _http(ctx, url="/nowhere", connection_id="c1")
    assert "404" in str(caught.value) and "k3y-secret" not in str(caught.value)
    assert _Handler.seen[0]["path"] == "/nowhere?key=k3y-secret"


def test_http_step_will_not_send_a_connection_sign_in_elsewhere(server, ctx):
    _with_connection(ctx, {"base_url": server, "auth_type": "bearer", "token": "conn-token"})
    with pytest.raises(ValueError, match="different site"):
        _http(ctx, url=f"http://127.0.0.1:{_Handler.other_port}/orders", connection_id="c1")
    assert _Handler.seen == []


def test_http_step_refuses_a_connection_from_another_workspace(server, ctx):
    _with_connection(ctx, {"base_url": server}, workspace="other")
    with pytest.raises(ValueError, match="no longer exists"):
        _http(ctx, url="/orders", connection_id="c1")


def test_http_step_refuses_a_connection_that_is_not_an_api(server, ctx):
    _with_connection(ctx, {"host": "db"}, kind="postgresql")
    with pytest.raises(ValueError, match="cannot make web requests"):
        _http(ctx, url="/orders", connection_id="c1")


def test_http_step_per_row_json_values_are_escaped(server, ctx):
    _per_row(ctx, ['say "hi" \\ bye'], url=f"{server}/json", method="POST", body='{"msg": "{name}", "n": 1}', body_type="json")
    assert json.loads(_Handler.seen[0]["body"]) == {"msg": 'say "hi" \\ bye', "n": 1}


def _source(ctx, **params):
    return _rows(ApiSourceNode({"connection_id": "c1", **params}).execute(ctx))


def test_api_source_reads_xml_through_its_connection(server, ctx):
    _with_connection(ctx, {"base_url": server, "auth_type": "api_key", "token": "k", "api_key_header": "X-Key"})
    assert _source(ctx, path="/orders", row_path="//customer") == [{"name": "Asha"}, {"name": "Ben"}]
    assert _Handler.seen[0]["headers"]["X-Key"] == "k"
    assert _Handler.seen[0]["headers"]["Accept"].startswith("application/json, application/xml")


def test_api_source_json_is_unchanged(server, ctx):
    _with_connection(ctx, {"base_url": server})
    assert _source(ctx, path="/json") == [{"id": 1}]


@pytest.mark.parametrize("path, message", [("/text", "neither JSON nor XML"), ("/html", "web page"), ("/png", "PNG image"), ("/fault", "Unknown region")])
def test_api_source_says_what_came_back(server, ctx, path, message):
    _with_connection(ctx, {"base_url": server})
    with pytest.raises(ValueError, match=message):
        _source(ctx, path=path, max_retries=0)


def test_api_source_pages_through_xml_with_the_form_names(server, ctx):
    _with_connection(ctx, {"base_url": server})
    rows = _source(ctx, path="/paged", row_path="//item", pagination="page_number", page_size=2, max_pages=5)
    assert [row["n"] for row in rows] == ["1", "2", "3", "4"]


def test_api_source_follows_a_link_header(server, ctx):
    _with_connection(ctx, {"base_url": server})
    assert _source(ctx, path="/linked?p=1", pagination="link_header") == [{"id": 1}, {"id": 2}]


def test_api_source_will_not_follow_a_next_page_to_another_site(server, ctx):
    _with_connection(ctx, {"base_url": server, "auth_type": "bearer", "token": "t"})
    with pytest.raises(ValueError, match="different site"):
        _source(ctx, path="/linked-away", pagination="link")


def test_connection_test_accepts_an_xml_api(server):
    from revoluzen_flow.connections.tester import ConnectionTester

    result = ConnectionTester().test_connection("rest_api", {"base_url": f"{server}/orders"})
    assert result["success"] is True
    assert _Handler.seen[0]["headers"]["Accept"].startswith("application/json, application/xml")


def test_a_connector_step_needs_nothing_upstream():
    from revoluzen_flow.ir.node_metadata import contract_for

    for step_type in ("saas_connector", "rest_connector"):
        assert contract_for(step_type)["required"] == 0


# ── SOAP is reached through a connector, not a connection type ──────────────


def test_soap_is_not_a_connection_type():
    assert "soap" not in CONNECTION_TYPES
    assert all("soap" not in members for members in CONNECTION_CATEGORIES.values())


def test_the_module_exports_what_it_says():
    assert all(hasattr(safe_xml, name) for name in safe_xml.__all__)
