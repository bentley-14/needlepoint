#!/bin/bash
# Regenerate CASE-6A2A0E69-2026 end files with the human-verified Dogwood evidence.
# Dogwood = public, unsealed Title VII employment suit (NOT a sealed qui tam) -> no first-to-file bar.
# iter-02 + iter-03 investigator evidence already corrected on disk (+ .prehumanfix.bak).
#
# WHY THIS IS A RUNBOOK, NOT A ONE-SHOT:
#   orchestrator.py:442 SKIPS Phase A+B when state.status is terminal (case_ready/abandoned/
#   max_iterations). A plain --resume-case on this COMPLETE case is a no-op. Forcing a clean
#   counsel/adversarial/writer regen requires (a) resetting the persisted status to non-terminal
#   and (b) trimming the iter-3 counsel records + removing downstream parsed.json checkpoints so
#   they re-run on corrected evidence. That state surgery should be validated with a human, and
#   the regen re-runs many opus[1m]/effort=max stages (defense/adv/case_writer) that WILL hit the
#   5h ceiling unless overage is enabled. So: this script backs up + prints the plan only.
set -euo pipefail
ROOT="<repo>"
ts=$(date +%Y%m%dT%H%M%S)
tar -czf "$ROOT/fraud-investigator-v2/state/CASE-6A2A0E69-2026.pre_dogwood_regen.$ts.tgz" \
    -C "$ROOT/fraud-investigator-v2/state" CASE-6A2A0E69-2026
echo "[1/2] Backed up -> CASE-6A2A0E69-2026.pre_dogwood_regen.$ts.tgz"
cat <<'PLAN'
[2/2] REGEN PLAN (finalize with Claude before mutating state; enable overage first):
  Option A (surgery): reset summary.json status -> non-terminal; null iter-3 counsel_build/
    first_chair/disposition in state; rm iter-03/counsel_*.parsed.json, adv-r*/.parsed.json,
    case_writer.parsed.json; then resume (command below). Re-runs counsel+adversarial+writer.
  Option B (clean, preferred): fresh full run (new case_id) now that docket_lookup.py + the
    COURT_RECORDS_LOOKUP guidance are in place, so the investigator self-classifies Dogwood.
  Resume command (Option A), run in a top-level terminal:
    python3 -m scripts.orchestrate_pipeline --invocation-mode investigate \
      --resume-case CASE-6A2A0E69-2026 \
      --queue-entry validation/triage/2023_top1-500.json#1900000026 \
      --max-iterations 6 --max-pre-rebuttal-iters 3 --max-adversarial 2
PLAN
