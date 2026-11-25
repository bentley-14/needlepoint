"""IRM-trained supervised ranker (Sparse IRM, Zhou et al. ICML 2022).

Layer 2 of the IRM + anomaly hybrid. Treats each
training year as a separate environment and penalises features whose
predictive direction varies across years — the exact failure mode the
2026-04-27 SHAP analysis surfaced (``partb_total_medicare_allowed``
SHAP polarity flips between 2013-17 and 2019).

Loss
----
    L(theta) = mean_e[CE_e(theta)]
             + lambda_irm * mean_e[ (∂/∂w CE_e(w * theta(x), y) |_{w=1})^2 ]
             + alpha_l1 * ||w_last||_1     (Sparse IRM extension)

The penalty term is the squared gradient of the per-env loss w.r.t. a
scalar dummy classifier weight; if the predictor is invariant across
environments, the gradient is zero in every env. The L1 term on the
final-layer weights drives spurious-feature coefficients to zero.

Public API
----------
train_irm(df, env_col, feature_cols, ...) -> IRMModel
IRMModel.score(df) -> pd.Series  (probabilities, polarity-aligned with classifier)
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Sequence

import numpy as np
import pandas as pd
import torch
import torch.nn as nn
import torch.nn.functional as F

if sys.platform == "darwin":
    # macOS pip builds of PyTorch and XGBoost each bundle libomp; with both multithreaded in
    # one process (walk-forward trains XGBoost, then this net) they deadlock. One torch thread
    # avoids it at little cost for these small networks.
    torch.set_num_threads(1)


# ---------------------------------------------------------------------------
# Model + IRM penalty
# ---------------------------------------------------------------------------

class _MLP(nn.Module):
    """Small MLP suitable for tabular fraud features.

    input → hidden → hidden → 1 logit.
    Default 128 → 64 hidden; linear (hidden=()) when set explicitly.
    """

    def __init__(self, in_dim: int, hidden: tuple[int, ...] = (128, 64), dropout: float = 0.1):
        super().__init__()
        layers: list[nn.Module] = []
        prev = in_dim
        for h in hidden:
            layers += [nn.Linear(prev, h), nn.ReLU(), nn.Dropout(dropout)]
            prev = h
        layers.append(nn.Linear(prev, 1))
        self.net = nn.Sequential(*layers)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.net(x).squeeze(-1)

    def last_linear(self) -> nn.Linear:
        # Last Linear layer (used for the L1 sparsity penalty on the
        # output projection); skip dropout/relu in the sequential.
        for layer in reversed(list(self.net)):
            if isinstance(layer, nn.Linear):
                return layer
        raise RuntimeError("MLP has no Linear layer (?)")


def _irm_penalty(logits: torch.Tensor, y: torch.Tensor) -> torch.Tensor:
    """Squared gradient of the per-env CE loss w.r.t. a dummy classifier w=1.

    Implements Arjovsky et al. 2019's IRMv1 penalty exactly. ``logits``
    and ``y`` are 1-D float tensors (after sigmoid → probabilities).
    """
    # Dummy scalar; requires_grad lets autograd compute the partial.
    w = torch.tensor(1.0, requires_grad=True, device=logits.device)
    loss = F.binary_cross_entropy_with_logits(logits * w, y.float(), reduction="mean")
    grad = torch.autograd.grad(loss, w, create_graph=True)[0]
    return grad.pow(2)


# ---------------------------------------------------------------------------
# Trained model wrapper
# ---------------------------------------------------------------------------

@dataclass
class IRMModel:
    """Fitted IRM model + the metadata needed to score new frames.

    Attributes
    ----------
    model : _MLP
        The torch module.
    feature_cols : list[str]
        Column order at fit time. score() reindexes.
    feature_means / feature_stds : np.ndarray
        Per-column standardisation stats (fit on training data only).
        IRM training is unstable without input scaling.
    train_envs : list[int]
        Environment values present at fit time (e.g. [2014, 2016, 2017]).
    """

    model: _MLP
    feature_cols: list[str]
    feature_means: np.ndarray
    feature_stds: np.ndarray
    train_envs: list[int] = field(default_factory=list)
    final_loss: float = 0.0
    history: list[dict] = field(default_factory=list)

    def score(self, df: pd.DataFrame) -> pd.Series:
        """Return per-row fraud probability (polarity matches XGBClassifier)."""
        X = self._prepare(df)
        self.model.eval()
        with torch.no_grad():
            logits = self.model(X)
            probs = torch.sigmoid(logits).cpu().numpy()
        return pd.Series(probs, index=df.index, name="irm_score")

    def _prepare(self, df: pd.DataFrame) -> torch.Tensor:
        X = df.reindex(columns=self.feature_cols, fill_value=0.0)
        X = X.astype(np.float32).fillna(0.0).to_numpy()
        X = (X - self.feature_means) / np.where(self.feature_stds > 1e-9, self.feature_stds, 1.0)
        return torch.from_numpy(X.astype(np.float32))


# ---------------------------------------------------------------------------
# Training loop
# ---------------------------------------------------------------------------

def train_irm(
    df: pd.DataFrame,
    env_col: str,
    feature_cols: Sequence[str],
    label_col: str = "y",
    *,
    n_epochs: int = 200,
    batch_size: int = 8192,
    lr: float = 1e-3,
    weight_decay: float = 1e-4,
    lambda_irm: float = 1.0,
    alpha_l1: float = 1e-4,
    hidden: tuple[int, ...] = (128, 64),
    dropout: float = 0.1,
    pos_weight: float | None = None,
    irm_warmup_epochs: int = 20,
    seed: int = 0,
    device: str = "cpu",
    verbose: bool = True,
) -> IRMModel:
    """Train an MLP with Sparse IRM loss across environments.

    Parameters
    ----------
    df
        Training DataFrame. Must contain ``env_col``, ``label_col``, and
        every column in ``feature_cols``.
    env_col
        Column name identifying the environment (typically ``puf_year``).
    feature_cols
        Numeric feature columns to use.
    label_col
        Binary target column (0/1).
    n_epochs
        Total epochs. ``irm_warmup_epochs`` of pure ERM first, then
        IRM penalty turned on.
    batch_size
        Per-env minibatch size. Each step samples a batch from each env
        and accumulates loss across them.
    lambda_irm
        Weight on the IRM penalty. 1.0 = balanced; the published paper
        uses 1.0 with a warm-up.
    alpha_l1
        Sparsity weight on the final-layer linear projection.
    pos_weight
        Class-imbalance weight for binary CE. None → auto from the
        full-data ratio (n_neg / n_pos).
    irm_warmup_epochs
        Train with ``lambda_irm = 0`` for this many epochs first; the
        IRM penalty is unstable at random initialisation.

    Returns
    -------
    IRMModel
        Fitted model with ``.score(df)`` API matching FraudModel.
    """
    torch.manual_seed(seed)
    np.random.seed(seed)
    device_t = torch.device(device)

    feature_cols = list(feature_cols)

    # ── Standardise features (fit stats on training only) ──────────────
    X_full = df[feature_cols].astype(np.float32).fillna(0.0).to_numpy()
    feature_means = X_full.mean(axis=0)
    feature_stds = X_full.std(axis=0)
    X_full = (X_full - feature_means) / np.where(feature_stds > 1e-9, feature_stds, 1.0)
    y_full = df[label_col].astype(int).to_numpy()
    env_full = df[env_col].astype(int).to_numpy()

    # Class imbalance weight (BCE pos_weight).
    if pos_weight is None:
        n_pos = max(int(y_full.sum()), 1)
        n_neg = int((y_full == 0).sum())
        pos_weight = n_neg / n_pos
    pos_weight_t = torch.tensor(float(pos_weight), device=device_t)

    # Per-env masks (cache as numpy index arrays).
    envs = sorted(np.unique(env_full).tolist())
    env_idx = {e: np.where(env_full == e)[0] for e in envs}
    if verbose:
        for e, idx in env_idx.items():
            print(f"  env {e}: {len(idx):,} rows  ({int(y_full[idx].sum())} pos)")

    # ── Model + optimizer ─────────────────────────────────────────────
    model = _MLP(in_dim=len(feature_cols), hidden=hidden, dropout=dropout).to(device_t)
    optim = torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=weight_decay)

    history: list[dict] = []
    rng = np.random.default_rng(seed)

    for epoch in range(n_epochs):
        model.train()
        # Per-epoch loss accumulators
        epoch_erm = 0.0
        epoch_pen = 0.0
        epoch_l1 = 0.0
        steps_per_epoch = max(min(len(idx) for idx in env_idx.values()) // batch_size, 1)
        lam = lambda_irm if epoch >= irm_warmup_epochs else 0.0

        for _step in range(steps_per_epoch):
            optim.zero_grad()
            env_losses = []
            env_penalties = []

            for e in envs:
                idx = env_idx[e]
                # Stratified batch: ensure ≥1 positive per env per step
                # by sampling positives + negatives separately.
                pos_idx = idx[y_full[idx] == 1]
                neg_idx = idx[y_full[idx] == 0]
                if len(pos_idx) == 0:
                    continue
                n_pos_b = max(min(batch_size // 4, len(pos_idx)), 1)
                n_neg_b = batch_size - n_pos_b
                batch_pos = rng.choice(pos_idx, size=n_pos_b, replace=len(pos_idx) < n_pos_b)
                batch_neg = rng.choice(neg_idx, size=min(n_neg_b, len(neg_idx)), replace=False)
                batch = np.concatenate([batch_pos, batch_neg])

                xb = torch.from_numpy(X_full[batch].astype(np.float32)).to(device_t)
                yb = torch.from_numpy(y_full[batch].astype(np.float32)).to(device_t)

                logits = model(xb)
                env_loss = F.binary_cross_entropy_with_logits(
                    logits, yb, pos_weight=pos_weight_t
                )
                env_pen = _irm_penalty(logits, yb)
                env_losses.append(env_loss)
                env_penalties.append(env_pen)

            if not env_losses:
                continue

            erm_term = torch.stack(env_losses).mean()
            penalty_term = torch.stack(env_penalties).mean()
            # L1 on final linear layer's weight
            last = model.last_linear()
            l1_term = last.weight.abs().sum()

            total = erm_term + lam * penalty_term + alpha_l1 * l1_term
            total.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), max_norm=10.0)
            optim.step()

            epoch_erm += erm_term.item()
            epoch_pen += penalty_term.item()
            epoch_l1 += l1_term.item()

        history.append({
            "epoch": epoch,
            "erm": epoch_erm / max(steps_per_epoch, 1),
            "irm_penalty": epoch_pen / max(steps_per_epoch, 1),
            "l1": epoch_l1 / max(steps_per_epoch, 1),
            "lambda_irm": lam,
        })
        if verbose and (epoch % max(n_epochs // 20, 1) == 0 or epoch == n_epochs - 1):
            h = history[-1]
            print(
                f"  epoch {epoch:3d}/{n_epochs}  "
                f"erm={h['erm']:.4f}  irm_pen={h['irm_penalty']:.6f}  "
                f"l1={h['l1']:.3f}  lam={h['lambda_irm']:.2f}"
            )

    return IRMModel(
        model=model,
        feature_cols=feature_cols,
        feature_means=feature_means,
        feature_stds=feature_stds,
        train_envs=envs,
        final_loss=history[-1]["erm"] if history else 0.0,
        history=history,
    )
