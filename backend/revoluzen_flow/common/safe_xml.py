"""Reading XML that comes from outside the product.

API replies, WSDL documents and uploaded files are all untrusted. The standard
parser expands entities a document declares for itself, which is how a few
hundred bytes of "billion laughs" use up the server's memory. This reader is
built on expat directly and refuses any document that declares an entity, so
nothing is expanded and nothing outside the document is ever fetched. A plain
DOCTYPE with no entities is harmless and still reads.

Everything that turns XML into rows lives here too, so a file, an HTTP Request
step, a connector and the API Explorer agree on what a row is:

* A row path says which element is one row: ``//order``, ``/orders/order``, or
  just ``order``. Namespace prefixes are ignored, because a namespace URL is
  rarely what someone means when they ask for "the order elements".
* Attributes and child elements become columns. A nested element becomes
  ``parent_child``; a repeated one stays together as JSON text in one column.
"""

from __future__ import annotations

import json
import re
from typing import Any
from xml.etree import ElementTree as ET
from xml.parsers import expat
from xml.sax.saxutils import escape

__all__ = [
    "MAX_DEPTH",
    "SoapFaultError",
    "UnsafeXmlError",
    "XmlError",
    "XmlReply",
    "detect_row_path",
    "element_to_row",
    "find_elements",
    "local_name",
    "looks_like_xml",
    "parse_xml",
    "parse_xml_file",
    "pretty_xml",
    "raise_for_soap_fault",
    "render_xml_template",
    "row_path_candidates",
    "rows_from_xml",
    "soap_fault",
    "xml_placeholders",
    "xml_structure",
    "xml_to_data",
]

# Deep enough for any real document; shallow enough that the recursive helpers
# below, and the standard serializer, cannot run out of stack.
MAX_DEPTH = 200


class XmlError(ValueError):
    """The text cannot be read as XML. The message is safe to show to a person."""


class UnsafeXmlError(XmlError):
    """The document tries something this reader refuses to do."""


class SoapFaultError(ValueError):
    """A SOAP service answered with a fault instead of a result."""

    def __init__(self, fault: dict[str, str]):
        self.fault = fault
        code = f" ({fault['code']})" if fault.get("code") else ""
        super().__init__(f"The service reported a fault{code}: {fault.get('message') or 'no message given'}")


def _refuse_entities(*_args: Any) -> None:
    raise UnsafeXmlError(
        "This XML declares its own entities. Flow refuses them because they can be used to attack the server."
    )


class _Reader:
    """One parse: expat feeding an ElementTree builder."""

    def __init__(self, encoding: str | None, keep_prefixes: bool, keep_comments: bool):
        self._target = ET.TreeBuilder(insert_comments=keep_comments)
        self._keep_prefixes = keep_prefixes
        self._depth = 0
        parser = expat.ParserCreate(encoding, None if keep_prefixes else "}")
        parser.buffer_text = True
        parser.ordered_attributes = True
        parser.specified_attributes = True
        parser.StartElementHandler = self._start
        parser.EndElementHandler = self._end
        parser.CharacterDataHandler = self._target.data
        if keep_comments:
            parser.CommentHandler = self._target.comment
        # Declaring an entity is refused outright, so expansion never starts.
        parser.EntityDeclHandler = _refuse_entities
        parser.UnparsedEntityDeclHandler = _refuse_entities
        parser.ExternalEntityRefHandler = _refuse_entities
        parser.SkippedEntityHandler = _refuse_entities
        self.parser = parser

    def _name(self, name: str) -> str:
        # expat writes a namespaced name as ``uri}local``; ElementTree wants ``{uri}local``.
        return "{" + name if not self._keep_prefixes and "}" in name else name

    def _start(self, name: str, attributes: list[str]) -> None:
        self._depth += 1
        if self._depth > MAX_DEPTH:
            raise UnsafeXmlError(f"This XML is nested more than {MAX_DEPTH} levels deep.")
        attrib = {self._name(attributes[i]): attributes[i + 1] for i in range(0, len(attributes), 2)}
        self._target.start(self._name(name), attrib)

    def _end(self, name: str) -> None:
        self._depth -= 1
        self._target.end(self._name(name))

    def finish(self) -> ET.Element:
        return self._target.close()


def _not_well_formed(exc: expat.ExpatError) -> XmlError:
    return XmlError(f"This is not well-formed XML: {expat.ErrorString(exc.code)} at line {exc.lineno}.")


def parse_xml(data: bytes | str, *, keep_prefixes: bool = False, keep_comments: bool = False) -> ET.Element:
    """Parse untrusted XML.

    Elements are named ``{namespace}local``, as ElementTree normally names them.
    ``keep_prefixes`` keeps names as written (``soap:Envelope``) and the ``xmlns``
    attributes, for showing a document the way it arrived.
    """
    encoding = None
    if isinstance(data, str):
        # The text is already decoded, so whatever its declaration says about
        # the bytes it was once stored in no longer applies.
        encoding = "utf-8"
        data = data.encode("utf-8", errors="replace")
    reader = _Reader(encoding, keep_prefixes, keep_comments)
    try:
        reader.parser.Parse(data, True)
    except expat.ExpatError as exc:
        raise _not_well_formed(exc) from None
    return reader.finish()


def parse_xml_file(path: str) -> ET.Element:
    """Parse an XML file with the same protections as :func:`parse_xml`."""
    reader = _Reader(None, False, False)
    try:
        with open(path, "rb") as handle:
            reader.parser.ParseFile(handle)
    except expat.ExpatError as exc:
        raise _not_well_formed(exc) from None
    return reader.finish()


def local_name(tag: Any) -> str:
    """``{uri}order`` or ``ns:order`` -> ``order``. Comments have no name."""
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1].rsplit(":", 1)[-1]


_XML_START = re.compile(rb"^\s*<(\?xml|[A-Za-z_][\w.\-:]*[\s/>])")


def looks_like_xml(raw: bytes) -> bool:
    """A cheap check of how a reply starts, for when its Content-Type says nothing useful."""
    head = raw[:512].lstrip(b"\xef\xbb\xbf")
    return bool(_XML_START.match(head))


# ── Row paths ───────────────────────────────────────────────────────────────

_PATH_HELP = (
    "A row path names the element that is one row, such as //order or /orders/order, "
    "with an optional condition like //order[@status='open']."
)
_NAME_CHARS = r"[^\s/\[\]@()='\"!]"
_ELEMENT_NAME = re.compile(rf"^{_NAME_CHARS}+$")
_CHILD_CONDITION = re.compile(rf"^\s*([^\s/\[\]@()='\"!.0-9]{_NAME_CHARS}*)\s*(!?=.*)?$", re.S)
_PREDICATE = re.compile(r"\[((?:[^\]'\"]|'[^']*'|\"[^\"]*\")*)\]")


def _split_steps(path: str) -> list[tuple[str, str]]:
    """``a//b[c='d/e']/f`` -> ``[("", "a"), ("//", "b[c='d/e']"), ("/", "f")]``."""
    steps: list[tuple[str, str]] = []
    current: list[str] = []
    separator = ""
    quote = ""
    depth = 0
    index = 0
    while index < len(path):
        char = path[index]
        if quote:
            quote = "" if char == quote else quote
        elif char in "'\"":
            quote = char
        elif char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
        elif char == "/" and depth == 0:
            steps.append((separator, "".join(current)))
            current = []
            separator = "//" if path[index + 1 : index + 2] == "/" else "/"
            index += len(separator)
            continue
        current.append(char)
        index += 1
    steps.append((separator, "".join(current)))
    return steps


def _predicate(condition: str) -> str:
    match = _CHILD_CONDITION.match(condition)
    if match and condition.strip() != "last()":
        return "{*}" + match.group(1).rsplit(":", 1)[-1] + (match.group(2) or "")
    return condition


def _translate_step(step: str) -> str:
    name, bracket, rest = step.strip().partition("[")
    name = name.strip()
    if name in ("*", ".", ".."):
        tag = name
    elif name.startswith("{") and "}" in name:
        tag = name
    elif name and not name.endswith("()") and _ELEMENT_NAME.match(name):
        tag = "{*}" + name.rsplit(":", 1)[-1]
    else:
        raise XmlError(_PATH_HELP)
    conditions = _PREDICATE.findall(bracket + rest)
    if "".join(f"[{condition}]" for condition in conditions) != (bracket + rest).strip():
        raise XmlError(_PATH_HELP)
    return tag + "".join(f"[{_predicate(condition)}]" for condition in conditions)


def _etree_path(row_path: str) -> str:
    path = row_path.strip()
    if path.startswith("//"):
        prefix, path = ".//", path[2:]
    elif path.startswith("/"):
        prefix, path = "", path[1:]
    else:
        prefix = ".//"
    return prefix + "".join(separator + _translate_step(step) for separator, step in _split_steps(path))


def find_elements(root: ET.Element, row_path: str) -> list[ET.Element]:
    """Every element a row path selects, whatever namespace it is in."""
    expression = _etree_path(row_path)
    # A parent for the root, so that /orders and //orders can select the root itself.
    document = ET.Element("document")
    document.append(root)
    try:
        return document.findall(expression)
    except (SyntaxError, KeyError, ValueError, StopIteration):
        raise XmlError(_PATH_HELP) from None
    finally:
        document.remove(root)


def _soap_content(root: ET.Element) -> ET.Element:
    """Inside a SOAP envelope, what matters is the first thing in the body."""
    if local_name(root.tag) == "Envelope":
        for child in root:
            if local_name(child.tag) == "Body" and len(child):
                return child[0]
    return root


def row_path_candidates(root: ET.Element, limit: int = 30) -> list[dict[str, Any]]:
    """The element paths that could be a row, best guess first.

    Elements that repeat come first. Among those, ones that hold fields of their
    own beat bare values, and shallower ones beat deeper: in
    ``<orders><order><line/><line/></order>...``, a row is an order. A document
    with nothing repeating still has one record, so that is offered alone.
    """
    counts: dict[tuple[str, ...], int] = {}
    has_fields: dict[tuple[str, ...], bool] = {}

    content = _soap_content(root)
    start: list[str] = []
    if content is not root:
        start = [local_name(root.tag), "Body"]

    stack: list[tuple[ET.Element, tuple[str, ...]]] = [(content, (*start, local_name(content.tag)))]
    while stack:
        element, path = stack.pop()
        counts[path] = counts.get(path, 0) + 1
        has_fields[path] = has_fields.get(path, False) or len(element) > 0 or bool(_attributes(element))
        stack.extend((child, (*path, local_name(child.tag))) for child in element if isinstance(child.tag, str))

    repeating = [path for path, count in counts.items() if count > 1]
    repeating.sort(key=lambda path: (not has_fields[path], len(path), -counts[path], path))
    if repeating:
        return [{"path": "/" + "/".join(path), "count": counts[path]} for path in repeating[:limit]]

    # Nothing repeats, so this is a single record, possibly inside wrappers.
    element, path = content, (*start, local_name(content.tag))
    while len(element) == 1 and not _attributes(element) and len(element[0]) > 0:
        element = element[0]
        path = (*path, local_name(element.tag))
    return [{"path": "/" + "/".join(path), "count": 1}]


def detect_row_path(root: ET.Element) -> str:
    return row_path_candidates(root, limit=1)[0]["path"]


# ── Rows ────────────────────────────────────────────────────────────────────


def _attributes(element: ET.Element) -> dict[str, str]:
    return {local_name(key): value for key, value in element.attrib.items() if not key.startswith("xmlns")}


def _element_text(element: ET.Element) -> str:
    return (element.text or "").strip()


def _value(element: ET.Element) -> Any:
    """An element as plain data: a string, or a dict when it has inner structure."""
    attributes = _attributes(element)
    children = [child for child in element if isinstance(child.tag, str)]
    text = _element_text(element)
    if not children and not attributes:
        return text or None
    out: dict[str, Any] = {f"@{key}": value for key, value in attributes.items()}
    for child in children:
        name = local_name(child.tag)
        value = _value(child)
        if name not in out:
            out[name] = value
        elif isinstance(out[name], list):
            out[name].append(value)
        else:
            out[name] = [out[name], value]
    if text:
        out["#text"] = text
    return out


def _flatten_into(row: dict[str, Any], element: ET.Element, prefix: str) -> None:
    for key, value in _attributes(element).items():
        row[f"{prefix}{key}"] = value
    groups: dict[str, list[ET.Element]] = {}
    for child in element:
        if isinstance(child.tag, str):
            groups.setdefault(local_name(child.tag), []).append(child)
    for name, members in groups.items():
        column = f"{prefix}{name}"
        if len(members) > 1:
            row[column] = json.dumps([_value(member) for member in members], ensure_ascii=False)
        elif len(members[0]) == 0:
            row[column] = _element_text(members[0]) or None
            for key, value in _attributes(members[0]).items():
                row[f"{column}_{key}"] = value
        else:
            _flatten_into(row, members[0], f"{column}_")


def element_to_row(element: ET.Element) -> dict[str, Any]:
    """One row: attributes and child elements as columns."""
    row: dict[str, Any] = {}
    _flatten_into(row, element, "")
    text = _element_text(element)
    if text:
        row.setdefault("value", text)
    elif not row:
        row["value"] = None
    return row


def rows_from_xml(root: ET.Element, row_path: str = "") -> list[dict[str, Any]]:
    """The rows a path selects; with no path, the repeating element is found for you."""
    return [element_to_row(element) for element in find_elements(root, row_path.strip() or detect_row_path(root))]


def xml_to_data(root: ET.Element) -> dict[str, Any]:
    """The whole document as nested data, so JSON-style paths and views work on it."""
    return {local_name(root.tag): _value(root)}


class XmlReply(dict):
    """An XML reply: nested data for dotted paths, plus the document for row paths."""

    def __init__(self, root: ET.Element):
        super().__init__(xml_to_data(root))
        self.root = root

    def rows(self, row_path: str) -> list[dict[str, Any]]:
        return rows_from_xml(self.root, row_path)

    def text_of(self, name: str) -> str | None:
        """The text of the first element with this name, anywhere in the document."""
        try:
            found = find_elements(self.root, f"//{name}")
        except XmlError:
            return None
        text = (found[0].text or "").strip() if found else ""
        return text or None


def xml_structure(root: ET.Element, limit: int = 200) -> list[dict[str, Any]]:
    """Each distinct path in the document, in order, and how often it occurs."""
    entries: dict[str, dict[str, Any]] = {}

    def note(path: str, kind: str) -> None:
        if path in entries:
            entries[path]["count"] += 1
        elif len(entries) < limit:
            entries[path] = {"path": path, "type": kind, "count": 1}

    stack: list[tuple[ET.Element, str]] = [(root, "/" + local_name(root.tag))]
    while stack:
        element, path = stack.pop()
        has_children = any(isinstance(child.tag, str) for child in element)
        note(path, "element" if has_children else "value")
        for key in _attributes(element):
            note(f"{path}/@{key}", "attribute")
        children = [(child, f"{path}/{local_name(child.tag)}") for child in element if isinstance(child.tag, str)]
        stack.extend(reversed(children))
    return list(entries.values())


def pretty_xml(data: bytes | str, sensitive: re.Pattern[str] | None = None) -> str:
    """The document indented, with prefixes as written and sensitive values masked."""
    root = parse_xml(data, keep_prefixes=True)
    if sensitive is not None:
        for element in root.iter():
            for key in element.attrib:
                if not key.startswith("xmlns") and sensitive.search(local_name(key)):
                    element.attrib[key] = "[redacted]"
            if sensitive.search(local_name(element.tag)):
                tail = element.tail
                element.clear()
                element.text = "[redacted]"
                element.tail = tail
    ET.indent(root, space="  ")
    return ET.tostring(root, encoding="unicode")


# ── SOAP faults ─────────────────────────────────────────────────────────────


def soap_fault(root: ET.Element) -> dict[str, str] | None:
    """The fault in a SOAP reply, or None. Handles SOAP 1.1 and 1.2 wording."""
    if local_name(root.tag) != "Envelope":
        return None
    body = next((child for child in root if local_name(child.tag) == "Body"), None)
    fault = None if body is None else next((child for child in body if local_name(child.tag) == "Fault"), None)
    if fault is None:
        return None

    def text_at(*names: str) -> str:
        element: ET.Element | None = fault
        for name in names:
            element = None if element is None else next((c for c in element if local_name(c.tag) == name), None)
        return "" if element is None else " ".join("".join(element.itertext()).split())

    return {
        "code": text_at("faultcode") or text_at("Code", "Value"),
        "message": (text_at("faultstring") or text_at("Reason", "Text"))[:1000],
        "detail": (text_at("detail") or text_at("Detail"))[:1000],
    }


def raise_for_soap_fault(root: ET.Element) -> None:
    fault = soap_fault(root)
    if fault is not None:
        raise SoapFaultError(fault)


# ── Request bodies ──────────────────────────────────────────────────────────

_PLACEHOLDER = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


def xml_placeholders(template: str) -> list[str]:
    """The ``{name}`` placeholders in a body, in order, each once."""
    return list(dict.fromkeys(match.group(1) for match in _PLACEHOLDER.finditer(template or "")))


def _is_empty(value: Any) -> bool:
    return value is None or value == "" or (isinstance(value, float) and value != value)


def _fill(text: str | None, values: dict[str, Any], clean=lambda value: value) -> str | None:
    if not text:
        return text

    def swap(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in values:
            return match.group(0)
        return "" if _is_empty(values[name]) else clean(str(values[name]))

    return _PLACEHOLDER.sub(swap, text)


def _is_blank(text: str | None, values: dict[str, Any]) -> bool:
    """True when this text is nothing but placeholders that have no value."""
    remaining = _PLACEHOLDER.sub(
        lambda match: "" if match.group(1) in values and _is_empty(values[match.group(1)]) else match.group(0),
        text or "",
    )
    return not remaining.strip()


def _entirely_blank(element: ET.Element, values: dict[str, Any]) -> bool:
    """True when nothing in this element, however deep, has a value."""
    attributes = [value for key, value in element.attrib.items() if not key.startswith("xmlns")]
    return (
        _is_blank(element.text, values)
        and all(_is_blank(value, values) for value in attributes)
        and all(_entirely_blank(child, values) and _is_blank(child.tail, values) for child in element)
    )


def _drop_blank_optionals(parent: ET.Element, values: dict[str, Any]) -> None:
    optional = False
    for child in list(parent):
        if not isinstance(child.tag, str):
            optional = (child.text or "").strip().rstrip(":").lower() == "optional"
            parent.remove(child)
            continue
        _drop_blank_optionals(child, values)
        if optional and _entirely_blank(child, values):
            parent.remove(child)
        optional = False


def _fill_tree(root: ET.Element, values: dict[str, Any]) -> None:
    for element in root.iter():
        element.text = _fill(element.text, values)
        element.tail = _fill(element.tail, values)
        for key, value in list(element.attrib.items()):
            if key.startswith("xmlns"):
                continue
            if _PLACEHOLDER.fullmatch(value.strip()) and _is_blank(value, values):
                del element.attrib[key]
            else:
                element.attrib[key] = _fill(value, values) or ""


def _escape_value(value: str) -> str:
    return escape(value, {'"': "&quot;", "'": "&apos;"})


def render_xml_template(template: str, values: dict[str, Any]) -> str:
    """Put values into an XML body whose placeholders look like ``{name}``.

    Values are always escaped, so a value can never add elements of its own. An
    element marked ``<!--Optional:-->`` (the mark a WSDL import writes, and one
    people know from other SOAP tools) is left out when it has no value, because
    many services reject an empty element where a date or number belongs. A
    ``{name}`` with no matching value is ordinary text and stays as written.
    """
    try:
        root = parse_xml(template, keep_prefixes=True, keep_comments=True)
    except XmlError:
        # A fragment, or a body the author is still writing. Fill it in as text.
        return _fill(template, values, _escape_value) or ""
    if not isinstance(root.tag, str):
        return template
    _drop_blank_optionals(root, values)
    _fill_tree(root, values)
    return ET.tostring(root, encoding="unicode")
