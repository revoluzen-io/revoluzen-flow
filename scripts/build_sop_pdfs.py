"""Render the Revoluzen Flow SOPs in docs/sop/ to PDF, for circulation.

    python scripts/build_sop_pdfs.py

Writes one PDF per procedure plus a combined document into dist/sop/ (which
is gitignored — the Markdown is the source of truth, the PDFs are a build
artifact and go stale the moment anyone edits it).

This parses the subset of Markdown the SOPs actually use and lays it out with
reportlab Platypus. Deliberately no pandoc or markdown dependency: reportlab
is already in the dev extra, and adding a toolchain to produce a handout is a
poor trade.

Deliberate choices:
  * A4. These are operating procedures for an Indian company; Letter would be
    the wrong default for the people printing them.
  * Standard Helvetica/Courier, which are WinAnsi-encoded. A scan of the
    sources found exactly one character outside that range (U+2192, the
    rightwards arrow), which is mapped rather than left to render as a black
    box. SUBSTITUTIONS is checked against every build, so a new one cannot
    slip in silently.
"""
import os
import re
import sys
from datetime import date

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    BaseDocTemplate, Frame, HRFlowable, KeepTogether, PageBreak, PageTemplate,
    Paragraph, Preformatted, Spacer, Table, TableStyle,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "docs")
OUT = os.path.join(ROOT, "dist", "docs-pdf")

# (relative path from docs/, title, the serif-italic descriptor, kind).
# Order is the order a reader should meet them.
DOCS = [
    ("user-guide.md", "Revoluzen Flow",
     "User Guide", "Product documentation"),
    ("sop/user-operations.md", "Running Revoluzen Flow",
     "For operators", "Standard Operating Procedure"),
    ("sop/developer-operations.md", "Developing Revoluzen Flow",
     "For developers", "Standard Operating Procedure"),
    ("sop/maintainer-operations.md", "Maintainer Operations",
     "For maintainers", "Standard Operating Procedure"),
    ("sop/security-response.md", "Responding to a Security Report",
     "For maintainers", "Standard Operating Procedure"),
    ("sop/go-live.md", "Taking Revoluzen Flow Public",
     "Launch runbook", "Standard Operating Procedure"),
    ("sop/README.md", "Standard Operating Procedures",
     "Index", "Standard Operating Procedure"),
]

# Characters the standard fonts cannot draw.
SUBSTITUTIONS = {"\u2192": "->"}

# ── brand, taken from revoluzen.io's "Spec Sheet" theme ────────────────
# Values read from D:\Siva\revoluzen-io/src/app/globals.css rather than
# matched by eye, so the document and the website agree.
# Text is --color-graphite, which is what the site sets on <body>: near-black
# with a teal cast, NOT the mid-teal --color-ink. Letters stay black and the
# gold does the signalling, exactly as the site does it.
INK = colors.HexColor("#0D2B33")        # --color-graphite — body and headings
INK_DEEP = colors.HexColor("#0D2B33")   # same; display type
MUTED = colors.HexColor("#3F5D65")      # --color-slate — secondary prose
FAINT = colors.HexColor("#5D7A83")      # captions, footer
GOLD = colors.HexColor("#B8860B")       # the signal colour
GOLD_HI = colors.HexColor("#D4A017")    # display gold, the emphasised word
PAPER = colors.HexColor("#F1F9FA")      # --color-bone, tinted surface
PAPER_2 = colors.HexColor("#EEF7F8")    # table header wash
RULE = colors.HexColor("#D3E2E5")       # --color-rule
RULE_SOFT = colors.HexColor("#DCECEE")

BRAND = INK
CODE_BG = PAPER

# The site publishes Instrument Sans as a variable font, which neither Satori
# (its own OG-image pipeline) nor reportlab can read — the repo keeps a static
# Instrument Serif Italic for exactly that reason. So: the real serif italic
# for the emphasised line, and Helvetica for text, which has the bold weight
# a variable-only family cannot give us here.
_FONTS = r"D:\Siva\revoluzen-io\assets\fonts"
SERIF = "Helvetica-Oblique"
try:
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    _p = os.path.join(_FONTS, "InstrumentSerif-Italic.ttf")
    if os.path.isfile(_p):
        pdfmetrics.registerFont(TTFont("InstrumentSerif-Italic", _p))
        SERIF = "InstrumentSerif-Italic"
except Exception:  # noqa: BLE001 — a missing brand font must not stop the build
    pass


def sanitise(text: str) -> str:
    for bad, good in SUBSTITUTIONS.items():
        text = text.replace(bad, good)
    return text


def assert_renderable(text: str, where: str) -> None:
    """Fail loudly rather than emit a page of black boxes."""
    for ch in set(text):
        try:
            ch.encode("cp1252")
        except UnicodeEncodeError:
            raise SystemExit(
                "%s contains U+%04X, which the PDF fonts cannot draw. "
                "Add it to SUBSTITUTIONS." % (where, ord(ch)))


# ── styles ─────────────────────────────────────────────────────────────

def build_styles():
    ss = getSampleStyleSheet()
    s = {}
    # Spec Sheet: tight display tracking, a hairline under every rule, and
    # the one emphasised element set in the brand's serif italic.
    s["title"] = ParagraphStyle("t", parent=ss["Title"], fontName="Helvetica-Bold",
                                fontSize=25, leading=29, textColor=INK_DEEP,
                                alignment=TA_LEFT, spaceAfter=2)
    s["emph"] = ParagraphStyle("em", fontName=SERIF, fontSize=15.5, leading=19,
                               textColor=GOLD_HI, alignment=TA_LEFT,
                               spaceAfter=12)
    s["kicker"] = ParagraphStyle("kk", fontName="Helvetica-Bold", fontSize=7.8,
                                 leading=11, textColor=GOLD, spaceAfter=2)
    s["subtitle"] = ParagraphStyle("st", fontName="Helvetica", fontSize=8.8,
                                   leading=13, textColor=FAINT, spaceAfter=14)
    s["h1"] = ParagraphStyle("h1", fontName="Helvetica-Bold", fontSize=15,
                             leading=19, textColor=INK, spaceBefore=19,
                             spaceAfter=6)
    s["h2"] = ParagraphStyle("h2", fontName="Helvetica-Bold", fontSize=11.8,
                             leading=15.5, textColor=INK, spaceBefore=14,
                             spaceAfter=4)
    s["h3"] = ParagraphStyle("h3", fontName="Helvetica-Bold", fontSize=10.2,
                             leading=13.5, textColor=INK, spaceBefore=10,
                             spaceAfter=3)
    s["body"] = ParagraphStyle("b", fontName="Helvetica", fontSize=9.6,
                               leading=14.4, textColor=INK, spaceAfter=7)
    s["bullet"] = ParagraphStyle("bu", parent=s["body"], leftIndent=13,
                                 bulletIndent=3, spaceAfter=3.5)
    s["bullet2"] = ParagraphStyle("bu2", parent=s["bullet"], leftIndent=27,
                                  bulletIndent=17)
    s["quote"] = ParagraphStyle("q", parent=s["body"], leftIndent=12,
                                textColor=colors.HexColor("#4B5563"),
                                borderPadding=0, spaceBefore=4, spaceAfter=8)
    s["code"] = ParagraphStyle("c", fontName="Courier", fontSize=8.1,
                               leading=11.2, textColor=INK)
    s["cell"] = ParagraphStyle("cl", fontName="Helvetica", fontSize=8.6,
                               leading=11.6, textColor=INK)
    s["cellh"] = ParagraphStyle("clh", parent=s["cell"],
                                fontName="Helvetica-Bold")
    return s


# ── inline markup ──────────────────────────────────────────────────────

_CODE = re.compile(r"`([^`]+)`")
_BOLD = re.compile(r"\*\*([^*]+)\*\*")
_ITAL = re.compile(r"(?<![*\w])\*([^*\n]+)\*(?!\*)")
_LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")


def inline(text: str) -> str:
    """Markdown inline -> reportlab's mini-HTML, escaping first."""
    spans: list[str] = []

    def stash(m):
        spans.append(m.group(1))
        return "\x00%d\x00" % (len(spans) - 1)

    text = _CODE.sub(stash, text)                       # protect code spans
    text = (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
    def link(m):
        label, target = m.group(1), m.group(2).replace("&amp;", "&")
        # An in-page "#anchor" is a Markdown destination, not a PDF one.
        # reportlab refuses to format a <link> to a target it cannot resolve
        # and aborts the whole build, so these render as plain emphasis.
        if target.startswith("#"):
            return "<b>%s</b>" % label
        return '<link href="%s" color="#0B3C47">%s</link>' % (target, label)

    text = _LINK.sub(link, text)
    text = _BOLD.sub(r"<b>\1</b>", text)
    text = _ITAL.sub(r"<i>\1</i>", text)

    def restore(m):
        code = spans[int(m.group(1))]
        code = (code.replace("&", "&amp;").replace("<", "&lt;")
                    .replace(">", "&gt;"))
        return ('<font face="Courier" size="8.6" backColor="#F0F2F5">%s</font>'
                % code)

    return re.sub("\x00(\\d+)\x00", restore, text)


# ── block parsing ──────────────────────────────────────────────────────

def parse(md: str, S) -> list:
    flow: list = []
    lines = md.split("\n")
    i = 0
    while i < len(lines):
        raw = lines[i]
        line = raw.rstrip()

        if not line.strip():
            i += 1
            continue

        # fenced code
        if line.lstrip().startswith("```"):
            i += 1
            buf = []
            while i < len(lines) and not lines[i].lstrip().startswith("```"):
                buf.append(lines[i])
                i += 1
            i += 1
            body = "\n".join(buf) or " "
            tbl = Table([[Preformatted(body, S["code"])]],
                        colWidths=[165 * mm])
            tbl.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, -1), CODE_BG),
                ("BOX", (0, 0), (-1, -1), 0.5, RULE),
                ("LEFTPADDING", (0, 0), (-1, -1), 7),
                ("RIGHTPADDING", (0, 0), (-1, -1), 7),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ]))
            flow += [Spacer(1, 3), tbl, Spacer(1, 8)]
            continue

        # table
        if line.startswith("|") and i + 1 < len(lines) and \
                re.match(r"^\|[\s:|-]+\|$", lines[i + 1].strip()):
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                rows.append(cells)
                i += 1
            header, body = rows[0], rows[2:]
            ncol = max(len(r) for r in rows)
            width = 165 * mm
            # First column carries the label and gets more room.
            widths = ([width * 0.3] + [width * 0.7 / (ncol - 1)] * (ncol - 1)
                      if ncol > 1 else [width])
            data = [[Paragraph(inline(c), S["cellh"]) for c in header]]
            for r in body:
                r = r + [""] * (ncol - len(r))
                data.append([Paragraph(inline(c), S["cell"]) for c in r])
            tbl = Table(data, colWidths=widths, repeatRows=1)
            tbl.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), PAPER_2),
                ("LINEBELOW", (0, 0), (-1, 0), 0.9, GOLD),
                ("GRID", (0, 0), (-1, -1), 0.4, RULE),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ]))
            flow += [Spacer(1, 3), tbl, Spacer(1, 9)]
            continue

        # headings
        m = re.match(r"^(#{1,4})\s+(.*)$", line)
        if m:
            depth, txt = len(m.group(1)), m.group(2)
            style = S["h1"] if depth <= 2 else S["h2"] if depth == 3 else S["h3"]
            if depth <= 2:
                flow.append(Spacer(1, 4))
            flow.append(Paragraph(inline(txt), style))
            if depth <= 2:
                flow.append(HRFlowable(width="100%", thickness=0.6,
                                       color=RULE, spaceAfter=7))
            i += 1
            continue

        # horizontal rule
        if re.match(r"^(-{3,}|\*{3,})$", line.strip()):
            flow.append(HRFlowable(width="100%", thickness=0.6, color=RULE,
                                   spaceBefore=6, spaceAfter=10))
            i += 1
            continue

        # blockquote
        if line.lstrip().startswith(">"):
            buf = []
            while i < len(lines) and lines[i].lstrip().startswith(">"):
                buf.append(lines[i].lstrip()[1:].strip())
                i += 1
            text = " ".join(x for x in buf if x)
            para = Paragraph(inline(text), S["quote"])
            tbl = Table([[para]], colWidths=[165 * mm])
            tbl.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#FDF8EC")),
                ("LINEBEFORE", (0, 0), (0, -1), 2.4, GOLD),
                ("LEFTPADDING", (0, 0), (-1, -1), 10),
                ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                ("TOPPADDING", (0, 0), (-1, -1), 7),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
            ]))
            flow += [Spacer(1, 3), tbl, Spacer(1, 9)]
            continue

        # lists
        m = re.match(r"^(\s*)([-*]|\d+\.)\s+(.*)$", raw)
        if m:
            indent, marker, txt = m.group(1), m.group(2), m.group(3)
            # continuation lines belong to this item
            i += 1
            while i < len(lines):
                nxt = lines[i]
                if not nxt.strip():
                    break
                if re.match(r"^\s*([-*]|\d+\.)\s+", nxt) or \
                        nxt.lstrip().startswith(("#", "|", "```", ">")):
                    break
                txt += " " + nxt.strip()
                i += 1
            ordered = marker not in ("-", "*")
            style = S["bullet2"] if len(indent) >= 2 else S["bullet"]
            bullet = marker if ordered else "\u2022"
            flow.append(Paragraph(inline(txt), style, bulletText=bullet))
            continue

        # paragraph
        buf = [line]
        i += 1
        while i < len(lines) and lines[i].strip() and \
                not re.match(r"^\s*([-*]|\d+\.)\s+", lines[i]) and \
                not lines[i].lstrip().startswith(("#", "|", "```", ">", "---")):
            buf.append(lines[i].strip())
            i += 1
        flow.append(Paragraph(inline(" ".join(buf)), S["body"]))

    return flow


# ── document chrome ────────────────────────────────────────────────────

class Doc(BaseDocTemplate):
    def __init__(self, path, footer):
        super().__init__(path, pagesize=A4,
                         leftMargin=22 * mm, rightMargin=23 * mm,
                         topMargin=20 * mm, bottomMargin=18 * mm,
                         title=footer, author="Revoluzen Private Limited")
        # Zero padding so the frame's usable width IS self.width. With the
        # default 6pt each side, a 165mm table overflows a 165mm frame by
        # exactly 12pt — which reportlab reports as a LayoutError only once
        # a page happens to start with one.
        frame = Frame(self.leftMargin, self.bottomMargin,
                      self.width, self.height, id="n",
                      leftPadding=0, rightPadding=0,
                      topPadding=0, bottomPadding=0)
        self.addPageTemplates([PageTemplate(id="p", frames=[frame],
                                            onPage=self._chrome)])
        self._footer = footer

    def _chrome(self, canvas, doc):
        canvas.saveState()
        # A gold hairline at the head of every page, matching the site's
        # rule-above-content treatment; the page's own content starts below it.
        canvas.setStrokeColor(GOLD)
        canvas.setLineWidth(1.6)
        top = self.bottomMargin + self.height + 6 * mm
        canvas.line(self.leftMargin, top, self.leftMargin + 18 * mm, top)

        y = self.bottomMargin - 7 * mm
        canvas.setStrokeColor(RULE_SOFT)
        canvas.setLineWidth(0.5)
        canvas.line(self.leftMargin, y + 4 * mm,
                    self.leftMargin + self.width, y + 4 * mm)
        canvas.setFont("Helvetica", 7.2)
        canvas.setFillColor(FAINT)
        canvas.drawString(self.leftMargin, y, self._footer)
        canvas.setFillColor(GOLD)
        canvas.drawRightString(self.leftMargin + self.width, y,
                               "%d" % canvas.getPageNumber())
        canvas.restoreState()


def cover(S, title, audience, source, kind="Standard Operating Procedure",
          srcdir="docs/sop"):
    """A spec-sheet masthead: rule, kicker, title, serif-italic descriptor."""
    return [
        HRFlowable(width="100%", thickness=2, color=GOLD, spaceAfter=9),
        Paragraph("REVOLUZEN&nbsp; FLOW", S["kicker"]),
        Paragraph(title, S["title"]),
        Paragraph(audience, S["emph"]),
        HRFlowable(width="100%", thickness=0.6, color=RULE, spaceAfter=7),
        Paragraph(
            "%s &nbsp;&middot;&nbsp; Revoluzen Private Limited "
            "&nbsp;&middot;&nbsp; %s<br/>"
            "Source of truth: <font face='Courier' size='8'>%s/%s</font>. "
            "If this PDF and that file disagree, the file is right."
            % (kind, date.today().isoformat(), srcdir, source),
            S["subtitle"]),
    ]


def main() -> int:
    os.makedirs(OUT, exist_ok=True)
    S = build_styles()
    combined: list = []
    made = []

    for idx, (rel, title, audience, kind) in enumerate(DOCS):
        path = os.path.join(SRC, rel)
        if not os.path.isfile(path):
            print("  MISSING", rel)
            continue
        md = sanitise(open(path, encoding="utf-8").read())
        assert_renderable(md, rel)
        # The masthead already carries the title; leaving the file's own H1
        # in prints it twice, two centimetres apart.
        md = re.sub(r"\A\s*#\s+[^\n]*\n+", "", md)

        base = os.path.basename(rel).replace(".md", "")
        if base == "README":
            base = "sop-index"
        srcdir = "docs/" + os.path.dirname(rel) if os.path.dirname(rel) else "docs"

        out = os.path.join(OUT, base + ".pdf")
        # "Revoluzen Flow - User Guide", not "Revoluzen Flow - Revoluzen Flow".
        Doc(out, "%s - %s" % (title, audience)).build(
            cover(S, title, audience, os.path.basename(rel), kind, srcdir)
            + parse(md, S))
        made.append(out)
        print("  %-36s -> %s" % (rel, os.path.basename(out)))

        # Build a SECOND, independent set of flowables for the combined
        # document. Platypus flowables carry layout state once a build has
        # consumed them, so reusing the same objects fails in ways that read
        # as nonsense ("too large on page 1" for a paragraph that fits).
        if idx:
            combined.append(PageBreak())
        combined += cover(S, title, audience, os.path.basename(rel),
                          kind, srcdir) + parse(md, S)

    allpath = os.path.join(OUT, "Revoluzen-Flow-Documentation.pdf")
    Doc(allpath, "Revoluzen Flow - User Guide and Operating Procedures"
        ).build(combined)
    made.append(allpath)
    print("  %-36s -> %s" % ("(all %d, combined)" % len(DOCS),
                             os.path.basename(allpath)))

    print("\n%d PDFs in %s" % (len(made), OUT))
    for p in made:
        print("   %7.1f KB  %s" % (os.path.getsize(p) / 1024, os.path.basename(p)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
