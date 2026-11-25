"""Cheap-replay loop (#6) — hermetic verdict replay over frozen summaries.

Unlike test_verdict_replay.py (which SKIPS without live calibration state dirs),
these build synthetic summary.json snapshots inline, so the replay mechanism is
exercised in CI with no live runs and no network. Plus a guard that replays any
REAL harvested fixtures committed under validation/replay/fixtures/.
"""
from __future__ import annotations

import glob
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "scripts"))

from replay_battery import (  # noqa: E402
    harvest, replay_fixtures, verdict_class, expected_for_kind, FIXTURES,
)
from verdict_replay import replay_summary, is_file  # noqa: E402


def _summary(*, fraud_more_plausible: bool, case_id="SYNTH", damages=1_000_000.0,
             anchor=True, evf=0.9):
    """A minimal summary.json that build_scorecard reads. fraud_more_plausible
    True + an anchor + damages → FILE; False → DECLINE (innocent_foreclosed fails)."""
    return {
        "case_id": case_id,
        "status": "complete",
        "subject": f"Synthetic Subject ({case_id})",
        "iterations": [{
            "phase": "rebuttal_closing",
            "counsel_build": {
                "counts_pled": [{"count_id": "C1", "damages_floor_usd": damages,
                                 "ceiling_requires": None}],
                "factual_basis_per_count": [{
                    "count_id": "C1",
                    "falsity_anchor_evidence_ids": ["E1"] if anchor else [],
                }],
            },
        }],
        "counts_retained": ["C1"],
        "adversarial_rounds": [],
        "foreclosure_ledger": {"fraud_is_more_plausible_inference": fraud_more_plausible},
        "expected_value_factor": evf,
    }


# ── the replay mechanism, hermetic ──────────────────────────────────────────

def test_synthetic_file_summary_replays_to_file():
    verdict, rationale = replay_summary(_summary(fraud_more_plausible=True))
    assert is_file(verdict)
    assert verdict_class(verdict) == "FILE"
    assert all(p["status"] != "FAILED" for p in rationale["predicates"])


def test_synthetic_decline_summary_replays_to_decline():
    verdict, rationale = replay_summary(_summary(fraud_more_plausible=False))
    assert not is_file(verdict)
    assert verdict_class(verdict) == "DECLINE"
    failed = [p["name"] for p in rationale["predicates"] if p["status"] == "FAILED"]
    assert "innocent_foreclosed" in failed


def test_no_falsity_anchor_declines():
    verdict, rationale = replay_summary(
        _summary(fraud_more_plausible=True, anchor=False))
    assert verdict_class(verdict) == "DECLINE"
    failed = [p["name"] for p in rationale["predicates"] if p["status"] == "FAILED"]
    assert "confirmed_false_claim" in failed


def test_expected_for_kind():
    assert expected_for_kind("true_positive") == "FILE"
    assert expected_for_kind("negative_control") == "DECLINE"
    assert expected_for_kind("adjudicated_true_negative") == "DECLINE"


# ── harvest → replay round-trip (the regression-capture path) ───────────────

def test_harvest_then_replay_roundtrip(tmp_path):
    # write a synthetic completed run, harvest it, replay the fixture
    state_dir = tmp_path / "CALIBRATION-XYZ"
    state_dir.mkdir()
    (state_dir / "summary.json").write_text(json.dumps(_summary(fraud_more_plausible=True)))
    fixtures = tmp_path / "fixtures"

    p = harvest(state_dir, "Test Subject", "FILE", npi="1234567893",
                kind="true_positive", fixtures_dir=fixtures)
    assert p.exists()
    fx = json.loads(p.read_text())
    assert fx["expected"] == "FILE" and fx["subject"] == "Test Subject"
    assert "summary" in fx  # the evidence is frozen into the fixture

    rows = replay_fixtures(fixtures)
    assert len(rows) == 1
    assert rows[0]["pass"] and rows[0]["got"] == "FILE"


def test_harvest_captures_a_diverging_case(tmp_path):
    # freeze a DECLINE summary but label it FILE → replay must flag the divergence
    state_dir = tmp_path / "CALIBRATION-DIV"
    state_dir.mkdir()
    (state_dir / "summary.json").write_text(json.dumps(_summary(fraud_more_plausible=False)))
    fixtures = tmp_path / "fixtures"
    harvest(state_dir, "Mislabeled", "FILE", fixtures_dir=fixtures)
    rows = replay_fixtures(fixtures)
    assert rows[0]["pass"] is False  # got DECLINE, expected FILE → caught


# ── guard any committed real fixtures (no-op until harvested) ────────────────

def test_committed_fixtures_still_match_expected():
    real = [f for f in glob.glob(str(FIXTURES / "*.json"))]
    if not real:
        import pytest
        pytest.skip("no harvested fixtures committed yet")
    rows = replay_fixtures(FIXTURES)
    diverged = [r for r in rows if not r["pass"]]
    assert not diverged, (
        "committed replay fixtures now decide differently under current logic: "
        + "; ".join(f"{r['subject']} expected {r['expected']} got {r['got']}"
                    for r in diverged))
