"""CLI for invoking any supporting agent (data_intake, data_auditor,
pipeline_engineer, feature_engineer, corpus_curator).

Launches the agent with `subprocess.Popen(..., start_new_session=True)` so it
survives reruns.

Usage:
  python scripts/run_support_agent.py --agent data_intake
  python scripts/run_support_agent.py --agent data_auditor
  python scripts/run_support_agent.py --agent pipeline_engineer --failed-run-id run_2026...
  python scripts/run_support_agent.py --agent feature_engineer --scheme-slug pstim --scheme-description "..."
  python scripts/run_support_agent.py --agent corpus_curator --since-date 2026-04-30

Each agent's output + events stream lands at
state/runs/agent_<name>_<run_id>/{events.jsonl, summary.json, *.parsed.json}.
"""
from __future__ import annotations

import argparse
import logging
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "fraud-investigator-v2" / "src"))

from fraud_investigator.agents import (
    auto_patcher, corpus_curator, data_auditor, data_intake, data_repair,
    feature_engineer, pipeline_engineer,
)
from fraud_investigator.event_sink import EventSink

LOG = logging.getLogger("run_support_agent")

STATE_ROOT = REPO_ROOT / "state" / "runs"

AGENT_RUNNERS = {
    "data_intake": data_intake.run,
    "data_auditor": data_auditor.run,
    "pipeline_engineer": pipeline_engineer.run,
    "feature_engineer": feature_engineer.run,
    "corpus_curator": corpus_curator.run,
    "auto_patcher": auto_patcher.run,
    "data_repair": data_repair.run,
}


def _new_run_id(agent: str) -> str:
    return f"agent_{agent}_{datetime.now(timezone.utc):%Y%m%dT%H%M%S}_{secrets.token_hex(3)}"


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    ap = argparse.ArgumentParser(description="Run a supporting agent")
    ap.add_argument("--agent", required=True, choices=list(AGENT_RUNNERS.keys()))
    # Per-agent options
    ap.add_argument("--failed-run-id", help="(pipeline_engineer / auto_patcher) The run to diagnose / patch")
    ap.add_argument("--diagnosis-run-id", help="(auto_patcher) The pipeline_engineer run dir id whose parsed.json to consume")
    ap.add_argument("--audit-run-id", help="(data_repair) The data_auditor run dir id whose parsed.json to repair")
    ap.add_argument("--scheme-slug", help="(feature_engineer) Slug for the scheme")
    ap.add_argument("--scheme-description", help="(feature_engineer) Free-text scheme description")
    ap.add_argument("--since-date", help="(corpus_curator) ISO date — only press releases since this")
    args = ap.parse_args()

    run_id = _new_run_id(args.agent)
    run_dir = STATE_ROOT / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    event_sink = EventSink(run_dir / "events.jsonl")

    LOG.info(f"=== Running agent {args.agent!r} → {run_dir.relative_to(REPO_ROOT)} ===")

    runner = AGENT_RUNNERS[args.agent]
    try:
        if args.agent == "pipeline_engineer":
            if not args.failed_run_id:
                ap.error("pipeline_engineer requires --failed-run-id")
            result = runner(failed_run_id=args.failed_run_id,
                            run_dir=run_dir, event_sink=event_sink)
        elif args.agent == "feature_engineer":
            if not (args.scheme_slug and args.scheme_description):
                ap.error("feature_engineer requires --scheme-slug AND --scheme-description")
            result = runner(scheme_slug=args.scheme_slug,
                            scheme_description=args.scheme_description,
                            run_dir=run_dir, event_sink=event_sink)
        elif args.agent == "corpus_curator":
            if not args.since_date:
                ap.error("corpus_curator requires --since-date YYYY-MM-DD")
            result = runner(since_date=args.since_date,
                            run_dir=run_dir, event_sink=event_sink)
        elif args.agent == "auto_patcher":
            if not (args.failed_run_id and args.diagnosis_run_id):
                ap.error("auto_patcher requires --failed-run-id AND --diagnosis-run-id")
            result = runner(diagnosis_run_id=args.diagnosis_run_id,
                            failed_run_id=args.failed_run_id,
                            run_dir=run_dir, event_sink=event_sink)
        elif args.agent == "data_repair":
            if not args.audit_run_id:
                ap.error("data_repair requires --audit-run-id")
            result = runner(audit_run_id=args.audit_run_id,
                            run_dir=run_dir, event_sink=event_sink)
        else:
            # data_intake, data_auditor — no extra args
            result = runner(run_dir=run_dir, event_sink=event_sink)
    except Exception as e:
        LOG.exception(f"Agent {args.agent!r} failed")
        return 1

    LOG.info(f"=== Agent {args.agent!r} complete ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main() or 0)
