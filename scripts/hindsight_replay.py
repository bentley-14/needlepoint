#!/usr/bin/env python3
"""Hindsight replay — DOJ enforcement as a delayed oracle (self-learning loop 1).

Every DOJ case in gold_set.jsonl was in our panels while the fraud ran.
This tool replays, for each defendant NPI, where OUR pipeline placed them:

    ranker   → rank/percentile in scored_full_{year}.parquet for active years
    triage   → verdict in validation/triage/*.json queues
    loop     → case state under fraud-investigator-v2/state/ (verdict, EVF)
    parked   → fraud-investigator-v2/state/parked/ registry

…then classifies each NPI on the funnel ladder and routes every miss to the
agent that owns the fix (feature_engineer / triage rubric / floor analysis).
Run monthly after a corpus_curator batch lands (`--since` limits to newly
added outcomes) — time itself becomes the training signal.

Usage:
    python scripts/hindsight_replay.py                      # full gold_set
    python scripts/hindsight_replay.py --since 2026-05-01   # new outcomes only
    python scripts/hindsight_replay.py --npi 1900000016     # one provider
    python scripts/hindsight_replay.py --format json

Output: results/hindsight/hindsight_<date>.{jsonl,md} + stdout summary.
Read-only over local artifacts; no network; fail-soft per data source
(missing scored panels / queues just mark that rung "unavailable").
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# Funnel ladder, strongest → weakest. classify() returns the highest rung hit.
LADDER = [
    "INVESTIGATED_FILED",        # loop ran → FILE-class verdict
    "INVESTIGATED_DECLINED",     # loop ran → declined/abandoned
    "INVESTIGATED_INCONCLUSIVE", # loop ran → NEEDS_HUMAN / in_progress / cap
    "PARKED",                    # slot-3 parked, watching for a trigger
    "TRIAGED_HIGH",              # triage HIGH_RISK but never investigated
    "TRIAGED_MODERATE",
    "TRIAGED_LOW_OR_DISMISS",    # triage saw them and waved them off  ← rubric miss
    "RANKED_TOP2K",              # ranker surfaced them; triage never saw them
    "IN_PANEL_MISSED",           # in panels, ranker never surfaced  ← feature miss
    "NOT_IN_PANEL",              # no Part B surface in active years ← data gap
]

FILE_CLASS = {"SETTLEMENT_READY", "FILE", "FILE_WITH_DISCOVERY_DEPS",
              "DECLARE_CASE_READY", "DECLARE_CASE_READY_WITH_DEPENDENCIES"}
DECLINE_CLASS = {"NOT_ACTIONABLE", "COUNSEL_ABANDONED", "INDEFENSIBLE_AT_CAP",
                 "RECONSIDER", "ABANDON_CASE"}

FOLLOWUP_BY_RUNG = {
    "TRIAGED_LOW_OR_DISMISS": "triage rubric — verdict waved off a confirmed "
        "DOJ defendant; review the 15-angle thresholds / computed norms for "
        "this specialty (prompts/triage.md)",
    "RANKED_TOP2K": "triage coverage — ranked in top-2K but absent from every "
        "triage queue; widen --k-range or re-run triage for the year",
    "IN_PANEL_MISSED": "feature_engineer — billed Part B in active years but "
        "the ranker never surfaced them; propose features from the DOJ "
        "scheme_summary (run the feature_engineer agent with it)",
    "NOT_IN_PANEL": "ABSENT from scored panels in active years — three very "
        "different causes: (a) active years pre-2015 (no scored panel exists), "
        "(b) no Medicare Part B surface at all (commercial/Medicaid-only — known "
        "corpus caveat, confirm via check_billing_surface.py), (c) IN the raw "
        "panel but dropped by the floor_0.50 agent filter — THAT one is a real "
        "pipeline miss; disambiguate with --check-panels",
    "INVESTIGATED_DECLINED": "floor analysis — the loop investigated a "
        "confirmed DOJ defendant and declined; read the abandon reasoning + "
        "floor audit in the case state (which slot failed?)",
}


# ── artifact loaders (each fail-soft) ────────────────────────────────────────

def load_gold_set(root: Path, since: str | None = None,
                  only_npi: str | None = None) -> list[dict]:
    rows = []
    p = root / "data" / "extracted" / "gold_set.jsonl"
    for line in p.read_text().splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        npi = str(r.get("npi") or "").strip()
        if not npi or npi == "None":
            continue
        if only_npi and npi != only_npi:
            continue
        if since and ((r.get("outcome") or {}).get("date") or "") < since:
            continue
        rows.append(r)
    return rows


def scored_rank_index(root: Path) -> dict[int, dict[str, int]]:
    """{year: {npi: rank}} from the freshest scored_full_{year}.parquet per
    year. Rank = 1-based position by score_rrf desc. Empty dict per missing
    year (fail-soft)."""
    out: dict[int, dict[str, int]] = {}
    panels: dict[int, Path] = {}
    for p in root.glob("results/walk_forward_v4_top*/scored_full_*.parquet"):
        m = re.search(r"scored_full_(\d{4})\.parquet$", p.name)
        if not m:
            continue
        y = int(m.group(1))
        if y not in panels or p.stat().st_mtime > panels[y].stat().st_mtime:
            panels[y] = p
    try:
        import duckdb
    except ImportError:
        return out
    for y, p in panels.items():
        try:
            df = duckdb.sql(
                f"SELECT npi, ROW_NUMBER() OVER (ORDER BY score_rrf DESC) AS rk "
                f"FROM read_parquet('{p}')").df()
            out[y] = dict(zip(df["npi"].astype(str).str.zfill(10),
                              df["rk"].astype(int)))
        except Exception:
            continue
    return out


def triage_index(root: Path) -> dict[str, list[dict]]:
    """{npi: [{year, verdict, confidence, queue}]} across all queue files."""
    out: dict[str, list[dict]] = {}
    for q in sorted(root.glob("validation/triage/**/*.json")):
        try:
            d = json.loads(q.read_text())
        except Exception:
            continue
        for item in d.get("items") or []:
            npi = str(item.get("npi") or "").zfill(10)
            out.setdefault(npi, []).append({
                "year": d.get("score_year"),
                "verdict": item.get("verdict"),
                "confidence": item.get("confidence"),
                "queue": str(q.relative_to(root)),
            })
    return out


def case_index(root: Path) -> dict[str, list[dict]]:
    """{npi: [{case_id, status, verdict, evf}]} from production case summaries
    (calibration runs are excluded — they're the battery's domain)."""
    out: dict[str, list[dict]] = {}
    for p in root.glob("fraud-investigator-v2/state/CASE-*/summary.json"):
        try:
            d = json.loads(p.read_text())
        except Exception:
            continue
        m = re.search(r"(?<!\d)(\d{10})(?!\d)", d.get("subject") or "")
        if not m:
            continue
        out.setdefault(m.group(1), []).append({
            "case_id": d.get("case_id"),
            "status": d.get("status"),
            "verdict": d.get("final_verdict"),
            "evf": d.get("expected_value_factor"),
        })
    return out


def parked_index(root: Path) -> dict[str, dict]:
    """{npi: parked_record} — both the parked NPI and its watch list count."""
    out: dict[str, dict] = {}
    for p in root.glob("fraud-investigator-v2/state/parked/*.json"):
        try:
            d = json.loads(p.read_text())
        except Exception:
            continue
        for npi in {str(d.get("npi") or "")} | set(d.get("watch_npis") or []):
            if npi:
                out[str(npi)] = d
    return out


def panel_presence(root: Path, npi: str, years: list[int]) -> list[int]:
    """Years (⊆ active years) in which the NPI has a Part B panel row.
    Prefers gold_set's own billing_validation when present (cheap); falls
    back to duckdb panel checks."""
    present = []
    try:
        import duckdb
    except ImportError:
        return present
    for y in years:
        for sub in ("processed", "panels"):
            p = root / "data" / sub / f"cms_partb_{y}.parquet"
            if p.exists():
                try:
                    n = duckdb.sql(
                        f"SELECT count(*) FROM read_parquet('{p}') "
                        f"WHERE Rndrng_NPI = ?", params=[npi]).fetchone()[0]
                    if n:
                        present.append(y)
                except Exception:
                    pass
                break
    return present


# ── classification ───────────────────────────────────────────────────────────

def classify(npi: str, active_years: list[int], *, ranks: dict,
             triage: dict, cases: dict, parked: dict,
             billing_years: list[int] | None) -> dict:
    """Pure: place one NPI on the funnel ladder from pre-built indexes."""
    npi = str(npi).zfill(10)
    rec: dict = {"npi": npi, "active_years": active_years}

    rec["ranker"] = {str(y): ranks[y][npi] for y in active_years
                     if y in ranks and npi in ranks[y]}
    rec["ranker_years_available"] = sorted(set(active_years) & set(ranks))
    best = min(rec["ranker"].values()) if rec["ranker"] else None
    rec["best_rank"] = best

    rec["triage"] = triage.get(npi, [])
    verdicts = {t["verdict"] for t in rec["triage"]}
    rec["cases"] = cases.get(npi, [])
    rec["parked"] = bool(parked.get(npi))

    if rec["cases"]:
        vs = {c.get("verdict") for c in rec["cases"]}
        if vs & FILE_CLASS:
            rung = "INVESTIGATED_FILED"
        elif vs & DECLINE_CLASS:
            rung = "INVESTIGATED_DECLINED"
        else:
            rung = "INVESTIGATED_INCONCLUSIVE"
    elif rec["parked"]:
        rung = "PARKED"
    elif "HIGH_RISK" in verdicts:
        rung = "TRIAGED_HIGH"
    elif "MODERATE_RISK" in verdicts:
        rung = "TRIAGED_MODERATE"
    elif verdicts & {"LOW_RISK", "DISMISS"}:
        rung = "TRIAGED_LOW_OR_DISMISS"
    elif best is not None and best <= 2000:
        rung = "RANKED_TOP2K"
    else:
        in_panel = bool(rec["ranker"]) or bool(billing_years)
        rung = "IN_PANEL_MISSED" if in_panel else "NOT_IN_PANEL"

    rec["classification"] = rung
    rec["followup"] = FOLLOWUP_BY_RUNG.get(rung, "")
    return rec


def replay(root: Path, *, since: str | None = None,
           only_npi: str | None = None, check_panels: bool = False) -> dict:
    """Build all indexes once, classify every gold_set NPI, aggregate."""
    gold = load_gold_set(root, since=since, only_npi=only_npi)
    ranks = scored_rank_index(root)
    triage = triage_index(root)
    cases = case_index(root)
    parked = parked_index(root)

    results, seen = [], set()
    for row in gold:
        npi = str(row["npi"]).zfill(10)
        if npi in seen:
            continue
        seen.add(npi)
        years = [int(y) for y in (row.get("all_active_years") or [])]
        billing = None
        if check_panels:
            billing = panel_presence(root, npi, years)
        rec = classify(npi, years, ranks=ranks, triage=triage, cases=cases,
                       parked=parked, billing_years=billing)
        rec["defendant_name"] = row.get("defendant_name")
        rec["defendant_specialty"] = row.get("defendant_specialty")
        rec["fraud_types"] = row.get("fraud_types")
        rec["outcome_date"] = (row.get("outcome") or {}).get("date")
        results.append(rec)

    funnel = Counter(r["classification"] for r in results)
    return {
        "generated": date.today().isoformat(),
        "since": since,
        "n_npis": len(results),
        "scored_years_available": sorted(ranks),
        "funnel": {k: funnel.get(k, 0) for k in LADDER},
        "results": results,
    }


# ── rendering ────────────────────────────────────────────────────────────────

def render_md(report: dict) -> str:
    lines = [
        f"# Hindsight replay — {report['generated']}"
        + (f" (outcomes since {report['since']})" if report["since"] else ""),
        "",
        f"{report['n_npis']} unique DOJ defendant NPIs · scored years "
        f"available: {report['scored_years_available']}",
        "",
        "## Funnel (where our pipeline placed confirmed fraudsters)",
        "",
        "| rung | n | share |",
        "|---|---|---|",
    ]
    n = max(report["n_npis"], 1)
    for k in LADDER:
        v = report["funnel"].get(k, 0)
        lines.append(f"| {k} | {v} | {v / n:.0%} |")
    lines += ["", "## Routed follow-ups (the learning signal)", ""]
    for rung, owner in FOLLOWUP_BY_RUNG.items():
        hits = [r for r in report["results"] if r["classification"] == rung]
        if not hits:
            continue
        lines.append(f"### {rung} ({len(hits)}) → {owner}")
        for r in hits[:15]:
            lines.append(
                f"- {r['npi']} {r.get('defendant_name') or '?'} "
                f"({r.get('defendant_specialty') or '?'}; "
                f"{','.join(r.get('fraud_types') or [])[:60]}) "
                f"best_rank={r.get('best_rank')}")
        if len(hits) > 15:
            lines.append(f"- …and {len(hits) - 15} more (see jsonl)")
        lines.append("")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--since", default=None,
                    help="only gold_set rows with outcome.date >= this (YYYY-MM-DD)")
    ap.add_argument("--npi", default=None, help="replay a single NPI")
    ap.add_argument("--check-panels", action="store_true",
                    help="confirm Part B panel presence for misses (slower)")
    ap.add_argument("--format", choices=("md", "json"), default="md")
    ap.add_argument("--out-dir", type=Path,
                    default=REPO_ROOT / "results" / "hindsight")
    ap.add_argument("--root", type=Path, default=REPO_ROOT)
    args = ap.parse_args(argv)

    report = replay(args.root, since=args.since, only_npi=args.npi,
                    check_panels=args.check_panels)

    args.out_dir.mkdir(parents=True, exist_ok=True)
    stamp = report["generated"]
    jl = args.out_dir / f"hindsight_{stamp}.jsonl"
    with jl.open("w") as f:
        for r in report["results"]:
            f.write(json.dumps(r, default=str) + "\n")
    md = args.out_dir / f"hindsight_{stamp}.md"
    md.write_text(render_md(report))

    if args.format == "json":
        print(json.dumps({k: v for k, v in report.items() if k != "results"},
                         indent=2))
    else:
        print(render_md(report))
    print(f"\nwrote {jl} + {md}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
