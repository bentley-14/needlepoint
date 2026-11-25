"""Fast verdict-replay oracle — recompute a case's FinalVerdict from SAVED state.

The whole refactor (legible legal-predicate filing test) hinges on being able to
change the verdict logic and re-check the golden calibration cases in
milliseconds, instead of a 4-hour live re-run. This module does exactly that: it
reads a completed run's ``summary.json``, builds the legal-predicate Scorecard
from the already-persisted facts (falsity anchors, foreclosure ledger,
adversarial dispositions, discount/EVF, counts retained), and calls the SOLE
decision function ``fraud_investigator.verdict.compute_verdict`` — no agents, no
LLM. When the verdict logic changes, the replay reflects it automatically.

Usage:
    python scripts/verdict_replay.py <state_dir> [--production]
    # or import replay_verdict(state_dir, calibration=True) -> (FinalVerdict, dict)
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "fraud-investigator-v2" / "src"
sys.path.insert(0, str(_SRC))

from fraud_investigator import verdict as _verdict  # noqa: E402
from fraud_investigator.schemas import FinalVerdict  # noqa: E402


def replay_summary(summary: dict, calibration: bool = True) -> tuple[FinalVerdict, dict]:
    """Recompute the verdict from an in-memory summary dict (== summary.json).

    The core of the replay oracle, decoupled from the filesystem so a FROZEN
    fixture (which embeds the summary) can reuse it without a state dir — see
    scripts/replay_battery.py. Uses the canonical verdict.build_scorecard so the
    oracle and the live orchestrator share one decision path."""
    sc = _verdict.build_scorecard(summary, calibration=calibration)
    decision = _verdict.compute_verdict(sc)
    rationale = {
        "verdict": decision.verdict.value,
        "summary": decision.rationale,
        "predicates": [{"name": p.name, "status": p.status, "why": p.rationale}
                       for p in decision.predicates],
        "scorecard": {
            "n_counts_with_confirmed_falsity": sc.n_counts_with_confirmed_falsity,
            "fraud_more_plausible": sc.fraud_more_plausible,
            "confirmed_damages_usd": sc.confirmed_damages_usd,
            "evf": sc.evf, "calibration": sc.calibration,
        },
    }
    return decision.verdict, rationale


def replay_verdict(state_dir: str | Path, calibration: bool = True) -> tuple[FinalVerdict, dict]:
    """Recompute the final verdict from a saved summary.json. Thin wrapper over
    replay_summary that reads the file."""
    summary = json.loads((Path(state_dir) / "summary.json").read_text())
    return replay_summary(summary, calibration=calibration)


FILE_CLASS = {FinalVerdict.SETTLEMENT_READY, FinalVerdict.FILE_WITH_DISCOVERY_DEPS}


def is_file(verdict: FinalVerdict) -> bool:
    return verdict in FILE_CLASS


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("state_dir")
    ap.add_argument("--production", action="store_true",
                    help="apply economics carve-outs (default: calibration mode)")
    args = ap.parse_args(argv)
    verdict, rationale = replay_verdict(args.state_dir, calibration=not args.production)
    print(json.dumps({"verdict": verdict.value, "is_file": is_file(verdict),
                      "rationale": rationale}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
