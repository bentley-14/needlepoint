#!/usr/bin/env bash
# Serial, sleep-resilient calibration QUEUE over the battery manifest's true
# positives. Each subject runs the full leak-safe clean-room loop via
# run_calibration_smoke.sh on the tmux backend (personal usage limits). Serial
# on purpose (one heavy run per machine — M4 memory); sleep-resilient (the whole
# tree suspends/resumes with the laptop); resumable (skips subjects that already
# have a completed calibration run). STOPS the whole queue on a watchdog leak
# (a leak means the jail is broken — do not keep running).
#
# Usage:  bash scripts/calibration/run_calibration_queue.sh
# Log:    /tmp/np_tmux_run/cal_queue.log
set -u
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO"
MANIFEST="$REPO/validation/battery/battery.json"
# NOT /tmp: macOS periodic cleanup deleted /tmp/np_tmux_run mid-batch on
# 2026-08-02, taking the queue log + subjects TSV (and plausibly the tmux
# socket dir) with it. Overnight state must live on durable disk.
LOGDIR="$REPO/.overnight_logs/calibration"
LOG="$LOGDIR/cal_queue.log"
mkdir -p "$LOGDIR"

# Execution backend. Default to the PROVEN headless `claude_p` jail: it is
# --max-turns-bounded and silently auto-denies non-allowlisted tools, so it does
# not exhibit the intermittent zero-output stall the interactive tmux panes hit
# on long reasoning steps (2026-06-16: a counsel_build pane produced nothing for
# 25+ min). The tmux backend (+ guard hook) remains available for the
# personal-usage-limits path — set FRAUD_INV_EXEC_BACKEND=tmux — once that hang
# is resolved. Headless calibration is session-launchable (PR #204) and bills
# Agent-SDK credit.
export FRAUD_INV_EXEC_BACKEND="${FRAUD_INV_EXEC_BACKEND:-claude_p}"

# Calibration validates whether the LOOP does things correctly, not the ceiling
# of its reasoning — so default to the fast `draft` profile (Sonnet everywhere)
# instead of production opus[1m]/max. A single opus-max run took ~2h; Sonnet
# turns the 10-TP queue from an overnight job into a tractable one. Override by
# pre-setting FRAUD_INV_PROFILE=production for a fidelity check.
export FRAUD_INV_PROFILE="${FRAUD_INV_PROFILE:-draft}"

# Concurrency guard: never start while another heavy loop is live (M4 OOM rule).
if pgrep -fl "orchestrate_pipeline|fraud_investigator " >/dev/null 2>&1; then
  echo "[$(date '+%F %T')] REFUSING: another orchestrator/loop is already running — one heavy run at a time." | tee -a "$LOG"
  pgrep -fl "orchestrate_pipeline|fraud_investigator " | tee -a "$LOG"
  exit 3
fi

echo "[$(date '+%F %T')] === calibration queue start (pid $$, backend=tmux) ===" >>"$LOG"

# Emit the battery subjects as  npi <TAB> as_of <TAB> subject_hint.
# Both true_positives (expect FILE — recall) AND negative_controls (expect
# ABANDON/DECLINE — precision: control_file_rate / evf_separation). Controls are
# first in the manifest, so they run first on a fresh launch.
# adjudicated_true_negative added 2026-07-31: declined / merits-dismissed qui
# tams are the PRECISION FIREWALL (a FILE on one is a false positive counsel
# would really have lost). Excluding them here is why the battery's
# adjudicated_tn_* metrics never populated.
# CAL_ONLY_NPIS=<comma list> restricts the queue to a slice — subjects are ~3h
# each and strictly serial, so a full pass is a multi-day commitment.
SUBJECTS="$LOGDIR/cal_subjects.tsv"
CAL_ONLY_NPIS="${CAL_ONLY_NPIS:-}" python3 - "$MANIFEST" >"$SUBJECTS" <<'PY'
import json, os, sys
d = json.load(open(sys.argv[1]))
only = {x.strip() for x in (os.environ.get("CAL_ONLY_NPIS") or "").split(",") if x.strip()}
for s in d["subjects"]:
    if s.get("kind") not in ("true_positive", "negative_control", "adjudicated_true_negative"):
        continue
    if only and str(s["npi"]) not in only:
        continue
    print(f"{s['npi']}\t{s['as_of']}\t{s.get('subject_hint') or s['name']}")
PY

# True if this subject already has a terminal calibration run (skip it —
# resumable). Matches by NPI **or** by normalized name tokens: completed summaries
# store `subject` as a name string that frequently lacks the NPI (e.g. "Steven
# Provider Baobab"), so an NPI-only match silently re-ran finished subjects
# (~4h each). We also pass the manifest hint and require its core name tokens
# (titles dropped, ≥2 tokens) to be a subset of the stored subject's tokens.
# CAL_STALE_BEFORE=YYYY-MM-DD — a run STARTED before this date does not count as
# completed, so the queue re-runs it. Needed because a schema/prompt change can
# silently invalidate older runs: the falsity-anchor slot landed 2026-06-11
# (commit 64e98ec), and every calibration run before it records ZERO anchors —
# not because the subject lacked one, but because the field did not exist. Those
# runs replay as a spurious P1 FAILED. Default empty = prior behavior.
completed_for() {
  CAL_STALE_BEFORE="${CAL_STALE_BEFORE:-}" python3 - "$1" "$2" <<'PY'
import json, glob, os, re, sys
npi = sys.argv[1]
hint = sys.argv[2] if len(sys.argv) > 2 else ""
stale_before = (os.environ.get("CAL_STALE_BEFORE") or "").strip()
TERM = {"case_ready", "complete", "abandoned", "max_iterations", "case_complete"}
_TITLES = {"md", "do", "dr", "jr", "sr", "ii", "iii", "phd", "dpm", "od", "mds"}
def toks(s):
    return {t for t in re.findall(r"[a-z]{2,}", (s or "").lower()) if t not in _TITLES}
want = toks(hint)
for f in glob.glob("fraud-investigator-v2/state/calibration/CALIBRATION-*/summary.json"):
    try:
        d = json.load(open(f))
    except Exception:
        continue
    if str(d.get("status")) not in TERM:
        continue
    if stale_before and str(d.get("start_time") or "")[:10] < stale_before:
        continue          # pre-dates a schema/prompt change — not a usable run
    subj = d.get("subject") or {}
    raw = json.dumps(subj)
    snpi = subj.get("npi") if isinstance(subj, dict) else None
    name_match = len(want) >= 2 and want <= toks(raw)
    if snpi == npi or npi in raw or name_match:
        sys.exit(0)   # already done
sys.exit(1)
PY
}

# Block until the API is reachable. A dead network does NOT fail fast inside
# `claude -p`: on 2026-08-01 all 8 subjects died on ENOTFOUND, but only after
# multi-hour hangs per attempt (worst: 11,062s) x3 retries — ~15h and $32.78 of
# cache-write billing for zero data. curl exits 0 on any HTTP response (401 from
# an unauthenticated /v1/models is a healthy connection), so this probes
# reachability, not auth.
wait_for_api() {
  local tries=0 max="${CAL_NET_WAIT_TRIES:-60}"     # 60 x 30s = 30 min
  while ! curl -s -o /dev/null --max-time 10 https://api.anthropic.com/v1/models 2>/dev/null; do
    tries=$((tries+1))
    if [ "$tries" -ge "$max" ]; then return 1; fi
    [ "$tries" -eq 1 ] && echo "[$(date '+%F %T')] network unreachable — waiting (max $((max*30/60))m)" >>"$LOG"
    sleep 30
  done
  [ "$tries" -gt 0 ] && echo "[$(date '+%F %T')] network recovered after $((tries*30))s" >>"$LOG"
  return 0
}

n_run=0 n_skip=0 n_fail=0
while IFS=$'\t' read -r npi asof hint; do
  [ -z "$npi" ] && continue
  if completed_for "$npi" "$hint"; then
    echo "[$(date '+%F %T')] SKIP $npi — already has a completed calibration run" >>"$LOG"
    n_skip=$((n_skip+1)); continue
  fi
  if ! wait_for_api; then
    echo "[$(date '+%F %T')] !!! NETWORK DOWN — stopping queue before $npi rather than burning a subject on ENOTFOUND. Re-run when connectivity returns; completed subjects are skipped." >>"$LOG"
    exit 3
  fi
  echo "[$(date '+%F %T')] === RUN $npi  as_of=$asof  ($hint) ===" >>"$LOG"
  bash "$REPO/scripts/calibration/run_calibration_smoke.sh" "$npi" "$asof" "$hint" >>"$LOG" 2>&1
  rc=$?
  echo "[$(date '+%F %T')] $npi exited rc=$rc" >>"$LOG"
  if [ "$rc" -eq 2 ]; then
    echo "[$(date '+%F %T')] !!! LEAK DETECTED on $npi — STOPPING QUEUE (clean-room jail is broken). Investigate before continuing." >>"$LOG"
    exit 2
  fi
  [ "$rc" -ne 0 ] && n_fail=$((n_fail+1))
  n_run=$((n_run+1))
  sleep 10
done < "$SUBJECTS"

echo "[$(date '+%F %T')] === queue done: ran=$n_run skipped=$n_skip nonzero_exits=$n_fail; scoring ===" >>"$LOG"
PYTHONPATH=.:fraud-investigator-v2/src python3 scripts/calibration_battery.py --report >>"$LOG" 2>&1
echo "[$(date '+%F %T')] === DONE — see calibration_battery report above ===" >>"$LOG"
