"""Tier 1 combined: per-archetype + Bauder features + cohort anomaly + per-specialty + stacking.

All components run WITHOUT PECOS (honest baseline). Computes:

  1. Per-archetype rankers (sum-pool) — baseline + Bauder features
  2. Cohort anomaly (IsolationForest per specialty×state)
  3. Per-specialty rankers (within-specialty scoring)
  4. RRF fusion of (1) + (2) + (3) with grid-searched weights on val
  5. Top-K eval at each combination and the final stacked ensemble

Reports comparative recall@K for each component + the combined stack
to verify additive lift.

Usage:
    python scripts/experiments/run_tier1_combined.py \
        --rules-from results/rule_candidates_v2_2018/set_cover_rules.json \
        --out results/tier1_combined
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

from fraudlib.eval.archetypes import ARCHETYPES
from fraudlib.ranker.bauder_features import attach_bauder_features
from scripts.experiments.discover_multisource_rules_v2 import (
    SCANNABLE_FEATURES, SPECIALTY_BUCKETS, _add_peer_pcts, _bucket_mask,
)
from scripts.experiments.run_ablation import (
    _feature_matrix, _prepare_year_ablation,
)
from scripts.experiments.train_per_archetype_rankers import (
    K_VALUES, _NON_FEATURE_COLS_EXTENDED, _train_one_archetype,
)

DEFAULT_OUT_DIR = Path("results/tier1_combined")
RRF_K = 60


def _prepare_year_with_bauder(year: int, rules: list[dict]) -> pd.DataFrame:
    """Load + filter + stamp + NO PECOS + Bauder features."""
    df = _prepare_year_ablation(year, rules, use_pecos=False, apply_filter=True)
    df = attach_bauder_features(df, year, npi_col="npi")
    return df


def _train_specialty_model(
    specialty: str, terms: list[str],
    train_df: pd.DataFrame, val_df: pd.DataFrame,
    feat_cols: list[str],
):
    """Train binary XGBoost within a specialty."""
    import xgboost as xgb
    train_mask = _bucket_mask(train_df, terms)
    val_mask = _bucket_mask(val_df, terms)
    train_sub = train_df[train_mask].reset_index(drop=True)
    val_sub = val_df[val_mask].reset_index(drop=True)
    y_train = train_sub["y_eval"].values
    n_pos = int(y_train.sum())
    if n_pos < 5 or len(train_sub) < 100:
        return None
    n_neg = int((y_train == 0).sum())
    scale = (n_neg / max(n_pos, 1)) * 2.0
    X_train, _ = _feature_matrix(train_sub)
    X_train = X_train.reindex(columns=feat_cols, fill_value=0)
    X_val, _ = _feature_matrix(val_sub)
    X_val = X_val.reindex(columns=feat_cols, fill_value=0)
    y_val = val_sub["y_eval"].values
    has_val = len(val_sub) > 0 and y_val.sum() > 0
    clf_kwargs = dict(
        n_estimators=300, max_depth=8, learning_rate=0.1,
        min_child_weight=1, subsample=0.8, colsample_bytree=0.8,
        scale_pos_weight=scale, tree_method="hist",
        enable_categorical=True,
        eval_metric="aucpr", n_jobs=-1, verbosity=0, random_state=0,
    )
    # XGBoost 2.x requires eval_set when early_stopping_rounds is set in
    # the constructor. For specialty buckets with no positives in val
    # (common for narrow specialties in early walk-forward rounds), drop
    # early stopping and train the full n_estimators.
    if has_val:
        clf_kwargs["early_stopping_rounds"] = 20
    clf = xgb.XGBClassifier(**clf_kwargs)
    eval_set = [(X_val, y_val)] if has_val else None
    clf.fit(X_train, y_train, eval_set=eval_set, verbose=False)
    return clf


def _train_cohort_forests(train_df, feat_cols, min_cohort=50, verbose=True):
    """IsolationForest per (provider_type, state).

    Optimizations (2026-05-05):
    - n_jobs=1 per forest (multiprocessing overhead > benefit for small cohorts)
    - n_estimators=50 (reduced from 100 — marginal quality loss, 2x speed)
    - max_samples=256 (default 'auto' but explicit for clarity)
    - Global forest subsampled to 100K (was 200K)
    """
    from sklearn.ensemble import IsolationForest
    train_neg = train_df[train_df["y_eval"] == 0].reset_index(drop=True)
    X, _ = _feature_matrix(train_neg)
    X = X.reindex(columns=feat_cols, fill_value=0).select_dtypes(include="number").fillna(0.0)
    forests = {}
    global_if = IsolationForest(n_estimators=50, max_samples=256, contamination=0.02, random_state=0, n_jobs=1)
    global_if.fit(X.sample(n=min(100_000, len(X)), random_state=0))
    forests[("__global__", "__global__")] = global_if
    for (pt, st), group in train_neg.groupby(["partb_provider_type", "partb_state"]):
        if len(group) >= min_cohort:
            forests[(pt, st)] = IsolationForest(
                n_estimators=50, max_samples=256, contamination=0.02, random_state=0, n_jobs=1
            ).fit(X.iloc[group.index])
    for pt, group in train_neg.groupby("partb_provider_type"):
        key = (pt, "__specialty__")
        if key not in forests and len(group) >= min_cohort:
            forests[key] = IsolationForest(
                n_estimators=50, max_samples=256, contamination=0.02, random_state=0, n_jobs=1
            ).fit(X.iloc[group.index])
    if verbose:
        print(f"  Trained {len(forests)} cohort forests", flush=True)
    return forests


def _score_cohort(df, feat_cols, forests):
    X, _ = _feature_matrix(df)
    X = X.reindex(columns=feat_cols, fill_value=0).select_dtypes(include="number").fillna(0.0).reset_index(drop=True)
    df_r = df.reset_index(drop=True)
    scores = np.full(len(df_r), -np.inf)
    for (pt, st), group in df_r.groupby(["partb_provider_type", "partb_state"]):
        f = forests.get((pt, st)) or forests.get((pt, "__specialty__")) or forests[("__global__", "__global__")]
        scores[group.index.values] = -f.decision_function(X.iloc[group.index.values].values)
    return scores


def _topk_recall(ranked_y, n_pos, ks=K_VALUES):
    return {k: int(ranked_y[:k].sum()) / max(n_pos, 1) for k in ks}


def run(rules_path: Path, out_dir: Path, verbose: bool = True):
    out_dir.mkdir(parents=True, exist_ok=True)
    rules = json.loads(rules_path.read_text())["rules"]
    train_years = (2014, 2016, 2017)
    val_year = 2018
    test_years = (2019, 2020)

    if verbose:
        print("=== Loading frames (no-PECOS + Bauder features) ===")
    train_frames = [_prepare_year_with_bauder(y, rules) for y in train_years]
    train_df = pd.concat(train_frames, ignore_index=True)
    val_df = _prepare_year_with_bauder(val_year, rules)
    test_dfs = {y: _prepare_year_with_bauder(y, rules) for y in test_years}

    # NPI-symmetric holdout
    val_pos = set(val_df.loc[val_df["y_eval"] == 1, "npi_padded"])
    test_pos: set[str] = set()
    for t in test_dfs.values():
        test_pos |= set(t.loc[t["y_eval"] == 1, "npi_padded"])
    holdout = val_pos | test_pos
    train_df = train_df[~train_df["npi_padded"].isin(holdout)].reset_index(drop=True)
    val_df = val_df[~val_df["npi_padded"].isin(test_pos)].reset_index(drop=True)
    if verbose:
        print(f"After holdout: train={len(train_df):,} ({int(train_df['y_eval'].sum())} pos)")
    _, feat_cols = _feature_matrix(train_df)
    if verbose:
        print(f"Features: {len(feat_cols)} (incl Bauder)")
        bauder_feats = [c for c in feat_cols if c.startswith("bauder_")]
        print(f"  Bauder features: {bauder_feats}")

    # ── Component 1: Per-archetype rankers ──
    if verbose:
        print(f"\n=== Component 1: Per-archetype rankers ({len(ARCHETYPES)} archetypes) ===")
    arch_models = {}
    for arch in ARCHETYPES:
        m, _ = _train_one_archetype(arch, train_df, val_df, feat_cols, verbose=verbose)
        if m is not None:
            arch_models[arch] = m

    # ── Component 2: Cohort anomaly forests ──
    if verbose:
        print(f"\n=== Component 2: Cohort anomaly forests ===")
    forests = _train_cohort_forests(train_df, feat_cols, verbose=verbose)

    # ── Component 3: Per-specialty rankers ──
    if verbose:
        print(f"\n=== Component 3: Per-specialty rankers ===")
    spec_models = {}
    for spec, terms in SPECIALTY_BUCKETS.items():
        m = _train_specialty_model(spec, terms, train_df, val_df, feat_cols)
        if m is not None:
            spec_models[spec] = m
            if verbose:
                n_pos = int(train_df[_bucket_mask(train_df, terms)]["y_eval"].sum())
                print(f"  [{spec:>20}] trained (train_pos={n_pos})")
    if verbose:
        print(f"  {len(spec_models)} specialty models trained")

    # ── Score all eval frames ──
    eval_frames = {val_year: val_df, **test_dfs}
    all_results = []

    for y, frame in eval_frames.items():
        if verbose:
            print(f"\n=== Scoring year={y} ===")
        n_pos = int(frame["y_eval"].sum())
        X, _ = _feature_matrix(frame)
        X = X.reindex(columns=feat_cols, fill_value=0)

        # Per-archetype scores
        arch_scores = pd.DataFrame(index=frame.index)
        for arch, model in arch_models.items():
            arch_scores[f"s_{arch}"] = model.predict_proba(X)[:, 1]
        score_arch_sum = arch_scores.sum(axis=1).values

        # Cohort anomaly scores
        score_anomaly = _score_cohort(frame, feat_cols, forests)

        # Per-specialty scores (route each row to its specialty model)
        score_spec = np.full(len(frame), -1.0)
        for spec, model in spec_models.items():
            mask = _bucket_mask(frame, SPECIALTY_BUCKETS[spec])
            if mask.any():
                sub_X = X.iloc[mask.values]
                score_spec[mask.values] = model.predict_proba(sub_X)[:, 1]

        # Ranks (1 = highest)
        rank_arch = (-pd.Series(score_arch_sum)).rank(method="min").values.astype(int)
        rank_anom = (-pd.Series(score_anomaly)).rank(method="min").values.astype(int)
        rank_spec = (-pd.Series(score_spec)).rank(method="min").values.astype(int)

        # RRF combinations
        rrf_arch_anom = 1.0 / (RRF_K + rank_arch) + 1.0 / (RRF_K + rank_anom)
        rrf_all3 = (1.0 / (RRF_K + rank_arch) + 1.0 / (RRF_K + rank_anom)
                    + 1.0 / (RRF_K + rank_spec))

        # Weighted stacking (grid-search on val for Tier 1.5)
        # For now: equal-weight RRF of all 3 (rrf_all3 above)

        y_eval = frame["y_eval"].values
        methods = {
            "arch_sum_pool": score_arch_sum,
            "cohort_anomaly": score_anomaly,
            "per_specialty": score_spec,
            "rrf_arch+anomaly": rrf_arch_anom,
            "rrf_all3": rrf_all3,
        }
        for method_name, scores in methods.items():
            order = np.argsort(-scores)
            ranked_y = y_eval[order]
            recalls = _topk_recall(ranked_y, n_pos)
            for k, r in recalls.items():
                all_results.append({
                    "year": y, "method": method_name, "k": k,
                    "recall_at_k": r, "n_positives": n_pos,
                    "tp": int(ranked_y[:k].sum()),
                })
            if verbose:
                line = f"  {method_name:>20}: " + " ".join(
                    f"K={k}: {recalls[k]:.1%}" for k in (100, 1000, 5000, 10000)
                )
                print(line)

    results_df = pd.DataFrame(all_results)
    results_df.to_csv(out_dir / "tier1_topk_all_methods.csv", index=False)

    # Pivot for headline
    pivot = results_df[results_df["k"].isin((1000, 5000, 10000))].pivot_table(
        index="method", columns=["year", "k"], values="recall_at_k"
    )
    pivot.to_csv(out_dir / "tier1_pivot.csv")
    if verbose:
        print(f"\n## Tier 1 recall@K pivot")
        print(pivot.applymap(lambda x: f"{x:.1%}" if pd.notna(x) else "-").to_string())

    (out_dir / "metrics.json").write_text(json.dumps({
        "n_archetype_models": len(arch_models),
        "n_specialty_models": len(spec_models),
        "n_cohort_forests": len(forests),
        "n_features": len(feat_cols),
        "bauder_features": [c for c in feat_cols if c.startswith("bauder_")],
    }, indent=2))
    if verbose:
        print(f"\nWrote {out_dir}/")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rules-from", type=Path, required=True)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT_DIR)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=FutureWarning)
        run(args.rules_from, args.out, verbose=not args.quiet)
    return 0


if __name__ == "__main__":
    sys.exit(main())
