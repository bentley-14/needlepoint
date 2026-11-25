#!/usr/bin/env python3
"""Pull + ingest the RELATIONSHIP data layer (tasks #73-75) — the evidence for
the dominant fraud MO (kickback/recruitment, 38% of org convictions), which is
invisible in billing shape (validated: Genetic Tech scored zero on every
aggregate signal despite conviction).

Sources (all free, api.data.cms.gov 'provider-data' / data.cms.gov distributions):
  ownership   Open Payments OWNERSHIP/investment-interest records (2017+):
              physician ownership in the entities they refer to — the direct
              kickback-MO evidence.  -> data/processed/openpayments_ownership.parquet
  pecos       Medicare FFS Public Provider Enrollment + REASSIGNMENT sub-file:
              individual->org reassignment edges (sharper entity resolution
              than Org_PAC_ID).     -> data/processed/pecos_reassignment.parquet
  pos         Provider of Services file (CCN-keyed facility characteristics):
              the CCN<->NPI bridge that makes the 34 facility labels usable.
              -> data/processed/pos_facility.parquet

Design: each source = discover latest distribution via the data.cms.gov
dataset API (stable dataset UUIDs), stream-download CSV, normalize minimal
columns, write parquet (atomic tmp+rename), print row counts. Failures are
per-source (one bad source never kills the others). Push to R2 is left to the
existing `make push-data` flow (additive, maintainer-triggered).

Usage:
    python scripts/pull_relationship_data.py --sources ownership,pecos,pos
    python scripts/pull_relationship_data.py --sources ownership --dry-run
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
import urllib.request
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = REPO_ROOT / "data" / "processed"
UA = {"User-Agent": "needlepoint-data/1.0"}

# data.cms.gov dataset identifiers (stable across releases; the API returns the
# latest distribution). Verified reachable 2026-07 via the public catalog.
DATASETS = {
    "ownership": {
        "title_match": "ownership",
        "catalog": "https://openpaymentsdata.cms.gov/api/1/metastore/schemas/dataset/items?show-reference-ids",
        "keep": None,   # keep all columns; ownership files are small
        "out": "openpayments_ownership.parquet",
    },
    "pecos": {
        "catalog": "https://data.cms.gov/data.json",
        "title_match": "revalidation reassignment list",
        "out": "pecos_reassignment.parquet",
        "keep": None,
    },
    "pos": {
        "catalog": "https://data.cms.gov/data.json",
        "title_match": "provider of services file - clinical laboratories",
        "title_match_alt": "provider of services",
        "out": "pos_facility.parquet",
        "keep": None,
    },
}


def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=180) as r:
        return r.read()


def _latest_csv_distribution(catalog_url: str, title_match: str,
                             alt: str | None = None) -> tuple[str, str] | None:
    """(title, csv_url) of the newest dataset whose title matches."""
    doc = json.loads(_get(catalog_url).decode("utf-8", "replace"))
    # data.cms.gov data.json -> {"dataset":[...]}; openpaymentsdata metastore -> bare list
    items = doc if isinstance(doc, list) else doc.get("dataset", [])
    best: tuple[str, str, str] | None = None   # (modified, title, url)
    for d in items:
        title = str(d.get("title", ""))
        tl = title.lower()
        if title_match not in tl and not (alt and alt in tl):
            continue
        for dist in d.get("distribution", []) or []:
            # metastore nests the payload under "data"
            payload = dist.get("data") if isinstance(dist.get("data"), dict) else dist
            url = payload.get("downloadURL") or payload.get("accessURL") or ""
            fmt = str(payload.get("format") or payload.get("mediaType") or "").lower()
            if url and ("csv" in fmt or ".csv" in url.lower()):
                mod = str(d.get("modified", ""))
                if best is None or mod > best[0]:
                    best = (mod, title, url)
    return (best[1], best[2]) if best else None


def pull(source: str, dry_run: bool = False) -> bool:
    spec = DATASETS[source]
    hit = _latest_csv_distribution(spec["catalog"], spec["title_match"],
                                   spec.get("title_match_alt"))
    if not hit:
        print(f"[{source}] no CSV distribution found (catalog change?) — SKIPPED")
        return False
    title, url = hit
    print(f"[{source}] {title}\n          {url}")
    if dry_run:
        return True
    raw = _get(url)
    print(f"[{source}] downloaded {len(raw)/1e6:.1f} MB")
    try:
        df = pd.read_csv(io.BytesIO(raw), dtype=str, low_memory=False,
                         on_bad_lines="skip")
    except UnicodeDecodeError:
        # CMS enrollment files ship windows-1252/latin-1 (PECOS reassignment)
        df = pd.read_csv(io.BytesIO(raw), dtype=str, low_memory=False,
                         on_bad_lines="skip", encoding="latin-1")
    out = OUT_DIR / spec["out"]
    tmp = out.with_suffix(".parquet.tmp")
    df.to_parquet(tmp, index=False)
    os.replace(tmp, out)
    print(f"[{source}] -> {out} ({len(df):,} rows, {len(df.columns)} cols)")
    return True


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--sources", default="ownership,pecos,pos")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    ok = 0
    for s in [x.strip() for x in args.sources.split(",") if x.strip()]:
        if s not in DATASETS:
            print(f"unknown source {s}"); continue
        try:
            ok += bool(pull(s, args.dry_run))
        except Exception as e:
            print(f"[{s}] FAILED: {str(e)[:200]}")
    print(f"{ok} source(s) ok")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
