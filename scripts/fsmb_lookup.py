"""Per-NPI state medical-board action lookup (FSMB dataset).

Thin, schema-correct wrapper over data/processed/state_board_actions_fsmb.parquet
(columns: npi, name, action_date, action_type, state, source) so investigator agents
stop hand-writing duckdb with guessed column names. Returns any state-board disciplinary
actions on record for an NPI / name / state.

NOTE (2026-06-01): the FSMB parquet is currently a near-empty STUB (not yet ingested at
scale). Until it's populated, prefer `scripts/revoked_provider_lookup.py` (CMS § 424.535
revocations — populated) for enforcement/discipline signal, and WebSearch the relevant
state medical board for license status/discipline. This tool is correct + ready for when
the FSMB data lands.

Usage:
    python scripts/fsmb_lookup.py --npi 1900000075
    python scripts/fsmb_lookup.py --last-name Cedar --state IN
"""
from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))  # repo root, so fraudlib imports

import argparse
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
    r2 = _LocalR2()

REPO = Path(__file__).resolve().parents[1]
FSMB = REPO / "data/processed/state_board_actions_fsmb.parquet"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--npi", help="10-digit NPI")
    ap.add_argument("--last-name", help="provider last name (ILIKE)")
    ap.add_argument("--state", help="2-letter state")
    args = ap.parse_args(argv)
    if not (args.npi or args.last_name or args.state):
        print("ERROR: pass --npi, --last-name, and/or --state", file=sys.stderr)
        return 2

    _src = r2.parquet_source(FSMB, f"{r2.R2_PANELS}/{FSMB.name}")
    con = r2.duckdb_r2()
    total = con.execute(f"SELECT COUNT(*) FROM read_parquet('{_src}')").fetchone()[0]
    conds = []
    if args.npi:
        conds.append(f"CAST(npi AS VARCHAR) = '{args.npi.strip()}'")
    if args.last_name:
        conds.append(f"name ILIKE '%{args.last_name.replace(chr(39), chr(39)*2)}%'")
    if args.state:
        conds.append(f"UPPER(state) = '{args.state.strip().upper()}'")
    where = " AND ".join(conds)
    rows = con.execute(
        f"SELECT npi, name, action_date, action_type, state, source "
        f"FROM read_parquet('{_src}') WHERE {where} ORDER BY action_date DESC").fetchall()
    print(f"=== FSMB state-board actions (dataset rows: {total:,}) ===")
    if total <= 5:
        print("  ⚠ FSMB parquet is a STUB (not yet ingested at scale). Use "
              "revoked_provider_lookup.py + WebSearch the state board until populated.\n")
    if not rows:
        print("  No FSMB board actions on record for this query.")
        return 0
    for npi, name, adate, atype, state, source in rows:
        print(f"  [{adate}] {name} (NPI {npi}, {state}) — {atype}  [{source}]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
