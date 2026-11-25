#!/usr/bin/env bash
# Multi-stream leak watchdog for calibration-loop runs.
#
# Tails every file in the run's state dir (each agent writes its own
# *.raw.json + *.stderr.log under iter-NN/ and adv-NN/ subdirs); on any
# leak-pattern hit, writes a marker file with the matched line, the file
# it came from, and a timestamp. The orchestrator can be killed manually
# from outside, or the smoke runner can poll the marker file.
#
# Usage:
#   bash scripts/calibration_loop_watchdog.sh <run-state-dir> [<leak-marker-path>]
#
# Example:
#   bash scripts/calibration_loop_watchdog.sh \
#       fraud-investigator-v2/state/calibration/CALIBRATION-AB12CD34 \
#       /tmp/needlepoint_loop_leak.txt &
#
# Exit codes:
#   0 — leak detected (marker written)
#   124 — timeout reached without leak (default 60min wall)
#   1 — bad args

set -u

if [ $# -lt 1 ]; then
    echo "Usage: $0 <run-state-dir> [<leak-marker-path>]" >&2
    exit 1
fi

RUN_DIR="$1"
LEAK="${2:-/tmp/needlepoint_loop_leak.txt}"
TIMEOUT_SEC="${WATCHDOG_TIMEOUT_SEC:-3600}"

if [ ! -d "$RUN_DIR" ]; then
    echo "Run dir does not exist: $RUN_DIR" >&2
    exit 1
fi

# Leak patterns (extended regex, case-insensitive).
# Three classes:
#   1) Defendant-named identifiers: defendant's surname/practice/brand/URL
#      pieces — should be EMPTY for non-Aspen runs, but kept here for the
#      Aspen smoke baseline. Edit per-target as the calibration set grows.
#   2) Settlement-specific numbers / dates / docket / prosecutor names — these
#      are post-cutoff facts that should never appear in agent output.
#   3) Gold_set field names — if any agent's stream contains these field
#      names, the agent definitionally read gold_set (which the launch-flag
#      Read-allowlist should prevent — this is the catch-net).
# Two classes:
#   ANSWER — post-cutoff / answer-key facts that must NEVER appear in ANY mode
#     (settlement amount + date, docket, prosecutors, sealed qui tam, and gold_set
#     field names that only exist if the agent actually read gold_set). In a
#     fail-closed export these can ONLY appear via model-recall (training-data
#     recognition) or a sandbox escape — so ANSWER hits are the real signal.
#   NAME — defendant/practice/WDKY-PR-URL fragments. A leak ONLY when the name is
#     WITHHELD from the agent (legacy empty-sandbox mode). In export mode the name
#     is given in the subject string (public info, not the answer), so flagging it
#     would false-positive on legitimate usage.
# Per-NPI generalization: if CALIB_LEAK_TOKENS_FILE points at the JSON emitted
# by scripts/calibration_leak_tokens.py (answer/name regexes derived from THIS
# NPI's gold_set record at setup time), use it — so the watchdog works for any
# target, not just Aspen. Otherwise fall back to the built-in Aspen set.
ANSWER_PATTERNS=''
NAME_PATTERNS=''
if [ -n "${CALIB_LEAK_TOKENS_FILE:-}" ] && [ -f "${CALIB_LEAK_TOKENS_FILE}" ]; then
    ANSWER_PATTERNS=$(python3 -c "import json,sys;print(json.load(open(sys.argv[1])).get('answer',''))" "$CALIB_LEAK_TOKENS_FILE" 2>/dev/null)
    NAME_PATTERNS=$(python3 -c "import json,sys;print(json.load(open(sys.argv[1])).get('name',''))" "$CALIB_LEAK_TOKENS_FILE" 2>/dev/null)
fi
if [ -z "$ANSWER_PATTERNS" ]; then
    # Built-in fallback (Aspen hard answer facts + gold_set field names in
    # JSON-KEY form). NO generic FCA terms (qui tam/relator) — those are the legal
    # agents' native vocabulary and caused false positives; field names use the
    # "key": form so they match a gold_set record dump, not prose.
    ANSWER_PATTERNS='bumgarner|timothy thompson|3:20-cv|\$250[,.]000|\$250k|2025-08-27|FAST_SANITY_PASS|SOURCE_CONFIRMED|"defendant_name":|"scheme_summary":|"all_active_years":|"npi_confidence":|"amount_usd":'
    NAME_PATTERNS='aspen|lpcmh|louisville patient centered|openaccesshealthcarepsc|open access healthcare|justice.gov/usao-wdky/pr/louisville'
fi
# CALIB_NAME_WITHHELD=1 (default; legacy mode): name hidden -> name usage IS a leak.
# CALIB_NAME_WITHHELD=0 (export mode): name provided in subject -> only ANSWER tokens leak.
if [ "${CALIB_NAME_WITHHELD:-1}" = "0" ] || [ -z "$NAME_PATTERNS" ]; then
    LEAK_PATTERNS="$ANSWER_PATTERNS"
else
    LEAK_PATTERNS="${ANSWER_PATTERNS}|${NAME_PATTERNS}"
fi

rm -f "$LEAK"
START=$(date +%s)
echo "watchdog: monitoring $RUN_DIR for leak patterns (timeout ${TIMEOUT_SEC}s)"
echo "watchdog: marker file = $LEAK"

# Full-scan-each-iter: every 3s, recursively grep ALL relevant files for the
# leak patterns. For a typical run (30 agent steps × ~50KB each = ~1.5MB total)
# this is ~150ms per scan — negligible compared to the run wall-clock. Simpler
# and more robust than maintaining offset state across iterations.
while true; do
    NOW=$(date +%s)
    ELAPSED=$((NOW - START))
    if [ $ELAPSED -gt "$TIMEOUT_SEC" ]; then
        echo "watchdog: timeout reached (${ELAPSED}s); no leak detected"
        exit 124
    fi

    # grep -lr returns paths of files containing a match. -I skips binary.
    # --include limits to the file types agent output lands in.
    # Scan AGENT OUTPUT only. We do NOT scan *.prompt.md: prompts are
    # orchestrator-authored input, and the base investigator prompt legitimately
    # DOCUMENTS the gold_set schema (field names like defendant_name / scheme_summary)
    # — matching those would false-positive on our own documentation, not a leak.
    # A genuine leak manifests in what the agent PRODUCES (raw/parsed/events), and
    # any leaked discover finding that flows into a later prompt is already present
    # in that discover step's own parsed.json.
    HIT=$(grep -ilrE "$LEAK_PATTERNS" \
            --include='*.raw.json' \
            --include='*.stderr.log' \
            --include='*.parsed.json' \
            --include='events.jsonl' \
            "$RUN_DIR" 2>/dev/null | head -1)

    if [ -n "$HIT" ]; then
        TS=$(date '+%Y-%m-%d %H:%M:%S')
        LINE=$(grep -iE "$LEAK_PATTERNS" "$HIT" | head -1)
        MATCH=$(echo "$LINE" | grep -iEo "$LEAK_PATTERNS" | head -1)
        {
            echo "=== LEAK DETECTED at $TS ==="
            echo "file: $HIT"
            echo "matched token: $MATCH"
            echo "matched line (first 300 chars):"
            echo "$LINE" | head -c 300
            echo
        } > "$LEAK"
        echo "watchdog: LEAK in $HIT — exiting 0 (marker written to $LEAK)"
        exit 0
    fi

    sleep 3
done
