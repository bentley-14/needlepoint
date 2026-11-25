"""Compute DMEPOS rich features — supplier concentration, category mix,
specialty-relative percentiles, suppression flags.

Targets the kickback / brace-mill scheme blind spot identified in the
post-v9-retrain investigation: the existing 13-col DMEPOS feature set
(``data/cache/dmepos_features/dmepos_<year>.parquet``) captures volume +
one supplier ratio. This script adds the *concentration* and *anomaly*
signals that distinguish a fraudulent DME-mill referrer from a legitimate
high-volume practitioner.

Output schema: per-NPI parquet at
``data/cache/feature_frames/dmepos_rich_<year>.parquet`` with ~40 columns
prefixed ``dmer_`` (DMEPOS-rich). Attached by ``feature_frames.py``
load_feature_frame() alongside hcpcs_rich.

Why ``dmer_`` not ``dme_``: the existing 13-col cache uses ``dme_*``
prefixes; new rich features get their own namespace so we can ablate them
cleanly (and they don't collide).

Feature families
----------------
1. **Supplier concentration** (8 cols): clms/svcs/pay/benes per supplier,
   n_suppliers itself, single-supplier flag.
2. **Inflation / overcharging** (3 cols): submitted-to-allowed ratio,
   standardized-vs-paid ratio (geographic anomaly).
3. **Category mix** (4 cols): DME/POS/Drug $ share + dominance.
4. **Per-category concentration** (4 cols): DME clms/supplier,
   DME pay/supplier, DME pay/bene, DME inflation ratio.
5. **Suppression flags** (4 cols): DME/POS/Drug Sprsn_Ind + any-flag.
6. **Beneficiary risk-profile** (3 cols): dual %, avg risk score, avg age.
7. **Specialty-relative percentiles** (6 cols): within-specialty rank of
   total pay, clms/supplier, pay/bene, n_suppliers (REV), DME share,
   sbmtd inflation.

Usage:
    python scripts/compute_dmepos_rich_features.py            # all years 2014-2023
    python scripts/compute_dmepos_rich_features.py --year 2022
    python scripts/compute_dmepos_rich_features.py --force    # regenerate even if cached
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

PROCESSED_DIR = Path("data/panels")
CACHE_DIR = Path("data/cache/feature_frames")

# DMEPOS Referring Provider PUF available from 2014.
YEARS = list(range(2014, 2024))


def _safe_div(num: pd.Series, denom: pd.Series, fill: float = 0.0) -> pd.Series:
    """Element-wise divide; denom<=0 or NaN → ``fill``; result clipped at 0."""
    n = pd.to_numeric(num, errors="coerce").fillna(0)
    d = pd.to_numeric(denom, errors="coerce").fillna(0)
    out = np.where(d > 0, n / d.replace(0, np.nan), fill)
    return pd.Series(out, index=num.index).fillna(fill)


def _spec_pct(series: pd.Series, group: pd.Series, reverse: bool = False) -> pd.Series:
    """Within-specialty percentile rank (0..1; 1 = max in group).

    ``reverse=True`` returns 1 - percentile (1 = MIN in group = most concentrated).
    NaN inputs return 0.5 (neutral).
    """
    s = pd.to_numeric(series, errors="coerce")
    pct = s.groupby(group).rank(pct=True, method="average")
    pct = pct.fillna(0.5)
    return (1.0 - pct) if reverse else pct


def compute_dmepos_rich(year: int) -> pd.DataFrame:
    """Return per-NPI rich DMEPOS feature frame for ``year``.

    Reads ``data/panels/cms_dmepos_{year}.parquet``. NPI column returned
    as zero-padded 10-char string. Numeric columns prefixed ``dmer_``.
    """
    src = PROCESSED_DIR / f"cms_dmepos_{year}.parquet"
    if not src.exists():
        raise FileNotFoundError(f"DMEPOS PUF not found: {src}")
    print(f"[dmer] {year}: reading {src.name}...")
    raw = pd.read_parquet(src)
    n_rows = len(raw)
    print(f"[dmer] {year}: {n_rows:,} rows, {raw.shape[1]} cols")

    out = pd.DataFrame(index=raw.index)
    out["npi"] = raw["Rfrg_NPI"].astype(str).str.zfill(10)

    # Specialty key for within-specialty percentiles. Fill missing → "UNK".
    spec_key = raw.get("Rfrg_Prvdr_Spclty_Desc", pd.Series("UNK", index=raw.index)).fillna("UNK").astype(str)

    # ── Coerce numeric base columns ──────────────────────────────────────
    NUMERIC = [
        "Tot_Suplrs", "Tot_Suplr_HCPCS_Cds", "Tot_Suplr_Benes",
        "Tot_Suplr_Clms", "Tot_Suplr_Srvcs",
        "Suplr_Sbmtd_Chrgs", "Suplr_Mdcr_Alowd_Amt",
        "Suplr_Mdcr_Pymt_Amt", "Suplr_Mdcr_Stdzd_Pymt_Amt",
        "DME_Tot_Suplrs", "DME_Tot_Suplr_HCPCS_Cds",
        "DME_Tot_Suplr_Benes", "DME_Tot_Suplr_Clms", "DME_Tot_Suplr_Srvcs",
        "DME_Suplr_Sbmtd_Chrgs", "DME_Suplr_Mdcr_Alowd_Amt",
        "DME_Suplr_Mdcr_Pymt_Amt",
        "POS_Suplr_Mdcr_Pymt_Amt", "Drug_Suplr_Mdcr_Pymt_Amt",
        "Bene_Avg_Age", "Bene_Avg_Risk_Scre",
        "Bene_Dual_Cnt", "Bene_Ndual_Cnt",
    ]
    r = pd.DataFrame(index=raw.index)
    for c in NUMERIC:
        r[c] = pd.to_numeric(raw.get(c, 0), errors="coerce").fillna(0)

    # ── 1. Supplier concentration ────────────────────────────────────────
    out["dmer_n_suppliers"] = r["Tot_Suplrs"]
    out["dmer_clms_per_supplier"] = _safe_div(r["Tot_Suplr_Clms"], r["Tot_Suplrs"])
    out["dmer_svcs_per_supplier"] = _safe_div(r["Tot_Suplr_Srvcs"], r["Tot_Suplrs"])
    out["dmer_pay_per_supplier"] = _safe_div(r["Suplr_Mdcr_Pymt_Amt"], r["Tot_Suplrs"])
    out["dmer_benes_per_supplier"] = _safe_div(r["Tot_Suplr_Benes"], r["Tot_Suplrs"])
    out["dmer_single_supplier_flag"] = (r["Tot_Suplrs"] == 1).astype(int)
    out["dmer_few_suppliers_flag"] = (r["Tot_Suplrs"] <= 3).astype(int)
    out["dmer_n_hcpcs"] = r["Tot_Suplr_HCPCS_Cds"]

    # ── 2. Inflation / overcharging ──────────────────────────────────────
    out["dmer_sbmtd_inflation"] = _safe_div(r["Suplr_Sbmtd_Chrgs"], r["Suplr_Mdcr_Alowd_Amt"])
    out["dmer_pay_to_alowd_ratio"] = _safe_div(r["Suplr_Mdcr_Pymt_Amt"], r["Suplr_Mdcr_Alowd_Amt"])
    # Geographic anomaly: standardized payment removes geographic adjustments;
    # pay/stdzd_pay > 1 indicates above-average-cost region. Brace-mills cluster
    # in low-cost regions, so we expect <1 here; far from 1 (either direction)
    # is anomalous.
    out["dmer_stdzd_pay_ratio"] = _safe_div(r["Suplr_Mdcr_Pymt_Amt"], r["Suplr_Mdcr_Stdzd_Pymt_Amt"], fill=1.0)

    # ── 3. Category mix ──────────────────────────────────────────────────
    total_pay = r["Suplr_Mdcr_Pymt_Amt"].clip(lower=0)
    out["dmer_dme_pay_share"] = _safe_div(r["DME_Suplr_Mdcr_Pymt_Amt"], total_pay)
    out["dmer_pos_pay_share"] = _safe_div(r["POS_Suplr_Mdcr_Pymt_Amt"], total_pay)
    out["dmer_drug_pay_share"] = _safe_div(r["Drug_Suplr_Mdcr_Pymt_Amt"], total_pay)
    # Single-category dominance: max share — high = referrer specializes in one DMEPOS type
    out["dmer_category_dominance"] = out[["dmer_dme_pay_share", "dmer_pos_pay_share", "dmer_drug_pay_share"]].max(axis=1)

    # ── 4. Per-category concentration (DME = highest-volume fraud target) ─
    out["dmer_dme_clms_per_supplier"] = _safe_div(r["DME_Tot_Suplr_Clms"], r["DME_Tot_Suplrs"])
    out["dmer_dme_pay_per_supplier"] = _safe_div(r["DME_Suplr_Mdcr_Pymt_Amt"], r["DME_Tot_Suplrs"])
    out["dmer_dme_pay_per_bene"] = _safe_div(r["DME_Suplr_Mdcr_Pymt_Amt"], r["DME_Tot_Suplr_Benes"])
    out["dmer_dme_sbmtd_inflation"] = _safe_div(r["DME_Suplr_Sbmtd_Chrgs"], r["DME_Suplr_Mdcr_Alowd_Amt"])

    # ── 5. Suppression flags ─────────────────────────────────────────────
    # CMS suppresses category-level cells with two codes (per PUF convention):
    #   '*' → cell n<11, redacted for privacy
    #   '#' → value redacted as negative or unreliable
    # Both mean "no published data" — a high-volume referrer whose DME cell
    # is suppressed is unusual. We flag both as suppression=1.
    for cat in ("DME", "POS", "Drug"):
        col_in = f"{cat}_Sprsn_Ind"
        col_out = f"dmer_{cat.lower()}_sprsn"
        if col_in in raw.columns:
            s = raw[col_in].astype(str)
            out[col_out] = s.isin(["*", "#"]).astype(int)
        else:
            out[col_out] = 0
    out["dmer_any_sprsn"] = (
        (out["dmer_dme_sprsn"] | out["dmer_pos_sprsn"] | out["dmer_drug_sprsn"])
    ).astype(int)

    # ── 6. Beneficiary risk profile ──────────────────────────────────────
    dual_total = (r["Bene_Dual_Cnt"] + r["Bene_Ndual_Cnt"]).clip(lower=0)
    out["dmer_dual_share"] = _safe_div(r["Bene_Dual_Cnt"], dual_total)
    out["dmer_avg_risk_scre"] = r["Bene_Avg_Risk_Scre"]
    out["dmer_avg_age"] = r["Bene_Avg_Age"]

    # ── 7. Specialty-relative percentiles ────────────────────────────────
    # The within-specialty rank catches deviations that absolute volume can't.
    # E.g. a Family Practice doc in the 99th pct of DME pay-per-bene WITHIN
    # FAM PRAC is suspicious even if absolute pay/bene is modest.
    out["dmer_total_pay_pct_spec"] = _spec_pct(r["Suplr_Mdcr_Pymt_Amt"], spec_key)
    out["dmer_clms_per_supplier_pct_spec"] = _spec_pct(out["dmer_clms_per_supplier"], spec_key)
    out["dmer_pay_per_bene_pct_spec"] = _spec_pct(
        _safe_div(r["Suplr_Mdcr_Pymt_Amt"], r["Tot_Suplr_Benes"]), spec_key
    )
    # REVERSE rank: 1 = fewest suppliers in specialty = most concentrated
    out["dmer_n_suppliers_pct_spec_rev"] = _spec_pct(r["Tot_Suplrs"], spec_key, reverse=True)
    out["dmer_dme_share_pct_spec"] = _spec_pct(out["dmer_dme_pay_share"], spec_key)
    out["dmer_sbmtd_inflation_pct_spec"] = _spec_pct(out["dmer_sbmtd_inflation"], spec_key)

    # ── Cleanup: inf → NaN → 0 for all numerics ──────────────────────────
    num_cols = [c for c in out.columns if c.startswith("dmer_")]
    out[num_cols] = out[num_cols].replace([np.inf, -np.inf], np.nan).fillna(0)

    n_feats = len(num_cols)
    print(f"[dmer] {year}: {len(out):,} NPIs × {n_feats} features")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--year", type=int, default=None,
                    help="Single year (default: all 2014-2023)")
    ap.add_argument("--force", action="store_true", help="Regenerate even if cache exists")
    args = ap.parse_args()

    years = [args.year] if args.year else YEARS
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    for y in years:
        out_path = CACHE_DIR / f"dmepos_rich_{y}.parquet"
        if out_path.exists() and not args.force:
            print(f"[dmer] {y}: cached → {out_path} (use --force to regenerate)")
            continue
        try:
            df = compute_dmepos_rich(y)
        except FileNotFoundError as e:
            print(f"[dmer] {y}: SKIP — {e}")
            continue
        df.to_parquet(out_path, index=False)
        print(f"[dmer] {y}: wrote → {out_path}  ({len(df):,} × {len(df.columns)})")

    return 0


if __name__ == "__main__":
    sys.exit(main())
