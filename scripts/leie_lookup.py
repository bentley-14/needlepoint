"""LEIE exclusion lookup — R2-backed agent tool.

Fetches the canonical LEIE parquet from r2:needlepoint-data/processed/v1/leie.parquet
on demand (cached locally for 24 h per session). Returns JSON for a given NPI.

PRODUCTION needlepoint pipeline only — wire into the production agent
allowlist. Do NOT add to calibration sandbox allowlist (gold_set was
historically derived against LEIE — leak risk).

Usage:
    python3 scripts/leie_lookup.py --npi 1234567890
        → {"npi": "1234567890", "excluded": true, "excldate": "20180315",
           "excltype": "1128a1", "lastname": "...", ...}

    cat npis.txt | python3 scripts/leie_lookup.py --bulk
        → one JSON object per line
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import pandas as pd

R2_REMOTE = "r2:needlepoint-data/processed/v1/leie.parquet"
CACHE_DIR = Path.home() / ".cache" / "fraud-detection" / "leie"
CACHE_FILE = CACHE_DIR / "leie.parquet"
CACHE_TTL_HOURS = 24


def _refresh_cache_if_stale() -> None:
    if CACHE_FILE.exists():
        age_hours = (time.time() - CACHE_FILE.stat().st_mtime) / 3600
        if age_hours < CACHE_TTL_HOURS:
            return
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from fraudlib import r2  # private bucket (rclone) or the public mirror (NP_DATA_URL)
    r2.r2_copy(R2_REMOTE, CACHE_FILE)


_DF_CACHE: pd.DataFrame | None = None


def _load_df() -> pd.DataFrame:
    global _DF_CACHE
    if _DF_CACHE is None:
        _refresh_cache_if_stale()
        _DF_CACHE = pd.read_parquet(CACHE_FILE)
    return _DF_CACHE


def lookup(npi: str) -> dict:
    df = _load_df()
    npi_z = str(npi).zfill(10)
    matches = df[df["NPI"].astype(str).str.zfill(10) == npi_z]
    if matches.empty:
        return {"npi": npi_z, "excluded": False}
    row = matches.iloc[0]
    reindate = str(row.get("REINDATE", "") or "")
    return {
        "npi": npi_z,
        "excluded": True,
        "excldate": str(row.get("EXCLDATE", "") or ""),
        "excltype": str(row.get("EXCLTYPE", "") or ""),
        "lastname": str(row.get("LASTNAME", "") or ""),
        "firstname": str(row.get("FIRSTNAME", "") or ""),
        "busname": str(row.get("BUSNAME", "") or ""),
        "specialty": str(row.get("SPECIALTY", "") or ""),
        "state": str(row.get("STATE", "") or ""),
        "reinstated": reindate if reindate and reindate != "00000000" else None,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--npi", help="Single NPI to check (10 digits)")
    ap.add_argument(
        "--bulk", action="store_true",
        help="Read one NPI per line from stdin; emit JSONL",
    )
    args = ap.parse_args()
    if args.npi:
        print(json.dumps(lookup(args.npi)))
    elif args.bulk:
        for line in sys.stdin:
            npi = line.strip()
            if not npi:
                continue
            print(json.dumps(lookup(npi)))
    else:
        ap.print_help()
        sys.exit(2)


if __name__ == "__main__":
    main()
