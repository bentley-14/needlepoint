"""Per-NPI group-practice / billing-entity structure from the CMS Doctors & Clinicians
(DAC, formerly Physician Compare) National Downloadable File.

The headline field is **Org_PAC_ID** (Group PAC ID) — the group practice's
billing-entity (≈ TIN-level) identity. NPPES gives only a practice *address*; the DAC
tells you who actually **shares a billing entity**, which is what kickback /
shared-group analysis needs:
  - two NPIs under the SAME Org_PAC_ID            = same billing entity (a group/ring)
  - co-located NPIs under DIFFERENT Org_PAC_IDs   = structuring signal (pill-mill/kickback)
  - one Org_PAC_ID across many addresses          = multi-site group (explains volume)

Full domain primer (acronyms, schemes, joins, drift): run
`python scripts/source_primer.py --source physician_compare`.

The DAC is an enrollment roster (no dollars/volume) — it's a STRUCTURAL lens on volume
found in the claims PUFs (Part B/D/DMEPOS). Multi-row per NPI (NPI × Org_PAC_ID ×
address). Snapshot-based (no data_year); the current symlink is queried by default.

Usage:
    python scripts/physician_compare_lookup.py --npi 1900000075                 # the NPI's enrollments
    python scripts/physician_compare_lookup.py --npi 1900000075 --shared-entity # who shares its Org_PAC_ID
    python scripts/physician_compare_lookup.py --npi 1900000075 --co-located    # same address, diff entity
    python scripts/physician_compare_lookup.py --org-pac-id 1234567890          # full roster of an entity
    python scripts/physician_compare_lookup.py --npi 1900000075 --snapshot 2024-06 --format json
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
    from pathlib import Path as _RP
    class _LocalR2:
        R2_PANELS = ""
        @staticmethod
        def parquet_source(local, key): return str(local)
        @staticmethod
        def fetch_local(local, key): return _RP(local)
        @staticmethod
        def attach_r2(con): pass
        @staticmethod
        def duckdb_r2():
            import duckdb; return duckdb.connect()
        @staticmethod
        def list_v1_years(family): return []
    r2 = _LocalR2()

REPO = Path(__file__).resolve().parents[1]
REG = REPO / "data/registries"

_COLS = ["npi", "Ind_PAC_ID", "Org_PAC_ID", "Org_Lgl_Nm", "Num_Org_Mem",
         "pri_spec", "adr_ln_1", "City", "state", "ZIP_Code", "snapshot"]


def _source(snapshot: str | None) -> str:
    """Resolve the local-or-R2 parquet for the current symlink or a dated snapshot."""
    fname = f"physician_compare_{snapshot}.parquet" if snapshot else "physician_compare.parquet"
    return r2.parquet_source(REG / fname, f"{r2.R2_PANELS}/{fname}")


def _present(con, src: str) -> list[str]:
    try:
        cols = {r[0] for r in con.execute(f"DESCRIBE SELECT * FROM read_parquet('{src}')").fetchall()}
    except duckdb.Error as e:
        print(f"ERROR: cannot open DAC parquet ({src}): {e}", file=sys.stderr)
        return []
    return [c for c in _COLS if c in cols]


def _query(con, src: str, where: str) -> list[dict]:
    keep = _present(con, src)
    if not keep:
        return []
    try:
        res = con.execute(
            f"SELECT {', '.join(keep)} FROM read_parquet('{src}') WHERE {where}"
        ).fetchall()
    except duckdb.Error as e:
        print(f"  query failed: {e}", file=sys.stderr)
        return []
    return [dict(zip(keep, r)) for r in res]


def _fmt_rows(rows: list[dict]) -> str:
    if not rows:
        return "  (none)"
    out = []
    for r in rows:
        addr = " ".join(str(r.get(k, "") or "") for k in ("adr_ln_1", "City", "state", "ZIP_Code")).strip()
        out.append(f"  NPI {r.get('npi','—')}  Org_PAC_ID {r.get('Org_PAC_ID','—')}  "
                   f"\"{(r.get('Org_Lgl_Nm') or '—')}\"  (members: {r.get('Num_Org_Mem','—')})  "
                   f"{r.get('pri_spec','—')}\n      {addr}")
    return "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--npi", help="10-digit NPI")
    ap.add_argument("--org-pac-id", dest="org_pac_id", help="list every clinician in a billing entity")
    ap.add_argument("--shared-entity", action="store_true",
                    help="with --npi: other NPIs sharing this NPI's Org_PAC_ID(s) (the ring)")
    ap.add_argument("--co-located", action="store_true",
                    help="with --npi: NPIs at the same address under a DIFFERENT Org_PAC_ID (structuring)")
    ap.add_argument("--snapshot", default=None, help="dated snapshot YYYY-MM (default: current file)")
    ap.add_argument("--limit", type=int, default=50, help="max rows for entity/co-location listings")
    ap.add_argument("--format", choices=["text", "json"], default="text")
    args = ap.parse_args(argv)

    if not args.npi and not args.org_pac_id:
        print("ERROR: provide --npi or --org-pac-id", file=sys.stderr)
        return 2
    if args.npi and not (len(args.npi.strip()) == 10 and args.npi.strip().isdigit()):
        print(f"ERROR: --npi must be a 10-digit NPI; got {args.npi!r}", file=sys.stderr)
        return 2

    con = r2.duckdb_r2()
    src = _source(args.snapshot)
    result: dict = {"source": src}

    if args.org_pac_id:
        roster = _query(con, src, f"CAST(Org_PAC_ID AS VARCHAR) = '{args.org_pac_id}' LIMIT {args.limit}")
        result["entity_roster"] = roster

    if args.npi:
        npi = args.npi.strip()
        own = _query(con, src, f"CAST(npi AS VARCHAR) = '{npi}'")
        result["enrollments"] = own
        pac_ids = sorted({str(r["Org_PAC_ID"]) for r in own if r.get("Org_PAC_ID")})
        addrs = sorted({(str(r.get("adr_ln_1") or ""), str(r.get("ZIP_Code") or ""))
                        for r in own if r.get("adr_ln_1")})

        if args.shared_entity and pac_ids:
            ids = ",".join(f"'{p}'" for p in pac_ids)
            result["shared_entity"] = _query(
                con, src,
                f"CAST(Org_PAC_ID AS VARCHAR) IN ({ids}) AND CAST(npi AS VARCHAR) <> '{npi}' LIMIT {args.limit}")
        if args.co_located and addrs:
            clauses = " OR ".join(
                f"(adr_ln_1 = '{a.replace(chr(39), chr(39)*2)}' AND CAST(ZIP_Code AS VARCHAR) = '{z}')"
                for a, z in addrs if a)
            ids = ",".join(f"'{p}'" for p in pac_ids) or "''"
            result["co_located_diff_entity"] = _query(
                con, src,
                f"({clauses}) AND CAST(Org_PAC_ID AS VARCHAR) NOT IN ({ids}) "
                f"AND CAST(npi AS VARCHAR) <> '{npi}' LIMIT {args.limit}")

    if args.format == "json":
        print(json.dumps(result, indent=2, default=str))
        return 0

    print(f"=== Doctors & Clinicians (DAC) — {src} ===")
    if "enrollments" in result:
        print(f"\nEnrollments for NPI {args.npi.strip()}:")
        print(_fmt_rows(result["enrollments"]))
    if "shared_entity" in result:
        print(f"\nShared billing entity (same Org_PAC_ID — potential ring):")
        print(_fmt_rows(result["shared_entity"]))
    if "co_located_diff_entity" in result:
        print(f"\nCo-located under a DIFFERENT entity (structuring signal):")
        print(_fmt_rows(result["co_located_diff_entity"]))
    if "entity_roster" in result:
        print(f"\nRoster for Org_PAC_ID {args.org_pac_id}:")
        print(_fmt_rows(result["entity_roster"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
