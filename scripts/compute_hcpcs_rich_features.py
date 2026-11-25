"""Compute per-(NPI, year) novel HCPCS features from raw Part B service files.

Reads ``data/panels/cms_partb_svc_<year>.parquet`` (one row per
(NPI, HCPCS, Place_Of_Srvc)), joins to ``fraudlib/ranker/hcpcs_taxonomy.py``,
and emits a small parquet per year with ~60 new feature columns
keyed by zero-padded NPI.

The output is meant to be JOINED to the existing
``data/cache/feature_frames/features_<year>.parquet`` for training.

Feature groups (~60 columns):

  Tier 1 — Universal (6):
    hcpcs_top1_code_share, hcpcs_revenue_hhi, hcpcs_srvcs_per_bene_day,
    hcpcs_distinct_codes_per_bene, hcpcs_off_specialty_dollar_share,
    hcpcs_sentinel_dollar_share

  Tier 2 — Specialty-specific code-family ratios (~35):
    derm: flap_share, flap_per_mohs, mohs_stages_per_case, biopsy_intensity
    cardiology: stress_per_bene, radiopharm_share, cath_intensity,
                stent_per_cath, echo_per_bene
    ophth: yag_per_cataract, anti_vegf_per_bene, visual_field_intensity,
           retinal_laser_intensity
    pain: nerve_block_density, rfa_intensity, esi_per_bene,
          trigger_point_share, synvisc_volume
    podiatry: nail_avulsion_ratio, debride_per_bene, diabetic_callus_freq
    lab/path: panel_share, 88305_intensity, toxicology_share, genetic_share
    mental: psych_60min_share, addiction_intensity
    radiology: mri_per_bene, ct_per_bene
    drug/infusion: infusion_intensity, unclassified_jcode_share,
                   hydration_share, chemo_per_bene
    hospice/HHA: hospice_cert_share, hha_face_to_face_share
    telehealth: tele_pos_share, tele_specific_hcpcs_share

  Tier 3 — Sentinel single-code shares (~20):
    hcpcs_sentinel_36561_share, hcpcs_sentinel_g0444_intensity, etc.

Usage:
    python scripts/compute_hcpcs_rich_features.py --year 2018
    python scripts/compute_hcpcs_rich_features.py --all-years
    python scripts/compute_hcpcs_rich_features.py --year 2022 --dry-run

Output → ``data/cache/feature_frames/hcpcs_rich_<year>.parquet``
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from fraudlib.ranker.hcpcs_taxonomy import (  # noqa: E402
    CODE_FAMILIES,
    SENTINEL_CODES,
    SPECIALTY_INSCOPE,
)

LOG = logging.getLogger("compute_hcpcs_rich_features")

PARTB_DIR = REPO_ROOT / "data/panels"  # cms_partb_svc_<year>.parquet
OUTPUT_DIR = REPO_ROOT / "data/cache/feature_frames"
YEARS = list(range(2013, 2024))


# ─────────────────────────────────────────────────────────────────────────────
# Universal Tier-1 features
# ─────────────────────────────────────────────────────────────────────────────
def _universal_features(df: pd.DataFrame) -> pd.DataFrame:
    """Compute universal features per NPI from per-(NPI, HCPCS, POS) rows."""
    # Aggregate to per-(NPI, HCPCS): sum across POS
    by_npi_code = df.groupby(["npi", "HCPCS_Cd"], observed=True).agg(
        srvcs=("Tot_Srvcs", "sum"),
        benes=("Tot_Benes", "sum"),
        bene_day_srvcs=("Tot_Bene_Day_Srvcs", "sum"),
        paid=("paid", "sum"),
    ).reset_index()

    # Aggregate to per-NPI for totals
    npi_tot = by_npi_code.groupby("npi", observed=True).agg(
        total_srvcs=("srvcs", "sum"),
        total_paid=("paid", "sum"),
        total_bene_day_srvcs=("bene_day_srvcs", "sum"),
        n_codes=("HCPCS_Cd", "nunique"),
    ).reset_index()

    # Top-1 share (max single-code services / total)
    top1 = by_npi_code.groupby("npi", observed=True)["srvcs"].max().rename("top1_srvcs")
    npi_tot = npi_tot.merge(top1, on="npi")
    npi_tot["hcpcs_top1_code_share"] = npi_tot["top1_srvcs"] / npi_tot["total_srvcs"].clip(lower=1)

    # HHI of revenue concentration: sum of (paid_share)^2
    pp = by_npi_code.copy()
    pp["paid_share"] = pp["paid"] / pp.groupby("npi", observed=True)["paid"].transform("sum").clip(lower=1)
    hhi = (pp["paid_share"] ** 2).groupby(pp["npi"], observed=True).sum().rename("hcpcs_revenue_hhi")
    npi_tot = npi_tot.merge(hhi, on="npi")

    # Stacking: services per bene-day
    npi_tot["hcpcs_srvcs_per_bene_day"] = (
        npi_tot["total_srvcs"] / npi_tot["total_bene_day_srvcs"].clip(lower=1)
    )

    # Distinct codes per beneficiary: (distinct codes) / (total benes summed across codes,
    # which approximates unique-bene-encounters)
    benes_total = by_npi_code.groupby("npi", observed=True)["benes"].sum().rename("benes_sum")
    npi_tot = npi_tot.merge(benes_total, on="npi")
    npi_tot["hcpcs_distinct_codes_per_bene"] = (
        npi_tot["n_codes"] / npi_tot["benes_sum"].clip(lower=1)
    )

    return npi_tot[["npi", "hcpcs_top1_code_share", "hcpcs_revenue_hhi",
                    "hcpcs_srvcs_per_bene_day", "hcpcs_distinct_codes_per_bene",
                    "total_srvcs", "total_paid", "n_codes", "benes_sum"]]


# ─────────────────────────────────────────────────────────────────────────────
# Off-specialty + sentinel features
# ─────────────────────────────────────────────────────────────────────────────
def _off_specialty_and_sentinel(df: pd.DataFrame) -> pd.DataFrame:
    """Compute off-specialty share + sentinel share per NPI (vectorized).

    Pre-compute per-row paid_offspec and paid_sentinel, then groupby+sum.
    Avoids slow lambda-with-df.loc lookups inside agg.
    """
    inscope_sets = {sp: set(codes) for sp, codes in SPECIALTY_INSCOPE.items()}

    # Build inscope mask: start with True (treat unknown specialty as in-scope —
    # don't penalize); then set False per known specialty for codes NOT in scope.
    inscope = np.ones(len(df), dtype=bool)
    known_specs = set(inscope_sets.keys())
    df_pt = df["provider_type"]
    df_code = df["HCPCS_Cd"]
    for spec, codes in inscope_sets.items():
        spec_mask = (df_pt == spec).values
        if not spec_mask.any():
            continue
        # For rows matching this specialty, inscope iff code in codes
        inscope[spec_mask] = df_code[spec_mask].isin(codes).values

    sentinel_set = set(SENTINEL_CODES)
    is_sentinel = df_code.isin(sentinel_set).values

    df2 = df[["npi", "paid"]].copy()
    df2["paid_offspec"] = df["paid"].where(~inscope, 0.0)
    df2["paid_sentinel"] = df["paid"].where(is_sentinel, 0.0)

    agg = df2.groupby("npi", observed=True).agg(
        total_paid_for_offspec=("paid", "sum"),
        offspec_paid=("paid_offspec", "sum"),
        sentinel_paid=("paid_sentinel", "sum"),
    ).reset_index()
    agg["hcpcs_off_specialty_dollar_share"] = (
        agg["offspec_paid"] / agg["total_paid_for_offspec"].clip(lower=1)
    )
    agg["hcpcs_sentinel_dollar_share"] = (
        agg["sentinel_paid"] / agg["total_paid_for_offspec"].clip(lower=1)
    )
    return agg[["npi", "hcpcs_off_specialty_dollar_share", "hcpcs_sentinel_dollar_share"]]


# ─────────────────────────────────────────────────────────────────────────────
# Code-family ratio + intensity features (Tier 2)
# ─────────────────────────────────────────────────────────────────────────────
def _code_family_features(df: pd.DataFrame) -> pd.DataFrame:
    """For each named code-family, compute per-NPI total services + dollars."""
    family_to_codes = CODE_FAMILIES

    # Filter to rows touching at least one family code
    all_family_codes: set[str] = set()
    for codes in family_to_codes.values():
        all_family_codes.update(codes)

    rel = df[df["HCPCS_Cd"].isin(all_family_codes)].copy()
    if rel.empty:
        # Build empty frame with right shape
        out = df.groupby("npi", observed=True).size().to_frame("dummy").drop(columns="dummy").reset_index()
        for fam in family_to_codes:
            out[f"fam_{fam}_srvcs"] = 0
            out[f"fam_{fam}_paid"] = 0.0
        return out

    # For each family, compute per-NPI total services
    pieces = []
    for fam_name, codes in family_to_codes.items():
        sub = rel[rel["HCPCS_Cd"].isin(codes)]
        if sub.empty:
            continue
        agg = sub.groupby("npi", observed=True).agg(
            **{f"fam_{fam_name}_srvcs": ("Tot_Srvcs", "sum"),
               f"fam_{fam_name}_paid":  ("paid", "sum"),
               f"fam_{fam_name}_benes": ("Tot_Benes", "sum")}
        ).reset_index()
        pieces.append(agg)

    if not pieces:
        out = df.groupby("npi", observed=True).size().to_frame("dummy").drop(columns="dummy").reset_index()
        return out

    # Merge all families together
    merged = pieces[0]
    for p in pieces[1:]:
        merged = merged.merge(p, on="npi", how="outer")
    return merged.fillna(0)


def _derive_ratios(npi_tot: pd.DataFrame, fams: pd.DataFrame) -> pd.DataFrame:
    """Compute the actual feature ratios from per-family totals."""
    out = npi_tot.merge(fams, on="npi", how="left").fillna(0)

    def col(name: str) -> pd.Series:
        return out.get(f"fam_{name}_srvcs", pd.Series(0, index=out.index))

    def col_paid(name: str) -> pd.Series:
        return out.get(f"fam_{name}_paid", pd.Series(0, index=out.index))

    def safe_div(a: pd.Series, b: pd.Series) -> pd.Series:
        b = b.replace(0, np.nan)
        return (a / b).fillna(0)

    benes = out["benes_sum"].clip(lower=1)
    total_paid = out["total_paid"].clip(lower=1)

    # Derm
    out["hcpcs_derm_flap_share"] = safe_div(col("derm_flap"), col("derm_flap") + col("derm_wound_repair"))
    out["hcpcs_derm_flap_per_mohs"] = safe_div(col("derm_flap"), col("derm_mohs_first") + col("derm_mohs_addl"))
    out["hcpcs_derm_mohs_stages_per_case"] = safe_div(col("derm_mohs_addl"), col("derm_mohs_first"))
    out["hcpcs_derm_biopsy_intensity"] = col("derm_biopsy") / benes
    out["hcpcs_derm_destruction_share"] = col_paid("derm_destruction") / total_paid

    # Cardiology
    out["hcpcs_card_stress_per_bene"] = col("card_stress") / benes
    out["hcpcs_card_radiopharm_share"] = col_paid("card_radiopharm") / total_paid
    out["hcpcs_card_cath_intensity"] = col("card_cath") / benes
    out["hcpcs_card_stent_per_cath"] = safe_div(col("card_pci_stent"), col("card_cath"))
    out["hcpcs_card_echo_per_bene"] = col("card_echo") / benes
    out["hcpcs_card_ep_study_per_bene"] = col("card_ep_study") / benes
    out["hcpcs_card_holter_per_bene"] = col("card_holter") / benes

    # Ophthalmology
    cataract_total = col("ophth_cataract_simple") + col("ophth_cataract_complex")
    out["hcpcs_ophth_yag_per_cataract"] = safe_div(col("ophth_yag"), cataract_total)
    out["hcpcs_ophth_complex_cataract_share"] = safe_div(col("ophth_cataract_complex"), cataract_total)
    out["hcpcs_ophth_anti_vegf_per_bene"] = col("ophth_anti_vegf_proc") / benes
    out["hcpcs_ophth_visual_field_intensity"] = col("ophth_visual_field") / benes
    out["hcpcs_ophth_retinal_laser_intensity"] = col("ophth_retinal_laser") / benes

    # Pain / Ortho
    out["hcpcs_pain_nerve_block_density"] = col("pain_nerve_block") / benes
    out["hcpcs_pain_rfa_intensity"] = col("pain_rfa") / benes
    out["hcpcs_pain_esi_per_bene"] = col("pain_esi") / benes
    out["hcpcs_pain_trigger_point_share"] = col_paid("pain_trigger_point") / total_paid
    out["hcpcs_pain_synvisc_share"] = col_paid("pain_synvisc") / total_paid
    out["hcpcs_ortho_arthroscopy_per_bene"] = col("ortho_arthroscopy") / benes
    out["hcpcs_ortho_joint_inj_per_bene"] = col("ortho_joint_injection") / benes

    # Podiatry
    out["hcpcs_pod_nail_avulsion_share"] = col_paid("pod_nail_avulsion") / total_paid
    out["hcpcs_pod_debride_per_bene"] = col("pod_debride_medical") / benes
    out["hcpcs_pod_diabetic_callus_freq"] = col("pod_diabetic_callus") / benes

    # Lab / Path
    out["hcpcs_lab_panel_share"] = col_paid("lab_panels") / total_paid
    out["hcpcs_path_88305_intensity"] = col("path_88305") / benes
    out["hcpcs_lab_toxicology_share"] = col_paid("lab_toxicology") / total_paid
    out["hcpcs_lab_genetic_share"] = col_paid("lab_genetic") / total_paid

    # Mental Health
    out["hcpcs_psych_60min_share"] = safe_div(col("psych_60min"), col("psych_all"))
    out["hcpcs_addiction_treatment_intensity"] = col("addiction_treatment") / benes

    # Radiology
    out["hcpcs_rad_mri_per_bene"] = col("rad_mri") / benes
    out["hcpcs_rad_ct_per_bene"] = col("rad_ct") / benes

    # Drug / Infusion
    out["hcpcs_infusion_intensity"] = col("infusion_admin") / benes
    out["hcpcs_unclassified_jcode_share"] = col_paid("unclassified_jcodes") / total_paid
    out["hcpcs_hydration_share"] = safe_div(col("hydration"), col("infusion_admin"))
    out["hcpcs_chemo_per_bene"] = col("chemo_admin") / benes

    # Hospice / HHA
    out["hcpcs_hospice_cert_share"] = col_paid("hospice_cert") / total_paid
    out["hcpcs_hha_face_to_face_share"] = col_paid("hha_face_to_face") / total_paid

    # Telehealth
    out["hcpcs_tele_specific_hcpcs_share"] = col_paid("telehealth_specific") / total_paid

    return out


def _telehealth_pos_share(df: pd.DataFrame) -> pd.DataFrame:
    """Per NPI: fraction of services billed at POS 02 or 10 (telehealth)."""
    is_tele = df["Place_Of_Srvc"].astype(str).isin(["02", "10"]).values
    df2 = pd.DataFrame({
        "npi": df["npi"].values,
        "srvcs": df["Tot_Srvcs"].values,
        "tele_srvcs": np.where(is_tele, df["Tot_Srvcs"].values, 0.0),
    })
    agg = df2.groupby("npi", observed=True).agg(
        total_srvcs=("srvcs", "sum"),
        tele_srvcs=("tele_srvcs", "sum"),
    ).reset_index()
    agg["hcpcs_tele_pos_share"] = agg["tele_srvcs"] / agg["total_srvcs"].clip(lower=1)
    return agg[["npi", "hcpcs_tele_pos_share"]]


def _sentinel_per_code_features(df: pd.DataFrame) -> pd.DataFrame:
    """Compute per-NPI share/intensity for ~10 of the highest-signal sentinel codes."""
    HIGH_SIGNAL_SENTINELS = [
        ("36561", "port_insertion"),
        ("92950", "cpr"),
        ("G0444", "depression_screen"),
        ("H2019", "mh_community_svc"),
        ("Q4101", "skin_substitute_q4101"),
        ("J3490", "unclassified_drug_j3490"),
        ("95810", "sleep_study"),
        ("G0480", "tox_g0480"),
        ("88305", "path_88305"),
        ("66821", "yag_capsulotomy"),
    ]
    out = df.groupby("npi", observed=True).agg(
        total_paid=("paid", "sum"),
        total_srvcs=("Tot_Srvcs", "sum"),
        total_benes=("Tot_Benes", "sum"),
    ).reset_index()
    benes = out["total_benes"].clip(lower=1)
    total_paid = out["total_paid"].clip(lower=1)
    for code, name in HIGH_SIGNAL_SENTINELS:
        sub = df[df["HCPCS_Cd"] == code].groupby("npi", observed=True).agg(
            srvcs=("Tot_Srvcs", "sum"),
            paid=("paid", "sum"),
        ).reset_index()
        out = out.merge(sub, on="npi", how="left").fillna({"srvcs": 0, "paid": 0})
        out[f"hcpcs_sentinel_{name}_share"] = out["paid"] / total_paid
        out[f"hcpcs_sentinel_{name}_per_bene"] = out["srvcs"] / benes
        out = out.drop(columns=["srvcs", "paid"])
    return out.drop(columns=["total_paid", "total_srvcs", "total_benes"])


# ─────────────────────────────────────────────────────────────────────────────
# Main per-year pipeline
# ─────────────────────────────────────────────────────────────────────────────
def compute_year(year: int) -> pd.DataFrame:
    path = PARTB_DIR / f"cms_partb_svc_{year}.parquet"
    if not path.exists():
        raise FileNotFoundError(path)
    LOG.info("year=%d: loading %s", year, path)
    df = pd.read_parquet(path, columns=[
        "Rndrng_NPI", "HCPCS_Cd", "Rndrng_Prvdr_Type", "Place_Of_Srvc",
        "Tot_Srvcs", "Tot_Benes", "Tot_Bene_Day_Srvcs", "Avg_Mdcr_Pymt_Amt",
    ])
    # Normalize columns
    df = df.rename(columns={
        "Rndrng_NPI": "npi",
        "Rndrng_Prvdr_Type": "provider_type",
    })
    df["npi"] = df["npi"].astype(str).str.zfill(10)
    df["HCPCS_Cd"] = df["HCPCS_Cd"].astype(str)
    df["paid"] = df["Tot_Srvcs"] * df["Avg_Mdcr_Pymt_Amt"]
    df = df.dropna(subset=["npi", "HCPCS_Cd"])
    LOG.info("  rows: %d, unique NPIs: %d", len(df), df["npi"].nunique())

    # Stage 1: universal features
    LOG.info("  computing Tier-1 universal features...")
    npi_tot = _universal_features(df)

    # Stage 2: off-specialty + sentinel
    LOG.info("  computing off-specialty + sentinel-aggregate...")
    osfs = _off_specialty_and_sentinel(df)

    # Stage 3: code-family aggregates
    LOG.info("  computing Tier-2 code-family aggregates...")
    fams = _code_family_features(df)

    # Stage 4: derive ratios
    LOG.info("  deriving Tier-2 ratios...")
    ratios = _derive_ratios(npi_tot, fams)

    # Stage 5: telehealth POS
    LOG.info("  computing telehealth POS share...")
    tele = _telehealth_pos_share(df)

    # Stage 6: per-sentinel-code shares (Tier 3)
    LOG.info("  computing Tier-3 sentinel-code shares...")
    sentinels = _sentinel_per_code_features(df)

    # Merge everything
    final = ratios.merge(osfs, on="npi", how="left")
    final = final.merge(tele, on="npi", how="left")
    final = final.merge(sentinels, on="npi", how="left")
    final = final.fillna(0)
    LOG.info("  final shape: %s", final.shape)
    return final


def _add_specialty_percentiles(final: pd.DataFrame, partb_path: Path,
                                 feature_cols: list[str]) -> pd.DataFrame:
    """Compute within-specialty percentile for each feature column.

    Requires re-loading provider_type from the partb file.
    """
    pt = pd.read_parquet(partb_path, columns=["Rndrng_NPI", "Rndrng_Prvdr_Type"]).drop_duplicates()
    pt = pt.rename(columns={"Rndrng_NPI": "npi", "Rndrng_Prvdr_Type": "provider_type"})
    pt["npi"] = pt["npi"].astype(str).str.zfill(10)
    final = final.merge(pt, on="npi", how="left")

    LOG.info("  computing within-specialty + global percentiles for %d features", len(feature_cols))
    for col in feature_cols:
        # Within-specialty percentile
        final[f"{col}_pct"] = final.groupby("provider_type")[col].rank(pct=True, na_option="bottom")
        final[f"{col}_global_pct"] = final[col].rank(pct=True, na_option="bottom")
    return final


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--year", type=int)
    ap.add_argument("--all-years", action="store_true")
    ap.add_argument("--out-dir", type=Path, default=OUTPUT_DIR)
    ap.add_argument("--no-percentiles", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    args.out_dir.mkdir(parents=True, exist_ok=True)

    if args.all_years:
        years = YEARS
    elif args.year:
        years = [args.year]
    else:
        LOG.error("specify --year YYYY or --all-years")
        return 2

    for y in years:
        final = compute_year(y)
        feature_cols = [c for c in final.columns
                        if c.startswith("hcpcs_") and not c.endswith(("_pct", "_global_pct"))]
        LOG.info("  base feature columns: %d", len(feature_cols))
        if not args.no_percentiles:
            partb_path = PARTB_DIR / f"cms_partb_svc_{y}.parquet"
            final = _add_specialty_percentiles(final, partb_path, feature_cols)

        # Drop helper columns
        drop_cols = [c for c in final.columns if c.startswith("fam_") or c in {"top1_srvcs"}]
        final = final.drop(columns=drop_cols, errors="ignore")
        LOG.info("  final columns: %d", len(final.columns))

        out_path = args.out_dir / f"hcpcs_rich_{y}.parquet"
        if not args.dry_run:
            final.to_parquet(out_path, index=False)
            LOG.info("  wrote → %s", out_path)
        else:
            LOG.info("  --dry-run: not writing")
    return 0


if __name__ == "__main__":
    sys.exit(main())
