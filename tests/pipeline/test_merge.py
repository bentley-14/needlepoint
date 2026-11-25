"""Tests for the gold_set merge identity classifier (link 4, dry-run).

Pure logic — no gold_set file, no network. Decision model / Fork-1:
pipeline/docs/GOLD_SET_GATE.md.
"""
from __future__ import annotations

from pipeline.scrapers.gold_set_builder import _classify_merge


def _gold(npi=None, years=None, source_url="", case_id="c"):
    return {"npi": npi, "all_active_years": years or [], "source_url": source_url, "case_id": case_id}


def _prop(npi="1900000015", years=None, source_url="https://e.com/a"):
    return {"npi": npi, "all_active_years": years or [2021, 2022], "source_url": source_url}


def test_append_brand_new_npi():
    action, idx, reason = _classify_merge(_prop(npi="9999999999"), gold_rows=[])
    assert action == "append" and idx is None and "new NPI" in reason


def test_append_new_npi_against_nonempty_goldset():
    gold = [_gold(npi="1111111111", years=[2018], source_url="https://e.com/other")]
    action, _, _ = _classify_merge(_prop(npi="2222222222"), gold)
    assert action == "append"


def test_update_via_source_url_resolves_unresolved_row():   # the Kapok case
    # existing row has NO npi but the same press release; proposal resolves the NPI.
    gold = [_gold(npi=None, years=[2013, 2014], source_url="https://justice.gov/x")]
    action, idx, reason = _classify_merge(
        _prop(npi="1900000069", years=[2017], source_url="https://justice.gov/x"), gold)
    assert action == "update" and idx == 0 and "unresolved" in reason


def test_update_via_npi_overlap_followup():
    gold = [_gold(npi="1900000015", years=[2021, 2022, 2023], source_url="https://e.com/indict")]
    action, idx, _ = _classify_merge(
        _prop(npi="1900000015", years=[2022, 2023], source_url="https://e.com/convict"), gold)
    assert action == "update" and idx == 0


def test_hold_known_npi_disjoint_years():                   # Sassafras-style follow-up / distinct case
    gold = [_gold(npi="1900000059", years=[2006, 2007, 2008], source_url="https://e.com/indict")]
    action, idx, reason = _classify_merge(
        _prop(npi="1900000059", years=[2019], source_url="https://e.com/convict"), gold)
    assert action == "hold" and idx is None and "distinct case" in reason


def test_hold_npi_matches_multiple_overlapping_rows():
    gold = [_gold(npi="1900000059", years=[2019]), _gold(npi="1900000059", years=[2019])]
    action, _, reason = _classify_merge(_prop(npi="1900000059", years=[2019]), gold)
    assert action == "hold" and "ambiguous" in reason


def test_hold_source_url_matches_multiple_rows():
    gold = [_gold(source_url="https://e.com/dup"), _gold(source_url="https://e.com/dup")]
    action, _, reason = _classify_merge(_prop(source_url="https://e.com/dup"), gold)
    assert action == "hold" and "human confirms" in reason


def test_append_new_defendant_from_partly_curated_release():
    # multi-defendant release: defendant A already resolved in gold_set; B is a
    # NEW defendant (new NPI) from the SAME release → must APPEND, not wrongly
    # UPDATE A's row (source_url is per-release, not per-defendant).
    gold = [_gold(npi="1111111111", years=[2015], source_url="https://e.com/ten-docs")]
    action, idx, reason = _classify_merge(
        _prop(npi="2222222222", years=[2015], source_url="https://e.com/ten-docs"), gold)
    assert action == "append" and idx is None


def test_source_url_normalization_insensitive():
    gold = [_gold(npi=None, years=[2015], source_url="https://e.com/Case/")]
    action, idx, _ = _classify_merge(
        _prop(npi="1234567890", years=[2015], source_url="https://e.com/case"), gold)
    assert action == "update" and idx == 0
