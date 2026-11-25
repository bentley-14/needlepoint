"""Apply the PCA fit from pca_analyze_gnn.py to per-year raw embeddings.

Reads ``data/cache/gnn_features/pca_fit.json`` (output of
``pca_analyze_gnn.py``) and applies it to each year's raw embedding
parquet, writing ``data/cache/gnn_features/gnn_pca_<year>.parquet``.

If the PCA fit was "pooled", the same components are applied to every
year. If "per-year", each year uses its own components.

Atomic write per year.

Usage:
    python scripts/experiments/export_gnn_pca.py --years 2013-2023
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

CACHE_DIR = Path("data/cache/gnn_features")


def _parse_years(spec: str) -> list[int]:
    if "-" in spec:
        a, b = spec.split("-", 1)
        return list(range(int(a), int(b) + 1))
    return [int(s) for s in spec.split(",")]


def _get_pca_for_year(fit: dict, year: int) -> tuple[np.ndarray, np.ndarray]:
    """Return (components, mean) for the given year per the fit's pooled/per-year choice."""
    if fit["pooled"]:
        return np.asarray(fit["pca_components"]), np.asarray(fit["pca_mean"])
    per = fit["per_year_fits"][str(year)]
    return np.asarray(per["pca_components"]), np.asarray(per["pca_mean"])


def export_pca_year(year: int, fit: dict) -> Path:
    raw_path = CACHE_DIR / f"gnn_raw_{year}.parquet"
    if not raw_path.exists():
        raise FileNotFoundError(raw_path)

    df = pd.read_parquet(raw_path)
    embed_cols = sorted([c for c in df.columns if c.startswith("gnn_embed_")],
                        key=lambda c: int(c.split("_")[-1]))
    emb = df[embed_cols].to_numpy(dtype=np.float64)

    components, mean = _get_pca_for_year(fit, year)
    n_components = components.shape[0]
    projected = (emb - mean) @ components.T  # (n_npis, n_components)

    out = pd.DataFrame(
        projected,
        columns=[f"gnn_pca_{i}" for i in range(n_components)],
    )
    out["npi"] = df["npi"].values
    out["gnn_in_vocab"] = df["gnn_in_vocab"].values
    out["year"] = year
    cols = ["npi"] + [f"gnn_pca_{i}" for i in range(n_components)] + ["gnn_in_vocab", "year"]
    out = out[cols]

    # Zero-fill PCA for out-of-vocab NPIs (they had zero embeddings,
    # which projects to a constant offset from the mean — set explicitly
    # to zero so downstream zero-fill semantics stay clean).
    out_of_vocab_mask = out["gnn_in_vocab"] == 0
    pca_cols = [f"gnn_pca_{i}" for i in range(n_components)]
    out.loc[out_of_vocab_mask, pca_cols] = 0.0

    out_path = CACHE_DIR / f"gnn_pca_{year}.parquet"
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    out.to_parquet(tmp, index=False)
    tmp.replace(out_path)

    print(f"[{year}] wrote {out_path} ({out_path.stat().st_size / 1024:.0f} KB) "
          f"with {n_components} PCs over {len(out):,} NPIs")
    return out_path


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--years", type=str, required=True)
    p.add_argument("--fit", type=Path, default=CACHE_DIR / "pca_fit.json")
    args = p.parse_args()

    if not args.fit.exists():
        raise SystemExit(f"PCA fit not found at {args.fit}. Run pca_analyze_gnn.py first.")
    fit = json.loads(args.fit.read_text())
    print(f"using PCA fit: pooled={fit['pooled']}, n_components={fit['n_components']}")

    years = _parse_years(args.years)
    for y in years:
        export_pca_year(y, fit)


if __name__ == "__main__":
    main()
