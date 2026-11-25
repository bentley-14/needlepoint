"""Cheap-replay loop — recompute the battery's verdicts from FROZEN cached runs.

`verdict_replay.py` recomputes ONE case's verdict from a saved summary.json in
milliseconds (no agents). This generalizes it into a regression loop over the
whole battery: HARVEST a completed run's summary + its expected label into a
frozen in-repo fixture, then REPLAY every fixture through the CURRENT
`compute_verdict` / `build_scorecard` and assert the verdict still matches.

Why it matters: the verdict/scoring logic (verdict.py, scoring.py thresholds,
build_scorecard, discounts) is a pure function of the agents' persisted
conclusions (falsity anchors, foreclosure ledger, adversarial dispositions,
counts, EVF — all in summary.json). So you can iterate on that logic and
re-check the entire battery in ms, with NO live re-runs and NO network — and a
change that flips a frozen case is caught immediately. This is what
`test_verdict_replay.py` did, but it SKIPPED without live state dirs; frozen
fixtures make the regression net hermetic + portable (CI-able).

What it does NOT cover: prompt changes (those change the agent OUTPUT, so they
need a live re-run — the deterministic post-agent layer is what replays cheaply).

Usage::

    # Harvest every terminal battery run into fixtures (run where the runs live):
    python scripts/replay_battery.py --harvest-battery

    # Harvest one run explicitly:
    python scripts/replay_battery.py --harvest <state_dir> \
        --subject "Provider Aspen" --expected FILE

    # Replay all frozen fixtures through the current logic (report):
    python scripts/replay_battery.py --report

    # CI gate — exit 1 if any frozen case now decides differently:
    python scripts/replay_battery.py --check
"""
from __future__ import annotations

import argparse
import glob
import json
import re
import sys
from datetime import date
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))  # so `import verdict_replay` works either way

from verdict_replay import replay_summary, is_file  # noqa: E402

REPO_ROOT = _HERE.parent
MANIFEST = REPO_ROOT / "validation" / "battery" / "battery.json"
FIXTURES = REPO_ROOT / "validation" / "replay" / "fixtures"
_CAL_GLOB = "fraud-investigator-v2/state/calibration/CALIBRATION-*/summary.json"
_TERMINAL = {"complete", "case_ready", "abandoned"}


def expected_for_kind(kind: str) -> str:
    """A true_positive should FILE; every negative subject should DECLINE."""
    return "FILE" if kind == "true_positive" else "DECLINE"


def verdict_class(verdict) -> str:
    """Map a FinalVerdict → FILE | DECLINE | OTHER:<value> (the comparable label)."""
    if is_file(verdict):
        return "FILE"
    if verdict.value in ("NOT_ACTIONABLE", "COUNSEL_ABANDONED", "INDEFENSIBLE_AT_CAP"):
        return "DECLINE"
    return f"OTHER:{verdict.value}"


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (s or "").lower()).strip("_")[:60]


# ── harvest ──────────────────────────────────────────────────────────────────

def harvest(state_dir: Path, subject: str, expected: str, *,
            npi: str = "", kind: str = "", fixtures_dir: Path = FIXTURES) -> Path:
    """Freeze a completed run's summary.json + its expected verdict into a fixture."""
    summary = json.loads((state_dir / "summary.json").read_text())
    fixtures_dir.mkdir(parents=True, exist_ok=True)
    fx = {
        "subject": subject,
        "npi": npi or "",
        "kind": kind or "",
        "expected": expected,
        "source_case_id": summary.get("case_id"),
        "harvested": date.today().isoformat(),
        "summary": summary,
    }
    out = fixtures_dir / f"{_slug(subject) or summary.get('case_id') or 'fixture'}.json"
    out.write_text(json.dumps(fx, indent=2))
    return out


def _latest_terminal_run(root: Path, npi: str, name: str) -> Path | None:
    """Newest terminal calibration run dir for a subject (by NPI in the subject
    string, else normalized name)."""
    want_name = re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).strip()
    best = None
    for f in glob.glob(str(root / _CAL_GLOB)):
        try:
            d = json.loads(Path(f).read_text())
        except Exception:
            continue
        if str(d.get("status")) not in _TERMINAL:
            continue
        subj = str(d.get("subject") or "")
        hit = (npi and npi in subj) or (
            want_name and want_name in re.sub(r"[^a-z0-9]+", " ", subj.lower()))
        if not hit:
            continue
        ts = d.get("last_updated") or d.get("start_time") or ""
        if best is None or ts > best[0]:
            best = (ts, Path(f).parent)
    return best[1] if best else None


def harvest_battery(root: Path, manifest: dict, *, fixtures_dir: Path = FIXTURES,
                    log=print) -> list[Path]:
    """Harvest a fixture for every battery subject that has a terminal run."""
    out = []
    for s in manifest["subjects"]:
        run = _latest_terminal_run(root, s["npi"], s["name"])
        if run is None:
            log(f"  · no terminal run for {s['name']} — skipped")
            continue
        p = harvest(run, s["name"], expected_for_kind(s["kind"]),
                    npi=s["npi"], kind=s["kind"], fixtures_dir=fixtures_dir)
        out.append(p)
        log(f"  ✓ {s['name']} → {p.name}")
    return out


# ── replay ───────────────────────────────────────────────────────────────────

def replay_fixtures(fixtures_dir: Path = FIXTURES) -> list[dict]:
    """Replay every frozen fixture through the current verdict logic."""
    rows = []
    for f in sorted(glob.glob(str(fixtures_dir / "*.json"))):
        fx = json.loads(Path(f).read_text())
        verdict, rationale = replay_summary(fx["summary"], calibration=True)
        got = verdict_class(verdict)
        rows.append({
            "fixture": Path(f).name,
            "subject": fx.get("subject"),
            "kind": fx.get("kind"),
            "expected": fx["expected"],
            "got": got,
            "verdict": verdict.value,
            "pass": got == fx["expected"],
            "rationale": rationale["summary"],
        })
    return rows


def render(rows: list[dict]) -> str:
    n = len(rows)
    n_pass = sum(1 for r in rows if r["pass"])
    lines = [
        f"# Battery verdict-replay — {date.today().isoformat()}",
        "",
        f"{n} frozen fixture(s) · {n_pass} pass · {n - n_pass} DIVERGED "
        "(current logic decides differently than the frozen expectation)",
        "",
        "| fixture | kind | expected | got | verdict | ok |",
        "|---|---|---|---|---|---|",
    ]
    for r in rows:
        lines.append(
            f"| {r['fixture']} | {r['kind'] or '—'} | {r['expected']} | {r['got']} "
            f"| {r['verdict']} | {'✓' if r['pass'] else '✗'} |")
    diverged = [r for r in rows if not r["pass"]]
    if diverged:
        lines += ["", "## Divergences", ""]
        for r in diverged:
            lines.append(f"- **{r['subject']}**: expected {r['expected']}, got "
                         f"{r['got']} ({r['verdict']}) — {r['rationale']}")
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--report", action="store_true", help="replay all fixtures, print table")
    g.add_argument("--check", action="store_true", help="exit 1 if any fixture diverged")
    g.add_argument("--harvest-battery", action="store_true",
                   help="freeze a fixture for every terminal battery run")
    g.add_argument("--harvest", metavar="STATE_DIR", help="freeze one run as a fixture")
    ap.add_argument("--subject", help="subject name (with --harvest)")
    ap.add_argument("--expected", choices=("FILE", "DECLINE"), help="expected class (with --harvest)")
    ap.add_argument("--root", type=Path, default=REPO_ROOT)
    ap.add_argument("--fixtures", type=Path, default=FIXTURES)
    args = ap.parse_args(argv)

    if args.harvest:
        if not (args.subject and args.expected):
            ap.error("--harvest requires --subject and --expected")
        p = harvest(Path(args.harvest), args.subject, args.expected, fixtures_dir=args.fixtures)
        print(f"harvested → {p}")
        return 0

    if args.harvest_battery:
        manifest = json.loads((args.root / "validation/battery/battery.json").read_text())
        print("Harvesting terminal battery runs into fixtures…")
        out = harvest_battery(args.root, manifest, fixtures_dir=args.fixtures)
        print(f"\n{len(out)} fixture(s) → {args.fixtures}")
        return 0

    rows = replay_fixtures(args.fixtures)
    if not rows:
        print(f"No fixtures in {args.fixtures} — harvest some first "
              f"(--harvest-battery, on a machine with completed runs).")
        return 0
    print(render(rows))
    if args.check:
        return 1 if any(not r["pass"] for r in rows) else 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
