"""Schema + shape tests for the policy-corpus fetcher layer.

Phase 1 scope is deliberately narrow: no live fetchers yet, so these
tests exercise the normalization layer (fraudlib.ingest.extract.normalize) and
the corpus schema on the repo. Per-source fetchers add their own
live-network tests behind ``@pytest.mark.live`` in later phases.

Run from the repo root with:

    /opt/anaconda3/bin/python -m pytest tests/test_policy_fetchers.py -v
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from fraudlib.ingest.extract.chunking import Chunk
from fraudlib.ingest.extract.normalize import (
    ALLOWED_SOURCES,
    PolicyRecord,
    chunks_to_records,
    to_record,
    write_jsonl,
)

SCHEMA_PATH = Path("data/source/policy/policy_guidelines.schema.json")

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


# ---------------------------------------------------------------------------
# Schema <-> code invariant
# ---------------------------------------------------------------------------


def test_corpus_allowed_sources_match_schema() -> None:
    schema = json.loads(SCHEMA_PATH.read_text())
    schema_sources = set(schema["properties"]["source"]["enum"])
    assert schema_sources == ALLOWED_SOURCES, (
        "ALLOWED_SOURCES in fraudlib.ingest.extract.normalize must match the "
        "schema enum exactly — update both together."
    )


# ---------------------------------------------------------------------------
# to_record — required fields, hash, slug doc_id
# ---------------------------------------------------------------------------


def test_to_record_fills_computed_fields() -> None:
    rec = to_record(
        source="pim",
        title="Program Integrity Manual Ch. 3",
        section="3.2.1 Medical Review",
        text="contractors must document each denial with a rationale.",
        url="https://example.test/pim",
        fetch_date="2026-04-21",
    )
    assert isinstance(rec, PolicyRecord)
    assert rec.doc_id  # auto-generated
    assert _SHA256_RE.match(rec.content_hash)
    assert rec.effective_date == ""
    assert rec.applies_to_specialties == []
    assert rec.applies_to_codes == []
    assert rec.extra == {}


def test_to_record_rejects_unknown_source() -> None:
    with pytest.raises(ValueError, match="ALLOWED_SOURCES"):
        to_record(
            source="bogus",
            title="x",
            section="y",
            text="z",
            url="",
            fetch_date="2026-04-21",
        )


def test_to_record_rejects_empty_text() -> None:
    with pytest.raises(ValueError):
        to_record(
            source="pim",
            title="t",
            section="s",
            text="   ",
            url="",
            fetch_date="2026-04-21",
        )


def test_to_record_normalizes_whitespace() -> None:
    rec = to_record(
        source="pim",
        title="t",
        section="s",
        text="  multi\n  line\t text  ",
        url="",
        fetch_date="2026-04-21",
    )
    assert rec.text == "multi line text"


def test_to_record_override_doc_id() -> None:
    rec = to_record(
        source="statute",
        title="31 USC 3729",
        section="(a)(1)",
        text="a person who knowingly presents a false claim",
        url="https://example.test/usc",
        fetch_date="2026-04-21",
        doc_id="usc_31_3729_a_1",
    )
    assert rec.doc_id == "usc_31_3729_a_1"


# ---------------------------------------------------------------------------
# chunks_to_records — per-chunk doc_ids, skip empty
# ---------------------------------------------------------------------------


def test_chunks_to_records_assigns_suffixed_doc_ids() -> None:
    chunks = [
        Chunk(text="first section body", section="1 First", section_number="1"),
        Chunk(text="second section body", section="2 Second", section_number="2"),
        Chunk(text="   ", section="empty", section_number=""),  # dropped
    ]
    recs = chunks_to_records(
        chunks,
        source="pim",
        title="Pub 100-08",
        url="https://example.test/pim",
        fetch_date="2026-04-21",
        doc_id_root="pim_ch3",
    )
    assert len(recs) == 2
    assert recs[0].doc_id == "pim_ch3_0000"
    assert recs[1].doc_id == "pim_ch3_0001"


# ---------------------------------------------------------------------------
# write_jsonl — round-trip against schema
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# fetch_oig — pure-parse paths (no live network)
# ---------------------------------------------------------------------------


def test_fetch_oig_strip_html() -> None:
    from fraudlib.ingest.policy.fetch_oig import _strip_html
    raw = (
        '<p data-block-key="abc">OIG will review <b>Medicare Part B</b> payments '
        'for &ldquo;chronic care management&rdquo;.</p>'
    )
    clean = _strip_html(raw)
    assert "OIG will review" in clean
    assert "chronic care management" in clean
    assert "<" not in clean
    assert "&ldquo;" not in clean


def test_fetch_oig_is_cms_row() -> None:
    from fraudlib.ingest.policy.fetch_oig import _Row, _is_cms_row

    cms = _Row(
        number="A-01", title="t", type_="Project", series_number="",
        series_title="", narrative="n", report_number="", component="OAS",
        agencies="CMS", status="ACTIVE", announced_date="2024-06-01",
        financial_tags="", issue_tags="", target_tags="",
    )
    non_cms = _Row(
        number="A-02", title="t", type_="Project", series_number="",
        series_title="", narrative="n", report_number="", component="OAS",
        agencies="NIH", status="ACTIVE", announced_date="2024-06-01",
        financial_tags="", issue_tags="", target_tags="",
    )
    multi = _Row(
        number="A-03", title="t", type_="Project", series_number="",
        series_title="", narrative="n", report_number="", component="OAS",
        agencies="Medicare|HRSA", status="ACTIVE", announced_date="",
        financial_tags="", issue_tags="", target_tags="",
    )
    assert _is_cms_row(cms)
    assert not _is_cms_row(non_cms)
    assert _is_cms_row(multi)


def test_fetch_oig_parse_date() -> None:
    from fraudlib.ingest.policy.fetch_oig import _parse_date
    assert _parse_date("2024-06-01") == "2024-06-01"
    assert _parse_date("") == ""
    assert _parse_date("06/01/2024") == ""  # non-ISO — refuse rather than misdate


def test_fetch_oig_row_to_record_skips_short() -> None:
    from fraudlib.ingest.policy.fetch_oig import _Row, _row_to_record
    short = _Row(
        number="A-04", title="t", type_="Project", series_number="",
        series_title="", narrative="<p>tiny</p>", report_number="",
        component="OAS", agencies="CMS", status="ACTIVE",
        announced_date="2024-06-01", financial_tags="", issue_tags="",
        target_tags="",
    )
    assert _row_to_record(short, fetch_date="2026-04-21") is None


# ---------------------------------------------------------------------------
# fetch_statutes — metadata integrity (no live network)
# ---------------------------------------------------------------------------


def test_statute_specs_are_well_formed() -> None:
    from fraudlib.ingest.policy.fetch_statutes import STATUTES
    ids = {s.doc_id for s in STATUTES}
    assert {"usc_31_3729", "usc_42_1320a_7b", "usc_42_1395nn"} <= ids
    for s in STATUTES:
        assert s.url.startswith("https://www.govinfo.gov/")
        assert s.url.endswith(".htm")
        assert s.section.startswith("§")
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", s.effective_date)


# ---------------------------------------------------------------------------
# fetch_policy_pdfs — chapter spec integrity (no live network)
# ---------------------------------------------------------------------------


def test_pim_chapter_specs_are_well_formed() -> None:
    from fraudlib.ingest.policy.fetch_policy_pdfs import PIM_CHAPTERS
    assert len(PIM_CHAPTERS) >= 10
    chapters = [c.chapter for c in PIM_CHAPTERS]
    assert 3 in chapters  # Medical review / corrective action
    assert 12 in chapters  # CERT
    assert 13 in chapters  # LCDs
    assert 14 not in chapters  # Reserved — must be skipped
    for c in PIM_CHAPTERS:
        assert c.filename.endswith(".pdf")
        assert c.url.startswith("https://www.cms.gov/")
        assert c.doc_id_root == f"pim_ch{c.chapter:02d}"


def test_write_jsonl_round_trips(tmp_path: Path) -> None:
    rec = to_record(
        source="oig_workplan",
        title="OIG Work Plan 2024",
        section="Podiatry",
        text="review of nail debridement overutilization",
        url="https://example.test/oig",
        fetch_date="2026-04-21",
        applies_to_specialties=["Podiatry"],
        applies_to_codes=["11720"],
    )
    path = tmp_path / "out.jsonl"
    n = write_jsonl([rec], path, append=False)
    assert n == 1
    loaded = json.loads(path.read_text().splitlines()[0])
    for field in ("doc_id", "source", "title", "section", "text", "url",
                  "fetch_date", "content_hash"):
        assert field in loaded and loaded[field] != ""
    assert loaded["applies_to_specialties"] == ["Podiatry"]
    assert loaded["applies_to_codes"] == ["11720"]
