"""Ablation harness for the per-archetype + cohort-anomaly + RRF pipeline.

One script, multiple flags, each producing a comparable top-K metrics
output. Designed to fill all the gaping holes from the 2026-04-28
critical review.

Variants supported via flags:

  --no-pecos                  Drop PECOS features (test PECOS leakage)
  --null-label                Shuffle y_eval before training (signal sanity)
  --no-taxonomy               Drop nppes_taxonomy_bucket
  --no-rule-fires             Drop rule__* feature columns. (STALE NOTE FIXED
                              2026-06-09: in v4 the panel filter is the AGENT
                              filter, not rules — rules_v3 is features-only.
                              For v4 ablations use NO_RULE_FEATURES=1 with
                              walk_forward_v4.py, not this v1 harness.)
  --drop-archetypes ARCH,...  Drop specified archetypes; remap their
                              positives to BILLING_FRAUD
  --aggregation MODE          sum (default), max, rank, count
  --no-filter                 Don't apply the rule filter (use full panel)

Same NPI-symmetric holdout, same XGBoost hyperparams as the v1 baseline
so comparisons are clean.

Usage:
    # Test PECOS leakage
    python scripts/experiments/run_ablation.py --no-pecos --label no_pecos
    # Null-label sanity
    python scripts/experiments/run_ablation.py --null-label --label null
    # Drop thin archetypes
    python scripts/experiments/run_ablation.py --drop-archetypes OPIOID_MILL,MEDICALLY_UNNECESSARY --label drop_thin
"""
from __future__ import annotations

import argparse
import json
import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fraudlib.eval.archetypes import ARCHETYPES, _fraud_types_to_schemes
from fraudlib.labels.ground_truth import load_positives_scoped
from scripts.experiments.train_per_archetype_rankers import (
    K_VALUES,
    _NON_FEATURE_COLS_EXTENDED,
    _attach_pecos,
    _stamp_archetype,
    _train_one_archetype,
)
from scripts.experiments.discover_multisource_rules_v2 import (
    SCANNABLE_FEATURES,
    _add_peer_pcts,
)
from scripts.experiments.train_filtered_ranker import (
    _apply_rules_get_fires,
    _load_year,
)

DEFAULT_OUT_DIR = Path("results/ablations")

PECOS_COLS = (
    "pecos_partb_eligible", "pecos_dme_eligible", "pecos_hha_eligible",
    "pecos_pmd_eligible", "pecos_in_directory",
    "pecos_hospice_eligible",
    "pecos_has_ordering_privilege", "pecos_has_referring_privilege",
    "pecos_n_privileges",
)

# Population means computed on the PECOS Order_and_Referring directory
# itself (Apr 2026 snapshot). Imputing missing-from-PECOS NPIs with these
# values masks the "missing = excluded" leak by making them indistinguishable
# from an average in-PECOS provider on each binary flag.
PECOS_POP_MEANS = {
    "pecos_partb_eligible": 0.964,
    "pecos_dme_eligible": 1.000,
    "pecos_hha_eligible": 0.845,
    "pecos_pmd_eligible": 0.795,
    "pecos_hospice_eligible": 0.571,
    "pecos_has_ordering_privilege": 1.000,
    "pecos_has_referring_privilege": 0.964,
    "pecos_n_privileges": 3.99,
}


def _stamp_archetype_pooled_leie(df: pd.DataFrame, year: int) -> pd.DataFrame:
    """Apples-to-apples literature-equivalent label set: ALL LEIE codes
    (not the billing-fraud-tight subset) ∪ DOJ. Matches Bauder 2017 /
    Hancock 2021 evaluation setups.
    """
    out: dict[str, str] = {}
    # DOJ priority
    doj = load_positives_scoped(year, min_confidence="any")
    for npi, meta in doj.items():
        schemes = _fraud_types_to_schemes(meta.get("fraud_types", []))
        out[str(npi).zfill(10)] = sorted(schemes)[0] if schemes else "PHANTOM_BILLING"
    # LEIE — POOLED (fraud_only=True with default codes = permissive set)
    leie_npis = post_year_excluded_npis(
        year, lookahead_years=5, fraud_only=True, codes=None,
    )
    for npi in leie_npis:
        npi_p = str(npi).zfill(10)
        if npi_p not in out:
            out[npi_p] = "BILLING_FRAUD"  # default bucket for un-tagged LEIE
    npi_str = df["npi"].astype(str).str.zfill(10)
    df = df.copy()
    df["npi_padded"] = npi_str
    df["archetype"] = npi_str.map(out).fillna("")
    df["y_eval"] = (df["archetype"] != "").astype(int)
    return df


def _attach_pecos_mean_imputed(df: pd.DataFrame) -> pd.DataFrame:
    """LEFT JOIN PECOS but impute missing-from-PECOS with population means.

    Removes the "missing-from-PECOS = excluded post-hoc" leak channel:
    for NPIs not in PECOS, their pecos_* fields are set to the
    population mean of in-PECOS providers, making them statistically
    indistinguishable from an average enrolled provider.

    Note: pecos_in_directory is intentionally NOT imputed (kept as
    True/False boolean) to allow evaluating whether the model still
    cheats via that column. Drop it via --no-taxonomy-style flag if
    you want to test the no-leak hypothesis end-to-end.
    """
    from fraudlib.ingest.providers.pecos import load_pecos_features
    from pathlib import Path as _Path
    pecos_path = _Path("data/registries/pecos_ordering_referring.parquet")
    if not pecos_path.exists():
        return df
    pecos = load_pecos_features(parquet_path=pecos_path)
    pecos["pecos_in_directory"] = True
    df = df.copy()
    df["npi_padded"] = df.get("npi_padded", df["npi"].astype(str).str.zfill(10))
    df = df.merge(
        pecos.rename(columns={"NPI": "npi_padded"}),
        on="npi_padded", how="left",
    )
    in_dir = df["pecos_in_directory"].fillna(False).astype(bool)
    for col, mean in PECOS_POP_MEANS.items():
        if col in df.columns:
            df.loc[~in_dir, col] = mean
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(mean)
    df["pecos_in_directory"] = in_dir.astype(int)
    return df


def _prepare_year_ablation(
    year: int,
    rules: list[dict],
    *,
    use_pecos: bool = True,
    pecos_mean_impute: bool = False,
    apply_filter: bool = True,
    add_rule_features: bool = True,
    labels: str = "tight",
) -> pd.DataFrame:
    df = _load_year(year)
    if labels == "pooled_leie":
        df = _stamp_archetype_pooled_leie(df, year)
    else:
        df = _stamp_archetype(df, year)
    if use_pecos:
        if pecos_mean_impute:
            df = _attach_pecos_mean_imputed(df)
        else:
            df = _attach_pecos(df)
    # Derive v3 rule features from raw columns
    if "partb_n_beneficiaries" in df.columns:
        benes = df["partb_n_beneficiaries"].clip(lower=1)
        svcs = df.get("partb_n_services", benes).clip(lower=1)
        pay = df.get("partb_total_medicare_payment", benes).clip(lower=1)
        allowed = df.get("partb_total_medicare_allowed", pay).clip(lower=1)
        if "partb_pay_per_bene" not in df.columns:
            df["partb_pay_per_bene"] = pay / benes
        if "partb_svcs_per_bene" not in df.columns:
            df["partb_svcs_per_bene"] = svcs / benes
        if "partb_dual_pct" not in df.columns and "partb_bene_dual_cnt" in df.columns:
            df["partb_dual_pct"] = df["partb_bene_dual_cnt"].fillna(0) / benes
        if "partb_under65_pct" not in df.columns and "partb_bene_age_lt_65_cnt" in df.columns:
            df["partb_under65_pct"] = df["partb_bene_age_lt_65_cnt"].fillna(0) / benes
        if "partb_over84_pct" not in df.columns and "partb_bene_age_gt_84_cnt" in df.columns:
            df["partb_over84_pct"] = df["partb_bene_age_gt_84_cnt"].fillna(0) / benes
        if "partb_drug_frac" not in df.columns and "partb_drug_mdcr_pymt_amt" in df.columns:
            df["partb_drug_frac"] = df["partb_drug_mdcr_pymt_amt"].fillna(0) / pay
        if "partb_chrg_to_allowed" not in df.columns and "partb_tot_sbmtd_chrg" in df.columns:
            df["partb_chrg_to_allowed"] = df["partb_tot_sbmtd_chrg"].fillna(0) / allowed
        if "partb_risk_score" not in df.columns and "partb_bene_avg_risk_score" in df.columns:
            df["partb_risk_score"] = df["partb_bene_avg_risk_score"]
        if "partb_avg_age" not in df.columns and "partb_bene_avg_age" in df.columns:
            df["partb_avg_age"] = df["partb_bene_avg_age"]

    df = _add_peer_pcts(df, SCANNABLE_FEATURES)
    rule_df, any_fires = _apply_rules_get_fires(df, rules)
    if add_rule_features:
        df = pd.concat([df, rule_df], axis=1)
    if apply_filter:
        df = df[any_fires].reset_index(drop=True)
    df["puf_year"] = year
    return df


def _drop_columns(df: pd.DataFrame, drop_pecos: bool, drop_taxonomy: bool,
                  drop_rule_fires: bool, drop_pecos_indicator: bool = False) -> pd.DataFrame:
    cols_to_drop = []
    if drop_pecos:
        cols_to_drop += [c for c in PECOS_COLS if c in df.columns]
    if drop_pecos_indicator and "pecos_in_directory" in df.columns:
        cols_to_drop.append("pecos_in_directory")
    if drop_taxonomy:
        for c in ("nppes_taxonomy_bucket",):
            if c in df.columns:
                cols_to_drop.append(c)
    if drop_rule_fires:
        cols_to_drop += [c for c in df.columns if c.startswith("rule__")]
    if cols_to_drop:
        df = df.drop(columns=cols_to_drop)
    return df


def _remap_archetypes(df: pd.DataFrame, drop: tuple[str, ...]) -> pd.DataFrame:
    if not drop:
        return df
    df = df.copy()
    df.loc[df["archetype"].isin(drop), "archetype"] = "BILLING_FRAUD"
    return df


def _shuffle_labels(df: pd.DataFrame, seed: int = 42) -> pd.DataFrame:
    """Shuffle y_eval and archetype together (preserves marginal distribution)."""
    df = df.copy()
    rng = np.random.default_rng(seed)
    perm = rng.permutation(len(df))
    df["y_eval"] = df["y_eval"].values[perm]
    df["archetype"] = df["archetype"].values[perm]
    return df


def _feature_matrix(df: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    numeric_cols = [
        c for c in df.select_dtypes(include="number").columns
        if c not in _NON_FEATURE_COLS_EXTENDED
    ]
    cat_cols = [
        c for c in df.select_dtypes(include="category").columns
        if c not in _NON_FEATURE_COLS_EXTENDED
    ]
    feature_cols = numeric_cols + cat_cols
    X = df[feature_cols].copy()
    X[numeric_cols] = X[numeric_cols].fillna(0.0)
    return X, feature_cols


def _aggregate(scores_df: pd.DataFrame, mode: str) -> pd.Series:
    cols = [c for c in scores_df.columns if c.startswith("score_")]
    if mode == "sum":
        return scores_df[cols].sum(axis=1)
    if mode == "max":
        return scores_df[cols].max(axis=1)
    if mode == "rank":
        ranks = pd.DataFrame({
            c: (-scores_df[c]).rank(method="min") for c in cols
        })
        return -ranks.sum(axis=1)  # negate so higher = better
    if mode == "count":
        thresh = 0.5
        return (scores_df[cols] > thresh).sum(axis=1).astype(float)
    raise ValueError(f"unknown agg mode: {mode}")


def run(
    rules_path: Path,
    train_years: tuple[int, ...],
    val_year: int,
    test_years: tuple[int, ...],
    out_dir: Path,
    label: str,
    *,
    use_pecos: bool = True,
    pecos_mean_impute: bool = False,
    drop_pecos_indicator: bool = False,
    drop_taxonomy: bool = False,
    drop_rule_fires: bool = False,
    drop_archetypes: tuple[str, ...] = (),
    null_label: bool = False,
    apply_filter: bool = True,
    aggregation: str = "sum",
    labels: str = "tight",
    verbose: bool = True,
) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    rules = json.loads(rules_path.read_text())["rules"]
    if verbose:
        print(f"[{label}] Loading frames + filter (use_pecos={use_pecos} "
              f"apply_filter={apply_filter} null_label={null_label})...")

    train_frames = [_prepare_year_ablation(
        y, rules, use_pecos=use_pecos,
        pecos_mean_impute=pecos_mean_impute,
        apply_filter=apply_filter,
        add_rule_features=not drop_rule_fires,
        labels=labels,
    ) for y in train_years]
    train_df = pd.concat(train_frames, ignore_index=True)
    val_df = _prepare_year_ablation(
        val_year, rules, use_pecos=use_pecos, apply_filter=apply_filter,
        add_rule_features=not drop_rule_fires,
    )
    test_dfs = {y: _prepare_year_ablation(
        y, rules, use_pecos=use_pecos,
        pecos_mean_impute=pecos_mean_impute,
        apply_filter=apply_filter,
        add_rule_features=not drop_rule_fires,
        labels=labels,
    ) for y in test_years}

    # Apply column drops + archetype remapping
    train_df = _drop_columns(train_df, not use_pecos, drop_taxonomy, drop_rule_fires, drop_pecos_indicator)
    val_df = _drop_columns(val_df, not use_pecos, drop_taxonomy, drop_rule_fires, drop_pecos_indicator)
    for y in test_dfs:
        test_dfs[y] = _drop_columns(test_dfs[y], not use_pecos, drop_taxonomy, drop_rule_fires, drop_pecos_indicator)
    train_df = _remap_archetypes(train_df, drop_archetypes)
    val_df = _remap_archetypes(val_df, drop_archetypes)
    for y in test_dfs:
        test_dfs[y] = _remap_archetypes(test_dfs[y], drop_archetypes)

    # NPI-symmetric holdout
    val_pos = set(val_df.loc[val_df["y_eval"] == 1, "npi_padded"])
    test_pos: set[str] = set()
    for t in test_dfs.values():
        test_pos |= set(t.loc[t["y_eval"] == 1, "npi_padded"])
    holdout = val_pos | test_pos
    train_df = train_df[~train_df["npi_padded"].isin(holdout)].reset_index(drop=True)
    val_df = val_df[~val_df["npi_padded"].isin(test_pos)].reset_index(drop=True)

    # Null-label perturbation (after holdout — applied to all frames)
    if null_label:
        train_df = _shuffle_labels(train_df, seed=42)
        val_df = _shuffle_labels(val_df, seed=43)
        for y in test_dfs:
            test_dfs[y] = _shuffle_labels(test_dfs[y], seed=44 + int(y))

    if verbose:
        print(f"[{label}] After holdout: train={len(train_df):,} "
              f"({int(train_df['y_eval'].sum())} pos)")

    _, feat_cols = _feature_matrix(train_df)
    if verbose:
        print(f"[{label}] feature schema: {len(feat_cols)} features")

    archetypes_to_train = [a for a in ARCHETYPES if a not in drop_archetypes]
    models: dict[str, object] = {}
    for archetype in archetypes_to_train:
        m, _ = _train_one_archetype(archetype, train_df, val_df, feat_cols, verbose=verbose)
        if m is not None:
            models[archetype] = m

    if not models:
        raise RuntimeError(f"[{label}] no archetype models trained")

    eval_frames = {val_year: val_df, **test_dfs}
    all_topk = []
    for y, frame in eval_frames.items():
        X, _ = _feature_matrix(frame)
        X = X.reindex(columns=feat_cols, fill_value=0)
        scored = frame.copy()
        for archetype, model in models.items():
            scored[f"score_{archetype}"] = model.predict_proba(X)[:, 1]
        scored["score_agg"] = _aggregate(scored, aggregation)
        ranked = scored.sort_values("score_agg", ascending=False).reset_index(drop=True)
        n_pos = int(ranked["y_eval"].sum())
        for k in K_VALUES:
            top_k = ranked.head(k)
            tp_k = int(top_k["y_eval"].sum())
            all_topk.append({
                "label": label, "year": y, "k": k,
                "tp": tp_k, "n_positives": n_pos,
                "recall_at_k": tp_k / n_pos if n_pos else 0.0,
                "precision_at_k": tp_k / k,
            })
        if verbose:
            line = (
                f"[{label}] year={y} (n_pos={n_pos}) " + " ".join(
                    f"K={k}: {int(ranked.head(k)['y_eval'].sum())} "
                    f"({int(ranked.head(k)['y_eval'].sum())/max(n_pos,1):.1%})"
                    for k in (100, 1000, 5000, 10000)
                )
            )
            print(line)

    df = pd.DataFrame(all_topk)
    df.to_csv(out_dir / f"topk_{label}.csv", index=False)
    return {"label": label, "rows": all_topk, "n_features": len(feat_cols)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rules-from", type=Path, required=True)
    ap.add_argument("--label", type=str, required=True)
    ap.add_argument("--no-pecos", action="store_true")
    ap.add_argument(
        "--pecos-mean-impute", action="store_true",
        help="Impute missing-from-PECOS NPIs with population means "
        "(removes the missing=excluded leak channel; in_directory still kept).",
    )
    ap.add_argument(
        "--drop-pecos-indicator", action="store_true",
        help="Drop pecos_in_directory column (combined with --pecos-mean-impute "
        "gives a truly leak-free PECOS test).",
    )
    ap.add_argument("--no-taxonomy", action="store_true")
    ap.add_argument("--no-rule-fires", action="store_true")
    ap.add_argument("--no-filter", action="store_true")
    ap.add_argument("--null-label", action="store_true")
    ap.add_argument("--drop-archetypes", type=str, default="")
    ap.add_argument(
        "--aggregation",
        choices=["sum", "max", "rank", "count"], default="sum",
    )
    ap.add_argument(
        "--labels",
        choices=["tight", "pooled_leie"], default="tight",
        help="tight = DOJ \u222a LEIE-billing-fraud-tight (current default). "
        "pooled_leie = DOJ \u222a LEIE-all-fraud-relevant-codes (apples-to-apples vs Bauder/Hancock literature).",
    )
    ap.add_argument(
        "--train-years",
        type=lambda s: tuple(int(x) for x in s.split(",")),
        default=(2014, 2016, 2017),
    )
    ap.add_argument("--val-year", type=int, default=2018)
    ap.add_argument(
        "--test-years",
        type=lambda s: tuple(int(x) for x in s.split(",")),
        default=(2019, 2020),
    )
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT_DIR)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    drop_archetypes = tuple(
        a.strip() for a in args.drop_archetypes.split(",") if a.strip()
    )
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=FutureWarning)
        run(
            rules_path=args.rules_from,
            train_years=args.train_years,
            val_year=args.val_year,
            test_years=args.test_years,
            out_dir=args.out,
            label=args.label,
            use_pecos=not args.no_pecos,
            pecos_mean_impute=args.pecos_mean_impute,
            drop_pecos_indicator=args.drop_pecos_indicator,
            drop_taxonomy=args.no_taxonomy,
            drop_rule_fires=args.no_rule_fires,
            drop_archetypes=drop_archetypes,
            null_label=args.null_label,
            apply_filter=not args.no_filter,
            aggregation=args.aggregation,
            labels=args.labels,
            verbose=not args.quiet,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
