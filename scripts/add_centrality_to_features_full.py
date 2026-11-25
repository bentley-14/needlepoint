"""Add PageRank + clustering centrality to features_full_{year}.parquet.

Final schema target: R2 base (525 cols) + 2 centrality cols = 527.
  - keep  graph_hcpcs_pagerank_full
  - keep  graph_hcpcs_clustering_full
  - DROP  graph_hcpcs_x3_diagonal   (raw A^3 diagonal / un-normalized intermediate)

GNN is left exactly as it is on R2 (verified bit-identical to local gnn_raw,
so no swap needed). This script only manages the centrality columns.

Idempotent: drops any pre-existing graph_hcpcs_* before re-joining, so re-runs
and partially-rebuilt files converge to the same 527-col schema.

Source selection per year:
  - if a `.bak_*` exists (a prior rebuild's pristine-R2 snapshot) → read from it,
    so the 4 partially-rebuilt years (2013-2016) revert to R2's original GNN bytes.
  - else read the current file (2017-2023 are still pristine R2) and snapshot it
    to `.bak_{epoch}` before overwriting.

Atomic write: tmp + rename. A `.bak` always remains as the 525-col pristine R2.

Usage:
    python scripts/add_centrality_to_features_full.py --dry-run
    python scripts/add_centrality_to_features_full.py --years 2016
    python scripts/add_centrality_to_features_full.py            # all years
"""
from __future__ import annotations

import argparse
import gc
import glob
import shutil
import time
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parents[1]
FEATURE_CACHE = REPO / "data" / "cache" / "feature_frames"
CENTRALITY_CACHE = REPO / "data" / "cache" / "graph"

KEEP_CENTRALITY = ["graph_hcpcs_pagerank_full", "graph_hcpcs_clustering_full"]
DROP_CENTRALITY = ["graph_hcpcs_x3_diagonal"]

LABEL_FRAGMENTS = (
    "y_eval", "y_doj", "y_leie", "archetype", "scheme_", "arch_",
    "fraud_case", "fraud_label", "fraud_leie", "label_source",
    "label_confidence", "label_bucket", "case_ids", "case_fraud_types",
    "rrf_rank", "score_rrf", "score_xgb", "score_anomaly", "score_neural",
    "score_specialty",
)


def detect_npi_key(df: pd.DataFrame) -> str:
    if "npi_padded" in df.columns:
        return "npi_padded"
    if "npi" in df.columns:
        return "npi"
    raise RuntimeError(f"no NPI column; cols sample: {list(df.columns)[:10]}")


def normalize_npi(s: pd.Series) -> pd.Series:
    return s.astype(str).str.replace(r"\.0$", "", regex=True).str.zfill(10)


def add_centrality(year: int, *, dry_run: bool = False) -> dict:
    ff_path = FEATURE_CACHE / f"features_full_{year}.parquet"
    ctr_path = CENTRALITY_CACHE / f"hcpcs_centrality_{year}.parquet"
    if not ff_path.exists():
        raise FileNotFoundError(f"missing features_full: {ff_path}")
    if not ctr_path.exists():
        raise FileNotFoundError(f"missing centrality: {ctr_path}")

    existing_baks = sorted(glob.glob(str(ff_path) + ".bak_*"))
    src_path = Path(existing_baks[0]) if existing_baks else ff_path
    have_bak = bool(existing_baks)

    print(f"\n=== {year} ===")
    print(f"  source: {src_path.name}  ({'pre-existing .bak' if have_bak else 'current file'})")
    t0 = time.time()

    ff = pd.read_parquet(src_path)
    n_rows = len(ff)
    n_cols_src = len(ff.columns)
    key = detect_npi_key(ff)
    ff[key] = normalize_npi(ff[key])
    print(f"  read: {n_rows:,} rows × {n_cols_src} cols  (key={key}, {time.time()-t0:.1f}s)")

    # Drop ALL pre-existing centrality cols (idempotent)
    drop_now = [c for c in ff.columns if c.startswith("graph_hcpcs_")]
    if drop_now:
        ff = ff.drop(columns=drop_now)
        print(f"  dropped pre-existing centrality: {drop_now}")

    # Join only the 2 keep-cols
    ctr = pd.read_parquet(ctr_path, columns=["npi", *KEEP_CENTRALITY])
    if "npi" in ctr.columns and key != "npi":
        ctr = ctr.rename(columns={"npi": key})
    ctr[key] = normalize_npi(ctr[key])
    n_dup = ctr.duplicated(subset=[key]).sum()
    if n_dup:
        print(f"  WARN: centrality has {n_dup} dup NPIs; keeping first")
        ctr = ctr.drop_duplicates(subset=[key], keep="first")

    ff = ff.merge(ctr, how="left", on=key, validate="m:1")
    if len(ff) != n_rows:
        raise RuntimeError(f"merge inflated rows: {n_rows} → {len(ff)}")
    n_matched = ff[KEEP_CENTRALITY[0]].notna().sum()
    for c in KEEP_CENTRALITY:
        ff[c] = ff[c].fillna(0.0)
    print(f"  +{len(KEEP_CENTRALITY)} centrality cols ({', '.join(KEEP_CENTRALITY)}); "
          f"{n_matched:,}/{n_rows:,} ({n_matched/n_rows:.1%}) matched; NaN→0")
    del ctr

    # Guards
    assert all(c in ff.columns for c in KEEP_CENTRALITY), "missing keep cols!"
    assert not any(c in ff.columns for c in DROP_CENTRALITY), "x3_diagonal still present!"
    leaks = [c for c in ff.columns if any(frag in c for frag in LABEL_FRAGMENTS)]
    if leaks:
        raise RuntimeError(f"LABEL LEAKAGE: {leaks}")

    n_cols_final = len(ff.columns)
    print(f"  final: {n_rows:,} rows × {n_cols_final} cols")

    if dry_run:
        print("  DRY RUN — not writing")
        del ff
        gc.collect()
        return dict(year=year, rows=n_rows, cols=n_cols_final, matched=n_matched, wrote=False)

    # Atomic write
    epoch = int(time.time())
    tmp_path = ff_path.with_suffix(f".parquet.tmp.{epoch}")
    t_w = time.time()
    ff.to_parquet(tmp_path, index=False)
    print(f"  wrote tmp ({time.time()-t_w:.1f}s, {tmp_path.stat().st_size/1e6:.0f} MB)")

    # Ensure a pristine .bak exists. If reading current file (no pre-existing bak),
    # snapshot it now before overwriting.
    if not have_bak:
        bak_path = ff_path.with_suffix(f".parquet.bak_{epoch}")
        shutil.move(str(ff_path), str(bak_path))
        print(f"  snapshot current → {bak_path.name}")
    elif ff_path.exists():
        # 528-col rebuilt file (e.g. 2013-2016); discard it, .bak is the keeper
        ff_path.unlink()

    shutil.move(str(tmp_path), str(ff_path))
    print(f"  ✓ {ff_path.name} ({time.time()-t0:.1f}s total)")
    del ff
    gc.collect()
    return dict(year=year, rows=n_rows, cols=n_cols_final, matched=n_matched, wrote=True)


def parse_years(spec: str) -> list[int]:
    if "," in spec:
        return [int(y) for y in spec.split(",") if y]
    if "-" in spec:
        lo, hi = spec.split("-")
        return list(range(int(lo), int(hi) + 1))
    return [int(spec)]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--years", default="2013-2023")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    years = parse_years(args.years)
    print(f"=== add_centrality: years={years} keep={KEEP_CENTRALITY} drop={DROP_CENTRALITY} dry_run={args.dry_run} ===")
    summary = []
    t_all = time.time()
    for y in years:
        summary.append(add_centrality(y, dry_run=args.dry_run))
    print("\n=== SUMMARY ===")
    print(pd.DataFrame(summary).to_string(index=False))
    print(f"\nelapsed: {(time.time()-t_all)/60:.1f} min")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
