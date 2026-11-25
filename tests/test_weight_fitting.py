"""Loop 4+5 groundwork — MAP weight fitter + hard-negative miner."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.fit_scoring_weights import (
    collect_labeled_cases,
    fit_map_logistic,
    fit_report,
    main as fit_main,
)
from scripts.mine_hard_negatives import mine


# ── the shrinkage invariants (the "no vibes" guarantees) ─────────────────────

def _toy(n=200, seed=0):
    rng = np.random.default_rng(seed)
    X = rng.uniform(0, 1, size=(n, 2))
    # feature 0 is truly predictive; feature 1 is noise
    p = 1 / (1 + np.exp(-(4 * X[:, 0] - 2)))
    y = (rng.uniform(size=n) < p).astype(float)
    return X, y


def test_lambda_infinity_returns_prior():
    X, y = _toy()
    prior = np.array([0.1, 0.9])
    w, _ = fit_map_logistic(X, y, prior, lam=1e9)
    assert np.allclose(w, prior, atol=1e-3)


def test_data_moves_predictive_weight_up_noise_down():
    X, y = _toy()
    prior = np.array([0.1, 0.9])  # prior is WRONG on purpose
    w, _ = fit_map_logistic(X, y, prior, lam=0.5)
    assert w[0] > prior[0]   # truly-predictive feature gains weight
    assert w[1] < prior[1]   # noise feature loses weight


def test_sample_weight_halves_influence():
    X, y = _toy()
    prior = np.array([0.1, 0.9])
    w_full, _ = fit_map_logistic(X, y, prior, np.ones(len(y)), lam=0.5)
    w_half, _ = fit_map_logistic(X, y, prior, np.full(len(y), 0.5), lam=0.5)
    # half-weighted data moves weights LESS from the prior
    assert abs(w_half[0] - prior[0]) < abs(w_full[0] - prior[0])


# ── dataset assembly + min-n refusal ─────────────────────────────────────────

def _mk_case(root, cid, npi, verdict, comps, calibration=False, adv_kills=0):
    base = root / "fraud-investigator-v2" / "state"
    d = (base / "calibration" / cid) if calibration else (base / cid)
    d.mkdir(parents=True)
    adv = [{"legal_adv_response": {"attack_dispositions":
            [{"disposition": "DISPOSITIVE_NOT_SURVIVED"}] * adv_kills}}] if adv_kills else []
    (d / "summary.json").write_text(json.dumps({
        "case_id": cid, "subject": f"Dr T NPI {npi}", "status": "complete",
        "final_verdict": verdict, "expected_value_factor": 0.1,
        "strength_components": comps, "adversarial_rounds": adv}))


def _mk_root(tmp_path):
    (tmp_path / "data" / "extracted").mkdir(parents=True)
    (tmp_path / "data" / "extracted" / "gold_set.jsonl").write_text(
        json.dumps({"npi": "1000000001"}) + "\n")
    (tmp_path / "validation" / "battery").mkdir(parents=True)
    (tmp_path / "validation" / "battery" / "battery.json").write_text(json.dumps(
        {"subjects": [{"name": "C", "npi": "1000000002",
                       "kind": "negative_control", "as_of": "2023-01-01"}]}))
    comps = {"factual_density": 0.8, "n_counts": 1.0, "regulatory": 0.0,
             "venue_precedent": 1.0, "same_district": 1.0,
             "within_practice": 1.0, "survival_residual": 0.5,
             "damages": 0.7, "medicaid_cross_program": 0.0,
             "discount_tax": -0.1}
    _mk_case(tmp_path, "CALIBRATION-T1", "1000000001", "SETTLEMENT_READY",
             comps, calibration=True)
    _mk_case(tmp_path, "CALIBRATION-C1", "1000000002", "NOT_ACTIONABLE",
             {**comps, "within_practice": 0.0}, calibration=True)
    # merits-killed unlabeled provider → hard negative
    _mk_case(tmp_path, "CASE-HN1", "1000000003", "NOT_ACTIONABLE",
             {**comps, "same_district": 0.0}, adv_kills=2)
    # EVF-economics decline (no kills) → NOT a hard negative
    _mk_case(tmp_path, "CASE-HN2", "1000000004", "INDEFENSIBLE_AT_CAP",
             comps, adv_kills=0)
    # gold_set NPI declined by us → oracle outranks, NOT a hard negative
    _mk_case(tmp_path, "CASE-HN3", "1000000001", "NOT_ACTIONABLE",
             comps, adv_kills=3)
    return tmp_path


def test_miner_rules(tmp_path):
    root = _mk_root(tmp_path)
    rows = mine(root)
    assert [r["npi"] for r in rows] == ["1000000003"]
    assert rows[0]["provisional"] is True
    assert rows[0]["n_dispositive_kills"] == 2


def test_collect_labels_and_sources(tmp_path):
    root = _mk_root(tmp_path)
    (root / "results").mkdir()
    (root / "results" / "hard_negatives.jsonl").write_text(
        json.dumps({"npi": "1000000003"}) + "\n"
        + json.dumps({"npi": "1000000001"}) + "\n")  # oracle conflict → dropped
    rows = collect_labeled_cases(root)
    by_npi = {r["npi"]: r for r in rows}
    assert by_npi["1000000001"]["label"] == 1            # gold_set wins
    assert by_npi["1000000001"]["source"] == "gold_set"
    assert by_npi["1000000002"]["label"] == 0
    assert by_npi["1000000003"]["sample_weight"] == 0.5  # provisional half-vote
    assert "1000000004" not in by_npi                    # unlabeled → excluded


def test_fit_report_shape_and_min_n_refusal(tmp_path, capsys):
    root = _mk_root(tmp_path)
    rows = collect_labeled_cases(root)
    rep = fit_report(rows, lam=5.0)
    assert rep["n_cases"] == 2 and rep["n_pos"] == 1
    assert "within_practice" in rep["weights"]
    assert "discount_tax" not in rep["weights"]  # leakage-excluded
    # CLI refuses to recommend below the floor
    rc = fit_main(["--report", "--root", str(root)])
    assert rc == 2
    assert "REPORT_ONLY" in capsys.readouterr().err
