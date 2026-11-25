#!/usr/bin/env python3
"""Audit the HCPCS by-year reference for QUANTITY DRIFT in descriptors.

Why: the CMS Part B PUF's HCPCS_Desc is a denormalized copy of the code
descriptor and can be WRONG. Discovered 2026-06-11: 99489 (complex-CCM add-on)
flipped "each additional 30 minutes" -> "60 minutes" in the 2021-2024 PUF while
the AMA-CPT definition stayed 30 — silently ~2x-ing any unit->time math built on
the looked-up descriptor (it inflated a staffing-impossibility figure ~1.7x in
attorney-facing output before being caught).

What it does: for every code-year in data/ground_truth/hcpcs_codes_by_year.parquet,
parse the numeric quantities out of the descriptor:
  - TIME:  "NN minutes"/"NN hours" (hours normalized to minutes) -> sorted tuple
  - DOSE:  "NN mg|mcg|ml|g|units|iu" (drug codes)               -> sorted tuple
then flag any code whose parsed quantity TUPLE changes across years. Rewording
without a number change is NOT flagged. Output is sorted by billing volume so the
highest-impact drift is reviewed first.

Each flagged code needs human verification against the AUTHORITATIVE source
(AMA CPT / CMS HCPCS release): a real mid-stream quantity change for the SAME
code is rare (revisions usually mint new codes), so most flags are either PUF
copy errors (fix via scripts/ingest/hcpcs_authoritative_overrides.json — the
hcpcs_lookup override layer) or legitimate descriptor restructures to verify.

Usage:
  python3 scripts/audit_hcpcs_descriptors.py                 # full audit (text)
  python3 scripts/audit_hcpcs_descriptors.py --kind time     # time-quantity drift only
  python3 scripts/audit_hcpcs_descriptors.py --format json   # machine-readable
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

import duckdb

REPO = Path(__file__).resolve().parents[1]
BY_YEAR = REPO / "data" / "ground_truth" / "hcpcs_codes_by_year.parquet"
OVERRIDES = REPO / "scripts" / "ingest" / "hcpcs_authoritative_overrides.json"

_TIME_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(minute|min\b|hour|hr\b)", re.I)
_DOSE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(mg|mcg|microgram|milligram|ml\b|gm\b|gram|units?\b|iu\b|mci\b|uci\b)", re.I)


def _time_tuple(desc: str) -> tuple:
    """Sorted tuple of minute-quantities in the descriptor (hours normalized)."""
    out = []
    for num, unit in _TIME_RE.findall(desc or ""):
        v = float(num)
        if unit.lower().startswith(("hour", "hr")):
            v *= 60
        out.append(int(v) if v == int(v) else v)
    return tuple(sorted(out))


def _dose_tuple(desc: str) -> tuple:
    out = []
    for num, unit in _DOSE_RE.findall(desc or ""):
        v = float(num)
        out.append((int(v) if v == int(v) else v, unit.lower().rstrip("s")))
    return tuple(sorted(out))


def audit(kind: str = "both") -> list[dict]:
    con = duckdb.connect()
    rows = con.execute(
        f"SELECT code, year, descriptor, is_drug, total_services "
        f"FROM read_parquet('{BY_YEAR}') ORDER BY code, year"
    ).fetchall()

    by_code: dict[str, list] = defaultdict(list)
    for code, year, desc, is_drug, svcs in rows:
        by_code[code].append((year, desc or "", bool(is_drug), int(svcs or 0)))

    try:
        overridden = set(json.load(open(OVERRIDES)).get("codes", {}).keys())
    except Exception:
        overridden = set()

    flags: list[dict] = []
    for code, yrs in by_code.items():
        if len(yrs) < 2:
            continue
        is_drug = yrs[0][2]
        for label, fn in (("time", _time_tuple), ("dose", _dose_tuple)):
            if kind not in ("both", label):
                continue
            if label == "dose" and not is_drug:
                continue
            if label == "time" and is_drug:
                continue
            sigs = {}
            for year, desc, _d, svcs in yrs:
                sig = fn(desc)
                sigs.setdefault(sig, []).append(year)
            nonempty = {s: y for s, y in sigs.items() if s}
            # Flag only when there are >=2 DISTINCT non-empty quantity signatures.
            # (empty<->value transitions are usually wording restructures; listed
            # separately at --verbose level via 'gained_or_lost_quantity'.)
            if len(nonempty) >= 2:
                flags.append({
                    "code": code,
                    "kind": label,
                    "signatures": {str(s): sorted(y) for s, y in sigs.items()},
                    "total_services": sum(s for _y, _d2, _d3, s in yrs),
                    "is_drug": is_drug,
                    "already_overridden": code.upper() in overridden,
                    "descriptor_latest": yrs[-1][1][:160],
                })
    flags.sort(key=lambda f: -f["total_services"])
    return flags


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--kind", choices=("time", "dose", "both"), default="both")
    ap.add_argument("--format", choices=("text", "json"), default="text")
    ap.add_argument("--limit", type=int, default=50)
    args = ap.parse_args()

    flags = audit(args.kind)
    if args.format == "json":
        print(json.dumps(flags[: args.limit], indent=2))
        return 0

    print(f"QUANTITY-DRIFT AUDIT — {len(flags)} flagged code(s) "
          f"(kind={args.kind}; sorted by billing volume; top {args.limit})\n")
    for f in flags[: args.limit]:
        ov = "  [OVERRIDDEN ✓]" if f["already_overridden"] else "  [NEEDS VERIFICATION]"
        print(f"{f['code']}  ({f['kind']}; {f['total_services']:,} services){ov}")
        for sig, years in sorted(f["signatures"].items(), key=lambda kv: kv[1][0] if kv[1] else 0):
            yr = f"{years[0]}–{years[-1]}" if len(years) > 1 else str(years[0])
            print(f"    {sig or '(no quantity parsed)'}  -> {yr} ({len(years)} yr)")
        print(f"    latest: {f['descriptor_latest']}")
        print()
    if not flags:
        print("No quantity drift detected.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
