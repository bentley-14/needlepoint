"""Tests for fraudlib/ranker/irm.py.

Three-test plan:
1. Synthetic 2-env dataset where one feature is invariant and another is
   spurious. After IRM training (lambda > 0), the model should rely on
   the invariant feature; with lambda=0 (ERM), it should rely on whichever
   feature has higher in-env predictive power.
2. IRMModel.score() returns probabilities in [0, 1] aligned with the
   sigmoid of the model's logits — sanity for the API contract.
3. lambda_irm=0 path reduces to ERM training (no IRM penalty applied).
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
pytest.importorskip("torch", reason="heavy ML stack deliberately not installed in targeted CI (see .github/workflows/tests.yml)")
import torch

from fraudlib.ranker.irm import IRMModel, _irm_penalty, _MLP, train_irm


# ---------------------------------------------------------------------------
# 1. IRM finds the invariant feature, ERM may pick the spurious one
# ---------------------------------------------------------------------------

def _mk_two_env_dataset(n_per_env: int = 2000, seed: int = 0) -> pd.DataFrame:
    """Build a dataset with one invariant + one spurious feature.

    - x_invariant: predicts y identically in both envs (slope = +1.0)
    - x_spurious: predicts y in env 0 only (slope = +2.0); flipped in env 1 (slope = -2.0)
    - y: noisy linear function of x_invariant only

    A model that relies on x_spurious will do well in-env but fail when
    tested on the unseen direction of the spurious correlation.
    """
    rng = np.random.default_rng(seed)
    rows = []
    for env, sp_slope in [(0, +2.0), (1, -2.0)]:
        for _ in range(n_per_env):
            x_inv = rng.normal(0, 1)
            # y depends ONLY on x_inv (with noise)
            logit = 1.0 * x_inv + rng.normal(0, 0.5)
            y = int(logit > 0)
            # x_spurious is correlated with y in this env, with sign flipping per env
            x_sp = sp_slope * (y - 0.5) + rng.normal(0, 0.5)
            rows.append({"x_invariant": x_inv, "x_spurious": x_sp, "y": y, "env": env})
    return pd.DataFrame(rows)


def test_irm_prefers_invariant_feature_over_spurious():
    """With lambda_irm > 0, the model's gradient w.r.t. x_spurious should
    be smaller than its gradient w.r.t. x_invariant when averaged across
    envs — that's the IRM invariance criterion."""
    df = _mk_two_env_dataset(n_per_env=3000, seed=0)
    model = train_irm(
        df,
        env_col="env",
        feature_cols=["x_invariant", "x_spurious"],
        n_epochs=80,
        irm_warmup_epochs=10,
        lambda_irm=10.0,        # strong IRM regularisation
        alpha_l1=0.0,           # disable sparsity for cleaner test
        hidden=(),              # linear model to make weights interpretable
        lr=5e-3,
        batch_size=512,
        verbose=False,
        seed=0,
    )
    # For a linear MLP (hidden=()), the last_linear weight directly encodes
    # the predictor's coefficients on the standardised inputs.
    last = model.model.last_linear()
    weights = last.weight.detach().cpu().numpy().flatten()
    w_invariant = abs(weights[0])  # x_invariant column
    w_spurious = abs(weights[1])   # x_spurious column

    # IRM should down-weight x_spurious relative to x_invariant.
    # Allow a generous threshold because tiny synthetic data is noisy.
    assert w_invariant > w_spurious, (
        f"IRM should prefer invariant feature; got "
        f"|w_inv|={w_invariant:.3f}, |w_sp|={w_spurious:.3f}"
    )


# ---------------------------------------------------------------------------
# 2. IRMModel.score returns sane probabilities
# ---------------------------------------------------------------------------

def test_irm_model_score_returns_probabilities_in_unit_interval():
    df = _mk_two_env_dataset(n_per_env=500, seed=1)
    model = train_irm(
        df,
        env_col="env",
        feature_cols=["x_invariant", "x_spurious"],
        n_epochs=10,
        irm_warmup_epochs=5,
        verbose=False,
        seed=1,
    )
    test_df = pd.DataFrame({
        "x_invariant": [0.0, 1.0, -1.0],
        "x_spurious": [0.0, 0.0, 0.0],
    })
    scores = model.score(test_df)
    assert scores.name == "irm_score"
    assert (scores >= 0).all() and (scores <= 1).all()
    # Polarity: higher x_invariant → higher fraud probability
    assert scores.iloc[1] > scores.iloc[0] > scores.iloc[2], (
        f"Expected increasing scores with x_invariant; got {scores.tolist()}"
    )


# ---------------------------------------------------------------------------
# 3. lambda_irm = 0 reduces to ERM (penalty term has no effect on training)
# ---------------------------------------------------------------------------

def test_lambda_zero_is_pure_erm():
    """With lambda_irm=0 and irm_warmup spanning all epochs, the IRM penalty
    is never applied. The training should converge to whatever ERM would
    converge to. We sanity-check this by training a tiny model and asserting
    the loss history shows decreasing ERM loss."""
    df = _mk_two_env_dataset(n_per_env=500, seed=2)
    model = train_irm(
        df,
        env_col="env",
        feature_cols=["x_invariant", "x_spurious"],
        n_epochs=20,
        irm_warmup_epochs=20,    # warmup whole run → lambda=0 throughout
        lambda_irm=0.0,
        alpha_l1=0.0,
        verbose=False,
        seed=2,
    )
    assert len(model.history) == 20
    erm_first = model.history[0]["erm"]
    erm_last = model.history[-1]["erm"]
    assert erm_last < erm_first, (
        f"ERM loss should decrease over training; got {erm_first:.4f} → {erm_last:.4f}"
    )
    # IRM penalty was never weighted, so its tracked value being non-zero
    # is fine — it just wasn't part of the gradient signal.
    assert all(h["lambda_irm"] == 0.0 for h in model.history)


# ---------------------------------------------------------------------------
# 4. IRM penalty formula: gradient of CE w.r.t. dummy w=1 is non-trivially zero
# ---------------------------------------------------------------------------

def test_irm_penalty_is_zero_for_random_predictions():
    """When logits are uncorrelated with y (random predictor), the per-env
    CE loss is at its maximum (no info), and the gradient w.r.t. w should
    be near zero. This is a numerical sanity, not a strict equality."""
    torch.manual_seed(0)
    n = 1000
    y = torch.randint(0, 2, (n,)).float()
    # Random logits — no relationship with y
    logits = torch.randn(n, requires_grad=True)
    pen = _irm_penalty(logits, y)
    assert torch.isfinite(pen)
    # Random predictor → small penalty (not exactly zero due to finite n)
    assert pen.item() < 1.0
