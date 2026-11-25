"""Expected-value queue gate (M5) — re-order a triage queue by EV before Layer 3.

Triage output enters Layer 3 in ML-rank order today; expensive case-building
should be spent in expected-value order instead:

    EV = Tot_Mdcr_Pymt_Amt (target's --year panel row; 0 if absent)
         × confidence
         × verdict weight (HIGH_RISK 1.0, MODERATE_RISK 0.4, LOW_RISK 0.05, DISMISS 0.0)

The output (--out) is a VALID TriageOutput JSON: `items` re-sorted by EV
descending (each item's original `rank` field is kept verbatim — it documents
the ML rank; the new ordering is the file order) and `batch_summary` prefixed
with "[ev-ranked] " + a one-line method note.

Usage:
    python scripts/queue_ev_rank.py --queue validation/triage/2023_top1-500.json --year 2023 --out ev_queue.json
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

VERDICT_WEIGHTS = {
    "HIGH_RISK": 1.0,
    "MODERATE_RISK": 0.4,
    "LOW_RISK": 0.05,
    "DISMISS": 0.0,
}


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


def _payments(panel_src: str, npis: list[str]) -> dict[str, float]:
    """{npi: Tot_Mdcr_Pymt_Amt} for the queue NPIs present in the panel."""
    if not npis:
        return {}
    con = r2.duckdb_r2()
    src = panel_src.replace("'", "''")
    placeholders = ",".join("?" for _ in npis)
    try:
        rows = con.execute(
            f"SELECT CAST(Rndrng_NPI AS VARCHAR), Tot_Mdcr_Pymt_Amt "
            f"FROM read_parquet('{src}') "
            f"WHERE CAST(Rndrng_NPI AS VARCHAR) IN ({placeholders})",
            npis,
        ).fetchall()
    except duckdb.Error as e:
        print(f"ERROR: cannot read panel {panel_src}: {e}", file=sys.stderr)
        raise SystemExit(2)
    return {npi: float(pay) if pay is not None else 0.0 for npi, pay in rows}


def ev_rank(queue: dict, panel_src: str, year: int) -> tuple[dict, list[dict]]:
    """Returns (re-ordered TriageOutput-shaped dict, scored rows for the table)."""
    items = queue.get("items") or []
    pay = _payments(panel_src, sorted({it["npi"] for it in items}))
    scored = []
    for it in items:
        payment = pay.get(it["npi"], 0.0)
        weight = VERDICT_WEIGHTS.get(it.get("verdict"), 0.0)
        confidence = float(it.get("confidence") or 0.0)
        scored.append({
            "item": it,
            "npi": it["npi"],
            "ev": payment * confidence * weight,
            "payment": payment,
            "confidence": confidence,
            "verdict": it.get("verdict"),
            "ml_rank": it.get("rank"),
        })
    # EV desc; original ML rank breaks ties so the order is deterministic.
    scored.sort(key=lambda s: (-s["ev"], s["ml_rank"]))
    note = (f"[ev-ranked] EV = Tot_Mdcr_Pymt_Amt({year}) x confidence x verdict weight "
            f"(HIGH_RISK 1.0 / MODERATE_RISK 0.4 / LOW_RISK 0.05 / DISMISS 0.0); "
            f"file order = EV desc, each item's rank field keeps the original ML rank. | ")
    out = dict(queue)
    out["items"] = [s["item"] for s in scored]
    out["batch_summary"] = note + (queue.get("batch_summary") or "")
    return out, scored


def print_table(scored: list[dict]) -> None:
    hdr = f"{'npi':>10} {'ev':>14} {'payment':>13} {'conf':>5} {'verdict':<13} {'ml_rank':>7}"
    print(hdr)
    print("-" * len(hdr))
    for s in scored:
        print(f"{s['npi']:>10} {s['ev']:>14,.0f} {s['payment']:>13,.0f} "
              f"{s['confidence']:>5.2f} {s['verdict'] or '—':<13} {s['ml_rank']:>7}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--queue", required=True, help="Triage queue JSON (TriageOutput schema)")
    ap.add_argument("--year", required=True, type=int, help="Panel year for the damages proxy")
    ap.add_argument("--panel", default=None, help="Explicit panel parquet path (overrides year lookup)")
    ap.add_argument("--out", default=None, help="Write the EV-ordered TriageOutput JSON here")
    args = ap.parse_args(argv)

    queue = json.loads(Path(args.queue).read_text())
    out, scored = ev_rank(queue, resolve_panel(args.year, args.panel), args.year)
    if args.out:
        Path(args.out).write_text(json.dumps(out, indent=2))
    print_table(scored)
    return 0


if __name__ == "__main__":
    sys.exit(main())
