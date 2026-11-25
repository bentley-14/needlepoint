"""Innocent-explanation pre-screen (failure_mode_taxonomy_2026-07.md #2 / the
Ridge+Rowan repeat-decline lesson).

Hermetic — mocks ring_enrichment.enrich_ring, leie_lookup.lookup,
ca_exclusion_lookup._check_si_by_npi, r2.duckdb_r2, and anchor_check.check_anchor
so the test never touches R2, the LEIE parquet, or the network. Mirrors
tests/test_anchor_check.py's monkeypatch-the-seam style.

Run: pytest tests/test_innocent_explanation_prescreen.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import innocent_explanation_prescreen as P  # noqa: E402
import ring_enrichment  # noqa: E402
import leie_lookup  # noqa: E402
import ca_exclusion_lookup  # noqa: E402
import anchor_check  # noqa: E402
from fraudlib import r2  # noqa: E402


def _fake_ring(npi: str, ring_npis: list[str] | None = None, entity: str = "Test Clinic LLC") -> dict:
    ring_npis = ring_npis or []
    members = [{"npi": npi, "is_seed": True, "anomalous": True, "rank": 5, "pct": 0.001,
               "name": "SEED PROVIDER", "specialty": "Pain Management", "state": "GA", "pay": 500000.0}]
    members += [{"npi": n, "is_seed": False, "anomalous": True, "rank": 10, "pct": 0.002,
                "name": f"RING {n}", "specialty": "Pain Management", "state": "GA", "pay": 300000.0}
               for n in ring_npis]
    return {
        "seed_npi": npi, "seed": members[0], "panel_year": 2024, "panel_n": 200000,
        "entities": [{"kind": "reassignment_vehicle", "name": entity, "group_pac_id": "O123"}],
        "ring_members": members, "n_ring_members": len(members),
        "n_anomalous": sum(1 for m in members if m["anomalous"]),
        "ring_flag": len(ring_npis) >= 1,
        "collectible_entity": {"kind": "reassignment_vehicle", "name": entity, "group_pac_id": "O123"},
        "novelty": {"verdict": "no public case found — potentially NOVEL", "hits": []},
        "notes": [],
    }


def _no_anchor_result(hcpcs: str, mac=None, effective_on=None) -> dict:
    """A dominant-code anchor lookup with no LCD/NCD (context only, never gates)."""
    return {"hcpcs": hcpcs, "mac": mac, "effective_on": effective_on,
            "lcd_exists": False, "ncd_exists": False, "article_only": False,
            "lcds": [], "ncds": [], "articles": [], "per_se_candidates": []}


def _empty_duckdb_df(*_args, **_kwargs) -> pd.DataFrame:
    return pd.DataFrame(columns=["HCPCS_Cd", "Tot_Srvcs"])


class _FakeConExecute:
    def __init__(self, df: pd.DataFrame):
        self._df = df

    def df(self):
        return self._df


def _fake_con(df: pd.DataFrame):
    con = MagicMock()
    con.execute.return_value = _FakeConExecute(df)
    return con


# ─────────────────────────────────────────────────────────────────────────────
# (a) excluded provider fixture -> track == "file_track" with the excluded signal
# ─────────────────────────────────────────────────────────────────────────────

def test_excluded_provider_forces_file_track():
    npi = "1234567890"
    ring_npi = "1111111111"

    def fake_leie_lookup(n):
        if n == ring_npi:
            return {"npi": n, "excluded": True, "excldate": "20220315", "excltype": "1128a1",
                    "lastname": "DOE", "firstname": "JANE", "reinstated": None}
        return {"npi": n, "excluded": False}

    with patch.object(ring_enrichment, "enrich_ring", return_value=_fake_ring(npi, [ring_npi])), \
         patch.object(P, "ring_enrichment", ring_enrichment), \
         patch.object(leie_lookup, "lookup", side_effect=fake_leie_lookup), \
         patch.object(ca_exclusion_lookup, "_check_si_by_npi", return_value=[]), \
         patch.object(r2, "duckdb_r2", return_value=_fake_con(_empty_duckdb_df())), \
         patch.object(anchor_check, "check_anchor", side_effect=_no_anchor_result):
        result = P.screen(npi, 2024)

    assert result["track"] == "file_track"
    signal_types = [s["type"] for s in result["per_se_signals"]]
    assert "excluded_or_revoked_provider_billing" in signal_types
    excl_signal = next(s for s in result["per_se_signals"]
                       if s["type"] == "excluded_or_revoked_provider_billing")
    assert ring_npi in excl_signal["npis"]
    assert "20220315" in excl_signal["detail"]


def test_excluded_signal_notes_reinstatement_overlap():
    """When LEIE carries a reinstatement date, the detail should say so —
    the task asks us to note whether exclusion dates overlap the billing
    window; reinstated-before-billing is a materially different fact than
    'excluded, never reinstated'."""
    npi = "1234567890"

    def fake_leie_lookup(n):
        return {"npi": n, "excluded": True, "excldate": "20100101",
                "excltype": "1128b4", "reinstated": "20120101"}

    with patch.object(ring_enrichment, "enrich_ring", return_value=_fake_ring(npi)), \
         patch.object(P, "ring_enrichment", ring_enrichment), \
         patch.object(leie_lookup, "lookup", side_effect=fake_leie_lookup), \
         patch.object(ca_exclusion_lookup, "_check_si_by_npi", return_value=[]), \
         patch.object(r2, "duckdb_r2", return_value=_fake_con(_empty_duckdb_df())), \
         patch.object(anchor_check, "check_anchor", side_effect=_no_anchor_result):
        result = P.screen(npi, 2024)

    assert result["track"] == "file_track"
    excl_signal = next(s for s in result["per_se_signals"]
                       if s["type"] == "excluded_or_revoked_provider_billing")
    assert "reinstated 20120101" in excl_signal["detail"]


# ─────────────────────────────────────────────────────────────────────────────
# (b) clean high-volume necessity-shaped fixture -> ambiguous_needs_judgment,
#     no per-se signals (this is the Rowan/Pain Clinic Mango shape: G0483 +
#     99214 at real volume but nothing per-se — see referral_batch_2026-07.md)
# ─────────────────────────────────────────────────────────────────────────────

def test_clean_high_volume_provider_is_ambiguous_needs_judgment():
    npi = "1900000053"  # Rowan NPI, used illustratively — all lookups mocked

    def clean_leie(n):
        return {"npi": n, "excluded": False}

    # Normal-volume 99214 (no impossible-day trip): 3,000 svcs/yr (~12.5/working
    # day) implies 3000*30/240/60 = 6.25 hours/working-day — a realistic full
    # clinical day, well under the ~16h/day threshold.
    normal_df = pd.DataFrame([{"HCPCS_Cd": "99214", "Tot_Srvcs": 3000.0}])

    with patch.object(ring_enrichment, "enrich_ring",
                      return_value=_fake_ring(npi, ["2222222222"], entity="Pain Clinic Mango Clinic")), \
         patch.object(P, "ring_enrichment", ring_enrichment), \
         patch.object(leie_lookup, "lookup", side_effect=clean_leie), \
         patch.object(ca_exclusion_lookup, "_check_si_by_npi", return_value=[]), \
         patch.object(r2, "duckdb_r2", return_value=_fake_con(normal_df)), \
         patch.object(anchor_check, "check_anchor", side_effect=_no_anchor_result):
        result = P.screen(npi, 2024)

    assert result["track"] == "ambiguous_needs_judgment"
    assert result["per_se_signals"] == []
    assert any("chart-dependent" in n or "referral-track" in n for n in result["notes"])
    assert result["entity"] == "Pain Clinic Mango Clinic"


def test_data_source_failure_degrades_gracefully_not_a_crash():
    """Any single data-source failure reports {_error: ...} on that check and
    never crashes the whole screen — per the explicit degrade-gracefully spec."""
    npi = "1234567890"

    with patch.object(ring_enrichment, "enrich_ring", return_value=_fake_ring(npi)), \
         patch.object(P, "ring_enrichment", ring_enrichment), \
         patch.object(leie_lookup, "lookup", side_effect=RuntimeError("R2 unreachable")), \
         patch.object(ca_exclusion_lookup, "_check_si_by_npi", side_effect=RuntimeError("R2 unreachable")), \
         patch.object(r2, "duckdb_r2", side_effect=RuntimeError("no rclone config")), \
         patch.object(anchor_check, "check_anchor", side_effect=RuntimeError("lcd parquet missing")):
        result = P.screen(npi, 2024)  # must not raise

    assert result["track"] == "ambiguous_needs_judgment"
    assert result["per_se_signals"] == []
    assert result["notes"], "data-source failures must surface as notes, not silence"
    # No dominant code could be identified (the impossible-day PUF query itself
    # failed), so anchor_context short-circuits with a note rather than calling
    # the (also-mocked-to-fail) anchor_check — that's correct: nothing to anchor.
    assert result["anchor_context"]["code"] is None
    assert result["anchor_context"]["notes"]


# ─────────────────────────────────────────────────────────────────────────────
# (c) impossible-day math unit test — absurd volume flags, normal doesn't
# ─────────────────────────────────────────────────────────────────────────────

def test_impossible_day_flags_absurd_psychotherapy_volume():
    """90837 (60-min psychotherapy) at 50,000 svcs/yr implies 50000*60/240/60
    = 208.3 hours/working-day — nowhere near physically possible."""
    npi = "9999999999"
    absurd_df = pd.DataFrame([{"HCPCS_Cd": "90837", "Tot_Srvcs": 50000.0}])
    with patch.object(r2, "duckdb_r2", return_value=_fake_con(absurd_df)):
        result = P.check_impossible_day(npi, 2024)

    assert result["signal"] is not None
    assert result["signal"]["type"] == "impossible_day"
    assert "APPROXIMATE" in result["signal"]["detail"]
    per_code = result["per_code"][0]
    assert per_code["code"] == "90837"
    assert per_code["implied_hours_per_day"] > P._IMPOSSIBLE_DAY_HOURS_THRESHOLD


def test_impossible_day_does_not_flag_normal_volume():
    """90834 (45-min) at 2,000 svcs/yr implies 2000*45/240/60 = 6.25
    hours/working-day — well within a normal caseload, must NOT flag."""
    npi = "9999999999"
    normal_df = pd.DataFrame([{"HCPCS_Cd": "90834", "Tot_Srvcs": 2000.0}])
    with patch.object(r2, "duckdb_r2", return_value=_fake_con(normal_df)):
        result = P.check_impossible_day(npi, 2024)

    assert result["signal"] is None
    assert result["per_code"][0]["implied_hours_per_day"] < P._IMPOSSIBLE_DAY_HOURS_THRESHOLD


def test_impossible_day_query_failure_reports_error_not_crash():
    with patch.object(r2, "duckdb_r2", side_effect=RuntimeError("R2 down")):
        result = P.check_impossible_day("9999999999", 2024)

    assert result["signal"] is None
    assert result["notes"], "a PUF query failure must surface as a note, not raise"


def test_anchor_check_failure_reports_error_not_crash():
    """When a dominant code IS identified but anchor_check itself raises
    (e.g. LCD parquet missing), anchor_context must degrade to {_error: ...}
    rather than propagate the exception."""
    with patch.object(anchor_check, "check_anchor", side_effect=RuntimeError("lcd parquet missing")):
        result = P.anchor_context("99214")

    assert result["code"] == "99214"
    assert result["lcd_exists"] is None
    assert result.get("_error")


def test_implied_hours_per_day_math_is_exact():
    """Direct unit check of the formula: minutes * Tot_Srvcs / 240 working
    days / 60 = hours/day."""
    hours = P._implied_hours_per_day("99214", 4800.0)  # 30 min/unit
    assert hours == pytest.approx(4800.0 * 30 / 240 / 60)
