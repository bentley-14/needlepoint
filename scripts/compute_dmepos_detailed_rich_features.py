"""Per-NPI rich features from the DETAILED DMEPOS PUF (code-level).

Reads ``data/panels/cms_dmepos_detailed_{year}.parquet`` — one row per
(Referring-NPI × HCPCS_CD × Suplr_Rentl_Ind) — and aggregates it to ONE row
per referring NPI, the grain every downstream NPI-keyed consumer expects.

Where the aggregate ``cms_dmepos`` (→ ``dmepos_rich``) already gives supplier
concentration / category mix at the referrer level, this captures what only the
code-level cut can see: the **distribution over HCPCS codes**. A brace-mill or
wheelchair-mill is a distribution collapsed onto a handful of high-margin codes
(L0650 back braces, K0823 power wheelchairs, E1390 oxygen) — so code
concentration (top-1 / HHI), DME-family mix (A/E/K/L), rental share, and
per-code supplier captivity are the fingerprint.

Output: ``data/cache/feature_frames/dmepos_detailed_rich_{year}.parquet``
(one row per NPI; numeric features prefixed ``dmed_``), mirroring the
``dmepos_rich`` / ``hcpcs_rich`` feature-frame convention.

Usage::

    python scripts/compute_dmepos_detailed_rich_features.py            # all years
    python scripts/compute_dmepos_detailed_rich_features.py --year 2023 --force
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
PANELS_DIR = REPO_ROOT / "data/panels"
CACHE_DIR = REPO_ROOT / "data/cache/feature_frames"
YEARS = tuple(range(2014, 2024))  # 2014-2023, matching the detailed PUF release

# Columns we actually need (read narrow — the full file is ~600MB CSV / year).
_COLS = [
    "npi", "Rfrg_NPI", "Rfrg_Prvdr_Spclty_Desc",
    "HCPCS_CD", "Suplr_Rentl_Ind", "RBCS_Desc",
    "Tot_Suplrs", "Tot_Suplr_Benes", "Tot_Suplr_Srvcs",
    "Avg_Suplr_Sbmtd_Chrg", "Avg_Suplr_Mdcr_Alowd_Amt", "Avg_Suplr_Mdcr_Pymt_Amt",
]


def _safe_div(num, den, fill=0.0):
    out = pd.to_numeric(num, errors="coerce") / pd.to_numeric(den, errors="coerce").replace(0, np.nan)
    return out.replace([np.inf, -np.inf], np.nan).fillna(fill)


def _spec_pct(series: pd.Series, group: pd.Series, reverse: bool = False) -> pd.Series:
    """Within-specialty percentile rank (0..1). Missing → 0.5."""
    s = pd.to_numeric(series, errors="coerce")
    pct = s.groupby(group).rank(pct=True, method="average").fillna(0.5)
    return (1.0 - pct) if reverse else pct


def compute_dmepos_detailed_rich(year: int) -> pd.DataFrame:
    src = PANELS_DIR / f"cms_dmepos_detailed_{year}.parquet"
    if not src.exists():
        raise FileNotFoundError(
            f"detailed DMEPOS panel not found: {src} "
            f"(pull from R2: rclone copy r2:needlepoint-data/processed/v1/ data/panels/ "
            f"--include 'cms_dmepos_detailed_*.parquet')")
    print(f"[dmed] {year}: reading {src.name}...")
    cols = [c for c in _COLS]
    raw = pd.read_parquet(src, columns=cols)
    print(f"[dmed] {year}: {len(raw):,} code-rows")

    npi = raw["npi"].astype(str).str.zfill(10) if "npi" in raw else raw["Rfrg_NPI"].astype(str).str.zfill(10)
    raw = raw.assign(_npi=npi)
    # Estimated paid per (code, rental) row: per-service avg payment × services.
    raw["_paid"] = (pd.to_numeric(raw["Avg_Suplr_Mdcr_Pymt_Amt"], errors="coerce").fillna(0)
                    * pd.to_numeric(raw["Tot_Suplr_Srvcs"], errors="coerce").fillna(0)).clip(lower=0)
    raw["_srvcs"] = pd.to_numeric(raw["Tot_Suplr_Srvcs"], errors="coerce").fillna(0)
    raw["_suplrs"] = pd.to_numeric(raw["Tot_Suplrs"], errors="coerce").fillna(0)
    raw["_prefix"] = raw["HCPCS_CD"].astype(str).str[0].str.upper()
    raw["_rbcs"] = raw["RBCS_Desc"].astype(str).str.lower()

    g = raw.groupby("_npi", sort=False)
    out = pd.DataFrame({"npi": list(g.groups.keys())}).set_index("npi")

    # ── 1. Volume / breadth ──────────────────────────────────────────────
    out["dmed_total_paid"] = g["_paid"].sum()
    out["dmed_total_srvcs"] = g["_srvcs"].sum()
    out["dmed_n_codes"] = g["HCPCS_CD"].nunique()
    out["dmed_n_rows"] = g.size()

    # ── 2. Code concentration (the mill fingerprint) ─────────────────────
    paid_by_code = raw.groupby(["_npi", "HCPCS_CD"])["_paid"].sum()
    # top-1 / top-5 code share of $, and Herfindahl (sum of squared code shares)
    code_share = (paid_by_code / paid_by_code.groupby(level=0).sum().replace(0, np.nan)).fillna(0)
    out["dmed_top1_code_paid_share"] = code_share.groupby(level=0).max()
    out["dmed_top5_code_paid_share"] = (code_share.groupby(level=0)
                                        .apply(lambda s: s.nlargest(5).sum()))
    out["dmed_code_hhi"] = (code_share ** 2).groupby(level=0).sum()

    # ── 3. DME family mix (HCPCS prefix: A supplies / E equip / K DMERC / L orthotic) ─
    for pre in ("A", "E", "K", "L"):
        share = (raw[raw["_prefix"] == pre].groupby("_npi")["_paid"].sum()
                 .reindex(out.index).fillna(0))
        out[f"dmed_paid_share_{pre.lower()}"] = _safe_div(share, out["dmed_total_paid"])
    # RBCS family shares for the highest-fraud-salience categories
    for label, kw in (("wheelchair", "wheelchair"), ("oxygen", "oxygen"),
                      ("orthotic", "orthos")):
        share = (raw[raw["_rbcs"].str.contains(kw, na=False)].groupby("_npi")["_paid"].sum()
                 .reindex(out.index).fillna(0))
        out[f"dmed_paid_share_{label}"] = _safe_div(share, out["dmed_total_paid"])

    # ── 4. Rental vs purchase ────────────────────────────────────────────
    rental_paid = (raw[raw["Suplr_Rentl_Ind"].astype(str).str.upper() == "Y"]
                   .groupby("_npi")["_paid"].sum().reindex(out.index).fillna(0))
    out["dmed_rental_paid_share"] = _safe_div(rental_paid, out["dmed_total_paid"])

    # ── 5. Supplier captivity (kickback signal: few DME cos per code) ────
    out["dmed_suppliers_per_code_mean"] = g["_suplrs"].mean()
    out["dmed_single_supplier_code_share"] = _safe_div(
        raw.assign(_one=(raw["_suplrs"] == 1).astype(int)).groupby("_npi")["_one"].sum(),
        out["dmed_n_rows"])

    # ── 6. Submitted-vs-allowed inflation (paid-weighted mean across codes) ─
    raw["_infl"] = _safe_div(raw["Avg_Suplr_Sbmtd_Chrg"], raw["Avg_Suplr_Mdcr_Alowd_Amt"], fill=1.0)
    wsum = raw.assign(_w=raw["_infl"] * raw["_paid"]).groupby("_npi")["_w"].sum()
    out["dmed_sbmtd_inflation"] = _safe_div(wsum, out["dmed_total_paid"], fill=1.0)

    # ── 7. Within-specialty percentiles ──────────────────────────────────
    spec = (raw.groupby("_npi")["Rfrg_Prvdr_Spclty_Desc"].first()
            .reindex(out.index).fillna("UNK").astype(str))
    out["dmed_total_paid_pct_spec"] = _spec_pct(out["dmed_total_paid"], spec)
    out["dmed_code_hhi_pct_spec"] = _spec_pct(out["dmed_code_hhi"], spec)
    out["dmed_top1_share_pct_spec"] = _spec_pct(out["dmed_top1_code_paid_share"], spec)

    out["data_year"] = year
    num_cols = [c for c in out.columns if c.startswith("dmed_")]
    out[num_cols] = out[num_cols].replace([np.inf, -np.inf], np.nan).fillna(0)
    out = out.reset_index()
    print(f"[dmed] {year}: {len(out):,} NPIs × {len(num_cols)} features")
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--year", type=int, default=None, help="single year (default: all 2014-2023)")
    ap.add_argument("--force", action="store_true", help="regenerate even if cache exists")
    args = ap.parse_args(argv)

    years = [args.year] if args.year else list(YEARS)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    for y in years:
        out_path = CACHE_DIR / f"dmepos_detailed_rich_{y}.parquet"
        if out_path.exists() and not args.force:
            print(f"[dmed] {y}: cached → {out_path.name} (use --force)")
            continue
        try:
            df = compute_dmepos_detailed_rich(y)
        except FileNotFoundError as e:
            print(f"[dmed] {y}: SKIP — {e}")
            continue
        df.to_parquet(out_path, index=False)
        print(f"[dmed] {y}: wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
