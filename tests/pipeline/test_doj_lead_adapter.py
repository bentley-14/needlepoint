"""Tests for pipeline.scrapers.doj_lead_adapter — curator_input + cache → Lead.

Uses tiny tmp fixtures so the test never depends on the real 33 MB warm cache
or the unmerged data branch.
"""
from __future__ import annotations

import json
from pathlib import Path

from pipeline.leads import make_lead_id
from pipeline.scrapers.doj_lead_adapter import doj_leads


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def _curator_row(url: str, *, gated: bool = True, **over) -> dict:
    row = {
        "url": url,
        "doc_id": make_lead_id(url),
        "title": "Doctor Convicted in Medicare Fraud Scheme",
        "published_date": "2026-05-22",
        "component": "Criminal Division",
        "uuid": "u-" + url[-4:],
        "matched_terms": "medicare|kickback",
        "passed_body_gate": gated,
        "body_chars": 1234,
    }
    row.update(over)
    return row


def _cache_row(url: str, body: str) -> dict:
    return {"url": url, "uuid": "u-" + url[-4:], "body": body}


def test_builds_lead_from_gated_row_joined_to_cache(tmp_path):
    url = "https://www.justice.gov/opa/pr/clinic-owner-convicted"
    ci = tmp_path / "curator_input.jsonl"
    cache = tmp_path / "cache.jsonl"
    _write_jsonl(ci, [_curator_row(url)])
    _write_jsonl(cache, [_cache_row(url, "<p>A Miami clinic owner billed Medicare for phantom claims.</p>")])

    leads = doj_leads(curator_input_path=ci, cache_path=cache)

    assert len(leads) == 1
    lead = leads[0]
    assert lead.provenance == "doj_direct"
    assert lead.primary_source_url == url
    assert lead.lead_id == make_lead_id(url)          # == the row's doc_id
    assert "phantom claims" in lead.raw_text
    assert "<p>" not in lead.raw_text                 # HTML stripped
    assert lead.published_date == "2026-05-22"
    assert lead.discovery_meta["matched_terms"] == ["medicare", "kickback"]


def test_gated_only_filter(tmp_path):
    u_ok = "https://e.com/gated"
    u_no = "https://e.com/ungated"
    ci = tmp_path / "ci.jsonl"
    cache = tmp_path / "cache.jsonl"
    _write_jsonl(ci, [_curator_row(u_ok, gated=True), _curator_row(u_no, gated=False)])
    _write_jsonl(cache, [_cache_row(u_ok, "<p>body one</p>"), _cache_row(u_no, "<p>body two</p>")])

    gated = doj_leads(curator_input_path=ci, cache_path=cache)
    assert {l.primary_source_url for l in gated} == {u_ok}

    both = doj_leads(curator_input_path=ci, cache_path=cache, gated_only=False)
    assert {l.primary_source_url for l in both} == {u_ok, u_no}


def test_skips_gated_row_with_no_cached_body(tmp_path, capsys):
    url = "https://e.com/missing-body"
    ci = tmp_path / "ci.jsonl"
    cache = tmp_path / "cache.jsonl"
    _write_jsonl(ci, [_curator_row(url)])
    _write_jsonl(cache, [])  # body not cached — the stale-cache situation

    leads = doj_leads(curator_input_path=ci, cache_path=cache)

    assert leads == []
    assert "no cached body" in capsys.readouterr().out


def test_url_join_is_normalization_insensitive(tmp_path):
    # curator_input has a trailing slash; cache stored it without — still joins.
    ci = tmp_path / "ci.jsonl"
    cache = tmp_path / "cache.jsonl"
    _write_jsonl(ci, [_curator_row("https://e.com/Case/")])
    _write_jsonl(cache, [_cache_row("https://e.com/case", "<p>matched anyway</p>")])

    leads = doj_leads(curator_input_path=ci, cache_path=cache)
    assert len(leads) == 1
    assert "matched anyway" in leads[0].raw_text
