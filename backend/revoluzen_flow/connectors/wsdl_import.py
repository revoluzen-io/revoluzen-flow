"""SOAP services, read from their WSDL.

A WSDL says which operations a service has, where it listens, and what each
request must look like. :func:`read_wsdl` turns that into a list of operations,
each with a ready-to-fill SOAP envelope whose values are ``{name}`` placeholders.
No SOAP library is needed: an envelope is XML, and the framework already sends
XML and reads XML replies.

What a generated envelope leaves to the person:

* Optional elements carry ``<!--Optional:-->`` and are dropped when left blank.
* Very large operations are trimmed: every required field is kept and optional
  ones are kept up to a limit, and the import says so. An envelope can always
  be edited by hand before it is tried or saved.

Documents are parsed with ``safe_xml``. Reaching other documents (imports) is the
caller's decision, through the ``fetch`` function it passes.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Iterator
from urllib.parse import urljoin
from xml.etree import ElementTree as ET
from xml.sax.saxutils import quoteattr

from revoluzen_flow.common.safe_xml import XmlError, parse_xml

__all__ = ["WsdlError", "read_wsdl"]

WSDL_NS = "http://schemas.xmlsoap.org/wsdl/"
WSDL2_NS = "http://www.w3.org/ns/wsdl"
SOAP_BINDINGS = {
    "http://schemas.xmlsoap.org/wsdl/soap/": "1.1",
    "http://schemas.xmlsoap.org/wsdl/soap12/": "1.2",
}
XSD_NS = "http://www.w3.org/2001/XMLSchema"
ENVELOPE_NS = {
    "1.1": "http://schemas.xmlsoap.org/soap/envelope/",
    "1.2": "http://www.w3.org/2003/05/soap-envelope",
}

MAX_DOCUMENTS = 25
MAX_OPERATIONS = 500
MAX_FIELDS = 60
MAX_REQUIRED_FIELDS = 150
MAX_TYPE_DEPTH = 8

QName = tuple[str, str]


class WsdlError(ValueError):
    """The document cannot be used as a WSDL. The message is safe to show."""


class _Node:
    """An element in its namespace scope.

    Attribute values like ``element="tns:GetOrders"`` only mean something
    relative to the prefixes in force where they are written, so the document
    is read with its prefixes intact and resolved here.
    """

    __slots__ = ("element", "scope", "ns", "name")

    def __init__(self, element: ET.Element, scope: dict[str, str]):
        declared = {
            key[6:] if key.startswith("xmlns:") else "": value
            for key, value in element.attrib.items()
            if key == "xmlns" or key.startswith("xmlns:")
        }
        self.scope = {**scope, **declared} if declared else scope
        prefix, _, self.name = element.tag.rpartition(":")
        self.ns = self.scope.get(prefix, "")
        self.element = element

    def __iter__(self) -> Iterator["_Node"]:
        for child in self.element:
            if isinstance(child.tag, str):
                yield _Node(child, self.scope)

    def get(self, attribute: str, default: str = "") -> str:
        return self.element.attrib.get(attribute, default)

    def first(self, ns: str, *names: str) -> "_Node | None":
        return next((child for child in self if child.ns == ns and child.name in names), None)

    def qname(self, attribute: str) -> QName | None:
        value = self.get(attribute).strip()
        if not value:
            return None
        prefix, _, local = value.rpartition(":")
        return self.scope.get(prefix, ""), local

    def documentation(self) -> str:
        node = self.first(WSDL_NS, "documentation") or self.first(XSD_NS, "documentation")
        return " ".join("".join(node.element.itertext()).split())[:200] if node is not None else ""


class _SchemaContext:
    __slots__ = ("target", "qualified")

    def __init__(self, target: str, qualified: bool):
        self.target = target
        self.qualified = qualified


class _Field:
    """One element or attribute of a request."""

    def __init__(self, name: str, ns: str = "", attribute: bool = False):
        self.name = name
        self.ns = ns
        self.attribute = attribute
        self.required = True
        self.repeats = False
        self.xsd_type = "string"
        self.children: list[_Field] = []
        self.attributes: list[_Field] = []
        self.placeholder = ""

    @property
    def is_leaf(self) -> bool:
        return not self.children


class _Reader:
    def __init__(self, fetch: Callable[[str], bytes] | None):
        self.fetch = fetch
        self.messages: dict[QName, list[dict[str, Any]]] = {}
        self.port_types: dict[QName, dict[str, dict[str, Any]]] = {}
        self.bindings: dict[QName, dict[str, Any]] = {}
        self.services: list[dict[str, Any]] = []
        self.elements: dict[QName, tuple[_Node, _SchemaContext]] = {}
        self.types: dict[QName, tuple[_Node, _SchemaContext]] = {}
        self.groups: dict[QName, tuple[_Node, _SchemaContext]] = {}
        self.attribute_groups: dict[QName, tuple[_Node, _SchemaContext]] = {}
        self.warnings: list[str] = []
        self.seen: set[str] = set()
        self.saw_definitions = False

    def warn(self, text: str) -> None:
        if text not in self.warnings and len(self.warnings) < 20:
            self.warnings.append(text)

    # ── Documents ───────────────────────────────────────────────────────

    def read_document(self, data: bytes | str, url: str = "", default_target: str = "") -> None:
        try:
            root = _Node(parse_xml(data, keep_prefixes=True), {})
        except XmlError as exc:
            raise WsdlError(f"This WSDL could not be read. {exc}") from None
        if root.ns == XSD_NS and root.name == "schema":
            self._read_schema(root, url, default_target)
            return
        if root.ns == WSDL2_NS:
            raise WsdlError("This is a WSDL 2.0 document. Flow reads WSDL 1.1, which nearly all SOAP services publish.")
        if root.ns != WSDL_NS or root.name != "definitions":
            raise WsdlError("This is XML, but it is not a WSDL. A WSDL starts with a <definitions> element.")
        self.saw_definitions = True
        target = root.get("targetNamespace")
        for node in root:
            if node.ns == WSDL_NS and node.name == "import":
                self._follow(node.get("location"), url)
            elif node.ns == WSDL_NS and node.name == "types":
                for schema in node:
                    if schema.ns == XSD_NS and schema.name == "schema":
                        self._read_schema(schema, url, "")
            elif node.ns == WSDL_NS and node.name == "message":
                self._read_message(node, target)
            elif node.ns == WSDL_NS and node.name == "portType":
                self._read_port_type(node, target)
            elif node.ns == WSDL_NS and node.name == "binding":
                self._read_binding(node, target)
            elif node.ns == WSDL_NS and node.name == "service":
                self._read_service(node)

    def _follow(self, location: str, base_url: str, default_target: str = "") -> None:
        if not location:
            return
        if self.fetch is None or not (base_url or location.startswith(("http://", "https://"))):
            self.warn(f"This WSDL refers to another file ({location}). Import it by its address so that file can be read too.")
            return
        url = urljoin(base_url, location)
        if url in self.seen:
            return
        if len(self.seen) >= MAX_DOCUMENTS:
            self.warn(f"This WSDL uses more than {MAX_DOCUMENTS} files. The rest were not read.")
            return
        self.seen.add(url)
        try:
            self.read_document(self.fetch(url), url, default_target)
        except (ValueError, OSError) as exc:
            self.warn(f"Could not read {location}: {str(exc)[:160]}")

    def _read_message(self, node: _Node, target: str) -> None:
        parts = [
            {"name": part.get("name"), "element": part.qname("element"), "type": part.qname("type")}
            for part in node
            if part.ns == WSDL_NS and part.name == "part"
        ]
        self.messages[(target, node.get("name"))] = parts

    def _read_port_type(self, node: _Node, target: str) -> None:
        operations: dict[str, dict[str, Any]] = {}
        for operation in node:
            if operation.ns == WSDL_NS and operation.name == "operation":
                message = operation.first(WSDL_NS, "input")
                operations[operation.get("name")] = {
                    "input": message.qname("message") if message is not None else None,
                    "documentation": operation.documentation(),
                }
        self.port_types[(target, node.get("name"))] = operations

    def _read_binding(self, node: _Node, target: str) -> None:
        version = ""
        style = "document"
        for child in node:
            if child.name == "binding" and child.ns in SOAP_BINDINGS:
                version = SOAP_BINDINGS[child.ns]
                style = child.get("style") or style
        if not version:
            return  # An HTTP GET/POST binding. Those are plain URLs, not SOAP.
        operations: dict[str, dict[str, Any]] = {}
        for operation in node:
            if operation.ns != WSDL_NS or operation.name != "operation":
                continue
            info: dict[str, Any] = {"action": "", "style": style, "use": "literal", "namespace": "", "headers": []}
            for child in operation:
                if child.name == "operation" and child.ns in SOAP_BINDINGS:
                    info["action"] = child.get("soapAction")
                    info["style"] = child.get("style") or style
                elif child.ns == WSDL_NS and child.name == "input":
                    for item in child:
                        if item.ns not in SOAP_BINDINGS:
                            continue
                        if item.name == "body":
                            info["use"] = item.get("use") or "literal"
                            info["namespace"] = item.get("namespace")
                        elif item.name == "header":
                            info["headers"].append({"message": item.qname("message"), "part": item.get("part")})
            operations[operation.get("name")] = info
        self.bindings[(target, node.get("name"))] = {"type": node.qname("type"), "version": version, "operations": operations}

    def _read_service(self, node: _Node) -> None:
        ports = []
        for port in node:
            if port.ns != WSDL_NS or port.name != "port":
                continue
            address = next((c for c in port if c.name == "address" and c.ns in SOAP_BINDINGS), None)
            if address is not None:
                ports.append({"name": port.get("name"), "binding": port.qname("binding"), "address": address.get("location")})
        self.services.append({"name": node.get("name"), "ports": ports})

    def _read_schema(self, schema: _Node, url: str, default_target: str) -> None:
        target = schema.get("targetNamespace") or default_target
        context = _SchemaContext(target, schema.get("elementFormDefault") == "qualified")
        tables = {
            "element": self.elements, "complexType": self.types, "simpleType": self.types,
            "group": self.groups, "attributeGroup": self.attribute_groups,
        }
        for node in schema:
            if node.ns != XSD_NS:
                continue
            if node.name in tables and node.get("name"):
                tables[node.name][(target, node.get("name"))] = (node, context)
            elif node.name == "import":
                self._follow(node.get("schemaLocation"), url)
            elif node.name in ("include", "redefine"):
                self._follow(node.get("schemaLocation"), url, target)

    # ── Request shape ───────────────────────────────────────────────────

    def element_field(self, node: _Node, context: _SchemaContext, depth: int, stack: frozenset) -> _Field | None:
        reference = node.qname("ref")
        if reference:
            found = self.elements.get(reference)
            if found is None:
                self.warn(f"The definition of {reference[1]} was not found.")
                return None
            field = self._element_body(found[0], found[1], reference[0], depth, stack)
        else:
            form = node.get("form") or ("qualified" if context.qualified else "unqualified")
            field = self._element_body(node, context, context.target if form == "qualified" else "", depth, stack)
        field.required = node.get("minOccurs", "1") != "0"
        field.repeats = node.get("maxOccurs", "1") not in ("0", "1")
        return field

    def global_field(self, name: QName) -> _Field | None:
        found = self.elements.get(name)
        if found is None:
            self.warn(f"The definition of {name[1]} was not found.")
            return None
        return self._element_body(found[0], found[1], name[0], 0, frozenset())

    def _element_body(self, node: _Node, context: _SchemaContext, ns: str, depth: int, stack: frozenset) -> _Field:
        field = _Field(node.get("name"), ns)
        inline = node.first(XSD_NS, "complexType")
        if inline is not None:
            self._complex(inline, context, field, depth, stack)
        elif node.first(XSD_NS, "simpleType") is None and node.qname("type"):
            self.apply_type(field, node.qname("type"), depth, stack)
        return field

    def apply_type(self, field: _Field, type_name: QName, depth: int, stack: frozenset) -> None:
        field.xsd_type = type_name[1]
        if type_name[0] == XSD_NS:
            return
        found = self.types.get(type_name)
        if found is None:
            self.warn(f"The definition of {type_name[1]} was not found.")
            return
        node, context = found
        if node.name == "simpleType":
            restriction = node.first(XSD_NS, "restriction")
            base = restriction.qname("base") if restriction is not None else None
            field.xsd_type = base[1] if base else "string"
        elif type_name not in stack and depth < MAX_TYPE_DEPTH:
            self._complex(node, context, field, depth + 1, stack | {type_name})

    def _complex(self, node: _Node, context: _SchemaContext, field: _Field, depth: int, stack: frozenset) -> None:
        for child in node:
            if child.ns != XSD_NS:
                continue
            if child.name in ("sequence", "all", "choice"):
                self._particles(child, context, field, depth, stack, child.name == "choice" or child.get("minOccurs") == "0")
            elif child.name == "group":
                self._group(child, field, depth, stack, child.get("minOccurs") == "0")
            elif child.name == "attribute":
                self._attribute(child, field)
            elif child.name == "attributeGroup" and child.qname("ref") in self.attribute_groups:
                group, group_context = self.attribute_groups[child.qname("ref")]
                self._complex(group, group_context, field, depth, stack)
            elif child.name in ("complexContent", "simpleContent"):
                derived = child.first(XSD_NS, "extension", "restriction")
                base = derived.qname("base") if derived is not None else None
                if derived is None or base is None:
                    continue
                if child.name == "simpleContent":
                    field.xsd_type = base[1]
                elif derived.name == "extension" and base[0] != XSD_NS:
                    self.apply_type(field, base, depth, stack)
                self._complex(derived, context, field, depth, stack)

    def _particles(self, group: _Node, context: _SchemaContext, field: _Field, depth: int, stack: frozenset, optional: bool) -> None:
        for child in group:
            if child.ns != XSD_NS:
                continue
            if child.name == "element":
                member = self.element_field(child, context, depth, stack)
                if member is not None:
                    member.required = member.required and not optional
                    field.children.append(member)
            elif child.name in ("sequence", "choice", "all"):
                self._particles(child, context, field, depth, stack, optional or child.name == "choice" or child.get("minOccurs") == "0")
            elif child.name == "group":
                self._group(child, field, depth, stack, optional)

    def _group(self, node: _Node, field: _Field, depth: int, stack: frozenset, optional: bool) -> None:
        reference = node.qname("ref")
        if reference not in self.groups or reference in stack:
            return
        group, context = self.groups[reference]
        inner = group.first(XSD_NS, "sequence", "all", "choice")
        if inner is not None:
            self._particles(inner, context, field, depth, stack | {reference}, optional or inner.name == "choice")

    def _attribute(self, node: _Node, field: _Field) -> None:
        if not node.get("name") or node.get("use") == "prohibited":
            return
        attribute = _Field(node.get("name"), attribute=True)
        attribute.required = node.get("use") == "required"
        type_name = node.qname("type")
        attribute.xsd_type = type_name[1] if type_name else "string"
        field.attributes.append(attribute)


# ── Filling fields and writing the envelope ────────────────────────────────


def _leaves(field: _Field, required_chain: bool, depth: int, path: tuple[str, ...], out: list) -> None:
    here = (*path, field.name)
    chain = required_chain and field.required
    for attribute in field.attributes:
        out.append((not (chain and attribute.required), depth + 1, attribute, (*here, f"@{attribute.name}")))
    if field.is_leaf:
        out.append((not chain, depth, field, here))
    for child in field.children:
        _leaves(child, chain, depth + 1, here, out)


def _placeholder(name: str, parent: str, used: set[str]) -> str:
    clean = re.sub(r"\W", "_", name, flags=re.ASCII) or "value"
    if clean[0].isdigit():
        clean = f"_{clean}"
    qualifier = re.sub(r"\W", "_", parent, flags=re.ASCII)
    candidates = [clean, f"{qualifier}_{clean}"]
    number = 2
    while True:
        for candidate in candidates:
            if candidate not in used:
                used.add(candidate)
                return candidate
        candidates = [f"{clean}_{number}"]
        number += 1


def _choose_fields(roots: list[_Field], warnings: list[str]) -> list[dict[str, Any]]:
    """Give each field that stays a placeholder, required ones first."""
    leaves: list = []
    for root in roots:
        _leaves(root, True, 0, (), leaves)
    leaves.sort(key=lambda item: (item[0], item[1]))

    kept: list[dict[str, Any]] = []
    used: set[str] = set()
    required_count = optional_count = dropped = 0
    for not_required, _, field, path in leaves:
        if not_required:
            within = optional_count < MAX_FIELDS - min(required_count, MAX_FIELDS)
            optional_count += 1
        else:
            within = required_count < MAX_REQUIRED_FIELDS
            required_count += 1
        if not within:
            dropped += 1
            continue
        parent = path[-2].lstrip("@") if len(path) > 1 else ""
        field.placeholder = _placeholder(field.name, parent, used)
        kept.append({
            "name": field.placeholder,
            "label": field.name,
            "type": field.xsd_type,
            "required": not not_required,
            "repeats": field.repeats,
            "path": "/".join(path),
        })
    if dropped:
        warnings.append(f"This operation is very large, so {dropped} fields were left out of the envelope. Add any you need by hand.")
    return kept


def _keeps_anything(field: _Field) -> bool:
    if any(attribute.placeholder for attribute in field.attributes):
        return True
    return bool(field.placeholder) if field.is_leaf else any(_keeps_anything(child) for child in field.children)


class _Prefixes:
    def __init__(self) -> None:
        self.by_namespace: dict[str, str] = {}

    def of(self, namespace: str) -> str:
        if namespace not in self.by_namespace:
            self.by_namespace[namespace] = "tns" if not self.by_namespace else f"ns{len(self.by_namespace)}"
        return self.by_namespace[namespace]


def _write_field(field: _Field, prefixes: _Prefixes, indent: str, lines: list[str], root: bool = False) -> None:
    if not root and not _keeps_anything(field):
        return
    name = f"{prefixes.of(field.ns)}:{field.name}" if field.ns else field.name
    attributes = "".join(f' {a.name}="{{{a.placeholder}}}"' for a in field.attributes if a.placeholder)
    if not root and not field.required:
        lines.append(f"{indent}<!--Optional:-->")
    if field.is_leaf:
        value = f"{{{field.placeholder}}}" if field.placeholder else ""
        lines.append(f"{indent}<{name}{attributes}>{value}</{name}>")
        return
    lines.append(f"{indent}<{name}{attributes}>")
    for child in field.children:
        _write_field(child, prefixes, f"{indent}  ", lines)
    lines.append(f"{indent}</{name}>")


def _part_field(reader: _Reader, part: dict[str, Any]) -> _Field | None:
    if part.get("element"):
        return reader.global_field(part["element"])
    field = _Field(part.get("name") or "value")
    if part.get("type"):
        reader.apply_type(field, part["type"], 0, frozenset())
    return field


def _body_fields(reader: _Reader, operation: str, parts: list[dict[str, Any]], binding: dict[str, Any], target: str) -> list[_Field]:
    if binding["style"] == "rpc":
        wrapper = _Field(operation, binding["namespace"] or target)
        for part in parts:
            field = _part_field(reader, part)
            if field is not None:
                wrapper.children.append(field)
        return [wrapper]
    return [field for field in (_part_field(reader, part) for part in parts) if field is not None]


def _operation(
    reader: _Reader, name: str, port_type: dict[str, Any], binding: dict[str, Any], version: str,
    port: dict[str, Any], service: str, target: str, base_url: str,
) -> dict[str, Any]:
    parts = reader.messages.get(port_type.get("input") or ("", ""), [])
    warnings: list[str] = []
    body = _body_fields(reader, name, parts, binding, target)

    header_fields: list[_Field] = []
    for header in binding["headers"]:
        wanted = reader.messages.get(header["message"] or ("", ""), [])
        for part in wanted:
            if part["name"] == header["part"]:
                field = _part_field(reader, part)
                if field is not None:
                    field.required = False
                    header_fields.append(field)

    fields = _choose_fields([*body, *header_fields], warnings)
    if binding["use"] == "encoded":
        warnings.append("This operation uses the older encoded style. The service may need type attributes added to the envelope.")

    prefixes = _Prefixes()
    body_lines: list[str] = []
    for field in body:
        _write_field(field, prefixes, "    ", body_lines, root=True)
    header_lines: list[str] = []
    for field in header_fields:
        _write_field(field, prefixes, "    ", header_lines)

    declarations = [f"xmlns:soapenv={quoteattr(ENVELOPE_NS[version])}"]
    declarations.extend(f"xmlns:{prefix}={quoteattr(ns)}" for ns, prefix in prefixes.by_namespace.items())
    lines = [f"<soapenv:Envelope {' '.join(declarations)}>"]
    if header_lines:
        lines.extend(["  <soapenv:Header>", *header_lines, "  </soapenv:Header>"])
    else:
        lines.append("  <soapenv:Header/>")
    lines.extend(["  <soapenv:Body>", *body_lines, "  </soapenv:Body>", "</soapenv:Envelope>"])

    action = binding["action"]
    if version == "1.1":
        headers = {"Content-Type": "text/xml; charset=utf-8", "SOAPAction": f'"{action}"'}
    else:
        content_type = "application/soap+xml; charset=utf-8"
        headers = {"Content-Type": f'{content_type}; action="{action}"' if action else content_type}

    endpoint = port["address"]
    if base_url and not endpoint.startswith(("http://", "https://")):
        endpoint = urljoin(base_url, endpoint)

    return {
        "method": "POST",
        "path": name,
        "summary": port_type.get("documentation", ""),
        "security": [],
        "soap": {
            "operation": name,
            "service": service,
            "port": port["name"],
            "version": version,
            "endpoint": endpoint,
            "headers": headers,
            "envelope": "\n".join(lines),
            "fields": fields,
            "warnings": warnings,
        },
    }


def read_wsdl(
    data: bytes | str,
    *,
    url: str = "",
    fetch: Callable[[str], bytes] | None = None,
) -> dict[str, Any]:
    """The operations a WSDL offers.

    ``fetch`` reads a document the WSDL imports. Without it (a pasted WSDL), the
    import is skipped and a warning says so.
    """
    reader = _Reader(fetch)
    if url:
        reader.seen.add(url)
    reader.read_document(data, url)
    if not reader.saw_definitions:
        raise WsdlError("This is an XML schema, not a WSDL. A WSDL starts with a <definitions> element.")

    operations: list[dict[str, Any]] = []
    names: set[str] = set()
    limit_reached = False
    # A service often offers the same operations over SOAP 1.1 and 1.2. Show each once.
    ports = sorted(
        ((service, port) for service in reader.services for port in service["ports"]),
        key=lambda item: (reader.bindings.get(item[1]["binding"]) or {}).get("version", "9"),
    )
    for service, port in ports:
        binding = reader.bindings.get(port["binding"])
        if binding is None:
            continue
        port_type = reader.port_types.get(binding["type"]) or {}
        for name, binding_operation in binding["operations"].items():
            if name in names:
                continue
            if len(operations) >= MAX_OPERATIONS:
                limit_reached = True
                break
            names.add(name)
            operations.append(_operation(
                reader, name, port_type.get(name) or {}, binding_operation, binding["version"],
                port, service["name"], (binding["type"] or ("", ""))[0], url,
            ))

    if not operations:
        raise WsdlError("This WSDL has no SOAP operations with an address to call.")
    operations.sort(key=lambda operation: operation["path"].lower())
    return {
        "kind": "soap",
        "base_url": "",
        "service": ports[0][0]["name"],
        "operations": operations,
        "limit": MAX_OPERATIONS,
        "limit_reached": limit_reached,
        "warnings": reader.warnings,
    }
