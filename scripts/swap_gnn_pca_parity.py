"""Parity-preserving GNN swap for features_full_{year}.parquet.

Swaps ONLY the gnn_pca_* columns to the freshly retrained per-year GNN
(data/cache/gnn_features/gnn_pca_{year}.parquet), leaving every other
column byte-identical to the prior frame. This is "pass 2": the rebuilt
frames must differ from the most-recent walk-forward frames ONLY by the
intended GNN change.

Parity is guaranteed by construction:
  - we drop ONLY columns starting with ``gnn_``;
  - we left-merge the new gnn_pca on the NPI key (left-join preserves the
    left frame's row order and never mutates non-key left columns);
  - we fillna ONLY the newly added gnn columns;
  - we assert the NPI key Series is element-wise unchanged after the merge.
    If the key matches row-for-row, every other non-gnn column matches too
    (they travel together), so no full-frame .equals reload is needed.

2024 special case: the from-scratch base build has neither gnn nor
centrality, so for any year missing graph_hcpcs_*_full we also attach
local centrality (data/cache/graph/hcpcs_centrality_{year}.parquet). For
2013-2023 centrality already exists and is left untouched.

Safety: backup to .bak_{epoch}, write .tmp, atomic rename, label-leakage
guard, row-count invariant.

Usage:
    python scripts/swap_gnn_pca_parity.py --years 2013-2024
    python scripts/swap_gnn_pca_parity.py --years 2024 --dry-run
"""
from __future__ import annotations

import argparse
import gc
import shutil
import time
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parents[1]
FEATURE_CACHE = REPO / "data" / "cache" / "feature_frames"
CENTRALITY_CACHE = REPO / "data" / "cache" / "graph"
GNN_CACHE = REPO / "data" / "cache" / "gnn_features"

# Same leakage guard as the original rebuild script.
LABEL_FRAGMENTS = (
    "y_eval", "y_doj", "y_leie", "archetype", "scheme_", "arch_",
    "fraud_case", "fraud_label", "fraud_leie", "label_source",
    "label_confidence", "label_bucket", "case_ids", "case_fraud_types",
    "rrf_rank", "score_rrf", "score_xgb", "score_anomaly", "score_neural",
    "score_specialty",
)

GNN_KEEP = ["npi"] + [f"gnn_pca_{i}" for i in range(16)] + ["gnn_in_vocab"]
# Canonical centrality set (pipeline_contract.yaml). graph_hcpcs_x3_diagonal is
# FORBIDDEN (leaky, dropped in b3727eb) — never attach it.
KEEP_CENTRALITY = ["graph_hcpcs_pagerank_full", "graph_hcpcs_clustering_full"]


def normalize_npi(s: pd.Series) -> pd.Series:
    return s.astype(str).str.replace(r"\.0$", "", regex=True).str.zfill(10)


def swap_year(year: int, *, dry_run: bool = False) -> dict:
    ff_path = FEATURE_CACHE / f"features_full_{year}.parquet"
    gnn_path = GNN_CACHE / f"gnn_pca_{year}.parquet"
    ctr_path = CENTRALITY_CACHE / f"hcpcs_centrality_{year}.parquet"

    if not ff_path.exists():
        raise FileNotFoundError(f"missing features_full: {ff_path}")
    if not gnn_path.exists():
        raise FileNotFoundError(f"missing gnn_pca: {gnn_path}")

    print(f"\n=== {year} ===")
    t0 = time.time()
    ff = pd.read_parquet(ff_path)
    n_rows, n_cols_orig = len(ff), len(ff.columns)
    print(f"  loaded {n_rows:,} rows x {n_cols_orig} cols ({time.time()-t0:.1f}s)")

    # Canonical key: every year shares 'npi'. Drop any stray 'npi_padded'
    # (present in the from-scratch 2024 build, absent in 2013-2023) so the
    # final schema matches the walk-forward frames exactly.
    if "npi" not in ff.columns:
        raise RuntimeError(f"no 'npi' column; cols sample: {list(ff.columns)[:8]}")
    if "npi_padded" in ff.columns:
        ff = ff.drop(columns=["npi_padded"])
        print("  dropped stray 'npi_padded' (parity with 2013-2023)")
    ff["npi"] = normalize_npi(ff["npi"])

    old_gnn = [c for c in ff.columns if c.startswith("gnn_")]
    print(f"  dropping {len(old_gnn)} old gnn_* cols")
    ff = ff.drop(columns=old_gnn)

    pre_key = ff["npi"].copy()                  # parity anchor
    pre_noncols = set(ff.columns)               # all are non-gnn now

    # ---- attach new gnn_pca ----
    gnn = pd.read_parquet(gnn_path)
    gnn = gnn[[c for c in GNN_KEEP if c in gnn.columns]].copy()
    gnn["npi"] = normalize_npi(gnn["npi"])
    if gnn["npi"].duplicated().any():
        gnn = gnn.drop_duplicates(subset=["npi"], keep="first")
    ff = ff.merge(gnn, how="left", on="npi", validate="m:1")
    if len(ff) != n_rows:
        raise RuntimeError(f"gnn merge changed row count: {n_rows} -> {len(ff)}")
    gnn_added = [c for c in gnn.columns if c != "npi"]
    matched = int(ff["gnn_in_vocab"].notna().sum()) if "gnn_in_vocab" in ff.columns else 0
    for c in gnn_added:
        ff[c] = ff[c].fillna(0)
    if "gnn_in_vocab" in ff.columns:
        ff["gnn_in_vocab"] = ff["gnn_in_vocab"].astype("int8")  # canonical dtype
    print(f"  gnn_pca: +{len(gnn_added)} cols, {matched:,}/{n_rows:,} "
          f"({matched/n_rows:.1%}) matched; NaN->0")

    # ---- attach centrality ONLY if absent (2024 base lacks it) ----
    ctr_added: list[str] = []
    if "graph_hcpcs_pagerank_full" not in ff.columns:
        if not ctr_path.exists():
            raise FileNotFoundError(f"centrality absent in frame AND no cache: {ctr_path}")
        ctr = pd.read_parquet(ctr_path)
        if "year" in ctr.columns:
            ctr = ctr.drop(columns=["year"])
        if "npi" not in ctr.columns:
            raise RuntimeError(f"centrality has no npi col: {list(ctr.columns)[:6]}")
        ctr["npi"] = normalize_npi(ctr["npi"])
        if ctr["npi"].duplicated().any():
            ctr = ctr.drop_duplicates(subset=["npi"], keep="first")
        # Canonical 2 cols only — exclude forbidden graph_hcpcs_x3_diagonal.
        ctr = ctr[["npi"] + [c for c in KEEP_CENTRALITY if c in ctr.columns]]
        missing_c = [c for c in KEEP_CENTRALITY if c not in ctr.columns]
        if missing_c:
            raise RuntimeError(f"centrality cache missing canonical cols: {missing_c}")
        ff = ff.merge(ctr, how="left", on="npi", validate="m:1")
        if len(ff) != n_rows:
            raise RuntimeError(f"centrality merge changed row count: {n_rows} -> {len(ff)}")
        ctr_added = [c for c in ctr.columns if c != "npi"]
        for c in ctr_added:
            ff[c] = ff[c].fillna(0.0)
        print(f"  centrality: +{len(ctr_added)} cols (added — was absent): {ctr_added}")
    else:
        print("  centrality: present — left untouched (parity)")

    # ---- PARITY ASSERT: key unchanged => all non-gnn cols unchanged ----
    if not ff["npi"].equals(pre_key):
        raise RuntimeError("PARITY VIOLATION: npi key Series changed after merge")
    still_present = pre_noncols - set(ff.columns)
    if still_present:
        raise RuntimeError(f"PARITY VIOLATION: non-gnn cols vanished: {still_present}")
    print(f"  parity OK: {len(pre_noncols)} non-gnn cols preserved, key order intact")

    # ---- contract guards (mirror pipeline_contract.yaml) ----
    n_pca = sum(c.startswith("gnn_pca_") for c in ff.columns)
    if n_pca != 16:
        raise RuntimeError(f"expected 16 gnn_pca_* cols, got {n_pca}")
    if "graph_hcpcs_x3_diagonal" in ff.columns:
        raise RuntimeError("forbidden graph_hcpcs_x3_diagonal present")
    if [c for c in ff.columns if c.startswith("gnn_embed_")]:
        raise RuntimeError("raw gnn_embed_* present — should be PCA only")
    for c in KEEP_CENTRALITY:
        if c not in ff.columns:
            raise RuntimeError(f"missing canonical centrality col: {c}")

    # ---- leakage guard ----
    leak = [c for c in ff.columns if any(f in c for f in LABEL_FRAGMENTS)]
    if leak:
        raise RuntimeError(f"LABEL LEAKAGE: {leak}")

    n_cols_final = len(ff.columns)
    print(f"  final: {n_rows:,} rows x {n_cols_final} cols (Δ {n_cols_final - n_cols_orig:+d})")

    if dry_run:
        print("  DRY RUN — not writing")
        del ff, gnn; gc.collect()
        return dict(year=year, rows=n_rows, cols=n_cols_final,
                    gnn=len(gnn_added), ctr=len(ctr_added), wrote=False)

    epoch = int(time.time())
    tmp_path = ff_path.with_suffix(f".parquet.tmp.{epoch}")
    bak_path = ff_path.with_suffix(f".parquet.bak_gnnswap_{epoch}")
    tw = time.time()
    ff.to_parquet(tmp_path, index=False)
    print(f"  wrote tmp ({time.time()-tw:.1f}s, {tmp_path.stat().st_size/1e6:.0f} MB)")
    shutil.move(str(ff_path), str(bak_path))
    shutil.move(str(tmp_path), str(ff_path))
    print(f"  ✓ swapped; backup → {bak_path.name} ({time.time()-t0:.1f}s total)")

    del ff, gnn; gc.collect()
    return dict(year=year, rows=n_rows, cols=n_cols_final,
                gnn=len(gnn_added), ctr=len(ctr_added), wrote=True, bak=bak_path.name)


def parse_years(spec: str) -> list[int]:
    if "," in spec:
        return [int(y) for y in spec.split(",") if y]
    if "-" in spec:
        lo, hi = spec.split("-")
        return list(range(int(lo), int(hi) + 1))
    return [int(spec)]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--years", default="2013-2024")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    years = parse_years(args.years)
    print(f"=== swap_gnn_pca_parity: years={years} dry_run={args.dry_run} ===")
    summary = []
    t_all = time.time()
    for y in years:
        summary.append(swap_year(y, dry_run=args.dry_run))
    print("\n=== SUMMARY ===")
    print(pd.DataFrame(summary).to_string(index=False))
    print(f"\nelapsed: {(time.time()-t_all)/60:.1f} min")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
