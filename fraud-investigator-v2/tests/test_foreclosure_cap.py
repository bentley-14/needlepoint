"""Guardrail 3 — the deterministic foreclosure cap.

Pins that scoring.foreclosure_residual reads the LEDGER ROWS (not the LLM's
self-reported max_live_plausibility), so a model that under-states its own
residual cannot dodge the strength cap. Hermetic — pure functions, no data.

Run: pytest fraud-investigator-v2/tests/test_foreclosure_cap.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fraud_investigator import scoring  # noqa: E402


def test_residual_is_max_live_plausibility():
    ledger = {"explanations": [
        {"id": "a", "status": "LIVE", "plausibility_unrebutted": 0.6},
        {"id": "b", "status": "LIVE", "plausibility_unrebutted": 0.3},
        {"id": "c", "status": "FORECLOSED", "plausibility_unrebutted": 0.9},
    ]}
    assert scoring.foreclosure_residual(ledger) == 0.6   # ignores the FORECLOSED 0.9


def test_no_live_rows_is_zero():
    ledger = {"explanations": [
        {"id": "a", "status": "FORECLOSED", "plausibility_unrebutted": 0.9},
        {"id": "b", "status": "PARTIAL", "plausibility_unrebutted": 0.4},
    ]}
    assert scoring.foreclosure_residual(ledger) == 0.0


def test_empty_and_none_ledger():
    assert scoring.foreclosure_residual(None) == 0.0
    assert scoring.foreclosure_residual({}) == 0.0
    assert scoring.foreclosure_residual({"explanations": []}) == 0.0


def test_ignores_llm_self_report_field():
    # The model claims 0.1, but a LIVE row is actually 0.7 — the row wins.
    ledger = {"max_live_plausibility": 0.1,
              "explanations": [{"id": "a", "status": "LIVE", "plausibility_unrebutted": 0.7}]}
    assert scoring.foreclosure_residual(ledger) == 0.7


def test_cap_threshold_triggers_at_coin_flip():
    # A coin-flip (0.5) innocent explanation is enough to block a clean FILE.
    assert scoring.FORECLOSURE_LIVE_CAP == 0.5
    below = {"explanations": [{"status": "LIVE", "plausibility_unrebutted": 0.49}]}
    at = {"explanations": [{"status": "LIVE", "plausibility_unrebutted": 0.5}]}
    assert scoring.foreclosure_residual(below) < scoring.FORECLOSURE_LIVE_CAP
    assert scoring.foreclosure_residual(at) >= scoring.FORECLOSURE_LIVE_CAP
