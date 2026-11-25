"""Tests for format_run_digest — the human-readable weekly-cron email body."""
from __future__ import annotations

from pipeline.scrapers.gold_set_builder import format_run_digest


def _report():
    return {
        "counts": {"accept": 2, "gate_hold": 2, "park": 1, "skip": 3},
        "append": [{"npi": "2222222222", "defendant_name": "New Doc", "all_active_years": [2020, 2021]}],
        "update": [{"npi": "1111111111", "defendant_name": "Known Doc", "target_case_id": "DOJ-x",
                    "target_has_manual_overrides": True, "changes": {"outcome": {"from": None, "to": {}}}}],
        "merge_hold": [],
        "backup": "gold_set.jsonl.bak_pre_curator_merge_2026-06-04T0000",
    }


def _proposals():
    return [
        {"npi": "2222222222", "defendant_name": "New Doc", "defendant_specialty": "Cardiology",
         "defendant_state": "FL", "source_url": "https://e.com/new", "gate_decision": "accept"},
        {"npi": "1111111111", "defendant_name": "Known Doc", "source_url": "https://e.com/known",
         "gate_decision": "accept"},
        {"npi": "3333333333", "defendant_name": "Fuzzy Person", "defendant_specialty": "NP",
         "defendant_state": "UT", "all_active_years": [2021], "source_url": "https://e.com/fuzzy",
         "gate_decision": "hold", "gate_reason": "name match is 'fuzzy' (not exact/prefix) ..."},
        {"npi": "4444444444", "defendant_name": "Ungrounded Person", "defendant_specialty": "Pain",
         "defendant_state": "TX", "all_active_years": [2022], "source_url": "https://e.com/ung",
         "gate_decision": "hold", "gate_reason": "active-years grounding 'inferred' ..."},
        {"npi": "5555555555", "defendant_name": "Too Recent", "all_active_years": [2026],
         "gate_decision": "park"},
    ]


def test_digest_has_added_and_review_sections():
    d = format_run_digest(_report(), _proposals())
    assert "1 label(s) added, 4 need review" in d
    assert "ADDED TO gold_set  (1)" in d and "New Doc" in d
    assert "NEEDS REVIEW  (4)" in d


def test_digest_actions_are_specific():
    d = format_run_digest(_report(), _proposals())
    # update on a manual-override row is flagged + actionable
    assert "MANUAL overrides" in d and "confirm whether to apply this update" in d
    # fuzzy-name hold → find/confirm NPI
    assert "find/confirm the right NPI" in d
    # ungrounded-years hold → supply the conduct window
    assert "supply the documented conduct window" in d
    # park → re-check when panels publish
    assert "PARK" in d and "re-check when CMS publishes" in d
    # source links carried through for action
    assert "https://e.com/fuzzy" in d


def test_digest_quiet_run_renders_cleanly():
    empty = {"counts": {"accept": 0, "gate_hold": 0, "park": 0, "skip": 0}, "append": [], "update": [], "merge_hold": []}
    d = format_run_digest(empty, [])
    assert "0 label(s) added, 0 need review" in d
