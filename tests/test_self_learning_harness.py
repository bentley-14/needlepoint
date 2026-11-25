"""Self-learning harness — hindsight replay + calibration battery.

All synthetic artifacts in tmp_path (no real data, no network): these tests
pin the funnel-ladder classification, the miss-routing, the battery metrics,
and the ratchet's regression detection.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.calibration_battery import (
    compute_metrics,
    outcome_class,
    ratchet_check,
)
from scripts.hindsight_replay import classify, replay

# ── synthetic repo builder ───────────────────────────────────────────────────

NPI_FILED = "1000000001"      # investigated → SETTLEMENT_READY
NPI_DECLINED = "1000000002"   # investigated → NOT_ACTIONABLE
NPI_PARKED = "1000000003"     # parked registry
NPI_TRIAGED_HI = "1000000004"
NPI_DISMISSED = "1000000005"  # triage DISMISS — rubric miss
NPI_RANKED = "1000000006"     # rank 500, never triaged
NPI_PANEL_MISS = "1000000007"  # in scored panel at rank 90000
NPI_ABSENT = "1000000008"     # nowhere


def _mk_repo(tmp: Path) -> Path:
    # gold_set
    gs = tmp / "data" / "extracted"
    gs.mkdir(parents=True)
    rows = []
    for npi, od in [(NPI_FILED, "2026-01-05"), (NPI_DECLINED, "2026-02-01"),
                    (NPI_PARKED, "2026-03-01"), (NPI_TRIAGED_HI, "2026-04-01"),
                    (NPI_DISMISSED, "2025-01-01"), (NPI_RANKED, "2025-02-01"),
                    (NPI_PANEL_MISS, "2025-03-01"), (NPI_ABSENT, "2025-04-01")]:
        rows.append({"npi": npi, "all_active_years": [2023],
                     "defendant_name": f"Dr {npi}", "defendant_specialty": "IM",
                     "fraud_types": ["upcoding"],
                     "outcome": {"type": "settlement", "date": od}})
    (gs / "gold_set.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows))

    # scored panel (2023): ranks by score desc
    sp = tmp / "results" / "walk_forward_v4_top5_test"
    sp.mkdir(parents=True)
    panel = [{"npi": NPI_TRIAGED_HI, "score_rrf": 0.9},
             {"npi": NPI_DISMISSED, "score_rrf": 0.8},
             {"npi": NPI_RANKED, "score_rrf": 0.7},
             {"npi": NPI_FILED, "score_rrf": 0.6}]
    # pad so NPI_PANEL_MISS lands beyond rank 2000
    panel += [{"npi": str(2000000000 + i), "score_rrf": 0.5 - i * 1e-7}
              for i in range(2100)]
    panel.append({"npi": NPI_PANEL_MISS, "score_rrf": 0.0001})
    pd.DataFrame(panel).to_parquet(sp / "scored_full_2023.parquet")

    # triage queue
    tq = tmp / "validation" / "triage"
    tq.mkdir(parents=True)
    (tq / "2023_top1-500.json").write_text(json.dumps({
        "score_year": 2023, "k_range": [1, 4],
        "items": [
            {"npi": NPI_TRIAGED_HI, "verdict": "HIGH_RISK", "confidence": 0.9},
            {"npi": NPI_DISMISSED, "verdict": "DISMISS", "confidence": 0.8},
            {"npi": NPI_FILED, "verdict": "HIGH_RISK", "confidence": 0.95},
            {"npi": NPI_DECLINED, "verdict": "HIGH_RISK", "confidence": 0.9},
        ],
        "batch_summary": "synthetic"}))

    # case states (production)
    for cid, npi, verdict, evf in [
            ("CASE-AAA", NPI_FILED, "SETTLEMENT_READY", 0.5),
            ("CASE-BBB", NPI_DECLINED, "NOT_ACTIONABLE", 0.0)]:
        d = tmp / "fraud-investigator-v2" / "state" / cid
        d.mkdir(parents=True)
        (d / "summary.json").write_text(json.dumps({
            "case_id": cid, "subject": f"Dr X NPI {npi} test",
            "status": "complete", "final_verdict": verdict,
            "expected_value_factor": evf}))

    # parked registry
    pk = tmp / "fraud-investigator-v2" / "state" / "parked"
    pk.mkdir(parents=True)
    (pk / "CASE-PPP.json").write_text(json.dumps(
        {"case_id": "CASE-PPP", "npi": NPI_PARKED,
         "watch_npis": [NPI_PARKED]}))
    return tmp


# ── hindsight replay ─────────────────────────────────────────────────────────

def test_funnel_ladder_classification(tmp_path):
    root = _mk_repo(tmp_path)
    report = replay(root)
    by_npi = {r["npi"]: r["classification"] for r in report["results"]}
    assert by_npi[NPI_FILED] == "INVESTIGATED_FILED"
    assert by_npi[NPI_DECLINED] == "INVESTIGATED_DECLINED"
    assert by_npi[NPI_PARKED] == "PARKED"
    assert by_npi[NPI_TRIAGED_HI] == "TRIAGED_HIGH"
    assert by_npi[NPI_DISMISSED] == "TRIAGED_LOW_OR_DISMISS"
    assert by_npi[NPI_RANKED] == "RANKED_TOP2K"
    assert by_npi[NPI_PANEL_MISS] == "IN_PANEL_MISSED"
    assert by_npi[NPI_ABSENT] == "NOT_IN_PANEL"
    # ladder precedence: investigation beats triage beats rank
    assert report["funnel"]["INVESTIGATED_FILED"] == 1
    assert report["funnel"]["TRIAGED_HIGH"] == 1  # NPI_FILED counted once, higher rung


def test_miss_routing_and_ranks(tmp_path):
    root = _mk_repo(tmp_path)
    report = replay(root)
    by_npi = {r["npi"]: r for r in report["results"]}
    assert "feature_engineer" in by_npi[NPI_PANEL_MISS]["followup"]
    assert "triage rubric" in by_npi[NPI_DISMISSED]["followup"]
    assert "floor analysis" in by_npi[NPI_DECLINED]["followup"]
    assert by_npi[NPI_RANKED]["best_rank"] == 3
    assert by_npi[NPI_PANEL_MISS]["best_rank"] > 2000


def test_since_filter(tmp_path):
    root = _mk_repo(tmp_path)
    report = replay(root, since="2026-01-01")
    assert report["n_npis"] == 4  # only the 2026 outcomes


def test_classify_is_pure_and_handles_empty_indexes():
    rec = classify("1234567893", [2020], ranks={}, triage={}, cases={},
                   parked={}, billing_years=None)
    assert rec["classification"] == "NOT_IN_PANEL"
    assert rec["best_rank"] is None


# ── battery ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _no_stale_gate(monkeypatch):
    """These fixtures' runs are dated 2026-01; disable the production staleness gate
    (CAL_STALE_BEFORE) so metrics are computed over them rather than excluded."""
    import scripts.calibration_battery as cb
    monkeypatch.setattr(cb, "CAL_STALE_BEFORE", "")


def _manifest():
    return {"subjects": [
        {"name": "TP One", "npi": "1000000001", "kind": "true_positive", "as_of": "2022-01-01"},
        {"name": "TP Two", "npi": "1000000002", "kind": "true_positive", "as_of": "2022-01-01"},
        {"name": "Ctl One", "npi": "1000000003", "kind": "negative_control", "as_of": "2022-01-01"},
        {"name": "Ctl Two", "npi": "1000000004", "kind": "negative_control", "as_of": "2022-01-01"},
    ]}


def _runs(ctl_two_verdict="NOT_ACTIONABLE"):
    return {
        "1000000001": [{"case_id": "C1", "started": "2026-01-01",
                        "verdict": "SETTLEMENT_READY", "evf": 0.4,
                        "n_dispositive_defeats": 0}],
        "1000000002": [
            {"case_id": "C2a", "started": "2026-01-01",
             "verdict": "NOT_ACTIONABLE", "evf": 0.0, "n_dispositive_defeats": 0},
            {"case_id": "C2b", "started": "2026-02-01",   # latest wins
             "verdict": "FILE_WITH_DISCOVERY_DEPS", "evf": 0.3,
             "n_dispositive_defeats": 1}],
        "1000000003": [{"case_id": "C3", "started": "2026-01-01",
                        "verdict": "INDEFENSIBLE_AT_CAP", "evf": 0.1,
                        "n_dispositive_defeats": 0}],
        "1000000004": [{"case_id": "C4", "started": "2026-01-01",
                        "verdict": ctl_two_verdict, "evf": 0.05,
                        "n_dispositive_defeats": 2}],
    }


def test_outcome_classes():
    assert outcome_class("SETTLEMENT_READY") == "FILE"
    assert outcome_class("FILE_WITH_DISCOVERY_DEPS") == "FILE"
    assert outcome_class("INDEFENSIBLE_AT_CAP") == "DECLINE"
    assert outcome_class("NEEDS_HUMAN") == "INCONCLUSIVE"
    assert outcome_class(None) == "INCONCLUSIVE"


def test_metrics_clean_battery():
    m = compute_metrics(_manifest(), _runs())
    assert m["tp_file_rate"] == 1.0          # latest run wins for TP Two
    assert m["control_file_rate"] == 0.0
    assert abs(m["evf_separation"] - ((0.4 + 0.3) / 2 - (0.1 + 0.05) / 2)) < 1e-9
    assert m["control_dispositive_defeat_rate"] == 0.5  # only Ctl Two has kills
    assert all(r["pass"] for r in m["subjects"])


def test_metrics_control_files_is_failure():
    m = compute_metrics(_manifest(), _runs(ctl_two_verdict="SETTLEMENT_READY"))
    assert m["control_file_rate"] == 0.5
    failing = [r for r in m["subjects"] if not r["pass"]]
    assert [r["npi"] for r in failing] == ["1000000004"]


def test_metrics_no_run_subject():
    manifest = _manifest()
    manifest["subjects"].append({"name": "New", "npi": "1999999999",
                                 "kind": "true_positive", "as_of": None})
    m = compute_metrics(manifest, _runs())
    assert m["n_no_run"] == 1
    # rates computed over scored subjects only
    assert m["tp_file_rate"] == 1.0


def test_ratchet_detects_each_regression():
    base = {"tp_file_rate": 1.0, "control_file_rate": 0.0,
            "evf_separation": 0.3, "control_dispositive_defeat_rate": 1.0}
    assert ratchet_check(dict(base), base) == []
    assert ratchet_check({**base, "tp_file_rate": 0.9}, base)
    assert ratchet_check({**base, "control_file_rate": 0.25}, base)
    assert ratchet_check({**base, "evf_separation": 0.1}, base)
    assert ratchet_check({**base, "control_dispositive_defeat_rate": 0.5}, base)
    # within tolerance: evf may shrink by ≤0.02
    assert ratchet_check({**base, "evf_separation": 0.285}, base) == []
    # None on either side is skipped, not a crash
    assert ratchet_check({**base, "evf_separation": None}, base) == []
