"""Theory-match recall — did the loop plead the ACTUAL DOJ fraud, not just file?

`tp_file_rate` ("did it file on a known DOJ NPI?") is too weak: a run can FILE on
the wrong fraud (Aspen's draft run filed on glucose/smoking/E&M outliers while
the real DOJ case was incident-to / NP-billed-as-physician). This scorer measures
the right thing — recall = did the loop INDEPENDENTLY plead a count targeting the
same fraud MECHANISM DOJ charged?

Leak-safety: the DOJ mechanism (validation/battery/doj_theory.json) is the ANSWER
KEY. It is used ONLY here, post-hoc, OUTSIDE the sandbox — never fed to the loop.

Match strictness: fraud-MECHANISM (not exact specifics). MATCH = the run pleaded a
retained count of the same mechanism (e.g. incident_to); MISS = it didn't (incl.
"filed, but on a different fraud"); INCONCLUSIVE = the gold_set summary was too
generic to pin the DOJ mechanism (a corpus-enrichment gap, not a loop failure).

Usage:
    python scripts/theory_match.py            # score calibration runs
    python scripts/theory_match.py --state-root fraud-investigator-v2/state
"""
from __future__ import annotations

import argparse
import glob
import json
import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_DOJ = _ROOT / "validation" / "battery" / "doj_theory.json"

# Canonical FCA fraud mechanisms + the patterns that tag a pleaded count to each.
FRAUD_TYPES: dict[str, list[str]] = {
    "incident_to": [r"incident[- ]to", r"nurse practitioner", r"\bNP[- ]rendered",
                    r"\bNP services", r"mid-?level", r"billed under the physician'?s NPI",
                    r"rendered by .*\b(NP|nurse)"],
    "false_dates": [r"multiple dates", r"appear(?:ed|s)? .*(?:performed|rendered) on",
                    r"date[- ]split", r"different dates of service", r"multiple days",
                    r"single date .* multiple"],
    "services_not_rendered": [r"not (?:rendered|provided|performed)", r"never (?:provided|rendered|performed)",
                              r"phantom", r"did not (?:render|provide|perform)", r"incarcerated",
                              r"deceased", r"clinic .* closed", r"impossible (?:volume|number|day)",
                              r"would have required more than"],
    "medically_unnecessary": [r"medically unnecessary", r"not reasonable and necessary",
                              r"\bunnecessary\b", r"no (?:legitimate )?clinical indication",
                              r"lack(?:ed|s)? medical necessity"],
    "upcoding": [r"upcod", r"highest[- ]complexity", r"high[- ]complexity", r"99215", r"99205",
                 r"higher[- ]level", r"more complex"],
    "unbundling": [r"unbundl", r"fragment", r"separately bill"],
    "kickback": [r"kickback", r"bribe", r"\bStark\b", r"remuneration", r"consultation fee",
                 r"referral fee", r"in exchange for"],
    "false_certification": [r"false(?:ly)? certif", r"condition(?:s)? of payment", r"attestation"],
    "dme": [r"\bDME\b", r"durable medical equipment", r"orthotic", r"\bbrace(?:s)?\b"],
    "drug_overbilling": [r"\bunits? (?:per|above|over)", r"\bvial", r"PREEMPT",
                         r"above .*(?:maximum|ceiling|label)", r"\bJ\d{4}\b"],
}
_COMPILED = {ft: [re.compile(p, re.I) for p in pats] for ft, pats in FRAUD_TYPES.items()}

_TERMINAL = {"complete", "case_ready", "abandoned"}


def classify_counts(summary: dict) -> set[str]:
    """Tag the run's RETAINED pleaded counts with fraud mechanisms (keyword)."""
    build = {}
    for it in (summary.get("iterations") or []):
        if it.get("counsel_build"):
            build = it["counsel_build"]
    retained = set(summary.get("counts_retained") or [])
    tags: set[str] = set()
    for c in (build.get("counts_pled") or []):
        if retained and c.get("count_id") not in retained:
            continue
        blob = " ".join(str(c.get(k) or "") for k in ("description", "statute", "mundane_alternative"))
        for ft, pats in _COMPILED.items():
            if any(p.search(blob) for p in pats):
                tags.add(ft)
    return tags


def _latest_run(state_root: Path, npi: str, name: str) -> dict | None:
    best = None
    for f in glob.glob(str(state_root / "**" / "summary.json"), recursive=True):
        try:
            d = json.load(open(f))
        except Exception:
            continue
        subj = json.dumps(d.get("subject") or "")
        last = (name.split()[-1] if name else "")
        if npi not in subj and (not last or last not in subj):
            continue
        if str(d.get("status")) not in _TERMINAL:
            continue
        ts = d.get("last_updated") or d.get("start_time") or ""
        if best is None or ts > best[0]:
            best = (ts, d)
    return best[1] if best else None


def score(state_root: Path) -> dict:
    doj = json.load(open(_DOJ))["subjects"]
    rows = []
    for npi, info in doj.items():
        run = _latest_run(state_root, npi, info.get("name", ""))
        doj_mech = set(info.get("doj_mechanisms") or [])
        row = {"npi": npi, "name": info.get("name"), "doj_mechanisms": sorted(doj_mech),
               "confidence": info.get("confidence")}
        if run is None:
            row["result"] = "NO_RUN"
        elif not doj_mech:
            row["result"] = "INCONCLUSIVE"  # DOJ mechanism not pinnable from gold_set
            row["run_mechanisms"] = sorted(classify_counts(run))
        else:
            run_tags = classify_counts(run)
            row["run_mechanisms"] = sorted(run_tags)
            row["filed"] = str(run.get("final_verdict")) in (
                "SETTLEMENT_READY", "FILE_WITH_DISCOVERY_DEPS")
            row["result"] = "MATCH" if (doj_mech & run_tags) else "MISS"
        rows.append(row)
    scoreable = [r for r in rows if r["result"] in ("MATCH", "MISS")]
    n_match = sum(1 for r in scoreable if r["result"] == "MATCH")
    filed = [r for r in rows if r.get("filed")]
    return {
        "theory_match_rate": (n_match / len(scoreable)) if scoreable else None,
        "n_match": n_match, "n_scoreable": len(scoreable),
        "file_rate_for_comparison": (len(filed) / len([r for r in rows if "filed" in r]))
                                    if any("filed" in r for r in rows) else None,
        "inconclusive": [r["name"] for r in rows if r["result"] == "INCONCLUSIVE"],
        "no_run": [r["name"] for r in rows if r["result"] == "NO_RUN"],
        "rows": rows,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--state-root", default="fraud-investigator-v2/state/calibration")
    args = ap.parse_args(argv)
    rep = score(Path(args.state_root))
    print(f"theory_match_rate = {rep['theory_match_rate']}  ({rep['n_match']}/{rep['n_scoreable']} scoreable)")
    print(f"(file_rate for comparison = {rep['file_rate_for_comparison']})")
    print(f"INCONCLUSIVE (DOJ mechanism not in gold_set): {rep['inconclusive']}")
    print(f"NO_RUN: {rep['no_run']}")
    print("\nper-subject:")
    for r in rep["rows"]:
        if r["result"] in ("MATCH", "MISS"):
            print(f"  {r['result']:5s} {r['name']:18s} DOJ={r['doj_mechanisms']} "
                  f"loop={r.get('run_mechanisms')} filed={r.get('filed')}")
        else:
            print(f"  {r['result']:12s} {r['name']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
