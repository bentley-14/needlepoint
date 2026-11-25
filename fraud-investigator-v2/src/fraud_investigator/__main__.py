"""CLI entry point.

Usage:
    python -m fraud_investigator "Dr. Jane Smith pain management NJ opioid"
    fraud-investigator-v2 "<subject>"   # if installed via pip install -e .

Optional flags:
    --case-id <id>               resume an existing case
    --max-pre-rebuttal-iters <N> Phase A build-loop cap (default 3)
    --max-iterations <N>         (vestigial — Phase A cap is --max-pre-rebuttal-iters)
    --max-adversarial <N>        override default (4)
    --state-dir <path>           override state/ directory
    --working-dir <path>         override CWD for claude -p subprocesses (cleanroom)
    --dry-run                    print what would run, don't spawn claude -p
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import click
from dotenv import load_dotenv

# Load .env from the v2 root before any other module imports CLAUDE_BIN etc.
_V2_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(_V2_ROOT / ".env", override=False)
# Also try a .env in the current working directory (e.g., inside a cleanroom)
load_dotenv(Path.cwd() / ".env", override=False)

from .orchestrator import (
    DEFAULT_MAX_ADVERSARIAL,
    DEFAULT_MAX_ITERATIONS,
    DEFAULT_MAX_PRE_REBUTTAL_ITERS,
    Orchestrator,
)

LOG = logging.getLogger("fraud_investigator")


@click.command(context_settings={"show_default": True})
@click.argument("subject", type=str)
@click.option("--case-id", default=None, type=str, help="Resume an existing case by id.")
@click.option("--max-iterations", default=DEFAULT_MAX_ITERATIONS, type=int,
              help="(vestigial in two-phase flow; use --max-pre-rebuttal-iters)")
@click.option("--max-pre-rebuttal-iters", default=DEFAULT_MAX_PRE_REBUTTAL_ITERS, type=int,
              help="Phase A build-loop cap (1-6). At this iter, first-chair must "
                   "choose PROCEED_TO_REBUTTAL or ABANDON_CASE.")
@click.option("--max-adversarial", default=DEFAULT_MAX_ADVERSARIAL, type=int)
@click.option("--state-dir", default=None, type=click.Path(path_type=Path),
              help="Override state/ directory.")
@click.option("--working-dir", default=None, type=click.Path(path_type=Path),
              help="Override CWD for all claude -p subprocess agents "
                   "(e.g., a cleanroom dir with symlinked-only data). "
                   "Orchestrator state still writes to --state-dir.")
@click.option("--dry-run", is_flag=True, help="Don't spawn claude -p; print the plan.")
@click.option("--calibration-as-of", default=None, type=str,
              help="Run in CALIBRATION clean-room mode with date cutoff YYYY-MM-DD. "
                   "REQUIRES --calibration-export (the fail-closed clean export). The agent "
                   "runs the real billing tools inside that out-of-repo export — no gold_set / "
                   "DOJ caches / .git / post-cutoff data are reachable; WebSearch/WebFetch/Grep/"
                   "Glob denied, pre-cutoff Wayback only. Case ID auto-synthesized as "
                   "CALIBRATION-<uuid>; state writes go to state/calibration/<case_id>/. Easiest "
                   "path: scripts/calibration/run_calibration_smoke.sh <NPI> <DATE> \"<SUBJECT>\" (builds the "
                   "export + runs + watchdog). Use ONLY on known true-positive NPIs.")
@click.option("--calibration-export", default=None, type=click.Path(path_type=Path),
              help="Path to a fail-closed CLEAN EXPORT built by "
                   "scripts/calibration/build_calibration_workspace.py --with-panels. Requires "
                   "--calibration-as-of. The agent's cwd becomes this export (which holds "
                   "ONLY pre-cutoff panels + answer-key-free analysis tools), so it runs the "
                   "real billing tools but cannot reach gold_set / DOJ caches / .git / "
                   "post-cutoff data. This is the data-rich, broad-tools calibration mode.")
@click.option("--defense-as-mock", is_flag=True, default=None,
              help="PR7 (experimental): replace counsel_rebuttal with the REAL "
                   "defense round 1 fired early during Phase B; the adversarial "
                   "loop replays the cached motion and continues from "
                   "legal_adv_response. Default OFF (also via env "
                   "FRAUD_INV_DEFENSE_AS_MOCK=1). A/B on calibration cases "
                   "before relying on it.")
@click.option("-v", "--verbose", count=True, help="-v info, -vv debug")
def main(
    subject: str,
    case_id: str | None,
    max_iterations: int,
    max_pre_rebuttal_iters: int,
    max_adversarial: int,
    state_dir: Path | None,
    working_dir: Path | None,
    dry_run: bool,
    calibration_as_of: str | None,
    calibration_export: Path | None,
    defense_as_mock: bool | None,
    verbose: int,
) -> int:
    level = logging.WARNING - 10 * verbose
    logging.basicConfig(level=max(level, logging.DEBUG), format="%(asctime)s %(name)s %(levelname)s %(message)s")

    orch = Orchestrator(
        subject=subject,
        case_id=case_id,
        max_iterations=max_iterations,
        max_pre_rebuttal_iters=max_pre_rebuttal_iters,
        max_adversarial=max_adversarial,
        state_dir=state_dir,
        working_dir=working_dir,
        dry_run=dry_run,
        calibration_as_of=calibration_as_of,
        calibration_export=calibration_export,
        defense_as_mock=defense_as_mock,
    )
    rc = orch.run()
    return rc


if __name__ == "__main__":
    sys.exit(main(standalone_mode=False) or 0)
