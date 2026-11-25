"""Multi-source rule discovery v2 — wider grid, finer peer groups, mix of
general + specialty-scoped rules. Optimizes within-specialty recall.

Differences vs v1 (`discover_multisource_rules.py`):
  - Peer-z grouping uses ``partb_provider_type × partb_state`` (granular),
    not the coarse ``nppes_taxonomy_bucket``
  - Adds GENERAL bucket (no specialty filter) so cross-specialty
    signatures (opioid mills, antipsychotic over-prescribers,
    pill-mill phantom-billing patterns) get candidates
  - Wider grid: percentile thresholds {0.50, 0.65, 0.75, 0.85, 0.90, 0.95}
    × volume floors {25, 50, 100, 250}
  - Adds inverse-feature predicates (``feat_pct ≤ low_threshold``) so we
    can build composites like "high partd_clms_per_bene AND low
    partb_n_services" for pill-mill catches
  - Selection metric: within-bucket recall (not raw TP); precision
    threshold relaxed to 0.1% (acknowledges that high-FP rules may
    catch unlabeled fraud — Phase 2 ranker will reorder)
  - Orthogonality matrix scoped to qualifying candidates only (faster)

Usage:
    python scripts/experiments/discover_multisource_rules_v2.py --year 2018 \
        --out results/rule_candidates_v2_2018
"""
from __future__ import annotations

import argparse
import json
import sys
import warnings
from itertools import combinations, product
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fraudlib.labels.ground_truth import load_positives_scoped

CACHE_DIRS = [
    Path("data/cache/feature_frames"),
]
DEFAULT_OUT_DIR = Path("results/rule_candidates_v3")

# Granular partb_provider_type buckets — fine-grained, matched against
# partb_provider_type via case-insensitive exact-or-prefix.
SPECIALTY_BUCKETS: dict[str, list[str]] = {
    "Family_Practice":      ["Family Practice"],
    "Internal_Medicine":    ["Internal Medicine"],
    "General_Practice":     ["General Practice"],
    "Nurse_Practitioner":   ["Nurse Practitioner"],
    "Physician_Assistant":  ["Physician Assistant"],
    "Psychiatry":           ["Psychiatry", "Geriatric Psychiatry", "Neuropsychiatry"],
    "Chiropractic":         ["Chiropractic"],
    "Emergency_Medicine":   ["Emergency Medicine"],
    "Anesthesiology":       ["Anesthesiology"],
    "CRNA":                 ["Certified Registered Nurse Anesthetist (CRNA)",
                              "Certified Registered Nurse Anesthetist"],
    "Pain_Management":      ["Pain Management", "Interventional Pain Management"],
    "PMR":                  ["Physical Medicine and Rehabilitation"],
    "Cardiology":           ["Cardiology", "Interventional Cardiology",
                              "Cardiac Electrophysiology"],
    "Orthopedic":           ["Orthopedic Surgery"],
    "OB_GYN":               ["Obstetrics & Gynecology"],
    "LCSW":                 ["Licensed Clinical Social Worker"],
    "Psychologist":         ["Psychologist, Clinical", "Clinical Psychologist",
                              "Psychologist (billing independently)"],
    "Podiatry":             ["Podiatry"],
    "General_Surgery":      ["General Surgery"],
    "Dermatology":          ["Dermatology"],
    "Hematology_Oncology":  ["Hematology/Oncology", "Medical Oncology",
                              "Hematology", "Hematology-Oncology"],
    # V3 combined buckets (aliases for rule matching)
    "Pain_PMR":             ["Pain Management", "Interventional Pain Management",
                              "Anesthesiology",
                              "Physical Medicine and Rehabilitation"],
    "NP_PA":                ["Nurse Practitioner", "Physician Assistant"],
    "Surgery":              ["General Surgery", "Orthopedic Surgery",
                              "Thoracic Surgery", "Vascular Surgery", "Neurosurgery"],
    "Hem_Onc":              ["Hematology-Oncology", "Hematology/Oncology",
                              "Medical Oncology", "Radiation Oncology"],
    "Ophthalmology":        ["Ophthalmology", "Optometry"],
    "LCSW_Psych":           ["Licensed Clinical Social Worker", "Psychologist",
                              "Clinical Psychologist",
                              "Psychologist, Clinical",
                              "Psychologist (billing independently)"],
    "Nephrology":           ["Nephrology"],
    "Neurology":            ["Neurology"],
    "Pulmonary":            ["Pulmonary Disease"],
    "Gastro":               ["Gastroenterology"],
    "PT_OT":                ["Physical Therapist in Private Practice",
                              "Occupational Therapist in Private Practice"],
}
GENERAL_BUCKET = "GENERAL"

SCANNABLE_FEATURES = [
    # Part B raw
    "partb_n_beneficiaries",
    "partb_n_services",
    "partb_total_medicare_payment",
    "partb_n_hcpcs_codes",
    # Part B derived (v3 rule features)
    "partb_payment_per_service",
    "partb_services_per_beneficiary",
    "partb_hcpcs_diversity",
    "partb_pay_per_bene",
    "partb_svcs_per_bene",
    "partb_dual_pct",
    "partb_under65_pct",
    "partb_over84_pct",
    "partb_drug_frac",
    "partb_chrg_to_allowed",
    "partb_risk_score",
    "partb_avg_age",
    # Part D
    "partd_opioid_clm_share",
    "partd_opioid_la_clm_share",
    "partd_antipsychotic_ge65_clm_share",
    "partd_antibiotic_clm_share",
    "partd_brand_clm_share",
    "partd_cost_per_clm",
    "partd_cost_per_bene",
    "partd_clms_per_bene",
    "partd_days_per_clm",
    # HCPCS
    "hcpcs_em_high_low_ratio",
    "hcpcs_em_high_share",
    "hcpcs_em_distribution_entropy",
    "hcpcs_em_new_vs_estab_ratio",
    "hcpcs_podiatry_debride_routine_ratio",
    "hcpcs_cataract_complex_ratio",
    "hcpcs_intravitreal_injection_intensity",
    "hcpcs_jcode_chemo_share",
    "hcpcs_jcode_eye_injection_share",
    "hcpcs_jcode_biologic_share",
]

VOLUME_FLOORS = [
    ("partb_n_beneficiaries", 25),
    ("partb_n_beneficiaries", 50),
    ("partb_n_beneficiaries", 100),
    ("partb_n_beneficiaries", 250),
]
HI_THRESHOLDS = [0.75, 0.85, 0.90, 0.95]
LO_THRESHOLDS = [0.10, 0.25, 0.50]   # for inverse predicates


def _find_cache(name: str) -> Path | None:
    for d in CACHE_DIRS:
        p = d / name
        if p.exists():
            return p
    return None


def _load_year(year: int) -> pd.DataFrame:
    for name in (f"test_{year}.parquet", f"val_{year}.parquet"):
        p = _find_cache(name)
        if p is not None:
            return pd.read_parquet(p)
    for d in CACHE_DIRS:
        for f in d.glob("train_*.parquet"):
            df = pd.read_parquet(f)
            if "puf_year" in df.columns and (df["puf_year"] == year).any():
                return df[df["puf_year"] == year].reset_index(drop=True)
    raise FileNotFoundError(f"no cached multi-source frame for year={year}")


def _stamp_positives(df: pd.DataFrame, year: int) -> pd.DataFrame:
    doj = set(load_positives_scoped(year, min_confidence="any").keys())
    doj = {str(n).zfill(10) for n in doj}
    df = df.copy()
    npi_str = df["npi"].astype(str).str.zfill(10)
    df["y_eval"] = npi_str.isin(doj).astype(int)
    df["y_doj"] = npi_str.isin(doj).astype(int)
    return df


def _add_peer_pcts(df: pd.DataFrame, features: list[str]) -> pd.DataFrame:
    """Compute peer percentiles within (partb_provider_type, partb_state).

    Also computes GLOBAL percentiles (across the entire panel) for
    cross-specialty general rules, written as ``<feature>_global_pct``.
    """
    df = df.copy()
    if "partb_provider_type" not in df.columns or "partb_state" not in df.columns:
        return df
    g = df.groupby(["partb_provider_type", "partb_state"], observed=True)
    for col in features:
        if col in df.columns:
            df[f"{col}_pct"] = g[col].rank(pct=True, na_option="keep")
            df[f"{col}_global_pct"] = df[col].rank(pct=True, na_option="keep")
    return df


def _bucket_mask(df: pd.DataFrame, terms: list[str]) -> pd.Series:
    """Match against partb_provider_type only (granular)."""
    pat = "|".join(map(lambda t: t.replace(" ", r"\s+"), terms))
    typ = df.get("partb_provider_type")
    if typ is None:
        return pd.Series(False, index=df.index)
    return typ.astype(str).str.contains(pat, case=False, regex=True, na=False)


def _profile_specialty(
    df: pd.DataFrame, bucket_name: str, terms: list[str] | None
) -> pd.DataFrame:
    """For each feature, compare positives vs peers within the bucket.

    ``terms=None`` → general bucket (no specialty filter); use GLOBAL
    percentiles instead of per-specialty.
    """
    if terms is None:
        sub = df.copy()
        pct_suffix = "_global_pct"
    else:
        in_bucket = _bucket_mask(df, terms)
        sub = df[in_bucket].copy()
        pct_suffix = "_pct"
    n_total = len(sub)
    n_pos = int(sub["y_eval"].sum())
    if n_pos < 3 or n_total < 100:
        return pd.DataFrame()
    rows = []
    for feat in SCANNABLE_FEATURES:
        col = f"{feat}{pct_suffix}"
        if col not in sub.columns:
            continue
        pos_pct = sub.loc[sub["y_eval"] == 1, col].dropna()
        peer_pct = sub.loc[sub["y_eval"] == 0, col].dropna()
        if len(pos_pct) < 3 or len(peer_pct) < 100:
            continue
        rows.append({
            "bucket": bucket_name,
            "feature": feat,
            "pct_suffix": pct_suffix,
            "n_pos": int(len(pos_pct)),
            "n_peer": int(len(peer_pct)),
            "pos_median_pct": float(pos_pct.median()),
            "peer_median_pct": float(peer_pct.median()),
            "differential": float(pos_pct.median() - peer_pct.median()),
        })
    return pd.DataFrame(rows)


def _generate_candidates(profile: pd.DataFrame, top_n_per_bucket: int = 8) -> list[dict]:
    cands: list[dict] = []
    if profile.empty:
        return cands
    for bucket in profile["bucket"].unique():
        sub = profile[profile["bucket"] == bucket].sort_values(
            "differential", ascending=False
        )
        # Keep features with ≥0.10 differential (or ≤-0.10 inverse)
        hi = sub[sub["differential"] >= 0.10].head(top_n_per_bucket)
        lo = sub[sub["differential"] <= -0.10].head(top_n_per_bucket)
        pct_suffix = sub["pct_suffix"].iloc[0] if not sub.empty else "_pct"
        feats_hi = hi["feature"].tolist()
        feats_lo = lo["feature"].tolist()
        # Single-feature high-direction candidates
        for feat, thresh, (vol_col, vol_n) in product(feats_hi, HI_THRESHOLDS, VOLUME_FLOORS):
            cands.append({
                "id": f"{bucket}__{feat}__hi{int(thresh*100):02d}__v{vol_n}",
                "bucket": bucket,
                "predicate_type": "single_hi",
                "feature_pct": f"{feat}{pct_suffix}",
                "threshold": thresh,
                "volume_col": vol_col,
                "volume_floor": vol_n,
            })
        # Single-feature low-direction candidates (inverse)
        for feat, thresh, (vol_col, vol_n) in product(feats_lo, LO_THRESHOLDS, VOLUME_FLOORS):
            cands.append({
                "id": f"{bucket}__{feat}__lo{int(thresh*100):02d}__v{vol_n}",
                "bucket": bucket,
                "predicate_type": "single_lo",
                "feature_pct": f"{feat}{pct_suffix}",
                "threshold": thresh,
                "volume_col": vol_col,
                "volume_floor": vol_n,
            })
        # Pair candidates: top-3 hi × top-3 hi
        for f1, f2 in combinations(feats_hi[:4], 2):
            for t1, t2, (vol_col, vol_n) in product([0.85, 0.90], [0.75, 0.85], VOLUME_FLOORS):
                cands.append({
                    "id": f"{bucket}__{f1}_hi{int(t1*100):02d}__{f2}_hi{int(t2*100):02d}__v{vol_n}",
                    "bucket": bucket,
                    "predicate_type": "pair_hi_hi",
                    "feature_pct_1": f"{f1}{pct_suffix}",
                    "threshold_1": t1,
                    "feature_pct_2": f"{f2}{pct_suffix}",
                    "threshold_2": t2,
                    "volume_col": vol_col,
                    "volume_floor": vol_n,
                })
        # Pair: top-2 hi × top-2 lo (e.g. high partd AND low partb_n_services)
        for f_hi, f_lo in product(feats_hi[:3], feats_lo[:3]):
            for t_hi, t_lo, (vol_col, vol_n) in product([0.85, 0.90], [0.25, 0.50], VOLUME_FLOORS):
                cands.append({
                    "id": f"{bucket}__{f_hi}_hi{int(t_hi*100):02d}__{f_lo}_lo{int(t_lo*100):02d}__v{vol_n}",
                    "bucket": bucket,
                    "predicate_type": "pair_hi_lo",
                    "feature_pct_1": f"{f_hi}{pct_suffix}",
                    "threshold_1": t_hi,
                    "feature_pct_2": f"{f_lo}{pct_suffix}",
                    "threshold_2": t_lo,
                    "volume_col": vol_col,
                    "volume_floor": vol_n,
                })
    return cands


def _eval_candidate(
    df: pd.DataFrame, cand: dict, bucket_terms: dict[str, list[str]]
) -> dict:
    if cand["bucket"] == GENERAL_BUCKET:
        bm = pd.Series(True, index=df.index)
    else:
        terms = bucket_terms.get(cand["bucket"])
        bm = _bucket_mask(df, terms) if terms else pd.Series(True, index=df.index)

    ptype = cand["predicate_type"]
    if ptype == "single_hi":
        col = cand["feature_pct"]
        if col not in df.columns:
            return {**cand, "n_fires": 0, "tp": 0, "fp": 0, "precision": 0.0,
                    "recall_within_bucket": 0.0, "fire_npis": frozenset(),
                    "n_pos_bucket": 0}
        m = bm & (df[col] >= cand["threshold"]) & (df[cand["volume_col"]] >= cand["volume_floor"])
    elif ptype == "single_lo":
        col = cand["feature_pct"]
        if col not in df.columns:
            return {**cand, "n_fires": 0, "tp": 0, "fp": 0, "precision": 0.0,
                    "recall_within_bucket": 0.0, "fire_npis": frozenset(),
                    "n_pos_bucket": 0}
        m = bm & (df[col] <= cand["threshold"]) & (df[cand["volume_col"]] >= cand["volume_floor"])
    elif ptype == "pair_hi_hi":
        c1, c2 = cand["feature_pct_1"], cand["feature_pct_2"]
        if c1 not in df.columns or c2 not in df.columns:
            return {**cand, "n_fires": 0, "tp": 0, "fp": 0, "precision": 0.0,
                    "recall_within_bucket": 0.0, "fire_npis": frozenset(),
                    "n_pos_bucket": 0}
        m = (bm & (df[c1] >= cand["threshold_1"])
             & (df[c2] >= cand["threshold_2"])
             & (df[cand["volume_col"]] >= cand["volume_floor"]))
    elif ptype == "pair_hi_lo":
        c1, c2 = cand["feature_pct_1"], cand["feature_pct_2"]
        if c1 not in df.columns or c2 not in df.columns:
            return {**cand, "n_fires": 0, "tp": 0, "fp": 0, "precision": 0.0,
                    "recall_within_bucket": 0.0, "fire_npis": frozenset(),
                    "n_pos_bucket": 0}
        m = (bm & (df[c1] >= cand["threshold_1"])
             & (df[c2] <= cand["threshold_2"])
             & (df[cand["volume_col"]] >= cand["volume_floor"]))
    else:
        return {**cand, "n_fires": 0, "tp": 0, "fp": 0, "precision": 0.0,
                "recall_within_bucket": 0.0, "fire_npis": frozenset(),
                "n_pos_bucket": 0}

    n_fires = int(m.sum())
    tp = int((m & (df["y_eval"] == 1)).sum())
    fp = n_fires - tp
    n_pos_bucket = int((bm & (df["y_eval"] == 1)).sum())
    fires = df.loc[m, "npi"].astype(str).str.zfill(10)
    return {
        **cand,
        "n_fires": n_fires,
        "tp": tp,
        "fp": fp,
        "n_pos_bucket": n_pos_bucket,
        "precision": tp / n_fires if n_fires else 0.0,
        "recall_within_bucket": tp / n_pos_bucket if n_pos_bucket else 0.0,
        "fire_npis": frozenset(fires.tolist()),
    }


def _orthogonality_matrix(qualifying: list[dict]) -> pd.DataFrame:
    rows = []
    fired = [(e["id"], e["fire_npis"]) for e in qualifying if e["n_fires"] > 0]
    for (i, (id_a, set_a)), (j, (id_b, set_b)) in combinations(enumerate(fired), 2):
        if not set_a or not set_b:
            continue
        inter = len(set_a & set_b)
        union = len(set_a | set_b)
        rows.append({
            "rule_a": id_a,
            "rule_b": id_b,
            "intersect_npis": inter,
            "union_npis": union,
            "jaccard": inter / union if union else 0.0,
        })
    return pd.DataFrame(rows)


def _select_non_redundant(
    qualifying: list[dict],
    orth: pd.DataFrame,
    jaccard_max: float = 0.7,
) -> list[dict]:
    """Greedy selection keyed by within-bucket recall × precision."""
    qual_sorted = sorted(
        qualifying,
        key=lambda e: (e["recall_within_bucket"], e["precision"], e["tp"]),
        reverse=True,
    )
    j: dict[tuple[str, str], float] = {}
    for _, row in orth[orth["jaccard"] >= jaccard_max].iterrows():
        a, b = row["rule_a"], row["rule_b"]
        j[(a, b)] = j[(b, a)] = float(row["jaccard"])
    chosen: list[dict] = []
    chosen_ids: set[str] = set()
    for cand in qual_sorted:
        if any((cand["id"], c) in j for c in chosen_ids):
            continue
        chosen.append(cand)
        chosen_ids.add(cand["id"])
    return chosen


def _serializable(d: dict) -> dict:
    return {k: v for k, v in d.items() if k != "fire_npis"}


def run(
    year: int,
    out_dir: Path,
    min_tp: int = 3,
    min_within_bucket_recall: float = 0.05,
    min_precision: float = 0.001,
    jaccard_max: float = 0.7,
    verbose: bool = True,
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    if verbose:
        print(f"[1/8] Loading multi-source frame for {year}...")
    df = _load_year(year)
    if verbose:
        print(f"      shape={df.shape}")
    if verbose:
        print(f"[2/8] Stamping positives...")
    df = _stamp_positives(df, year)
    n_pos = int(df["y_eval"].sum())
    n_doj = int(df["y_doj"].sum())
    if verbose:
        print(f"      positives: {n_pos} (DOJ={n_doj}, LEIE-only={int(df['y_leie_only'].sum())})")
    if verbose:
        print(f"[3/8] Computing peer percentiles within (provider_type, state) + global...")
    df = _add_peer_pcts(df, SCANNABLE_FEATURES)
    if verbose:
        print(f"[4/8] Profiling positives vs peers per bucket...")
    profiles = []
    for name, terms in SPECIALTY_BUCKETS.items():
        p = _profile_specialty(df, name, terms)
        if not p.empty:
            profiles.append(p)
    p = _profile_specialty(df, GENERAL_BUCKET, None)
    if not p.empty:
        profiles.append(p)
    profile = pd.concat(profiles, ignore_index=True) if profiles else pd.DataFrame()
    profile.to_csv(out_dir / "profile.csv", index=False)
    if verbose:
        print(f"      {len(profile)} (bucket, feature) signal rows across "
              f"{profile['bucket'].nunique() if not profile.empty else 0} buckets")
    if verbose:
        print(f"[5/8] Generating candidates...")
    candidates = _generate_candidates(profile, top_n_per_bucket=8)
    bucket_terms = {**SPECIALTY_BUCKETS}
    if verbose:
        print(f"      {len(candidates):,} candidates")
    if verbose:
        print(f"[6/8] Evaluating each candidate...")
    evaluations = []
    for i, c in enumerate(candidates):
        evaluations.append(_eval_candidate(df, c, bucket_terms))
        if verbose and (i + 1) % 500 == 0:
            print(f"      {i + 1}/{len(candidates)}")
    eval_df = pd.DataFrame([_serializable(e) for e in evaluations])
    eval_df.to_csv(out_dir / "candidate_evaluations.csv", index=False)
    qualifying = [
        e for e in evaluations
        if e["tp"] >= min_tp
        and e["recall_within_bucket"] >= min_within_bucket_recall
        and e["precision"] >= min_precision
    ]
    if verbose:
        print(f"      qualifying (TP≥{min_tp}, recall≥{min_within_bucket_recall:.0%}, "
              f"precision≥{min_precision:.1%}): {len(qualifying)}")
    if verbose:
        print(f"[7/8] Computing orthogonality (over qualifying only)...")
    orth = _orthogonality_matrix(qualifying)
    orth.to_csv(out_dir / "orthogonality_matrix.csv", index=False)
    if verbose:
        print(f"      {len(orth):,} pairs")
    if verbose:
        print(f"[8/8] Greedy non-redundant selection (Jaccard ≤ {jaccard_max})...")
    selected = _select_non_redundant(qualifying, orth, jaccard_max=jaccard_max)
    selected_ser = [_serializable(s) for s in selected]
    (out_dir / "selected_rules.json").write_text(
        json.dumps({"year": year, "n_selected": len(selected),
                    "rules": selected_ser}, indent=2)
    )
    union: set[str] = set()
    for s in selected:
        union |= s["fire_npis"]
    pos_npis = set(df.loc[df["y_eval"] == 1, "npi"].astype(str).str.zfill(10))
    tp_union = len(union & pos_npis)
    if verbose:
        print(f"\n=== SELECTED: {len(selected)} rules ===")
        for s in selected[:30]:
            print(f"  recall_b={s['recall_within_bucket']:.1%} TP={s['tp']:>3} "
                  f"prec={s['precision']:.3%} fires={s['n_fires']:>6}  {s['id']}")
        if len(selected) > 30:
            print(f"  ... ({len(selected) - 30} more)")
        print(f"\nUnion: {len(union):,} candidates, TP={tp_union}, "
              f"recall={tp_union/max(n_pos,1):.1%} "
              f"precision={tp_union/max(len(union),1):.3%}")
    summary = [
        f"# Multi-source rule discovery v2 — {year}",
        "",
        f"- Frame rows: {len(df):,}",
        f"- Positives: {n_pos:,} (DOJ={n_doj}, LEIE-only={int(df['y_leie_only'].sum())})",
        f"- Buckets profiled: {profile['bucket'].nunique() if not profile.empty else 0}",
        f"- Candidates generated: {len(candidates):,}",
        f"- Qualifying: {len(qualifying):,}",
        f"- Selected (non-redundant): {len(selected):,}",
        "",
        "## Union performance",
        "",
        f"- Candidates: {len(union):,}",
        f"- TP: {tp_union}",
        f"- Recall: {tp_union/max(n_pos,1):.1%}",
        f"- Precision: {tp_union/max(len(union),1):.3%}",
    ]
    (out_dir / "summary.md").write_text("\n".join(summary) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--year", type=int, default=2018)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT_DIR)
    ap.add_argument("--min-tp", type=int, default=3)
    ap.add_argument("--min-recall-within-bucket", type=float, default=0.05)
    ap.add_argument("--min-precision", type=float, default=0.001)
    ap.add_argument("--jaccard-max", type=float, default=0.7)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=FutureWarning)
        run(
            args.year,
            args.out,
            min_tp=args.min_tp,
            min_within_bucket_recall=args.min_recall_within_bucket,
            min_precision=args.min_precision,
            jaccard_max=args.jaccard_max,
            verbose=not args.quiet,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
