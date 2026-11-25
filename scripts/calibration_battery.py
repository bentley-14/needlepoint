#!/usr/bin/env python3
"""Calibration battery — the quality ratchet (self-learning loop 3).

A fixed panel of known DOJ true-positives + confirmed-clean negative
controls, scored against every completed calibration run, with a committed
baseline: a prompt/pipeline change is acceptable ONLY if the battery does
not regress. This converts prompt-tweaking from vibes into measurement.

Metrics (per docs/calibration/CYPRESS_FALSE_POSITIVE_ANALYSIS.md):
    tp_file_rate          — TPs reaching a FILE-class verdict (want 1.0)
    control_file_rate     — controls reaching FILE-class (want 0.0 — the
                            false-positive firewall)
    evf_separation        — mean TP EVF − mean control EVF (want wide)
    control_dispositive_defeat_rate — controls killed on the MERITS (≥1
                            DISPOSITIVE_NOT_SURVIVED), not just on EVF
                            economics; the Cypress finding made this the
                            defense chain's health metric

Usage:
    python scripts/calibration_battery.py --report            # score existing runs
    python scripts/calibration_battery.py --ratchet           # exit 1 on regression vs baseline
    python scripts/calibration_battery.py --update-baseline   # accept current as baseline
    python scripts/calibration_battery.py --launch            # emit launch commands for stale/missing subjects

Manifest: validation/battery/battery.json (subjects, kinds, as_of dates).
Baseline: validation/battery/baseline.json (committed; the ratchet anchor).
Runs are read from fraud-investigator-v2/state/calibration/*/summary.json.

--launch only PRINTS commands (calibration runs need a top-level terminal;
see docs/INVESTIGATOR_SANDBOX_EVAL.md).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date
from pathlib import Path
from statistics import mean

REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST = REPO_ROOT / "validation" / "battery" / "battery.json"
BASELINE = REPO_ROOT / "validation" / "battery" / "baseline.json"

FILE_CLASS = {"SETTLEMENT_READY", "FILE", "FILE_WITH_DISCOVERY_DEPS"}
DECLINE_CLASS = {"NOT_ACTIONABLE", "COUNSEL_ABANDONED", "INDEFENSIBLE_AT_CAP",
                 "RECONSIDER"}
# Anything else (NEEDS_HUMAN, PENDING_REBUTTAL_CAP, None) = inconclusive.

# Two flavours of negative subject, kept as DISTINCT metrics on purpose:
#   negative_control        — anomalous-but-unprosecuted providers (Baobab, Laurel).
#                             A FILE here MIGHT be a true-but-uncharged detection,
#                             so control_file_rate can't prove a false positive.
#   adjudicated_true_negative — qui tams the govt DECLINED + a court DISMISSED on
#                             the merits. A FILE here IS a false positive — this is
#                             the defensible precision metric (built by cohort_builder.py).
# control_file_rate stays negative_control-only so the committed baseline stays
# comparable; adjudicated TNs get their own headline line.

# Regression tolerances for --ratchet.
TOLERANCES = {
    "tp_file_rate": 0.0,           # may not drop at all
    "control_file_rate": 0.0,      # may not rise at all
    "adjudicated_tn_file_rate": 0.0,  # may not rise — the precision firewall
    "evf_separation": 0.02,        # may shrink by at most this
    "control_dispositive_defeat_rate": 0.0,  # may not drop
    "theory_match_rate": 0.05,     # recall on the DOJ mechanism; small slack
}


# ── run collection ───────────────────────────────────────────────────────────

def _norm_name(s: str) -> str:
    """Normalize a subject/name for matching: lowercase, collapse non-alnum."""
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def collect_runs(root: Path, manifest: dict | None = None) -> dict[str, list[dict]]:
    """{npi: [run, …]} from completed calibration summaries, oldest→newest.

    Keys each run to an NPI by the 10-digit NPI in its subject string; if the
    subject has none (a TP without a subject_hint runs with the bare name), falls
    back to matching the subject to a manifest subject by normalized NAME — so a
    completed run isn't dropped as NO_RUN just because its subject lacked the NPI.
    """
    name_to_npi = {_norm_name(s["name"]): s["npi"]
                   for s in (manifest or {}).get("subjects", [])}
    runs: dict[str, list[dict]] = {}
    for p in sorted(root.glob("fraud-investigator-v2/state/calibration/"
                              "CALIBRATION-*/summary.json")):
        try:
            d = json.loads(p.read_text())
        except Exception:
            continue
        subj = d.get("subject") or ""
        m = re.search(r"(?<!\d)(\d{10})(?!\d)", subj)
        npi = m.group(1) if m else name_to_npi.get(_norm_name(subj))
        if not npi:
            continue
        n_dispositive = 0
        for rnd in d.get("adversarial_rounds") or []:
            lar = rnd.get("legal_adv_response") or {}
            for a in lar.get("attack_dispositions") or []:
                if a.get("disposition") == "DISPOSITIVE_NOT_SURVIVED":
                    n_dispositive += 1
        runs.setdefault(npi, []).append({
            "case_id": d.get("case_id"),
            "started": d.get("start_time"),
            "verdict": d.get("final_verdict"),
            "evf": d.get("expected_value_factor"),
            "strength": d.get("strength_score"),
            "strength_bucket": d.get("strength_bucket"),
            "n_dispositive_defeats": n_dispositive,
            "status": d.get("status"),
        })
    for v in runs.values():
        v.sort(key=lambda r: r.get("started") or "")
    return runs


# A run that died on infrastructure is NOT a result. It still writes
# final_verdict=NEEDS_HUMAN, so selecting on `verdict` alone let a crashed run
# DISPLACE the subject's last real verdict — and because NEEDS_HUMAN classes as
# INCONCLUSIVE rather than FILE, the displacement silently *improved* the
# metrics. The 2026-08-01 batch (8/8 dead on ENOTFOUND) dropped
# control_file_rate 0.333 -> 0.000, erasing the known Laurel false positive and
# making precision look perfect. Skip failed runs so the prior valid result stands.
FAILED_STATUS = {"agent_error", "error", "crashed"}

# Mirror the queue's staleness gate (run_calibration_queue.sh CAL_STALE_BEFORE):
# a run started before a schema/prompt change is not comparable. Without this,
# the 2026-08-16 report scored Magnolia by her STALE June SETTLEMENT_READY while
# her current-code run sat at a checkpoint — mixing code generations in one
# metric row. Same default the queue uses (falsity-anchor slot, 2026-06-11).
CAL_STALE_BEFORE = os.environ.get("CAL_STALE_BEFORE", "2026-06-11")


def latest_completed(runs: list[dict]) -> dict | None:
    done = [r for r in runs
            if r.get("verdict") and str(r.get("status")) not in FAILED_STATUS
            and not (CAL_STALE_BEFORE and str(r.get("started") or "")[:10] < CAL_STALE_BEFORE)]
    return done[-1] if done else None


# ── metrics (pure) ───────────────────────────────────────────────────────────

def outcome_class(verdict: str | None) -> str:
    if verdict in FILE_CLASS:
        return "FILE"
    if verdict in DECLINE_CLASS:
        return "DECLINE"
    return "INCONCLUSIVE"


def compute_metrics(manifest: dict, runs_by_npi: dict[str, list[dict]]) -> dict:
    rows = []
    for s in manifest["subjects"]:
        run = latest_completed(runs_by_npi.get(s["npi"], []))
        oc = outcome_class(run["verdict"]) if run else "NO_RUN"
        expected = "FILE" if s["kind"] == "true_positive" else "DECLINE"
        rows.append({
            **{k: s.get(k) for k in ("name", "npi", "kind", "as_of")},
            "case_id": run.get("case_id") if run else None,
            "verdict": run.get("verdict") if run else None,
            "evf": run.get("evf") if run else None,
            "n_dispositive_defeats": run.get("n_dispositive_defeats") if run else None,
            "outcome_class": oc,
            "expected": expected,
            "pass": oc == expected,
            "n_runs": len(runs_by_npi.get(s["npi"], [])),
        })

    def _rate(kind: str, cls: str) -> float | None:
        scored = [r for r in rows if r["kind"] == kind
                  and r["outcome_class"] in ("FILE", "DECLINE")]
        return (sum(1 for r in scored if r["outcome_class"] == cls) / len(scored)
                if scored else None)

    def _dispositive_defeat_rate(kind: str) -> float | None:
        scored = [r for r in rows if r["kind"] == kind
                  and r["outcome_class"] in ("FILE", "DECLINE")]
        return (sum(1 for r in scored if (r["n_dispositive_defeats"] or 0) >= 1)
                / len(scored) if scored else None)

    tp_evfs = [r["evf"] for r in rows if r["kind"] == "true_positive"
               and r["evf"] is not None]
    ctl_evfs = [r["evf"] for r in rows if r["kind"] == "negative_control"
                and r["evf"] is not None]
    return {
        "generated": date.today().isoformat(),
        "n_subjects": len(rows),
        "n_no_run": sum(1 for r in rows if r["outcome_class"] == "NO_RUN"),
        "n_inconclusive": sum(1 for r in rows if r["outcome_class"] == "INCONCLUSIVE"),
        "n_adjudicated_tn": sum(1 for r in rows if r["kind"] == "adjudicated_true_negative"),
        "tp_file_rate": _rate("true_positive", "FILE"),
        "control_file_rate": _rate("negative_control", "FILE"),
        "adjudicated_tn_file_rate": _rate("adjudicated_true_negative", "FILE"),
        "evf_separation": (mean(tp_evfs) - mean(ctl_evfs)
                           if tp_evfs and ctl_evfs else None),
        "control_dispositive_defeat_rate": _dispositive_defeat_rate("negative_control"),
        "adjudicated_tn_dispositive_defeat_rate": _dispositive_defeat_rate("adjudicated_true_negative"),
        "subjects": rows,
    }


def ratchet_check(current: dict, baseline: dict,
                  tolerances: dict = TOLERANCES) -> list[str]:
    """Return regression messages ([] = pass). None-valued metrics on either
    side are skipped (can't regress against nothing)."""
    problems = []
    direction = {  # +1 = higher is better, -1 = lower is better
        "tp_file_rate": +1, "control_file_rate": -1,
        "adjudicated_tn_file_rate": -1, "theory_match_rate": +1,
        "evf_separation": +1, "control_dispositive_defeat_rate": +1,
    }
    for metric, sign in direction.items():
        cur, base = current.get(metric), baseline.get(metric)
        if cur is None or base is None:
            continue
        tol = tolerances.get(metric, 0.0)
        delta = (cur - base) * sign
        if delta < -tol - 1e-9:
            problems.append(
                f"{metric}: {base:.3f} → {cur:.3f} "
                f"({'drop' if sign > 0 else 'rise'} beyond tolerance {tol})")
    return problems


# ── rendering / launching ────────────────────────────────────────────────────

def render_md(m: dict, ratchet_problems: list[str] | None = None) -> str:
    f = lambda v: "—" if v is None else (f"{v:.3f}" if isinstance(v, float) else str(v))
    lines = [
        f"# Calibration battery — {m['generated']}",
        "",
        f"| metric | value | want |",
        f"|---|---|---|",
        f"| tp_file_rate | {f(m['tp_file_rate'])} | 1.000 |",
        f"| control_file_rate (anomalous) | {f(m['control_file_rate'])} | 0.000 |",
        f"| **adjudicated_tn_file_rate** | {f(m.get('adjudicated_tn_file_rate'))} | 0.000 (the precision firewall — declined/dismissed qui tams) |",
        f"| theory_match_rate | {f(m.get('theory_match_rate'))} | high (pled the DOJ mechanism) |",
        f"| evf_separation | {f(m['evf_separation'])} | wide |",
        f"| control_dispositive_defeat_rate | {f(m['control_dispositive_defeat_rate'])} | 1.000 (merits-layer kills, not EVF economics) |",
        f"| adjudicated_tn_dispositive_defeat_rate | {f(m.get('adjudicated_tn_dispositive_defeat_rate'))} | 1.000 (die on the merits) |",
        "",
        f"subjects: {m['n_subjects']} · no-run: {m['n_no_run']} · "
        f"inconclusive: {m['n_inconclusive']}",
        "",
        "| subject | kind | verdict | EVF | dispositive kills | pass |",
        "|---|---|---|---|---|---|",
    ]
    for r in m["subjects"]:
        lines.append(
            f"| {r['name']} ({r['npi']}) | {r['kind']} | {r['verdict'] or '—'} "
            f"| {f(r['evf'])} | {r['n_dispositive_defeats'] if r['n_dispositive_defeats'] is not None else '—'} "
            f"| {'✓' if r['pass'] else '✗ (' + r['outcome_class'] + ')'} |")
    if ratchet_problems is not None:
        lines += ["", "## Ratchet vs baseline",
                  "" if ratchet_problems else "PASS — no regression."]
        for p in ratchet_problems:
            lines.append(f"- REGRESSION: {p}")
    return "\n".join(lines)


def launch_commands(manifest: dict, runs_by_npi: dict,
                    profile: str | None = None) -> list[str]:
    """``profile`` prefixes each command with FRAUD_INV_PROFILE=<profile> so the
    smoke runner (and the orchestrator it spawns) runs under that profile —
    e.g. ``battery`` = production model split + the REAL defense red team
    (an un-red-teamed FILE is unvalidated). None keeps the smoke runner's
    own default (draft)."""
    env_prefix = f"FRAUD_INV_PROFILE={profile} " if profile else ""
    cmds = ["# Run from a TOP-LEVEL terminal (calibration spawns claude -p).",
            "# Sequential on purpose — don't run battery subjects concurrently."]
    for s in manifest["subjects"]:
        run = latest_completed(runs_by_npi.get(s["npi"], []))
        tag = "RERUN (stale)" if run else "MISSING"
        if not s.get("as_of"):
            cmds.append(f"# {s['name']}: no as_of in manifest — fill it in first")
            continue
        cmds.append(
            f"# {s['name']} [{s['kind']}] — {tag}\n"
            f"{env_prefix}bash scripts/calibration/run_calibration_smoke.sh "
            f"{s['npi']} {s['as_of']} \"{s.get('subject_hint') or s['name']}\"")
    return cmds


def _inject_theory_match(metrics: dict, root: Path) -> None:
    """Fold the post-hoc DOJ-mechanism recall score (scripts/theory_match.py) into
    the metrics dict — recall on WHETHER the loop pled the actual DOJ fraud
    mechanism, not just whether it filed. Leak-safe: theory_match reads the answer
    key (doj_theory.json) OUTSIDE the sandbox, never feeding it to the loop.
    Degrades to None if unavailable (missing doj_theory.json / import error) so
    the battery report stays robust."""
    try:
        here = str(Path(__file__).resolve().parent)
        if here not in sys.path:
            sys.path.insert(0, here)
        import theory_match  # type: ignore  (sibling script in scripts/)
        rep = theory_match.score(root / "fraud-investigator-v2/state/calibration")
        metrics["theory_match_rate"] = rep.get("theory_match_rate")
        metrics["theory_match_n_match"] = rep.get("n_match")
        metrics["theory_match_n_scoreable"] = rep.get("n_scoreable")
    except Exception as e:
        metrics.setdefault("theory_match_rate", None)
        metrics["theory_match_error"] = f"{type(e).__name__}: {e}"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--report", action="store_true")
    g.add_argument("--ratchet", action="store_true",
                   help="exit 1 if any metric regresses vs the committed baseline")
    g.add_argument("--update-baseline", action="store_true")
    g.add_argument("--launch", action="store_true",
                   help="print launch commands for missing/stale subjects")
    ap.add_argument("--profile", default=None,
                    help="FRAUD_INV_PROFILE for --launch commands (e.g. "
                         "'battery' = production model split + REAL defense "
                         "red team). Default: smoke runner's own default (draft).")
    ap.add_argument("--root", type=Path, default=REPO_ROOT)
    ap.add_argument("--manifest", type=Path, default=None)
    ap.add_argument("--baseline", type=Path, default=None)
    ap.add_argument("--format", choices=("md", "json"), default="md")
    args = ap.parse_args(argv)

    manifest_path = args.manifest or (args.root / "validation/battery/battery.json")
    baseline_path = args.baseline or (args.root / "validation/battery/baseline.json")
    manifest = json.loads(manifest_path.read_text())
    runs = collect_runs(args.root, manifest)

    if args.launch:
        print("\n".join(launch_commands(manifest, runs, profile=args.profile)))
        return 0

    metrics = compute_metrics(manifest, runs)
    _inject_theory_match(metrics, args.root)

    if args.update_baseline:
        slim = {k: v for k, v in metrics.items() if k != "subjects"}
        baseline_path.parent.mkdir(parents=True, exist_ok=True)
        baseline_path.write_text(json.dumps(slim, indent=2))
        print(f"baseline updated → {baseline_path}")
        print(render_md(metrics))
        return 0

    problems = None
    rc = 0
    if args.ratchet:
        if baseline_path.exists():
            problems = ratchet_check(metrics, json.loads(baseline_path.read_text()))
            rc = 1 if problems else 0
        else:
            problems = [f"no baseline at {baseline_path} — run --update-baseline first"]
            rc = 1

    if args.format == "json":
        out = {k: v for k, v in metrics.items()}
        if problems is not None:
            out["ratchet_problems"] = problems
        print(json.dumps(out, indent=2, default=str))
    else:
        print(render_md(metrics, problems))
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
