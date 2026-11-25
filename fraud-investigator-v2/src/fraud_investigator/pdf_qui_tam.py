"""Generate a court-style qui tam complaint PDF from a finished investigation.

Produces ``06_Qui_Tam_Complaint.pdf`` — a court-ready rendering in the form
of a federal qui tam complaint filed under seal: caption block, sequential
paragraph numbering, statutory framework, per-count causes of action,
lettered prayer for relief, signature block, PACER-style footer.

This is the *court-ready document* the case writer pipeline emits alongside
``05_Counsel_Memorandum.pdf`` (the lawyer-brief / analysis memo). Both ship
together; the memo is the analysis backing the complaint.

Structure mirrors a real qui tam complaint (reference:
``United States ex rel. Keaton v. Cerebral, Inc.``, N.D. Iowa
1:22-cv-00055, June 2022):

  1. Caption block
  2. ``COMPLAINT`` title
  3. Unnumbered preamble paragraph
  4. INTRODUCTION                              (¶¶ 1-7)
  5. JURISDICTION AND VENUE                    (¶¶ 8-11)
  6. FEDERALLY FUNDED HEALTH INSURANCE PROGRAMS — A. Medicare / B. Medicaid
     / C. Other Federal Health Care Programs   (¶¶ 12-26)
  7. Statutory framework — AKS / Stark / Medical Necessity (count-dependent)
  8. DEFENDANTS' FRAUD — A. Background / B. The Scheme / C. False Claims
  9. Count I, II, III...                       (one per retained count)
 10. DEMAND FOR JURY TRIAL
 11. DESIGNATED PLACE OF TRIAL
 12. PRAYER FOR RELIEF                         (lettered a–f)
 13. Signature block

Every page carries a PACER-style footer line plus a
"DRAFT — NOT FOR FILING" notice making clear that this is an AI-generated
artifact for internal review, not a filed pleading.

Source-of-truth: reads ``summary.json`` plus the
``04_Full_Investigation_Bundle.json`` for per-iteration evidence and
counsel_build output.

Usage::

    python -m fraud_investigator.pdf_qui_tam --case-dir investigations/Foo
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    HRFlowable, KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer,
    Table, TableStyle,
)

from fraud_investigator.qui_tam import boilerplate, renumber, subject_parser

log = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────────────────────
# Font registration: try real Times New Roman; fall back to built-in Times
# ────────────────────────────────────────────────────────────────────────────

# Font names used throughout. After _register_fonts(), these point either to
# real TNR TTFs or to ReportLab's built-in Times family.
FONT_BODY = "Times-Roman"
FONT_BODY_BOLD = "Times-Bold"
FONT_BODY_ITALIC = "Times-Italic"
FONT_BODY_BOLDITALIC = "Times-BoldItalic"


def _register_fonts() -> None:
    """Try to register true Times New Roman TTFs; fall back silently."""
    global FONT_BODY, FONT_BODY_BOLD, FONT_BODY_ITALIC, FONT_BODY_BOLDITALIC

    # Candidate paths (macOS first — that's the developer machine; Linux
    # paths included for portability).
    candidates = [
        ("TNR", "/System/Library/Fonts/Supplemental/Times New Roman.ttf"),
        ("TNR-Bold", "/System/Library/Fonts/Supplemental/Times New Roman Bold.ttf"),
        ("TNR-Italic", "/System/Library/Fonts/Supplemental/Times New Roman Italic.ttf"),
        ("TNR-BoldItalic", "/System/Library/Fonts/Supplemental/Times New Roman Bold Italic.ttf"),
    ]
    if not all(os.path.exists(p) for _, p in candidates):
        # Try Linux liberation-serif as a TNR-metric-compatible fallback
        candidates = [
            ("TNR", "/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf"),
            ("TNR-Bold", "/usr/share/fonts/truetype/liberation/LiberationSerif-Bold.ttf"),
            ("TNR-Italic", "/usr/share/fonts/truetype/liberation/LiberationSerif-Italic.ttf"),
            ("TNR-BoldItalic", "/usr/share/fonts/truetype/liberation/LiberationSerif-BoldItalic.ttf"),
        ]
    if not all(os.path.exists(p) for _, p in candidates):
        log.info("pdf_brief: TNR TTFs not found; using built-in Times family")
        return

    try:
        for name, path in candidates:
            pdfmetrics.registerFont(TTFont(name, path))
        FONT_BODY = "TNR"
        FONT_BODY_BOLD = "TNR-Bold"
        FONT_BODY_ITALIC = "TNR-Italic"
        FONT_BODY_BOLDITALIC = "TNR-BoldItalic"
        log.info("pdf_brief: registered Times New Roman TTFs")
    except Exception as e:
        log.warning("pdf_brief: TNR registration failed (%s); using built-ins", e)


_register_fonts()


# ────────────────────────────────────────────────────────────────────────────
# Style palette
# ────────────────────────────────────────────────────────────────────────────

INK_BLACK = colors.HexColor("#000000")
INK_FOOTER = colors.HexColor("#1a4f86")  # PACER footer blue
INK_DRAFT = colors.HexColor("#8a2e2c")


def _build_styles() -> dict[str, ParagraphStyle]:
    """Build the named ParagraphStyle table used throughout."""
    base = getSampleStyleSheet()
    s: dict[str, ParagraphStyle] = {}

    # Body — Times-family, 12pt, double-spaced (leading=24)
    s["Body"] = ParagraphStyle(
        "Body", parent=base["Normal"], fontSize=12, leading=24,
        fontName=FONT_BODY, textColor=INK_BLACK,
        alignment=TA_JUSTIFY, spaceAfter=4,
    )
    # Numbered paragraph — same as Body, but with a hanging indent for "1."
    s["Numbered"] = ParagraphStyle(
        "Numbered", parent=s["Body"],
        leftIndent=0.0 * inch, firstLineIndent=0.0,
        spaceAfter=4,
    )
    # Centered, unnumbered text (caption rows, preamble extras)
    s["BodyCenter"] = ParagraphStyle(
        "BodyCenter", parent=s["Body"], alignment=TA_CENTER,
    )
    # Unnumbered preamble — indented like a real complaint's opening sentence
    s["Preamble"] = ParagraphStyle(
        "Preamble", parent=s["Body"], firstLineIndent=0.5 * inch,
        alignment=TA_JUSTIFY, spaceBefore=12, spaceAfter=12,
    )

    # Court caption title (single line, top of page 1)
    s["CourtHeading"] = ParagraphStyle(
        "CourtHeading", parent=base["Normal"], fontSize=12, leading=14,
        fontName=FONT_BODY_BOLD, textColor=INK_BLACK, alignment=TA_CENTER,
        spaceBefore=0, spaceAfter=0,
    )
    # Caption-block name lines
    s["CaptionParty"] = ParagraphStyle(
        "CaptionParty", parent=base["Normal"], fontSize=12, leading=18,
        fontName=FONT_BODY_BOLD, textColor=INK_BLACK, alignment=TA_LEFT,
    )
    s["CaptionPartyPlain"] = ParagraphStyle(
        "CaptionPartyPlain", parent=base["Normal"], fontSize=12, leading=18,
        fontName=FONT_BODY, textColor=INK_BLACK, alignment=TA_LEFT,
    )
    s["CaptionMeta"] = ParagraphStyle(
        "CaptionMeta", parent=base["Normal"], fontSize=12, leading=18,
        fontName=FONT_BODY_BOLD, textColor=INK_BLACK, alignment=TA_LEFT,
    )
    s["CaptionTick"] = ParagraphStyle(
        "CaptionTick", parent=base["Normal"], fontSize=12, leading=18,
        fontName=FONT_BODY, textColor=INK_BLACK, alignment=TA_CENTER,
    )

    # COMPLAINT title (centered, bold, underlined, 14pt)
    s["ComplaintTitle"] = ParagraphStyle(
        "ComplaintTitle", parent=base["Normal"], fontSize=14, leading=20,
        fontName=FONT_BODY_BOLD, textColor=INK_BLACK, alignment=TA_CENTER,
        spaceBefore=18, spaceAfter=18,
    )

    # Section heading — centered, bold, underlined, ALL CAPS
    s["Section"] = ParagraphStyle(
        "Section", parent=base["Normal"], fontSize=12, leading=18,
        fontName=FONT_BODY_BOLD, textColor=INK_BLACK, alignment=TA_CENTER,
        spaceBefore=18, spaceAfter=12, keepWithNext=True,
    )
    # Sub-section heading (A./B./C.) — indented, bold, underlined, title case
    s["Subsection"] = ParagraphStyle(
        "Subsection", parent=base["Normal"], fontSize=12, leading=18,
        fontName=FONT_BODY_BOLD, textColor=INK_BLACK,
        leftIndent=0.5 * inch, alignment=TA_LEFT,
        spaceBefore=10, spaceAfter=8, keepWithNext=True,
    )
    # Count heading — centered (3 lines: "Count I", cause name, statute)
    s["CountHeading"] = ParagraphStyle(
        "CountHeading", parent=base["Normal"], fontSize=12, leading=18,
        fontName=FONT_BODY_BOLD, textColor=INK_BLACK, alignment=TA_CENTER,
        spaceBefore=18, spaceAfter=4, keepWithNext=True,
    )
    s["CountHeadingSmall"] = ParagraphStyle(
        "CountHeadingSmall", parent=base["Normal"], fontSize=12, leading=16,
        fontName=FONT_BODY_BOLD, textColor=INK_BLACK, alignment=TA_CENTER,
        keepWithNext=True,
    )

    # Prayer item — lettered (a) etc., hanging indent
    s["PrayerItem"] = ParagraphStyle(
        "PrayerItem", parent=s["Body"],
        leftIndent=0.6 * inch, firstLineIndent=-0.4 * inch,
        spaceAfter=6,
    )
    # Signature block — right-aligned
    s["Signature"] = ParagraphStyle(
        "Signature", parent=base["Normal"], fontSize=12, leading=18,
        fontName=FONT_BODY, textColor=INK_BLACK, alignment=TA_LEFT,
        leftIndent=3.5 * inch, spaceBefore=4, spaceAfter=2,
    )
    s["SignatureFirm"] = ParagraphStyle(
        "SignatureFirm", parent=s["Signature"], fontName=FONT_BODY_BOLD,
        spaceBefore=12,
    )

    return s


# ────────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────────


def _esc(s: Any) -> str:
    """Escape XML for ReportLab Paragraph rendering; preserve <b>/<i>/<u>/<br>."""
    if s is None:
        return ""
    t = str(s)
    import re as _re
    t = _re.sub(r"\s+", " ", t).strip()
    t = t.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    t = t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    t = (t.replace("&lt;b&gt;", "<b>").replace("&lt;/b&gt;", "</b>")
          .replace("&lt;i&gt;", "<i>").replace("&lt;/i&gt;", "</i>")
          .replace("&lt;u&gt;", "<u>").replace("&lt;/u&gt;", "</u>")
          .replace("&lt;br/&gt;", "<br/>").replace("&lt;br&gt;", "<br/>"))
    return t


def _money(x: Any) -> str:
    """Render a number as $X,XXX."""
    if x is None or x == "":
        return "—"
    if isinstance(x, str) and x.startswith("$"):
        return x
    try:
        n = float(x)
        return f"${n:,.0f}"
    except (TypeError, ValueError):
        return str(x)


def _short_subject(subject: str) -> str:
    """Trim 'Cedar, M.D. -- Internal Medicine -- ...' to 'Cedar, M.D.'."""
    return subject.split("--")[0].strip().rstrip(",")[:60]


def _strip_internal_codes(text: str) -> str:
    """Remove internal evidence-ID references that would not appear in a real
    qui tam complaint. Targets:

      - parenthetical: ``(ev-3-abms-im-only-not-pain)``,
        ``(ev-1-identity, ev-1-opioid-share)``
      - bracketed: ``[ev-1-identity]``,
        ``[ev-1-identity, ev-1-opioid-share, ...]``
      - bare-word: ``ev-3-opioid-top-percentile``
      - misc internal flags: ``DIVERGES_FROM_TARGET``, ``NEED_MORE_INVESTIGATION``

    Also cleans up double-spaces and orphaned punctuation left behind.
    """
    if not text:
        return text
    import re as _re
    # Bracketed list of one or more ev-ids
    text = _re.sub(r"\[\s*ev-[0-9a-z-]+(?:\s*,\s*ev-[0-9a-z-]+)*\s*\]",
                   "", text)
    # Parenthetical list of one or more ev-ids (with no other content)
    text = _re.sub(r"\(\s*ev-[0-9a-z-]+(?:\s*,\s*ev-[0-9a-z-]+)*\s*\)",
                   "", text)
    # "per ev-x" or "see ev-x" introductory phrases
    text = _re.sub(r"\bper\s+ev-[0-9a-z-]+", "", text, flags=_re.IGNORECASE)
    text = _re.sub(r"\bsee\s+ev-[0-9a-z-]+", "", text, flags=_re.IGNORECASE)
    # Bare-word ev-ids (post-prefix like " — ev-3-foo" or mid-sentence)
    text = _re.sub(r"\s*[—–-]?\s*ev-[0-9a-z-]+", "", text)
    # Workflow references: iter-1, iter-2, iter-3, "at this iteration"
    text = _re.sub(r"\biter-\d+\s*(?:build|findings?|pass|run)?\s*",
                   "", text, flags=_re.IGNORECASE)
    text = _re.sub(r"\bat this iteration\b", "", text, flags=_re.IGNORECASE)
    # Internal verdict tokens
    text = _re.sub(
        r"\b(?:DIVERGES_FROM_TARGET|NEED_MORE_INVESTIGATION|"
        r"INDEFENSIBLE_AT_CAP|FILE_WITH_DISCOVERY_DEPS|SETTLEMENT_READY|NOT_ACTIONABLE|"
        r"MITIGATED|SURVIVED|DISPOSITIVE_NOT_SURVIVED)\b",
        "", text)
    # Internal count-ID tracking codes in prose: (C1), (C2), [C3], etc.
    # Note: leaves bare "C1" alone (could be medical context); only strips
    # the bracketed/parenthesized forms that are clearly internal labels.
    text = _re.sub(r"\s*\(C\d+(?:/[A-Z0-9]+)?\)", "", text)
    text = _re.sub(r"\s*\[C\d+(?:/[A-Z0-9]+)?\]", "", text)
    # Tidy up: orphaned punctuation, leftover prepositions, double-spaces
    text = _re.sub(r"\(\s*\)", "", text)         # empty parens
    text = _re.sub(r"\[\s*\]", "", text)         # empty brackets
    # Parens left with only orphan prepositions/connectives ("(per)",
    # "(per )", "(, )"...) after evidence-id removal
    text = _re.sub(
        r"\(\s*(?:per|see|and|or|,)?\s*\)", "", text, flags=_re.IGNORECASE)
    # Trailing "per" or "see" before a closing paren (e.g., "... fees per)")
    text = _re.sub(r"\s+(?:per|see)\s*\)", ")", text, flags=_re.IGNORECASE)
    text = _re.sub(r"\s+,", ",", text)           # space-before-comma
    text = _re.sub(r"\s+\.", ".", text)          # space-before-period
    text = _re.sub(r"\s+;", ";", text)           # space-before-semicolon
    text = _re.sub(r",\s*\)", ")", text)         # ", )"
    text = _re.sub(r"\(\s+", "(", text)          # "( word" -> "(word"
    text = _re.sub(r"\s+\)", ")", text)          # "word )" -> "word)"
    text = _re.sub(r"\s{2,}", " ", text)         # collapse multi-spaces
    return text.strip()


# Roman numeral helper for Count headings
def _roman(n: int) -> str:
    vals = [(1000, "M"), (900, "CM"), (500, "D"), (400, "CD"),
            (100, "C"), (90, "XC"), (50, "L"), (40, "XL"),
            (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I")]
    out = []
    for v, sym in vals:
        while n >= v:
            out.append(sym)
            n -= v
    return "".join(out)


# ────────────────────────────────────────────────────────────────────────────
# Resolvers (read counsel_build output from per-iteration bundle)
# ────────────────────────────────────────────────────────────────────────────


def _resolve_counts(full_iterations: list) -> list[dict]:
    """Find the most recent counsel_build.counts_pled across iterations."""
    for it in reversed(full_iterations or []):
        cb = (it.get("counsel_build") or {}) if isinstance(it, dict) else {}
        cp = cb.get("counts_pled")
        if cp:
            return cp
    return []


def _resolve_factual_basis(full_iterations: list) -> dict[str, str]:
    """Map count_id → factual_basis text from the final counsel_build."""
    for it in reversed(full_iterations or []):
        cb = (it.get("counsel_build") or {}) if isinstance(it, dict) else {}
        fbpc = cb.get("factual_basis_per_count")
        if fbpc and isinstance(fbpc, dict):
            return fbpc
    return {}


def _resolve_rule9b(full_iterations: list) -> dict[str, list[str]]:
    """Pull global Rule 9(b) particularity from the final counsel_build."""
    for it in reversed(full_iterations or []):
        cb = (it.get("counsel_build") or {}) if isinstance(it, dict) else {}
        r9 = cb.get("rule_9b_elements")
        if r9 and isinstance(r9, dict):
            return r9
    return {}


def _resolve_case_theory(full_iterations: list) -> str:
    """Get the case_theory text from the final counsel_build."""
    for it in reversed(full_iterations or []):
        cb = (it.get("counsel_build") or {}) if isinstance(it, dict) else {}
        ct = cb.get("case_theory")
        if ct:
            return ct
    return ""


def _all_evidence_indexed(full_iterations: list) -> dict[str, dict]:
    """Aggregate ALL evidence items across iterations, deduped, indexed by id."""
    by_id: dict[str, dict] = {}
    for it in full_iterations or []:
        inv = (it.get("investigator") or {}) if isinstance(it, dict) else {}
        for ev in inv.get("evidence", []) or []:
            eid = ev.get("id", "")
            if eid and eid not in by_id:
                by_id[eid] = ev
    return by_id


def _evidence_by_count(full_iterations: list,
                       counts: list[dict] | None = None) -> dict[str, list[dict]]:
    """Map count_id → evidence list using count.supporting_evidence_ids."""
    all_ev = _all_evidence_indexed(full_iterations)
    by_count: dict[str, list[dict]] = {}
    if counts is None:
        counts = _resolve_counts(full_iterations)
    for c in counts:
        cid = c.get("count_id", "")
        if not cid:
            continue
        ev_list: list[dict] = []
        seen_ids: set[str] = set()
        for eid in (c.get("supporting_evidence_ids") or []):
            if eid in all_ev and eid not in seen_ids:
                ev_list.append(all_ev[eid])
                seen_ids.add(eid)
        if not ev_list:
            desc = (c.get("description") or "").lower()
            for ev in all_ev.values():
                if ev.get("id") in seen_ids:
                    continue
                for tag in (ev.get("supports_counts") or []):
                    if tag and tag.lower() in desc:
                        ev_list.append(ev)
                        seen_ids.add(ev.get("id", ""))
                        break
        by_count[cid] = ev_list
    return by_count


# ────────────────────────────────────────────────────────────────────────────
# Page footer (PACER-style + DRAFT notice)
# ────────────────────────────────────────────────────────────────────────────


def _draw_court_footer(canvas, doc, case_id: str, filed_date: str,
                       total_pages_hint: int | None = None) -> None:
    """Draw the PACER-style footer + DRAFT notice on every page."""
    canvas.saveState()
    w, h = letter
    # PACER footer line (synthesized — case number is the internal case_id)
    canvas.setFont(FONT_BODY, 9)
    canvas.setFillColor(INK_FOOTER)
    pacer_line = (f"Case {case_id}   "
                  f"Document 1   "
                  f"Filed {filed_date}   "
                  f"Page {doc.page} of —")
    canvas.drawCentredString(w / 2, 0.55 * inch, pacer_line)
    # DRAFT notice
    canvas.setFont(FONT_BODY_ITALIC, 8)
    canvas.setFillColor(INK_DRAFT)
    canvas.drawCentredString(
        w / 2, 0.38 * inch,
        "DRAFT — NOT FOR FILING. AI-generated for internal review.",
    )
    # Page-number top-right (centered above the body, like Cerebral's 3, 4, etc.)
    canvas.setFont(FONT_BODY, 10)
    canvas.setFillColor(INK_BLACK)
    canvas.drawCentredString(w / 2, h - 0.55 * inch, str(doc.page))
    canvas.restoreState()


# ────────────────────────────────────────────────────────────────────────────
# Section builders
# ────────────────────────────────────────────────────────────────────────────


def _court_caption(state: dict, parsed: dict, styles: dict, district: str,
                   division: str) -> list:
    """Page-1 court caption block."""
    out: list = []

    # Top three centered lines: court name + district + division
    out.append(Paragraph("IN THE UNITED STATES DISTRICT COURT",
                         styles["CourtHeading"]))
    out.append(Paragraph(f"FOR THE {district.upper()}",
                         styles["CourtHeading"]))
    if division:
        out.append(Paragraph(division.upper(), styles["CourtHeading"]))
    out.append(Spacer(1, 0.25 * inch))

    case_no = state.get("case_id", "—")
    case_no_short = case_no.replace("CASE-", "").replace("-2026", "")
    # Synthesize a plausible case number ("1:XX-cv-NNNNN-XXX")
    synth_case = f"1:26-cv-{int(abs(hash(case_no)) % 100000):05d}"

    name_caption = parsed["name_caption"]

    # Two-column caption table: text | ")" ticks | metadata
    # Using a 3-column layout to mimic the typewriter ")" convention.
    left_lines = [
        ("UNITED STATES OF AMERICA <i>ex rel.</i>,", styles["CaptionParty"]),
        ("[RELATOR NAME],", styles["CaptionParty"]),
        ("", styles["CaptionParty"]),
        ("Plaintiffs,", styles["CaptionPartyPlain"]),
        ("", styles["CaptionParty"]),
        ("v.", styles["CaptionPartyPlain"]),
        ("", styles["CaptionParty"]),
        (name_caption + ",", styles["CaptionParty"]),
        ("", styles["CaptionParty"]),
        ("Defendant.", styles["CaptionPartyPlain"]),
    ]
    right_lines = [
        ("", styles["CaptionMeta"]),
        (f"Case No. {synth_case}", styles["CaptionMeta"]),
        ("", styles["CaptionMeta"]),
        ("FILED <i>IN CAMERA</i> AND", styles["CaptionMeta"]),
        ("UNDER SEAL", styles["CaptionMeta"]),
        ("", styles["CaptionMeta"]),
        ("Jury Trial Demanded", styles["CaptionMeta"]),
        ("", styles["CaptionMeta"]),
        ("", styles["CaptionMeta"]),
        ("", styles["CaptionMeta"]),
    ]

    rows = []
    for (lt, ls), (rt, rs) in zip(left_lines, right_lines):
        left_cell = Paragraph(_esc(lt), ls) if lt else Paragraph("&nbsp;", ls)
        tick_cell = Paragraph(")", styles["CaptionTick"])
        right_cell = Paragraph(_esc(rt), rs) if rt else Paragraph("&nbsp;", rs)
        rows.append([left_cell, tick_cell, right_cell])

    cap_tbl = Table(
        rows,
        colWidths=[3.2 * inch, 0.2 * inch, 3.0 * inch],
    )
    cap_tbl.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    out.append(cap_tbl)

    # Horizontal rule under caption
    out.append(HRFlowable(width="100%", thickness=0.75, color=INK_BLACK,
                          spaceBefore=4, spaceAfter=4))
    # COMPLAINT title
    out.append(Paragraph("<u>COMPLAINT</u>", styles["ComplaintTitle"]))
    return out


def _preamble(parsed: dict, styles: dict) -> list:
    """Unnumbered opening paragraph."""
    n = parsed["name_caption"]
    text = (
        f"Plaintiff, the Relator, [RELATOR NAME], pursuant to the "
        f"<i>qui tam</i> provisions of the False Claims Act, 31 U.S.C. "
        f"§§ 3729-3733 (the “FCA”), files this Complaint against "
        f"Defendant {n}. In support, Relator alleges as follows:"
    )
    return [Paragraph(_esc(text), styles["Preamble"])]


def _section_heading(text: str, styles: dict) -> Paragraph:
    """Centered, bold, underlined, ALL CAPS section heading."""
    return Paragraph(f"<u>{_esc(text.upper())}</u>", styles["Section"])


def _render_paragraph(p: dict, styles: dict) -> list:
    """Render a paragraph dict (numbered/unnumbered/subsection) as flowables."""
    kind = p.get("kind", "numbered")
    text = p["text"]
    if kind == "subsection":
        # text contains <b>...</b> markup with letter
        return [Paragraph(_esc(text).replace("&lt;b&gt;", "<b>")
                          .replace("&lt;/b&gt;", "</b>") + "<u> </u>",
                          styles["Subsection"])]
    if kind == "prayer":
        letter = p.get("letter", "")
        return [Paragraph(f"{letter}){'&nbsp;' * 4}{_esc(text)}",
                          styles["PrayerItem"])]
    if kind == "unnumbered":
        return [Paragraph(_esc(text), styles["Preamble"])]
    # numbered
    n = p.get("number", "?")
    return [Paragraph(f"{n}.{'&nbsp;' * 4}{_esc(text)}", styles["Numbered"])]


def _render_subsection(text: str, styles: dict) -> Paragraph:
    """Render a sub-section heading (A. Foo / B. Bar / C. Baz)."""
    return Paragraph(f"<b><u>{_esc(text)}</u></b>", styles["Subsection"])


# ────────────────────────────────────────────────────────────────────────────
# Assemble the document body (returns flat list of paragraph dicts +
# section/subsection markers for the layout pass)
# ────────────────────────────────────────────────────────────────────────────


def _assemble_introduction(parsed: dict, state: dict, full_iterations: list,
                           district: str) -> list[dict]:
    """INTRODUCTION (¶¶ 1-7-ish)."""
    name = parsed["name_prose"]
    specialty = parsed["specialty"]
    npi = parsed["npi"]
    city = parsed["city"]
    state_abbr = parsed["state"]

    case_theory = _strip_internal_codes(
        _resolve_case_theory(full_iterations).strip())
    counts = _resolve_counts(full_iterations)
    count_descriptions = [_strip_internal_codes(c.get("description", ""))
                          for c in counts]
    schemes_summary = "; ".join(
        _strip_internal_codes(c.get("statute", c.get("description", "scheme")))
        for c in counts
        if c.get("count_id") in (state.get("counts_retained") or [])
    ) or "the schemes described herein"

    paras: list[dict] = []
    # ¶ 1 — one-sentence statement of the action
    paras.append({"text":
        f"Relator brings this action on behalf of the United States of "
        f"America against Defendant for treble damages and civil penalties "
        f"arising from Defendant’s false statements and false claims in "
        f"violation of the False Claims Act (“FCA”), 31 U.S.C. § 3729 "
        f"<i>et seq.</i> The violations involve healthcare services and "
        f"items billed to federally-funded healthcare programs by "
        f"{name}, an individual provider enrolled as {specialty} (NPI "
        f"{npi}) practicing in {city}, {state_abbr}.",
        "kind": "numbered"})
    # ¶ 2 — summary of the scheme (case theory if available)
    if case_theory:
        # Cap length to ~600 chars to keep the intro tight
        ct = case_theory if len(case_theory) <= 700 else case_theory[:680] + "…"
        paras.append({"text": ct, "kind": "numbered"})
    else:
        paras.append({"text":
            f"Defendant {name} engaged in {schemes_summary} that caused "
            f"false and fraudulent claims to be presented to Medicare and, "
            f"in certain instances, Medicaid and other federally-funded "
            f"healthcare programs.",
            "kind": "numbered"})
    # ¶ 3 — resulting damage
    paras.append({"text":
        "As a result of Defendant’s conduct, federal health insurance "
        "programs, including but not limited to Medicare and Medicaid, have "
        "been caused to pay false and fraudulent claims for reimbursement.",
        "kind": "numbered"})
    # ¶¶ 4-5 — FCA elements boilerplate
    paras.extend(boilerplate.fca_elements_paragraphs())
    # ¶ 6 — disclosure-statement compliance
    paras.extend(boilerplate.disclosure_statement_compliance_paragraph(district))
    return paras


def _assemble_jurisdiction(district: str) -> list[dict]:
    """JURISDICTION AND VENUE paragraphs."""
    paras: list[dict] = []
    jv = boilerplate.jurisdiction_venue_paragraphs(district)
    # Splice the public-disclosure-bar disclaimer after the subject-matter para
    # (matches Cerebral's order: ¶ 8 jurisdiction, ¶ 9 public-disclosure, ¶ 10
    # personal jurisdiction, ¶ 11 venue).
    pdb = boilerplate.public_disclosure_bar_disclaimer()
    paras.append(jv[0])
    paras.extend(pdb)
    paras.extend(jv[1:])
    return paras


def _assemble_programs() -> list[dict]:
    """FEDERALLY FUNDED HEALTH INSURANCE PROGRAMS."""
    paras: list[dict] = []
    paras.extend(boilerplate.medicare_program_paragraphs())
    paras.extend(boilerplate.medicaid_program_paragraphs())
    paras.extend(boilerplate.other_federal_programs_paragraphs())
    return paras


def _assemble_defendants_fraud(parsed: dict, state: dict,
                                full_iterations: list) -> list[dict]:
    """DEFENDANTS' FRAUD section with A/B/C sub-sections."""
    name = parsed["name_prose"]
    specialty = parsed["specialty"]
    npi = parsed["npi"]
    city = parsed["city"]
    state_abbr = parsed["state"]

    case_theory = _strip_internal_codes(
        _resolve_case_theory(full_iterations).strip())
    factual_basis = {k: _strip_internal_codes(v) for k, v
                     in _resolve_factual_basis(full_iterations).items()}
    counts = _resolve_counts(full_iterations)
    counts_retained = set(state.get("counts_retained") or [])
    ev_by_count = _evidence_by_count(full_iterations, counts)

    paras: list[dict] = []

    # A. Background
    paras.append({"text": "<b>A. Defendant’s Background and Practice</b>",
                  "kind": "subsection"})
    paras.append({"text":
        f"Defendant {name} is an individual healthcare provider enrolled with "
        f"Medicare as a provider of {specialty} services (NPI {npi}), "
        f"practicing in {city}, {state_abbr}. At all times relevant to this "
        f"Complaint, Defendant submitted, or caused to be submitted, claims "
        f"for payment to Medicare and other federally-funded healthcare "
        f"programs.",
        "kind": "numbered"})
    paras.append({"text":
        "Defendant participates in the Medicare program as a Part B and Part "
        "D provider and is required, as a condition of payment, to comply "
        "with all applicable Medicare statutes, regulations, and program "
        "instructions, including the medical-necessity requirement (42 "
        "U.S.C. § 1395y(a)(1)(A)), the federal Anti-Kickback Statute (42 "
        "U.S.C. § 1320a-7b), and the physician self-referral provisions "
        "(42 U.S.C. § 1395nn).",
        "kind": "numbered"})

    # B. The Scheme
    paras.append({"text": "<b>B. The Scheme</b>", "kind": "subsection"})
    if case_theory:
        # Split case theory into 2-3 paragraphs at sentence boundaries
        import re
        sentences = re.split(r"(?<=[.!?])\s+", case_theory)
        chunk_size = max(2, len(sentences) // 3)
        for i in range(0, len(sentences), chunk_size):
            chunk = " ".join(sentences[i:i + chunk_size]).strip()
            if chunk:
                paras.append({"text": chunk, "kind": "numbered"})
    else:
        paras.append({"text":
            "The specific schemes alleged in this Complaint are described in "
            "the Counts below; each Count incorporates by reference the "
            "factual allegations of this section.",
            "kind": "numbered"})

    # C. False Claims (per-count factual basis + tie-to-presentment)
    paras.append({"text": "<b>C. Resulting False Claims to Federal Programs</b>",
                  "kind": "subsection"})
    for c in counts:
        cid = c.get("count_id", "C?")
        if counts_retained and cid not in counts_retained:
            continue
        desc = _strip_internal_codes(c.get("description")
                                      or c.get("statute", ""))
        fb = factual_basis.get(cid, "")

        # Para describing the count's scheme + tie to presentment.
        # Lead with the count's roman-numeral identifier instead of the
        # internal C1/C2 token (which is a tracking code, not a citation).
        scheme_para = (
            f"{desc.strip().rstrip('.')}."
            + (f" {fb.strip().rstrip('.')}." if fb else "")
            + f" Each claim for reimbursement submitted to Medicare and/or "
              f"Medicaid that arose from this conduct was tainted by the "
              f"unlawful scheme and therefore false within the meaning of the "
              f"False Claims Act, 31 U.S.C. § 3729(a)(1)(A) and (B); no "
              f"further express or implied false statement is required to "
              f"render such claims false, and none can render them legitimate."
        )
        paras.append({"text": scheme_para, "kind": "numbered"})

    return paras


def _assemble_counts(state: dict, full_iterations: list,
                     parsed: dict) -> list[dict]:
    """One Count per retained count. Returns a list of dicts with extra
    ``count_heading`` markers — the layout pass turns these into the centered
    multi-line Count headings."""
    counts = _resolve_counts(full_iterations)
    counts_retained = set(state.get("counts_retained") or [])
    factual_basis = {k: _strip_internal_codes(v) for k, v
                     in _resolve_factual_basis(full_iterations).items()}
    ev_by_count = _evidence_by_count(full_iterations, counts)

    out: list[dict] = []
    count_i = 0
    for c in counts:
        cid = c.get("count_id", "")
        if counts_retained and cid not in counts_retained:
            continue
        count_i += 1
        roman = _roman(count_i)
        statute_raw = (c.get("statute") or "").strip()
        # Always name the cause "Federal False Claims Act" (the operative
        # cause of action; underlying statutes are predicates that render
        # the resulting claims false).
        name = "Federal False Claims Act"
        # Build a clean citation line. Standard FCA presentment/false-record:
        # 31 U.S.C. §§ 3729(a)(1)(A) and (B). If the count cites predicates
        # (Stark or AKS), surface them on the citation line too.
        citation = "31 U.S.C. §§ 3729(a)(1)(A) and (B)"
        predicates = []
        if "1320a-7b" in statute_raw or "1320a" in statute_raw:
            predicates.append("42 U.S.C. § 1320a-7b (Anti-Kickback Statute)")
        if "1395nn" in statute_raw:
            predicates.append("42 U.S.C. § 1395nn (Physician Self-Referral)")
        if predicates:
            citation += "  —  Predicates: " + "; ".join(predicates)

        # Count heading marker (three-line, centered)
        out.append({"kind": "count_heading", "roman": roman,
                    "name": name, "citation": citation, "cid": cid})

        # Reallegation paragraph (first numbered para of each count)
        out.append({"text":
            "Relator realleges and incorporates by reference the allegations "
            "contained in the foregoing paragraphs of this Complaint.",
            "kind": "numbered"})
        # Treble damages claim sentence
        out.append({"text":
            "This is a claim for treble damages and penalties under the False "
            "Claims Act, 31 U.S.C. § 3729 <i>et seq.</i>, as amended.",
            "kind": "numbered"})
        # Count body — uses factual_basis + scheme description
        desc = (c.get("description") or "").strip()
        fb = (factual_basis.get(cid) or "").strip()
        body = (
            f"By virtue of the acts described above, Defendant knowingly "
            f"caused false or fraudulent claims to be made to federally-funded "
            f"healthcare programs."
        )
        out.append({"text": body, "kind": "numbered"})

        if desc:
            out.append({"text":
                f"Specifically, {_strip_internal_codes(desc).rstrip('.')}.",
                "kind": "numbered"})
        if fb:
            out.append({"text":
                f"In support of this Count, {fb.rstrip('.')}.",
                "kind": "numbered"})

        out.append({"text":
            "Each claim for reimbursement that resulted from this conduct, "
            "submitted to a federal healthcare program, represents a false or "
            "fraudulent claim for payment under the FCA. The Government, "
            "unaware of the falsity of the records, statements and claims "
            "made or caused to be made by Defendant, paid the claims that "
            "were non-payable as a result of the foregoing, and that were "
            "therefore false under the FCA.",
            "kind": "numbered"})

        # Rule 9(b) particularity reference — refers to the disclosure
        # statement as the location of factual particulars, without echoing
        # internal evidence-ID tracking codes.
        ev_list = ev_by_count.get(cid, [])
        if ev_list:
            out.append({"text":
                "The factual particulars supporting this Count, including the "
                "specific dates, billing codes, patient counts, and dollar "
                "amounts identifying representative false claims, are set "
                "forth in Relator’s confidential disclosure statement served "
                "on the Attorney General of the United States and the United "
                "States Attorney pursuant to 31 U.S.C. § 3730(b)(2).",
                "kind": "numbered"})

        out.append({"text":
            "By reason of Defendant’s acts, the United States has been "
            "damaged in a substantial amount to be determined at trial.",
            "kind": "numbered"})

    if count_i == 0:
        # Defensive: produce one boilerplate Count if no counts found
        out.append({"kind": "count_heading", "roman": "I",
                    "name": "Federal False Claims Act",
                    "citation": "31 U.S.C. §§ 3729(a)(1)(A) and (B)",
                    "cid": "C1"})
        out.append({"text":
            "Relator realleges and incorporates by reference the allegations "
            "contained in the foregoing paragraphs of this Complaint.",
            "kind": "numbered"})
        out.append({"text":
            "By virtue of the acts described above, Defendant knowingly "
            "caused false or fraudulent claims to be made to federally-funded "
            "healthcare programs.",
            "kind": "numbered"})

    return out


# ────────────────────────────────────────────────────────────────────────────
# Layout: flatten paragraphs into a ReportLab story
# ────────────────────────────────────────────────────────────────────────────


def _flow_paragraphs(paras: list[dict], styles: dict) -> list:
    """Convert numbered paragraph dicts into ReportLab flowables."""
    out: list = []
    for p in paras:
        kind = p.get("kind", "numbered")
        if kind == "subsection":
            # text has <b>...</b> markup with the letter
            t = p["text"]
            out.append(Paragraph(t, styles["Subsection"]))
        elif kind == "numbered":
            n = p.get("number", "?")
            t = _esc(p["text"])
            out.append(Paragraph(f"{n}.{'&nbsp;' * 4}{t}", styles["Numbered"]))
        elif kind == "unnumbered":
            out.append(Paragraph(_esc(p["text"]), styles["Preamble"]))
        elif kind == "prayer":
            letter = p.get("letter", "")
            out.append(Paragraph(f"{letter}){'&nbsp;' * 4}{_esc(p['text'])}",
                                  styles["PrayerItem"]))
        elif kind == "count_heading":
            out.append(Paragraph(f"<u>Count {p['roman']}</u>",
                                  styles["CountHeading"]))
            out.append(Paragraph(_esc(p["name"]),
                                  styles["CountHeadingSmall"]))
            out.append(Paragraph(_esc(p["citation"]),
                                  styles["CountHeadingSmall"]))
            out.append(Spacer(1, 6))
    return out


def _signature_block(styles: dict) -> list:
    """Right-aligned attorney signature block."""
    out: list = []
    out.append(Spacer(1, 0.4 * inch))
    out.append(Paragraph("Respectfully submitted,", styles["Signature"]))
    out.append(Spacer(1, 18))
    out.append(Paragraph("[FIRM NAME]", styles["SignatureFirm"]))
    out.append(Spacer(1, 6))
    out.append(Paragraph(
        "By: <i>s/[Counsel Name Placeholder]</i>",
        styles["Signature"]))
    out.append(Paragraph("[Counsel Name], [State Bar No.]", styles["Signature"]))
    out.append(Paragraph("[Firm Street Address]", styles["Signature"]))
    out.append(Paragraph("[City], [State] [Zip]", styles["Signature"]))
    out.append(Paragraph("[Telephone]", styles["Signature"]))
    out.append(Paragraph("[Email]", styles["Signature"]))
    out.append(Spacer(1, 10))
    out.append(Paragraph("<b>ATTORNEYS FOR RELATOR</b>", styles["Signature"]))
    return out


# ────────────────────────────────────────────────────────────────────────────
# Top-level orchestration
# ────────────────────────────────────────────────────────────────────────────


def build_brief(state: dict, full_iterations: list | None,
                adv_rounds: list | None,
                output_path: Path,
                post_run_updates: list | None = None) -> Path:
    """Build the qui tam complaint PDF.

    Signature matches the historical ``build_brief`` so the orchestrator's
    call site is unchanged. ``adv_rounds`` and ``post_run_updates`` are
    accepted for compatibility but no longer rendered — adversarial
    survival, post-run updates, evidence schedule, statistical appendix,
    and data sources audit are internal work-product and do not belong in
    a complaint.
    """
    styles = _build_styles()
    parsed = subject_parser.parse_subject(state.get("subject", ""))
    district = subject_parser.infer_district(
        parsed["state"], state.get("litigation_venue"), parsed.get("city"))
    division = subject_parser.division_from_city(parsed["city"])

    case_id = state.get("case_id", "—")
    filed_date = datetime.utcnow().strftime("%m/%d/%Y")

    # 1. Assemble the numbered-paragraph body in canonical order, partitioned
    #    by section so the layout pass can interleave section headings.
    introduction = _assemble_introduction(parsed, state,
                                          full_iterations or [], district)
    jurisdiction = _assemble_jurisdiction(district)
    programs = _assemble_programs()

    # Framework sections — select based on count descriptions
    counts = _resolve_counts(full_iterations or [])
    counts_retained = set(state.get("counts_retained") or [])
    retained_descs = [c.get("description", "") for c in counts
                       if (not counts_retained
                           or c.get("count_id") in counts_retained)]
    framework_keys = boilerplate.select_framework_sections(retained_descs)

    framework_sections: list[tuple[str, list[dict]]] = []
    for key in framework_keys:
        heading, paras = boilerplate.framework_section(key)
        framework_sections.append((heading, paras))

    defendants_fraud = _assemble_defendants_fraud(parsed, state,
                                                   full_iterations or [])
    counts_paras = _assemble_counts(state, full_iterations or [], parsed)

    # 2. Number the paragraphs sequentially across the entire body.
    all_numbered: list[dict] = []
    all_numbered.extend(introduction)
    all_numbered.extend(jurisdiction)
    all_numbered.extend(programs)
    for _, paras in framework_sections:
        all_numbered.extend(paras)
    all_numbered.extend(defendants_fraud)
    all_numbered.extend(counts_paras)
    all_numbered = renumber.assign_sequential_numbers(all_numbered)

    # Split numbered output back into per-section lists (the renumber pass
    # preserved order, so we can split by length).
    sections: list[tuple[str, list[dict]]] = []
    cursor = 0

    def _take(n: int) -> list[dict]:
        nonlocal cursor
        chunk = all_numbered[cursor:cursor + n]
        cursor += n
        return chunk

    sections.append(("INTRODUCTION", _take(len(introduction))))
    sections.append(("JURISDICTION AND VENUE", _take(len(jurisdiction))))
    sections.append(("FEDERALLY FUNDED HEALTH INSURANCE PROGRAMS",
                     _take(len(programs))))
    for (heading, paras) in framework_sections:
        sections.append((heading, _take(len(paras))))
    sections.append(("DEFENDANTS’ FRAUD", _take(len(defendants_fraud))))
    sections.append(("COUNTS", _take(len(counts_paras))))

    # 3. Build the ReportLab story.
    story: list = []
    story += _court_caption(state, parsed, styles, district, division)
    story += _preamble(parsed, styles)

    for heading, paras in sections:
        if heading == "COUNTS":
            # Counts each get their own centered heading; no umbrella heading
            story += _flow_paragraphs(paras, styles)
            continue
        story.append(_section_heading(heading, styles))
        story += _flow_paragraphs(paras, styles)

    # Terminal blocks (unnumbered)
    story.append(_section_heading("DEMAND FOR JURY TRIAL", styles))
    story += _flow_paragraphs(boilerplate.jury_demand_block(), styles)

    story.append(_section_heading("DESIGNATED PLACE OF TRIAL", styles))
    story += _flow_paragraphs(
        boilerplate.place_of_trial_block(parsed["city"], parsed["state"]),
        styles)

    story.append(_section_heading("PRAYER FOR RELIEF", styles))
    story.append(Paragraph(
        "<b>WHEREFORE</b>, Relator respectfully prays this Court:",
        styles["Body"]))
    story += _flow_paragraphs(boilerplate.prayer_for_relief_items(), styles)

    story += _signature_block(styles)

    # 4. Build the document.
    doc = SimpleDocTemplate(
        str(output_path),
        pagesize=letter,
        leftMargin=1.0 * inch, rightMargin=1.0 * inch,
        topMargin=1.0 * inch, bottomMargin=0.85 * inch,
        title=f"Qui Tam Complaint — {case_id}",
        author="Fraud-Investigator Pipeline (AI-generated draft)",
    )

    def _draw_footer(canvas, doc):
        _draw_court_footer(canvas, doc, case_id, filed_date)

    doc.build(story, onFirstPage=_draw_footer, onLaterPages=_draw_footer)
    return output_path


def build_from_case_dir(case_dir: Path,
                         out_filename: str = "06_Qui_Tam_Complaint.pdf") -> Path:
    """Convenience entry point — read state + bundle from a case dir and emit PDF."""
    case_dir = Path(case_dir)
    summary_p = case_dir / "summary.json"
    if not summary_p.exists():
        for legacy in ("03_Full_State_Investigation.json", "03_Full_State.json"):
            if (case_dir / legacy).exists():
                summary_p = case_dir / legacy
                break
    if not summary_p.exists():
        raise FileNotFoundError(
            f"No summary.json or 03_Full_State*.json in {case_dir}"
        )
    state = json.loads(summary_p.read_text())

    # Prefer the richer bundle when present, but DEFAULT to summary.json's own
    # iterations/adversarial_rounds. Otherwise a run whose 04_Full_Investigation_Bundle
    # was never assembled (e.g. a crash in the case-writer/finalize step) renders a
    # SKELETON complaint with no counts — summary.json already carries
    # counsel_build.counts_pled / case_theory / rule_9b_elements / factual_basis_per_count.
    full_iterations: list = state.get("iterations") or []
    adv_rounds: list = state.get("adversarial_rounds") or []
    bundle_p = None
    for fn in ("04_Full_Investigation_Bundle.json",
               "05_Full_Investigation_Bundle.json"):
        if (case_dir / fn).exists():
            bundle_p = case_dir / fn
            break
    post_run_updates: list = []
    if bundle_p is not None:
        try:
            bundle = json.loads(bundle_p.read_text())
            full_iterations = bundle.get("iterations") or full_iterations
            adv_rounds = bundle.get("adversarial_rounds") or adv_rounds
            post_run_updates = bundle.get("post_run_updates") or []
            bundle_state = bundle.get("summary") or {}
            if bundle_state:
                merged = dict(state)
                merged.update(bundle_state)
                state = merged
        except Exception as e:
            print(f"  warning: bundle load failed ({e}); using state only",
                  file=sys.stderr)

    out_path = case_dir / out_filename
    return build_brief(state, full_iterations, adv_rounds, out_path,
                       post_run_updates=post_run_updates)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--case-dir", required=True, type=Path,
                    help="Path to a case directory")
    ap.add_argument("--out", default="06_Qui_Tam_Complaint.pdf",
                    help="Output filename within case-dir")
    args = ap.parse_args(argv)

    out = build_from_case_dir(args.case_dir, args.out)
    print(f"Generated: {out} ({out.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
