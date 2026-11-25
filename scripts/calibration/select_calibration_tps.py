"""Select recent, data-rich TRUE-POSITIVE backfill candidates for the battery.

The calibration battery enforces a data-coverage floor (as_of >= 2018; our core
Medicare panels start 2013). This proposes gold_set DOJ cases that clear it —
DRY-RUN only: prints + writes a candidate list for human keep/exclude review;
it does NOT touch validation/battery/battery.json.

Filters (all required):
  - a Medicare billing surface (billing_validation.billing_status == BILLING_FOUND)
  - conduct reaches into the data window: max active year >= --min-last-year (2018)
  - NOT already a battery subject
Proposed as_of = (last active fraud year + 1), capped to [2018, 2024] — the cutoff
sits just after the fraud billing so the agent sees it, before public DOJ disclosure
(the reviewer should still sanity-check as_of < the DOJ press date for leak-safety).

Usage::
    python scripts/calibration/select_calibration_tps.py            # top 15
    python scripts/calibration/select_calibration_tps.py --n 25 --min-last-year 2019
"""
from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
GOLD = REPO / "data/extracted/gold_set.jsonl"
BATTERY = REPO / "validation/battery/battery.json"
OUT_DIR = REPO / "validation/battery"
DATA_END = 2024  # latest panel year


def _battery_npis() -> set[str]:
    b = json.loads(BATTERY.read_text())
    return {str(s["npi"]) for s in b["subjects"]}


def _billing_years(rec: dict) -> list[int]:
    """Authoritative Medicare Part B billing years in the data window — the REAL
    surface the agent can analyze. NOT all_active_years, which is contaminated
    with disposition/press years (e.g. a 2026 settlement date) and so overstates
    recency while hiding a thin/absent billing history."""
    bv = rec.get("billing_validation") or {}
    return sorted(int(y) for y in (bv.get("partb_years") or [])
                  if str(y).isdigit() and 2013 <= int(y) <= DATA_END)


def _doj_year(rec: dict) -> int | None:
    oc = rec.get("outcome") if isinstance(rec.get("outcome"), dict) else {}
    d = str(oc.get("date") or "")
    return int(d[:4]) if d[:4].isdigit() else None


def select(n: int, min_billing_years: int, min_doj_year: int) -> list[dict]:
    in_battery = _battery_npis()
    seen: dict[str, dict] = {}
    for line in GOLD.read_text().splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        npi = str(r.get("npi") or "")
        if not npi or npi in in_battery:
            continue
        billing = _billing_years(r)
        if len(billing) < min_billing_years:
            continue                       # thin/absent Medicare surface — unscoreable
        doj_year = _doj_year(r)
        if doj_year is None or doj_year < min_doj_year:
            continue                       # not a recent case
        # as_of brackets the real billing (sees all of it) and stays in [2018, data+1]
        as_of = max(2018, min(max(billing) + 1, DATA_END + 1))
        oc = r.get("outcome") if isinstance(r.get("outcome"), dict) else {}
        doj_date = str(oc.get("date") or "")
        leak_ok = (not doj_date) or (f"{as_of}-01-01" < doj_date)
        cand = {
            "npi": npi,
            "name": r.get("defendant_name") or r.get("nppes_last_name") or "?",
            "specialty": r.get("defendant_specialty") or r.get("nppes_taxonomy") or "?",
            "state": r.get("defendant_state") or "?",
            "fraud_types": r.get("fraud_types") or [],
            "n_billing_years": len(billing),
            "billing_span": f"{billing[0]}-{billing[-1]}",
            "doj_year": doj_year,
            "proposed_as_of": f"{as_of}-01-01",
            "doj_date": doj_date,
            "settlement_usd": oc.get("amount_usd"),
            "as_of_before_doj": leak_ok,
            "case_name": (r.get("case_name") or "")[:80],
            "source_url": r.get("source_url") or "",
        }
        # keep the deepest-surface record per NPI
        if npi not in seen or cand["n_billing_years"] > seen[npi]["n_billing_years"]:
            seen[npi] = cand
    cands = sorted(seen.values(), key=lambda c: (-c["doj_year"], -c["n_billing_years"]))
    return cands[:n]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--n", type=int, default=15, help="max candidates")
    ap.add_argument("--min-billing-years", type=int, default=5,
                    help="require >= this many Medicare Part B billing years in 2013-2024 (surface depth)")
    ap.add_argument("--min-doj-year", type=int, default=2022,
                    help="DOJ disposition year must be >= this (recent case)")
    args = ap.parse_args(argv)

    cands = select(args.n, args.min_billing_years, args.min_doj_year)
    stamp = date.today().isoformat()
    jsonl = OUT_DIR / f"tp_backfill_candidates_{stamp}.jsonl"
    md = OUT_DIR / f"tp_backfill_candidates_{stamp}.md"
    with jsonl.open("w") as f:
        for c in cands:
            f.write(json.dumps(c) + "\n")
    lines = [
        f"# Calibration TP backfill candidates ({stamp})",
        "",
        f"{len(cands)} recent + data-rich gold_set TPs not already in the battery "
        f"(≥ {args.min_billing_years} Part B billing years in 2013–2024, DOJ ≥ {args.min_doj_year}). "
        "DRY-RUN — review keep/exclude, then add approved to `validation/battery/battery.json`.",
        "",
        "⚠️ Reviewer check: confirm `proposed_as_of` is BEFORE the DOJ press/charge date "
        "(leak-safety) and prefer obscure cases (small settlement, low press).",
        "",
        "| npi | name | specialty | st | billing yrs | proposed as_of | DOJ date | leak-safe? | settlement | fraud_types |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for c in cands:
        amt = c["settlement_usd"]
        amt_s = f"${amt/1e6:.1f}M" if isinstance(amt, (int, float)) and amt else "—"
        leak = "✓" if c["as_of_before_doj"] else "⚠️ LEAK"
        lines.append(
            f"| {c['npi']} | {c['name'][:24]} | {c['specialty'][:20]} | {c['state']} | "
            f"{c['n_billing_years']} ({c['billing_span']}) | {c['proposed_as_of']} | {c['doj_date'] or '—'} | {leak} | "
            f"{amt_s} | {','.join(c['fraud_types'])[:40]} |")
    md.write_text("\n".join(lines) + "\n")
    print("\n".join(lines))
    print(f"\nwrote {jsonl}\n      {md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
