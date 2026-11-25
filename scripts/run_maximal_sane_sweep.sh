#!/bin/bash
# Maximal-sane walk-forward sweep (main-repo fast-path → features_full).
# Scores 2015-2023 SERIALLY (one year at a time) — memory-safe per M4 constraint.
# Features: gnn_pca-16 + centrality(pagerank,clustering) + dmer + xprog + hcpcs-242.
# Labels: cleaned gold_set (1,001 unique NPIs). Pre-flight: 0 FAIL / 0 WARN.
# Timing (measured 2026-06-09, M4 + warm features_full cache): ~2.5-15 min/year
# (early years fastest; later years have more train folds). Full sweep well under 2h.
#
# R2 staging (opt-in):
#   NP_R2_STAGING=1 bash scripts/run_maximal_sane_sweep.sh
# When set: stage the UNION for all $YEARS once before the loop, pass NP_KEEP_CACHE=1
# to each per-year invocation (no re-pull/re-purge between years), then purge once
# after the full sweep (unless the caller had NP_KEEP_CACHE already set).
set -uo pipefail
cd "<repo>"
mkdir -p results
LOG="results/maximal_sane_sweep.log"
# YEARS override lets us resume a partial sweep, e.g. YEARS="2017 2018 ... 2023".
YEARS="${YEARS:-2015 2016 2017 2018 2019 2020 2021 2022 2023}"
echo "=== maximal-sane sweep starting $(date) | years: ${YEARS} ===" | tee -a "$LOG"

# ── R2 staging: stage the union for all years ONCE before the loop ──────────
_SWEEP_R2_RUN_ID=""
_CALLER_KEEP_CACHE="${NP_KEEP_CACHE:-0}"
if [ "${NP_R2_STAGING:-0}" = "1" ]; then
  echo "=== R2 staging: staging working set for years: ${YEARS} ===" | tee -a "$LOG"
  _SWEEP_R2_RUN_ID=$(python3 -c "
import os, sys
sys.path.insert(0, '.')
from fraudlib.ranker.r2_working_set import sweep_orphans, stage, manifest
sweep_orphans()
years = [int(y) for y in '${YEARS}'.split()]
items = manifest(years, attach_extras=True)
run_id, fetched = stage(items)
print(run_id)
" 2>&1 | tee -a "$LOG" | tail -1)
  echo "  sweep run_id=${_SWEEP_R2_RUN_ID}" | tee -a "$LOG"
  # Pass NP_KEEP_CACHE=1 so per-year invocations don't re-pull or purge between years.
  # Unset NP_R2_STAGING so per-year invocations don't independently re-stage/re-purge
  # (the sweep already staged the full union above).
  export NP_KEEP_CACHE=1
  export NP_R2_STAGING=0
fi

for YEAR in ${YEARS}; do
  echo "--- score_year=${YEAR} starting $(date) ---" | tee -a "$LOG"
  KMP_DUPLICATE_LIB_OK=TRUE OMP_NUM_THREADS=6 nice -n 10 \
    python3 -u scripts/experiments/walk_forward_v4.py --score-year "$YEAR" \
    > "results/sweep_${YEAR}.log" 2>&1
  RC=$?
  echo "--- score_year=${YEAR} finished rc=${RC} $(date) ---" | tee -a "$LOG"
  if [ "$RC" -ne 0 ]; then
    echo "  ${YEAR}: FAILED rc=${RC} — last lines:" | tee -a "$LOG"
    tail -15 "results/sweep_${YEAR}.log" | sed 's/^/    /' | tee -a "$LOG"
    echo "  ABORTING sweep (fix ${YEAR} before continuing)." | tee -a "$LOG"
    exit "$RC"
  fi
  RS=$(ls results/walk_forward_v4_*_y${YEAR}_*/recall_summary.json 2>/dev/null | head -1)
  if [ -n "$RS" ] && [ -f "$RS" ]; then
    python3 -c "import json,sys; d=json.load(open('$RS'))[0]; print(f\"  ${YEAR}: R@1k={d.get('recall@1000','?'):.4f} R@5k={d.get('recall@5000','?'):.4f} R@10k={d.get('recall@10000','?'):.4f} n_pos={d.get('n_pos_score_filtered','?')} TP@10k={d.get('tp@10000','?')}\")" 2>/dev/null | tee -a "$LOG" \
      || echo "  ${YEAR}: recall_summary present but unparseable" | tee -a "$LOG"
  else
    echo "  ${YEAR}: no recall_summary.json found" | tee -a "$LOG"
  fi
done

echo "" | tee -a "$LOG"
echo "=== SWEEP COMPLETE $(date) ===" | tee -a "$LOG"

# ── R2 staging: purge the sweep working set ONCE after the full loop ─────────
# Skip if the caller originally had NP_KEEP_CACHE=1 (they want the panels kept).
if [ "${NP_R2_STAGING:-0}" = "1" ] && [ -n "${_SWEEP_R2_RUN_ID}" ] && [ "${_CALLER_KEEP_CACHE}" != "1" ]; then
  echo "=== R2 staging: purging sweep working set (run_id=${_SWEEP_R2_RUN_ID}) ===" | tee -a "$LOG"
  python3 -c "
import sys
sys.path.insert(0, '.')
from fraudlib.ranker.r2_working_set import purge
purge('${_SWEEP_R2_RUN_ID}')
" 2>&1 | tee -a "$LOG"
  echo "  purge complete." | tee -a "$LOG"
fi
# Final table
python3 - <<'PYEOF' | tee -a "$LOG"
import json, glob
print(f"\n{'year':<6}{'n_pos':<7}{'R@1k':<9}{'R@5k':<9}{'R@10k':<9}{'TP@10k':<8}")
print("-"*52)
for y in range(2015, 2024):
    cand = glob.glob(f"results/walk_forward_v4_*_y{y}_*/recall_summary.json")
    if cand:
        d = json.load(open(cand[0]))[0]
        print(f"{y:<6}{d.get('n_pos_score_filtered',0):<7}{d.get('recall@1000',0):<9.4f}{d.get('recall@5000',0):<9.4f}{d.get('recall@10000',0):<9.4f}{d.get('tp@10000',0):<8}")
    else:
        print(f"{y:<6}(no result)")
PYEOF
