"""Bipartite NPI×HCPCS graph data loading for GNN training.

Loads the per-year Part B service panels, builds a unified bipartite
incidence matrix (NPIs × HCPCS codes) suitable for GNN message passing.
Mirrors the filtering used by ``direct_billing_features`` in
``fraudlib.ranker.graph`` so the GNN sees the same edges that the
centrality features were validated against.

Key design points:
  - **Bipartite**: two node types — NPIs (Type 1, ~1.17M in panel) and
    HCPCS codes (~500 after dropping top-15 ubiquitous E&M codes).
  - **Edge list as dense (2, num_edges) int64 tensor** — no scipy sparse,
    so GNN message passing can run on MPS without falling back to CPU
    for sparse ops PyTorch hasn't ported yet.
  - **Year-pooled by default**: the self-supervised masked-code pretrain
    has no label dependency, so pooling years gives better code-embedding
    coverage (handles HCPCS code drift like 2020+ telehealth additions).
  - **Optional test-split exclusion**: defense-in-depth against the
    case-level test split from ``v4_gold._split_for_case``. The
    walk-forward harness uses temporal splits not case splits, so this
    is belt-and-suspenders — but we wire it for completeness.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd

# Reuse the canonical panels location used by the centrality builder.
PANELS_DIR = Path("data/panels")


@dataclass
class BipartiteGraph:
    """Bipartite NPI × HCPCS graph.

    Attributes
    ----------
    npi_to_idx
        Map from 10-digit NPI string → row index in the NPI node table.
    code_to_idx
        Map from HCPCS code string → row index in the code node table.
    edge_index
        ``(2, num_edges)`` int64 ndarray. Row 0 = NPI index. Row 1 = code index.
        Used as the canonical edge list for GNN message passing.
    edge_year
        ``(num_edges,)`` int16 ndarray giving the panel year each edge came
        from. Optional — only populated when pooling multiple years.
    num_npis
        Cached len of ``npi_to_idx``.
    num_codes
        Cached len of ``code_to_idx``.
    meta
        Dict capturing the filtering params (top_k_codes, drop_top_codes_k,
        included years, exclusion sets) so the encoder can verify its
        export vocab matches its training vocab.
    """

    npi_to_idx: dict[str, int]
    code_to_idx: dict[str, int]
    edge_index: np.ndarray
    edge_year: np.ndarray | None
    num_npis: int = field(init=False)
    num_codes: int = field(init=False)
    meta: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.num_npis = len(self.npi_to_idx)
        self.num_codes = len(self.code_to_idx)


def _load_panel_year(year: int, panels_dir: Path) -> pd.DataFrame:
    panel = panels_dir / f"cms_partb_svc_{year}.parquet"
    if not panel.exists():
        raise FileNotFoundError(panel)
    df = pd.read_parquet(panel, columns=["Rndrng_NPI", "HCPCS_Cd"])
    df["Rndrng_NPI"] = df["Rndrng_NPI"].astype(str).str.zfill(10)
    df["HCPCS_Cd"] = df["HCPCS_Cd"].astype(str)
    df = df.dropna().drop_duplicates()
    return df


def _filter_codes(
    df: pd.DataFrame,
    *,
    drop_top_codes_k: int,
    top_k_codes: int,
    fixed_code_vocab: Iterable[str] | None = None,
) -> tuple[pd.DataFrame, list[str]]:
    """Drop top-K ubiquitous codes, restrict to top_k_codes by frequency.

    If ``fixed_code_vocab`` is supplied, skip the selection logic and
    restrict the frame to that vocab (used by per-year export to match
    the pretrain code-vocab exactly).
    """
    if fixed_code_vocab is not None:
        keep = set(fixed_code_vocab)
        df = df[df["HCPCS_Cd"].isin(keep)]
        # Preserve the input vocab order so the embedding indices align.
        vocab = [c for c in fixed_code_vocab if c in keep]
        return df, vocab

    counts = df["HCPCS_Cd"].value_counts()
    if drop_top_codes_k > 0:
        ubiq = counts.nlargest(drop_top_codes_k).index.tolist()
        df = df[~df["HCPCS_Cd"].isin(ubiq)]
        counts = df["HCPCS_Cd"].value_counts()

    top_codes = counts.nlargest(top_k_codes).index.tolist()
    df = df[df["HCPCS_Cd"].isin(top_codes)]
    return df, top_codes


def build_bipartite(
    years: Iterable[int],
    *,
    panels_dir: Path = PANELS_DIR,
    drop_top_codes_k: int = 15,
    top_k_codes: int = 500,
    exclude_npis: set[str] | None = None,
    fixed_code_vocab: Iterable[str] | None = None,
    include_year_attr: bool = True,
    verbose: bool = True,
) -> BipartiteGraph:
    """Build a (potentially year-pooled) bipartite NPI×HCPCS graph.

    Parameters
    ----------
    years
        Iterable of years to include (e.g. ``range(2013, 2024)`` for pooled
        pretrain, or ``[2023]`` for per-year export).
    panels_dir
        Directory containing ``cms_partb_svc_<year>.parquet`` files.
    drop_top_codes_k
        Drop the K most-billed HCPCS codes before selecting ``top_k_codes``.
        Mirrors ``direct_billing_features`` so the GNN sees the same edge
        set the centrality features were validated against.
    top_k_codes
        Keep the top K codes by frequency (per pooled year set, not per
        individual year).
    exclude_npis
        Optional set of NPIs (10-digit zero-padded strings) to exclude from
        the graph entirely. Used for defense-in-depth case-split exclusion.
    fixed_code_vocab
        If supplied, skip top-K-codes selection and restrict to this vocab.
        Required for per-year export to ensure the GNN sees the same codes
        it was pretrained on.
    include_year_attr
        Whether to compute ``edge_year`` (one entry per edge). Set False
        for single-year builds to save a few MB.
    verbose
        Print per-year stats.

    Returns
    -------
    BipartiteGraph
        Edges + vocabularies + metadata.
    """
    years = list(years)
    frames: list[pd.DataFrame] = []
    for y in years:
        df = _load_panel_year(y, panels_dir)
        df["__year"] = np.int16(y)
        if exclude_npis:
            df = df[~df["Rndrng_NPI"].isin(exclude_npis)]
        if verbose:
            print(f"  {y}: {len(df):,} (NPI, code) edges before filter "
                  f"({df['Rndrng_NPI'].nunique():,} NPIs, "
                  f"{df['HCPCS_Cd'].nunique():,} codes)")
        frames.append(df)

    pooled = pd.concat(frames, ignore_index=True)
    pooled = pooled.drop_duplicates(["Rndrng_NPI", "HCPCS_Cd", "__year"])

    pooled, code_vocab = _filter_codes(
        pooled,
        drop_top_codes_k=drop_top_codes_k,
        top_k_codes=top_k_codes,
        fixed_code_vocab=fixed_code_vocab,
    )
    if verbose:
        print(f"  pooled after code filter (drop_top={drop_top_codes_k}, "
              f"top_k={top_k_codes}): {len(pooled):,} edges, "
              f"{pooled['Rndrng_NPI'].nunique():,} NPIs, {len(code_vocab):,} codes")

    # Build node-index tables. NPI order = sorted for reproducibility.
    npis_sorted = sorted(pooled["Rndrng_NPI"].unique())
    npi_to_idx = {n: i for i, n in enumerate(npis_sorted)}
    code_to_idx = {c: j for j, c in enumerate(code_vocab)}

    # Map to integer edges
    src = pooled["Rndrng_NPI"].map(npi_to_idx).to_numpy(dtype=np.int64)
    dst = pooled["HCPCS_Cd"].map(code_to_idx).to_numpy(dtype=np.int64)
    edge_index = np.stack([src, dst], axis=0)

    edge_year = None
    if include_year_attr and len(years) > 1:
        edge_year = pooled["__year"].to_numpy(dtype=np.int16)

    meta = {
        "years": years,
        "drop_top_codes_k": drop_top_codes_k,
        "top_k_codes": top_k_codes,
        "num_npis": len(npi_to_idx),
        "num_codes": len(code_to_idx),
        "num_edges": int(edge_index.shape[1]),
        "exclude_npis_count": len(exclude_npis or ()),
        "fixed_code_vocab": fixed_code_vocab is not None,
    }
    if verbose:
        print(f"  graph: {meta['num_npis']:,} NPIs × {meta['num_codes']:,} codes, "
              f"{meta['num_edges']:,} edges")

    return BipartiteGraph(
        npi_to_idx=npi_to_idx,
        code_to_idx=code_to_idx,
        edge_index=edge_index,
        edge_year=edge_year,
        meta=meta,
    )


def load_test_split_npis(gold_path: Path) -> set[str]:
    """Return the set of NPIs whose case_id hashes to the 'test' split.

    Uses :func:`fraudlib.ingest.labels.v4_gold._split_for_case` for the
    deterministic 60/30/10 train/test/val split. The walk-forward harness
    uses TEMPORAL splits, so test-split exclusion is belt-and-suspenders;
    we expose it so pretrain can be hardened if downstream eval ever
    moves to case-level holdout.
    """
    from fraudlib.ingest.labels.v4_gold import _split_for_case

    out: set[str] = set()
    with gold_path.open() as f:
        for line in f:
            r = json.loads(line)
            case_id = r.get("case_id") or ""
            if (r.get("split") or _split_for_case(case_id)) != "test":
                continue
            npi = r.get("npi") or r.get("v4_npi")
            if npi:
                out.add(str(npi).zfill(10))
    return out
