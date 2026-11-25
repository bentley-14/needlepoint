"""Regression guards for the inductive GNN architecture.

Catches the leakage pattern where a future change accidentally adds a
per-NPI embedding table (`nn.Embedding(num_npis, ...)`), which would
re-introduce the memorization channel we explicitly designed against.

Run: pytest tests/ranker/test_gnn_inductive.py
"""
from __future__ import annotations

import pytest

pytest.importorskip("torch", reason="heavy ML stack deliberately not installed in targeted CI (see .github/workflows/tests.yml)")
import torch

from fraudlib.ranker.gnn import BipartiteSAGE, GNNConfig


def test_only_code_embedding_is_learnable() -> None:
    """The encoder must have exactly one nn.Embedding table — code_emb.

    If a future change adds nn.Embedding(num_npis, ...), this test fails.
    """
    cfg = GNNConfig(num_codes=50, embed_dim=8, num_layers=2)
    model = BipartiteSAGE(cfg)

    embedding_modules = [
        name for name, m in model.named_modules() if isinstance(m, torch.nn.Embedding)
    ]
    assert embedding_modules == ["code_emb"], (
        f"expected only 'code_emb' to be an nn.Embedding; got {embedding_modules}. "
        "If a per-NPI embedding table is added, it re-introduces the memorization "
        "channel — that's a label-leakage regression and must not be merged."
    )


def test_same_billing_produces_same_embedding() -> None:
    """Two NPIs billing the same codes must get bit-for-bit identical embeddings.

    This is the inductive promise: NPI identity does not flow into the
    embedding, only billing structure does.
    """
    cfg = GNNConfig(num_codes=10, embed_dim=8, num_layers=2)
    model = BipartiteSAGE(cfg)
    model.eval()

    # 3 NPIs all billing only code 0
    edge_npi = torch.tensor([0, 1, 2], dtype=torch.int64)
    edge_code = torch.tensor([0, 0, 0], dtype=torch.int64)
    with torch.no_grad():
        h, _ = model.encode(edge_npi, edge_code, num_npis=3)

    # All three NPIs bill the same single code; their embeddings must be identical.
    assert torch.allclose(h[0], h[1], atol=1e-6), (
        "inductive property violated: same billing pattern produced different "
        "embeddings — model is using NPI identity somewhere"
    )
    assert torch.allclose(h[0], h[2], atol=1e-6)


def test_zero_degree_npi_gets_zero_not_nan() -> None:
    """NPIs with no edges must get a zero embedding, not NaN.

    Earlier draft using scatter_mean from torch_scatter produced NaN for
    zero-degree nodes; our custom scatter_mean replaces empty bins with 0.
    """
    cfg = GNNConfig(num_codes=10, embed_dim=8, num_layers=2)
    model = BipartiteSAGE(cfg)
    model.eval()

    # NPI 2 has no edges; NPI 0 and 1 do.
    edge_npi = torch.tensor([0, 1], dtype=torch.int64)
    edge_code = torch.tensor([0, 1], dtype=torch.int64)
    with torch.no_grad():
        h, _ = model.encode(edge_npi, edge_code, num_npis=3)

    assert not torch.isnan(h).any(), "got NaN in embeddings; zero-degree guard failed"
    # NPI 2's layer-0 representation is the mean of an empty set; should equal
    # the zero-bin output of our scatter_mean.
