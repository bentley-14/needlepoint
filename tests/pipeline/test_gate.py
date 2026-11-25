"""Tests for the gold_set gate (link 3) pure logic — outcome-cap + router.

The live pieces (_verify_npi → NPPES, billing surface → panels) are exercised
by the integration run, not here. These cover the deterministic decision logic.
Decision model: pipeline/docs/GOLD_SET_GATE.md.
"""
from __future__ import annotations

import pytest

import pandas as pd

import pipeline.scrapers.gold_set_builder as m
from pipeline.scrapers.gold_set_builder import (
    GoldSetBuilder,
    _cap_active_years,
    _outcome_year_of,
    _route_proposal,
    _split_name,
)


# ---------------------------------------------------------------------------
# outcome-cap (port of cap_active_years_by_outcome)
# ---------------------------------------------------------------------------


def test_cap_in_bounds_unchanged():
    capped, original, tag = _cap_active_years([2018, 2019], 2020)
    assert capped == [2018, 2019] and original is None and tag == "in_bounds"


def test_cap_truncates_overrun():
    capped, original, tag = _cap_active_years([2018, 2019, 2020, 2021], 2019)
    assert capped == [2018, 2019] and original == [2018, 2019, 2020, 2021]
    assert tag == "truncated_at_2019"


def test_cap_all_post_outcome_collapses_to_outcome_year():
    capped, original, tag = _cap_active_years([2020, 2021], 2018)
    assert capped == [2018] and original == [2020, 2021]
    assert tag == "all_post_outcome_collapsed_to_year"


def test_cap_no_outcome_date_left_unchanged():
    capped, original, tag = _cap_active_years([2018, 2019], None)
    assert capped == [2018, 2019] and original is None and tag == "no_outcome_date"


def test_outcome_year_parsing():
    assert _outcome_year_of("2024-06-11") == 2024
    assert _outcome_year_of(None) is None
    assert _outcome_year_of("") is None


def test_split_name():
    assert _split_name("Alex Quinn Example") == ("Alex", "Example")
    assert _split_name("") == ("", "")


# ---------------------------------------------------------------------------
# router — the four buckets. KEY RULE: NPI-confidence gates whether the surface
# verdict is trusted, so an uncertain NPI → HOLD regardless of surface.
# ---------------------------------------------------------------------------

def _route(**over):
    base = dict(
        npi="1900000015", exists=True, entity_type="NPI-1", name_match="exact",
        surface_verdict="SURFACE_FOUND", n_candidates=1,
        active_years_source="source_stated",
    )
    base.update(over)
    return _route_proposal(**base)


def test_accept_clean_individual():               # Kapok
    assert _route()[0] == "accept"


def test_accept_web_corroborated_years():         # Kapok (nudged): years found via web
    assert _route(active_years_source="web_corroborated")[0] == "accept"


def test_hold_inferred_years():                   # years guessed, no source
    decision, reason = _route(active_years_source="inferred")
    assert decision == "hold" and "active-years grounding" in reason


def test_hold_outcome_fallback_years():           # Kapok (default): billing/fallback, ungrounded
    decision, reason = _route(active_years_source="outcome_fallback")
    assert decision == "hold" and "active-years grounding" in reason


def test_hold_unknown_grounding():                # missing signal defaults to ungrounded → HOLD
    assert _route(active_years_source="unknown")[0] == "hold"
    assert _route(active_years_source=None)[0] == "hold"


def test_skip_org_npi():                          # Ruby Scott — definitive structural
    decision, reason = _route(entity_type="NPI-2")
    assert decision == "skip" and "organization" in reason


def test_skip_no_npi():
    decision, reason = _route(npi=None, exists=False)
    assert decision == "skip" and "no resolvable NPI" in reason


def test_skip_no_surface_with_confident_npi():    # Boxelder — confident NPI, genuinely no surface
    decision, reason = _route(surface_verdict="NO_SURFACE")
    assert decision == "skip" and "NO_SURFACE" in reason


def test_park_unvalidatable_with_confident_npi():  # recent fraud, panels not published yet
    assert _route(surface_verdict="UNVALIDATABLE_NO_PANELS")[0] == "park"


def test_hold_fuzzy_name_even_with_surface():     # uncertain NPI → HOLD before trusting surface
    decision, reason = _route(name_match="fuzzy")
    assert decision == "hold" and "name match" in reason


def test_hold_fuzzy_name_overrides_no_surface():  # Banyan's real case — the bug we fixed
    # fuzzy NPI + NO_SURFACE must be HOLD (find the right NPI), NOT a structural SKIP.
    decision, reason = _route(name_match="fuzzy", surface_verdict="NO_SURFACE")
    assert decision == "hold"


def test_hold_multiple_candidates():
    decision, reason = _route(n_candidates=3)
    assert decision == "hold" and "3 NPI candidates" in reason


# ---------------------------------------------------------------------------
# already-labeled dedupe (gate-level): a proposal for an NPI already in
# gold_set is a pure duplicate unless it brings NEW active years. Runs inside
# transform() — short-circuits the router because the NPI is already trusted.
# ---------------------------------------------------------------------------

def test_gold_year_index_unions_years_and_cases():
    rows = [
        {"npi": "111", "all_active_years": [2020, 2021], "case_id": "A"},
        {"npi": "111", "all_active_years": [2021, 2022], "case_id": "B"},
        {"npi": "", "all_active_years": [2099], "case_id": "X"},   # null NPI ignored
    ]
    idx = GoldSetBuilder._gold_year_index(rows)
    assert idx["111"] == ({2020, 2021, 2022}, ["A", "B"])
    assert "" not in idx


def _gate_one(proposal, gold_rows, monkeypatch, *, name_match="exact",
              entity_type="NPI-1", verdict="SURFACE_FOUND"):
    """Run transform() on a single proposal with NPPES + panels stubbed out."""
    monkeypatch.setattr(GoldSetBuilder, "_load_gold_rows", staticmethod(lambda: gold_rows))
    monkeypatch.setattr(GoldSetBuilder, "_verify_npi", staticmethod(lambda npi, first: {
        "exists": bool(npi), "entity_type": entity_type, "name_match": name_match,
        "enum_year": None, "nppes_specialty": None, "nppes_states": []}))
    import scripts.check_billing_surface as cbs
    monkeypatch.setattr(cbs, "load_surfaces", lambda base, years: ({}, set()))
    monkeypatch.setattr(cbs, "verdict_for", lambda npi, yrs, surf, np_: (verdict, {}, []))
    row = GoldSetBuilder().transform(pd.DataFrame([proposal])).iloc[0]
    return row["gate_decision"], row["gate_reason"]


def _proposal(npi="1900000015", years=(2021, 2022, 2023, 2024, 2025), name="Provider Kapok"):
    return {"defendant_name": name, "defendant_npi_candidates": [npi],
            "all_active_years": list(years), "outcome_date": None,
            "active_years_source": "source_stated", "source_url": "https://e.com/x"}


def test_dedupe_known_npi_no_new_years_skips(monkeypatch):       # Kapok (re-found)
    gold = [{"npi": "1900000015", "all_active_years": [2021, 2022, 2023, 2024, 2025],
             "case_id": "DOJ-2026-provider-kapok"}]
    decision, reason = _gate_one(_proposal(), gold, monkeypatch)
    assert decision == "skip" and "already_labeled" in reason
    assert "DOJ-2026-provider-kapok" in reason


def test_dedupe_known_npi_fuzzy_name_no_new_years_still_skips(monkeypatch):  # Provider Banyan (fuzzy first-name variant)
    # NPI already a confirmed label → trusted → the fuzzy NPPES name is moot.
    gold = [{"npi": "1900000025", "all_active_years": [2021, 2022, 2023, 2024, 2025],
             "case_id": "DOJ-2026-provider-banyan"}]
    decision, reason = _gate_one(
        _proposal(npi="1900000025", name="Provider Banyan"), gold, monkeypatch, name_match="fuzzy")
    assert decision == "skip" and "already_labeled" in reason


def test_dedupe_known_npi_new_years_surfaces_as_update(monkeypatch):
    # Curator finds an EARLIER fraud year than the existing row → potential
    # conduct-window extension → accept (merge will defer it as an UPDATE).
    gold = [{"npi": "1900000015", "all_active_years": [2022, 2023, 2024, 2025],
             "case_id": "DOJ-2026-provider-kapok"}]
    decision, reason = _gate_one(
        _proposal(years=(2019, 2022, 2023, 2024, 2025)), gold, monkeypatch)
    assert decision == "accept" and "new active years" in reason and "2019" in reason


def test_dedupe_unknown_npi_falls_through_to_router(monkeypatch):
    # NPI not in gold_set → normal routing applies (clean individual → accept).
    decision, reason = _gate_one(_proposal(npi="9999999999"), [], monkeypatch)
    assert decision == "accept" and "billing surface" in reason
