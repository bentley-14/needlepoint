"""Phase 2 ranker LIBRARY (historical entry point; see scripts/REGISTRY.yaml).

STATUS 2026-06-09: kept as a LIBRARY — walk_forward_v4's import chain uses
helpers from here (via train_per_archetype_rankers / run_ablation). The pipeline
described below is the HISTORICAL phase-2 standalone flow; two claims are stale:
LEIE was dropped as a label source 2026-05-05 (DOJ-only labels now), and
rules_v3 has 59 rules (rule__000-058), not 27. In v4 the panel filter is the
AGENT filter (floor_0.50), not the rule filter.

Historical pipeline (as originally run):
  1. Load multi-source frames from data/cache/feature_frames/ cache
  2. Stamp positives (then: DOJ high∪medium ∪ LEIE billing-fraud; LEIE since dropped)
  3. Apply set-cover rule filter — drop rows where no rule fires
  4. Compute peer percentiles within (partb_provider_type, partb_state)
  5. Apply each rule's predicate to get binary fire columns (59 in rules_v3)
  6. NPI-symmetric holdout: drop NPIs in test-year positive set from
     train+val (matches Phase 1.5 protocol). Hash-mod-5 NPI split for
     val held out from train.
  7. Train XGBoost binary classifier (depth=8, lr=0.1, n=200,
     min_child=1, scale_pos_weight × 2)
  8. Score val + test → top-K recall metrics

Outputs:
  metrics.json         — per-year top-K recall + precision
  top500.csv           — top-500 of test-year ranking
  shap_summary.csv     — feature importance via SHAP (top 30)
  config.json          — hyperparams + run config

Usage:
    python scripts/experiments/train_filtered_ranker.py \
        --rules-from results/rule_candidates_v2_2018/set_cover_rules.json \
        --train-years 2014,2016,2017 \
        --val-year 2018 \
        --test-years 2019,2020 \
        --out results/filtered_ranker_v1
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

from fraudlib.eval.evalkit import npi_symmetric_holdout
from fraudlib.labels.ground_truth import load_positives_scoped

from scripts.experiments.discover_multisource_rules_v2 import (
    GENERAL_BUCKET,
    SCANNABLE_FEATURES,
    SPECIALTY_BUCKETS,
    _add_peer_pcts,
    _bucket_mask,
)
from scripts.experiments.measure_top_k_v2 import _rule_mask

CACHE_DIRS = [
    Path("data/cache/feature_frames"),
]
DEFAULT_OUT_DIR = Path("results/filtered_ranker_v1")
K_VALUES = (100, 250, 500, 1000, 2500, 5000, 10000)


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


def _stamp_positives_year(df: pd.DataFrame, year: int) -> pd.DataFrame:
    """Stamp ``y_eval`` for year-specific verified positives (DOJ-only)."""
    doj = set(load_positives_scoped(year, min_confidence="any").keys())
    doj = {str(n).zfill(10) for n in doj}
    npi_str = df["npi"].astype(str).str.zfill(10)
    df = df.copy()
    df["y_eval"] = npi_str.isin(doj).astype(int)
    df["npi_padded"] = npi_str
    return df


def _apply_rules_get_fires(df: pd.DataFrame, rules: list[dict]) -> tuple[pd.DataFrame, pd.Series]:
    """Apply each rule's predicate, return (rule_fire_df, any_rule_fires_mask).

    rule_fire_df has columns named ``rule__{i}`` (one per rule, boolean).
    """
    fire_cols = {}
    any_fires = pd.Series(False, index=df.index)
    for i, rule in enumerate(rules):
        m = _rule_mask(df, rule)
        fire_cols[f"rule__{i:03d}"] = m.astype(int)
        any_fires = any_fires | m
    return pd.DataFrame(fire_cols, index=df.index), any_fires


def _prepare_year(
    year: int, rules: list[dict]
) -> pd.DataFrame:
    """Load + filter + percentile + rule-fire one year. Returns row-restricted frame."""
    df = _load_year(year)
    df = _stamp_positives_year(df, year)
    df = _add_peer_pcts(df, SCANNABLE_FEATURES)
    rule_df, any_fires = _apply_rules_get_fires(df, rules)
    df = pd.concat([df, rule_df], axis=1)
    df = df[any_fires].reset_index(drop=True)
    df["puf_year"] = year
    return df


_NON_FEATURE_COLS = {
    "npi", "npi_padded", "y_eval", "y_doj", "y_leie_only", "puf_year",
    "fraud_case", "y", "label_bucket", "case_ids", "case_fraud_types",
    "fraud_label", "fraud_leie", "label_source", "label_confidence",
    "case_tier", "archetype", "rrf_rank", "score_rrf",
    "score_xgb", "score_anomaly", "score_neural", "score_specialty",
    # Multi-label scheme columns (2026-05-05) — these are LABELS, not features
    "scheme_PHANTOM_BILLING", "scheme_MED_UNNECESSARY", "scheme_KICKBACK",
    "scheme_OPIOID", "scheme_HHA_HOSPICE", "scheme_DME", "scheme_UPCODING",
    "scheme_NPI_THEFT", "scheme_TELEHEALTH", "scheme_LAB_FRAUD",
    # V1 archetype columns (6-bucket variant) — also LABELS
    "arch_OPIOID_MILL", "arch_BILLING_FRAUD", "arch_KICKBACK",
    "arch_UPCODING", "arch_MEDICALLY_UNNECESSARY", "arch_OTHER_FRAUD",
}


def _feature_matrix(df: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    """Build feature matrix from a prepared frame."""
    numeric_cols = [
        c for c in df.select_dtypes(include="number").columns
        if c not in _NON_FEATURE_COLS
    ]
    cat_cols = [
        c for c in df.select_dtypes(include="category").columns
        if c not in _NON_FEATURE_COLS
    ]
    feature_cols = numeric_cols + cat_cols
    X = df[feature_cols].copy()
    X[numeric_cols] = X[numeric_cols].fillna(0.0)
    return X, feature_cols


def run(
    rules_path: Path,
    train_years: tuple[int, ...],
    val_year: int,
    test_years: tuple[int, ...],
    out_dir: Path,
    verbose: bool = True,
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    rules = json.loads(rules_path.read_text())["rules"]
    if verbose:
        print(f"Loaded {len(rules)} set-cover rules")

    # Load + filter all years
    if verbose:
        print(f"\nPreparing train years {train_years}...")
    train_frames = []
    for y in train_years:
        if verbose:
            print(f"  {y}...")
        train_frames.append(_prepare_year(y, rules))
    train_df = pd.concat(train_frames, ignore_index=True)
    if verbose:
        print(f"  train rows: {len(train_df):,}, positives: {int(train_df['y_eval'].sum())}")

    if verbose:
        print(f"\nPreparing val year {val_year}...")
    val_df = _prepare_year(val_year, rules)
    if verbose:
        print(f"  val rows: {len(val_df):,}, positives: {int(val_df['y_eval'].sum())}")

    if verbose:
        print(f"\nPreparing test years {test_years}...")
    test_dfs = {}
    for y in test_years:
        test_dfs[y] = _prepare_year(y, rules)
        if verbose:
            print(f"  {y}: {len(test_dfs[y]):,} rows, "
                  f"positives: {int(test_dfs[y]['y_eval'].sum())}")

    # NPI-symmetric holdout: drop val-year positive NPIs and test-year positive
    # NPIs from train. Important: also do val/train NPI split via hash.
    if verbose:
        print(f"\nNPI-symmetric holdout...")
    val_npis = set(val_df.loc[val_df["y_eval"] == 1, "npi_padded"])
    test_npis: set[str] = set()
    for y, t in test_dfs.items():
        test_npis |= set(t.loc[t["y_eval"] == 1, "npi_padded"])
    holdout_npis = val_npis | test_npis
    before = len(train_df)
    train_df = train_df[~train_df["npi_padded"].isin(holdout_npis)].reset_index(drop=True)
    if verbose:
        print(f"  dropped {before - len(train_df):,} train rows (val/test NPI holdout)")
    # Also drop test NPIs from val (val should not contain test NPIs)
    before_v = len(val_df)
    val_df = val_df[~val_df["npi_padded"].isin(test_npis)].reset_index(drop=True)
    if verbose:
        print(f"  dropped {before_v - len(val_df):,} val rows (test NPI holdout)")

    # Build feature matrices
    if verbose:
        print(f"\nBuilding feature matrices...")
    X_train, feat_cols = _feature_matrix(train_df)
    y_train = train_df["y_eval"].values
    X_val, _ = _feature_matrix(val_df)
    X_val = X_val.reindex(columns=feat_cols, fill_value=0)
    if verbose:
        print(f"  X_train: {X_train.shape}, positives: {int(y_train.sum())}")
        print(f"  X_val:   {X_val.shape}, positives: {int(val_df['y_eval'].sum())}")

    # Scale-pos-weight
    n_pos = int(y_train.sum())
    n_neg = int((y_train == 0).sum())
    scale = (n_neg / max(n_pos, 1)) * 2.0  # ×2 multiplier per Phase 2 plan

    if verbose:
        print(f"\nTraining XGBoost (scale_pos_weight={scale:.1f})...")
    import xgboost as xgb
    clf = xgb.XGBClassifier(
        n_estimators=300,
        max_depth=8,
        learning_rate=0.1,
        min_child_weight=1,
        subsample=0.8,
        colsample_bytree=0.8,
        scale_pos_weight=scale,
        tree_method="hist",
        enable_categorical=True,
        early_stopping_rounds=20,
        eval_metric="aucpr",
        n_jobs=-1,
        verbosity=1,
        random_state=0,
    )
    clf.fit(
        X_train, y_train,
        eval_set=[(X_val, val_df["y_eval"].values)],
        verbose=False,
    )

    # Eval on val + test
    metrics: dict = {"val_year": val_year, "test_years": list(test_years)}
    all_top_k_rows = []
    eval_frames = {val_year: val_df, **test_dfs}
    for y, frame in eval_frames.items():
        X, _ = _feature_matrix(frame)
        X = X.reindex(columns=feat_cols, fill_value=0)
        scores = clf.predict_proba(X)[:, 1]
        scored = frame.copy()
        scored["_score"] = scores
        scored = scored.sort_values("_score", ascending=False).reset_index(drop=True)
        scored["_rank"] = np.arange(1, len(scored) + 1)
        n_pos = int(scored["y_eval"].sum())
        for k in K_VALUES:
            top_k = scored.head(k)
            tp_k = int(top_k["y_eval"].sum())
            recall_k = tp_k / n_pos if n_pos else 0.0
            all_top_k_rows.append({
                "year": y,
                "k": k,
                "n_positives": n_pos,
                "tp": tp_k,
                "recall_at_k": recall_k,
                "precision_at_k": tp_k / k,
            })
            if verbose:
                print(f"  year={y} K={k:>5}: TP={tp_k:>3} "
                      f"recall={recall_k:.1%} prec={tp_k/k:.3%}")
        if y in test_years:
            scored.head(500).to_csv(out_dir / f"top500_{y}.csv", index=False)

    pd.DataFrame(all_top_k_rows).to_csv(out_dir / "top_k_metrics.csv", index=False)

    # SHAP summary
    if verbose:
        print(f"\nSHAP summary...")
    booster = clf.get_booster()
    pos_mask = (val_df["y_eval"].values == 1)
    if pos_mask.sum() > 0:
        X_pos = X_val.iloc[pos_mask]
        dmat = xgb.DMatrix(X_pos, enable_categorical=True)
        shap_vals = booster.predict(dmat, pred_contribs=True)[:, :-1]
        importance = pd.Series(np.abs(shap_vals).mean(axis=0), index=feat_cols)
        importance = importance.sort_values(ascending=False).head(30)
        importance.to_csv(out_dir / "shap_summary.csv")
        if verbose:
            print(importance.head(10).to_string())

    # Headline pivot
    metrics["top_k_pivot"] = (
        pd.DataFrame(all_top_k_rows)
        .pivot(index="year", columns="k", values="recall_at_k")
        .to_dict()
    )
    metrics["best_iteration"] = clf.best_iteration
    metrics["scale_pos_weight"] = scale
    metrics["n_features"] = len(feat_cols)
    (out_dir / "metrics.json").write_text(json.dumps(metrics, indent=2, default=str))

    if verbose:
        pivot = pd.DataFrame(all_top_k_rows).pivot(
            index="year", columns="k", values="recall_at_k"
        )
        pivot.columns = [f"recall@{k}" for k in pivot.columns]
        print(f"\n## Recall@K by year")
        print(pivot.applymap(lambda x: f"{x:.1%}").to_string())
        print(f"\nWrote {out_dir}/")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rules-from", type=Path, required=True)
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
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=FutureWarning)
        run(
            rules_path=args.rules_from,
            train_years=args.train_years,
            val_year=args.val_year,
            test_years=args.test_years,
            out_dir=args.out,
            verbose=not args.quiet,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
