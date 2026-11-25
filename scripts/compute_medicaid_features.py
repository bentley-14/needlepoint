"""Per-(NPI, year) Medicaid T-MSIS feature builder -- all states.

Reads ``data/raw/medicaid/provider_spending_full.parquet`` (or the indexed
``.duckdb`` mirror when present; see ``make pull-medicaid``), aggregates via
DuckDB SQL (never loads the ~238M-row / ~3GB source into pandas), and emits
one parquet per year at ``data/processed/medicaid_features_{year}.parquet``.

This is the feature LAYER ONLY:
no ranker wiring, no loop wiring, no Type-2/entity work. Purely additive.

Feature groups
--------------
Core volume:
    medicaid_total_pay, medicaid_claim_lines, medicaid_n_hcpcs

Concentration (the Red Sun signal -- computed on paid-dollar share, same
convention as ``compute_hcpcs_rich_features.py``'s ``hcpcs_revenue_hhi``):
    top1_code_share, code_hhi, top_hcpcs

Trajectory:
    yoy_growth (this year's paid $ / prior year's; null if no prior-year row)

Class:
    service_class -- personal_care / home_health / hospice / transport / lab /
    other, derived from the dominant (top-paid) HCPCS code family. First-pass
    HCPCS-prefix heuristic (see ``_classify_service_class``), NOT an
    authoritative CMS taxonomy.

Within-service-class percentiles (the fix for a competitor's dossier fault: a
single-code PCS agency isn't anomalous among ITS peers -- only concentration
ABOVE the class norm is a signal):
    top1_code_share_pctile_in_class, code_hhi_pctile_in_class,
    medicaid_total_pay_pctile_in_class, yoy_growth_pctile_in_class

Data-quality gate (fraudlib/medicaid_quality.py):
    provider_state (via NPPES bulk NPI->state; see NPPES_PATH), data_reliability

Cross-program block (best-effort; see the module docstring's TODO below):
    medicare_also, medicare_partb_pay, cross_program_ratio

Cross-program TODO
-------------------
Joins to ``data/processed/cms_partb_{year}.parquet`` (the by-provider Part-B
PUF, ``Tot_Mdcr_Pymt_Amt`` column) for years where that file is present
locally. Per the build spec's explicit escape hatch: if a year's Part-B panel
isn't present, ``medicare_also``/``medicare_partb_pay``/``cross_program_ratio``
are emitted as null for that year rather than blocking the Medicaid build --
pull it via ``make pull-data`` (or a targeted rclone copy of
``cms_partb_<year>.parquet`` from ``r2:needlepoint-data/processed/v1``) to backfill.

Feature-frame version (bumped 2026-07-16, see FEATURE_FRAME_VERSION below)
----------------------------------------------------------------------------
``feature_frame_version`` is stamped onto every row so P4 (the org scoring
head this feeds) can tell a v2 frame (audit fixes F1/F2/F3/F5/F6) from a v1
frame. v1 -> v2 changes:

- F1: ``*_pctile_in_class_state`` columns added alongside the existing
  national ``*_pctile_in_class`` ones -- the national percentile pools across
  all states, which the audit showed is unsafe for personal_care/hospice
  (a handful of states already set the class "normal"). Cells with fewer than
  ``MIN_STATE_CLASS_N`` (service_class, provider_state) rows -- or a missing
  provider_state -- get NaN + ``pctile_in_class_state_low_n=True`` rather than
  a falsely precise percentile off a handful of providers.
- F2: ``top1_code_share``/``code_hhi`` are now computed on the POSITIVE-paid
  subset of a provider's codes (adjustment/reversal codes that net negative
  for the year are excluded from this basis, not from ``medicaid_total_pay``
  itself, which stays the true net) -- structurally bounds both in [0,1]
  instead of only clamping after the fact. Dropped-code counts are exposed via
  the new ``n_negative_rows`` diagnostic. A belt-and-braces ``.clip(0, 1)`` +
  warning-on-fire still runs on ``top1_code_share``/``code_hhi``/
  ``cross_program_ratio`` (the last one inherits the same root cause, F7).
- F3: ``top_hcpcs`` (and therefore ``service_class``) now has an explicit,
  deterministic tie-break (``code_paid DESC, hcpcs ASC``) instead of a bare
  ``ARG_MAX``, which had none and rode DuckDB's non-guaranteed parallel
  floating-point summation order across runs.
- F5/F6: ``HOSPICE_CODES`` now lists Q5001-Q5010 explicitly instead of a
  blanket ``Q5`` prefix (which also caught CMS's unrelated Q5101-Q5146
  biosimilar-drug codes); the ``lab`` prefix rules (P-codes, 5-digit codes
  starting with "8") are gated on membership in the in-repo HCPCS reference
  tables so non-standard placeholder values in the raw T-MSIS feed (e.g.
  "8888888", "PIN02") fall through to ``other`` instead of ``lab``.

Usage:
    python scripts/compute_medicaid_features.py --year 2023
    python scripts/compute_medicaid_features.py --all-years
    python scripts/compute_medicaid_features.py --year 2023 --dry-run

Output -> ``data/processed/medicaid_features_{year}.parquet``
"""
from __future__ import annotations

import argparse
import functools
import logging
import sys
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from fraudlib.medicaid_quality import medicaid_reliability  # noqa: E402

LOG = logging.getLogger("compute_medicaid_features")

DUCKDB_PATH = REPO_ROOT / "data/raw/medicaid/provider_spending.duckdb"
PARQUET_PATH = REPO_ROOT / "data/raw/medicaid/provider_spending_full.parquet"
NPPES_PATH = REPO_ROOT / "data/processed/nppes_bulk.parquet"
PARTB_DIR = REPO_ROOT / "data/processed"  # cms_partb_<year>.parquet (by-provider PUF)
OUTPUT_DIR = REPO_ROOT / "data/processed"

# HCPCS reference tables used to validate the `lab` service_class prefix
# rules (F6 in medicaid_features_audit_2026-07-12.md). hcpcs_level2_full
# covers HCPCS-II (alpha-prefixed) codes; hcpcs_codes additionally covers
# CPT (5-digit numeric) codes -- the union is needed since T-MSIS lab
# billing spans both.
HCPCS_REFERENCE_TABLES = (
    REPO_ROOT / "data/ground_truth/hcpcs_level2_full.parquet",
    REPO_ROOT / "data/ground_truth/hcpcs_codes.parquet",
)

# Feature-frame version stamped onto every output row (see the module
# docstring's "Feature-frame version" section) -- bumped v1 -> v2 by the
# medicaid_features_audit_2026-07-12.md fixes (F1/F2/F3/F5/F6); v2 -> v3 by
# the 2026-07-16 service-class widening (behavioral/day_services/nursing_pdn
# + NEMT T-codes), which re-partitions the within-class percentile cohorts.
FEATURE_FRAME_VERSION = "v3"

# T-MSIS coverage years (mirrors scripts/medicaid_lookup.py::DEFAULT_YEARS).
YEARS = [2018, 2019, 2020, 2021, 2022, 2023, 2024]

# F1: minimum (service_class, provider_state) cell size to trust a
# state-stratified percentile; audit doesn't pin an exact threshold (F1's
# "For labeling" note asks only for the columns to exist), so this follows
# the build spec's example floor. Smaller cells get NaN + a low-N flag
# (`pctile_in_class_state_low_n`) instead of a percentile off a handful of
# providers.
MIN_STATE_CLASS_N = 20

# Filter to real provider NPIs (10-digit, starts with 1 or 2) -- excludes the
# ~3.3% NULL billing NPIs (state/local agency rows). Mirrors medicaid_lookup.py.
NPI_FILTER = "BILLING_PROVIDER_NPI_NUM ~ '^[12][0-9]{9}$'"

SERVICE_CLASSES = ("personal_care", "home_health", "hospice", "transport",
                   "behavioral", "day_services", "nursing_pdn", "lab", "other")

# ── service_class HCPCS-family heuristic ────────────────────────────────────
# First-pass code/prefix classifier -- enough to group providers into the
# peer cohort the within-class percentile needs, NOT a validated clinical
# taxonomy. personal_care is anchored on the exact codes the spec named
# (T1019/T1020); the rest use standard HCPCS Level II / CPT range conventions.
#
# v3 widening (2026-07-16): the first target-screen run showed 12 of the top
# 14 shortlist orgs falling into the catch-all `other` class — behavioral/SUD
# programs, day-hab centers, private-duty nursing, and T-code NEMT were all
# pooled into one mongrel peer group, softening the within-class-and-state
# percentiles (the Red Sun denominator lesson, half-applied). Every code
# below was verified against data/ground_truth/hcpcs_level2_full.parquet /
# hcpcs_codes.parquet descriptors before being classed.
PERSONAL_CARE_CODES = {"T1019", "T1020", "S5125", "S5126"}
HOME_HEALTH_CODES = {"T1021", "T1022", "S9123", "S9124", "G0299", "G0300"}
HOME_HEALTH_PREFIXES = ("G015", "G016", "G017")  # Medicare home-health per-visit G-codes
HOSPICE_CODES = {
    "T2042", "T2043", "T2044", "T2045", "T2046", "G0182",
    # Hospice site-of-service codes, Q5001-Q5010 ONLY -- NOT the full "Q5"
    # prefix, which CMS also uses for unrelated biosimilar-drug injection
    # codes (Q5101-Q5146+); see F5 in medicaid_features_audit_2026-07-12.md.
    "Q5001", "Q5002", "Q5003", "Q5004", "Q5005",
    "Q5006", "Q5007", "Q5008", "Q5009", "Q5010",
}
TRANSPORT_PREFIX = "A0"  # ambulance / transportation
# NEMT T-codes (verified: "Non-emergency transportation; ..." descriptors).
TRANSPORT_CODES = {"T2001", "T2002", "T2003", "T2004", "T2005", "T2007"}
# CPT psychiatry/psychotherapy section (verified descriptors). H#### HCPCS
# codes (the Alcohol & Drug Abuse / Behavioral Health section, H0001-H2037)
# are matched by shape in the classifier — NOT a bare "H" prefix, because the
# H section also contains two-letter MODIFIERS (HN, HZ, ...) that are not
# billing codes.
BEHAVIORAL_CPT_CODES = {"90791", "90792", "90832", "90834", "90837",
                        "90839", "90846", "90847", "90853"}
# Adult day care (S5100-S5105) + day-habilitation waiver (T2020/T2021).
DAY_SERVICES_CODES = {"S5100", "S5101", "S5102", "S5105", "T2020", "T2021"}
# Private-duty / independent nursing (T1000) + hourly RN/LPN (T1002/T1003).
NURSING_PDN_CODES = {"T1000", "T1002", "T1003"}


@functools.lru_cache(maxsize=1)
def _valid_hcpcs_codes() -> frozenset[str]:
    """Union of ``code`` values from the in-repo HCPCS reference tables.

    Gates the ``lab`` prefix rules in :func:`_classify_service_class` (F6:
    raw T-MSIS state feeds contain non-standard placeholder values, e.g.
    "8888888"/"PIN02", that match the P-/8-prefix shape but aren't real
    codes). Cached once per process. A missing/unreadable table is skipped;
    if BOTH are unavailable this returns an empty frozenset, and the caller
    falls back to the original shape-only rule (fail-open -- matches this
    module's data_reliability gate convention; a reference-table hiccup
    should never be the thing that breaks a feature build).
    """
    codes: set[str] = set()
    for path in HCPCS_REFERENCE_TABLES:
        if not path.exists():
            continue
        try:
            col = pd.read_parquet(path, columns=["code"])["code"]
            codes.update(col.dropna().astype(str).str.strip().str.upper())
        except Exception:
            continue
    return frozenset(codes)


def _classify_service_class(hcpcs: str | None) -> str:
    """Map a dominant (top-paid) HCPCS code to a coarse service_class bucket."""
    if not hcpcs or not isinstance(hcpcs, str):
        return "other"
    code = hcpcs.strip().upper()
    if not code:
        return "other"
    if code in PERSONAL_CARE_CODES:
        return "personal_care"
    if code in HOME_HEALTH_CODES or code.startswith(HOME_HEALTH_PREFIXES):
        return "home_health"
    if code in HOSPICE_CODES:
        return "hospice"
    if code.startswith(TRANSPORT_PREFIX) or code in TRANSPORT_CODES:
        return "transport"
    if code in NURSING_PDN_CODES:
        return "nursing_pdn"
    if code in DAY_SERVICES_CODES:
        return "day_services"
    if code in BEHAVIORAL_CPT_CODES or (
            len(code) == 5 and code[0] == "H" and code[1:].isdigit()):
        # H#### = the HCPCS behavioral/SUD section; the digit check excludes
        # H-section MODIFIERS (HN, HZ, ...) which are not billing codes.
        return "behavioral"
    if code.startswith("P") or (code.isdigit() and code.startswith("8")):
        # CPT Pathology/Lab section is 80047-89398 (all 5-digit codes starting
        # with '8'); HCPCS P-codes (P2028-P9615) are also lab/path. Gate on
        # membership in the reference tables so non-standard placeholder
        # values (F6) fall through to `other` instead of `lab`.
        valid = _valid_hcpcs_codes()
        if not valid or code in valid:
            return "lab"
        return "other"
    return "other"


def _safe_div(num: pd.Series, denom: pd.Series, fill: float = 0.0) -> pd.Series:
    """Element-wise divide; denom<=0/NaN -> ``fill``. Mirrors compute_dmepos_rich_features.py."""
    n = pd.to_numeric(num, errors="coerce").fillna(0)
    d = pd.to_numeric(denom, errors="coerce").fillna(0)
    out = np.where(d > 0, n / d.replace(0, np.nan), fill)
    return pd.Series(out, index=num.index).fillna(fill)


def _clamp_unit_interval(col: pd.Series, name: str, year: int) -> pd.Series:
    """Belt-and-braces invariant: ``col`` must be in [0, 1] (F2/F7 in
    medicaid_features_audit_2026-07-12.md).

    top1_code_share/code_hhi/cross_program_ratio are all structurally
    bounded by construction once fed a positive-only basis (see
    _fetch_year_raw's F2 fix), so this should essentially never fire -- it's
    a last-resort guard, not the primary mechanism. Logs a warning (with the
    count/min/max) whenever it actually does, so a silent regression upstream
    doesn't go unnoticed.
    """
    numeric = pd.to_numeric(col, errors="coerce")
    oob = numeric.notna() & ((numeric < 0) | (numeric > 1))
    n_oob = int(oob.sum())
    if n_oob:
        LOG.warning(
            "  %d: %s had %d/%d value(s) outside [0,1] (min=%.6g, max=%.6g) -- clamping",
            year, name, n_oob, int(numeric.notna().sum()), numeric.min(), numeric.max(),
        )
    return numeric.clip(lower=0.0, upper=1.0)


def _connect() -> tuple[duckdb.DuckDBPyConnection, str]:
    """Open the indexed DuckDB mirror when available, else fall back to the parquet.

    Mirrors ``scripts/medicaid_lookup.py::_connect()``.
    """
    if DUCKDB_PATH.exists():
        con = duckdb.connect(str(DUCKDB_PATH), read_only=True)
        return con, "dataset"
    if PARQUET_PATH.exists():
        con = duckdb.connect()
        return con, f"read_parquet('{PARQUET_PATH}')"
    raise FileNotFoundError(
        f"Neither {DUCKDB_PATH} nor {PARQUET_PATH} found. Run `make pull-medicaid` first."
    )


# ─────────────────────────────────────────────────────────────────────────────
# SQL fetch -- ALL heavy aggregation happens here (DuckDB, not pandas). The
# source table is scanned once per call; by_code/by_npi collapse it from
# ~238M rows down to one row per (npi, year) before anything touches pandas.
# ─────────────────────────────────────────────────────────────────────────────
def _fetch_year_raw(
    con: duckdb.DuckDBPyConnection,
    table: str,
    year: int,
    nppes_path: Path = NPPES_PATH,
    partb_path: Path | None = None,
) -> pd.DataFrame:
    """Return the raw per-(npi, year) aggregate DuckDB computes for ``year``.

    Columns: npi, year, medicaid_total_pay, medicaid_claim_lines,
    medicaid_n_hcpcs, top1_paid, top_hcpcs, sum_sq_paid, positive_total_pay,
    n_negative_rows, prior_pay, provider_state, medicare_partb_pay.
    ``prior_pay``/``top1_paid``/``sum_sq_paid``/``positive_total_pay``/
    ``n_negative_rows`` are intermediates consumed by :func:`_derive_features`
    and dropped before the final parquet is written.

    F2 fix (medicaid_features_audit_2026-07-12.md): T-MSIS contains negative
    ``TOTAL_PAID`` rows (claim adjustments/reversals) that, once summed per
    (npi, year, hcpcs), can leave individual codes netting negative for the
    year. ``top1_paid``/``sum_sq_paid``/``positive_total_pay`` are computed
    from ONLY the codes that net positive for the year, so
    ``top1_code_share``/``code_hhi`` (computed against ``positive_total_pay``
    in :func:`_derive_features`, not ``medicaid_total_pay``) are structurally
    bounded in [0,1] rather than only clamped after the fact.
    ``medicaid_total_pay``/``medicaid_claim_lines``/``medicaid_n_hcpcs`` are
    NOT filtered -- they stay the true net/full activity, adjustments
    included. ``n_negative_rows`` counts how many of a provider-year's codes
    netted negative (0 in the overwhelming majority of rows).
    """
    prior_year = year - 1

    partb_join = ""
    partb_select = "CAST(NULL AS DOUBLE) AS medicare_partb_pay"
    if partb_path is not None and partb_path.exists():
        partb_join = f"LEFT JOIN read_parquet('{partb_path}') AS pb ON pb.npi = cur.npi"
        partb_select = "pb.Tot_Mdcr_Pymt_Amt AS medicare_partb_pay"

    nppes_select = "nppes.state AS provider_state"
    nppes_join = f"LEFT JOIN read_parquet('{nppes_path}') AS nppes ON nppes.npi = cur.npi"
    if not nppes_path.exists():
        nppes_select = "CAST(NULL AS VARCHAR) AS provider_state"
        nppes_join = ""

    query = f"""
    WITH filtered AS (
        SELECT BILLING_PROVIDER_NPI_NUM AS npi,
               CAST(SUBSTR(CLAIM_FROM_MONTH, 1, 4) AS INTEGER) AS yr,
               HCPCS_CODE AS hcpcs,
               TOTAL_PAID AS paid,
               TOTAL_CLAIM_LINES AS claim_lines
        FROM {table}
        WHERE {NPI_FILTER}
          AND SUBSTR(CLAIM_FROM_MONTH, 1, 4) IN ('{year}', '{prior_year}')
    ),
    by_code AS (
        SELECT npi, yr, hcpcs,
               SUM(paid) AS code_paid,
               SUM(claim_lines) AS code_claims
        FROM filtered
        GROUP BY npi, yr, hcpcs
    ),
    by_code_ranked AS (
        -- F3 fix: explicit, deterministic tie-break (code_paid DESC, hcpcs
        -- ASC) so the top-paid code is a stable function of the data, not of
        -- DuckDB's parallel-summation/tie-resolution order across runs.
        SELECT *,
               ROW_NUMBER() OVER (
                   PARTITION BY npi, yr ORDER BY code_paid DESC, hcpcs ASC
               ) AS code_rank
        FROM by_code
    ),
    by_npi AS (
        SELECT npi, yr,
               SUM(code_paid) AS total_pay,
               SUM(code_claims) AS claim_lines,
               COUNT(DISTINCT hcpcs) AS n_hcpcs,
               MAX(CASE WHEN code_rank = 1 THEN hcpcs END) AS top_hcpcs,
               -- F2 fix: concentration basis excludes codes that net
               -- negative for the year (adjustment/reversal claims).
               SUM(CASE WHEN code_paid > 0 THEN code_paid ELSE 0 END) AS positive_total_pay,
               MAX(CASE WHEN code_paid > 0 THEN code_paid END) AS top1_paid,
               SUM(CASE WHEN code_paid > 0 THEN POWER(code_paid, 2) ELSE 0 END) AS sum_sq_paid,
               SUM(CASE WHEN code_paid < 0 THEN 1 ELSE 0 END) AS n_negative_rows
        FROM by_code_ranked
        GROUP BY npi, yr
    ),
    cur AS (SELECT * FROM by_npi WHERE yr = {year}),
    prior AS (SELECT npi, total_pay AS prior_pay FROM by_npi WHERE yr = {prior_year})
    SELECT
        cur.npi AS npi,
        cur.yr AS year,
        cur.total_pay AS medicaid_total_pay,
        cur.claim_lines AS medicaid_claim_lines,
        cur.n_hcpcs AS medicaid_n_hcpcs,
        cur.top1_paid AS top1_paid,
        cur.top_hcpcs AS top_hcpcs,
        cur.sum_sq_paid AS sum_sq_paid,
        cur.positive_total_pay AS positive_total_pay,
        cur.n_negative_rows AS n_negative_rows,
        prior.prior_pay AS prior_pay,
        {nppes_select},
        {partb_select}
    FROM cur
    LEFT JOIN prior ON prior.npi = cur.npi
    {nppes_join}
    {partb_join}
    """
    return con.execute(query).fetchdf()


# ─────────────────────────────────────────────────────────────────────────────
# Pure derivation -- takes the raw per-(npi, year) frame (from DuckDB, or a
# synthetic fixture in tests) and computes every derived column. No file I/O,
# no DuckDB -- this is what tests/test_medicaid_features.py exercises.
# ─────────────────────────────────────────────────────────────────────────────
def _derive_features(raw: pd.DataFrame, year: int, partb_available: bool = True) -> pd.DataFrame:
    """Compute the final feature frame from :func:`_fetch_year_raw`'s output.

    ``raw`` must have: npi, year, medicaid_total_pay, medicaid_claim_lines,
    medicaid_n_hcpcs, top1_paid, top_hcpcs, sum_sq_paid, positive_total_pay,
    n_negative_rows, prior_pay, provider_state, medicare_partb_pay.

    ``partb_available`` distinguishes two different reasons ``medicare_partb_pay``
    can be null for a row: (a) the join was attempted (the year's Part-B panel
    IS present) and simply found no match -- a CONFIRMED "doesn't bill Part-B",
    so ``medicare_also=False`` and ``cross_program_ratio`` is still computable
    (1.0 = 100% Medicaid); vs (b) the panel wasn't present for this year at all
    (the build's escape hatch) -- genuinely unknown, so both are null. Pass
    ``partb_available=False`` when :func:`compute_year` couldn't find that
    year's ``cms_partb_<year>.parquet``.
    """
    out = raw.copy()
    out["npi"] = out["npi"].astype(str)

    # ── Concentration (paid-dollar share, matches hcpcs_revenue_hhi convention) ──
    # F2: the basis is the POSITIVE-paid subset (codes netting negative for the
    # year -- adjustment/reversal claims -- are excluded upstream in
    # _fetch_year_raw), which structurally bounds share/hhi in [0,1].
    # medicaid_total_pay stays the true net and is NOT the concentration
    # denominator. A provider whose codes ALL net <=0 gets 0.0 for both
    # (no positive basis -> no concentration signal), not a division blow-up.
    out["top1_code_share"] = _safe_div(out["top1_paid"], out["positive_total_pay"])
    out["code_hhi"] = _safe_div(out["sum_sq_paid"], out["positive_total_pay"] ** 2)
    out["n_negative_rows"] = (
        pd.to_numeric(out["n_negative_rows"], errors="coerce").fillna(0).astype("int64")
    )

    # ── Trajectory: null (not 0) when there's no prior-year row or prior pay<=0 ──
    prior_pay = pd.to_numeric(out["prior_pay"], errors="coerce")
    total_pay = pd.to_numeric(out["medicaid_total_pay"], errors="coerce")
    out["yoy_growth"] = np.where(prior_pay > 0, total_pay / prior_pay, np.nan)

    # ── service_class from the dominant code ─────────────────────────────────
    out["service_class"] = out["top_hcpcs"].map(_classify_service_class)

    # ── Within-service-class percentiles (the core fix: concentration/scale is
    # only a signal ABOVE class peers, not in absolute terms) ─────────────────
    pctile_cols = {
        "top1_code_share": "top1_code_share_pctile_in_class",
        "code_hhi": "code_hhi_pctile_in_class",
        "medicaid_total_pay": "medicaid_total_pay_pctile_in_class",
        "yoy_growth": "yoy_growth_pctile_in_class",
    }
    for src_col, out_col in pctile_cols.items():
        out[out_col] = out.groupby("service_class")[src_col].rank(pct=True, na_option="keep")

    # ── F1: within-(service_class, provider_state) percentiles alongside the
    # national ones -- national pooling masks state-saturated fraud (a whole
    # class can be "normal" statewide; audit F1). Cells smaller than
    # MIN_STATE_CLASS_N, or rows with no provider_state, get NaN plus the
    # pctile_in_class_state_low_n flag instead of a falsely precise
    # percentile computed off a handful of providers. ──────────────────────
    state_grp = out.groupby(["service_class", "provider_state"], dropna=False)
    state_cell_n = state_grp["npi"].transform("size")
    state_trustworthy = out["provider_state"].notna() & (state_cell_n >= MIN_STATE_CLASS_N)
    for src_col, out_col in pctile_cols.items():
        state_col = out_col + "_state"   # *_pctile_in_class -> *_pctile_in_class_state
        ranked = state_grp[src_col].rank(pct=True, na_option="keep")
        out[state_col] = ranked.where(state_trustworthy)
    out["pctile_in_class_state_low_n"] = ~state_trustworthy

    # ── Data-quality gate: map each distinct provider_state once, not per-row ──
    distinct_states = out["provider_state"].dropna().unique()
    rating_by_state = {s: medicaid_reliability(s, year) for s in distinct_states}
    default_rating = medicaid_reliability(None, year)
    out["data_reliability"] = out["provider_state"].map(rating_by_state).fillna(default_rating)

    # ── Cross-program block (nullable; see module docstring TODO) ────────────
    # matched=True means the by-NPI join found a Part-B row for this NPI-year;
    # matched=False (when partb_available) is a CONFIRMED miss, not an unknown.
    matched = out["medicare_partb_pay"].notna()
    if partb_available:
        out["medicare_also"] = matched.astype("boolean")
        medicare_pay_for_ratio = pd.to_numeric(out["medicare_partb_pay"], errors="coerce").fillna(0.0)
        denom = total_pay.fillna(0) + medicare_pay_for_ratio
        out["cross_program_ratio"] = np.where(denom > 0, total_pay.fillna(0) / denom, np.nan)
    else:
        out["medicare_also"] = pd.array([pd.NA] * len(out), dtype="boolean")
        out["cross_program_ratio"] = np.full(len(out), np.nan)

    # ── F2/F7 belt-and-braces: all three are structurally bounded by
    # construction now, so the clamp is a warning canary for upstream
    # regressions, not the primary mechanism. ────────────────────────────────
    for _col in ("top1_code_share", "code_hhi", "cross_program_ratio"):
        out[_col] = _clamp_unit_interval(out[_col], _col, year)

    # Feature-frame version stamp (see the module docstring's version section).
    out["feature_frame_version"] = FEATURE_FRAME_VERSION

    final_cols = [
        "npi", "year",
        "medicaid_total_pay", "medicaid_claim_lines", "medicaid_n_hcpcs",
        "top_hcpcs", "service_class",
        "top1_code_share", "code_hhi", "yoy_growth", "n_negative_rows",
        "top1_code_share_pctile_in_class", "code_hhi_pctile_in_class",
        "medicaid_total_pay_pctile_in_class", "yoy_growth_pctile_in_class",
        "top1_code_share_pctile_in_class_state", "code_hhi_pctile_in_class_state",
        "medicaid_total_pay_pctile_in_class_state", "yoy_growth_pctile_in_class_state",
        "pctile_in_class_state_low_n",
        "provider_state", "data_reliability",
        "medicare_also", "medicare_partb_pay", "cross_program_ratio",
        "feature_frame_version",
    ]
    return out[final_cols]


def compute_year(
    con: duckdb.DuckDBPyConnection,
    table: str,
    year: int,
    nppes_path: Path = NPPES_PATH,
    partb_dir: Path = PARTB_DIR,
) -> pd.DataFrame:
    """Fetch + derive the full feature frame for one year."""
    partb_path = partb_dir / f"cms_partb_{year}.parquet"
    partb_available = partb_path.exists()
    if not partb_available:
        LOG.warning(
            "  %d: cms_partb_%d.parquet not found under %s -- "
            "medicare_also/medicare_partb_pay/cross_program_ratio will be null for this year",
            year, year, partb_dir,
        )
        partb_path = None
    if not nppes_path.exists():
        LOG.warning("  %d: %s not found -- provider_state/data_reliability will default to 'reliable'",
                    year, nppes_path)

    LOG.info("  %d: aggregating via DuckDB...", year)
    raw = _fetch_year_raw(con, table, year, nppes_path=nppes_path, partb_path=partb_path)
    LOG.info("  %d: %d distinct billing NPIs", year, len(raw))
    return _derive_features(raw, year, partb_available=partb_available)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--year", type=int, help="Single year (2018-2024)")
    ap.add_argument("--all-years", action="store_true")
    ap.add_argument("--out-dir", type=Path, default=OUTPUT_DIR)
    ap.add_argument("--dry-run", action="store_true", help="Compute but don't write parquet")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    if args.all_years:
        years = YEARS
    elif args.year:
        years = [args.year]
    else:
        LOG.error("specify --year YYYY or --all-years")
        return 2

    args.out_dir.mkdir(parents=True, exist_ok=True)

    try:
        con, table = _connect()
    except FileNotFoundError as e:
        LOG.error(str(e))
        return 2

    for y in years:
        df = compute_year(con, table, y)
        LOG.info("  %d: final shape %d NPIs x %d cols", y, len(df), len(df.columns))
        out_path = args.out_dir / f"medicaid_features_{y}.parquet"
        if not args.dry_run:
            df.to_parquet(out_path, index=False)
            LOG.info("  %d: wrote -> %s", y, out_path)
        else:
            LOG.info("  %d: --dry-run, not writing", y)

    return 0


if __name__ == "__main__":
    sys.exit(main())
