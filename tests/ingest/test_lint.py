"""Unit tests for fraudlib.labels.lint.lint().

Locks in the 2026-04-27 linter rewrite that dropped schema-bookkeeping
fails from 204/565 to 0/561 in the full corpus audit. Each test
documents one concrete invariant the linter enforces.
"""
from __future__ import annotations

import pytest

from fraudlib.labels.lint import (
    CASE_ID_RE,
    NPI_RE,
    PANEL_END_YEAR,
    PANEL_START_YEAR,
    TIER_A_KINDS,
    TIER_C_KINDS,
    lint,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _baseline_case(**overrides) -> dict:
    """A minimum lint-passing case dict for tests to override."""
    base = {
        "case_id": "DOJ-2017-ginkgo-oncology",
        "active_years": [2013, 2014, 2015, 2016],
        "outcome": {"type": "conviction", "date": "2017-08-15"},
        "defendants": [{
            "name": "Provider Ginkgo",
            "role": "physician",
            "npi": "1881604470",
            "npi_confidence": "high",
        }],
        "tier": "A",
        "sources": [{
            "url": "https://www.justice.gov/opa/pr/something",
            "kind": "doj_press_release",
            "accessed": "2026-04-27",
        }],
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# case_id slug pattern
# ---------------------------------------------------------------------------

class TestCaseId:
    @pytest.mark.parametrize("cid", [
        "DOJ-2017-ginkgo-oncology",            # standard hand-curated
        "LEIE-2024-jackson-anita-other",     # multi-segment
        "CL-2018-trotter-anti-kickbac",      # CL-prefixed slug
        "CL-cacd-2_20_cv_02538",             # CL-prefixed with underscores
        "CL-cand-3_20_cr_00375",             # district + docket-number style
        "CAMFCU-2025-canales-hospice",       # state MFCU prefix
    ])
    def test_valid_slugs(self, cid):
        assert CASE_ID_RE.match(cid), f"{cid!r} should match"

    @pytest.mark.parametrize("cid", [
        "",
        "lower-case-fail",            # no uppercase prefix
        "123-numeric-prefix-fail",    # prefix must be A-Z
        "DOJ",                         # no separator
        "DOJ-",                        # trailing separator only
    ])
    def test_invalid_slugs(self, cid):
        assert not CASE_ID_RE.match(cid), f"{cid!r} should not match"


# ---------------------------------------------------------------------------
# active_years range
# ---------------------------------------------------------------------------

class TestActiveYears:
    def test_in_range(self):
        case = _baseline_case(active_years=[2013, 2014, 2015])
        assert lint(case) == []

    def test_pre_2010_conduct_allowed(self):
        """Real cases (Wasserman, Small Smiles, etc.) have conduct
        starting 2003-2009. Linter must accept these."""
        case = _baseline_case(active_years=[2003, 2004, 2005, 2006])
        errs = lint(case)
        assert not any("active_years" in e for e in errs), errs

    def test_below_panel_start(self):
        case = _baseline_case(active_years=[1999, 2000])
        errs = lint(case)
        assert any("active_years" in e for e in errs)

    def test_above_panel_end(self):
        case = _baseline_case(active_years=[PANEL_END_YEAR + 1])
        errs = lint(case)
        assert any("active_years" in e for e in errs)

    def test_typo_year(self):
        """Typo like 1923 instead of 2023 should fail."""
        case = _baseline_case(active_years=[1923, 2014])
        errs = lint(case)
        assert any("active_years" in e for e in errs)

    def test_empty_active_years(self):
        case = _baseline_case(active_years=[])
        errs = lint(case)
        assert not any("active_years" in e for e in errs)


# ---------------------------------------------------------------------------
# outcome.date format
# ---------------------------------------------------------------------------

class TestOutcomeDate:
    @pytest.mark.parametrize("date", [
        "2017-08-15",  # full ISO
        "2017-08",     # year-month
        "2017",        # year only
    ])
    def test_accepted_date_formats(self, date):
        case = _baseline_case(outcome={"type": "conviction", "date": date})
        errs = lint(case)
        assert not any("outcome.date" in e for e in errs), errs

    @pytest.mark.parametrize("date", [
        "August 15, 2017",       # natural language
        "8/15/2017",              # US slash format
        "2017-13-01",             # invalid month
        "20170815",               # no separators
    ])
    def test_rejected_date_formats(self, date):
        case = _baseline_case(outcome={"type": "conviction", "date": date})
        errs = lint(case)
        assert any("outcome.date" in e for e in errs), errs

    def test_outcome_date_predates_active_years_is_OK(self):
        """The 2026-04-27 rewrite drops the predates check. Conduct can
        start years before formal indictment date, especially when
        active_years was repaired to recover pre-PUF onset."""
        case = _baseline_case(
            active_years=[2013, 2014, 2015],
            outcome={"type": "conviction", "date": "2010-01-01"},
        )
        assert lint(case) == []


# ---------------------------------------------------------------------------
# defendant NPI / confidence
# ---------------------------------------------------------------------------

class TestDefendant:
    def test_valid_high_conf_npi(self):
        case = _baseline_case()
        assert lint(case) == []

    def test_high_conf_without_npi(self):
        case = _baseline_case(defendants=[{
            "name": "Foo", "role": "physician", "npi": None, "npi_confidence": "high",
        }])
        errs = lint(case)
        assert any("confidence=high but no NPI" in e for e in errs)

    def test_corporate_na_with_npi(self):
        case = _baseline_case(defendants=[{
            "name": "Acme LLC", "role": "corporate",
            "npi": "1881604470", "npi_confidence": "corporate_na",
        }])
        errs = lint(case)
        assert any("corporate_na but has NPI" in e for e in errs)

    def test_invalid_npi_format(self):
        case = _baseline_case(defendants=[{
            "name": "Foo", "role": "physician",
            "npi": "12345", "npi_confidence": "high",
        }])
        errs = lint(case)
        assert any("not 10-digit" in e for e in errs)

    def test_pending_hunt_no_npi_ok(self):
        case = _baseline_case(defendants=[{
            "name": "Foo", "role": "physician",
            "npi": None, "npi_confidence": "pending_hunt",
        }])
        # pending_hunt without NPI is fine — we just couldn't find them
        errs = lint(case)
        assert not any("confidence=" in e for e in errs)


# ---------------------------------------------------------------------------
# Tier / source kind allow-lists
# ---------------------------------------------------------------------------

class TestTierSourceCoherence:
    @pytest.mark.parametrize("kind", sorted(TIER_A_KINDS))
    def test_all_tier_a_kinds_pass(self, kind):
        """Every kind in the allow-list should satisfy tier=A."""
        case = _baseline_case(
            tier="A",
            sources=[{"url": "https://example.com/x", "kind": kind, "accessed": "2026-04-27"}],
        )
        errs = lint(case)
        assert not any("tier=A" in e for e in errs), f"kind={kind!r}: {errs}"

    def test_tier_a_with_only_leie(self):
        """leie_only is NOT a Tier-A kind by itself."""
        case = _baseline_case(
            tier="A",
            sources=[{"url": "https://example.com/x", "kind": "leie_only", "accessed": "2026-04-27"}],
            defendants=[{
                "name": "Foo", "role": "physician",
                "npi": None, "npi_confidence": "corporate_na",
            }],
        )
        errs = lint(case)
        assert any("tier=A" in e for e in errs)

    def test_tier_c_rejects_leie_only(self):
        # LEIE was removed from the labels path (9acaca4, 2026-06-07 —
        # DOJ-only label policy): a case sourced ONLY by an LEIE match is
        # no longer an acceptable tier-C source kind. lint() must flag it.
        case = _baseline_case(
            tier="C",
            sources=[{"url": "https://example.com/x", "kind": "leie_only", "accessed": "2026-04-27"}],
        )
        errs = lint(case)
        assert any("tier=C" in e for e in errs), (
            "leie_only must NOT satisfy tier-C source requirements (LEIE is not a label source)"
        )

    def test_tier_c_accepts_real_kinds_too(self):
        case = _baseline_case(
            tier="C",
            sources=[{"url": "https://example.com/x", "kind": "doj_press_release", "accessed": "2026-04-27"}],
        )
        errs = lint(case)
        assert not any("tier=C" in e for e in errs)

    def test_empty_sources_fails(self):
        case = _baseline_case(sources=[])
        errs = lint(case)
        assert any("sources" in e for e in errs)


# (Removed: a corpus-wide smoke test that walked an archived case-centric
# JSONL. `lint()` is targeted at that older schema; the canonical corpus
# (`data/extracted/gold_set.jsonl`) has a different NPI-centric shape, so
# retargeting the smoke test would be a fiction. Per-case unit tests above
# still cover the linter's behavior.)
