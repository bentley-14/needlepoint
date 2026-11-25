#!/usr/bin/env python3
"""Fit the strength-score weights against realized outcomes (loop 4).

THE FORMULA (no vibes):

    The strength score is a probability claim — P(this case deserves filing).
    The principled estimator is MAP logistic regression with the CURRENT
    hand-set weights as the PRIOR MEAN:

        w* = argmax_w  Σᵢ log σ(yᵢ · (b + w·xᵢ))  −  λ · ‖w − w_prior‖²

    where x = the per-case strength components scoring.py already computes
    (each 0-1), y = the realized label, w_prior = scoring.WEIGHTS, and λ
    controls prior trust. Properties that make this defensible:

      · n = 0          → w* = w_prior exactly (today's behavior, unchanged)
      · small n        → data nudges only the weights it has evidence about
      · large n        → weights converge to the empirical log-odds each
                         factor contributes to real FCA outcomes
      · interpretable  → each weight remains "how much this factor moves the
                         filing-worthiness odds", now measured, not asserted

    LABELS come from the world, not from us:
      y=1  DOJ-confirmed defendants (gold_set NPIs; battery true_positives)
      y=0  confirmed-clean controls (battery negative_controls) and
           provisionally — mined hard negatives (cases the loop killed ON THE
           MERITS; see mine_hard_negatives.py). Provisional labels carry
           sample-weight 0.5 and are re-audited monthly by hindsight replay:
           if DOJ later charges a "negative", the label flips — the delayed
           oracle corrects us.

    The LEGAL grounding: the features are a quantified elements-of-the-claim
    checklist (falsity anchor ↔ falsity; within-practice controls + same-
    district precedent ↔ scienter; regulatory predicates ↔ materiality/
    scienter; survival_residual ↔ the mock-MTD), and the labels are outcomes
    the legal system actually produced. Fitted weights are therefore
    empirical estimates of each element's contribution to real FCA results —
    the same way litigation-finance underwriters fit docket-outcome models.

GUARDRAIL: below --min-n labeled cases (default 20, with ≥5 per class) this
tool REPORTS but refuses to RECOMMEND (exit 2) — replacing one set of
asserted numbers with overfit ones would be worse than vibes. Pure numpy;
no sklearn (CI-safe).

Usage:
    python scripts/fit_scoring_weights.py --report
    python scripts/fit_scoring_weights.py --report --lam 5.0 --min-n 20
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "fraud-investigator-v2" / "src"))

DECLINE_CLASS = {"NOT_ACTIONABLE", "COUNSEL_ABANDONED", "INDEFENSIBLE_AT_CAP",
                 "RECONSIDER"}
FILE_CLASS = {"SETTLEMENT_READY", "FILE", "FILE_WITH_DISCOVERY_DEPS"}


def prior_weights() -> dict[str, float]:
    from fraud_investigator import scoring
    return {k: v for k, v in scoring.WEIGHTS.items() if k != "discount_tax"}
    # discount_tax is an adversarial-outcome penalty, not a case feature —
    # excluded from the fit (it's partly determined by the label's own
    # adjudication, i.e. leakage).


# ── dataset assembly ─────────────────────────────────────────────────────────

def _npi_of(summary: dict) -> str | None:
    m = re.search(r"(?<!\d)(\d{10})(?!\d)", summary.get("subject") or "")
    return m.group(1) if m else None


def collect_labeled_cases(root: Path) -> list[dict]:
    """(features, label, weight) rows from completed case summaries whose NPI
    has a ground-truth label."""
    # label sources
    gold_npis: set[str] = set()
    gs = root / "data" / "extracted" / "gold_set.jsonl"
    if gs.exists():
        for line in gs.read_text().splitlines():
            if line.strip():
                npi = str(json.loads(line).get("npi") or "")
                if npi and npi != "None":
                    gold_npis.add(npi)
    controls: set[str] = set()
    bman = root / "validation" / "battery" / "battery.json"
    if bman.exists():
        for s in json.loads(bman.read_text()).get("subjects", []):
            if s.get("kind") == "negative_control":
                controls.add(s["npi"])
    hard_neg: set[str] = set()
    hn = root / "results" / "hard_negatives.jsonl"
    if hn.exists():
        for line in hn.read_text().splitlines():
            if line.strip():
                hard_neg.add(str(json.loads(line).get("npi")))
    hard_neg -= gold_npis  # the oracle wins over our provisional labels

    rows = []
    for p in list(root.glob("fraud-investigator-v2/state/CASE-*/summary.json")) + \
             list(root.glob("fraud-investigator-v2/state/calibration/*/summary.json")):
        try:
            d = json.loads(p.read_text())
        except Exception:
            continue
        comps = d.get("strength_components")
        npi = _npi_of(d)
        if not comps or not npi or not d.get("final_verdict"):
            continue
        if npi in gold_npis:
            label, wt, src = 1, 1.0, "gold_set"
        elif npi in controls:
            label, wt, src = 0, 1.0, "battery_control"
        elif npi in hard_neg:
            label, wt, src = 0, 0.5, "hard_negative_provisional"
        else:
            continue
        rows.append({"npi": npi, "case_id": d.get("case_id"), "label": label,
                     "sample_weight": wt, "source": src,
                     "components": comps, "verdict": d.get("final_verdict")})
    # latest case per NPI (avoid duplicate-NPI pseudo-replication)
    by_npi: dict[str, dict] = {}
    for r in rows:
        by_npi[r["npi"]] = r
    return list(by_npi.values())


# ── MAP logistic fit (pure numpy) ────────────────────────────────────────────

def fit_map_logistic(X: np.ndarray, y: np.ndarray, w_prior: np.ndarray,
                     sample_weight: np.ndarray | None = None,
                     lam: float = 5.0, lr: float = 0.05,
                     iters: int = 5000) -> tuple[np.ndarray, float]:
    """Gradient ascent on the L2-shrunk-to-prior log-likelihood.
    Returns (weights, bias). λ→∞ ⇒ weights→prior (tested invariant)."""
    n, k = X.shape
    sw = np.ones(n) if sample_weight is None else np.asarray(sample_weight, float)
    w = w_prior.astype(float).copy()
    b = 0.0
    # Proximal gradient (forward-backward splitting): take a plain
    # likelihood step, then apply the EXACT closed-form shrinkage toward the
    # prior — w ← prior + (w − prior)/(1 + 2λ·lr/n). Unconditionally stable
    # for any λ (a naive gradient step on the penalty term explodes for
    # large λ; caught by the λ→∞⇒prior unit test).
    for _ in range(iters):
        z = X @ w + b
        p = 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))
        g = (y - p) * sw                       # ∂loglik/∂z
        step = lr / max(n, 1)
        w = w + step * (X.T @ g)               # forward: likelihood
        w = w_prior + (w - w_prior) / (1.0 + 2.0 * lam * step)  # backward: prox
        b += step * g.sum()
    return w, b


def fit_report(rows: list[dict], lam: float) -> dict:
    prior = prior_weights()
    feats = sorted(prior)
    X = np.array([[float((r["components"] or {}).get(f) or 0.0) for f in feats]
                  for r in rows])
    y = np.array([r["label"] for r in rows], dtype=float)
    sw = np.array([r["sample_weight"] for r in rows], dtype=float)
    w_prior = np.array([prior[f] for f in feats])
    w, b = fit_map_logistic(X, y, w_prior, sw, lam=lam)
    # normalize fitted weights to the prior's scale for apples-to-apples view
    scale = w_prior.sum() / max(w.sum(), 1e-9) if w.sum() > 0 else 1.0
    return {
        "generated": date.today().isoformat(),
        "n_cases": len(rows),
        "n_pos": int(y.sum()), "n_neg": int((1 - y).sum()),
        "by_source": {s: sum(1 for r in rows if r["source"] == s)
                      for s in {r["source"] for r in rows}},
        "lambda": lam,
        "bias": round(float(b), 4),
        "weights": {f: {"prior": round(float(wp), 4),
                        "fitted": round(float(wf), 4),
                        "fitted_normalized": round(float(wf * scale), 4),
                        "delta": round(float(wf - wp), 4)}
                    for f, wp, wf in zip(feats, w_prior, w)},
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--report", action="store_true", required=True)
    ap.add_argument("--root", type=Path, default=REPO_ROOT)
    ap.add_argument("--lam", type=float, default=5.0,
                    help="prior-trust λ (higher = stickier hand weights)")
    ap.add_argument("--min-n", type=int, default=20)
    ap.add_argument("--min-per-class", type=int, default=5)
    args = ap.parse_args(argv)

    rows = collect_labeled_cases(args.root)
    rep = fit_report(rows, args.lam) if rows else {
        "generated": date.today().isoformat(), "n_cases": 0}
    print(json.dumps(rep, indent=2))

    n_pos = rep.get("n_pos", 0)
    n_neg = rep.get("n_neg", 0)
    if (rep["n_cases"] < args.min_n or n_pos < args.min_per_class
            or n_neg < args.min_per_class):
        print(f"\nREPORT_ONLY: n={rep['n_cases']} (pos={n_pos}, neg={n_neg}) "
              f"is below the recommendation floor (min-n={args.min_n}, "
              f"min-per-class={args.min_per_class}). Keep the hand weights; "
              f"the battery + hindsight loops grow this dataset over time.",
              file=sys.stderr)
        return 2
    print("\nRECOMMENDATION: review weights.fitted_normalized vs prior; adopt "
          "via a PR that updates scoring.WEIGHTS + re-runs the battery "
          "ratchet.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
