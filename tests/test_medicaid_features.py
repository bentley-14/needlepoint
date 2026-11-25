"""Tests for scripts/compute_medicaid_features.py.

Pure-local: small synthetic frames matching ``_fetch_year_raw()``'s output
contract. No T-MSIS parquet, no DuckDB, no network -- the whole point of
factoring ``_derive_features``/``_classify_service_class`` out of the SQL
fetch is that this suite never needs the real ~3GB source.

Covers:
- concentration math (single-code provider -> top1_code_share == code_hhi == 1.0)
- service_class derivation from the dominant HCPCS code
- within-service-class percentiles are CLASS-RELATIVE, not global (the core
  fix for the "single-service business isn't anomalous" false-positive)
- the data_reliability gate is applied per-row from provider_state
- yoy_growth null-safety (no prior year / prior pay == 0)
- cross-program block (medicare_also / cross_program_ratio) nullability
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from scripts.compute_medicaid_features import _classify_service_class, _derive_features


def _mk_raw(rows: list[dict], year: int = 2023) -> pd.DataFrame:
    """Build a synthetic raw frame matching ``_fetch_year_raw()``'s contract.

    Each dict may override any of the columns below; unset ones get a
    sensible default so every row is a valid single-code T1019 personal-care
    biller unless the test says otherwise.
    """
    defaults = {
        "medicaid_claim_lines": 100,
        "medicaid_n_hcpcs": 1,
        "top_hcpcs": "T1019",
        "prior_pay": np.nan,
        "provider_state": None,
        "medicare_partb_pay": np.nan,
    }
    built = []
    for i, r in enumerate(rows):
        row = {**defaults, **r}
        row.setdefault("npi", str(1_000_000_000 + i))
        # F2 contract additions: unless a test overrides them, a fixture row has
        # no negative-netting codes, so the positive basis IS the net total.
        row.setdefault("positive_total_pay", row.get("medicaid_total_pay", 0.0))
        row.setdefault("n_negative_rows", 0)
        row["year"] = year
        built.append(row)
    return pd.DataFrame(built)


# ---------------------------------------------------------------------------
# Concentration math
# ---------------------------------------------------------------------------

def test_single_code_concentration_is_one():
    """A provider whose entire paid $ sits in one HCPCS code -> share == hhi == 1.0."""
    raw = _mk_raw([
        {"medicaid_total_pay": 50_000.0, "top1_paid": 50_000.0,
         "sum_sq_paid": 50_000.0 ** 2, "top_hcpcs": "T1019"},
    ])
    out = _derive_features(raw, year=2023)
    assert out.loc[0, "top1_code_share"] == pytest.approx(1.0)
    assert out.loc[0, "code_hhi"] == pytest.approx(1.0)


def test_concentration_below_one_when_split_across_codes():
    """Two equal-paid codes -> share == 0.5, hhi == 0.5 (sanity check on the formula)."""
    raw = _mk_raw([
        {"medicaid_total_pay": 200.0, "top1_paid": 100.0,
         "sum_sq_paid": 100.0 ** 2 + 100.0 ** 2, "top_hcpcs": "T1019", "medicaid_n_hcpcs": 2},
    ])
    out = _derive_features(raw, year=2023)
    assert out.loc[0, "top1_code_share"] == pytest.approx(0.5)
    assert out.loc[0, "code_hhi"] == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# service_class derivation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("hcpcs,expected", [
    ("T1019", "personal_care"),
    ("T1020", "personal_care"),
    ("s5125", "personal_care"),  # case-insensitive
    ("T1021", "home_health"),
    ("G0151", "home_health"),
    ("T2042", "hospice"),
    ("Q5001", "hospice"),
    ("q5010", "hospice"),   # last hospice site-of-service code, case-insensitive
    ("Q5101", "other"),     # F5: biosimilar-drug Q-code -- NOT hospice
    ("Q5119", "other"),     # F5: another biosimilar
    ("A0427", "transport"),
    ("T2003", "transport"),    # v3: NEMT encounter/trip T-code
    ("t2005", "transport"),    # v3: stretcher van, case-insensitive
    ("T1000", "nursing_pdn"),  # v3: private-duty nursing
    ("T1002", "nursing_pdn"),  # v3: hourly RN services
    ("H2035", "behavioral"),   # v3: alcohol/drug treatment per hour
    ("H0020", "behavioral"),   # v3: methadone administration
    ("90837", "behavioral"),   # v3: psychotherapy 60 min (CPT)
    ("HN", "other"),           # v3: H-section MODIFIER, not a billing code
    ("S5105", "day_services"),  # v3: center-based day care
    ("T2021", "day_services"),  # v3: day-hab waiver per 15 min
    ("87491", "lab"),
    ("P9612", "lab"),
    ("99213", "other"),   # E&M code -- none of the buckets
    (None, "other"),
    ("", "other"),
])
def test_classify_service_class(hcpcs, expected):
    assert _classify_service_class(hcpcs) == expected


# ---------------------------------------------------------------------------
# Within-service-class percentiles: class-relative, not global
# ---------------------------------------------------------------------------

def test_pctile_in_class_median_concentration_not_flagged():
    """9 personal_care agencies all at top1_code_share=1.0 (the PCS norm) + 1 at 0.5.

    Being at the ubiquitous 100%-one-code concentration is the COMMON case in
    this class -- it must NOT rank near the top of class (that's the exact
    false-positive the within-class percentile exists to fix).
    """
    rows = [
        {"medicaid_total_pay": 100_000.0, "top1_paid": 100_000.0,
         "sum_sq_paid": 100_000.0 ** 2, "top_hcpcs": "T1019"}
        for _ in range(9)
    ]
    rows.append({"medicaid_total_pay": 100_000.0, "top1_paid": 50_000.0,
                  "sum_sq_paid": 2 * 50_000.0 ** 2, "top_hcpcs": "T1019", "medicaid_n_hcpcs": 2})
    out = _derive_features(_mk_raw(rows), year=2023)

    tied = out[out["top1_code_share"] == 1.0]["top1_code_share_pctile_in_class"]
    # 9/10 tied at the max -> average-rank percentile == 0.6, not near 1.0.
    assert all(v == pytest.approx(0.6) for v in tied), tied.tolist()

    low = out[out["top1_code_share"] == 0.5]["top1_code_share_pctile_in_class"].iloc[0]
    assert low == pytest.approx(0.1)


def test_pctile_in_class_is_class_relative_not_global():
    """The SAME top1_code_share=1.0 must rank differently depending on whether
    it's common (personal_care) or rare (lab) within its own service_class."""
    pc_rows = [
        {"medicaid_total_pay": 100_000.0, "top1_paid": 100_000.0,
         "sum_sq_paid": 100_000.0 ** 2, "top_hcpcs": "T1019"}
        for _ in range(9)
    ]
    pc_rows.append({"medicaid_total_pay": 100_000.0, "top1_paid": 50_000.0,
                     "sum_sq_paid": 2 * 50_000.0 ** 2, "top_hcpcs": "T1019", "medicaid_n_hcpcs": 2})
    lab_rows = [
        {"medicaid_total_pay": 100_000.0, "top1_paid": 20_000.0,
         "sum_sq_paid": 5 * 20_000.0 ** 2, "top_hcpcs": "87491", "medicaid_n_hcpcs": 5}
        for _ in range(9)
    ]
    lab_rows.append({"medicaid_total_pay": 100_000.0, "top1_paid": 100_000.0,
                      "sum_sq_paid": 100_000.0 ** 2, "top_hcpcs": "87491"})
    out = _derive_features(_mk_raw(pc_rows + lab_rows), year=2023)

    pc_full = out[(out["service_class"] == "personal_care") & (out["top1_code_share"] == 1.0)]
    lab_full = out[(out["service_class"] == "lab") & (out["top1_code_share"] == 1.0)]

    assert pc_full["top1_code_share_pctile_in_class"].iloc[0] == pytest.approx(0.6)
    assert lab_full["top1_code_share_pctile_in_class"].iloc[0] == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# data_reliability gate
# ---------------------------------------------------------------------------

def test_reliability_map_applied_per_state(monkeypatch):
    import scripts.compute_medicaid_features as cmf

    def fake_reliability(state, year):
        return {"NY": "unusable", "CA": "high_concern"}.get((state or "").upper(), "reliable")

    monkeypatch.setattr(cmf, "medicaid_reliability", fake_reliability)

    raw = _mk_raw([
        {"medicaid_total_pay": 10.0, "top1_paid": 10.0, "sum_sq_paid": 100.0, "provider_state": "NY"},
        {"medicaid_total_pay": 10.0, "top1_paid": 10.0, "sum_sq_paid": 100.0, "provider_state": "ca"},
        {"medicaid_total_pay": 10.0, "top1_paid": 10.0, "sum_sq_paid": 100.0, "provider_state": "TX"},
        {"medicaid_total_pay": 10.0, "top1_paid": 10.0, "sum_sq_paid": 100.0, "provider_state": None},
    ])
    out = cmf._derive_features(raw, year=2023)
    got = dict(zip(out["provider_state"].fillna("NONE"), out["data_reliability"]))
    assert got["NY"] == "unusable"
    assert got["ca"] == "high_concern"
    assert got["TX"] == "reliable"
    assert got["NONE"] == "reliable"


# ---------------------------------------------------------------------------
# yoy_growth null-safety
# ---------------------------------------------------------------------------

def test_yoy_growth_null_safety():
    raw = _mk_raw([
        {"medicaid_total_pay": 200.0, "top1_paid": 200.0, "sum_sq_paid": 200.0 ** 2,
         "prior_pay": np.nan},          # no prior-year row at all
        {"medicaid_total_pay": 200.0, "top1_paid": 200.0, "sum_sq_paid": 200.0 ** 2,
         "prior_pay": 0.0},             # prior year present but paid $0 (avoid inf)
        {"medicaid_total_pay": 200.0, "top1_paid": 200.0, "sum_sq_paid": 200.0 ** 2,
         "prior_pay": 100.0},           # normal doubling
    ])
    out = _derive_features(raw, year=2023)
    assert pd.isna(out.loc[0, "yoy_growth"])
    assert pd.isna(out.loc[1, "yoy_growth"])
    assert out.loc[2, "yoy_growth"] == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# Cross-program block
# ---------------------------------------------------------------------------

def test_cross_program_ratio_and_medicare_also_when_partb_available():
    """When the Part-B panel IS available, a join miss is a CONFIRMED 'no Part-B
    billing' -- medicare_also=False (not null) and cross_program_ratio is still
    computable (1.0 = 100% Medicaid), not suppressed just because of the miss."""
    raw = _mk_raw([
        {"medicaid_total_pay": 300.0, "top1_paid": 300.0, "sum_sq_paid": 300.0 ** 2,
         "medicare_partb_pay": 100.0},
        {"medicaid_total_pay": 300.0, "top1_paid": 300.0, "sum_sq_paid": 300.0 ** 2,
         "medicare_partb_pay": np.nan},
    ])
    out = _derive_features(raw, year=2023, partb_available=True)
    assert out.loc[0, "medicare_also"] == True  # noqa: E712
    assert out.loc[0, "cross_program_ratio"] == pytest.approx(300.0 / 400.0)
    assert out.loc[1, "medicare_also"] == False  # noqa: E712 -- confirmed miss, not unknown
    assert out.loc[1, "cross_program_ratio"] == pytest.approx(1.0)  # Medicaid-only, confirmed


def test_cross_program_block_null_when_partb_unavailable():
    """When the year's Part-B panel isn't present at all (the build's escape
    hatch), medicare_also/cross_program_ratio must be null regardless of any
    per-row medicare_partb_pay value -- genuinely unknown, not a confirmed miss."""
    raw = _mk_raw([
        {"medicaid_total_pay": 300.0, "top1_paid": 300.0, "sum_sq_paid": 300.0 ** 2,
         "medicare_partb_pay": np.nan},
    ])
    out = _derive_features(raw, year=2023, partb_available=False)
    assert pd.isna(out.loc[0, "medicare_also"])
    assert pd.isna(out.loc[0, "cross_program_ratio"])


# ---------------------------------------------------------------------------
# Output schema contract
# ---------------------------------------------------------------------------

def test_derive_features_output_schema():
    raw = _mk_raw([{"medicaid_total_pay": 10.0, "top1_paid": 10.0, "sum_sq_paid": 100.0}])
    out = _derive_features(raw, year=2023)
    expected_cols = {
        "npi", "year", "medicaid_total_pay", "medicaid_claim_lines", "medicaid_n_hcpcs",
        "top_hcpcs", "service_class", "top1_code_share", "code_hhi", "yoy_growth",
        "n_negative_rows",
        "top1_code_share_pctile_in_class", "code_hhi_pctile_in_class",
        "medicaid_total_pay_pctile_in_class", "yoy_growth_pctile_in_class",
        "top1_code_share_pctile_in_class_state", "code_hhi_pctile_in_class_state",
        "medicaid_total_pay_pctile_in_class_state", "yoy_growth_pctile_in_class_state",
        "pctile_in_class_state_low_n",
        "provider_state", "data_reliability", "medicare_also", "medicare_partb_pay",
        "cross_program_ratio", "feature_frame_version",
    }
    assert set(out.columns) == expected_cols


# ---------------------------------------------------------------------------
# F2: positive-paid concentration basis (negative adjustment/reversal codes)
# ---------------------------------------------------------------------------

def test_negative_reversal_codes_dont_break_bounds():
    """A provider with reversal codes netting negative: net total (50) is SMALLER
    than the positive basis (100). Under the old net-denominator math,
    top1_code_share would be 60/50 = 1.2 -- the audit's F2 bug. The positive
    basis bounds both in [0,1] and the diagnostic counts the negative codes."""
    raw = _mk_raw([{
        "medicaid_total_pay": 50.0,          # true net (kept as-is)
        "positive_total_pay": 100.0,         # positive-only basis
        "top1_paid": 60.0,
        "sum_sq_paid": 60.0 ** 2 + 40.0 ** 2,
        "n_negative_rows": 2,
    }])
    out = _derive_features(raw, year=2023)
    assert out.loc[0, "top1_code_share"] == pytest.approx(0.6)
    assert out.loc[0, "code_hhi"] == pytest.approx((60.0 ** 2 + 40.0 ** 2) / 100.0 ** 2)
    assert out.loc[0, "n_negative_rows"] == 2
    assert out.loc[0, "medicaid_total_pay"] == pytest.approx(50.0)  # net stays net


def test_all_negative_provider_no_blowup():
    """Every code nets <=0 -> no positive basis -> both concentrations are 0.0
    (no signal), never a division error or a value outside [0,1]."""
    raw = _mk_raw([{
        "medicaid_total_pay": -25.0,
        "positive_total_pay": 0.0,
        "top1_paid": np.nan,
        "sum_sq_paid": 0.0,
        "n_negative_rows": 3,
    }])
    out = _derive_features(raw, year=2023)
    assert out.loc[0, "top1_code_share"] == 0.0
    assert out.loc[0, "code_hhi"] == 0.0
    assert out.loc[0, "n_negative_rows"] == 3


def test_clamp_unit_interval_guard():
    """The belt-and-braces clamp: out-of-range values are clipped (and only
    warned about), NaN passes through untouched."""
    import scripts.compute_medicaid_features as cmf
    col = pd.Series([-0.2, 0.5, 1.7, np.nan])
    out = cmf._clamp_unit_interval(col, "top1_code_share", 2023)
    assert out.tolist()[:3] == [0.0, 0.5, 1.0]
    assert pd.isna(out.iloc[3])


# ---------------------------------------------------------------------------
# F1: within-(service_class, provider_state) percentiles + low-N flag
# ---------------------------------------------------------------------------

def _two_state_class_rows():
    """25 TX + 25 CA personal-care rows. CA pays dwarf TX pays, so the top TX
    provider is nationally mid-pack but the extreme within its own state --
    exactly the state-saturation pattern audit F1 says national pooling hides."""
    tx = [
        {"medicaid_total_pay": float(i * 1_000), "top1_paid": float(i * 1_000),
         "sum_sq_paid": float(i * 1_000) ** 2, "provider_state": "TX"}
        for i in range(1, 26)
    ]
    ca = [
        {"medicaid_total_pay": float(100_000 + i * 10_000), "top1_paid": float(100_000 + i * 10_000),
         "sum_sq_paid": float(100_000 + i * 10_000) ** 2, "provider_state": "CA"}
        for i in range(1, 26)
    ]
    return tx + ca


def test_state_pctile_surfaces_state_extreme_hidden_nationally():
    out = _derive_features(_mk_raw(_two_state_class_rows()), year=2023)
    top_tx = out[(out["provider_state"] == "TX")].sort_values("medicaid_total_pay").iloc[-1]
    # Nationally: rank 25 of 50 -> 0.5. Within TX: rank 25 of 25 -> 1.0.
    assert top_tx["medicaid_total_pay_pctile_in_class"] == pytest.approx(0.5)
    assert top_tx["medicaid_total_pay_pctile_in_class_state"] == pytest.approx(1.0)
    assert bool(top_tx["pctile_in_class_state_low_n"]) is False


def test_state_pctile_low_n_cell_gets_nan_and_flag():
    """A (class, state) cell below MIN_STATE_CLASS_N must NOT get a percentile
    (3 providers can't define a distribution) -- NaN + flag instead."""
    rows = _two_state_class_rows()
    rows += [
        {"medicaid_total_pay": float(5_000 * i), "top1_paid": float(5_000 * i),
         "sum_sq_paid": float(5_000 * i) ** 2, "provider_state": "FL"}
        for i in (1, 2, 3)
    ]
    out = _derive_features(_mk_raw(rows), year=2023)
    fl = out[out["provider_state"] == "FL"]
    assert fl["medicaid_total_pay_pctile_in_class_state"].isna().all()
    assert fl["pctile_in_class_state_low_n"].all()
    tx = out[out["provider_state"] == "TX"]
    assert tx["medicaid_total_pay_pctile_in_class_state"].notna().all()
    assert not tx["pctile_in_class_state_low_n"].any()


def test_state_pctile_missing_state_gets_nan_and_flag():
    rows = _two_state_class_rows()
    rows.append({"medicaid_total_pay": 7_777.0, "top1_paid": 7_777.0,
                 "sum_sq_paid": 7_777.0 ** 2, "provider_state": None})
    out = _derive_features(_mk_raw(rows), year=2023)
    no_state = out[out["provider_state"].isna()]
    assert no_state["medicaid_total_pay_pctile_in_class_state"].isna().all()
    assert no_state["pctile_in_class_state_low_n"].all()


# ---------------------------------------------------------------------------
# F3: deterministic top-code tie-break (SQL level, in-memory DuckDB)
# ---------------------------------------------------------------------------

def test_top_hcpcs_tie_break_is_deterministic(tmp_path):
    """Two codes with EXACTLY equal paid $ -- top_hcpcs must be the
    lexicographically smaller code, independent of row insertion order."""
    import duckdb
    import scripts.compute_medicaid_features as cmf

    rows = [
        ("1234567890", "2023-01", "T1019", 500.0, 10),
        ("1234567890", "2023-02", "S5125", 500.0, 10),   # exact tie with T1019
    ]
    tops = []
    for ordering in (rows, rows[::-1]):
        con = duckdb.connect()
        con.execute("""CREATE TABLE t (BILLING_PROVIDER_NPI_NUM VARCHAR,
                       CLAIM_FROM_MONTH VARCHAR, HCPCS_CODE VARCHAR,
                       TOTAL_PAID DOUBLE, TOTAL_CLAIM_LINES BIGINT)""")
        con.executemany("INSERT INTO t VALUES (?,?,?,?,?)", ordering)
        raw = cmf._fetch_year_raw(con, "t", 2023,
                                  nppes_path=tmp_path / "missing_nppes.parquet",
                                  partb_path=None)
        tops.append(raw.loc[0, "top_hcpcs"])
    assert tops[0] == tops[1] == "S5125"   # code_paid DESC, hcpcs ASC


# ---------------------------------------------------------------------------
# F6: lab prefix rules gated on the HCPCS reference tables
# ---------------------------------------------------------------------------

def test_lab_gate_rejects_placeholder_codes(monkeypatch):
    import scripts.compute_medicaid_features as cmf
    monkeypatch.setattr(cmf, "_valid_hcpcs_codes", lambda: frozenset({"87491", "P9612"}))
    assert cmf._classify_service_class("87491") == "lab"
    assert cmf._classify_service_class("P9612") == "lab"
    assert cmf._classify_service_class("8888888") == "other"   # T-MSIS placeholder
    assert cmf._classify_service_class("PIN02") == "other"     # T-MSIS placeholder


def test_lab_gate_fails_open_without_reference_tables(monkeypatch):
    """If neither reference table is readable the gate must not break the
    build -- shape-only rule applies (documented fail-open)."""
    import scripts.compute_medicaid_features as cmf
    monkeypatch.setattr(cmf, "_valid_hcpcs_codes", lambda: frozenset())
    assert cmf._classify_service_class("8888888") == "lab"


# ---------------------------------------------------------------------------
# Feature-frame version stamp
# ---------------------------------------------------------------------------

def test_feature_frame_version_stamped():
    import scripts.compute_medicaid_features as cmf
    raw = _mk_raw([{"medicaid_total_pay": 10.0, "top1_paid": 10.0, "sum_sq_paid": 100.0}])
    out = _derive_features(raw, year=2023)
    assert (out["feature_frame_version"] == cmf.FEATURE_FRAME_VERSION).all()
    # version-agnostic: the stamp must exist and follow the vN convention
    # (pinning the literal made this test stale on every legitimate bump)
    assert cmf.FEATURE_FRAME_VERSION.startswith("v")
