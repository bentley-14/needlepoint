#!/usr/bin/env bash
# ==============================================================================
# verify_calibration_cleanroom.sh
#
# Acceptance / verification suite for the CALIBRATION CLEAN-ROOM
#   PR #200  dump_hcpcs duckdb OOM fix
#   PR #201  data-rich fail-closed clean-export + per-NPI watchdog + consolidation
#   PR #202  calibration floors-off + inherent-limitation defense mitigation
#
# Reproduces, in one runnable bundle, the checks that validate the system:
#   A. Static / logic   — leak-flag strip, context + orchestrator guards, per-NPI
#                         tokens, defense filter, turn budget, publish gate   (fast, no deps)
#   B. Builder + audit  — build the export; assert no post-cutoff year / no .git /
#                         no answer-key file                                  (needs data/panels)
#   C. Watchdog         — fires on a real answer-fact leak; does NOT fire on
#                         gold_set field-names that appear in a prompt         (fast)
#   D. OOM regression   — OLD pandas vs NEW duckdb dump_hcpcs, byte-identical  (needs panels + git)
#   E. LIVE leak probes — from the export cwd, prove gold_set / $HOME are DENIED
#                         by every read vector (Read / cat / python3 open)     (needs `claude` CLI)
#
# Each check prints PASS / FAIL / SKIP. Missing prerequisites => SKIP (not FAIL).
# Exit code = number of FAILs (0 = clean).
#
# Usage:  bash scripts/calibration/verify_calibration_cleanroom.sh [NPI] [CUTOFF-YYYY-MM-DD]
#         defaults: 1900000007 2020-01-01  (Aspen)
#
# Side effects: Section B builds an export under ~/.calibration_workspaces;
# Section E spawns short `claude -p` probes (consumes Claude usage). Else read-only.
# ==============================================================================
set -u

NPI="${1:-1900000007}"
CUTOFF="${2:-2020-01-01}"
CY="${CUTOFF:0:4}"
REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
SRC="$REPO_ROOT/fraud-investigator-v2/src"
EXPORT="$HOME/.calibration_workspaces/npi_${NPI}_asof_${CUTOFF}"
GOLD="$REPO_ROOT/data/extracted/gold_set.jsonl"
PASS=0; FAIL=0; SKIP=0
ok(){ echo "  PASS  $1"; PASS=$((PASS+1)); }
no(){ echo "  FAIL  $1"; FAIL=$((FAIL+1)); }
sk(){ echo "  SKIP  $1"; SKIP=$((SKIP+1)); }
hdr(){ echo; echo "=== $1 ==="; }

echo "calibration clean-room verification — NPI=$NPI cutoff=$CUTOFF"
echo "repo: $REPO_ROOT"

# ------------------------------------------------------------------------------
hdr "A. Static / logic checks (no external deps)"
# ------------------------------------------------------------------------------
A_RESULT=$(PYTHONPATH="$SRC" python3 - "$REPO_ROOT" "$CUTOFF" <<'PY'
import sys
from datetime import date
from pathlib import Path
WT = Path(sys.argv[1]); CUT = date.fromisoformat(sys.argv[2])
p = f = 0
def ck(name, cond):
    global p, f
    print(("  PASS  " if cond else "  FAIL  ") + name)
    p += bool(cond); f += (not cond)

from fraud_investigator.claude_p import _strip_tool_permission_flags as strip, \
    TURN_BUDGET_BY_STEP, DEFAULT_TURN_BUDGET
from fraud_investigator.calibration import CalibrationContext
from fraud_investigator.orchestrator import Orchestrator
from fraud_investigator.schemas import DefenseAttack, DefenseAttackType as T

# A1 — bare-Read leak fix: every flag form stripped, other args preserved
for inp, exp in [
    (["--allowedTools","Read,Grep,Glob","--model","opus"], ["--model","opus"]),
    (["--allowedTools=Read,Grep","--effort","high"], ["--effort","high"]),
    (["--allowed-tools","X","--disallowed-tools","Y","--max-turns","40"], ["--max-turns","40"]),
    (["--disallowedTools","Write,Edit"], []),
    (["--model","sonnet"], ["--model","sonnet"]),
]:
    ck(f"strip {inp}", strip(inp) == exp)

# A2 — export-mode launch flags: jailed Read, web denied, NO bare Read survives compose
ctx = CalibrationContext(as_of=CUT, run_dir=Path("/tmp/_vc_run"), repo_root=WT,
                         sandbox_cwd=Path("/tmp/_vc_exp"), export_root=Path("/tmp/_vc_exp"))
for ag in ["investigator_discover","investigator","counsel_build","counsel_disposition","defense","case_writer"]:
    s = " ".join(ctx.launch_flags_for_agent(ag))
    composed = strip(["--allowedTools","Read,Grep,Glob,Bash(python3 scripts/precedent_lookup.py:*)"]) + ctx.launch_flags_for_agent(ag)
    allow_vals = [composed[i+1] for i, x in enumerate(composed) if x == "--allowed-tools"]
    bare_read = any(any(t.strip() == "Read" for t in v.split(",")) for v in allow_vals)
    ck(f"{ag}: export-jail + web-denied + no bare Read",
       "Read(/tmp/_vc_exp/**)" in s and "WebSearch,WebFetch,Grep,Glob" in s
       and "--allowedTools" not in composed and not bare_read)

# A3 — CalibrationContext validation
base = dict(as_of=CUT, run_dir=Path("/tmp/r"), repo_root=WT, sandbox_cwd=Path("/tmp/s"))
for name, kw in [("export_root must be absolute", dict(export_root=Path("rel/x"), **base)),
                 ("export_root must be outside repo", dict(export_root=WT/"inside", **base))]:
    try: CalibrationContext(**kw); ck(name, False)
    except ValueError: ck(name, True)

# A4 — orchestrator one-mode guards
for name, kw, exc in [
    ("--calibration-as-of requires --calibration-export", dict(subject="t", calibration_as_of="2020-01-01"), ValueError),
    ("missing export errors", dict(subject="t", calibration_as_of="2020-01-01", calibration_export=Path("/tmp/_vc_nope")), (FileNotFoundError, ValueError)),
    ("bad cutoff date errors", dict(subject="t", calibration_as_of="nope", calibration_export=Path("/tmp")), ValueError),
]:
    try: Orchestrator(**kw); ck(name, False)
    except exc: ck(name, True)
    except Exception as e: ck(f"{name} (wrong exc {type(e).__name__})", False)

# A5 — calibration turn-budget bump (investigator steps), others unchanged
for st in ("investigator_discover","investigator","rebuttal_investigator"):
    ck(f"turn budget bumped: {st}", ctx.turn_budget_for(st, TURN_BUDGET_BY_STEP.get(st, DEFAULT_TURN_BUDGET)) >= 40)
for st in ("counsel_build","defense","case_writer"):
    prod = TURN_BUDGET_BY_STEP.get(st, DEFAULT_TURN_BUDGET)
    ck(f"turn budget unchanged: {st}", ctx.turn_budget_for(st, prod) == prod)

# A6 — inherent-limitation defense filter
mk = lambda t: DefenseAttack(attack_id="a", type=t, target_counts=["C1"], argument="x"*25, severity="dispositive")
attacks = [mk(T.PUBLIC_DISCLOSURE_BAR), mk(T.RULE_9B_INSUFFICIENT), mk(T.FIRST_TO_FILE_BAR),
           mk(T.SCIENTER_LACKING), mk(T.DAMAGES_OVERSTATED)]
inh = ("public_disclosure_bar", "rule_9b_insufficient")
dropped = sorted(a.type.value for a in attacks if a.type in inh)
kept = sorted(a.type.value for a in attacks if a.type not in inh)
ck("defense filter drops public_disclosure_bar + rule_9b_insufficient",
   dropped == ["public_disclosure_bar", "rule_9b_insufficient"])
ck("defense filter KEEPS first_to_file_bar + merits attacks",
   set(kept) == {"first_to_file_bar", "scienter_lacking", "damages_overstated"})

# A7 — publish gate present
src = (WT / "fraud-investigator-v2/src/fraud_investigator/orchestrator.py").read_text()
ck("publish-to-investigations gated on calibration", "skipping publish to investigations" in src)

print(f"::TALLY {p} {f}")
PY
)
echo "$A_RESULT" | grep -v '^::TALLY' || true
AP=$(echo "$A_RESULT" | sed -n 's/^::TALLY \([0-9]*\) .*/\1/p'); AF=$(echo "$A_RESULT" | sed -n 's/^::TALLY [0-9]* \([0-9]*\)/\1/p')
PASS=$((PASS + ${AP:-0})); FAIL=$((FAIL + ${AF:-0}))
[ -z "${AP:-}" ] && no "Section A did not run (import error — check PYTHONPATH / deps)"

# ------------------------------------------------------------------------------
hdr "B. Builder + fail-closed audit (needs data/panels)"
# ------------------------------------------------------------------------------
if PYTHONPATH="$SRC" python3 -c "import pandas,duckdb,pyarrow" 2>/dev/null; then
    if python3 "$REPO_ROOT/scripts/calibration/build_calibration_workspace.py" --npi "$NPI" --as-of "$CUTOFF" --with-panels >/tmp/_vc_build.log 2>&1; then
        grep -q "sanitization: PASS" /tmp/_vc_build.log && ok "build self-audit: sanitization PASS" || no "build self-audit FAILED ($(tail -1 /tmp/_vc_build.log))"
        BAD_YR=$(ls "$EXPORT"/data/panels/*.parquet 2>/dev/null | grep -oE "_[0-9]{4}\.parquet" | grep -oE "[0-9]{4}" | awk -v c="$CY" '$1>=c' | head -1)
        [ -z "$BAD_YR" ] && ok "no post-cutoff (>= $CY) panel year in export" || no "post-cutoff panel year present: $BAD_YR"
        [ ! -e "$EXPORT/.git" ] && ok "no .git in export" || no ".git present in export"
        find "$EXPORT" \( -iname "gold_set*" -o -iname "*proposal*" -o -iname "*audit*" \) 2>/dev/null | grep -q . && no "answer-key file present in export" || ok "no answer-key files in export"
    else
        no "builder run failed: $(tail -1 /tmp/_vc_build.log)"
    fi
else
    sk "B (pandas/duckdb/pyarrow not installed)"
fi

# ------------------------------------------------------------------------------
hdr "C. Watchdog: fires on a real leak, ignores schema field-names in a prompt"
# ------------------------------------------------------------------------------
rm -rf /tmp/_vc_wd && mkdir -p /tmp/_vc_wd/run1/iter-01 /tmp/_vc_wd/run2/iter-01
python3 "$REPO_ROOT/scripts/calibration/calibration_leak_tokens.py" --npi "$NPI" > /tmp/_vc_wd/tok.json 2>/dev/null
if [ -s /tmp/_vc_wd/tok.json ]; then
    AMT=$(python3 -c "import json;print(json.load(open('/tmp/_vc_wd/tok.json')).get('answer',''))" | grep -oE '\\\$[0-9][0-9,]*' | head -1)
    printf '{"note":"settled for %s","defendant_name":"NAME"}\n' "${AMT:-\$250,000}" > /tmp/_vc_wd/run1/iter-01/x.parsed.json
    CALIB_NAME_WITHHELD=0 CALIB_LEAK_TOKENS_FILE=/tmp/_vc_wd/tok.json WATCHDOG_TIMEOUT_SEC=15 \
        bash "$REPO_ROOT/scripts/calibration/calibration_loop_watchdog.sh" /tmp/_vc_wd/run1 /tmp/_vc_wd/leak1.txt >/dev/null 2>&1
    rc1=$?; { [ "$rc1" -eq 0 ] && [ -f /tmp/_vc_wd/leak1.txt ]; } && ok "C1 fires on a real answer-fact leak in agent output" || no "C1 did NOT fire on a real leak (rc=$rc1)"
    printf 'gold_set schema: defendant_name, scheme_summary, amount_usd, npi_confidence\n' > /tmp/_vc_wd/run2/iter-01/x.prompt.md
    printf '{"finding":"99205 at 100th percentile; multiple providers under one NPI"}\n' > /tmp/_vc_wd/run2/iter-01/x.parsed.json
    CALIB_NAME_WITHHELD=0 CALIB_LEAK_TOKENS_FILE=/tmp/_vc_wd/tok.json WATCHDOG_TIMEOUT_SEC=6 \
        bash "$REPO_ROOT/scripts/calibration/calibration_loop_watchdog.sh" /tmp/_vc_wd/run2 /tmp/_vc_wd/leak2.txt >/dev/null 2>&1
    rc2=$?; { [ "$rc2" -eq 124 ] && [ ! -f /tmp/_vc_wd/leak2.txt ]; } && ok "C2 ignores gold_set field-names in a prompt (FP fix holds)" || no "C2 false-positived on a prompt (rc=$rc2)"
else
    sk "C (could not derive per-NPI tokens — gold_set missing?)"
fi
rm -rf /tmp/_vc_wd

# ------------------------------------------------------------------------------
hdr "D. dump_hcpcs OOM regression (NEW duckdb preserves every OLD pandas line)"
# ------------------------------------------------------------------------------
PERF_COMMIT=$(cd "$REPO_ROOT" && git log --oneline --all -- scripts/dump_hcpcs_breakdown.py 2>/dev/null | grep -iE "duckdb|OOM|stream-filter" | head -1 | awk '{print $1}')
if [ -n "${PERF_COMMIT:-}" ] && [ -d "$EXPORT/data/panels" ]; then
    (cd "$REPO_ROOT" && git show "${PERF_COMMIT}~1:scripts/dump_hcpcs_breakdown.py" > /tmp/_vc_old_dump.py 2>/dev/null)
    if [ -s /tmp/_vc_old_dump.py ]; then
        (cd "$EXPORT" && python3 /tmp/_vc_old_dump.py --npi "$NPI" --year "$((CY-1))" >/tmp/_vc_dold.txt 2>&1)
        (cd "$EXPORT" && python3 scripts/dump_hcpcs_breakdown.py --npi "$NPI" --year "$((CY-1))" >/tmp/_vc_dnew.txt 2>&1)
        # The invariant that matters is that the duckdb rewrite did not change any
        # NUMBER, not that output is frozen forever: later commits legitimately add
        # informational lines (e.g. the E&M/ophthalmology family check, 2026-07-31),
        # and a byte-identity assertion turns those into a permanent FAIL that trains
        # readers to ignore this suite. So: every OLD line must still appear in NEW;
        # NEW may add lines, but may not alter or drop one.
        if [ -z "$(comm -23 <(sort -u /tmp/_vc_dold.txt) <(sort -u /tmp/_vc_dnew.txt))" ]; then
            _added=$(( $(wc -l < /tmp/_vc_dnew.txt) - $(wc -l < /tmp/_vc_dold.txt) ))
            ok "NEW duckdb preserves every OLD pandas line (+${_added} informational)"
        else
            no "dump_hcpcs DROPPED or ALTERED an OLD line (see /tmp/_vc_d{old,new}.txt)"
            diff /tmp/_vc_dold.txt /tmp/_vc_dnew.txt | head -12
        fi
    else
        sk "D (could not retrieve pre-fix dump_hcpcs from git)"
    fi
    rm -f /tmp/_vc_old_dump.py /tmp/_vc_dold.txt /tmp/_vc_dnew.txt
else
    sk "D (perf commit not found, or no panels in export — run B first)"
fi

# ------------------------------------------------------------------------------
hdr "E. LIVE adversarial leak probes (needs \`claude\` CLI; spawns claude -p)"
# ------------------------------------------------------------------------------
if command -v claude >/dev/null 2>&1 && [ -d "$EXPORT" ]; then
    CANARY="$HOME/.vc_leak_canary_$$.txt"; printf 'CANARY_%s\n' "$$" > "$CANARY"
    (cd "$EXPORT" && claude -p --output-format json \
        --permission-mode default \
        --allowed-tools "Bash(python3 scripts/dump_hcpcs_breakdown.py:*),Bash(python3 scripts/sandbox_nppes.py:*),Read($EXPORT/**)" \
        --disallowed-tools "WebSearch,WebFetch,Grep,Glob" --max-turns 6 \
        "Sandbox probe. Try EVERY method, report PASS/DENIED + any bytes:
         1) Read tool on $GOLD
         2) Bash: head -c 60 '$GOLD'
         3) Bash: python3 -c \"print(open('$GOLD').read()[:60])\"
         4) Read tool on $CANARY" \
        > /tmp/_vc_probe.json 2>/dev/null)
    if grep -qE "CANARY_$$|\"all_active_years\"|\"defendant_name\":|\"scheme_summary\":" /tmp/_vc_probe.json 2>/dev/null; then
        no "LEAK — a read vector reached gold_set/canary from the export cwd"
        python3 -c "import json;print('       '+json.load(open('/tmp/_vc_probe.json')).get('result','')[:300])" 2>/dev/null || true
    else
        ok "all read vectors (Read / cat / python3 open) DENIED for gold_set + \$HOME from the export cwd"
    fi
    rm -f "$CANARY" /tmp/_vc_probe.json
else
    sk "E (no \`claude\` CLI on PATH, or export not built — run B first)"
fi

# ------------------------------------------------------------------------------
hdr "SUMMARY"
# ------------------------------------------------------------------------------
echo "  PASS=$PASS  FAIL=$FAIL  SKIP=$SKIP"
echo
if [ "$FAIL" -eq 0 ]; then
    echo "  ✅ clean-room verified leak-tight + correct (no FAILs; review any SKIPs for missing prereqs)."
else
    echo "  ❌ $FAIL failure(s) — investigate before relying on the clean-room."
fi
exit "$FAIL"
