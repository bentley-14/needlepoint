"""Bipartite GraphSAGE encoder for NPI×HCPCS graph — INDUCTIVE.

Plain PyTorch — no PyTorch Geometric (partial MPS support). Message
passing is implemented with explicit ``index_select`` + ``scatter_add``
on dense edge_index tensors, which fully support MPS.

Architecture: bipartite mean-aggregation GraphSAGE, **inductive variant**.

  - Code embeddings: learned ``nn.Embedding(num_codes, embed_dim)``.
    Transductive — code IDs are stable across years.
  - NPI initial representation: **NOT a learned table**. Computed at
    forward time as the mean of the NPI's billed code embeddings. This
    is the standard inductive GraphSAGE pattern.
  - Per layer: NPI ← projection(self + mean(code neighbors)); same for
    codes. Both with LayerNorm + ReLU.

**Why inductive (no `nn.Embedding(num_npis, ...)`):**

A learnable per-NPI embedding table would let the model memorize specific
NPIs across years — including DOJ-positive NPIs that appear in multiple
years' billing data. That's a serious label-leakage vector even though
the pretrain objective itself is self-supervised (the encoder still
*identifies* specific NPIs by their embedding row). Dropping the table
forces the model to derive each NPI's representation purely from their
code billing pattern, so:
  - Same NPI billing the same codes in two years → same embedding (no
    memorization channel).
  - Different code mix in different years → different embedding (correctly
    captures change).
  - Train/test NPI identity is not encoded.

For the masked-code training objective, this also means: when we mask
some of an NPI's codes, the model can't "look up" the NPI's identity to
infer the masked codes. It has to learn from the remaining context codes
alone, which is what we want.

Configurable hyperparameters: ``num_layers``, ``embed_dim``.
"""
from __future__ import annotations

from dataclasses import dataclass

import torch
import torch.nn as nn


@dataclass
class GNNConfig:
    num_codes: int
    embed_dim: int = 64
    num_layers: int = 2
    dropout: float = 0.1
    # num_npis kept for backward-compat in encoder_meta.json; not used
    # to create an embedding table any more.
    num_npis: int | None = None


def scatter_mean(
    src: torch.Tensor, index: torch.Tensor, dim_size: int
) -> torch.Tensor:
    """Mean-aggregate ``src`` rows into ``dim_size`` bins indexed by ``index``.

    src     : (E, D) tensor of edge messages
    index   : (E,) int64 indices into the output bins (NPI or code idx)
    dim_size: number of output bins

    Returns (dim_size, D). Empty bins → 0 (NOT NaN). Avoids the NaN that
    scatter_mean from torch_scatter gives for zero-degree nodes.
    """
    D = src.shape[1]
    out = torch.zeros((dim_size, D), dtype=src.dtype, device=src.device)
    out.scatter_add_(0, index.unsqueeze(1).expand(-1, D), src)

    counts = torch.zeros(dim_size, dtype=src.dtype, device=src.device)
    counts.scatter_add_(0, index, torch.ones_like(index, dtype=src.dtype))
    counts = counts.clamp_min(1.0).unsqueeze(1)
    return out / counts


class BipartiteSAGELayer(nn.Module):
    """One layer of bipartite mean-aggregation GraphSAGE.

    Symmetric: NPI gets a message from neighboring codes; codes get a
    message from neighboring NPIs. Each side concatenates its self
    embedding with the aggregated message and projects through a Linear.
    """

    def __init__(self, embed_dim: int):
        super().__init__()
        self.proj_npi = nn.Linear(embed_dim * 2, embed_dim)
        self.proj_code = nn.Linear(embed_dim * 2, embed_dim)
        self.norm_npi = nn.LayerNorm(embed_dim)
        self.norm_code = nn.LayerNorm(embed_dim)

    def forward(
        self,
        h_npi: torch.Tensor,        # (N, D)
        h_code: torch.Tensor,       # (C, D)
        edge_npi_idx: torch.Tensor, # (E,) NPI indices for each edge
        edge_code_idx: torch.Tensor # (E,) code indices for each edge
    ) -> tuple[torch.Tensor, torch.Tensor]:
        # NPI ← aggregate(code neighbors)
        msgs_to_npi = h_code[edge_code_idx]  # (E, D)
        agg_npi = scatter_mean(msgs_to_npi, edge_npi_idx, h_npi.shape[0])
        h_npi_new = self.proj_npi(torch.cat([h_npi, agg_npi], dim=1))
        h_npi_new = self.norm_npi(torch.relu(h_npi_new))

        # Code ← aggregate(NPI neighbors)
        msgs_to_code = h_npi[edge_npi_idx]  # (E, D)
        agg_code = scatter_mean(msgs_to_code, edge_code_idx, h_code.shape[0])
        h_code_new = self.proj_code(torch.cat([h_code, agg_code], dim=1))
        h_code_new = self.norm_code(torch.relu(h_code_new))

        return h_npi_new, h_code_new


class BipartiteSAGE(nn.Module):
    """Inductive bipartite GraphSAGE encoder.

    Stores only the code embedding table; NPI representations are
    computed at forward time from the bipartite edge structure.

    Two methods:
      - ``encode(edge_npi_idx, edge_code_idx, num_npis)`` runs
        message passing and returns (h_npi, h_code). ``num_npis`` is the
        number of NPI nodes for this forward pass (varies between
        pretrain and per-year export — that's exactly the inductive
        property we want).
      - ``predict_codes(...)`` scores each (NPI, code) pair via dot
        product between final NPI embedding and final code embedding.
        Used by the masked-code training objective. Decoder uses tied
        weights with the code embedding table.
    """

    def __init__(self, cfg: GNNConfig):
        super().__init__()
        self.cfg = cfg
        # ONLY the code embedding is a learned table. NPI representations
        # are induced from the bipartite edges at forward time.
        self.code_emb = nn.Embedding(cfg.num_codes, cfg.embed_dim)
        self.layers = nn.ModuleList(
            [BipartiteSAGELayer(cfg.embed_dim) for _ in range(cfg.num_layers)]
        )
        self.dropout = nn.Dropout(cfg.dropout)
        nn.init.normal_(self.code_emb.weight, std=0.1)

    def _init_npi_from_codes(
        self,
        edge_npi_idx: torch.Tensor,
        edge_code_idx: torch.Tensor,
        h_code: torch.Tensor,
        num_npis: int,
    ) -> torch.Tensor:
        """Layer-0 NPI representation: mean of billed code embeddings.

        This is the inductive piece. An NPI's initial vector is *derived*
        from the codes they bill — there's no learnable NPI-identity
        table to memorize.
        """
        # h_code[edge_code_idx] = (E, D) — each edge's code embedding
        # scatter_mean to NPIs → (num_npis, D)
        msgs = h_code[edge_code_idx]
        return scatter_mean(msgs, edge_npi_idx, num_npis)

    def encode(
        self,
        edge_npi_idx: torch.Tensor,
        edge_code_idx: torch.Tensor,
        num_npis: int,
    ) -> tuple[torch.Tensor, torch.Tensor]:
        """Run ``num_layers`` of message passing; return (npi_h, code_h).

        Parameters
        ----------
        edge_npi_idx, edge_code_idx
            Edge list as (E,) int64 tensors.
        num_npis
            Number of NPI nodes in this graph. Required because NPI is
            inductive — no per-NPI embedding table to infer size from.
        """
        h_code = self.dropout(self.code_emb.weight)
        h_npi = self._init_npi_from_codes(
            edge_npi_idx, edge_code_idx, h_code, num_npis
        )
        for layer in self.layers:
            h_npi, h_code = layer(h_npi, h_code, edge_npi_idx, edge_code_idx)
        return h_npi, h_code

    def forward_npi(
        self,
        edge_npi_idx: torch.Tensor,
        edge_code_idx: torch.Tensor,
        num_npis: int,
    ) -> torch.Tensor:
        h_npi, _ = self.encode(edge_npi_idx, edge_code_idx, num_npis)
        return h_npi

    def predict_codes(
        self,
        edge_npi_idx: torch.Tensor,
        edge_code_idx: torch.Tensor,
        target_npi_idx: torch.Tensor,
        num_npis: int,
    ) -> torch.Tensor:
        """Score each ``target_npi_idx`` × all codes.

        Returns logits ``(len(target_npi_idx), num_codes)`` — dot product
        of NPI's encoded embedding with every code's encoded embedding.
        Used by the masked-code training objective. Decoder uses tied
        weights with the code embedding table.
        """
        h_npi, h_code = self.encode(edge_npi_idx, edge_code_idx, num_npis)
        h_target = h_npi[target_npi_idx]  # (B, D)
        return h_target @ h_code.T  # (B, num_codes)
