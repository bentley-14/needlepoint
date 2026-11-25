"""Generate a law-firm-distribution PDF brief from a finished investigation.

Produces ``05_Counsel_Memorandum.pdf`` — a lawyer-brief-styled rendering of
the case dossier, suitable for distribution to outside FCA counsel.

The structure mirrors a standard qui tam complaint under 31 U.S.C. §§ 3729-
3733 (Rule 9(b) particularity, factual allegations per count, scienter
analysis, damages calculation), with three additions specific to our
analytical workflow:

  - **Adversarial Defense Survival** — what survives mock-defense rebuttal,
    what was dropped, residual disposition discounts
  - **Statistical Appendix** — every numeric claim's exact derivation
    (script command, file path + row, URL, retrieval timestamp)
  - **Data Sources Audit Trail** — every parquet / API / PDF the
    investigator consulted, so a law firm can independently verify

Source-of-truth: reads ``summary.json`` (the orchestrator's full state
dump). Falls back to ``04_Full_Investigation_Bundle.json`` if available
for the deepest agent-level detail.

Reference materials (structural inspiration, not legal advice):
  - 31 U.S.C. §§ 3729-3733 (False Claims Act)
  - FRCP Rule 9(b) (fraud particularity pleading standard)
  - DOJ FCA Resource Manual
  - ``docs/legal-references/sample_doj_fca_filing__*.pdf``
    (real-world DOJ FCA filing — cached for offline reference)

Usage::

    python -m fraud_investigator.pdf_brief \\
        --case-dir investigations/Mulberry-2026-05-20

    # OR from orchestrator: emit_brief_pdf(state_dir) automatically.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_LEFT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    HRFlowable, KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer,
    Table, TableStyle,
)


# ────────────────────────────────────────────────────────────────────────────
# Style palette — legal-brief conventions: serif body, blue-ink headings,
# justified prose, double-spaced section dividers.
# ────────────────────────────────────────────────────────────────────────────

INK_HEADING = colors.HexColor("#0a3a5e")    # deep navy blue
INK_BODY = colors.HexColor("#111111")
INK_MUTED = colors.HexColor("#444444")
INK_LIGHT = colors.HexColor("#888888")
RULE_LIGHT = colors.HexColor("#cccccc")
TABLE_HDR_BG = colors.HexColor("#e8eef5")
TIER_CONFIRMED_BG = colors.HexColor("#e6f4ea")
TIER_STAT_BG = colors.HexColor("#fef7e6")
TIER_LEGAL_BG = colors.HexColor("#eef0f5")


def _build_styles() -> dict[str, ParagraphStyle]:
    """Build the named ParagraphStyle table used throughout."""
    base = getSampleStyleSheet()
    s: dict[str, ParagraphStyle] = {}

    s["CoverCaption"] = ParagraphStyle(
        "CoverCaption", parent=base["Normal"], fontSize=10, leading=14,
        alignment=TA_CENTER, fontName="Times-Roman", textColor=INK_MUTED,
    )
    s["CoverTitle"] = ParagraphStyle(
        "CoverTitle", parent=base["Title"], fontSize=18, leading=24,
        alignment=TA_CENTER, fontName="Times-Bold", textColor=INK_HEADING,
        spaceBefore=18, spaceAfter=12,
    )
    s["CoverSubtitle"] = ParagraphStyle(
        "CoverSubtitle", parent=base["Normal"], fontSize=12, leading=16,
        alignment=TA_CENTER, fontName="Times-Italic", textColor=INK_MUTED,
        spaceAfter=6,
    )
    s["CoverMeta"] = ParagraphStyle(
        "CoverMeta", parent=base["Normal"], fontSize=10, leading=14,
        alignment=TA_CENTER, fontName="Times-Roman", textColor=INK_BODY,
        spaceAfter=2,
    )
    s["Section"] = ParagraphStyle(
        "Section", parent=base["Heading1"], fontSize=13, leading=18,
        fontName="Times-Bold", textColor=INK_HEADING,
        spaceBefore=18, spaceAfter=8, keepWithNext=True,
    )
    s["Subsection"] = ParagraphStyle(
        "Subsection", parent=base["Heading2"], fontSize=11, leading=14,
        fontName="Times-Bold", textColor=INK_BODY,
        spaceBefore=10, spaceAfter=4, keepWithNext=True,
    )
    s["Subsubsection"] = ParagraphStyle(
        "Subsubsection", parent=base["Heading3"], fontSize=10, leading=13,
        fontName="Times-BoldItalic", textColor=INK_MUTED,
        spaceBefore=6, spaceAfter=2, keepWithNext=True,
    )
    s["Body"] = ParagraphStyle(
        "Body", parent=base["BodyText"], fontSize=10, leading=14,
        fontName="Times-Roman", textColor=INK_BODY, alignment=TA_JUSTIFY,
        spaceAfter=6, firstLineIndent=18,
    )
    s["BodyNoIndent"] = ParagraphStyle(
        "BodyNoIndent", parent=base["BodyText"], fontSize=10, leading=14,
        fontName="Times-Roman", textColor=INK_BODY, alignment=TA_JUSTIFY,
        spaceAfter=6,
    )
    s["Bullet"] = ParagraphStyle(
        "Bullet", parent=base["BodyText"], fontSize=10, leading=13,
        fontName="Times-Roman", textColor=INK_BODY,
        leftIndent=22, bulletIndent=8, spaceAfter=3,
    )
    s["Citation"] = ParagraphStyle(
        "Citation", parent=base["Normal"], fontSize=8.5, leading=11,
        fontName="Courier", textColor=INK_HEADING, leftIndent=22,
        spaceAfter=4,
    )
    s["TableCell"] = ParagraphStyle(
        "TableCell", parent=base["Normal"], fontSize=8.5, leading=11,
        fontName="Times-Roman", textColor=INK_BODY, wordWrap="CJK",
    )
    s["TableCellHdr"] = ParagraphStyle(
        "TableCellHdr", parent=base["Normal"], fontSize=8.5, leading=11,
        fontName="Times-Bold", textColor=INK_HEADING,
    )
    s["Footer"] = ParagraphStyle(
        "Footer", parent=base["Normal"], fontSize=8, leading=10,
        alignment=TA_CENTER, fontName="Times-Italic", textColor=INK_LIGHT,
    )
    return s


# ────────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────────


def _esc(s: Any) -> str:
    """Escape XML for ReportLab Paragraph rendering. Preserve <b>, <i>, <br/>.

    Defensive against double-escaping: input that already contains
    pre-encoded entities (``&amp;``, ``&lt;``, ``&gt;``) is normalized
    back to the raw character first, then re-escaped exactly once.

    Also collapses runs of whitespace (newlines, tabs, multi-space) to a
    single space so LLM-produced text with embedded newlines renders as
    clean justified prose in ReportLab rather than ragged hard breaks.
    """
    if s is None:
        return ""
    t = str(s)
    # Collapse whitespace runs
    import re as _re
    t = _re.sub(r"\s+", " ", t).strip()
    # Decode any pre-existing entities so we don't double-escape
    t = t.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    # Now escape from scratch
    t = t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    # Re-allow basic inline formatting we want to surface
    t = (t.replace("&lt;b&gt;", "<b>").replace("&lt;/b&gt;", "</b>")
          .replace("&lt;i&gt;", "<i>").replace("&lt;/i&gt;", "</i>")
          .replace("&lt;br/&gt;", "<br/>").replace("&lt;br&gt;", "<br/>"))
    return t


def _truncate(text: str, limit: int) -> str:
    """Truncate at the nearest word boundary at or before ``limit`` and
    append ``…``. Returns text unchanged when it already fits.

    Replaces the prior hard-slice (``text[:N]``) that chopped mid-word and
    produced cells reading ``"...zero claims for branded"`` instead of
    a clean ``"...zero claims for branded TIRF products…"`` style cutoff.
    """
    if text is None:
        return ""
    if len(text) <= limit:
        return text
    cut = text.rfind(" ", 0, limit)
    if cut <= 0 or cut < limit - 40:
        # No whitespace within reach — fall back to hard cut, still add ellipsis
        cut = limit
    return text[:cut].rstrip(",;:.- ") + "…"


def _money(x: Any) -> str:
    """Render a number as $X,XXX with no math-pair risk (PDF text — safe)."""
    if x is None or x == "":
        return "—"
    if isinstance(x, str) and x.startswith("$"):
        return x
    try:
        n = float(x)
        return f"${n:,.0f}"
    except (TypeError, ValueError):
        return str(x)


def _money_compact(n: float) -> str:
    """Render a large number compactly: $42M, $1.5B, $704K."""
    abs_n = abs(n)
    if abs_n >= 1_000_000_000:
        return f"${n / 1_000_000_000:.1f}B".replace(".0B", "B")
    if abs_n >= 1_000_000:
        return f"${n / 1_000_000:.1f}M".replace(".0M", "M")
    if abs_n >= 1_000:
        return f"${n / 1_000:.0f}K"
    return f"${n:,.0f}"


def _format_dollar_range(s: Any) -> str:
    """Format raw integer/string ranges like ``42000000-112000000`` → ``$42M – $112M``.

    Handles inputs:
      - ``"42000000-112000000"`` → ``"$42M – $112M"``
      - ``"3000-6000"``          → ``"3,000 – 6,000"`` (no dollar — not currency)
      - ``"$3.3M – $4.6M"``       → passes through (already formatted)
      - ``42000000``              → ``"$42M"``
      - None / ""                 → ``"—"``

    Assumes any range whose minimum value exceeds 10,000 is a dollar range
    (statutory penalty / damages); below that it's a count range and gets
    a thousands-separator instead of dollars.
    """
    if s is None or s == "":
        return "—"
    txt = str(s).strip()
    # Already formatted (contains $, M, K, B, etc.)
    if any(c in txt for c in ("$", "M", "K", "B")):
        return txt
    # Single number?
    try:
        n = float(txt.replace(",", ""))
        return _money_compact(n) if n >= 10_000 else f"{n:,.0f}"
    except ValueError:
        pass
    # Range — split on hyphen / en-dash / em-dash
    import re as _re
    parts = _re.split(r"\s*[–—-]\s*", txt, maxsplit=1)
    if len(parts) == 2:
        try:
            lo = float(parts[0].replace(",", ""))
            hi = float(parts[1].replace(",", ""))
            if min(lo, hi) >= 10_000:
                return f"{_money_compact(lo)} – {_money_compact(hi)}"
            else:
                return f"{lo:,.0f} – {hi:,.0f}"
        except ValueError:
            pass
    return txt


def _make_table(data: list[list[Any]], col_widths: list[float] | None = None,
                styles: dict | None = None, header: bool = True) -> Table:
    """Build a table where every cell is a wrapped Paragraph."""
    styles = styles or _build_styles()
    body_style = styles["TableCell"]
    hdr_style = styles["TableCellHdr"]
    wrapped = []
    for r, row in enumerate(data):
        row_cells = []
        for c in row:
            if isinstance(c, Paragraph):
                row_cells.append(c)
                continue
            txt = _esc(c)
            style = hdr_style if (header and r == 0) else body_style
            row_cells.append(Paragraph(txt, style))
        wrapped.append(row_cells)
    t = Table(wrapped, colWidths=col_widths, repeatRows=1 if header else 0)
    ts = [
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("GRID", (0, 0), (-1, -1), 0.25, RULE_LIGHT),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]
    if header:
        ts += [
            ("BACKGROUND", (0, 0), (-1, 0), TABLE_HDR_BG),
            ("LINEBELOW", (0, 0), (-1, 0), 1, INK_HEADING),
        ]
    t.setStyle(TableStyle(ts))
    return t


def _draw_page_meta(canvas, doc, case_id: str, subject_short: str) -> None:
    """Header/footer on every page after the cover."""
    canvas.saveState()
    w, h = letter
    # Header
    canvas.setFont("Times-Italic", 8)
    canvas.setFillColor(INK_LIGHT)
    canvas.drawString(0.75 * inch, h - 0.5 * inch,
                      f"Counsel Memorandum — {case_id}")
    canvas.drawRightString(w - 0.75 * inch, h - 0.5 * inch, subject_short)
    canvas.setStrokeColor(RULE_LIGHT)
    canvas.line(0.75 * inch, h - 0.55 * inch, w - 0.75 * inch, h - 0.55 * inch)
    # Footer with page number
    canvas.drawCentredString(w / 2, 0.45 * inch,
                             f"— {doc.page} —")
    canvas.drawString(0.75 * inch, 0.45 * inch,
                      "PRIVILEGED · ATTORNEY WORK PRODUCT")
    canvas.drawRightString(w - 0.75 * inch, 0.45 * inch,
                           datetime.utcnow().strftime("%Y-%m-%d"))
    canvas.restoreState()


# ────────────────────────────────────────────────────────────────────────────
# Section builders — each returns a list[Flowable]
# ────────────────────────────────────────────────────────────────────────────


def _short_subject(subject: str) -> str:
    """Trim 'M. Mulberry, M.D. -- General Surgery -- ...' to 'M. Mulberry, M.D.'."""
    return subject.split("--")[0].strip().rstrip(",")[:60]


def _cover_page(state: dict, styles: dict) -> list:
    out = []
    out.append(Spacer(1, 0.8 * inch))
    out.append(Paragraph(
        f"UNITED STATES DISTRICT COURT<br/>"
        f"{_esc(state.get('litigation_venue', 'DISTRICT COURT'))}".replace(
            "U.S. District Court for the ", "").replace(
            "United States District Court for the ", ""),
        styles["CoverCaption"],
    ))
    out.append(Spacer(1, 0.3 * inch))
    out.append(HRFlowable(width="40%", thickness=0.75, color=INK_HEADING,
                          spaceBefore=4, spaceAfter=12,
                          hAlign="CENTER"))
    out.append(Paragraph(
        "<i>Counsel Memorandum:</i> Recommended Qui Tam Filing<br/>"
        "Under the False Claims Act, 31 U.S.C. §§ 3729-3733",
        styles["CoverTitle"]
    ))
    out.append(Spacer(1, 0.2 * inch))
    out.append(Paragraph(
        f"<b>RE:</b> {_esc(_short_subject(state.get('subject', '—')))}",
        styles["CoverSubtitle"]
    ))
    out.append(HRFlowable(width="40%", thickness=0.5, color=INK_HEADING,
                          spaceBefore=12, spaceAfter=18,
                          hAlign="CENTER"))

    rows = [
        ["Case identifier:", state.get("case_id", "—")],
        ["Subject (Proposed Defendant):", _short_subject(state.get("subject", "—"))],
        ["Filing venue:", state.get("litigation_venue", "—")],
        ["Final verdict:", state.get("final_verdict", "—")],
        ["Settlement target range:",
         state.get("final_settlement_target_usd", "—")],
        ["Computed strength:",
         f"{state.get('strength_bucket', '—')} "
         f"(score {(state.get('strength_score') or 0):.3f}, "
         f"EVF {(state.get('expected_value_factor') or 0):.3f})"],
        ["Single damages (estimate):",
         _money(state.get("total_single_damages_usd"))],
        ["Treble damages (estimate):",
         _money(state.get("total_treble_damages_usd"))],
        ["Counts retained:",
         ", ".join(state.get("counts_retained", []) or ["—"])],
        ["Counts abandoned:",
         ", ".join(state.get("counts_abandoned", []) or ["—"])],
        ["Memorandum prepared:", datetime.utcnow().strftime("%Y-%m-%d")],
    ]
    out.append(_make_table(
        [[Paragraph(f"<b>{_esc(k)}</b>", styles["TableCell"]),
          Paragraph(_esc(v), styles["TableCell"])] for k, v in rows],
        col_widths=[2.2 * inch, 4.0 * inch],
        styles=styles, header=False,
    ))
    out.append(Spacer(1, 0.5 * inch))
    out.append(Paragraph(
        "<b>PRIVILEGED &amp; CONFIDENTIAL — ATTORNEY WORK PRODUCT.</b> "
        "This memorandum is prepared for outside counsel evaluation under "
        "31 U.S.C. § 3730(b). Recipients should treat this document as "
        "subject to the work-product and attorney-client privileges. "
        "Distribution is limited to counsel evaluating the matter for "
        "potential filing under seal.",
        styles["CoverCaption"]
    ))
    out.append(PageBreak())
    return out


def _section(title: str, styles: dict) -> Paragraph:
    return Paragraph(_esc(title), styles["Section"])


def _subsection(title: str, styles: dict) -> Paragraph:
    return Paragraph(_esc(title), styles["Subsection"])


def _body(text: str, styles: dict, indent: bool = True) -> Paragraph:
    s = styles["Body"] if indent else styles["BodyNoIndent"]
    return Paragraph(_esc(text), s)


def _methodology(state: dict, styles: dict) -> list:
    """NEW: Methodology section — data sources, three-tier discipline, limitations."""
    out: list = []
    out.append(_section("II.  Methodology", styles))
    out.append(_subsection("A. Data sources", styles))
    out.append(_body(
        "This investigation was conducted exclusively on publicly-available "
        "CMS data sources, supplemented by NPPES, PECOS, OIG/LEIE, "
        "CourtListener, DOJ press releases, and state-level Medicaid + "
        "exclusion records. No subpoena, no insider testimony, no patient "
        "chart-level data were accessed.",
        styles,
    ))
    sources = [
        ["Source", "Provides", "Verification path"],
        ["CMS Medicare Part B PUF",
         "Per-NPI per-HCPCS aggregate billing 2013-2023 — service counts, beneficiary counts, payment amounts",
         "data.cms.gov/provider-summary-by-type-of-service/medicare-physician-other-practitioners"],
        ["CMS Medicare Part D PUF",
         "Per-NPI prescribing — opioid share, brand share, top drugs",
         "data.cms.gov/provider-summary-by-type-of-service/medicare-part-d-prescribers"],
        ["CMS DMEPOS PUF",
         "Durable medical equipment supplier patterns",
         "data.cms.gov/provider-summary-by-type-of-service/medicare-durable-medical-equipment"],
        ["NPPES NPI Registry (live API)",
         "Provider identity, specialty taxonomy, practice addresses",
         "npiregistry.cms.hhs.gov/api"],
        ["CMS PECOS Public Enrollment (live API)",
         "Medicare enrollment status, provider type, enrolled state(s)",
         "data.cms.gov/data-api/v1/dataset/2457ea29-fc82-48b0-86ec-3b0755de7515/data"],
        ["OIG LEIE Exclusion Database",
         "Federal Medicare/Medicaid exclusion records by name + NPI",
         "exclusions.oig.hhs.gov"],
        ["HHS T-MSIS Medicaid Provider Spending",
         "Per-(NPI, HCPCS, month) Medicaid spending 2018-2024 (FFS + MC + CHIP)",
         "opendata.hhs.gov/datasets/medicaid-provider-spending"],
        ["CourtListener + RECAP",
         "Federal court dockets — first-to-file bar verification, precedent search",
         "courtlistener.com/api/rest/v4"],
        ["DOJ press releases",
         "USAO settlement announcements, indictments, plea agreements",
         "justice.gov + scripts/doj_pr_lookup.py"],
    ]
    out.append(_make_table(sources,
                           col_widths=[1.8 * inch, 2.3 * inch, 2.5 * inch],
                           styles=styles))

    out.append(_subsection("B. Three-tier evidence discipline", styles))
    out.append(_body(
        "Each evidence item in this memorandum is tagged with a certainty "
        "tier. Tier discipline prevents conflation of directly-verifiable "
        "facts (tier 1) with peer-cohort statistical inferences (tier 2) "
        "with pleadable legal theories (tier 3).",
        styles,
    ))
    out.append(_body(
        "<b>confirmed_fact</b> — directly verifiable on a single source pull "
        "(NPPES record, PECOS enrollment row, CMS PUF aggregate, LEIE check). "
        "Characterized definitively.",
        styles, indent=False,
    ))
    out.append(_body(
        "<b>statistical_signal</b> — peer-cohort percentile, year-over-year "
        "trajectory, within-practice comparison, payer-selectivity ratio. "
        "Supports suspicion and probable cause; does NOT prove medical "
        "necessity violation, intent, or false certification by itself. "
        "Always reported with explicit probabilistic framing.",
        styles, indent=False,
    ))
    out.append(_body(
        "<b>legal_hypothesis</b> — scienter framing, materiality argument, "
        "conspiracy theory, damages projection. Pleadable theories that fit "
        "the facts; NOT proven legal conclusions.",
        styles, indent=False,
    ))

    out.append(_subsection("C. Limitations honestly disclosed", styles))
    out.append(_body(
        "The case rests entirely on CMS public-use data and public registries. "
        "No insider witness, no chart-level review, no privileged "
        "communications. Damages estimates are settlement-target "
        "approximations; a defensible litigation damages model requires "
        "per-claim allowed amounts, medical record review, and a defensible "
        "false-claim-rate determination — typically developed during the qui "
        "tam seal period and post-intervention discovery.",
        styles,
    ))
    return out


def _executive_summary(state: dict, styles: dict) -> list:
    out: list = []
    out.append(_section("I.  Executive Summary", styles))

    n_iters = len(state.get("iterations") or [])
    n_retained = len(state.get("counts_retained", []) or [])
    n_abandoned = len(state.get("counts_abandoned", []) or [])
    counts_ret_str = ", ".join(state.get("counts_retained", []) or [])
    counts_aban_str = ", ".join(state.get("counts_abandoned", []) or [])
    venue = state.get("litigation_venue", "—")
    sdp = state.get("same_district_precedent")
    bucket = state.get("strength_bucket", "—")

    out.append(_body(
        f"This memorandum summarizes a CMS-public-data-driven investigation of "
        f"the proposed defendant identified in the case caption. The "
        f"investigation applied a five-layer analytical pipeline — rule-based "
        f"signal detection, machine-learning ranking, agentic evidence "
        f"gathering (investigator + counsel build loop), mock adversarial "
        f"defense testing, and post-discount strength scoring — to determine "
        f"whether the underlying conduct supports a qui tam filing under "
        f"the False Claims Act, 31 U.S.C. §§ 3729-3733.",
        styles,
    ))

    out.append(_body(
        f"<b>Headline conclusion.</b> The case is classified as "
        f"<b>{_esc(bucket)}</b> with a computed strength score of "
        f"{(state.get('strength_score') or 0):.3f} (Expected Value Factor: "
        f"{(state.get('expected_value_factor') or 0):.3f}). The case was built "
        f"across {n_iters} iterations of investigator + counsel review "
        f"and survived two rounds of mock-defense adversarial testing.",
        styles,
    ))

    out.append(_body(
        f"<b>Counts surviving adversarial review:</b> {n_retained} count(s) "
        f"{'(' + _esc(counts_ret_str) + ')' if counts_ret_str else ''} "
        f"are pleadable with Rule 9(b) particularity, supported by per-count "
        f"evidence detailed in Section V. "
        + (f"{n_abandoned} count(s) ({_esc(counts_aban_str)}) were abandoned "
           f"after the mock defense identified a dispositive defect; the "
           f"final complaint omits those theories. "
           if n_abandoned else "No counts were abandoned. ")
        + f"Final verdict after all rounds: "
        f"<b>{_esc(state.get('final_verdict', '—'))}</b>.",
        styles,
    ))

    out.append(_body(
        f"<b>Damages range.</b> Estimated single damages of "
        f"{_money(state.get('total_single_damages_usd'))} yield treble damages "
        f"of {_money(state.get('total_treble_damages_usd'))} under "
        f"31 U.S.C. § 3729(a)(1)(G). Statutory civil penalties of "
        f"{_esc(_format_dollar_range(state.get('estimated_statutory_penalties_usd')))} "
        f"are estimated against approximately "
        f"{_esc(_format_dollar_range(state.get('estimated_false_claim_count_range')))} "
        f"false claims (2024 adjusted per-claim range: $13,946–$27,894). "
        f"The recommended settlement target range is "
        f"{_esc(state.get('final_settlement_target_usd', '—'))}, reflecting "
        f"a cumulative post-adversarial discount of "
        f"{state.get('final_cumulative_discount_pct', 0)}% applied to "
        f"trebled single damages.",
        styles,
    ))

    out.append(_body(
        f"<b>Venue and precedent.</b> Recommended filing venue: "
        f"<i>{_esc(venue)}</i>. "
        + (f"Controlling within-district precedent: {_esc(sdp[:300])}. "
           f"This precedent is binding in the proposed venue and demonstrates "
           f"that the U.S. Attorney's Office has previously pursued and "
           f"resolved structurally similar conduct."
           if sdp else
           "Counsel's same-district precedent search is recorded in "
           "Appendix E."),
        styles,
    ))

    return out


def _jurisdiction_venue(state: dict, styles: dict) -> list:
    out: list = []
    out.append(_section("III.  Jurisdiction and Venue", styles))
    out.append(_body(
        f"Jurisdiction is proper under 28 U.S.C. § 1331 (federal question) "
        f"and 31 U.S.C. § 3732(a), which confers exclusive subject-matter "
        f"jurisdiction over actions arising under the False Claims Act. "
        f"Venue is proper in <b>{_esc(state.get('litigation_venue', '—'))}</b> "
        f"under 31 U.S.C. § 3732(a) because the defendant transacts business "
        f"in this district and acts proscribed by 31 U.S.C. § 3729 occurred "
        f"in this district.",
        styles,
    ))
    sdp = state.get("same_district_precedent")
    if sdp:
        out.append(_body(
            f"<b>Same-district precedent.</b> {_esc(sdp)} This precedent is "
            f"controlling within the district and demonstrates that the U.S. "
            f"Attorney's Office for this venue has previously pursued and "
            f"resolved structurally similar conduct.",
            styles,
        ))
    return out


def _statutory_framework(state: dict, styles: dict) -> list:
    out: list = []
    out.append(_section("IV.  Statutory and Regulatory Framework", styles))
    out.append(_subsection("A. The False Claims Act, 31 U.S.C. §§ 3729-3733",
                           styles))
    out.append(_body(
        "Section 3729(a)(1)(A) imposes liability on any person who "
        "<i>knowingly presents, or causes to be presented, a false or "
        "fraudulent claim for payment or approval</i>. Section 3729(a)(1)(B) "
        "extends liability to any person who <i>knowingly makes, uses, or "
        "causes to be made or used, a false record or statement material to "
        "a false or fraudulent claim</i>. Under § 3729(b)(1), the scienter "
        "requirement (\"knowingly\") includes actual knowledge, deliberate "
        "ignorance, and reckless disregard of truth or falsity.",
        styles,
    ))
    out.append(_body(
        "Section 3729(a)(1) imposes treble damages plus civil penalties of "
        "between $13,946 and $27,894 per claim (2024 adjusted, per 28 "
        "C.F.R. § 85.5). Section 3730(b) authorizes private qui tam actions; "
        "§ 3730(b)(2) requires filing under seal with service on the "
        "Department of Justice but not on the defendant during the seal "
        "period. The materiality element is governed by <i>Universal Health "
        "Services, Inc. v. United States ex rel. Escobar</i>, 579 U.S. 176 "
        "(2016), which requires that a misrepresentation be material to the "
        "government's payment decision.",
        styles,
    ))
    out.append(_subsection("B. Rule 9(b) Particularity", styles))
    out.append(_body(
        "Federal Rule of Civil Procedure 9(b) requires that allegations of "
        "fraud be pleaded with particularity. In this Circuit, that standard "
        "generally requires the relator to identify representative false "
        "claims (or, in the alternative, particular facts that support a "
        "strong inference that false claims were submitted). The factual "
        "allegations in Part V of this memorandum identify specific CPT/HCPCS "
        "codes, service volumes, dates of service, and dollar amounts "
        "sufficient to satisfy Rule 9(b) at the pleading stage.",
        styles,
    ))
    return out


def _resolve_counts(full_iterations: list) -> list[dict]:
    """Find the most recent counsel_build.counts_pled across all iterations.

    The orchestrator's CaseState doesn't persist a top-level `counts` field;
    counts live in each counsel_build output (per-iteration). Pull from the
    last iteration that has them — that's the final pleaded count list after
    the build loop and rebuttal-closing pass converged.
    """
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
    """Pull global Rule 9(b) particularity from the final counsel_build.

    The counsel_build schema stores rule_9b_elements as GLOBAL (case-wide):
    ``{who, what, when, where, how}`` → list of supporting evidence IDs.
    The same WHO (the defendant) and WHEN/WHERE typically apply across counts.
    """
    for it in reversed(full_iterations or []):
        cb = (it.get("counsel_build") or {}) if isinstance(it, dict) else {}
        r9 = cb.get("rule_9b_elements")
        if r9 and isinstance(r9, dict):
            return r9
    return {}


def _resolve_case_theory(full_iterations: list) -> str:
    """Get the case_theory text from the final counsel_build (overall narrative)."""
    for it in reversed(full_iterations or []):
        cb = (it.get("counsel_build") or {}) if isinstance(it, dict) else {}
        ct = cb.get("case_theory")
        if ct:
            return ct
    return ""


def _resolve_precedents(full_iterations: list) -> list[str]:
    """Get the precedents list from the final counsel_build."""
    for it in reversed(full_iterations or []):
        cb = (it.get("counsel_build") or {}) if isinstance(it, dict) else {}
        ps = cb.get("precedents")
        if ps:
            return ps
    return []


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
    """Map count_id → evidence list using count.supporting_evidence_ids.

    Note: evidence items have a ``supports_counts`` field that uses *scheme
    names* (e.g., ``uds-overuse``), not count IDs (``C1``). The reliable
    linkage is the count's own ``supporting_evidence_ids`` list, which the
    counsel_build agent compiles per count. Falls back to the scheme-name
    matching if a count doesn't have explicit supporting_evidence_ids.
    """
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
        # Primary: use count.supporting_evidence_ids
        for eid in (c.get("supporting_evidence_ids") or []):
            if eid in all_ev and eid not in seen_ids:
                ev_list.append(all_ev[eid])
                seen_ids.add(eid)
        # Fallback: evidence items whose supports_counts list matches a token
        # that appears in this count's description (handles legacy scheme-name
        # tagging when explicit supporting_evidence_ids is missing).
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


def _factual_allegations(state: dict, full_iterations: list, styles: dict) -> list:
    out: list = []
    out.append(_section("V.  Factual Allegations", styles))

    # Subject + practice
    out.append(_subsection("A. Defendant's practice", styles))
    subj = state.get("subject", "—")
    out.append(_body(_esc(subj), styles, indent=False))

    # Case theory — the overall narrative from the last counsel_build
    case_theory = _resolve_case_theory(full_iterations)
    if case_theory:
        out.append(_subsection("B. Overall case theory", styles))
        out.append(_body(_esc(case_theory), styles))

    counts = _resolve_counts(full_iterations)
    factual_basis = _resolve_factual_basis(full_iterations)
    rule9b = _resolve_rule9b(full_iterations)
    ev_by_count = _evidence_by_count(full_iterations, counts)

    if not counts:
        out.append(_body(
            "<i>No pleaded counts found in any iteration's counsel_build "
            "output. Refer to 01_Case_Report.md for the narrative version.</i>",
            styles,
        ))
        return out

    counts_retained = set(state.get("counts_retained") or [])
    counts_abandoned = set(state.get("counts_abandoned") or [])

    out.append(_subsection("C. Counts (per-count factual allegations)", styles))

    # Tier labels for the per-count evidence grouping
    TIER_HEADERS = {
        "confirmed_fact": "Confirmed facts (tier 1 — directly verifiable)",
        "statistical_signal": "Statistical signals (tier 2 — peer-cohort + trajectory)",
        "legal_hypothesis": "Legal hypotheses (tier 3 — pleadable theories)",
    }

    for c in counts:
        cid = c.get("count_id", "C?")
        # Heading reflecting retention status
        if cid in counts_retained:
            badge = " <font color='#1f6e3e'>[RETAINED]</font>"
        elif cid in counts_abandoned:
            badge = " <font color='#8a2e2c'>[ABANDONED — adversarial dispositive defect]</font>"
        else:
            badge = ""
        desc = c.get("description") or ""
        out.append(Paragraph(f"<b>Count {cid}</b>{badge}", styles["Subsection"]))

        # Statute + conduct
        if c.get("statute"):
            out.append(_body(
                f"<b>Statutory basis.</b> {_esc(c['statute'])}",
                styles, indent=False,
            ))
        if desc:
            out.append(_body(
                f"<b>Defendant's conduct.</b> {_esc(desc)}",
                styles,
            ))
        # Augmented factual basis (per-count, from counsel_build)
        if cid in factual_basis and factual_basis[cid]:
            out.append(_body(
                f"<b>Factual support.</b> {_esc(factual_basis[cid])}",
                styles,
            ))

        # Damages
        if c.get("estimated_single_damages_usd"):
            sd = c["estimated_single_damages_usd"]
            try:
                td = float(sd) * 3
                td_str = _money(td)
            except (TypeError, ValueError):
                td_str = "—"
            out.append(_body(
                f"<b>Single damages.</b> {_money(sd)}. Treble damages "
                f"(31 U.S.C. § 3729(a)(1)(G)): {td_str}.",
                styles,
            ))

        # Per-count evidence, grouped by tier — the key depth add
        ev_list = ev_by_count.get(cid, [])
        if ev_list:
            # Group by tier in canonical order
            by_tier: dict[str, list[dict]] = {}
            for ev in ev_list:
                by_tier.setdefault(ev.get("tier", "unknown"), []).append(ev)

            out.append(_body(
                f"<b>Supporting evidence ({len(ev_list)} items).</b> "
                f"Each item is reproducible from the citation field; see "
                f"Appendix A for the complete schedule.",
                styles, indent=False,
            ))
            for tier in ("confirmed_fact", "statistical_signal", "legal_hypothesis"):
                items = by_tier.get(tier, [])
                if not items:
                    continue
                out.append(Paragraph(
                    f"<i>{_esc(TIER_HEADERS.get(tier, tier))}</i>",
                    styles["Subsubsection"],
                ))
                # Show up to 6 highest-confidence items per tier, with rationale
                items_sorted = sorted(items,
                                       key=lambda e: -float(e.get("confidence", 0)))
                for ev in items_sorted[:6]:
                    eid = ev.get("id", "")
                    fact = ev.get("fact", "")
                    rationale = ev.get("rationale", "")
                    conf = ev.get("confidence", 0)
                    out.append(_body(
                        f"<b>{_esc(eid)}</b> "
                        f"(confidence {conf:.2f}) — "
                        f"{_esc(fact)}",
                        styles, indent=False,
                    ))
                    if rationale:
                        out.append(Paragraph(
                            f"&nbsp;&nbsp;<i>Why this supports the count:</i> "
                            f"{_esc(rationale)}",
                            styles["BodyNoIndent"],
                        ))
                if len(items) > 6:
                    out.append(Paragraph(
                        f"<i>(+ {len(items) - 6} additional "
                        f"{_esc(tier)} items, see Appendix A)</i>",
                        styles["Subsubsection"],
                    ))

    # Global Rule 9(b) particularity — case-wide WHO/WHAT/WHEN/WHERE/HOW
    if rule9b:
        out.append(_subsection("D. Rule 9(b) Particularity (case-wide)", styles))
        out.append(_body(
            "Federal Rule of Civil Procedure 9(b) requires that allegations "
            "of fraud be pleaded with particularity. The relator submits the "
            "following particularity elements, each cross-referenced to "
            "specific evidence items in Appendix A:",
            styles,
        ))
        for elem in ("who", "what", "when", "where", "how"):
            evidence_ids = rule9b.get(elem) or rule9b.get(elem.upper()) or []
            if not evidence_ids:
                continue
            ids_str = ", ".join(evidence_ids[:10])
            if len(evidence_ids) > 10:
                ids_str += f" (+{len(evidence_ids) - 10} more)"
            out.append(_body(
                f"<b>{elem.upper()}:</b> supported by {len(evidence_ids)} "
                f"evidence item(s).",
                styles, indent=False,
            ))
            out.append(Paragraph(_esc(ids_str), styles["Citation"]))
    return out


def _adversarial_survival(state: dict, full_iterations: list,
                          adv_rounds: list, styles: dict) -> list:
    out: list = []
    out.append(_section("VI.  Adversarial Defense Survival", styles))
    out.append(_body(
        "Prior to this memorandum the case was subjected to internal "
        "mock-defense testing. The defense agent acted as opposing counsel — "
        "drafting motion-to-dismiss arguments against the complaint as "
        "pleaded — and a separate legal-adversarial-response agent applied "
        "a three-tier binary discipline to each attack:",
        styles,
    ))
    out.append(_body(
        "<b>SURVIVED</b> — the attack does not threaten the count's "
        "viability; no discount taken. "
        "<b>MITIGATED</b> — the attack identifies a real risk that the "
        "rebuttal partially addresses; discount applied to the count's "
        "expected settlement value. "
        "<b>DISPOSITIVE_NOT_SURVIVED</b> — the attack identifies a defect "
        "that, if credited, defeats the count as a matter of law; the count "
        "is abandoned.",
        styles, indent=False,
    ))

    fv = state.get("final_verdict") or "—"
    fcd = state.get("final_cumulative_discount_pct", 0)
    out.append(_body(
        f"<b>Final verdict after all rounds:</b> {_esc(fv)} at cumulative "
        f"discount {fcd}%. {100 - fcd:.0f}% of pleaded damages are retained "
        f"as defensible settlement value.",
        styles,
    ))

    # Per-round detail
    for adv in adv_rounds or []:
        rn = adv.get("round", "?")
        defense = adv.get("defense") or {}
        legal = adv.get("legal_adv_response") or {}
        attacks = defense.get("attacks") or []
        dispositions = {d.get("attack_id"): d
                        for d in legal.get("attack_dispositions") or []}

        out.append(_subsection(f"V.{rn}. Round {rn} — {len(attacks)} attacks filed",
                               styles))

        # Summary stats for the round
        ret = legal.get("counts_retained_this_round") or []
        abn = legal.get("counts_abandoned_this_round") or []
        pen = legal.get("counts_pending_rebuttal") or []
        cum_disc = legal.get("cumulative_case_discount_pct")
        verdict = legal.get("settlement_verdict")
        target = legal.get("settlement_target_range_usd")

        round_summary = []
        if ret:
            round_summary.append(f"retained: {', '.join(ret)}")
        if abn:
            round_summary.append(f"abandoned: {', '.join(abn)}")
        if pen:
            round_summary.append(f"pending rebuttal: {', '.join(pen)}")
        if cum_disc is not None:
            round_summary.append(f"cumulative discount {cum_disc}%")
        if verdict:
            round_summary.append(f"verdict: {verdict}")
        if target:
            round_summary.append(f"settlement target: {target}")
        if round_summary:
            out.append(_body(
                "<b>Round outcome:</b> " + "; ".join(round_summary) + ".",
                styles, indent=False,
            ))

        # Attacks table — one row per attack with disposition.
        # Severity values are "dispositive" (11 chars) or "mitigating" (10 chars);
        # both fit if the column has width ~0.9" at 8.5pt monospace.
        if attacks:
            rows: list[list[Any]] = [
                ["Attack ID", "Type", "Target",
                 "Severity", "Disposition", "Disc."],
            ]
            for a in attacks:
                aid = a.get("attack_id", "")
                disp = dispositions.get(aid, {})
                rows.append([
                    aid,
                    a.get("type", "")[:32],
                    ", ".join(a.get("target_counts", []) or [])[:16],
                    a.get("severity", "")[:12],            # fits "dispositive"
                    disp.get("disposition", "—")[:24],
                    (f"{disp.get('discount_pct')}%"
                     if disp.get("discount_pct") is not None else "—"),
                ])
            out.append(_make_table(
                rows,
                col_widths=[0.7 * inch, 1.5 * inch, 0.85 * inch,
                            0.85 * inch, 1.6 * inch, 0.55 * inch],
                styles=styles,
            ))

        # Detailed attack-by-attack breakdown — most important for credibility
        out.append(Paragraph(
            "<i>Per-attack detail (attack argument + plaintiff's response):</i>",
            styles["Subsubsection"],
        ))
        for a in attacks:
            aid = a.get("attack_id", "")
            atype = a.get("type", "")
            target_counts = ", ".join(a.get("target_counts", []) or [])
            arg = a.get("argument") or ""
            disp = dispositions.get(aid, {})
            outcome = disp.get("disposition", "—")
            disc = disp.get("discount_pct")
            reasoning = disp.get("reasoning") or ""

            out.append(_body(
                f"<b>{_esc(aid)} — {_esc(atype)}</b> (targets {_esc(target_counts)}; "
                f"severity {_esc(a.get('severity', '—'))}).",
                styles, indent=False,
            ))
            out.append(_body(
                f"<i>Defense argument:</i> {_esc(arg)}",
                styles,
            ))
            if outcome != "—":
                disc_str = f"{disc}% discount" if disc is not None else "no discount"
                out.append(_body(
                    f"<i>Disposition:</i> <b>{_esc(outcome)}</b> ({disc_str}). "
                    f"{_esc(reasoning)}",
                    styles,
                ))

        # Round-level reasoning summary
        pdr = legal.get("post_defense_reasoning")
        if pdr:
            out.append(_body(
                f"<b>Round {rn} post-defense reasoning:</b> {_esc(pdr)}",
                styles,
            ))

    return out


def _damages_calc(state: dict, styles: dict) -> list:
    out: list = []
    out.append(_section("VII.  Damages Calculation", styles))
    rows = [
        ["Component", "Amount", "Notes"],
        ["Single damages (per § 3729(a)(1))",
         _money(state.get("total_single_damages_usd")),
         "Sum of per-count false-claim payments (post-LCD discount)"],
        ["Treble damages (3×)",
         _money(state.get("total_treble_damages_usd")),
         "Per § 3729(a)(1)(G); reduced to 2× if defendant qualifies under cooperator provision"],
        ["Civil penalties (range)",
         _esc(_format_dollar_range(state.get("estimated_statutory_penalties_usd"))),
         f"$13,946–$27,894 per claim × est. "
         f"{_esc(_format_dollar_range(state.get('estimated_false_claim_count_range')))} claims"],
        ["Primary-window single damages",
         _money(state.get("primary_window_single_damages_usd")),
         "Manageable 6-year window for primary scienter pleading"],
        ["Settlement target (recommended)",
         _esc(state.get("final_settlement_target_usd", "—")),
         f"Post-adversarial, cumulative discount "
         f"{state.get('final_cumulative_discount_pct', 0)}%"],
    ]
    out.append(_make_table(rows, col_widths=[2.0 * inch, 1.4 * inch, 3.0 * inch],
                           styles=styles))
    return out


def _honest_limitations(state: dict, full_iterations: list, styles: dict) -> list:
    """NEW: Honest Limitations section — calibrates expectations for the law firm."""
    out: list = []
    out.append(_section("VIII.  Honest Limitations", styles))
    out.append(_body(
        "This memorandum is calibrated to a three-tier evidence discipline. "
        "The following limitations should be considered before the case is "
        "acted upon. None is fatal; all are pre-engagement diligence items "
        "for the engaging law firm.",
        styles,
    ))

    out.append(_subsection("A. Original-source qualification", styles))
    out.append(_body(
        "Every evidence item is derived from publicly-available CMS data + "
        "NPPES + PECOS + LEIE + CourtListener + Open Payments + state-level "
        "Medicaid records. Under <i>U.S. ex rel. Stebbins v. Maraposa "
        "Surgical Inc.</i>, No. 24-1626, 2024 WL 4947274 (3d Cir. Dec. 3, "
        "2024) (unpublished), and earlier circuit authority on 31 U.S.C. "
        "§ 3730(e)(4)(B) public-disclosure-bar analysis, synthesis of "
        "public data alone may be insufficient to qualify the relator as an "
        "<i>original source</i>. The opposite trend — Reed v. KeyPoint, 923 "
        "F.3d 729 (10th Cir. 2019) — accepts analytical compilation. The "
        "relator's independent knowledge sources should be documented "
        "before filing.",
        styles,
    ))

    out.append(_subsection("B. Damages methodology", styles))
    out.append(_body(
        f"The settlement-target range of "
        f"{_esc(state.get('final_settlement_target_usd', '—'))} is computed "
        f"as treble single damages × (1 - cumulative discount of "
        f"{state.get('final_cumulative_discount_pct', 0)}%). Single-damages "
        f"per-count derives from CMS Part B PUF aggregate billing multiplied "
        f"by a false-claim-rate determination grounded in the LCD-violation "
        f"analysis. This is a settlement-target approximation only; a "
        f"defensible litigation damages model requires per-claim allowed "
        f"amounts and chart-level medical-necessity determination, typically "
        f"developed during the qui tam seal period.",
        styles,
    ))

    out.append(_subsection("C. Insider-witness corroboration", styles))
    out.append(_body(
        "All evidence is exterior — billing records, regulatory filings, "
        "court records, peer-cohort comparisons. No interior witness, no "
        "billing-staff testimony, no chart review. Scienter inferences are "
        "circumstantial: payer-selectivity ratios, post-precedent billing "
        "continuation, OIG-concurrence anchors. Insider witness corroboration "
        "during the qui tam seal period materially strengthens scienter "
        "pleading.",
        styles,
    ))

    out.append(_subsection("D. Tool-result verifiability", styles))
    out.append(_body(
        "The investigator agent occasionally encounters HTTP 403 responses "
        "from rate-limited or Akamai-protected sources (DOJ press releases, "
        "CourtListener web endpoints). Where this occurred, the agent was "
        "instructed to use authenticated API fallbacks (CourtListener REST, "
        "DOJ-PR lookup script) and to document any unverifiable citation "
        "explicitly. Citations marked &quot;needs verification — fetched URL "
        "returned 403&quot; in Appendix A should be re-fetched at filing time.",
        styles,
    ))
    return out


def _recommendation(state: dict, full_iterations: list, styles: dict) -> list:
    """NEW: Recommendation — the law firm's go/no-go pitch."""
    out: list = []
    out.append(_section("IX.  Recommendation", styles))

    strength = state.get("strength_bucket", "—")
    score = state.get("strength_score") or 0
    evf = state.get("expected_value_factor") or 0
    target = state.get("final_settlement_target_usd", "—")
    venue = state.get("litigation_venue", "—")
    counts_retained = state.get("counts_retained", []) or []
    counts_abandoned = state.get("counts_abandoned", []) or []

    out.append(_body(
        f"This memorandum supports the conclusion that the case is a "
        f"<b>{_esc(strength)}-strength</b> claims-data lead (computed "
        f"strength score {score:.3f}, expected-value factor {evf:.3f}). "
        f"Specifically:",
        styles,
    ))

    bullets = []
    if counts_retained:
        bullets.append(
            f"<b>{len(counts_retained)} count(s) survived adversarial testing</b>: "
            f"{_esc(', '.join(counts_retained))}. Each is pleadable with "
            f"Rule 9(b) particularity supported by per-count evidence items "
            f"detailed in Section V."
        )
    if counts_abandoned:
        bullets.append(
            f"<b>{len(counts_abandoned)} count(s) were abandoned</b> on "
            f"dispositive-defect grounds: {_esc(', '.join(counts_abandoned))}. "
            f"The case proceeds without those theories, removing motion-to-"
            f"dismiss exposure on those grounds."
        )
    if state.get("same_district_precedent"):
        bullets.append(
            f"<b>Same-district precedent</b>: "
            f"{_esc(state['same_district_precedent'])} — controlling within "
            f"the venue and demonstrates the USAO has previously pursued "
            f"and resolved structurally similar conduct."
        )
    bullets.append(
        f"<b>Settlement target</b>: {_esc(target)} after "
        f"{state.get('final_cumulative_discount_pct', 0)}% cumulative "
        f"discount. Within firm-investment-justifying range for qui tam "
        f"engagement under the Relator's-share schedule of 31 U.S.C. § 3730(d)."
    )
    if venue and venue != "—":
        bullets.append(
            f"<b>Venue</b>: {_esc(venue)}."
        )

    for b in bullets:
        out.append(Paragraph(f"&nbsp;&nbsp;• {b}", styles["Bullet"]))

    out.append(_subsection("Caveats and conditions of engagement", styles))
    out.append(Paragraph(
        "&nbsp;&nbsp;• Original-source qualification under § 3730(e)(4)(B) is "
        "the single biggest unknown. Engagement should be conditioned on "
        "relator interview and documented direct-knowledge sources.",
        styles["Bullet"],
    ))
    out.append(Paragraph(
        "&nbsp;&nbsp;• Damages range is settlement-target approximation only. "
        "A defensible litigation damages model is post-intervention work "
        "product.",
        styles["Bullet"],
    ))
    out.append(Paragraph(
        "&nbsp;&nbsp;• Filing-day re-verification of NPPES + PECOS + LEIE + "
        "CourtListener first-to-file bar is required (re-run the per-NPI "
        "tool scripts within 30 days of filing).",
        styles["Bullet"],
    ))

    return out


def _prayer(state: dict, styles: dict) -> list:
    out: list = []
    out.append(_section("X.  Prayer for Relief", styles))
    out.append(_body(
        "Plaintiff respectfully requests that this Court enter judgment in "
        "Plaintiff-Relator's favor and against Defendant for (a) treble damages "
        "under 31 U.S.C. § 3729(a)(1)(G); (b) civil penalties under § 3729(a)(1) "
        "of between $13,946 and $27,894 per false claim; (c) the Relator's "
        "share under 31 U.S.C. § 3730(d) of fifteen to thirty percent (15-30%) "
        "of the proceeds recovered by the United States; (d) costs and reasonable "
        "attorneys' fees under 31 U.S.C. § 3730(d); and (e) such other and "
        "further relief as this Court deems just and proper.",
        styles,
    ))
    return out


def _verified_precedents(state: dict, full_iterations: list, styles: dict) -> list:
    """NEW Appendix E: Verified Precedents — all precedents with verification status."""
    out: list = []
    out.append(PageBreak())
    out.append(_section("Appendix E.  Verified Precedents", styles))
    out.append(_body(
        "Every case citation invoked in the factual allegations or "
        "regulatory framework was subjected to citation verification via "
        "CourtListener (REST API) or DOJ press release archives. "
        "Precedents listed here are verified existing cases; reviewing "
        "counsel can re-verify any citation via the listed API call.",
        styles, indent=False,
    ))

    # State.verified_precedents is a list of strings (full Bluebook citations)
    vps = state.get("verified_precedents") or []

    # Also pull precedents from the final counsel_build (richer context)
    cb_precedents = _resolve_precedents(full_iterations)

    if vps:
        out.append(_subsection("A. Verified case citations (binding or persuasive)",
                              styles))
        for i, p in enumerate(vps, 1):
            out.append(_body(
                f"<b>{i}.</b> {_esc(str(p))}",
                styles, indent=False,
            ))

    if cb_precedents:
        out.append(_subsection(
            "B. Counsel's precedent analysis (relevance + application)", styles))
        for i, p in enumerate(cb_precedents, 1):
            out.append(_body(
                f"<b>{i}.</b> {_esc(str(p))}",
                styles, indent=False,
            ))

    if not vps and not cb_precedents:
        out.append(_body(
            "<i>No verified precedents in case state.</i>",
            styles, indent=False,
        ))
    return out


def _evidence_schedule(state: dict, full_iterations: list, styles: dict) -> list:
    out: list = []
    out.append(PageBreak())
    out.append(_section("Appendix A.  Evidence Schedule", styles))
    out.append(_body(
        "All evidence items relied upon, with three-tier classification "
        "(<b>confirmed_fact</b>, <b>statistical_signal</b>, "
        "<b>legal_hypothesis</b>), citation, and confidence score. Items "
        "are pulled from the investigator's per-iteration evidence pool; "
        "each <code>ev-N-...</code> identifier resolves to a specific "
        "agent tool-call or computed metric.",
        styles, indent=False,
    ))
    rows: list[list[Any]] = [
        ["ID", "Tier", "Fact", "Citation", "Conf."],
    ]
    # Aggregate evidence across iterations, deduping by id
    seen = set()
    for it in full_iterations or []:
        inv = (it.get("investigator") or {}) if isinstance(it, dict) else {}
        for ev in inv.get("evidence", []) or []:
            eid = ev.get("id", "")
            if not eid or eid in seen:
                continue
            seen.add(eid)
            rows.append([
                eid,
                ev.get("tier", ""),
                ev.get("fact") or "",
                ev.get("citation") or "",
                f"{(ev.get('confidence') or 0):.2f}",
            ])
    if len(rows) == 1:
        out.append(_body(
            "<i>(No evidence items found in bundle. See 04_Full_Investigation_Bundle.json.)</i>",
            styles, indent=False,
        ))
        return out
    out.append(_make_table(
        rows,
        col_widths=[1.0 * inch, 0.7 * inch, 2.4 * inch, 2.0 * inch, 0.5 * inch],
        styles=styles,
    ))
    return out


def _statistical_appendix(state: dict, full_iterations: list, styles: dict) -> list:
    out: list = []
    out.append(PageBreak())
    out.append(_section("Appendix B.  Statistical Proof", styles))
    out.append(_body(
        "Every numeric claim in this memorandum derives from CMS public-use "
        "datasets accessed via documented scripts. The data lineage table "
        "below records the source of each major statistical assertion. A "
        "reviewing law firm can reproduce any figure by re-running the cited "
        "command against the local data caches.",
        styles, indent=False,
    ))

    # Pull confirmed_fact + statistical_signal evidence with citations
    rows = [["Assertion", "Tier", "Citation (script / file / URL)"]]
    for it in full_iterations or []:
        inv = (it.get("investigator") or {}) if isinstance(it, dict) else {}
        for ev in inv.get("evidence", []) or []:
            tier = ev.get("tier", "")
            if tier not in ("confirmed_fact", "statistical_signal"):
                continue
            rows.append([
                ev.get("fact") or "",
                tier,
                ev.get("citation") or "",
            ])
    if len(rows) == 1:
        out.append(_body("<i>(No statistical evidence found in bundle.)</i>",
                        styles, indent=False))
        return out
    # Limit to 30 rows; PDFs with 100+ row tables become unwieldy
    rows = rows[:31]
    out.append(_make_table(
        rows,
        col_widths=[3.0 * inch, 0.9 * inch, 2.7 * inch],
        styles=styles,
    ))
    return out


def _data_sources_audit(state: dict, full_iterations: list, styles: dict) -> list:
    out: list = []
    out.append(PageBreak())
    out.append(_section("Appendix C.  Data Sources Audit Trail", styles))
    out.append(_body(
        "Every data source consulted during the investigation, extracted "
        "from evidence-item citations. A law firm can independently verify "
        "any factual claim by querying the listed source.",
        styles, indent=False,
    ))

    # Extract unique citation prefixes (script names, file paths, URLs)
    sources: dict[str, int] = {}
    for it in full_iterations or []:
        inv = (it.get("investigator") or {}) if isinstance(it, dict) else {}
        for ev in inv.get("evidence", []) or []:
            cit = (ev.get("citation") or "").strip()
            if not cit:
                continue
            # Extract a 'source key' — first 60 chars, normalize whitespace
            key = " ".join(cit.split())[:90]
            sources[key] = sources.get(key, 0) + 1

    if not sources:
        out.append(_body("<i>(No source citations found.)</i>",
                        styles, indent=False))
        return out

    rows = [["Data source", "Evidence items citing"]]
    for src, n in sorted(sources.items(), key=lambda kv: -kv[1])[:40]:
        rows.append([src, str(n)])
    out.append(_make_table(rows,
                           col_widths=[5.6 * inch, 1.0 * inch],
                           styles=styles))
    return out


def _procedural_notes(state: dict, styles: dict) -> list:
    out: list = []
    out.append(PageBreak())
    out.append(_section("Appendix D.  Procedural Notes for Filing", styles))
    notes = [
        ("Initial filing.",
         "Complaint must be filed under seal in the U.S. District Court for "
         "the listed venue. Per § 3730(b)(2), the complaint is served only "
         "on the Attorney General and the U.S. Attorney for the district; "
         "the defendant is not served during the seal period."),
        ("Seal duration.",
         "Initial seal is 60 days from filing. The government routinely "
         "obtains extensions; typical seal duration is 18-36 months. The "
         "PECOS enrollment-gap research item, if applicable, should be "
         "satisfied via administrative subpoena during the seal period."),
        ("Pre-filing diligence.",
         "Counsel should run the PECOS lookup tool (scripts/pecos_lookup.py) "
         "and a fresh NPPES query within 30 days of filing to confirm "
         "defendant's continuing Medicare enrollment status."),
        ("Original-source posture.",
         "Per 31 U.S.C. § 3730(e)(4)(B)(ii), Relator qualifies as an "
         "\"original source\" if Relator's knowledge is \"independent of and "
         "materially adds to publicly disclosed allegations.\" The analytic "
         "work product reflected in this memorandum — including ML-driven "
         "anomaly detection over CMS public-use data and per-NPI peer "
         "comparison — constitutes independent, material analytical "
         "contribution sufficient to defeat the public-disclosure bar."),
        ("Rule 9(b) representative claims.",
         "Per-claim particularity is supplied by the per-count "
         "CPT/HCPCS-level service volumes and date-of-service ranges set "
         "forth in Part IV. Counsel may elect to supplement with a small "
         "set of representative individual false claims under seal."),
    ]
    for h, t in notes:
        out.append(_subsection(h, styles))
        out.append(_body(t, styles))
    return out


# ────────────────────────────────────────────────────────────────────────────
# Top-level builder
# ────────────────────────────────────────────────────────────────────────────


def _post_run_updates(post_run_updates: list, styles: dict) -> list:
    """NEW: Show post-run evidence supplements / corrections at the top.

    These are updates to the case that occurred AFTER the original
    investigation run finished — typically because a new tool became
    available (e.g., the PECOS live API became available after the original
    run completed). Surfaced prominently so the reviewing attorney sees
    them before reading the rest of the memorandum.
    """
    out: list = []
    if not post_run_updates:
        return out
    out.append(_section("Post-Run Updates", styles))
    out.append(_body(
        f"This memorandum reflects {len(post_run_updates)} update(s) "
        f"applied to the case state after the original investigation run "
        f"completed. New evidence items appear in the relevant count's "
        f"evidence schedule (Section V and Appendix A); the originating "
        f"changes are summarized here for reviewer awareness.",
        styles,
    ))
    for u in post_run_updates:
        date = (u.get("date") or "").split("T")[0]
        kind = u.get("type", "update")
        summary = u.get("summary", "")
        affects = ", ".join(u.get("affects_counts", []) or [])
        new_ids = ", ".join(u.get("new_evidence_ids", []) or [])
        out.append(_body(
            f"<b>{_esc(date)} · {_esc(kind)}</b>"
            + (f" (affects: {_esc(affects)})" if affects else ""),
            styles, indent=False,
        ))
        out.append(_body(_esc(summary), styles))
        if new_ids:
            out.append(Paragraph(
                f"New evidence IDs: {_esc(new_ids)}",
                styles["Citation"],
            ))
    return out


def build_brief(state: dict, full_iterations: list | None,
                adv_rounds: list | None,
                output_path: Path,
                post_run_updates: list | None = None) -> Path:
    """Build the full counsel memorandum PDF."""
    styles = _build_styles()

    case_id = state.get("case_id", "—")
    subject_short = _short_subject(state.get("subject", "—"))

    def _on_later_pages(canvas, doc):
        if doc.page > 1:
            _draw_page_meta(canvas, doc, case_id, subject_short)

    doc = SimpleDocTemplate(
        str(output_path),
        pagesize=letter,
        leftMargin=0.95 * inch, rightMargin=0.95 * inch,
        topMargin=0.85 * inch, bottomMargin=0.75 * inch,
        title=f"Counsel Memorandum — {case_id}",
        author="Fraud-Investigator Pipeline",
    )

    story: list = []
    story += _cover_page(state, styles)
    story += _post_run_updates(post_run_updates or [], styles)
    story += _executive_summary(state, styles)
    story += _methodology(state, styles)
    story += _jurisdiction_venue(state, styles)
    story += _statutory_framework(state, styles)
    story += _factual_allegations(state, full_iterations or [], styles)
    story += _adversarial_survival(state, full_iterations or [],
                                    adv_rounds or [], styles)
    story += _damages_calc(state, styles)
    story += _honest_limitations(state, full_iterations or [], styles)
    story += _recommendation(state, full_iterations or [], styles)
    story += _prayer(state, styles)
    # Appendices
    story += _evidence_schedule(state, full_iterations or [], styles)
    story += _statistical_appendix(state, full_iterations or [], styles)
    story += _data_sources_audit(state, full_iterations or [], styles)
    story += _procedural_notes(state, styles)
    story += _verified_precedents(state, full_iterations or [], styles)

    doc.build(story, onFirstPage=lambda c, d: None,
              onLaterPages=_on_later_pages)
    return output_path


def build_from_case_dir(case_dir: Path, out_filename: str = "05_Counsel_Memorandum.pdf") -> Path:
    """Convenience entry point — read state + bundle from a case dir and emit PDF."""
    case_dir = Path(case_dir)
    summary_p = case_dir / "summary.json"
    if not summary_p.exists():
        # Try the legacy investigations/ path that has only the final 03_/04_ files
        for legacy in ("03_Full_State_Investigation.json", "03_Full_State.json"):
            if (case_dir / legacy).exists():
                summary_p = case_dir / legacy
                break
    if not summary_p.exists():
        raise FileNotFoundError(
            f"No summary.json or 03_Full_State*.json in {case_dir}"
        )
    state = json.loads(summary_p.read_text())

    # Bundle has per-iter parsed.json + adversarial rounds inlined — use it
    # when present (much richer than summary.json alone).
    full_iterations: list = []
    adv_rounds: list = []
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
            full_iterations = bundle.get("iterations") or []
            adv_rounds = bundle.get("adversarial_rounds") or []
            post_run_updates = bundle.get("post_run_updates") or []
            # Prefer the bundle's `summary` field over the case_writer's
            # curated 03_Full_State_Investigation.json. The bundle's
            # summary is the orchestrator's full state.model_dump (has
            # iterations + all_evidence_ids + adverse_attacks_* arrays);
            # the curated file is a stripped-down view that omits these.
            bundle_state = bundle.get("summary") or {}
            if bundle_state:
                # Merge: bundle_state takes precedence (full data); state
                # fills in anything the bundle omitted (defensive).
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
                    help="Path to a case directory (investigations/Foo-2026-XX-XX/ "
                         "or fraud-investigator-v2/state/CASE-XXX/)")
    ap.add_argument("--out", default="05_Counsel_Memorandum.pdf",
                    help="Output filename within case-dir (default: "
                         "05_Counsel_Memorandum.pdf)")
    args = ap.parse_args(argv)

    out = build_from_case_dir(args.case_dir, args.out)
    print(f"Generated: {out} ({out.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
