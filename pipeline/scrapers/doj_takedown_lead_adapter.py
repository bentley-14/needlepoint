"""Takedown adapter — ``takedown_cases.jsonl`` rows → :class:`Lead`s.

The thin per-source shim for the takedown discovery path (link 1b), peer to
:mod:`pipeline.scrapers.doj_lead_adapter`. The scraper
(:mod:`pipeline.scrapers.doj_takedown`) already wrote one self-contained row per
case — the case-summary paragraph lives in the row itself (``raw_text``), so
unlike the press-release adapter there's no separate body cache to join.

``provenance="doj_takedown"``. ``primary_source_url`` is the case's charging-PDF
URL (the authoritative citation); ``raw_text`` is the summary the curator
extracts from. Rows without a usable summary (no name match / co-defendant
fragment dropped by the scraper's length guard) are **skipped** — a Lead needs
non-empty ``raw_text``. All takedown-format knowledge stops here; everything
downstream (curator, gate, merge, the curated ledger + spend cap in
``run_once``) sees only ``Lead``s, identical to the DOJ-direct path.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

from pipeline.leads import Lead, make_lead_id
from pipeline.scrapers.doj_takedown import TAKEDOWN_DIR

# The scraper's per-case worklist (its ``_write`` output name + dir).
TAKEDOWN_CASES_PATH = TAKEDOWN_DIR / "takedown_cases.jsonl"

PROVENANCE = "doj_takedown"


def takedown_leads(cases_path: Path = TAKEDOWN_CASES_PATH) -> list[Lead]:
    """Build :class:`Lead`s from the takedown case worklist.

    A row with no ``raw_text`` (case-summary not matched, or dropped as too
    short) is skipped with a tally — the court-doc PDF alone isn't worth a
    curator turn. Returns ``[]`` when the worklist is absent (no takedown
    discovered this run), so the weekly job degrades cleanly to press-release
    leads only.
    """
    leads: list[Lead] = []
    skipped_no_summary = 0
    for row in _read_jsonl(cases_path):
        url = (row.get("url") or "").strip()
        raw_text = (row.get("raw_text") or "").strip()
        if not url or not raw_text:
            skipped_no_summary += 1
            continue
        leads.append(
            Lead(
                lead_id=make_lead_id(url),
                provenance=PROVENANCE,
                primary_source_url=url,
                raw_text=raw_text,
                title=(row.get("case_name") or "").strip(),
                published_date=row.get("published_date"),
                discovery_meta={
                    "scope": row.get("scope"),                 # federal | state
                    "jurisdiction": row.get("jurisdiction"),
                    "civ_crim": row.get("civ_crim"),
                    "doc_type": row.get("doc_type"),
                },
            )
        )
    print(
        f"[doj_takedown_lead_adapter] {len(leads)} lead(s) "
        f"({skipped_no_summary} row(s) skipped — no usable summary)",
        flush=True,
    )
    return leads


def _read_jsonl(path: Path) -> Iterator[dict]:
    path = Path(path)
    if not path.exists():
        return
    with path.open() as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)
