"""Calibration-battery metrics — the adjudicated-true-negative wiring (3b).

A new ``kind: "adjudicated_true_negative"`` subject must get its OWN precision
metric (adjudicated_tn_file_rate), kept distinct from control_file_rate (the
anomalous-control rate, which stays negative_control-only so the committed
baseline stays comparable). Pins that a FILE on an adjudicated TN — a real false
positive — shows up where it should and not where it shouldn't.
"""
from __future__ import annotations

from scripts.calibration_battery import compute_metrics, ratchet_check


def _run(verdict, evf=0.3, disp=0, started="2026-06-18T00:00:00"):
    return {"verdict": verdict, "evf": evf, "n_dispositive_defeats": disp,
            "started": started, "case_id": "C", "status": "complete"}


def _subj(npi, kind, name=None):
    return {"npi": npi, "kind": kind, "name": name or f"S{npi}", "as_of": "2020-01-01"}


def _manifest(*subjects):
    return {"subjects": list(subjects)}


def test_adjudicated_tn_file_rate_separate_from_control():
    manifest = _manifest(
        _subj("1000000001", "true_positive"),
        _subj("1000000002", "negative_control"),
        _subj("1000000003", "adjudicated_true_negative"),
        _subj("1000000004", "adjudicated_true_negative"),
    )
    runs = {
        "1000000001": [_run("FILE")],            # TP files (good)
        "1000000002": [_run("NOT_ACTIONABLE")],  # anomalous control declines (good)
        "1000000003": [_run("NOT_ACTIONABLE")],  # adjudicated TN declines (good)
        "1000000004": [_run("FILE")],            # adjudicated TN FILES → false positive
    }
    m = compute_metrics(manifest, runs)
    assert m["tp_file_rate"] == 1.0
    assert m["control_file_rate"] == 0.0           # negative_control only — unaffected
    assert m["adjudicated_tn_file_rate"] == 0.5    # 1 of 2 adjudicated TNs filed
    assert m["n_adjudicated_tn"] == 2


def test_adjudicated_tn_expected_decline_and_pass():
    manifest = _manifest(_subj("1000000003", "adjudicated_true_negative"))
    m = compute_metrics(manifest, {"1000000003": [_run("NOT_ACTIONABLE")]})
    row = m["subjects"][0]
    assert row["expected"] == "DECLINE"            # not a true_positive → expect DECLINE
    assert row["pass"] is True


def test_adjudicated_tn_absent_is_none():
    manifest = _manifest(_subj("1000000001", "true_positive"),
                         _subj("1000000002", "negative_control"))
    m = compute_metrics(manifest, {"1000000001": [_run("FILE")],
                                   "1000000002": [_run("FILE")]})
    assert m["adjudicated_tn_file_rate"] is None   # no adjudicated subjects
    assert m["n_adjudicated_tn"] == 0
    assert m["control_file_rate"] == 1.0           # control filed (bad, but counted)


def test_adjudicated_tn_dispositive_defeat_rate():
    manifest = _manifest(_subj("1000000003", "adjudicated_true_negative"),
                         _subj("1000000004", "adjudicated_true_negative"))
    runs = {"1000000003": [_run("NOT_ACTIONABLE", disp=2)],  # declined on a dispositive defeat
            "1000000004": [_run("NOT_ACTIONABLE", disp=0)]}  # declined without one
    m = compute_metrics(manifest, runs)
    assert m["adjudicated_tn_dispositive_defeat_rate"] == 0.5


def test_ratchet_flags_adjudicated_tn_file_rate_rise():
    probs = ratchet_check({"adjudicated_tn_file_rate": 0.25},
                          {"adjudicated_tn_file_rate": 0.0})
    assert any("adjudicated_tn_file_rate" in p for p in probs)


def test_ratchet_flags_theory_match_drop():
    probs = ratchet_check({"theory_match_rate": 0.40},
                          {"theory_match_rate": 0.80})
    assert any("theory_match_rate" in p for p in probs)
