"""Mainframe and older file formats in the File step.

Covers EBCDIC code pages, record splitting (lines, fixed, RDW, BDW), the value
decoders (packed and zoned decimal, binary, IBM floating point), fixed-width
text, COBOL copybooks (OCCURS, OCCURS DEPENDING ON, REDEFINES with rules,
several 01 records) and the File step reading all of it. The sample records
are built here byte by byte, so every expected value is known.
"""
from __future__ import annotations

import io
from decimal import Decimal

import duckdb
import pytest

from revoluzen_flow.legacy_files import codepages, values
from revoluzen_flow.legacy_files.copybook import CopybookError, Options, compile_copybook, read_copybook_file
from revoluzen_flow.legacy_files.fixed_width import parse_columns, read_fixed_width
from revoluzen_flow.legacy_files.records import RecordError, iter_records
from revoluzen_flow.nodes.base import ExecutionContext
from revoluzen_flow.nodes.file_node import FileSourceNode


# ── helpers that write mainframe bytes ──

def packed(value: int, digits: int) -> bytes:
    nibbles = str(abs(value)).rjust(digits, "0") + ("d" if value < 0 else "c")
    if len(nibbles) % 2:
        nibbles = "0" + nibbles
    return bytes.fromhex(nibbles)


def zoned(value: int, digits: int) -> bytes:
    text = str(abs(value)).rjust(digits, "0")
    out = bytearray(0xF0 | int(c) for c in text)
    out[-1] = (0xD0 if value < 0 else 0xC0) | int(text[-1])
    return bytes(out)


def ebc(text: str, length: int, code_page: str = "cp037") -> bytes:
    return text.ljust(length).encode(code_page)


def _ctx(tmp_path, full_run=True, app_state=None):
    return ExecutionContext(conn=duckdb.connect(), data_dir=str(tmp_path), full_run=full_run, app_state=app_state or {})


# ── code pages ──

def test_cp1047_differs_from_cp037_only_in_six_places():
    codepages.register_codecs()
    data = bytes(range(256))
    a, b = data.decode("cp037"), data.decode("cp1047")
    changed = [i for i in range(256) if a[i] != b[i]]
    assert changed == [0x5F, 0xAD, 0xB0, 0xBA, 0xBB, 0xBD]
    assert bytes([0xAD, 0xBD, 0x5F]).decode("cp1047") == "[]^"
    assert "[x]".encode("cp1047") == bytes([0xAD, 0xA7, 0xBD])


def test_code_page_names_and_unknown_code_page():
    assert codepages.normalise("IBM-037") == "cp037"
    assert codepages.normalise("1047") == "cp1047"
    assert codepages.normalise("ascii") == "latin-1"
    assert codepages.is_ebcdic("cp273") and not codepages.is_ebcdic("utf-8")
    with pytest.raises(ValueError, match="Unknown code page 'cp9999'"):
        codepages.normalise("cp9999")


# ── records ──

def test_lines_in_ascii_and_ebcdic():
    ascii_records = list(iter_records(io.BytesIO(b"ab\r\ncd\nef"), "lines"))
    assert [(n, r) for n, _, r in ascii_records] == [(1, b"ab"), (2, b"cd"), (3, b"ef")]
    assert [o for _, o, _ in ascii_records] == [0, 4, 7]
    ebcdic = b"\xc1\xc2\x15\xc3\x25"  # AB NL C LF
    assert [r for _, _, r in iter_records(io.BytesIO(ebcdic), "lines", ebcdic=True)] == [b"\xc1\xc2", b"\xc3"]


def test_fixed_records_allow_trailing_padding_but_not_a_cut_record():
    data = b"AAAABBBB\r\n"
    assert [r for _, _, r in iter_records(io.BytesIO(data), "fixed", record_length=4)] == [b"AAAA", b"BBBB"]
    with pytest.raises(RecordError, match="partial record: 3 bytes"):
        list(iter_records(io.BytesIO(b"AAAABBB"), "fixed", record_length=4))


def test_rdw_and_bdw_records():
    def rdw(payload: bytes) -> bytes:
        return (len(payload) + 4).to_bytes(2, "big") + b"\x00\x00" + payload

    data = rdw(b"one") + rdw(b"three")
    assert [r for _, _, r in iter_records(io.BytesIO(data), "rdw")] == [b"one", b"three"]
    block = rdw(b"one") + rdw(b"three")
    blocked = (len(block) + 4).to_bytes(2, "big") + b"\x00\x00" + block
    assert [r for _, _, r in iter_records(io.BytesIO(blocked * 2), "bdw_rdw")] == [b"one", b"three"] * 2
    with pytest.raises(RecordError, match="valid length"):
        list(iter_records(io.BytesIO(b"\x00\x08\x01\x00abcd"), "rdw"))


def test_record_limit_stops_early():
    data = b"\n".join(b"x" for _ in range(100))
    assert len(list(iter_records(io.BytesIO(data), "lines", limit=5))) == 5


# ── values ──

def test_packed_decimal():
    assert values.packed(bytes.fromhex("12345c"), 2) == Decimal("123.45")
    assert values.packed(bytes.fromhex("12345d"), 2) == Decimal("-123.45")
    assert values.packed(bytes.fromhex("12345f")) == 12345
    assert values.packed(b"\x40\x40\x40") is None
    assert values.packed(b"\x00\x00") is None
    with pytest.raises(values.BadValue, match="not a packed decimal"):
        values.packed(bytes.fromhex("1a345c"))
    with pytest.raises(values.BadValue, match="unknown sign"):
        values.packed(bytes.fromhex("123459"))


def test_zoned_decimal_in_ebcdic():
    assert values.zoned_ebcdic(bytes.fromhex("f1f2c3"), 1) == Decimal("12.3")
    assert values.zoned_ebcdic(bytes.fromhex("f1f2d3")) == -123
    assert values.zoned_ebcdic(bytes.fromhex("d1f2f3"), sign_leading=True) == -123
    assert values.zoned_ebcdic(bytes.fromhex("60f1f2f3"), sign_leading=True, sign_separate=True) == -123
    assert values.zoned_ebcdic(bytes.fromhex("f1f2f34e"), sign_separate=True) == 123
    assert values.zoned_ebcdic(bytes.fromhex("4040f5")) == 5
    with pytest.raises(values.BadValue):
        values.zoned_ebcdic(bytes.fromhex("f1c1f3"))


def test_zoned_decimal_as_text_with_overpunch():
    assert values.zoned_text("000123{", 2) == Decimal("12.30")
    assert values.zoned_text("000012J") == -121
    assert values.zoned_text("12p", micro_focus=True) == -120  # Micro Focus negative zero
    with pytest.raises(values.BadValue):
        values.zoned_text("12x")  # only copybook records read p..y as signs
    assert values.zoned_text("-45") == -45
    assert values.zoned_text("   ") is None


def test_binary_and_floating_point():
    assert values.binary(b"\xff\xfe", signed=True) == -2
    assert values.binary(b"\xff\xfe", signed=False) == 65534
    assert values.binary(b"\x00\x00\x30\x39", 2) == Decimal("123.45")
    assert values.binary(b"\x39\x30\x00\x00", big_endian=False) == 12345
    assert values.ibm_float(bytes.fromhex("41100000")) == 1.0
    assert values.ibm_float(bytes.fromhex("c276a000")) == -118.625
    assert values.ibm_float(bytes.fromhex("4110000000000000")) == 1.0


def test_decimal_text_and_dates():
    assert values.decimal_text("1,234.50", 2) == Decimal("1234.50")
    assert values.decimal_text("12.50-", 2) == Decimal("-12.50")
    assert values.decimal_text("(3.5)", 2) == Decimal("-3.50")
    assert values.decimal_text("1.234,5", 2, decimal_separator=",") == Decimal("1234.50")
    with pytest.raises(values.BadValue, match="more than 2 decimal places"):
        values.decimal_text("1.234", 2)
    fmt, has_time = values.date_pattern("YYYYMMDD")
    assert str(values.date_text("20261101", fmt, has_time)) == "2026-11-01"
    assert values.date_text("00000000", fmt, has_time) is None
    with pytest.raises(ValueError, match="Use YYYY"):
        values.date_pattern("YYYYQQ")


# ── fixed-width ──

def test_fixed_width_with_types_header_and_trailer(tmp_path):
    lines = [
        "HDR 2026-11-01",
        "00042" + "ACME LTD".ljust(18) + "0000012345 " + "20261101",
        "00007" + "BETA".ljust(18) + "000000050J " + "20261102",
        "TRL 2",
    ]
    path = tmp_path / "accounts.txt"
    path.write_bytes(("\r\n".join(lines) + "\r\n").encode("latin-1"))
    rel, info = read_fixed_width(duckdb.connect(), str(path), {
        "fixed_columns": [
            {"name": "id", "start": 1, "length": 5, "type": "integer"},
            {"name": "name", "start": 6, "length": 18},
            {"name": "amount", "start": 24, "length": 11, "type": "implied", "decimals": 2},
            {"name": "opened", "start": 35, "length": 8, "type": "date", "format": "YYYYMMDD"},
        ],
        "skip_lines_start": 1, "skip_lines_end": 1, "encoding": "latin-1",
    })
    assert [str(t) for t in rel.types] == ["BIGINT", "VARCHAR", "DECIMAL(11,2)", "DATE"]
    rows = rel.fetchall()
    assert rows[0][:3] == (42, "ACME LTD", Decimal("123.45"))
    assert rows[1][:3] == (7, "BETA", Decimal("-5.01"))
    assert str(rows[1][3]) == "2026-11-02"
    assert info["rows"] == 2


def test_fixed_width_from_widths_in_ebcdic_without_line_breaks(tmp_path):
    records = ebc("A1", 2) + ebc("SMITH", 6) + ebc("B2", 2) + ebc("JONES", 6)
    path = tmp_path / "people.dat"
    path.write_bytes(records)
    rel, _ = read_fixed_width(duckdb.connect(), str(path), {
        "fixed_widths": "2, 6", "fixed_names": "code, surname",
        "encoding": "cp037", "record_format": "fixed", "record_length": 8,
    })
    assert rel.columns == ["code", "surname"]
    assert rel.fetchall() == [("A1", "SMITH"), ("B2", "JONES")]


def test_fixed_width_bad_value_stops_or_is_left_empty(tmp_path):
    path = tmp_path / "bad.txt"
    path.write_text("12x\n345\n", encoding="ascii")
    params = {"fixed_columns": [{"name": "n", "start": 1, "length": 3, "type": "integer"}]}
    with pytest.raises(ValueError, match=r"line 1 \(byte 0\), column 'n' \(positions 1 to 3\): not a number"):
        read_fixed_width(duckdb.connect(), str(path), params)
    rel, info = read_fixed_width(duckdb.connect(), str(path), {**params, "bad_value_rule": "empty"})
    assert rel.fetchall() == [(None,), (345,)]
    assert info["bad_values"] == 1


def test_fixed_width_column_checks():
    with pytest.raises(ValueError, match="needs its columns"):
        parse_columns({})
    with pytest.raises(ValueError, match="starts at 0"):
        parse_columns({"fixed_columns": [{"name": "a", "start": 0, "length": 2}]})
    with pytest.raises(ValueError, match="unknown type 'money'"):
        parse_columns({"fixed_columns": [{"name": "a", "start": 1, "length": 2, "type": "money"}]})
    columns = parse_columns({"fixed_columns": [{"name": "a", "start": 1, "length": 2}, {"name": "a", "start": 3, "length": 2}]})
    assert [c.name for c in columns] == ["a", "a_2"]


# ── COBOL copybooks ──

CUSTOMER_COPYBOOK = """\
000100 01  CUSTOMER-RECORD.
000200     05  REC-TYPE            PIC X.
000300     05  CUST-ID             PIC 9(6).
000400     05  CUST-NAME           PIC X(20).
000500     05  BALANCE             PIC S9(7)V99 COMP-3.
000600     05  CREDIT-LIMIT        PIC S9(5)V99.
000700     05  ORDER-COUNT         PIC S9(4) COMP.
000800     05  MONTHLY-SALES       PIC S9(5)V99 COMP-3 OCCURS 3 TIMES.
000900     05  DETAILS.
001000         10  PAYMENT-INFO.
001100             15  PAY-AMOUNT  PIC S9(7)V99 COMP-3.
001200             15  PAY-DATE    PIC 9(8).
001300         10  REFUND-INFO REDEFINES PAYMENT-INFO.
001400             15  REFUND-REASON PIC X(13).
001500     05  STATUS-CODE         PIC X(2).
001600         88  STATUS-ACTIVE   VALUE 'AC'.
001700         88  STATUS-CLOSED   VALUE 'CL'.
"""
RULES = [
    {"branch": "PAYMENT-INFO", "field": "REC-TYPE", "values": "P"},
    {"branch": "REFUND-INFO", "field": "REC_TYPE", "values": "'R'"},
]


def customer_record(rec_type, cust_id, name, balance, limit, orders, sales, detail, status) -> bytes:
    out = ebc(rec_type, 1) + ebc(str(cust_id).rjust(6, "0"), 6) + ebc(name, 20)
    out += packed(balance, 9) + zoned(limit, 7) + orders.to_bytes(2, "big", signed=True)
    for amount in sales:
        out += packed(amount, 7)
    if rec_type == "P":
        out += packed(detail[0], 9) + ebc(detail[1], 8)
    else:
        out += ebc(detail, 13)
    out += ebc(status, 2)
    assert len(out) == 68
    return out


CUSTOMER_DATA = (
    customer_record("P", 42, "ACME LTD", 123456789, -150000, 7, [100, -250, 99999], (-1050, "20261101"), "AC")
    + customer_record("R", 7, "BETA & SONS", -5, 0, -2, [0, 0, 1], "DAMAGED GOODS", "CL")
)


def test_copybook_layout_and_typed_values():
    layout = compile_copybook(CUSTOMER_COPYBOOK, Options(code_page="cp037"), RULES)
    assert layout.record_length == 68
    assert [name for name, _ in layout.columns] == [
        "REC_TYPE", "CUST_ID", "CUST_NAME", "BALANCE", "CREDIT_LIMIT", "ORDER_COUNT",
        "MONTHLY_SALES_1", "MONTHLY_SALES_2", "MONTHLY_SALES_3", "PAY_AMOUNT", "PAY_DATE",
        "REFUND_REASON", "STATUS_CODE",
    ]
    rel, info = read_copybook_file(duckdb.connect(), io.BytesIO(CUSTOMER_DATA), CUSTOMER_COPYBOOK,
                                   {"code_page": "cp037", "redefines_rules": RULES})
    assert [str(t) for t in rel.types] == [
        "VARCHAR", "BIGINT", "VARCHAR", "DECIMAL(9,2)", "DECIMAL(7,2)", "BIGINT", "DECIMAL(7,2)",
        "DECIMAL(7,2)", "DECIMAL(7,2)", "DECIMAL(9,2)", "BIGINT", "VARCHAR", "VARCHAR",
    ]
    payment, refund = rel.fetchall()
    assert payment == ("P", 42, "ACME LTD", Decimal("1234567.89"), Decimal("-1500.00"), 7, Decimal("1.00"),
                       Decimal("-2.50"), Decimal("999.99"), Decimal("-10.50"), 20261101, None, "AC")
    assert refund == ("R", 7, "BETA & SONS", Decimal("-0.05"), Decimal("0.00"), -2, Decimal("0.00"),
                      Decimal("0.00"), Decimal("0.01"), None, None, "DAMAGED GOODS", "CL")
    assert info["rows"] == 2 and info["bad_values"] == 0


def test_copybook_without_a_rule_names_the_redefines():
    with pytest.raises(ValueError, match=r"record 2 \(byte 68\), field PAY-AMOUNT .*REDEFINES; add a rule"):
        read_copybook_file(duckdb.connect(), io.BytesIO(CUSTOMER_DATA), CUSTOMER_COPYBOOK, {"code_page": "cp037"})
    rel, info = read_copybook_file(duckdb.connect(), io.BytesIO(CUSTOMER_DATA), CUSTOMER_COPYBOOK,
                                   {"code_page": "cp037", "bad_value_rule": "empty"})
    rows = rel.fetchall()
    assert rows[1][9] is None and rows[1][11] == "DAMAGED GOODS"
    assert info["bad_values"] >= 1


def test_occurs_depending_on_with_variable_records():
    copybook = """
       01  ORDER-REC.
           05  ORDER-ID      PIC 9(4).
           05  LINE-COUNT    PIC 9(2).
           05  ORDER-LINE    OCCURS 1 TO 5 TIMES DEPENDING ON LINE-COUNT.
               10  SKU       PIC X(3).
               10  QTY       PIC S9(3) COMP-3.
           05  ORDER-TOTAL   PIC S9(5)V99 COMP-3.
    """

    def record(order_id, lines, total):
        body = ebc(str(order_id).rjust(4, "0"), 4) + ebc(str(len(lines)).rjust(2, "0"), 2)
        for sku, qty in lines:
            body += ebc(sku, 3) + packed(qty, 3)
        body += packed(total, 7)
        return (len(body) + 4).to_bytes(2, "big") + b"\x00\x00" + body

    data = record(1, [("AAA", 2)], 1999) + record(2, [("BBB", 1), ("CCC", -3), ("DDD", 4)], -50)
    rel, _ = read_copybook_file(duckdb.connect(), io.BytesIO(data), copybook,
                                {"code_page": "cp037", "record_format": "rdw"})
    columns = rel.columns
    assert columns[:4] == ["ORDER_ID", "LINE_COUNT", "SKU_1", "QTY_1"] and columns[-1] == "ORDER_TOTAL"
    first, second = (dict(zip(columns, row)) for row in rel.fetchall())
    assert first["SKU_1"] == "AAA" and first["QTY_1"] == 2 and first["SKU_2"] is None
    assert first["ORDER_TOTAL"] == Decimal("19.99")  # moved up: only one line was sent
    assert second["SKU_3"] == "DDD" and second["QTY_2"] == -3 and second["SKU_4"] is None
    assert second["ORDER_TOTAL"] == Decimal("-0.50")


def test_counts_before_two_variable_tables():
    copybook = """
       01  REC.
           05  COUNT-A   PIC 9.
           05  COUNT-B   PIC 9.
           05  A-ITEM    PIC X(2) OCCURS 0 TO 3 DEPENDING ON COUNT-A.
           05  B-ITEM    PIC X(2) OCCURS 0 TO 3 DEPENDING ON COUNT-B.
           05  TAIL      PIC X(2).
    """
    record = ebc("1", 1) + ebc("2", 1) + ebc("a1", 2) + ebc("b1", 2) + ebc("b2", 2) + ebc("ZZ", 2)
    layout = compile_copybook(copybook, Options())
    decoded = dict(zip([n for n, _ in layout.columns], layout.decode(record, 1, 0)))
    assert decoded["A_ITEM_1"] == "a1" and decoded["A_ITEM_2"] is None
    assert decoded["B_ITEM_1"] == "b1" and decoded["B_ITEM_2"] == "b2"
    assert decoded["TAIL"] == "ZZ"


def test_several_01_records_with_rules():
    copybook = """
       01  HEADER-REC.
           05  HDR-TYPE      PIC X.
           05  RUN-DATE      PIC 9(8).
       01  DETAIL-REC.
           05  DTL-TYPE      PIC X.
           05  AMOUNT        PIC S9(5)V99 COMP-3.
           05  FILLER        PIC X(5).
    """
    rules = [{"branch": "HEADER-REC", "field": "HDR-TYPE", "values": "H"},
             {"branch": "DETAIL-REC", "field": "HDR-TYPE", "values": "D"}]
    data = ebc("H", 1) + ebc("20261111", 8) + ebc("D", 1) + packed(-12345, 7) + ebc("", 4)
    rel, _ = read_copybook_file(duckdb.connect(), io.BytesIO(data), copybook,
                                {"code_page": "cp037", "redefines_rules": rules, "record_length": 9})
    assert rel.columns == ["HDR_TYPE", "RUN_DATE", "DTL_TYPE", "AMOUNT"]
    assert rel.fetchall() == [("H", 20261111, None, None), (None, None, "D", Decimal("-123.45"))]


def test_free_format_copybook_with_other_clauses():
    copybook = """
    *> a free-format copybook
    01 MISC-REC.
       05 CODE-FIELD PIC X(4) VALUE SPACES.
       05 RATE       USAGE COMP-1.
       05 BIG-RATE   COMPUTATIONAL-2.
       05 SIGNED-LEAD PIC S9(3) SIGN IS LEADING SEPARATE CHARACTER.
       05 SHOWN      PIC ZZ9.99-.
       05 WIDE-NAME  PIC N(2).
       05 FILLER     PIC X(2).
       05 CODES      PIC X OCCURS 2 TIMES INDEXED BY CODE-IDX.
    """
    record = (ebc("ABCD", 4) + bytes.fromhex("41100000") + bytes.fromhex("c276a00000000000")
              + bytes([0x60]) + bytes.fromhex("f0f4f2") + ebc(" 12.50-", 7) + "Hi".encode("utf-16-be")
              + ebc("", 2) + ebc("XY", 2))
    rel, _ = read_copybook_file(duckdb.connect(), io.BytesIO(record), copybook, {"code_page": "cp037"})
    assert rel.columns == ["CODE_FIELD", "RATE", "BIG_RATE", "SIGNED_LEAD", "SHOWN", "WIDE_NAME", "CODES_1", "CODES_2"]
    assert rel.fetchall() == [("ABCD", 1.0, -118.625, -42, Decimal("-12.50"), "Hi", "X", "Y")]


def test_same_field_names_are_qualified_by_their_group():
    copybook = """
       01  PARTY.
           05  BUYER.
               10  NAME   PIC X(3).
           05  SELLER.
               10  NAME   PIC X(3).
    """
    layout = compile_copybook(copybook)
    assert [n for n, _ in layout.columns] == ["BUYER_NAME", "SELLER_NAME"]


def test_ascii_converted_copybook_reads_overpunched_signs():
    copybook = "01 R.\n   05 AMT PIC S9(5)V99.\n   05 TXT PIC X(3)."
    data = b"000123}ABC\n001000AXYZ\n"
    rel, _ = read_copybook_file(duckdb.connect(), io.BytesIO(data), copybook,
                                {"code_page": "latin-1", "record_format": "lines"})
    assert rel.fetchall() == [(Decimal("-12.30"), "ABC"), (Decimal("100.01"), "XYZ")]


@pytest.mark.parametrize("copybook, message", [
    ("01 R.\n   05 A PIC X(2) SYNC.", "SYNCHRONIZED"),
    ("COPY OTHERBOOK.", "includes another"),
    ("01 R.\n   05 A PIC X(2) WIBBLE.", "line 2: did not understand 'WIBBLE'"),
    ("01 R.\n   05 A PIC X(2).\n   05 B REDEFINES Z PIC X(2).", "redefines Z"),
    ("01 R.\n   05 A PIC 9(2) OCCURS 1 TO 3 DEPENDING ON NOPE.", "depends on NOPE"),
    ("", "empty"),
    ("01 R.\n   05 A PIC X(2) USAGE POINTER.", "POINTER"),
    ("01 R.\n   05 A PIC X(2)\n   05 B PIC X(2).", "line 3: '05' looks like the level number .* period missing at the end of A"),
])
def test_copybook_errors_name_the_line(copybook, message):
    with pytest.raises(CopybookError, match=message):
        compile_copybook(copybook)


def test_fixed_format_ignores_the_identification_area():
    lines = ["000100 01  REC.", "000200     05  A   PIC X(2).", "000300     05  B   PIC 9(3)."]
    layout = compile_copybook("\n".join(line.ljust(72) + "CUSTREC1" for line in lines))
    assert [name for name, _ in layout.columns] == ["A", "B"]
    assert layout.record_length == 5


def test_unknown_redefines_rule_part_lists_the_parts():
    with pytest.raises(CopybookError, match="Parts in this copybook: PAYMENT-INFO, REFUND-INFO"):
        compile_copybook(CUSTOMER_COPYBOOK, rules=[{"branch": "NOPE", "field": "REC-TYPE", "values": "P"}])


def test_describe_for_the_settings_screen():
    described = compile_copybook(CUSTOMER_COPYBOOK).describe()
    assert described["record_length"] == 68
    balance = next(f for f in described["fields"] if f["column"] == "BALANCE")
    assert balance["start"] == 28 and balance["length"] == 5
    assert balance["type"] == "Number, signed packed decimal (COMP-3), 9 digits, 2 decimal places"
    assert described["redefines"] == [{"group": 0, "start": 54, "branches": ["PAYMENT-INFO", "REFUND-INFO"]}]
    assert "REC-TYPE" in described["selector_fields"]


# ── the File step ──

def test_file_step_reads_a_copybook_file_with_a_sample_limit(tmp_path):
    (tmp_path / "CUSTMAST.dat").write_bytes(CUSTOMER_DATA * 3)
    params = {"file_path": "CUSTMAST.dat", "format": "copybook", "copybook": CUSTOMER_COPYBOOK,
              "code_page": "cp037", "redefines_rules": RULES}
    full = FileSourceNode(params).execute(_ctx(tmp_path)).fetchall()
    assert len(full) == 6
    sample = FileSourceNode({**params, "sample_rows": 4}).execute(_ctx(tmp_path, full_run=False)).fetchall()
    assert len(sample) == 4


def test_file_step_reads_the_copybook_from_a_file_and_globs(tmp_path):
    (tmp_path / "layout.cpy").write_text(CUSTOMER_COPYBOOK, encoding="utf-8")
    (tmp_path / "in").mkdir()
    (tmp_path / "in" / "a.dat").write_bytes(CUSTOMER_DATA)
    (tmp_path / "in" / "b.dat").write_bytes(CUSTOMER_DATA[:68])
    rel = FileSourceNode({
        "file_path": str(tmp_path / "in" / "*.dat"), "format": "copybook", "copybook_path": "layout.cpy",
        "redefines_rules": RULES,
    }).execute(_ctx(tmp_path))
    assert len(rel.fetchall()) == 3


def test_file_step_reads_fixed_width(tmp_path):
    (tmp_path / "fw.txt").write_text("0001Alpha\n0002Beta \n", encoding="utf-8")
    rel = FileSourceNode({
        "file_path": "fw.txt", "format": "fixed_width",
        "fixed_columns": [{"name": "id", "start": 1, "length": 4, "type": "integer"},
                          {"name": "name", "start": 5, "length": 5}],
    }).execute(_ctx(tmp_path))
    assert rel.fetchall() == [(1, "Alpha"), (2, "Beta")]


def test_file_step_reads_ebcdic_csv_and_the_encoding_is_used(tmp_path):
    (tmp_path / "ebcdic.csv").write_bytes("id,city\n1,Köln\n2,[Zürich]\n".replace("\n", "\x85").encode("cp1047"))
    rel = FileSourceNode({"file_path": "ebcdic.csv", "encoding": "cp1047"}).execute(_ctx(tmp_path))
    assert rel.fetchall() == [(1, "Köln"), (2, "[Zürich]")]
    (tmp_path / "latin.csv").write_bytes("id,name\n1,Müller\n".encode("latin-1"))
    rel = FileSourceNode({"file_path": "latin.csv", "encoding": "latin-1"}).execute(_ctx(tmp_path))
    assert rel.fetchall() == [(1, "Müller")]


def test_file_step_refuses_an_encrypted_file_it_was_not_told_to_decrypt(tmp_path):
    (tmp_path / "orders.csv.pgp").write_bytes(b"\x85\x01\x0c")
    with pytest.raises(ValueError, match="is PGP-encrypted. Turn on 'Decrypt with PGP'"):
        FileSourceNode({"file_path": "orders.csv.pgp"}).execute(_ctx(tmp_path))


def test_dev_sample_limit_from_the_editor_form_is_used(tmp_path):
    (tmp_path / "many.csv").write_text("n\n" + "\n".join(str(i) for i in range(50)) + "\n", encoding="utf-8")
    rel = FileSourceNode({"file_path": "many.csv", "sample_rows": 1000, "dev_sample_rows": 7}).execute(
        _ctx(tmp_path, full_run=False))
    assert len(rel.fetchall()) == 7


def test_file_step_reads_over_an_sftp_connection(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from revoluzen_flow.nodes import _remote_files

    fetched = {}

    def fake_download(config, conn_type, path, target):
        fetched.update(config=config, conn_type=conn_type, path=_remote_files.remote_path(config, path))
        with open(target, "wb") as out:
            out.write(CUSTOMER_DATA)
        return fetched["path"]

    class Connections:
        def get(self, connection_id, workspace_id=None):
            return SimpleNamespace(config={"host": "mainframe.example", "protocol": "sftp", "remote_path": "/outbound"},
                                   type="ftp", credential_id=None)

    monkeypatch.setattr(_remote_files, "download", fake_download)
    ctx = _ctx(tmp_path, app_state={"connection_store": Connections()})
    rel = FileSourceNode({"connection_id": "c1", "file_path": "CUSTMAST.dat", "format": "copybook",
                          "copybook": CUSTOMER_COPYBOOK, "redefines_rules": RULES}).execute(ctx)
    assert len(rel.fetchall()) == 2
    assert fetched["path"] == "/outbound/CUSTMAST.dat" and fetched["conn_type"] == "ftp"
    assert _remote_files.protocol_of(fetched["config"], "ftp") == "sftp"
    assert _remote_files.remote_path({"remote_path": "/in"}, "/abs/x.dat") == "/abs/x.dat"


def test_generic_source_and_destination_reach_the_file_step(tmp_path):
    from revoluzen_flow.ir.schema import StepType
    from revoluzen_flow.nodes.generic import DEST_MAP, SOURCE_MAP, GenericSourceNode

    assert SOURCE_MAP["file"] == StepType.FILE_SOURCE and DEST_MAP["file"] == StepType.FILE_SINK
    (tmp_path / "fw.txt").write_text("0001Alpha\n0002Beta \n", encoding="utf-8")
    rel = GenericSourceNode({"connector_type": "file", "file_path": "fw.txt", "format": "fixed_width",
                             "fixed_widths": "4, 5", "fixed_names": "id, name"}).execute(_ctx(tmp_path))
    assert rel.fetchall() == [("0001", "Alpha"), ("0002", "Beta")]


def test_validator_asks_for_what_the_file_step_needs():
    from revoluzen_flow.ir.schema import Step, StepType, Workflow
    from revoluzen_flow.ir.validator import validate_workflow

    def messages(step_type, params):
        return [e.message for e in validate_workflow(Workflow(steps=[Step(id="s1", type=step_type, params=params)]))]

    found = messages(StepType.SOURCE, {"connector_type": "file", "file_path": "x.dat", "format": "copybook"})
    assert "COBOL copybook records need the copybook: paste it, or give its file." in found
    assert not any("connection_id" in m for m in found), "a local file needs no saved connection"
    found = messages(StepType.FILE_SOURCE, {"file_path": "x.txt", "format": "fixed_width", "pgp_decrypt": True})
    assert "Fixed-width text needs its columns: start and length, or a list of widths." in found
    assert "Choose the PGP key credential, or turn PGP off." in found
    found = messages(StepType.DESTINATION, {"connector_type": "file", "file_path": "o.csv", "pgp_encrypt": True})
    assert "Choose the PGP key credential, or turn PGP off." in found
    assert messages(StepType.SOURCE, {"connector_type": "file", "file_path": "x.dat", "format": "copybook",
                                      "copybook": "01 R.\n 05 A PIC X."}) == []


def test_sql_preview_does_not_pretend_to_read_mainframe_formats():
    from revoluzen_flow.compile.sql_preview import _compile_source

    step = {"type": "file_source", "params": {"file_path": "x.dat", "format": "copybook"}}
    assert _compile_source(step, None) is None
    step = {"type": "file_source", "params": {"file_path": "x.csv.pgp", "pgp_decrypt": True}}
    assert _compile_source(step, None) is None
