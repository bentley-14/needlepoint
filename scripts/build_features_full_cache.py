"""One-shot builder for ``features_full_{year}.parquet`` merged caches.

Iterates 2013-2023, calls ``load_feature_frame(year, attach_extras=True)``,
which triggers the new full-cache write path inside ``feature_frames.py``.

Run with:
    python scripts/build_features_full_cache.py
    python scripts/build_features_full_cache.py --years 2022,2023  # subset
"""
from __future__ import annotations

import argparse
import gc
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fraudlib.ranker.feature_frames import load_feature_frame, FEATURE_CACHE


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", default="2013,2014,2015,2016,2017,2018,2019,2020,2021,2022,2023",
                    help="Comma-separated years (default: 2013-2023)")
    ap.add_argument("--force", action="store_true",
                    help="Delete existing full caches before rebuilding")
    args = ap.parse_args()

    years = [int(y) for y in args.years.split(",")]

    print(f"=== building full caches for years: {years} ===")
    t_total = time.time()
    for y in years:
        full_path = FEATURE_CACHE / f"features_full_{y}.parquet"
        if args.force and full_path.exists():
            full_path.unlink()
            print(f"  {y}: removed existing full cache (--force)")
        t0 = time.time()
        # The cache write happens inside load_feature_frame; nothing extra needed
        df = load_feature_frame(y, attach_extras=True, use_full_cache=True)
        elapsed = time.time() - t0
        print(f"  {y}: done ({elapsed:.1f}s, {len(df):,} rows × {len(df.columns)} cols)")
        del df
        gc.collect()
    elapsed_total = time.time() - t_total
    print(f"\n=== built {len(years)} year caches in {elapsed_total/60:.1f} min ===")


if __name__ == "__main__":
    raise SystemExit(main() or 0)
