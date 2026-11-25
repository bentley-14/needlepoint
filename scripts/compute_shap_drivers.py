#!/usr/bin/env python3
"""Per-NPI feature attribution ("SHAP drivers") for the ranker's XGBoost heads.

Surfaces, for every scored NPI, the signed feature contributions that drove
its XGBoost score — the "why was this provider flagged?" explanation
used by triage.

Method (honest scope):
  • XGBoost native `pred_contribs=True` (TreeSHAP via the booster), NOT
    shap.TreeExplainer — the latter misreads this model's encoding and yields
    values that don't reconstruct to the predicted probability
    (fraudlib/ranker/pipeline.py documents this).
  • REFIT-DERIVED (the fast path chosen 2026-06-13): the per-archetype XGB
    heads are refit here from the prior-year scored panels using
    walk_forward_v4's exact config (n_estimators=300, max_depth=8, lr=0.1,
    subsample/colsample 0.8, scale_pos_weight=2·neg/pos, hist, aucpr,
    early-stop 50, seed 0; CAT_COLS dropped → all-numeric matrix). This is
    NOT the byte-identical booster that produced the live score_rrf — small
    drift is possible; attributions are labeled `refit-derived` and explain
    the XGBoOST COMPONENT only (blend weight ~3/4; the neural head, weight
    ~1/4, is not covered by TreeSHAP).
  • Per NPI we attribute against the DOMINANT head — the archetype head whose
    predicted probability is highest for that NPI — and emit the top-N signed
    drivers + a plain-English summary.

Usage:
    python scripts/compute_shap_drivers.py --score-year 2023
    python scripts/compute_shap_drivers.py --score-year 2023 --top-n 8 --limit 2000
Output: data/cache/shap_drivers/shap_drivers_{year}.parquet
        columns: npi, dominant_archetype, head_prob, drivers (json), flag_summary
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

# Canonical config — mirror walk_forward_v4 exactly (verified 2026-06-13).
ARCHETYPES = ("PHANTOM_BILLING", "MED_UNNECESSARY", "KICKBACK", "OPIOID", "UPCODING")
LABEL_PREFIX = "scheme_"
CAT_COLS = {"nppes_credential_bucket", "nppes_taxonomy_bucket"}
XGB_PARAMS = dict(
    n_estimators=300, max_depth=8, learning_rate=0.1, min_child_weight=1,
    subsample=0.8, colsample_bytree=0.8, tree_method="hist",
    enable_categorical=True, eval_metric="aucpr", n_jobs=-1, verbosity=0,
    random_state=0,
)
EARLY_STOPPING_ROUNDS = 50

# Plain-English driver context (reused from fraudlib/ranker/pipeline.py when
# importable; falls back to a humanized feature name otherwise).
try:
    from fraudlib.ranker.pipeline import _fraud_context  # type: ignore
except Exception:  # pragma: no cover
    def _fraud_context(feature: str, shap_val: float) -> str:
        return ""


def _panel_for_year(year: int, results_glob: str) -> Path | None:
    cands = sorted(REPO_ROOT.glob(f"{results_glob}/scored_full_{year}.parquet"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    return cands[0] if cands else None


def _feature_cols(df: pd.DataFrame) -> list[str]:
    """All-numeric feature columns: numeric dtype, minus labels / ids / outputs
    / CAT_COLS. Explicit guards exclude score_*/y_eval/scheme_* so a SCORED
    panel's extra output columns can't leak into the refit."""
    try:
        from scripts.experiments.run_ablation import _NON_FEATURE_COLS_EXTENDED as NF
    except Exception:
        NF = set()
    out = []
    for c in df.select_dtypes(include="number").columns:
        if c in NF or c in CAT_COLS:
            continue
        if c.startswith(("score_", "scheme_", "arch_")) or c in (
                "y_eval", "rrf_rank", "puf_year", "fraud_case", "fraud_label",
                "fraud_leie", "label_confidence"):
            continue
        out.append(c)
    return out


def _load_panel(path: Path, feat_cols: list[str] | None = None):
    df = pd.read_parquet(path)
    if "npi" not in df.columns and "npi_padded" in df.columns:
        df["npi"] = df["npi_padded"]
    df["npi"] = df["npi"].astype(str).str.zfill(10)
    if feat_cols is None:
        feat_cols = _feature_cols(df)
    X = df.reindex(columns=feat_cols, fill_value=0.0).astype(np.float32)
    return df, X, feat_cols


def _label_for(df: pd.DataFrame, arch: str) -> np.ndarray:
    col = f"{LABEL_PREFIX}{arch}"
    if col in df.columns:
        return (df[col].fillna(0) > 0).astype(int).values
    # fallback: archetype string match
    return (df.get("archetype", pd.Series([""] * len(df))) == arch).astype(int).values


def fit_heads(train_paths: list[Path], val_path: Path | None):
    import xgboost as xgb
    frames = [pd.read_parquet(p) for p in train_paths]
    train_df = pd.concat(frames, ignore_index=True)
    feat_cols = _feature_cols(train_df)
    Xtr = train_df.reindex(columns=feat_cols, fill_value=0.0).astype(np.float32)
    Xvl = yvl_df = None
    if val_path is not None:
        yvl_df = pd.read_parquet(val_path)
        Xvl = yvl_df.reindex(columns=feat_cols, fill_value=0.0).astype(np.float32)

    heads = {}
    for arch in ARCHETYPES:
        ytr = _label_for(train_df, arch)
        n_pos = int(ytr.sum())
        if n_pos < 5:
            print(f"  {arch:<18} skipped ({n_pos} pos)", file=sys.stderr)
            continue
        scale = ((len(ytr) - n_pos) / max(n_pos, 1)) * 2.0
        params = dict(XGB_PARAMS, scale_pos_weight=scale)
        clf = xgb.XGBClassifier(**params,
                                early_stopping_rounds=EARLY_STOPPING_ROUNDS)
        if Xvl is not None:
            clf.fit(Xtr, ytr, eval_set=[(Xvl, _label_for(yvl_df, arch))],
                    verbose=False)
        else:
            clf2 = xgb.XGBClassifier(**params)
            clf2.fit(Xtr, ytr, verbose=False)
            clf = clf2
        heads[arch] = clf
        print(f"  {arch:<18} fit (n_pos={n_pos})", file=sys.stderr)
    return heads, feat_cols


def compute_drivers(score_year: int, *, train_years: list[int] | None = None,
                    top_n: int = 8, limit: int | None = None,
                    results_glob: str = "results/walk_forward_v4_top*") -> pd.DataFrame:
    import xgboost as xgb
    score_path = _panel_for_year(score_year, results_glob)
    if score_path is None:
        raise FileNotFoundError(f"no scored panel for {score_year}")
    if train_years is None:
        train_years = list(range(2013, score_year - 1))
    train_paths = [p for y in train_years if (p := _panel_for_year(y, results_glob))]
    if not train_paths:
        raise FileNotFoundError(f"no prior-year panels for train_years={train_years}")
    val_path = _panel_for_year(score_year - 1, results_glob)
    print(f"refit heads: train={[p.parent.name.split('_y')[-1][:4] for p in train_paths]} "
          f"val={score_year-1} → attribute {score_year}", file=sys.stderr)

    heads, feat_cols = fit_heads(train_paths, val_path)
    if not heads:
        raise RuntimeError("no archetype heads trained (insufficient positives)")

    sdf, Xs, _ = _load_panel(score_path, feat_cols)
    # Sort by the model's own ranking so --limit selects the TOP-ranked NPIs
    # (the ones the GUI flags + the ones with meaningful positive drivers),
    # not an arbitrary NPI-ordered slice.
    rank_col = "rrf_rank" if "rrf_rank" in sdf.columns else (
        "score_rrf" if "score_rrf" in sdf.columns else None)
    if rank_col:
        asc = (rank_col == "rrf_rank")  # rank: lower=better; score: higher=better
        order = sdf[rank_col].sort_values(ascending=asc).index
        sdf = sdf.loc[order].reset_index(drop=True)
        Xs = Xs.loc[order].reset_index(drop=True)
    if limit:
        sdf = sdf.head(limit).copy()
        Xs = Xs.iloc[:limit]

    # Per-head prob + contribs over the score panel
    probs, contribs = {}, {}
    dmat = xgb.DMatrix(Xs, enable_categorical=True)
    for arch, clf in heads.items():
        probs[arch] = clf.predict_proba(Xs)[:, 1]
        c = clf.get_booster().predict(dmat, pred_contribs=True)
        contribs[arch] = c[:, :-1]  # drop bias column

    # Dominant head = the head where this NPI ranks BEST (highest within-head
    # percentile), NOT cross-head argmax of raw prob — the 5 heads aren't
    # calibrated to each other (scale_pos_weight inflates each head's bias
    # differently), so raw-argmax picks the same head for everyone. RRF fuses
    # per-head RANKS, so the head an NPI ranks highest in is what actually
    # drove its fused score — and attributing there yields net-POSITIVE
    # drivers ("what flagged it"), which a wrong head does not.
    head_names = list(heads)
    prob_mat = np.column_stack([probs[a] for a in head_names])
    # within-head percentile rank (1.0 = top of that head's distribution)
    pct_mat = np.column_stack([
        pd.Series(probs[a]).rank(pct=True).values for a in head_names])
    dominant_idx = pct_mat.argmax(axis=1)

    rows = []
    feat_arr = np.array(feat_cols)
    for i, npi in enumerate(sdf["npi"].values):
        arch = head_names[dominant_idx[i]]
        sv = contribs[arch][i]
        # "Why flagged" = top positive contributions first; then the strongest
        # mitigating (negative) ones, up to top_n total.
        pos_idx = [j for j in np.argsort(sv)[::-1] if sv[j] > 0][:top_n]
        neg_idx = [j for j in np.argsort(sv) if sv[j] < 0][:max(0, top_n - len(pos_idx))]
        order = pos_idx + neg_idx
        drivers = []
        for j in order:
            f = str(feat_arr[j])
            val = float(Xs.iloc[i, j])
            shp = float(sv[j])
            drivers.append({"feature": f, "value": round(val, 4),
                            "shap": round(shp, 5),
                            "direction": "up" if shp >= 0 else "down",
                            "context": _fraud_context(f, shp)})
        pos = [d["feature"] for d in drivers if d["direction"] == "up"][:3]
        summary = (f"XGBoost[{arch}] flagged this NPI mainly on "
                   + ", ".join(pos) if pos else f"XGBoost[{arch}] (no positive drivers)")
        rows.append({"npi": npi, "dominant_archetype": arch,
                     "head_prob": round(float(prob_mat[i, dominant_idx[i]]), 5),
                     "drivers": json.dumps(drivers), "flag_summary": summary})
    return pd.DataFrame(rows)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--score-year", type=int, required=True)
    ap.add_argument("--top-n", type=int, default=8)
    ap.add_argument("--limit", type=int, default=None,
                    help="cap to the top-N ranked NPIs (the GUI subset); omit for all scored")
    ap.add_argument("--results-glob", default="results/walk_forward_v4_top*")
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)

    df = compute_drivers(args.score_year, top_n=args.top_n, limit=args.limit,
                         results_glob=args.results_glob)
    out = args.out or (REPO_ROOT / "data" / "cache" / "shap_drivers"
                       / f"shap_drivers_{args.score_year}.parquet")
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(out)
    print(f"wrote {len(df)} NPI driver-rows → {out}", file=sys.stderr)
    # stdout sample
    print(df.head(3).to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
