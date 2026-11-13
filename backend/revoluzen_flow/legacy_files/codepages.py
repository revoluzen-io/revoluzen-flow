"""Character sets for mainframe and older files.

EBCDIC is the character set of IBM mainframes (z/OS, z/VSE) and IBM i. A file
copied from one in binary holds EBCDIC bytes, which read as nonsense unless the
right code page is used. Python ships most of the common ones. Code page 1047
(used by z/OS UNIX and C programs) is not shipped, so it is built here from
code page 037: the two differ in six positions only (the square brackets, the
caret, the not sign, Y-acute and the diaeresis).

Code pages here are single-byte, so one byte is one character and a column's
start and length mean the same thing in bytes and in characters.
"""

from __future__ import annotations

import codecs
from typing import Callable

# value, label shown on screen. Order = most common first.
EBCDIC_CODE_PAGES: list[tuple[str, str]] = [
    ("cp037", "EBCDIC 037 (US, Canada)"),
    ("cp1140", "EBCDIC 1140 (US, Canada, with euro sign)"),
    ("cp1047", "EBCDIC 1047 (z/OS UNIX, Latin-1)"),
    ("cp500", "EBCDIC 500 (International)"),
    ("cp273", "EBCDIC 273 (Germany, Austria)"),
    ("cp875", "EBCDIC 875 (Greek)"),
    ("cp1026", "EBCDIC 1026 (Turkish)"),
    ("cp424", "EBCDIC 424 (Hebrew)"),
]

# Text code pages that are not EBCDIC, for files already turned into ASCII.
ASCII_CODE_PAGES: list[tuple[str, str]] = [
    ("latin-1", "ASCII / Latin-1 (already converted)"),
    ("cp1252", "Windows-1252"),
    ("utf-8", "UTF-8"),
]

_EBCDIC = {value for value, _ in EBCDIC_CODE_PAGES}

# Names people type for the same code page.
_ALIASES = {
    "037": "cp037", "ibm037": "cp037", "ibm_037": "cp037", "ebcdic_cp_us": "cp037",
    "1140": "cp1140", "ibm1140": "cp1140", "ibm_1140": "cp1140",
    "1047": "cp1047", "ibm1047": "cp1047", "ibm_1047": "cp1047",
    "500": "cp500", "ibm500": "cp500", "ibm_500": "cp500",
    "273": "cp273", "ibm273": "cp273", "ibm_273": "cp273",
    "875": "cp875", "ibm875": "cp875", "1026": "cp1026", "ibm1026": "cp1026",
    "424": "cp424", "ibm424": "cp424",
    "latin1": "latin-1", "latin_1": "latin-1", "iso_8859_1": "latin-1", "iso-8859-1": "latin-1",
    "ascii": "latin-1", "us_ascii": "latin-1",
    "utf8": "utf-8", "utf_8": "utf-8",
}

# cp1047 = cp037 with these bytes changed.
_CP1047_CHANGES = {
    0x5F: "^",  # ^   (cp037 has the not sign here)
    0xB0: "¬",  # not sign (cp037 has ^ here)
    0xAD: "[",  # [   (cp037: Y-acute)
    0xBA: "Ý",  # Y-acute (cp037: [)
    0xBB: "¨",  # diaeresis (cp037: ])
    0xBD: "]",  # ]   (cp037: diaeresis)
}


def _build_cp1047() -> codecs.CodecInfo:
    chars = list(bytes(range(256)).decode("cp037"))
    for position, char in _CP1047_CHANGES.items():
        chars[position] = char
    decoding_table = "".join(chars)
    encoding_table = codecs.charmap_build(decoding_table)

    def encode(text: str, errors: str = "strict"):
        return codecs.charmap_encode(text, errors, encoding_table)

    def decode(data: bytes, errors: str = "strict"):
        return codecs.charmap_decode(data, errors, decoding_table)

    class _Incremental(codecs.IncrementalDecoder):
        def decode(self, data, final=False):  # noqa: D401 - codec protocol
            return codecs.charmap_decode(data, self.errors, decoding_table)[0]

    class _IncrementalEncoder(codecs.IncrementalEncoder):
        def encode(self, text, final=False):  # noqa: D401 - codec protocol
            return codecs.charmap_encode(text, self.errors, encoding_table)[0]

    return codecs.CodecInfo(
        name="cp1047",
        encode=encode,
        decode=decode,
        incrementalencoder=_IncrementalEncoder,
        incrementaldecoder=_Incremental,
    )


_CP1047: codecs.CodecInfo | None = None


def _search(name: str) -> codecs.CodecInfo | None:
    global _CP1047
    if name.lower().replace("-", "_") in ("cp1047", "ibm1047", "ibm_1047"):
        if _CP1047 is None:
            _CP1047 = _build_cp1047()
        return _CP1047
    return None


_registered = False


def register_codecs() -> None:
    """Make ``bytes.decode("cp1047")`` work in this process. Safe to call often."""
    global _registered
    if _registered:
        return
    try:
        codecs.lookup("cp1047")
    except LookupError:
        codecs.register(_search)
    _registered = True


def normalise(code_page: str | None, default: str = "cp037") -> str:
    """The code page's canonical name, or a clear error for an unknown one."""
    raw = (code_page or "").strip()
    if not raw:
        return default
    key = raw.lower().replace(" ", "_").replace("-", "_")
    name = _ALIASES.get(key, raw.lower())
    register_codecs()
    try:
        codecs.lookup(name)
    except LookupError:
        known = ", ".join(value for value, _ in EBCDIC_CODE_PAGES + ASCII_CODE_PAGES)
        raise ValueError(f"Unknown code page '{raw}'. Choose one of: {known}.") from None
    return name


def is_ebcdic(code_page: str | None) -> bool:
    try:
        return normalise(code_page, default="") in _EBCDIC
    except ValueError:
        return False


def single_byte(code_page: str) -> bool:
    """True when one byte is always one character."""
    name = normalise(code_page)
    return name in _EBCDIC or name in ("latin-1", "cp1252")


def decoder(code_page: str) -> Callable[[bytes], str]:
    """A fast bytes -> text function for the code page. Bytes a code page has
    no character for become U+FFFD instead of stopping the read."""
    name = normalise(code_page)
    info = codecs.lookup(name)
    decode = info.decode

    def _decode(data: bytes) -> str:
        return decode(data, "replace")[0]

    return _decode


def space_byte(code_page: str) -> int:
    """The byte a space is written as: 0x40 in EBCDIC, 0x20 elsewhere."""
    return 0x40 if is_ebcdic(code_page) else 0x20
