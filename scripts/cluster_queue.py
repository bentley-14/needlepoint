"""Cluster-level casework (M2) — group triage-queue candidates into practice
clusters BEFORE Layer 3.

HIGH_RISK candidates at the same practice address currently get independent
full investigations that each rediscover each other. This tool joins the
triage queue (TriageOutput JSON) to the year's Part B panel address columns
(Rndrng_Prvdr_St1, Rndrng_Prvdr_Zip5) and groups queue members by exact
address pair, so a human can launch ONE combined office-suite investigation
instead of N overlapping ones.

Only HIGH_RISK + MODERATE_RISK members are clustered; LOW_RISK / DISMISS are
excluded from clustering but counted in the report.

Usage:
    python scripts/cluster_queue.py --queue validation/triage/2023_top1-500.json --year 2023
    python scripts/cluster_queue.py --queue ... --year 2023 --format json --out clusters.json
"""
from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))  # repo root, so fraudlib imports

import argparse
import json
import sys
from pathlib import Path

import duckdb

try:
    from fraudlib import r2
except Exception:  # calibration jail / standalone copy: no fraudlib → local-only, never R2
    class _LocalR2:
        R2_PANELS = ""
        @staticmethod
        def parquet_source(local, key): return str(local)
        @staticmethod
        def duckdb_r2():
            import duckdb; return duckdb.connect()
    r2 = _LocalR2()

REPO = Path(__file__).resolve().parents[1]

CLUSTERABLE_VERDICTS = {"HIGH_RISK", "MODERATE_RISK"}


def resolve_panel(year: int, override: str | None = None) -> str:
    """Part B panel source for `year`: explicit override → data/processed →
    data/panels → R2 (shim degrades to the local processed path offline)."""
    if override:
        return str(override)
    for sub in ("data/processed", "data/panels"):
        p = REPO / sub / f"cms_partb_{year}.parquet"
        if p.exists():
            return str(p)
    return r2.parquet_source(REPO / f"data/processed/cms_partb_{year}.parquet",
                             f"{r2.R2_PANELS}/cms_partb_{year}.parquet")


def _panel_address_map(panel_src: str, npis: list[str]) -> dict[str, tuple[str, str, float]]:
    """{npi: (street, zip5, tot_medicare_payment)} for the queue NPIs."""
    if not npis:
        return {}
    con = r2.duckdb_r2()
    src = panel_src.replace("'", "''")
    placeholders = ",".join("?" for _ in npis)
    try:
        rows = con.execute(
            f"SELECT CAST(Rndrng_NPI AS VARCHAR), Rndrng_Prvdr_St1, "
            f"       CAST(Rndrng_Prvdr_Zip5 AS VARCHAR), Tot_Mdcr_Pymt_Amt "
            f"FROM read_parquet('{src}') "
            f"WHERE CAST(Rndrng_NPI AS VARCHAR) IN ({placeholders})",
            npis,
        ).fetchall()
    except duckdb.Error as e:
        print(f"ERROR: cannot read panel {panel_src}: {e}", file=sys.stderr)
        raise SystemExit(2)
    out = {}
    for npi, st1, zip5, pay in rows:
        out[npi] = ((st1 or "").strip(), (zip5 or "").strip(),
                    float(pay) if pay is not None else 0.0)
    return out


def build_report(items: list[dict], panel_src: str, year: int) -> dict:
    clusterable = [it for it in items if it.get("verdict") in CLUSTERABLE_VERDICTS]
    excluded = len(items) - len(clusterable)
    addr = _panel_address_map(panel_src, sorted({it["npi"] for it in clusterable}))

    groups: dict[tuple[str, str], list[dict]] = {}
    no_address = 0
    for it in clusterable:
        rec = addr.get(it["npi"])
        if rec is None or not (rec[0] and rec[1]):
            no_address += 1
            continue
        member = {
            "npi": it["npi"],
            "verdict": it["verdict"],
            "confidence": it.get("confidence"),
            "payment": rec[2],
            "specialty": it.get("specialty"),
        }
        groups.setdefault((rec[0], rec[1]), []).append(member)

    clusters = []
    singletons = 0
    for (street, zip5), members in groups.items():
        if len(members) < 2:
            singletons += 1
            continue
        members = sorted(members, key=lambda m: -m["payment"])
        npi_list = ", ".join(m["npi"] for m in members)
        clusters.append({
            "address": {"street": street, "zip5": zip5},
            "n_members": len(members),
            "combined_payment": round(sum(m["payment"] for m in members), 2),
            "members": members,
            "suggested_subject": (
                f"Practice cluster NPIs {npi_list} at {street}, ZIP {zip5} — "
                f"investigate as office_suite analysis unit; co-billers are queue-flagged"
            ),
        })
    clusters.sort(key=lambda c: -c["combined_payment"])
    return {
        "year": year,
        "n_queue_items": len(items),
        "n_clusterable": len(clusterable),
        "n_excluded_low_dismiss": excluded,
        "n_no_panel_address": no_address,
        "n_singletons": singletons,
        "n_clusters": len(clusters),
        "clusters": clusters,
    }


def print_table(report: dict) -> None:
    print(f"=== Practice clusters — score year {report['year']} ===")
    print(f"  queue items: {report['n_queue_items']} | clusterable (HIGH+MODERATE): "
          f"{report['n_clusterable']} | excluded LOW/DISMISS: {report['n_excluded_low_dismiss']} | "
          f"no panel address: {report['n_no_panel_address']} | singletons: {report['n_singletons']}")
    if not report["clusters"]:
        print("  No multi-member practice clusters in this queue.")
        return
    for i, c in enumerate(report["clusters"], 1):
        a = c["address"]
        print(f"\n[{i}] {a['street']}, ZIP {a['zip5']} — {c['n_members']} members, "
              f"combined Tot_Mdcr_Pymt_Amt ${c['combined_payment']:,.0f}")
        hdr = f"    {'npi':>10}  {'verdict':<13} {'conf':>5} {'payment':>13}  specialty"
        print(hdr)
        for m in c["members"]:
            conf_s = "—" if m["confidence"] is None else f"{m['confidence']:.2f}"
            pay_s = f"${m['payment']:,.0f}"
            print(f"    {m['npi']:>10}  {m['verdict']:<13} {conf_s:>5} "
                  f"{pay_s:>13}  {m['specialty'] or '—'}")
        print(f"    → {c['suggested_subject']}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--queue", required=True, help="Triage queue JSON (TriageOutput schema)")
    ap.add_argument("--year", required=True, type=int, help="Panel year (cms_partb_{year}.parquet)")
    ap.add_argument("--panel", default=None, help="Explicit panel parquet path (overrides year lookup)")
    ap.add_argument("--format", choices=["table", "json"], default="table")
    ap.add_argument("--out", default=None, help="Also write the JSON report to this path")
    args = ap.parse_args(argv)

    queue = json.loads(Path(args.queue).read_text())
    report = build_report(queue.get("items") or [], resolve_panel(args.year, args.panel), args.year)
    if args.out:
        Path(args.out).write_text(json.dumps(report, indent=2))
    if args.format == "json":
        print(json.dumps(report, indent=2))
    else:
        print_table(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
