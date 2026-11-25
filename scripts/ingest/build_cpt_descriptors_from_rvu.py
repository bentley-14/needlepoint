#!/usr/bin/env python3
"""Build a per-year CANONICAL CPT/HCPCS descriptor table from the CMS Medicare
Physician Fee Schedule Relative Value (RVU) files — the authoritative free source.

WHY: the HCPCS reference descriptors were sourced from the CMS Part B PUF's
denormalized `HCPCS_Desc`, which is wrong for some codes (e.g. 99489 shown as
"each additional 60 minutes" in 2021-2024; the AMA-CPT value is 30). The MPFS RVU
file (RVUyyA) is CMS's authoritative per-year file: every payable CPT/HCPCS code +
its short descriptor (carries the correct time quantum) + status indicator. CMS is
licensed to publish the SHORT descriptor; the AMA-copyrighted LONG descriptor is not
freely redistributable, so this captures the authoritative short descriptor.

Output: data/ground_truth/cpt_descriptors_by_year.parquet
  columns: code, year, short_descriptor, status_code, source

Usage:
  python3 scripts/ingest/build_cpt_descriptors_from_rvu.py            # all mapped years
  python3 scripts/ingest/build_cpt_descriptors_from_rvu.py --years 2024
Pre-2022 RVU files live behind legacy CMS URLs not in the map yet (TODO: add).
"""
from __future__ import annotations

import argparse, csv, io, re, sys, urllib.request, zipfile
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parents[2]
OUT = REPO / "data" / "ground_truth" / "cpt_descriptors_by_year.parquet"
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"}

# year -> RVU zip URL. Two CMS hosting patterns (site was reorganized ~2022):
#   2013-2019: legacy /Medicare/.../PhysicianFeeSched/Downloads/RVU{YY}A.zip
#   2022-2026: current /files/zip/rvu{YY}a.zip
# 2020 & 2021: CMS removed these from both live paths AND they are not in the
# Wayback CDX (verified 2026-06-11). They are intentionally absent here — the
# canonicalize step falls back to the PUF descriptor for 2020/2021 with an
# explicit provenance flag (NOT a silent gap); 2020 PUF is error-free and 2021's
# known errors are corrected by the authoritative-override layer.
_LEGACY = "https://www.cms.gov/Medicare/Medicare-Fee-for-Service-Payment/PhysicianFeeSched/Downloads/RVU{YY}A.zip"
_CURRENT = "https://www.cms.gov/files/zip/rvu{yy}a.zip"
YEAR_URL = {y: _LEGACY.format(YY=str(y)[2:]) for y in range(2013, 2020)}
YEAR_URL.update({y: _CURRENT.format(yy=str(y)[2:]) for y in range(2022, 2027)})
_CODE_RE = re.compile(r"^[0-9]{4}[0-9A-Z]$|^[A-Z][0-9]{4}$")  # CPT 5-digit or HCPCS-II A####


def _pprrvu_rows(zbytes: bytes, year: int) -> list[dict]:
    zf = zipfile.ZipFile(io.BytesIO(zbytes))
    name = next((n for n in zf.namelist() if re.search(r"pprrvu.*\.csv$", n, re.I)), None)
    if not name:
        raise RuntimeError(f"no PPRRVU csv in RVU zip for {year}")
    text = zf.read(name).decode("latin-1")
    out, seen = [], set()
    for row in csv.reader(io.StringIO(text)):
        if len(row) < 4:
            continue
        code = (row[0] or "").strip().strip('"').upper()
        mod = (row[1] or "").strip()
        desc = (row[2] or "").strip()
        status = (row[3] or "").strip()
        if not _CODE_RE.match(code) or not desc:
            continue
        if code in seen and mod:            # prefer the base (blank-modifier) row
            continue
        seen.add(code)
        out.append({"code": code, "year": year, "short_descriptor": desc,
                    "status_code": status, "source": "cms_mpfs_rvu"})
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--years", help="comma list (default: all mapped)")
    args = ap.parse_args()
    years = ([int(y) for y in args.years.split(",")] if args.years
             else sorted(YEAR_URL))
    rows: list[dict] = []
    for y in years:
        url = YEAR_URL.get(y)
        if not url:
            print(f"  {y}: no URL mapped (legacy — skipping)", file=sys.stderr)
            continue
        print(f"  {y}: downloading {url} ...", file=sys.stderr)
        data = urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=120).read()
        yr = _pprrvu_rows(data, y)
        print(f"  {y}: {len(yr):,} codes", file=sys.stderr)
        rows.extend(yr)
    if not rows:
        print("no rows", file=sys.stderr); return 1
    df = pd.DataFrame(rows)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT.with_suffix(".parquet.tmp")
    df.to_parquet(tmp, index=False)
    tmp.replace(OUT)
    print(f"wrote {OUT} — {len(df):,} rows, {df['code'].nunique():,} codes, "
          f"years {df['year'].min()}-{df['year'].max()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
