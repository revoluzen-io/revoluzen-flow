"""A finished report as email, chat text, PDF and Word.

All of these read the same blocks the page shows, so nothing is worked out
twice. The HTML uses nested tables and inline styles, the only layout that
survives Outlook and Gmail. Cards stay neutral; colour is only in the value.
"""

from __future__ import annotations

import io
from html import escape
from typing import Any
from xml.sax.saxutils import escape as xml_escape

_TONE_COLOR = {
    "good": "#059669", "bad": "#e11d48", "warn": "#d97706",
    "primary": "#2563eb", "neutral": "#1e293b", "": "#1e293b",
}
_BAR_COLOR = {"good": "#34d399", "bad": "#fb7185", "neutral": "#cbd5e1"}
_FONT = "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"


def _cell_text(value: Any) -> str:
    return str(value.get("text", "")) if isinstance(value, dict) else str(value)


def _cell_tone(value: Any) -> str:
    return str(value.get("tone", "")) if isinstance(value, dict) else ""


def _chunks(items: list, size: int) -> list[list]:
    return [items[i:i + size] for i in range(0, len(items), size)]


def _shown_note(block: dict) -> str:
    shown, total = len(block["rows"]), block.get("total", len(block["rows"]))
    return f"Showing {shown:,} of {total:,}." if total > shown else ""


def report_subject(result: dict) -> str:
    return f"{result['name']}: {result['period']['label']}"


# ── Email ───────────────────────────────────────────────────────────────


def _html_kpis(block: dict) -> str:
    rows = []
    for chunk in _chunks(block["items"], 3):
        cells = []
        for item in chunk:
            color = _TONE_COLOR.get(item.get("tone", ""), _TONE_COLOR[""])
            detail = (
                f"<div style='font-size:11px;color:#94a3b8;margin-top:3px'>{escape(item['detail'])}</div>"
                if item.get("detail") else ""
            )
            cells.append(
                "<td width='33%' style='padding:5px;vertical-align:top'>"
                "<div style='border:1px solid #e2e8f0;border-radius:8px;padding:11px 13px;background:#ffffff'>"
                f"<div style='font-size:11px;font-weight:600;color:#64748b'>{escape(item['label'])}</div>"
                f"<div style='font-size:21px;font-weight:700;color:{color};margin-top:3px'>{escape(item['value'])}</div>"
                f"{detail}</div></td>"
            )
        cells.extend("<td width='33%'></td>" for _ in range(3 - len(chunk)))
        rows.append("<tr>" + "".join(cells) + "</tr>")
    return "<table cellpadding='0' cellspacing='0' width='100%' style='border-collapse:collapse'>" + "".join(rows) + "</table>"


def _html_table(block: dict) -> str:
    if not block["rows"]:
        return f"<p style='font-size:13px;color:#64748b;margin:6px 0'>{escape(block['empty'])}</p>"
    head = "".join(
        f"<th align='{c['align']}' style='padding:8px 10px;background:#f1f5f9;color:#64748b;"
        "font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.05em;"
        f"border-bottom:2px solid #e2e8f0'>{escape(c['label'])}</th>"
        for c in block["columns"]
    )
    body = []
    for row in block["rows"]:
        cells = []
        for column, value in zip(block["columns"], row):
            tone = _cell_tone(value)
            weight = "font-weight:600;" if tone else ""
            cells.append(
                f"<td align='{column['align']}' style='padding:8px 10px;font-size:12.5px;{weight}"
                f"color:{_TONE_COLOR.get(tone, _TONE_COLOR[''])};border-bottom:1px solid #f1f5f9;"
                f"word-break:break-word'>{escape(_cell_text(value))}</td>"
            )
        body.append("<tr>" + "".join(cells) + "</tr>")
    note = _shown_note(block)
    tail = f"<p style='font-size:11px;color:#94a3b8;margin:6px 0 0'>{note}</p>" if note else ""
    return (
        "<table cellpadding='0' cellspacing='0' width='100%' style='border-collapse:collapse;"
        f"border:1px solid #e2e8f0;background:#ffffff'><tr>{head}</tr>{''.join(body)}</table>{tail}"
    )


def _html_bars(block: dict) -> str:
    if not block.get("has_data"):
        return f"<p style='font-size:13px;color:#64748b;margin:6px 0'>{escape(block['empty'])}</p>"
    biggest = max(sum(r["values"].values()) for r in block["rows"]) or 1
    rows = []
    for row in block["rows"]:
        total = sum(row["values"].values())
        parts = []
        for series in block["series"]:
            value = row["values"].get(series["key"], 0)
            if value:
                width = max(1, round(value / biggest * 100))
                parts.append(
                    f"<td width='{width}%' height='12' bgcolor='{_BAR_COLOR[series['tone']]}' "
                    f"style='background:{_BAR_COLOR[series['tone']]};font-size:1px;line-height:1px'>&nbsp;</td>"
                )
        used = sum(max(1, round(row["values"].get(s["key"], 0) / biggest * 100)) for s in block["series"] if row["values"].get(s["key"], 0))
        if used < 100:
            parts.append(f"<td width='{100 - used}%' style='font-size:1px;line-height:1px'>&nbsp;</td>")
        bar = "<table cellpadding='0' cellspacing='0' width='100%' style='border-collapse:collapse'><tr>" + "".join(parts) + "</tr></table>"
        failed = row["values"].get("failed", 0)
        count = f"{total:,}" + (f" <span style='color:#e11d48'>({failed:,} failed)</span>" if failed else "")
        rows.append(
            "<tr>"
            f"<td style='padding:3px 8px 3px 0;font-size:11.5px;color:#64748b;white-space:nowrap'>{escape(row['label'])}</td>"
            f"<td width='70%' style='padding:3px 0'>{bar}</td>"
            f"<td align='right' style='padding:3px 0 3px 8px;font-size:11.5px;color:#1e293b;white-space:nowrap'>{count}</td>"
            "</tr>"
        )
    key = " &nbsp; ".join(
        f"<span style='color:{_BAR_COLOR[s['tone']]}'>&#9632;</span> {escape(s['label'])}" for s in block["series"]
    )
    return (
        "<table cellpadding='0' cellspacing='0' width='100%' style='border-collapse:collapse'>"
        + "".join(rows) + f"</table><p style='font-size:11px;color:#64748b;margin:6px 0 0'>{key}</p>"
    )


def _html_text(block: dict) -> str:
    color = _TONE_COLOR.get(block.get("tone", ""), "#334155")
    return f"<p style='font-size:13.5px;line-height:1.55;color:{color};margin:6px 0;white-space:pre-wrap'>{escape(block['text'])}</p>"


_HTML_BLOCKS = {"kpis": _html_kpis, "table": _html_table, "bars": _html_bars, "text": _html_text}


def render_html(result: dict, footer: str = "") -> str:
    parts: list[str] = []
    for section in result["sections"]:
        heading = (
            f"<h2 style='font-size:15px;font-weight:700;color:#0f172a;margin:0 0 3px'>{escape(section['title'])}</h2>"
            if section.get("title") else ""
        )
        scope = (
            f"<p style='font-size:11px;color:#94a3b8;margin:0 0 6px'>{escape(section['scope_note'])}</p>"
            if section.get("scope_note") else ""
        )
        blocks = "".join(f"<div style='margin-top:6px'>{_HTML_BLOCKS[b['kind']](b)}</div>" for b in section["blocks"])
        parts.append(f"<div style='margin-top:24px'>{heading}{scope}{blocks}</div>")

    filters = " &middot; ".join(escape(f) for f in result.get("filters") or [])
    filters_line = f"<div style='font-size:12px;color:#64748b;margin-top:3px'>{filters}</div>" if filters else ""
    description = (
        f"<div style='font-size:13px;color:#475569;margin-top:6px'>{escape(result['description'])}</div>"
        if result.get("description") else ""
    )
    notes = "".join(f"<p style='font-size:12px;color:#d97706;margin:12px 0 0'>{escape(n)}</p>" for n in result.get("notes") or [])
    extra = f"<br>{escape(footer)}" if footer else ""
    return f"""<html><body style="margin:0;padding:24px;background:#f1f5f9;{_FONT}">
<table cellpadding="0" cellspacing="0" width="100%"><tr><td align="center">
<table cellpadding="0" cellspacing="0" width="100%" style="max-width:780px;background:#fafafa;border:1px solid #e2e8f0;border-radius:12px">
<tr><td style="padding:26px 28px">
<div style="font-size:21px;font-weight:700;color:#0f172a">{escape(result['name'])}</div>
<div style="font-size:13px;color:#475569;margin-top:4px">{escape(result['period']['label'])}</div>
{filters_line}{description}{notes}{''.join(parts)}
<p style="font-size:11px;color:#94a3b8;margin:28px 0 0">Made by Revoluzen Flow on {escape(result['generated_label'])}.{extra}</p>
</td></tr></table></td></tr></table></body></html>"""


def render_text(result: dict, footer: str = "") -> str:
    lines = [result["name"], result["period"]["label"]]
    lines.extend(result.get("filters") or [])
    if result.get("description"):
        lines.append(result["description"])
    lines.extend(result.get("notes") or [])
    for section in result["sections"]:
        lines.append("")
        if section.get("title"):
            lines.append(section["title"].upper())
        if section.get("scope_note"):
            lines.append(section["scope_note"])
        for block in section["blocks"]:
            if block["kind"] == "kpis":
                for item in block["items"]:
                    detail = f" ({item['detail']})" if item.get("detail") else ""
                    lines.append(f"  {item['label']}: {item['value']}{detail}")
            elif block["kind"] == "table":
                if not block["rows"]:
                    lines.append(f"  {block['empty']}")
                    continue
                lines.append("  " + " | ".join(c["label"] for c in block["columns"]))
                for row in block["rows"]:
                    lines.append("  " + " | ".join(_cell_text(v) for v in row))
                note = _shown_note(block)
                if note:
                    lines.append(f"  {note}")
            elif block["kind"] == "bars":
                if not block.get("has_data"):
                    lines.append(f"  {block['empty']}")
                    continue
                for row in block["rows"]:
                    values = ", ".join(f"{s['label']} {row['values'].get(s['key'], 0)}" for s in block["series"])
                    lines.append(f"  {row['label']}: {values}")
            else:
                lines.append(block["text"])
    lines.append("")
    lines.append(f"Made by Revoluzen Flow on {result['generated_label']}.")
    if footer:
        lines.append(footer)
    return "\n".join(lines)


# ── Chat ────────────────────────────────────────────────────────────────

_CHAT_LIMIT = 2800


def render_chat(result: dict) -> str:
    """Slack and Teams get the headlines; the tables stay in the email."""
    lines = [f"*{result['name']}*", result["period"]["label"]]
    for section in result["sections"]:
        pieces: list[str] = []
        for block in section["blocks"]:
            if block["kind"] == "kpis":
                pieces.append(" · ".join(f"{i['label']} {i['value']}" for i in block["items"]))
            elif block["kind"] == "table":
                if not block["rows"]:
                    pieces.append(block["empty"])
                    continue
                for row in block["rows"][:5]:
                    pieces.append("• " + " · ".join(_cell_text(v) for v in row[:3]))
                if block.get("total", 0) > 5:
                    pieces.append(f"and {block['total'] - 5:,} more")
            elif block["kind"] == "text":
                pieces.append(block["text"])
        if pieces:
            lines.append("")
            if section.get("title"):
                lines.append(f"*{section['title']}*")
            lines.extend(pieces)
    text = "\n".join(lines)
    return text if len(text) <= _CHAT_LIMIT else text[:_CHAT_LIMIT - 1] + "…"


# ── PDF ─────────────────────────────────────────────────────────────────


def render_pdf(result: dict) -> bytes:
    from reportlab.graphics.shapes import Drawing, Rect
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_RIGHT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    title = ParagraphStyle("t", fontName="Helvetica-Bold", fontSize=18, leading=22, textColor=colors.HexColor("#0f172a"))
    sub = ParagraphStyle("s", fontName="Helvetica", fontSize=10, leading=13, textColor=colors.HexColor("#475569"))
    heading = ParagraphStyle("h", fontName="Helvetica-Bold", fontSize=12, leading=15, textColor=colors.HexColor("#0f172a"), spaceBefore=12, spaceAfter=3)
    small = ParagraphStyle("n", fontName="Helvetica", fontSize=8, leading=10, textColor=colors.HexColor("#94a3b8"))
    body = ParagraphStyle("b", fontName="Helvetica", fontSize=8.5, leading=11, textColor=colors.HexColor("#1e293b"))
    body_right = ParagraphStyle("br", parent=body, alignment=TA_RIGHT)
    head_style = ParagraphStyle("th", fontName="Helvetica-Bold", fontSize=7.5, leading=9, textColor=colors.HexColor("#64748b"))
    head_right = ParagraphStyle("thr", parent=head_style, alignment=TA_RIGHT)

    width = A4[0] - 30 * mm
    story: list[Any] = [
        Paragraph(xml_escape(result["name"]), title),
        Paragraph(xml_escape(result["period"]["label"]), sub),
    ]
    for line in result.get("filters") or []:
        story.append(Paragraph(xml_escape(line), sub))
    if result.get("description"):
        story.append(Spacer(1, 4))
        story.append(Paragraph(xml_escape(result["description"]), sub))

    def colored(text: str, tone: str, style: ParagraphStyle) -> Paragraph:
        color = _TONE_COLOR.get(tone, _TONE_COLOR[""])
        return Paragraph(f'<font color="{color}">{xml_escape(text)}</font>', style)

    def kpi_table(block: dict) -> Table:
        value_style = ParagraphStyle("v", fontName="Helvetica-Bold", fontSize=14, leading=17)
        rows = []
        for chunk in _chunks(block["items"], 3):
            cells = []
            for item in chunk:
                inner = [
                    Paragraph(xml_escape(item["label"]), small),
                    colored(item["value"], item.get("tone", ""), value_style),
                ]
                if item.get("detail"):
                    inner.append(Paragraph(xml_escape(item["detail"]), small))
                cells.append(inner)
            cells.extend("" for _ in range(3 - len(chunk)))
            rows.append(cells)
        made = Table(rows, colWidths=[width / 3] * 3)
        made.setStyle(TableStyle([
            ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#e2e8f0")),
            ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#e2e8f0")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ]))
        return made

    def data_table(block: dict) -> list[Any]:
        if not block["rows"]:
            return [Paragraph(xml_escape(block["empty"]), body)]
        # Sized by what each column holds, so a short heading is not cut in two.
        sizes = [
            min(max(len(c["label"]) + 2, 7, *(len(_cell_text(r[i])) for r in block["rows"])), 34)
            for i, c in enumerate(block["columns"])
        ]
        widths = [width * s / sum(sizes) for s in sizes]
        rows = [[
            Paragraph(xml_escape(c["label"]).upper(), head_right if c["align"] == "right" else head_style)
            for c in block["columns"]
        ]]
        for row in block["rows"]:
            rows.append([
                colored(_cell_text(v), _cell_tone(v), body_right if c["align"] == "right" else body)
                for c, v in zip(block["columns"], row)
            ])
        made = Table(rows, colWidths=widths, repeatRows=1)
        made.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f1f5f9")),
            ("LINEBELOW", (0, 0), (-1, 0), 1, colors.HexColor("#e2e8f0")),
            ("LINEBELOW", (0, 1), (-1, -1), 0.25, colors.HexColor("#f1f5f9")),
            ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#e2e8f0")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ]))
        note = _shown_note(block)
        return [made] + ([Paragraph(note, small)] if note else [])

    def bars_table(block: dict) -> list[Any]:
        if not block.get("has_data"):
            return [Paragraph(xml_escape(block["empty"]), body)]
        biggest = max(sum(r["values"].values()) for r in block["rows"]) or 1
        bar_width = width * 0.68
        rows = []
        for row in block["rows"]:
            drawing = Drawing(bar_width, 8)
            x = 0.0
            for series in block["series"]:
                value = row["values"].get(series["key"], 0)
                if value:
                    span = value / biggest * bar_width
                    drawing.add(Rect(x, 0, span, 8, fillColor=colors.HexColor(_BAR_COLOR[series["tone"]]), strokeColor=None))
                    x += span
            total = sum(row["values"].values())
            failed = row["values"].get("failed", 0)
            rows.append([
                Paragraph(xml_escape(row["label"]), body), drawing,
                Paragraph(f"{total:,}" + (f" ({failed:,} failed)" if failed else ""), body_right),
            ])
        made = Table(rows, colWidths=[width * 0.16, bar_width, width * 0.16])
        made.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("TOPPADDING", (0, 0), (-1, -1), 1), ("BOTTOMPADDING", (0, 0), (-1, -1), 1)]))
        return [made]

    for section in result["sections"]:
        if section.get("title"):
            story.append(Paragraph(xml_escape(section["title"]), heading))
        if section.get("scope_note"):
            story.append(Paragraph(xml_escape(section["scope_note"]), small))
        for block in section["blocks"]:
            story.append(Spacer(1, 3))
            if block["kind"] == "kpis":
                story.append(kpi_table(block))
            elif block["kind"] == "table":
                story.extend(data_table(block))
            elif block["kind"] == "bars":
                story.extend(bars_table(block))
            else:
                story.append(Paragraph(xml_escape(block["text"]).replace("\n", "<br/>"), body))

    story.append(Spacer(1, 12))
    story.append(Paragraph(f"Made by Revoluzen Flow on {xml_escape(result['generated_label'])}.", small))

    buffer = io.BytesIO()
    SimpleDocTemplate(
        buffer, pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm, topMargin=14 * mm, bottomMargin=14 * mm,
        title=result["name"], author="Revoluzen Flow",
    ).build(story)
    return buffer.getvalue()


# ── Word ────────────────────────────────────────────────────────────────


def render_docx(result: dict) -> bytes:
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Pt, RGBColor

    def rgb(tone: str) -> RGBColor:
        return RGBColor.from_string(_TONE_COLOR.get(tone, _TONE_COLOR[""]).lstrip("#").upper())

    document = Document()
    document.add_heading(result["name"], level=0)
    document.add_paragraph(result["period"]["label"])
    for line in result.get("filters") or []:
        document.add_paragraph(line)
    if result.get("description"):
        document.add_paragraph(result["description"])

    def fill(target, text: str, tone: str = "", align: str = "left", bold: bool = False, size: float = 9) -> None:
        paragraph = target.paragraphs[0]
        run = paragraph.add_run(text)
        run.font.size = Pt(size)
        run.bold = bold
        if tone:
            run.font.color.rgb = rgb(tone)
        if align == "right":
            paragraph.alignment = WD_ALIGN_PARAGRAPH.RIGHT

    for section in result["sections"]:
        if section.get("title"):
            document.add_heading(section["title"], level=1)
        if section.get("scope_note"):
            document.add_paragraph(section["scope_note"])
        for block in section["blocks"]:
            if block["kind"] == "kpis":
                made = document.add_table(rows=2, cols=len(block["items"]))
                made.style = "Table Grid"
                for index, item in enumerate(block["items"]):
                    fill(made.cell(0, index), item["label"], bold=True, size=8)
                    fill(made.cell(1, index), item["value"], item.get("tone", ""), bold=True, size=11)
            elif block["kind"] == "table":
                if not block["rows"]:
                    document.add_paragraph(block["empty"])
                    continue
                made = document.add_table(rows=1 + len(block["rows"]), cols=len(block["columns"]))
                made.style = "Table Grid"
                for index, column in enumerate(block["columns"]):
                    fill(made.cell(0, index), column["label"], align=column["align"], bold=True, size=8)
                for row_index, row in enumerate(block["rows"], start=1):
                    for index, (column, value) in enumerate(zip(block["columns"], row)):
                        fill(made.cell(row_index, index), _cell_text(value), _cell_tone(value), column["align"])
                note = _shown_note(block)
                if note:
                    document.add_paragraph(note)
            elif block["kind"] == "bars":
                if not block.get("has_data"):
                    document.add_paragraph(block["empty"])
                    continue
                made = document.add_table(rows=1 + len(block["rows"]), cols=1 + len(block["series"]))
                made.style = "Table Grid"
                fill(made.cell(0, 0), "Period", bold=True, size=8)
                for index, series in enumerate(block["series"], start=1):
                    fill(made.cell(0, index), series["label"], align="right", bold=True, size=8)
                for row_index, row in enumerate(block["rows"], start=1):
                    fill(made.cell(row_index, 0), row["label"])
                    for index, series in enumerate(block["series"], start=1):
                        fill(made.cell(row_index, index), f"{row['values'].get(series['key'], 0):,}", align="right")
            else:
                document.add_paragraph(block["text"])
    document.add_paragraph(f"Made by Revoluzen Flow on {result['generated_label']}.")

    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()
