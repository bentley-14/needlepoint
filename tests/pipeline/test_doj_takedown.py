"""National Takedown scraper parsing + lead adapter (no network — HTML fixtures)."""
from __future__ import annotations

import json

from pipeline.scrapers.doj_takedown import (
    defendant_name,
    discover_takedown_year,
    parse_court_docs,
    segment_summaries,
)
from pipeline.scrapers.doj_takedown_lead_adapter import takedown_leads

# Minimal but structurally-faithful court-documents page: a federal table
# (USAO District) and a state table (State), each row linking a charging doc.
COURT_DOCS = """
<table>
  <tr><th>USAO District</th><th>Case Name</th><th>Civil/Criminal</th><th>Court Document</th></tr>
  <tr><td>DAZ</td><td>United States v. Alex Example</td><td>Criminal</td>
      <td><a href="/criminal/media/111/dl?inline">Indictment</a></td></tr>
  <tr><td>CDCA</td><td>United States v. Provider Quince, et al.</td><td>Criminal</td>
      <td><a href="https://www.justice.gov/criminal/media/222/dl?inline">Indictment</a></td></tr>
  <tr><td>NDTX</td><td>United States v. Jane Roe</td><td>Criminal</td>
      <td><a href="/criminal/media/444/dl?inline">Information</a></td></tr>
  <tr><td>NDTX</td><td>United States v. John Doe</td><td>Criminal</td>
      <td><a href="/criminal/media/555/dl?inline">Information</a></td></tr>
</table>
<table>
  <tr><th>State</th><th>Case Name</th><th>Civil/Criminal</th><th>Court Document</th></tr>
  <tr><td>Arizona</td><td>State of Arizona v. Clinic Beech</td><td>Criminal</td>
      <td><a href="/criminal/media/333/dl?inline">Indictment</a></td></tr>
</table>
"""

SUMMARIES = """
<div>District of Arizona</div>
<p>Alex Example, 38, of Glendale, Arizona, was charged by indictment with conspiracy
   to commit health care fraud involving a clinic that billed Arizona Medicaid over $44 million.</p>
<p>Provider Quince, and co-defendants wrote controlled-substance prescriptions to one
   another without a legitimate medical purpose over many months in California.</p>
<div>Northern District of Texas</div>
<p>Jane Roe and John Doe, both of Dallas, Texas, were charged with billing Medicare
   roughly $3 million for durable medical equipment that was never provided to patients.</p>
<div>State of Arizona</div>
<p>Clinic Beech billed for services not rendered totaling $2 million in false claims
   to the state Medicaid program over a multi-year scheme.</p>
"""


def test_defendant_name_strips_wrappers():
    assert defendant_name("United States v. Alex Example") == "Alex Example"
    assert defendant_name("United States v. Provider Quince, et al.") == "Provider Quince"
    assert defendant_name("State of Arizona v. Clinic Beech") == "Clinic Beech"
    assert defendant_name("Commonwealth of Puerto Rico v. Lab del Pueblo") == "Lab del Pueblo"


def test_parse_court_docs_tags_scope_and_absolutizes_urls():
    cases = parse_court_docs(COURT_DOCS)
    assert len(cases) == 5
    by_name = {c["case_name"]: c for c in cases}
    m = by_name["United States v. Alex Example"]
    assert m["scope"] == "federal" and m["jurisdiction"] == "DAZ" and m["doc_type"] == "Indictment"
    assert m["url"] == "https://www.justice.gov/criminal/media/111/dl?inline"   # relative → absolute
    # already-absolute href is preserved
    assert by_name["United States v. Provider Quince, et al."]["url"].endswith("/media/222/dl?inline")
    assert by_name["State of Arizona v. Clinic Beech"]["scope"] == "state"


def test_segment_summaries_slices_by_name_and_drops_fragments():
    cases = segment_summaries(SUMMARIES, parse_court_docs(COURT_DOCS))
    by_name = {c["case_name"]: c for c in cases}
    # Example's paragraph is captured (and not bleeding into Quince's).
    mtext = by_name["United States v. Alex Example"]["raw_text"]
    assert "Glendale" in mtext and "$44 million" in mtext and "Quince" not in mtext
    # Adjacent co-defendants: "Jane Roe and John Doe…" — Roe's slice is a tiny
    # fragment (< MIN_SUMMARY_CHARS) → dropped; Doe's slice carries the paragraph.
    assert by_name["United States v. Jane Roe"]["raw_text"] is None
    assert "durable medical equipment" in (by_name["United States v. John Doe"]["raw_text"] or "")


def test_discover_takedown_year():
    html = "a 2024-national-hcf-court-documents b 2025-national-hcf-court-documents"
    assert discover_takedown_year(html) == 2025
    assert discover_takedown_year("no links here") is None


def test_takedown_leads_builds_and_skips_summaryless(tmp_path):
    path = tmp_path / "takedown_cases.jsonl"
    rows = [
        {"url": "https://www.justice.gov/criminal/media/111/dl?inline",
         "case_name": "United States v. Alex Example", "scope": "federal",
         "jurisdiction": "DAZ", "civ_crim": "Criminal", "doc_type": "Indictment",
         "published_date": "2026-06-23", "raw_text": "Alex Example billed Medicaid $44 million."},
        {"url": "https://www.justice.gov/criminal/media/444/dl?inline",
         "case_name": "United States v. Jane Roe", "scope": "federal",
         "jurisdiction": "NDTX", "civ_crim": "Criminal", "doc_type": "Information",
         "published_date": "2026-06-23", "raw_text": None},          # no summary → skipped
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows))

    leads = takedown_leads(path)
    assert len(leads) == 1
    ld = leads[0]
    assert ld.provenance == "doj_takedown"
    assert ld.primary_source_url.endswith("/media/111/dl?inline")
    assert ld.title == "United States v. Alex Example"
    assert ld.discovery_meta["scope"] == "federal"
    assert ld.published_date == "2026-06-23"


def test_takedown_leads_empty_when_no_worklist(tmp_path):
    # No takedown discovered this run → adapter returns [], weekly job degrades cleanly.
    assert takedown_leads(tmp_path / "absent.jsonl") == []
