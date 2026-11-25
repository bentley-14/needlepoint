"""Self-supervised pretrain of the bipartite GraphSAGE encoder (INDUCTIVE).

Objective: masked code prediction. Per epoch:
  1. Randomly mask 15% of edges as "target" (held out from message passing).
  2. Run the encoder on the remaining 85% "context" edges.
  3. For each target edge (npi_i, code_j), predict code_j from npi_i's
     encoded embedding via dot product against the full code embedding
     table. Cross-entropy loss.
  4. Backprop end-to-end.

No labels used — purely self-supervised. Encoder learns to compress
each NPI's billing pattern into a ``embed_dim``-vector that captures
which other codes the NPI tends to co-bill.

**Inductive architecture (leakage fix):** The encoder has NO per-NPI
embedding table — each NPI's representation is derived at forward time
from the codes they bill. This prevents the encoder from memorizing
specific positive NPIs across years. Combined with the downstream
XGBoost NPI-symmetric holdout, this is sufficient label-leakage
protection.

**Default: pool 2013-2023.** Given the inductive architecture, training
on all years is fine — labels never enter pretrain, and there's no
per-NPI memorization channel. Pooling gives better code-embedding
coverage and handles HCPCS drift across years.

The ``--score-year`` flag is retained as an *optional ablation* — for
strict experimental purists who want to verify pooled-vs-cohort doesn't
change anything. Not required for the production path.

Output: ``data/cache/gnn_features/encoder_{tag}_L{num_layers}_D{embed_dim}.pt``
where ``tag`` is ``pooled`` (default) or ``S{score_year}`` (cohort ablation).

Usage:
    # Production (recommended): pool all years
    python scripts/experiments/pretrain_gnn.py

    # Custom hyperparameters for ablation
    python scripts/experiments/pretrain_gnn.py --num-layers 3 --embed-dim 32

    # Cohort ablation: train only on years <= score_year - 2 (optional, not required)
    python scripts/experiments/pretrain_gnn.py --score-year 2022

    # Smoke test on a single year
    python scripts/experiments/pretrain_gnn.py --years 2018 --epochs 3
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
import torch
import torch.nn.functional as F

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fraudlib.ranker.gnn import BipartiteSAGE, GNNConfig  # noqa: E402
from fraudlib.ranker.gnn_data import build_bipartite  # noqa: E402

CACHE_DIR = Path("data/cache/gnn_features")


def _parse_years(spec: str) -> list[int]:
    if "-" in spec:
        a, b = spec.split("-", 1)
        return list(range(int(a), int(b) + 1))
    return [int(s) for s in spec.split(",")]


def _split_edges(
    edge_index: np.ndarray,
    *,
    val_frac: float = 0.05,
    seed: int = 0,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Split edges into (train_mask, val_mask, train_indices, val_indices).

    val_mask edges are held out across the whole training run (validation set);
    train_mask edges are subject to per-epoch random masking for the masked-code
    objective.
    """
    rng = np.random.default_rng(seed)
    E = edge_index.shape[1]
    perm = rng.permutation(E)
    n_val = int(E * val_frac)
    val_idx = perm[:n_val]
    train_idx = perm[n_val:]
    return train_idx, val_idx


def _evaluate_topk(
    model: BipartiteSAGE,
    edge_index_t: torch.Tensor,
    val_npi: np.ndarray,
    val_code: np.ndarray,
    *,
    num_npis: int,
    k: int = 10,
    batch_size: int = 4096,
) -> float:
    """Top-k masked-code accuracy on held-out edges.

    For each held-out (npi, code) edge: encode NPI using ALL edges (the
    val edge wasn't seen during pretrain context anyway), get predicted
    logits over codes, check if true code is in top-k.
    """
    model.eval()
    with torch.no_grad():
        edge_npi = edge_index_t[0]
        edge_code = edge_index_t[1]
        h_npi, h_code = model.encode(edge_npi, edge_code, num_npis=num_npis)
        scores = h_npi @ h_code.T  # (N, C)

        # Batch the validation lookups
        n_correct = 0
        n_total = len(val_npi)
        for start in range(0, n_total, batch_size):
            end = min(start + batch_size, n_total)
            npi_batch = torch.from_numpy(val_npi[start:end]).to(scores.device).long()
            code_batch = torch.from_numpy(val_code[start:end]).to(scores.device).long()
            batch_scores = scores[npi_batch]  # (B, C)
            topk = batch_scores.topk(k, dim=1).indices  # (B, k)
            hits = (topk == code_batch.unsqueeze(1)).any(dim=1)
            n_correct += hits.sum().item()
    model.train()
    return n_correct / max(n_total, 1)


def train(
    years: list[int],
    *,
    score_year: int | None,
    num_layers: int,
    embed_dim: int,
    epochs: int,
    learning_rate: float,
    mask_frac: float,
    val_frac: float,
    device_str: str,
    out_path: Path,
    meta_path: Path,
    max_edges_per_epoch: int | None = None,
    verbose: bool = True,
) -> None:
    print(f"building bipartite graph over years {years} (score-year={score_year})...")
    bg = build_bipartite(years, verbose=verbose)
    print(f"  result: {bg.num_npis:,} NPIs × {bg.num_codes:,} codes, "
          f"{bg.edge_index.shape[1]:,} edges")

    device = torch.device(device_str)
    print(f"using device: {device}")

    edge_index = torch.from_numpy(bg.edge_index).to(device)

    train_idx, val_idx = _split_edges(bg.edge_index, val_frac=val_frac, seed=0)
    print(f"  train edges: {len(train_idx):,}  val edges: {len(val_idx):,}")

    val_npi = bg.edge_index[0, val_idx]
    val_code = bg.edge_index[1, val_idx]

    # Train context = train_idx subset of full edge_index
    train_edge_index_np = bg.edge_index[:, train_idx]

    # Inductive: no num_npis in the model (NPI rep derived from edges).
    cfg = GNNConfig(
        num_codes=bg.num_codes,
        embed_dim=embed_dim,
        num_layers=num_layers,
        num_npis=bg.num_npis,  # stored for meta only, not used in arch
    )
    model = BipartiteSAGE(cfg).to(device)
    n_params = sum(p.numel() for p in model.parameters() if p.requires_grad)
    print(f"  model params: {n_params:,}")

    optimizer = torch.optim.AdamW(model.parameters(), lr=learning_rate, weight_decay=1e-5)
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=epochs)

    rng = np.random.default_rng(0)

    history = []
    for epoch in range(epochs):
        t0 = time.time()

        # Per-epoch random edge subsample (if budget set) — handles MPS buffer
        # cap on large pooled graphs. Each epoch sees a fresh random subset.
        if max_edges_per_epoch is not None and len(train_idx) > max_edges_per_epoch:
            epoch_perm = rng.permutation(len(train_idx))[:max_edges_per_epoch]
            epoch_train_idx = train_idx[epoch_perm]
            epoch_edges = bg.edge_index[:, epoch_train_idx]
        else:
            epoch_edges = train_edge_index_np

        # Per-epoch random masking: pick 15% of (subsampled) train edges as targets
        n_train = epoch_edges.shape[1]
        n_mask = int(n_train * mask_frac)
        mask_perm = rng.permutation(n_train)
        mask_target_pos = mask_perm[:n_mask]   # positions in epoch_edges
        context_pos = mask_perm[n_mask:]       # positions in epoch_edges kept as context

        context_edges = epoch_edges[:, context_pos]
        target_edges = epoch_edges[:, mask_target_pos]

        context_t = torch.from_numpy(context_edges).to(device)
        target_npi_t = torch.from_numpy(target_edges[0]).to(device).long()
        target_code_t = torch.from_numpy(target_edges[1]).to(device).long()

        # Forward (inductive: pass num_npis explicitly)
        model.train()
        optimizer.zero_grad()
        h_npi, h_code = model.encode(context_t[0], context_t[1], num_npis=bg.num_npis)
        # Target NPIs: their embedding × code table = logits
        h_targets = h_npi[target_npi_t]  # (T, D)
        logits = h_targets @ h_code.T     # (T, C)
        loss = F.cross_entropy(logits, target_code_t)

        # Backward
        loss.backward()
        # Clip to avoid blowups on early epochs
        torch.nn.utils.clip_grad_norm_(model.parameters(), max_norm=1.0)
        optimizer.step()
        scheduler.step()

        # Validation
        if (epoch + 1) % max(1, epochs // 10) == 0 or epoch == 0 or epoch == epochs - 1:
            top10 = _evaluate_topk(model, edge_index, val_npi, val_code, num_npis=bg.num_npis, k=10)
            top1 = _evaluate_topk(model, edge_index, val_npi, val_code, num_npis=bg.num_npis, k=1)
            elapsed = time.time() - t0
            history.append({
                "epoch": epoch + 1,
                "loss": float(loss.item()),
                "val_top1": top1,
                "val_top10": top10,
                "epoch_sec": elapsed,
            })
            print(f"  epoch {epoch+1:>3}/{epochs}: loss={loss.item():.4f}  "
                  f"val_top1={top1:.3f}  val_top10={top10:.3f}  "
                  f"({elapsed:.1f}s)")

    # Save
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    torch.save(
        {
            "state_dict": model.state_dict(),
            "config": cfg.__dict__,
            "npi_to_idx": bg.npi_to_idx,
            "code_to_idx": bg.code_to_idx,
        },
        tmp,
    )
    tmp.replace(out_path)
    print(f"  wrote encoder → {out_path}")

    meta = {
        "config": cfg.__dict__,
        "graph_meta": bg.meta,
        "training": {
            "years": years,
            "score_year": score_year,
            "max_train_year": max(years),
            "epochs": epochs,
            "mask_frac": mask_frac,
            "val_frac": val_frac,
            "learning_rate": learning_rate,
            "device": device_str,
        },
        "architecture": {
            "inductive": True,
            "has_npi_embedding_table": False,
            "note": "NPI representations are induced from code-embedding aggregation at forward time",
        },
        "history": history,
        "num_npis": bg.num_npis,
        "num_codes": bg.num_codes,
        "num_edges": int(bg.edge_index.shape[1]),
    }
    tmp_meta = meta_path.with_suffix(meta_path.suffix + ".tmp")
    tmp_meta.write_text(json.dumps(meta, indent=2))
    tmp_meta.replace(meta_path)
    print(f"  wrote meta → {meta_path}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--years", type=str, default="2013-2023",
                   help="Year range (e.g. 2013-2023) or comma list. "
                        "Default pools all years.")
    p.add_argument("--score-year", type=int, default=None,
                   help="OPTIONAL ablation: train on years <= score_year - 2 "
                        "instead of the --years range. Not required given the "
                        "inductive architecture; only use for ablation runs.")
    p.add_argument("--num-layers", type=int, default=2)
    p.add_argument("--embed-dim", type=int, default=64)
    p.add_argument("--epochs", type=int, default=30)
    p.add_argument("--lr", type=float, default=1e-3)
    p.add_argument("--mask-frac", type=float, default=0.15)
    p.add_argument("--val-frac", type=float, default=0.05)
    p.add_argument("--device", choices=("cpu", "mps", "cuda"), default="mps")
    p.add_argument("--max-edges-per-epoch", type=int, default=None,
                   help="Cap the number of train edges seen per epoch. Required for "
                        "MPS when the pooled graph exceeds ~15M edges (MPS buffer "
                        "cap). Recommended ~8M for M4 MPS. Each epoch picks a fresh "
                        "random subset; over many epochs the encoder sees the full graph.")
    args = p.parse_args()

    # Resolve the training cohort.
    if args.score_year is not None:
        # Cohort ablation: override --years with score-year-derived range
        max_year = args.score_year - 2
        years = list(range(2013, max_year + 1))
        score_year = args.score_year
        print(f"cohort ablation: years 2013-{max_year} (score-year={score_year})")
        tag = f"S{score_year}"
    else:
        years = _parse_years(args.years)
        score_year = None
        print(f"pooled training cohort: years {years}")
        tag = "pooled"

    suffix = f"{tag}_L{args.num_layers}_D{args.embed_dim}"
    out_path = CACHE_DIR / f"encoder_{suffix}.pt"
    meta_path = CACHE_DIR / f"encoder_{suffix}.meta.json"

    train(
        years=years,
        score_year=score_year,
        num_layers=args.num_layers,
        embed_dim=args.embed_dim,
        epochs=args.epochs,
        learning_rate=args.lr,
        mask_frac=args.mask_frac,
        val_frac=args.val_frac,
        device_str=args.device,
        out_path=out_path,
        meta_path=meta_path,
        max_edges_per_epoch=args.max_edges_per_epoch,
    )


if __name__ == "__main__":
    main()
