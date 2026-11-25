#!/usr/bin/env python3
"""R2 janitor — local run artifacts are disposable once R2 verifiably has them.

R2 (r2:needlepoint-data/artifacts/) is the canonical home for run artifacts
(sync_artifacts_r2.sh pushes at every run completion). Local copies under
fraud-investigator-v2/state/ and state/runs/ are working cache: this janitor
deletes local dirs that are (a) older than the TTL, (b) NOT parked, and
(c) VERIFIED present on R2 — `rclone check --one-way` per directory, every
local file must exist remotely with matching size/hash. No verification,
no delete. DRY-RUN by default; `--apply` to actually delete.

    python scripts/r2_janitor.py                 # dry-run report
    python scripts/r2_janitor.py --apply         # verify + delete
    python scripts/r2_janitor.py --ttl-days 60   # keep more history
    make janitor / make janitor-apply

Never touched: anything younger than --ttl-days (newest-file mtime), parked
cases (state/parked/ registry — their state must stay resumable locally),
the parked registry itself, and anything that fails R2 verification.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
R2_PREFIX = "r2:needlepoint-data/artifacts"

# (local glob, R2 subdir) — mirrors sync_artifacts_r2.sh exactly.
TARGETS = [
    ("fraud-investigator-v2/state/CASE-*", "cases"),
    ("fraud-investigator-v2/state/calibration/CALIBRATION-*", "calibration"),
    ("state/runs/run_*", "pipeline_runs"),
]


def newest_mtime(d: Path) -> float:
    mt = d.stat().st_mtime
    for p in d.rglob("*"):
        try:
            mt = max(mt, p.stat().st_mtime)
        except OSError:
            pass
    return mt


def parked_case_ids(root: Path) -> set[str]:
    out: set[str] = set()
    for p in root.glob("fraud-investigator-v2/state/parked/*.json"):
        try:
            cid = json.loads(p.read_text()).get("case_id")
            if cid:
                out.add(cid)
        except Exception:
            pass
        out.add(p.stem)  # filename is <case_id>.json — belt and braces
    return out


def rclone_verified(local: Path, remote: str, timeout: int = 300) -> bool:
    """True iff EVERY local file exists on R2 with matching size/hash."""
    try:
        p = subprocess.run(
            ["rclone", "check", str(local), remote, "--one-way",
             "--contimeout", "15s", "--timeout", "120s"],
            capture_output=True, text=True, timeout=timeout)
        return p.returncode == 0
    except Exception:
        return False


def plan(root: Path, *, ttl_days: float, now: float | None = None,
         parked: set[str] | None = None) -> list[dict]:
    """Pure planning: which dirs are deletion CANDIDATES (age + not-parked).
    Verification + deletion happen in execute() — injectable for tests."""
    now = now or time.time()
    parked = parked if parked is not None else parked_case_ids(root)
    cutoff = now - ttl_days * 86400
    actions: list[dict] = []
    for glob_pat, r2_sub in TARGETS:
        for d in sorted(root.glob(glob_pat)):
            if not d.is_dir():
                continue
            rec = {"dir": str(d), "remote": f"{R2_PREFIX}/{r2_sub}/{d.name}"}
            if d.name in parked:
                actions.append({**rec, "action": "KEEP", "why": "parked case"})
                continue
            age_d = (now - newest_mtime(d)) / 86400
            if age_d < ttl_days:
                actions.append({**rec, "action": "KEEP",
                                "why": f"young ({age_d:.0f}d < ttl {ttl_days:.0f}d)"})
                continue
            actions.append({**rec, "action": "CANDIDATE",
                            "why": f"age {age_d:.0f}d"})
    return actions


def execute(actions: list[dict], *, apply: bool,
            verifier=rclone_verified) -> list[dict]:
    out = []
    for a in actions:
        if a["action"] != "CANDIDATE":
            out.append(a)
            continue
        if not verifier(Path(a["dir"]), a["remote"]):
            out.append({**a, "action": "KEEP",
                        "why": a["why"] + " — R2 VERIFICATION FAILED (kept; "
                        "run sync_artifacts_r2.sh and retry)"})
            continue
        if apply:
            shutil.rmtree(a["dir"], ignore_errors=False)
            out.append({**a, "action": "DELETED",
                        "why": a["why"] + " — verified on R2"})
        else:
            out.append({**a, "action": "WOULD_DELETE",
                        "why": a["why"] + " — verified on R2 (dry-run)"})
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--ttl-days", type=float, default=28.0)
    ap.add_argument("--apply", action="store_true",
                    help="actually delete (default: dry-run)")
    ap.add_argument("--root", type=Path, default=REPO_ROOT)
    args = ap.parse_args(argv)

    if not shutil.which("rclone"):
        print("r2_janitor: rclone not found — nothing done (artifacts stay local).")
        return 0

    actions = execute(plan(args.root, ttl_days=args.ttl_days), apply=args.apply)
    by = {}
    for a in actions:
        by.setdefault(a["action"], []).append(a)
    for action in ("DELETED", "WOULD_DELETE", "KEEP"):
        for a in by.get(action, []):
            print(f"{action:13} {a['dir']}  ({a['why']})")
    freed = len(by.get("DELETED", [])) or len(by.get("WOULD_DELETE", []))
    print(f"\n{len(actions)} dir(s) scanned · "
          f"{len(by.get('DELETED', []))} deleted · "
          f"{len(by.get('WOULD_DELETE', []))} would-delete · "
          f"{len(by.get('KEEP', []))} kept"
          + ("" if args.apply else "   (dry-run — use --apply)"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
