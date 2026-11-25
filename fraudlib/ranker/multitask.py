"""Multi-task neural network for per-archetype fraud detection.

Shared encoder MLP (3 hidden layers, 256 units) learns a provider
representation. Per-archetype binary heads predict fraud for each
archetype. The shared representation is trained on the union of all
archetype losses, so signal from BILLING_FRAUD (450 positives) helps
regularize OPIOID_MILL (22 positives).

Architecture:
    provider features (N)
        │
        ▼
    SharedEncoder: FC(N→256) → ReLU → BN → Dropout
                   FC(256→128) → ReLU → BN → Dropout
                   FC(128→64) → ReLU → BN → Dropout
        │
        ▼  shared embedding (64-d)
        │
        ├──▶ Head_OPIOID:      FC(64→1) → Sigmoid
        ├──▶ Head_BILLING:     FC(64→1) → Sigmoid
        ├──▶ Head_KICKBACK:    FC(64→1) → Sigmoid
        ├──▶ Head_UPCODING:    FC(64→1) → Sigmoid
        ├──▶ Head_MED_UNNEC:   FC(64→1) → Sigmoid
        └──▶ Head_STATE_EXCL:  FC(64→1) → Sigmoid

Loss = Σ_archetypes (class_weight_i × BCE(head_i(x), y_i))

Per Johnson 2023 (IEEE Access): multi-task shared encoder on Part B/D
gives +5% F1 over single-head on heterogeneous Medicare fraud labels.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

import numpy as np
import pandas as pd
import torch
import torch.nn as nn
from torch.utils.data import DataLoader, TensorDataset


@dataclass
class MultitaskConfig:
    hidden_dims: tuple[int, ...] = (256, 128, 64)
    dropout: float = 0.3
    lr: float = 1e-3
    weight_decay: float = 1e-4
    batch_size: int = 2048
    n_epochs: int = 50
    patience: int = 8
    device: str = "auto"          # "auto" resolves to mps (Apple Silicon) / cuda / cpu
    seed: int = 0                 # reproducibility: seeds torch + numpy + DataLoader


class SharedEncoder(nn.Module):
    def __init__(self, input_dim: int, hidden_dims: tuple[int, ...], dropout: float):
        super().__init__()
        layers = []
        prev = input_dim
        for h in hidden_dims:
            layers.extend([
                nn.Linear(prev, h),
                nn.ReLU(),
                nn.BatchNorm1d(h),
                nn.Dropout(dropout),
            ])
            prev = h
        self.net = nn.Sequential(*layers)
        self.output_dim = hidden_dims[-1]

    def forward(self, x):
        return self.net(x)


class ArchetypeHead(nn.Module):
    def __init__(self, input_dim: int):
        super().__init__()
        self.fc = nn.Linear(input_dim, 1)

    def forward(self, x):
        return self.fc(x).squeeze(-1)


class MultitaskFraudModel(nn.Module):
    def __init__(self, input_dim: int, archetypes: Sequence[str], config: MultitaskConfig):
        super().__init__()
        self.encoder = SharedEncoder(input_dim, config.hidden_dims, config.dropout)
        self.heads = nn.ModuleDict({
            arch: ArchetypeHead(self.encoder.output_dim) for arch in archetypes
        })
        self.archetypes = list(archetypes)

    def forward(self, x):
        z = self.encoder(x)
        return {arch: head(z) for arch, head in self.heads.items()}

    def predict_proba(self, x):
        """Return per-archetype probabilities as dict of arrays."""
        self.eval()
        with torch.no_grad():
            logits = self.forward(x)
            return {arch: torch.sigmoid(l).cpu().numpy() for arch, l in logits.items()}


def _seed_everything(seed: int) -> None:
    """Seed python / numpy / torch RNGs for reproducible training.

    Exactly reproducible on CPU. On MPS (Apple Silicon) a few ops are not fully
    deterministic even when seeded, so GPU runs are reproducible-ish. The XGBoost
    heads are seeded separately (random_state=0)."""
    import random
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)


def _resolve_device(spec: str) -> torch.device:
    """Resolve config.device; "auto" prefers MPS (M-series), then CUDA, then CPU.
    The MULTITASK_DEVICE env var overrides everything (operator escape hatch)."""
    import os
    env = os.environ.get("MULTITASK_DEVICE", "").strip()
    if env:
        return torch.device(env)
    if spec and spec != "auto":
        return torch.device(spec)
    if torch.backends.mps.is_available():
        return torch.device("mps")
    if torch.cuda.is_available():
        return torch.device("cuda")
    return torch.device("cpu")


def train_multitask(
    X_train: np.ndarray,
    y_train: dict[str, np.ndarray],
    X_val: np.ndarray,
    y_val: dict[str, np.ndarray],
    archetypes: Sequence[str],
    config: MultitaskConfig | None = None,
    verbose: bool = True,
) -> MultitaskFraudModel:
    """Train a multi-task fraud model.

    Parameters
    ----------
    X_train, X_val : float arrays (n_samples, n_features)
    y_train, y_val : dict[archetype → binary array (n_samples,)]
    archetypes : list of archetype names
    config : hyperparameters
    """
    if config is None:
        config = MultitaskConfig()
    import os as _os
    _eff_seed = int(_os.environ.get("MULTITASK_SEED", config.seed))
    _seed_everything(_eff_seed)
    device = _resolve_device(config.device)
    n_features = X_train.shape[1]

    model = MultitaskFraudModel(n_features, archetypes, config).to(device)

    # Class weights per archetype (inverse frequency, capped)
    class_weights = {}
    for arch in archetypes:
        n_pos = y_train[arch].sum()
        n_neg = len(y_train[arch]) - n_pos
        w = float(min(n_neg / max(n_pos, 1), 50000.0))   # python float -> torch float32 (MPS rejects float64)
        class_weights[arch] = w

    optimizer = torch.optim.AdamW(
        model.parameters(), lr=config.lr, weight_decay=config.weight_decay
    )
    scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(
        optimizer, mode="min", patience=3, factor=0.5
    )

    # Build tensors
    X_t = torch.tensor(X_train, dtype=torch.float32)
    y_ts = {arch: torch.tensor(y_train[arch], dtype=torch.float32) for arch in archetypes}
    X_v = torch.tensor(X_val, dtype=torch.float32).to(device)
    y_vs = {arch: torch.tensor(y_val[arch], dtype=torch.float32).to(device) for arch in archetypes}

    train_ds = TensorDataset(X_t, *[y_ts[a] for a in archetypes])
    _g = torch.Generator()
    _g.manual_seed(_eff_seed)
    train_dl = DataLoader(train_ds, batch_size=config.batch_size, shuffle=True,
                          drop_last=True, generator=_g)

    best_val_loss = float("inf")
    best_state = None
    patience_counter = 0

    for epoch in range(config.n_epochs):
        model.train()
        epoch_loss = 0.0
        for batch in train_dl:
            x_b = batch[0].to(device)
            optimizer.zero_grad()
            logits = model(x_b)
            loss = torch.tensor(0.0, device=device)
            for i, arch in enumerate(archetypes):
                y_b = batch[i + 1].to(device)
                w = class_weights[arch]
                pos_weight = torch.tensor([w], device=device)
                loss += nn.functional.binary_cross_entropy_with_logits(
                    logits[arch], y_b, pos_weight=pos_weight
                )
            loss.backward()
            optimizer.step()
            epoch_loss += loss.item()

        # Validation loss
        model.eval()
        with torch.no_grad():
            val_logits = model(X_v)
            val_loss = 0.0
            for arch in archetypes:
                w = class_weights[arch]
                pos_weight = torch.tensor([w], device=device)
                val_loss += nn.functional.binary_cross_entropy_with_logits(
                    val_logits[arch], y_vs[arch], pos_weight=pos_weight
                ).item()

        scheduler.step(val_loss)

        if val_loss < best_val_loss:
            best_val_loss = val_loss
            best_state = {k: v.cpu().clone() for k, v in model.state_dict().items()}
            patience_counter = 0
        else:
            patience_counter += 1

        if verbose and (epoch + 1) % 5 == 0:
            print(f"    epoch {epoch+1:>3}: train_loss={epoch_loss/len(train_dl):.4f} "
                  f"val_loss={val_loss:.4f} lr={optimizer.param_groups[0]['lr']:.6f}")

        if patience_counter >= config.patience:
            if verbose:
                print(f"    early stop at epoch {epoch+1}")
            break

    if best_state is not None:
        model.load_state_dict(best_state)
    model.eval()
    return model


def score_multitask(
    model: MultitaskFraudModel,
    X: np.ndarray,
    aggregation: str = "sum",
) -> np.ndarray:
    """Score providers with the multi-task model.

    Returns a 1-d array of aggregated fraud scores.
    """
    device = next(model.parameters()).device
    X_t = torch.tensor(X, dtype=torch.float32).to(device)
    probs = model.predict_proba(X_t)
    prob_matrix = np.column_stack([probs[arch] for arch in model.archetypes])
    if aggregation == "sum":
        return prob_matrix.sum(axis=1)
    if aggregation == "max":
        return prob_matrix.max(axis=1)
    raise ValueError(f"unknown aggregation: {aggregation}")
