"""Fast hcpcs_centrality build via the bipartite trick (Option C).

Computes per-NPI bipartite-projected PageRank and clustering using only
the small NPI x HCPCS incidence matrix I — never materializes the full
NPI x NPI graph (which would be ~1M x 1M = trillions of entries -> OOM).

The trick (from an earlier branch: scripts/experiments/
extend_centrality_cache.py):

  PageRank:    A @ p = I @ (I.T @ p) - codes_per_npi * p
               where A = I @ I.T - self_loops; 50 power iterations.

  Clustering numerator:
               x3[i] = I[i,:] @ M^2 @ I[i,:].T
               where M = I.T @ I (codes x codes, small dense — ~500 x 500).

  Clustering denominator (weighted-degree proxy, Option C):
               d[i] = (I @ code_pop)[i] - codes_per_npi[i]
               Degree proxy in seconds vs the original binary-codepartners
               chunked matmul that took ~25 min/year. PageRank_full is
               identical to the original; clustering_full differs in
               normalization only.

Output: data/cache/graph/hcpcs_centrality_{year}.parquet
Columns:
  - npi (string, 10-digit zero-padded)
  - graph_hcpcs_pagerank_full   (float64)
  - graph_hcpcs_x3_diagonal     (float64)
  - graph_hcpcs_clustering_full (float64)
  - year                        (int64)

Filtering:
  - drop_top_codes_k=15: drop the 15 most-ubiquitous HCPCS codes
    (E&M 99213, 99214, etc. — every PCP bills these; no fraud signal)
  - top_k_codes=500: keep top 500 codes after the drop

Per-year time: ~30-60 seconds.

Usage:
    python scripts/experiments/build_hcpcs_centrality_fast.py --years 2013-2023
    python scripts/experiments/build_hcpcs_centrality_fast.py --year 2023
"""
from __future__ import annotations

import argparse
import time
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.sparse import coo_matrix

PANELS_DIR = Path("data/panels")
CACHE_DIR = Path("data/cache/graph")


def build_incidence(year: int, top_k_codes: int, drop_top_codes_k: int):
    """Return (I_csr, npis_array) where I is sparse NPI x HCPCS incidence."""
    panel_path = PANELS_DIR / f"cms_partb_svc_{year}.parquet"
    if not panel_path.exists():
        raise FileNotFoundError(panel_path)

    df = pd.read_parquet(panel_path, columns=["Rndrng_NPI", "HCPCS_Cd"])
    df["Rndrng_NPI"] = df["Rndrng_NPI"].astype(str).str.zfill(10)
    df = df.dropna()

    if drop_top_codes_k > 0:
        ubiq = df["HCPCS_Cd"].value_counts().nlargest(drop_top_codes_k).index.tolist()
        print(f"    dropping {drop_top_codes_k} ubiquitous codes: {ubiq}")
        df = df[~df["HCPCS_Cd"].isin(ubiq)]

    top_codes = df["HCPCS_Cd"].value_counts().nlargest(top_k_codes).index
    df = df[df["HCPCS_Cd"].isin(top_codes)]
    df = df.drop_duplicates(["Rndrng_NPI", "HCPCS_Cd"])

    npis = df["Rndrng_NPI"].unique()
    codes = df["HCPCS_Cd"].unique()
    n2i = {n: i for i, n in enumerate(npis)}
    c2j = {c: j for j, c in enumerate(codes)}

    rows = df["Rndrng_NPI"].map(n2i).to_numpy()
    cols = df["HCPCS_Cd"].map(c2j).to_numpy()
    data = np.ones(len(df), dtype=np.int32)
    I = coo_matrix((data, (rows, cols)), shape=(len(npis), len(codes))).tocsr()
    return I, npis


def bipartite_pagerank(
    I,
    *,
    alpha: float = 0.85,
    max_iter: int = 50,
    tol: float = 1e-9,
    verbose: bool = True,
):
    """PageRank on A = I @ I.T - diag(self_loops) via the bipartite trick.

    Returns (pagerank_vector, weighted_degree_vector).
    """
    N = I.shape[0]
    codes_per_npi = np.asarray(I.sum(axis=1)).flatten().astype(np.float64)
    code_pop = np.asarray(I.sum(axis=0)).flatten().astype(np.float64)

    d = (I @ code_pop) - codes_per_npi
    safe_d = np.where(d > 0, d, 1.0)

    p = np.ones(N) / N
    for it in range(max_iter):
        x = p / safe_d
        x = np.where(d > 0, x, 0.0)
        Ax = I @ (I.T @ x) - codes_per_npi * x
        p_new = alpha * Ax + (1 - alpha) / N
        s = p_new.sum()
        if s > 0:
            p_new = p_new / s
        delta = np.abs(p_new - p).sum()
        p = p_new
        if verbose and (it < 3 or it % 10 == 0 or it == max_iter - 1):
            print(f"    pagerank iter {it+1}: L1 delta = {delta:.2e}")
        if delta < tol:
            if verbose:
                print(f"    pagerank converged at iter {it+1}")
            break
    return p, d


def bipartite_x3_diagonal(
    I,
    *,
    chunk_size: int = 10_000,
    verbose: bool = True,
) -> np.ndarray:
    """Compute diag(X^3) where X = I @ I.T, via X^3 = I @ M^2 @ I.T."""
    N = I.shape[0]
    if verbose:
        print(f"    clustering: computing M = I.T @ I "
              f"({I.shape[1]} x {I.shape[1]} dense)")
    M = (I.T @ I).toarray().astype(np.float64)

    if verbose:
        print(f"    clustering: computing M^2")
    M2 = M @ M

    x3_diag = np.zeros(N, dtype=np.float64)
    n_chunks = (N + chunk_size - 1) // chunk_size
    if verbose:
        print(f"    clustering: chunked X^3 diagonal over {N:,} NPIs "
              f"in {n_chunks} chunks of {chunk_size}")
    for start in range(0, N, chunk_size):
        end = min(start + chunk_size, N)
        I_chunk = I[start:end].toarray().astype(np.float64)
        IM2_chunk = I_chunk @ M2
        x3_diag[start:end] = (IM2_chunk * I_chunk).sum(axis=1)
    return x3_diag


def build_one_year(
    year: int,
    *,
    top_k_codes: int = 500,
    drop_top_codes_k: int = 15,
) -> Path:
    t0 = time.time()
    print(f"\n[{year}] === starting ===")

    print(f"[{year}] building bipartite incidence I "
          f"(top_k_codes={top_k_codes}, drop_top_codes_k={drop_top_codes_k}) ...")
    I, npis = build_incidence(year, top_k_codes, drop_top_codes_k)
    print(f"[{year}]   I shape={I.shape}, nnz={I.nnz:,}")

    panel_path = PANELS_DIR / f"cms_partb_svc_{year}.parquet"
    panel_npis_raw = pd.read_parquet(panel_path, columns=["Rndrng_NPI"])
    panel_npis = (
        panel_npis_raw["Rndrng_NPI"].astype(str).str.zfill(10).unique()
    )
    print(f"[{year}]   panel has {len(panel_npis):,} unique NPIs total")
    print(f"[{year}]   bipartite-projected coverage: {len(npis):,} "
          f"({100 * len(npis) / len(panel_npis):.1f}%)")

    print(f"[{year}] computing bipartite PageRank...")
    pr_full, weighted_degree = bipartite_pagerank(I)
    print(f"[{year}]   PR sum={pr_full.sum():.4f} (should be ~1.0)")

    print(f"[{year}] computing X^3 diagonal (clustering numerator)...")
    x3 = bipartite_x3_diagonal(I)

    d = weighted_degree
    denom = np.where(d >= 2, d * (d - 1), 1.0)
    clustering = np.where(d >= 2, x3 / denom, 0.0)

    proj_df = pd.DataFrame({
        "npi": npis,
        "graph_hcpcs_pagerank_full": pr_full.astype(np.float64),
        "graph_hcpcs_x3_diagonal": x3.astype(np.float64),
        "graph_hcpcs_clustering_full": clustering.astype(np.float64),
    })
    full_df = pd.DataFrame({"npi": panel_npis})
    out = full_df.merge(proj_df, on="npi", how="left")
    for col in ("graph_hcpcs_pagerank_full", "graph_hcpcs_x3_diagonal",
                "graph_hcpcs_clustering_full"):
        out[col] = out[col].fillna(0.0).astype(np.float64)
    out["year"] = year

    n_nonzero = int((out["graph_hcpcs_pagerank_full"] > 0).sum())
    print(f"[{year}]   non-zero pagerank_full: {n_nonzero:,} "
          f"({100 * n_nonzero / len(out):.1f}%)")

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    out_path = CACHE_DIR / f"hcpcs_centrality_{year}.parquet"
    tmp_path = out_path.with_suffix(out_path.suffix + ".tmp")
    out.to_parquet(tmp_path, index=False)
    tmp_path.replace(out_path)

    elapsed = time.time() - t0
    print(f"[{year}] wrote {out_path} "
          f"({out_path.stat().st_size / 1024:.0f} KB) "
          f"in {elapsed:.1f}s")
    return out_path


def _parse_years(spec: str) -> list[int]:
    if "-" in spec:
        a, b = spec.split("-", 1)
        return list(range(int(a), int(b) + 1))
    return [int(s) for s in spec.split(",")]


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    grp = p.add_mutually_exclusive_group(required=True)
    grp.add_argument("--year", type=int)
    grp.add_argument("--years", type=str,
                     help="Range '2013-2023' or list '2014,2015,2016'")
    p.add_argument("--top-k-codes", type=int, default=500)
    p.add_argument("--drop-top-codes", type=int, default=15)
    args = p.parse_args()

    years = [args.year] if args.year is not None else _parse_years(args.years)

    t_total = time.time()
    for y in years:
        build_one_year(
            y,
            top_k_codes=args.top_k_codes,
            drop_top_codes_k=args.drop_top_codes,
        )
    print(f"\n=== ALL YEARS DONE in {time.time() - t_total:.1f}s "
          f"({len(years)} years) ===")


if __name__ == "__main__":
    main()
