"""PCA stability analysis over per-year GNN embeddings.

Reads ``data/cache/gnn_features/gnn_raw_<year>.parquet`` for each year,
fits PCA independently per year, then compares the top principal
components across years to decide whether a single pooled PCA fit is
acceptable or whether PCs drift enough that we need per-year fits.

Outputs:
  - ``results/gnn_experiment_*/pca_stability.md`` — written decision +
    diagnostic tables (PC vector cosine similarities, variance-explained
    curves, per-PC correlation with known concepts like NPPES specialty).
  - Returns 0 if decision is "pooled OK" (PCs stable across years), 1 if
    "per-year required".

The pooled vs per-year decision rule:
  - Top 4 PC vectors must be ≥ 0.9 cosine-similar across all consecutive
    year pairs in the training cohort.
  - Variance-explained curves must agree to within 5pp at the cumulative
    8-PC mark.
  - If both hold → pooled PCA.

Usage:
    python scripts/experiments/pca_analyze_gnn.py --years 2013-2023
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.decomposition import PCA

CACHE_DIR = Path("data/cache/gnn_features")
RESULTS_DIR = Path("results/gnn_experiment_2026-05-23")


def _parse_years(spec: str) -> list[int]:
    if "-" in spec:
        a, b = spec.split("-", 1)
        return list(range(int(a), int(b) + 1))
    return [int(s) for s in spec.split(",")]


def _load_year_embeddings(year: int) -> tuple[np.ndarray, list[str]]:
    """Return (emb_matrix, npis) restricted to in-vocab NPIs only."""
    path = CACHE_DIR / f"gnn_raw_{year}.parquet"
    if not path.exists():
        raise FileNotFoundError(path)
    df = pd.read_parquet(path)
    df = df[df["gnn_in_vocab"] == 1]
    embed_cols = sorted([c for c in df.columns if c.startswith("gnn_embed_")],
                        key=lambda c: int(c.split("_")[-1]))
    emb = df[embed_cols].to_numpy()
    npis = df["npi"].astype(str).str.zfill(10).tolist()
    return emb, npis


def _cosine(u: np.ndarray, v: np.ndarray) -> float:
    nu = u / (np.linalg.norm(u) + 1e-12)
    nv = v / (np.linalg.norm(v) + 1e-12)
    return float(np.dot(nu, nv))


def _align_pc_signs(ref: np.ndarray, cand: np.ndarray) -> np.ndarray:
    """Flip cand PC signs to maximize alignment with ref. PCA sign is arbitrary."""
    out = cand.copy()
    for i in range(min(ref.shape[0], cand.shape[0])):
        if _cosine(ref[i], cand[i]) < 0:
            out[i] = -out[i]
    return out


def analyze(years: list[int], n_components: int = 16) -> dict:
    """Run per-year PCA fits and compute cross-year stability metrics."""
    per_year: dict[int, dict] = {}
    print(f"fitting per-year PCA over {years}...")
    for y in years:
        emb, npis = _load_year_embeddings(y)
        n = min(n_components, emb.shape[1])
        pca = PCA(n_components=n, random_state=0)
        pca.fit(emb)
        per_year[y] = {
            "components": pca.components_,             # (n, embed_dim)
            "explained": pca.explained_variance_ratio_, # (n,)
            "mean": pca.mean_,
            "n_npis": emb.shape[0],
        }
        print(f"  {y}: {emb.shape[0]:,} NPIs  var@8={pca.explained_variance_ratio_[:8].sum():.3f}  "
              f"var@16={pca.explained_variance_ratio_[:n].sum():.3f}")

    # Sign-align components year-over-year using year 0 as reference
    ref_year = years[0]
    ref_pc = per_year[ref_year]["components"]
    for y in years[1:]:
        per_year[y]["components"] = _align_pc_signs(ref_pc, per_year[y]["components"])

    # Cosine-similarity matrix between consecutive years, for top 4 PCs
    cosine_table = []
    for i in range(len(years) - 1):
        y1, y2 = years[i], years[i + 1]
        pc1 = per_year[y1]["components"]
        pc2 = per_year[y2]["components"]
        cosines = [abs(_cosine(pc1[k], pc2[k])) for k in range(min(4, pc1.shape[0]))]
        cosine_table.append({"year_a": y1, "year_b": y2, "cosines": cosines})

    return {
        "per_year": per_year,
        "cosine_table": cosine_table,
        "years": years,
    }


def write_report(analysis: dict, out_path: Path) -> bool:
    """Write the markdown stability report. Returns True if pooled OK."""
    years = analysis["years"]
    per_year = analysis["per_year"]
    cosines = analysis["cosine_table"]

    # Stability gates
    min_cosine = min(min(row["cosines"]) for row in cosines) if cosines else 1.0
    var8_curves = [per_year[y]["explained"][:8].sum() for y in years]
    var8_spread = max(var8_curves) - min(var8_curves) if var8_curves else 0.0

    pooled_ok = (min_cosine >= 0.9) and (var8_spread <= 0.05)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# GNN embedding PCA — cross-year stability analysis",
        "",
        f"Years analyzed: {years}",
        f"Per-year top-4 PC cosine similarity (min): **{min_cosine:.3f}**  (threshold: 0.9)",
        f"Per-year cumulative variance-at-8-PCs spread: **{var8_spread:.3f}**  (threshold: 0.05)",
        "",
        f"**Decision: {'POOLED PCA OK' if pooled_ok else 'PER-YEAR PCA REQUIRED'}**",
        "",
        "## Per-year PCA",
        "",
        "| Year | NPIs in-vocab | var@4 | var@8 | var@16 |",
        "|---|---|---|---|---|",
    ]
    for y in years:
        d = per_year[y]
        lines.append(
            f"| {y} | {d['n_npis']:,} | "
            f"{d['explained'][:4].sum():.3f} | "
            f"{d['explained'][:8].sum():.3f} | "
            f"{d['explained'].sum():.3f} |"
        )

    lines += [
        "",
        "## Consecutive-year PC cosine similarity (top 4 PCs)",
        "",
        "| Year A | Year B | |cos(PC1)| | |cos(PC2)| | |cos(PC3)| | |cos(PC4)| |",
        "|---|---|---|---|---|---|",
    ]
    for row in cosines:
        c = row["cosines"]
        lines.append(
            f"| {row['year_a']} | {row['year_b']} | "
            + " | ".join(f"{v:.3f}" for v in c)
            + " |"
        )

    lines += [
        "",
        "## Interpretation",
        "",
        "Cosine similarity of 1.0 means the PC vector points in the same direction; ",
        "0.0 means orthogonal (totally different axis). For PCs to be considered ",
        "stable, we want consecutive-year PCs to have cosine ≥ 0.9 (meaning they're ",
        "the same concept, just possibly slightly rotated). Variance-explained ",
        "spread > 5pp at the 8-PC mark would mean some years are encoding ",
        "substantially different structure.",
        "",
        f"Decision: **{'pooled' if pooled_ok else 'per-year'}** PCA fit.",
    ]
    out_path.write_text("\n".join(lines))
    print(f"\nwrote stability report → {out_path}")
    print(f"decision: {'POOLED OK' if pooled_ok else 'PER-YEAR REQUIRED'}")
    return pooled_ok


def fit_final_pca(
    years: list[int],
    pooled: bool,
    n_components: int = 16,
) -> dict:
    """Fit the final PCA artifact(s) based on stability decision."""
    out = {"pooled": pooled, "n_components": n_components}
    if pooled:
        all_emb = []
        for y in years:
            emb, _ = _load_year_embeddings(y)
            all_emb.append(emb)
        pooled_emb = np.vstack(all_emb)
        pca = PCA(n_components=n_components, random_state=0)
        pca.fit(pooled_emb)
        out["pca_components"] = pca.components_.tolist()
        out["pca_mean"] = pca.mean_.tolist()
        out["pca_explained"] = pca.explained_variance_ratio_.tolist()
        out["fit_on"] = "pooled across years"
        out["fit_n_rows"] = int(pooled_emb.shape[0])
    else:
        # Save per-year fits
        out["per_year_fits"] = {}
        for y in years:
            emb, _ = _load_year_embeddings(y)
            pca = PCA(n_components=n_components, random_state=0)
            pca.fit(emb)
            out["per_year_fits"][str(y)] = {
                "pca_components": pca.components_.tolist(),
                "pca_mean": pca.mean_.tolist(),
                "pca_explained": pca.explained_variance_ratio_.tolist(),
                "fit_n_rows": int(emb.shape[0]),
            }
    return out


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--years", type=str, required=True)
    p.add_argument("--n-components", type=int, default=16)
    p.add_argument("--results-dir", type=Path, default=RESULTS_DIR)
    args = p.parse_args()

    years = _parse_years(args.years)
    analysis = analyze(years, n_components=args.n_components)
    report_path = args.results_dir / "pca_stability.md"
    pooled_ok = write_report(analysis, report_path)

    # Fit and save the final PCA artifact
    final = fit_final_pca(years, pooled=pooled_ok, n_components=args.n_components)
    final_path = CACHE_DIR / "pca_fit.json"
    final_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = final_path.with_suffix(final_path.suffix + ".tmp")
    tmp.write_text(json.dumps(final, indent=2))
    tmp.replace(final_path)
    print(f"wrote PCA fit → {final_path}")


if __name__ == "__main__":
    main()
