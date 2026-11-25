"""Tests for scripts/queue_ev_rank.py — expected-value queue ordering (M5).

Pure-local: synthetic Part B panel parquet + synthetic TriageOutput JSON in
tmp_path; round-trip validity is checked against the real TriageOutput schema
(PYTHONPATH includes fraud-investigator-v2/src in CI).

Run: PYTHONPATH=.:fraud-investigator-v2/src pytest tests/test_queue_ev_rank.py -v
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pandas as pd
import pytest

from fraud_investigator.pipeline_schemas import TriageOutput

REPO = Path(__file__).resolve().parents[1]

_spec = importlib.util.spec_from_file_location(
    "queue_ev_rank", REPO / "scripts" / "queue_ev_rank.py")
qer = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(qer)


def _item(npi, rank, verdict, confidence):
    return {
        "npi": npi,
        "rank": rank,
        "verdict": verdict,
        "confidence": confidence,
        "matched_archetype": "BILLING_FRAUD" if verdict == "HIGH_RISK" else None,
        "signals_detected": (["peer_deviation", "billing_intensity"]
                             if verdict in ("HIGH_RISK", "MODERATE_RISK") else []),
        "reasoning": ("Synthetic test record: pay_per_bene_pct=0.99 and "
                      "opioid_clm_share=0.32 are both extreme for this specialty; "
                      "fabricated purely so the schema's 100-char floor is met."),
        "specialty": "Family Practice",
        "follow_up_questions": [],
    }


@pytest.fixture()
def queue_path(tmp_path) -> Path:
    queue = {
        "score_year": 2023,
        "k_range": [1, 5],
        "items": [
            _item("1000000001", 1, "HIGH_RISK", 0.9),       # 1,000 × 0.9 × 1.0   = 900
            _item("1000000002", 2, "MODERATE_RISK", 0.5),   # 10,000 × 0.5 × 0.4  = 2,000
            _item("1000000003", 3, "HIGH_RISK", 0.9),       # absent from panel    = 0
            _item("1000000004", 4, "LOW_RISK", 1.0),        # 100,000 × 1.0 × 0.05 = 5,000
            _item("1000000005", 5, "DISMISS", 1.0),         # weight 0             = 0
        ],
        "batch_summary": "Synthetic queue for queue_ev_rank tests — five providers, mixed verdicts.",
    }
    p = tmp_path / "queue.json"
    p.write_text(json.dumps(queue))
    return p


@pytest.fixture()
def panel_path(tmp_path) -> Path:
    df = pd.DataFrame({
        "Rndrng_NPI": ["1000000001", "1000000002", "1000000004", "1000000005"],
        "Tot_Mdcr_Pymt_Amt": [1_000.0, 10_000.0, 100_000.0, 1_000_000_000.0],
    })
    p = tmp_path / "cms_partb_2023.parquet"
    df.to_parquet(p)
    return p


def _run(queue_path, panel_path, tmp_path) -> Path:
    out = tmp_path / "ev_queue.json"
    rc = qer.main(["--queue", str(queue_path), "--year", "2023",
                   "--panel", str(panel_path), "--out", str(out)])
    assert rc == 0
    return out


def test_ev_ordering_and_verdict_weights(queue_path, panel_path, tmp_path):
    out = json.loads(_run(queue_path, panel_path, tmp_path).read_text())
    # EV desc: LOW w/ huge payment (5,000) > MODERATE (2,000) > HIGH (900);
    # the two zero-EV items (panel-absent HIGH, DISMISS) tie-break by ML rank.
    assert [it["npi"] for it in out["items"]] == [
        "1000000004", "1000000002", "1000000001", "1000000003", "1000000005"]


def test_ev_values_use_verdict_weights(queue_path, panel_path):
    queue = json.loads(queue_path.read_text())
    _, scored = qer.ev_rank(queue, str(panel_path), 2023)
    ev = {s["npi"]: s["ev"] for s in scored}
    assert ev["1000000001"] == pytest.approx(1_000 * 0.9 * 1.0)
    assert ev["1000000002"] == pytest.approx(10_000 * 0.5 * 0.4)
    assert ev["1000000003"] == 0.0    # absent from panel → payment 0
    assert ev["1000000004"] == pytest.approx(100_000 * 1.0 * 0.05)
    assert ev["1000000005"] == 0.0    # DISMISS weight 0 despite $1B payment


def test_rank_field_kept_verbatim_and_summary_prefixed(queue_path, panel_path, tmp_path):
    out = json.loads(_run(queue_path, panel_path, tmp_path).read_text())
    rank_by_npi = {it["npi"]: it["rank"] for it in out["items"]}
    assert rank_by_npi == {"1000000001": 1, "1000000002": 2, "1000000003": 3,
                           "1000000004": 4, "1000000005": 5}
    assert out["batch_summary"].startswith("[ev-ranked] ")
    # The original summary is preserved after the method note.
    assert out["batch_summary"].endswith("mixed verdicts.")


def test_output_round_trips_triage_output_schema(queue_path, panel_path, tmp_path):
    out_path = _run(queue_path, panel_path, tmp_path)
    parsed = TriageOutput.model_validate_json(out_path.read_text())
    assert [it.npi for it in parsed.items] == [
        "1000000004", "1000000002", "1000000001", "1000000003", "1000000005"]
    assert parsed.score_year == 2023
