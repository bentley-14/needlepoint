"""CI-safe tests for fraud_investigator.usage.case_cost.

Uses a synthetic tmp case dir with two *.raw.json step artifacts so the tests
are fully self-contained and never touch real state dirs.
"""
from __future__ import annotations

import json
import pytest
from pathlib import Path

from fraud_investigator.usage import case_cost


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_raw(path: Path, cost_usd: float, input_tokens: int, output_tokens: int,
               model: str = "claude-opus-4-8") -> None:
    """Write a minimal *.raw.json artifact to *path*."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "type": "result",
        "total_cost_usd": cost_usd,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        },
        "modelUsage": {
            model: {
                "inputTokens": input_tokens,
                "outputTokens": output_tokens,
                "cacheReadInputTokens": 0,
                "cacheCreationInputTokens": 0,
                "costUSD": cost_usd,
            }
        },
        "duration_ms": 5000,
        "num_turns": 3,
    }))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_grand_total(tmp_path):
    """Two steps in two phases sum correctly."""
    _write_raw(tmp_path / "iter-01" / "investigator.raw.json",
               cost_usd=2.50, input_tokens=1000, output_tokens=5000)
    _write_raw(tmp_path / "adv-r1" / "defense.raw.json",
               cost_usd=1.30, input_tokens=500, output_tokens=3000)

    result = case_cost(tmp_path)

    assert result["usd"] == pytest.approx(3.80, abs=1e-9)
    assert result["input_tokens"] == 1500
    assert result["output_tokens"] == 8000


def test_by_stage_breakdown(tmp_path):
    """by_stage keys match phase dir names."""
    _write_raw(tmp_path / "iter-01" / "investigator.raw.json",
               cost_usd=2.00, input_tokens=800, output_tokens=4000)
    _write_raw(tmp_path / "iter-01" / "counsel_build.raw.json",
               cost_usd=1.50, input_tokens=300, output_tokens=2500)
    _write_raw(tmp_path / "adv-r1" / "defense.raw.json",
               cost_usd=1.00, input_tokens=200, output_tokens=1500)

    result = case_cost(tmp_path)

    assert "iter-01" in result["by_stage"]
    assert "adv-r1" in result["by_stage"]
    assert result["by_stage"]["iter-01"]["usd"] == pytest.approx(3.50, abs=1e-9)
    assert result["by_stage"]["iter-01"]["input_tokens"] == 1100
    assert result["by_stage"]["adv-r1"]["usd"] == pytest.approx(1.00, abs=1e-9)


def test_empty_case_dir(tmp_path):
    """Empty dir returns None usd without crashing."""
    result = case_cost(tmp_path)

    assert result["usd"] is None
    assert result["input_tokens"] == 0
    assert result["output_tokens"] == 0
    assert result["by_stage"] == {}


def test_no_raw_json_in_dir(tmp_path):
    """Dir with only non-raw.json files -> None usd."""
    (tmp_path / "iter-01").mkdir()
    (tmp_path / "iter-01" / "notes.txt").write_text("not a raw file")

    result = case_cost(tmp_path)
    assert result["usd"] is None


def test_corrupt_raw_json_skipped(tmp_path):
    """A corrupt raw.json is silently skipped; valid steps still count."""
    _write_raw(tmp_path / "iter-01" / "good.raw.json",
               cost_usd=5.00, input_tokens=2000, output_tokens=8000)
    # Write a corrupt JSON file
    (tmp_path / "iter-01" / "bad.raw.json").write_text("{broken json:")

    result = case_cost(tmp_path)
    assert result["usd"] == pytest.approx(5.00, abs=1e-9)
    assert result["input_tokens"] == 2000


def test_zero_cost_raw_json(tmp_path):
    """A step with total_cost_usd=null/0 doesn't produce None usd."""
    _write_raw(tmp_path / "iter-01" / "step.raw.json",
               cost_usd=0.0, input_tokens=100, output_tokens=200)

    result = case_cost(tmp_path)
    assert result["usd"] == pytest.approx(0.0, abs=1e-9)
    # usd=0.0 is known (not unknown), so it should not be None
    assert result["usd"] is not None


def test_missing_total_cost_field(tmp_path):
    """raw.json without total_cost_usd field defaults to 0 (not None)."""
    raw = {
        "usage": {"input_tokens": 500, "output_tokens": 1000,
                  "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
        "modelUsage": {},
        "duration_ms": 1000,
        "num_turns": 1,
    }
    (tmp_path / "iter-01").mkdir()
    (tmp_path / "iter-01" / "step.raw.json").write_text(json.dumps(raw))

    result = case_cost(tmp_path)
    assert result["usd"] == pytest.approx(0.0, abs=1e-9)
    assert result["input_tokens"] == 500
    assert result["output_tokens"] == 1000


def test_raw_jsonl_final_result_line_parsed(tmp_path):
    """Streaming-path artifacts (*.raw.jsonl) carry cost in the LAST
    type:"result" line — opus #283 finding 1."""
    d = tmp_path / "iter-01"
    d.mkdir()
    lines = [
        json.dumps({"type": "system", "subtype": "init"}),
        json.dumps({"type": "assistant", "message": {}}),
        json.dumps({"type": "result", "total_cost_usd": 2.692,
                    "usage": {"input_tokens": 2517, "output_tokens": 65923}}),
    ]
    (d / "defense.raw.jsonl").write_text("\n".join(lines))
    out = case_cost(tmp_path)
    assert out["usd"] == pytest.approx(2.692)
    assert out["input_tokens"] == 2517
    assert out["output_tokens"] == 65923


def test_attempt_jsonl_files_never_counted(tmp_path):
    """*.raw.attempt*.jsonl duplicate (or exceed!) the winning attempt's
    cost — a real adv-r2 had a FAILED attempt at $5.73 vs final $0.57.
    Only the final <step>.raw.jsonl counts — opus #283 finding 2."""
    d = tmp_path / "adv-r2"
    d.mkdir()
    final = json.dumps({"type": "result", "total_cost_usd": 0.57,
                        "usage": {"input_tokens": 10, "output_tokens": 20}})
    failed = json.dumps({"type": "result", "total_cost_usd": 5.73,
                         "usage": {"input_tokens": 99, "output_tokens": 99}})
    (d / "legal_adv_response.raw.jsonl").write_text(final)
    (d / "legal_adv_response.raw.attempt1.jsonl").write_text(failed)
    (d / "legal_adv_response.raw.attempt2.jsonl").write_text(final)
    out = case_cost(tmp_path)
    assert out["usd"] == pytest.approx(0.57)   # NOT 0.57+5.73+0.57
    assert out["input_tokens"] == 10


def test_jsonl_without_result_line_tolerated(tmp_path):
    d = tmp_path / "iter-01"
    d.mkdir()
    (d / "step.raw.jsonl").write_text(json.dumps({"type": "assistant"}))
    out = case_cost(tmp_path)
    assert out["usd"] is None
