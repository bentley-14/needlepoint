"""Export per-year GNN embeddings from a trained encoder.

Given a pretrained encoder (output of ``pretrain_gnn.py``), runs each
year's bipartite incidence through the frozen encoder and writes a
parquet with one row per NPI in the panel.

The encoder is inductive (no per-NPI embedding table), so this is
just a forward pass — each NPI's embedding is derived from their
specific year's billing codes via message passing.

Schema of output ``data/cache/gnn_features/gnn_raw_<year>.parquet``:
    npi             (str, 10-digit)
    gnn_embed_0..N  (float64, N = embed_dim)
    gnn_in_vocab    (int8, 1 if NPI billed any in-vocab code, else 0)
    year            (int)

NPIs in the panel who only bill out-of-vocab codes get a zero embedding
and ``gnn_in_vocab=0`` so the downstream attach can fill cleanly.

Atomic write per year so a crash never destroys earlier years' exports.

Usage:
    # Single year
    python scripts/experiments/export_gnn_embeddings.py \\
        --encoder data/cache/gnn_features/encoder_pooled_L2_D64.pt \\
        --year 2023

    # All years (typical production run after pretrain)
    python scripts/experiments/export_gnn_embeddings.py \\
        --encoder data/cache/gnn_features/encoder_pooled_L2_D64.pt \\
        --years 2013-2023
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import torch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fraudlib.ranker.gnn import BipartiteSAGE, GNNConfig  # noqa: E402
from fraudlib.ranker.gnn_data import build_bipartite  # noqa: E402

PANELS_DIR = Path("data/panels")
CACHE_DIR = Path("data/cache/gnn_features")


def _parse_years(spec: str) -> list[int]:
    if "-" in spec:
        a, b = spec.split("-", 1)
        return list(range(int(a), int(b) + 1))
    return [int(s) for s in spec.split(",")]


def _load_panel_all_npis(year: int) -> list[str]:
    """Return every NPI present in the year's source panel (10-digit padded)."""
    panel = PANELS_DIR / f"cms_partb_svc_{year}.parquet"
    df = pd.read_parquet(panel, columns=["Rndrng_NPI"])
    return df["Rndrng_NPI"].astype(str).str.zfill(10).unique().tolist()


def export_year(
    year: int,
    encoder_path: Path,
    *,
    device_str: str = "mps",
    verbose: bool = True,
) -> Path:
    """Export embeddings for one year."""
    t0 = time.time()

    # Load encoder
    ckpt = torch.load(encoder_path, map_location="cpu", weights_only=False)
    cfg = GNNConfig(**ckpt["config"])
    code_vocab = list(ckpt["code_to_idx"].keys())
    if verbose:
        print(f"[{year}] encoder: num_codes={cfg.num_codes}, embed_dim={cfg.embed_dim}, "
              f"num_layers={cfg.num_layers}")

    # Build bipartite restricted to the encoder's code vocab
    bg = build_bipartite(
        years=[year],
        panels_dir=PANELS_DIR,
        fixed_code_vocab=code_vocab,
        verbose=False,
    )
    if verbose:
        print(f"[{year}] graph: {bg.num_npis:,} NPIs × {bg.num_codes:,} codes, "
              f"{bg.edge_index.shape[1]:,} edges")

    # Init model with encoder weights
    model = BipartiteSAGE(cfg)
    model.load_state_dict(ckpt["state_dict"])
    device = torch.device(device_str)
    model = model.to(device).eval()

    # Forward pass — single full-graph pass since graph is small enough on its own year
    edge_npi = torch.from_numpy(bg.edge_index[0]).long().to(device)
    edge_code = torch.from_numpy(bg.edge_index[1]).long().to(device)
    with torch.no_grad():
        h_npi, _ = model.encode(edge_npi, edge_code, num_npis=bg.num_npis)
    emb = h_npi.cpu().numpy()
    if verbose:
        print(f"[{year}] computed embeddings: {emb.shape}  in {time.time()-t0:.1f}s")

    # Build a DataFrame keyed by NPI for the in-vocab subset
    in_vocab_npis = sorted(bg.npi_to_idx.keys())
    in_vocab_df = pd.DataFrame(emb, columns=[f"gnn_embed_{i}" for i in range(emb.shape[1])])
    in_vocab_df["npi"] = in_vocab_npis
    in_vocab_df["gnn_in_vocab"] = np.int8(1)

    # Pad with zeros for NPIs in panel but out of vocab
    all_npis_in_panel = _load_panel_all_npis(year)
    out = pd.DataFrame({"npi": all_npis_in_panel})
    out = out.merge(in_vocab_df, on="npi", how="left")
    embed_cols = [c for c in out.columns if c.startswith("gnn_embed_")]
    out[embed_cols] = out[embed_cols].fillna(0.0).astype(np.float64)
    out["gnn_in_vocab"] = out["gnn_in_vocab"].fillna(0).astype(np.int8)
    out["year"] = year

    n_in = int((out["gnn_in_vocab"] == 1).sum())
    n_out = len(out) - n_in
    if verbose:
        print(f"[{year}] panel coverage: {len(out):,} NPIs total — "
              f"{n_in:,} in-vocab + {n_out:,} zero-fill ({n_in/len(out)*100:.1f}%)")

    # Atomic write
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    out_path = CACHE_DIR / f"gnn_raw_{year}.parquet"
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    out.to_parquet(tmp, index=False)
    tmp.replace(out_path)
    if verbose:
        print(f"[{year}] wrote {out_path} ({out_path.stat().st_size / 1024 / 1024:.1f} MB) "
              f"in {time.time()-t0:.1f}s total")
    return out_path


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--encoder", type=Path, required=True,
                   help="Path to encoder .pt file (from pretrain_gnn.py)")
    grp = p.add_mutually_exclusive_group(required=True)
    grp.add_argument("--year", type=int, help="Single year")
    grp.add_argument("--years", type=str, help="Year range (e.g. 2013-2023) or comma list")
    p.add_argument("--device", choices=("cpu", "mps", "cuda"), default="mps")
    args = p.parse_args()

    years = [args.year] if args.year is not None else _parse_years(args.years)

    for y in years:
        export_year(y, args.encoder, device_str=args.device)


if __name__ == "__main__":
    main()
