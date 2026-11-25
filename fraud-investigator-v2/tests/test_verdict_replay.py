"""Golden verdict-replay regression — the oracle that gates the refactor.

Re-applies the current verdict ladder to the SAVED calibration summaries (no
re-run) and asserts the expected outcome per case. This is the fast regression
gate: as the verdict logic is refactored (consolidated into compute_verdict,
then the predicate test, then Gap-2), these assertions catch any unintended
drift in milliseconds.

Baseline (current code, with Gap-1 foreclosure-DECLINE already in the tree):
  - Aspen (TP)  → FILE-class   (recall)
  - Willow   (TP)  → FILE-class   (recall)
  - Baobab    (ctrl)→ NOT_ACTIONABLE (Gap-1: fraud NOT more-plausible)
  - Laurel     (ctrl)→ FILE-class   (still files; Gap-2/Phase-3 will flip it)

If the live calibration state dirs aren't present, the case is skipped (this is
a local oracle over real runs, not a hermetic unit test).

Run: pytest fraud-investigator-v2/tests/test_verdict_replay.py -v
"""
from __future__ import annotations

import glob
import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "scripts"))

from verdict_replay import replay_verdict, is_file  # noqa: E402

_CAL = _ROOT / "fraud-investigator-v2" / "state" / "calibration"

# subject substring → (kind, expected outcome under CURRENT base logic)
GOLDEN = {
    "Aspen": ("true_positive", "FILE"),
    "Willow":   ("true_positive", "FILE"),
    "Baobab":    ("negative_control", "DECLINE"),
    "Laurel":     ("negative_control", "FILE"),   # Phase-3 (Gap-2) flips this to DECLINE
}
_TERMINAL = {"complete", "case_ready", "abandoned"}


def _latest_terminal_run(subject_substr: str) -> Path | None:
    """Newest terminal calibration run dir whose subject contains the substring."""
    best = None
    for f in glob.glob(str(_CAL / "CALIBRATION-*" / "summary.json")):
        try:
            d = json.load(open(f))
        except Exception:
            continue
        if subject_substr not in json.dumps(d.get("subject") or ""):
            continue
        if str(d.get("status")) not in _TERMINAL:
            continue
        ts = d.get("last_updated") or d.get("start_time") or ""
        if best is None or ts > best[0]:
            best = (ts, Path(f).parent)
    return best[1] if best else None


@pytest.mark.parametrize("subject,expected", [(s, v[1]) for s, v in GOLDEN.items()])
def test_golden_replay(subject, expected):
    run = _latest_terminal_run(subject)
    if run is None:
        pytest.skip(f"no terminal calibration run found for {subject!r}")
    verdict, rationale = replay_verdict(run, calibration=True)
    got = "FILE" if is_file(verdict) else ("DECLINE" if verdict.value in (
        "NOT_ACTIONABLE", "COUNSEL_ABANDONED") else f"OTHER:{verdict.value}")
    assert got == expected, (
        f"{subject}: expected {expected}, got {verdict.value}\n"
        f"rationale: {json.dumps(rationale, indent=2)}")


def test_oracle_imports_and_runs():
    # smoke: the replay function is importable and returns a FinalVerdict + rationale
    run = _latest_terminal_run("Willow")
    if run is None:
        pytest.skip("no Willow run")
    verdict, rationale = replay_verdict(run, calibration=True)
    assert "predicates" in rationale and "verdict" in rationale
    assert all({"name", "status", "why"} <= set(p) for p in rationale["predicates"])
