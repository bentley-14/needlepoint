#!/usr/bin/env bash
# Run the full needlepoint loop in CALIBRATION clean-room mode on a known TP,
# with the leak watchdog tailing in parallel.
#
# Usage:
#   bash scripts/calibration/run_calibration_smoke.sh <NPI> <CUTOFF-YYYY-MM-DD> "<SANITIZED-SUBJECT>"
#
# Example (Aspen):
#   bash scripts/calibration/run_calibration_smoke.sh \
#     1900000007 \
#     2020-01-01 \
#     "Provider Aspen -- Family Practice -- Anytown, KY -- NPI 1900000007"
#
# What it does:
#   1. Launches the watchdog in the background, polling the run's state dir
#      for any leak-pattern hit.
#   2. Launches the orchestrator with --calibration-as-of <CUTOFF>.
#   3. After the orchestrator exits, kills the watchdog if still alive.
#   4. Reports: orchestrator exit code, watchdog verdict, run dir path.
#
# CAVEAT: This script does NOT pre-flight model-recognition (asking a fresh
# Claude "what do you know about <Name>"). Do that yourself before adding a
# new TP to the calibration set — model recognition is unsolvable by sandbox.
#
# The orchestrator stays in foreground so you can see its progress. The
# watchdog runs in background; its exit signals leak detection (you can
# manually `kill` the orchestrator if so).
set -u

if [ $# -lt 3 ]; then
    echo "Usage: $0 <NPI> <CUTOFF-YYYY-MM-DD> \"<SANITIZED-SUBJECT>\"" >&2
    echo "Example: $0 1900000007 2020-01-01 \"Provider Aspen -- Family Practice -- KY -- NPI 1900000007\"" >&2
    exit 1
fi

NPI="$1"
CUTOFF="$2"
SUBJECT="$3"

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO_ROOT"

# Calibration tests loop correctness, not reasoning ceiling — default to the fast
# `draft` profile (Sonnet everywhere) unless the caller forces production. The
# orchestrator subprocess (launched below) inherits this exported var.
# PROFILE=battery (or FRAUD_INV_PROFILE=battery) selects the battery profile:
# production model split (Sonnet-5 querying / Opus reasoning gates) + the REAL
# defense red team (defense_as_mock defaults ON) — use it for battery/ratchet
# runs whose FILE verdicts must be red-team-validated. `PROFILE` is the short
# alias; an explicit FRAUD_INV_PROFILE still wins.
export FRAUD_INV_PROFILE="${FRAUD_INV_PROFILE:-${PROFILE:-draft}}"

# Default to the PROVEN headless `claude_p` jail (--max-turns-bounded, silent
# auto-deny) — the interactive tmux panes hit an intermittent zero-output stall
# on long reasoning steps (2026-06-16). Set FRAUD_INV_EXEC_BACKEND=tmux to use
# the personal-usage-limits path once that hang is resolved.
export FRAUD_INV_EXEC_BACKEND="${FRAUD_INV_EXEC_BACKEND:-claude_p}"

# Sanitize-subject smoke check: the SUBJECT string the user passes is what
# flows into agent context. It MUST NOT contain post-cutoff knowledge.
# (Names are fine — defendant name is public; the leak surface is the verified
# fraud allegation.)
FORBIDDEN_IN_SUBJECT='(settled|settlement|indict|plea|qui tam|whistleblow|\$[0-9])'
if echo "$SUBJECT" | grep -iqE "$FORBIDDEN_IN_SUBJECT"; then
    echo "ERROR: subject string contains a post-cutoff token. Strip outcome/dollar/disposition fields:" >&2
    echo "  $SUBJECT" >&2
    exit 1
fi

# Predict the calibration case_id pattern + state-dir path; the orchestrator
# will mint a CALIBRATION-<8-hex-uuid> at runtime. We don't know the UUID
# up front, so we let the orchestrator start, then locate the freshest
# CALIBRATION-* dir under the calibration state root.
CAL_STATE_ROOT="$REPO_ROOT/fraud-investigator-v2/state/calibration"
mkdir -p "$CAL_STATE_ROOT"

# Durable, not /tmp: macOS periodic cleanup wiped /tmp mid-batch 2026-08-02.
# TOKENS_FILE especially is read by the watchdog for the entire multi-hour run.
RUN_LOG_DIR="$REPO_ROOT/.overnight_logs/calibration"
mkdir -p "$RUN_LOG_DIR"
ORCH_LOG="$RUN_LOG_DIR/needlepoint_loop_orch.log"
WATCHDOG_LOG="$RUN_LOG_DIR/needlepoint_loop_watchdog.log"
LEAK_MARKER="$RUN_LOG_DIR/needlepoint_loop_leak.txt"
TOKENS_FILE="$RUN_LOG_DIR/needlepoint_loop_tokens.json"
rm -f "$ORCH_LOG" "$WATCHDOG_LOG" "$LEAK_MARKER" "$TOKENS_FILE"

echo "=== monitored calibration loop run ==="
echo "  NPI:     $NPI"
echo "  cutoff:  $CUTOFF"
echo "  subject: $SUBJECT"
echo "  state root: $CAL_STATE_ROOT"
echo "  orchestrator log: $ORCH_LOG"
echo "  watchdog log: $WATCHDOG_LOG"
echo "  leak marker (only if leak detected): $LEAK_MARKER"
echo ""

# Build the fail-closed clean export FIRST (pre-cutoff panels + answer-key-free
# tools, no gold_set / DOJ caches / .git / post-cutoff data). The build runs its
# own sanitization audit and exits non-zero on any leak — abort the run if so.
EXPORT="$HOME/.calibration_workspaces/npi_${NPI}_asof_${CUTOFF}"
echo "=== building clean export (--with-panels) ==="
python3 "$REPO_ROOT/scripts/calibration/build_calibration_workspace.py" \
    --npi "$NPI" --as-of "$CUTOFF" --with-panels
BUILD_RC=$?
if [ "$BUILD_RC" -ne 0 ]; then
    echo "ERROR: clean-export build failed (rc=$BUILD_RC) — sanitization audit did not pass. Aborting." >&2
    exit 1
fi
if [ ! -d "$EXPORT" ]; then
    echo "ERROR: expected export not found at $EXPORT after build. Aborting." >&2
    exit 1
fi
echo "  export: $EXPORT"

# Derive THIS NPI's leak tokens from gold_set (at setup, outside the sandbox) so
# the watchdog generalizes to any NPI instead of hardcoding one defendant.
echo "=== deriving per-NPI leak tokens (from gold_set) ==="
python3 "$REPO_ROOT/scripts/calibration/calibration_leak_tokens.py" --npi "$NPI" > "$TOKENS_FILE" 2>/dev/null
if [ -s "$TOKENS_FILE" ]; then
    echo "  tokens: $TOKENS_FILE"
else
    echo "  WARNING: could not derive per-NPI tokens; watchdog falls back to built-in set" >&2
fi

# Launch orchestrator in the background so we can also launch the watchdog,
# then wait for the orchestrator to finish.
echo "=== launching orchestrator ==="
# Cap adversarial rounds at 3 for calibration (default is 4). Calibration measures
# the FILE-vs-abandon recall verdict — set at disposition — so the later rounds
# mainly refine the discount %; trimming a round cuts wall-time without changing
# the recall signal. Override via $FRAUD_INV_MAX_ADVERSARIAL.
PYTHONPATH="$REPO_ROOT/fraud-investigator-v2/src" python3 -m fraud_investigator \
    "$SUBJECT" \
    --calibration-as-of "$CUTOFF" \
    --calibration-export "$EXPORT" \
    --max-adversarial "${FRAUD_INV_MAX_ADVERSARIAL:-3}" \
    -v \
    > "$ORCH_LOG" 2>&1 &
ORCH_PID=$!
echo "orchestrator PID: $ORCH_PID"

# Wait for the run dir to materialize so we can point the watchdog at it.
# The orchestrator creates state_root/CALIBRATION-<uuid>/ on .run() entry.
#
# DISCOVERY: pick the newest CALIBRATION-* dir under the calibration state
# root that was created in the last 60s. Earlier versions used
# `find -newer "$ORCH_LOG"` which RACED when both files landed in the same
# second (verified bug 2026-06-03: watchdog never started because the dir
# mtime equaled the log mtime, and -newer requires STRICTLY newer).
# `ls -1dt | head -1` gives the most-recent matching dir unconditionally;
# the age window confirms it's from THIS launch, not a stale prior run.
LAUNCH_TIME=$(date +%s)
echo "=== waiting for run dir to materialize ==="
RUN_DIR=""
for i in $(seq 1 60); do
    CANDIDATE=$(ls -1dt "$CAL_STATE_ROOT"/CALIBRATION-* 2>/dev/null | head -1)
    if [ -n "$CANDIDATE" ] && [ -d "$CANDIDATE" ]; then
        # stat -f for macOS; stat -c for Linux. Try both.
        DIR_MTIME=$(stat -f %m "$CANDIDATE" 2>/dev/null || stat -c %Y "$CANDIDATE" 2>/dev/null || echo 0)
        AGE=$((LAUNCH_TIME - DIR_MTIME))
        # Accept if dir was created at-or-after launch time (allow ±5s clock skew).
        # Negative AGE = dir is newer than launch (definitely ours).
        if [ "$AGE" -le 5 ]; then
            RUN_DIR="$CANDIDATE"
            echo "  found run dir: $RUN_DIR (age vs launch: ${AGE}s)"
            break
        fi
    fi
    sleep 1
done
if [ -z "$RUN_DIR" ]; then
    echo "WARNING: run dir didn't materialize within 30s — watchdog won't run"
else
    echo "=== launching watchdog ==="
    # CALIB_NAME_WITHHELD=0: this is export mode — the defendant name is provided in
    # the subject (public), so the watchdog flags only ANSWER tokens (settlement /
    # docket / prosecutors / gold_set fields), i.e. model-recall or escape leaks.
    CALIB_NAME_WITHHELD=0 CALIB_LEAK_TOKENS_FILE="$TOKENS_FILE" WATCHDOG_TIMEOUT_SEC=3600 \
        bash "$REPO_ROOT/scripts/calibration/calibration_loop_watchdog.sh" \
        "$RUN_DIR" "$LEAK_MARKER" > "$WATCHDOG_LOG" 2>&1 &
    WATCHDOG_PID=$!
    echo "watchdog PID: $WATCHDOG_PID"
fi

# Wait for orchestrator to finish (or for the user to ^C the wait)
echo ""
echo "=== orchestrator running (PID $ORCH_PID); tail $ORCH_LOG to follow ==="
wait $ORCH_PID
ORCH_EXIT=$?

# Tear down watchdog if it's still alive
if [ -n "${WATCHDOG_PID:-}" ]; then
    kill -KILL "$WATCHDOG_PID" 2>/dev/null
fi

echo ""
echo "=== orchestrator exit: $ORCH_EXIT ==="
if [ -f "$LEAK_MARKER" ]; then
    echo "=== WATCHDOG VERDICT: LEAK DETECTED ==="
    cat "$LEAK_MARKER"
    echo ""
    echo "RUN DIR: $RUN_DIR"
    exit 2
else
    echo "=== WATCHDOG VERDICT: CLEAN (no leak pattern fired) ==="
    echo "RUN DIR: $RUN_DIR"
fi

# Post-hoc final scan (in case the watchdog timed out before final files landed)
echo ""
echo "=== post-hoc final leak scan (export mode: ANSWER tokens only — name is provided) ==="
# Primary: the SAME per-NPI answer tokens the live watchdog uses (docket
# numbers, dates, PR-URL slugs, prosecutor names — derived from gold_set by
# calibration_leak_tokens.py at setup). Fallback below MIRRORS the watchdog's
# built-in ANSWER fallback verbatim (calibration_loop_watchdog.sh — keep the
# two in sync): precise Aspen hard-answer facts + gold_set field names in
# JSON-KEY form. NO generic FCA vocabulary (qui tam / settlement / bare
# dollar amounts) — that is the legal agents' native language and would
# false-alarm on every FILE verdict.
LEAK_PATTERNS=$(python3 -c "import json,sys;print(json.load(open(sys.argv[1])).get('answer',''))" "$TOKENS_FILE" 2>/dev/null)
[ -z "$LEAK_PATTERNS" ] && LEAK_PATTERNS='bumgarner|timothy thompson|3:20-cv|\$250[,.]000|\$250k|2025-08-27|FAST_SANITY_PASS|SOURCE_CONFIRMED|"defendant_name":|"scheme_summary":|"all_active_years":|"npi_confidence":|"amount_usd":'
POST_HITS=$(grep -ilrE "$LEAK_PATTERNS" \
    --include='*.raw.json' --include='*.stderr.log' --include='*.parsed.json' --include='events.jsonl' \
    "$RUN_DIR" 2>/dev/null | head -5)
if [ -n "$POST_HITS" ]; then
    echo "POST-HOC LEAK in:"
    echo "$POST_HITS"
    exit 2
fi
echo "post-hoc: no leak patterns found"

exit $ORCH_EXIT
