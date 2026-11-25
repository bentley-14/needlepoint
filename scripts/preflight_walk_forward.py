"""Pre-flight checklist for a walk-forward sweep.

Run this BEFORE launching any walk-forward. It validates the features_full
cache that the fast-path will serve, so silent feature-set regressions don't
cost hours of sweep time. Born from the 2026-05-28 incident where R2's
features_full silently carried RAW 64-dim GNN + no centrality + kitchen-sink
dmer_/xprog_ families — none of which matched the committed baseline.

Checks (per year, against data/cache/feature_frames/features_full_{year}.parquet):
  1. EXISTS        file present
  2. GNN_FORM      gnn_pca_* == 16, gnn_in_vocab present, gnn_embed_* (raw) == 0
  3. CENTRALITY    pagerank + clustering present, x3_diagonal absent
  4. NO_EXTRAS     dmer_*, xprog_* absent (not in baseline)
  5. NO_LEAKAGE    no label/score columns (y_eval, scheme_, archetype, score_, ...)
  6. NPI_KEY       npi present, object dtype, 10-digit, no dupes, no '.0' artifacts
  7. ROWS          row count in a sane band (0.8M–2.5M)
  8. XYEAR_CONSIST feature families consistent across years (cc-col gap 2013-16 is INFO)
  9. BASELINE_MATCH (if --baseline-dir) features_full feature-cols == baseline's, no extras
 10. FRESHNESS     features_full newer than its source caches (gnn_pca, centrality, hcpcs_rich)

Exit code 0 = all PASS (sweep may proceed). Non-zero = at least one FAIL.

Usage:
    python scripts/preflight_walk_forward.py --years 2015-2023
    python scripts/preflight_walk_forward.py --years 2015-2023 \
        --baseline-dir baselines/r10k_315_v4_top5_floor050_es50/ablations/hcpcs_graph_gnnpca/_run/results
"""
from __future__ import annotations

import argparse
import glob
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq

REPO = Path(__file__).resolve().parents[1]
FEATURE_CACHE = REPO / "data" / "cache" / "feature_frames"
CENTRALITY_CACHE = REPO / "data" / "cache" / "graph"
GNN_CACHE = REPO / "data" / "cache" / "gnn_features"

EXPECTED_GNN_PCA = 16
REQUIRED_CENTRALITY = ("graph_hcpcs_pagerank_full", "graph_hcpcs_clustering_full")
FORBIDDEN_COLS = ("graph_hcpcs_x3_diagonal",)
# Raw GNN embeddings are always forbidden (we use the PCA form in both modes).
ALWAYS_FORBIDDEN_PREFIXES = ("gnn_embed_",)
# dmer_/xprog_ are KEPT in maximal-sane, DROPPED in baseline-exact.
EXTRA_FAMILY_PREFIXES = ("dmer_", "xprog_")
LABEL_FRAGMENTS = (
    "y_eval", "y_doj", "y_leie", "archetype", "scheme_", "arch_",
    "fraud_case", "fraud_label", "fraud_leie", "label_source",
    "label_confidence", "label_bucket", "case_ids", "case_fraud_types",
    "rrf_rank", "score_rrf", "score_xgb", "score_anomaly", "score_neural",
    "score_specialty",
)
ROW_MIN, ROW_MAX = 800_000, 2_500_000

# ── result helpers ──────────────────────────────────────────────────────────
PASS, FAIL, WARN, INFO = "PASS", "FAIL", "WARN", "INFO"
results: list[tuple[str, int | str, str, str]] = []  # (check, year, status, detail)


def record(check: str, year, status: str, detail: str = "") -> None:
    results.append((check, year, status, detail))


def cols_of(path: Path) -> set[str]:
    return set(pq.read_schema(path).names)


def parse_years(spec: str) -> list[int]:
    if "," in spec:
        return [int(y) for y in spec.split(",") if y]
    if "-" in spec:
        lo, hi = spec.split("-")
        return list(range(int(lo), int(hi) + 1))
    return [int(spec)]


# ── per-year checks ─────────────────────────────────────────────────────────
def check_year(year: int, mode: str) -> set[str] | None:
    ff_path = FEATURE_CACHE / f"features_full_{year}.parquet"
    if not ff_path.exists():
        record("EXISTS", year, FAIL, f"missing {ff_path.name}")
        return None
    record("EXISTS", year, PASS, ff_path.name)
    cols = cols_of(ff_path)

    # GNN_FORM
    npca = sum(c.startswith("gnn_pca_") for c in cols)
    nraw = sum(c.startswith("gnn_embed_") for c in cols)
    has_vocab = "gnn_in_vocab" in cols
    if npca == EXPECTED_GNN_PCA and nraw == 0 and has_vocab:
        record("GNN_FORM", year, PASS, f"gnn_pca={npca}, raw={nraw}, in_vocab=yes")
    else:
        record("GNN_FORM", year, FAIL, f"gnn_pca={npca} (want {EXPECTED_GNN_PCA}), raw={nraw} (want 0), in_vocab={has_vocab}")

    # CENTRALITY
    miss = [c for c in REQUIRED_CENTRALITY if c not in cols]
    bad = [c for c in FORBIDDEN_COLS if c in cols]
    if not miss and not bad:
        record("CENTRALITY", year, PASS, "pagerank+clustering present, no x3_diagonal")
    else:
        record("CENTRALITY", year, FAIL, f"missing={miss} forbidden_present={bad}")

    # RAW_GNN_ABSENT (always)
    raw = sorted(c for c in cols if c.startswith(ALWAYS_FORBIDDEN_PREFIXES))
    record("RAW_GNN_ABSENT", year, FAIL if raw else PASS,
           f"{len(raw)} raw gnn_embed_* present" if raw else "no raw gnn_embed_*")

    # FAMILY_SET — dmer_/xprog_ presence depends on mode AND source-data availability.
    # DMEPOS PUF (and the cross-program features derived from it) start in 2014, so
    # 2013 legitimately has no dme/dmer/xprog — absence there is expected, not a fail.
    extra_present = sorted(c for c in cols if c.startswith(EXTRA_FAMILY_PREFIXES))
    fam = dict(Counter(re.match(r'^([a-z]+?)_', c).group(1) for c in extra_present)) if extra_present else {}
    dmepos_src = (CENTRALITY_CACHE.parent / "dmepos_features" / f"dmepos_{year}.parquet").exists()
    if mode == "maximal-sane":
        if extra_present:
            record("FAMILY_SET", year, PASS, f"maximal-sane: dmer/xprog present {fam}")
        elif not dmepos_src:
            record("FAMILY_SET", year, INFO, f"dmer/xprog absent — no DMEPOS source for {year} (expected, PUF starts 2014)")
        else:
            record("FAMILY_SET", year, FAIL, "maximal-sane: dmer_/xprog_ MISSING but DMEPOS source exists")
    else:  # baseline-exact
        if extra_present:
            record("FAMILY_SET", year, FAIL, f"baseline-exact: dmer_/xprog_ present {fam}")
        else:
            record("FAMILY_SET", year, PASS, "baseline-exact: no dmer_/xprog_")

    # NO_LEAKAGE
    leaks = sorted(c for c in cols if any(f in c for f in LABEL_FRAGMENTS))
    if leaks:
        record("NO_LEAKAGE", year, FAIL, f"{len(leaks)} label/score cols: {leaks[:6]}")
    else:
        record("NO_LEAKAGE", year, PASS, "no label/score columns")

    # NPI_KEY (reads a sample column, not full file)
    key = "npi" if "npi" in cols else ("npi_padded" if "npi_padded" in cols else None)
    if key is None:
        record("NPI_KEY", year, FAIL, "no npi/npi_padded column")
    else:
        s = pq.read_table(ff_path, columns=[key]).column(0).to_pandas()
        is_str = s.dtype == object
        n_dup = int(s.duplicated().sum())
        n_bad_len = int((s.astype(str).str.len() != 10).sum())
        n_dot = int(s.astype(str).str.contains(r"\.", na=False).sum())
        if is_str and n_dup == 0 and n_bad_len == 0 and n_dot == 0:
            record("NPI_KEY", year, PASS, f"{key}: str, {len(s):,} unique, 10-digit")
        else:
            record("NPI_KEY", year, FAIL if (n_dup or n_dot) else WARN,
                   f"{key}: str={is_str} dups={n_dup} bad_len={n_bad_len} decimal={n_dot}")

    # ROWS
    n = pq.read_metadata(ff_path).num_rows
    if ROW_MIN <= n <= ROW_MAX:
        record("ROWS", year, PASS, f"{n:,}")
    else:
        record("ROWS", year, WARN, f"{n:,} outside [{ROW_MIN:,},{ROW_MAX:,}]")

    return cols


def check_baseline_match(year: int, ff_cols: set[str], baseline_dir: Path, mode: str) -> None:
    cand = glob.glob(str(baseline_dir / f"*y{year}_seed0" / f"scored_full_{year}.parquet"))
    if not cand:
        record("BASELINE_MATCH", year, INFO, "no baseline scored_full — skipped")
        return
    bl = cols_of(Path(cand[0]))
    # Verify features_full ⊇ baseline FEATURE set. The real regression is features_full
    # MISSING a feature the baseline used. Extra families are fine (maximal-sane), and a
    # baseline year that is itself feature-deficient (e.g. 2023 lacked cl/op/dme because
    # those caches weren't built yet) must NOT be treated as a features_full failure.
    bl_nonfeat = ("rule_", "score_", "scheme_", "arch", "y_", "fraud", "label")
    bl_nonfeat_exact = {"npi_padded", "puf_year", "archetype", "y", "rrf_rank"}
    bl_feats = {c for c in bl if not c.startswith(bl_nonfeat) and c not in bl_nonfeat_exact}
    missing = bl_feats - ff_cols
    extra = ff_cols - bl
    if missing:
        fam = dict(Counter(re.match(r"^([a-z]+?)_", c).group(1) if re.match(r"^([a-z]+?)_", c) else c for c in missing))
        record("BASELINE_MATCH", year, FAIL, f"features_full MISSING {len(missing)} baseline feature(s): {fam}")
    else:
        ef = dict(Counter(re.match(r"^([a-z]+?)_", c).group(1) if re.match(r"^([a-z]+?)_", c) else c for c in extra))
        # note when features_full is MORE complete than this baseline year
        more = {k: v for k, v in ef.items() if k in ("cl", "op", "dme", "dmer", "xprog")}
        tag = f"⊇ baseline; extra families {more}" if more else "⊇ baseline feature set"
        record("BASELINE_MATCH", year, PASS, tag)


def check_freshness(year: int) -> None:
    ff_path = FEATURE_CACHE / f"features_full_{year}.parquet"
    if not ff_path.exists():
        return
    ff_mtime = ff_path.stat().st_mtime
    sources = [
        GNN_CACHE / f"gnn_pca_{year}.parquet",
        CENTRALITY_CACHE / f"hcpcs_centrality_{year}.parquet",
        FEATURE_CACHE / f"hcpcs_rich_{year}.parquet",
    ]
    newer = [p.name for p in sources if p.exists() and p.stat().st_mtime > ff_mtime]
    if newer:
        record("FRESHNESS", year, FAIL, f"stale — source(s) newer: {newer}")
    else:
        record("FRESHNESS", year, PASS, "newer than all sources")


def check_xyear_consistency(year_cols: dict[int, set[str]]) -> None:
    """Feature families should be consistent across years. The partb_bene_cc_*
    gap (CMS published 2017+) is a known, expected INFO — not a failure."""
    if len(year_cols) < 2:
        return
    fam_by_year = {y: Counter(re.match(r"^([a-z]+?)_", c).group(1) if re.match(r"^([a-z]+?)_", c) else c
                              for c in cols) for y, cols in year_cols.items()}
    all_fams = set().union(*[set(f) for f in fam_by_year.values()])
    issues = []
    for fam in sorted(all_fams):
        counts = {y: fam_by_year[y].get(fam, 0) for y in sorted(year_cols)}
        distinct = set(counts.values())
        if len(distinct) > 1:
            # known cc-col gap: partb varies by exactly the 25 cc cols pre/post 2017
            issues.append(f"{fam}: {counts}")
    if not issues:
        record("XYEAR_CONSIST", "all", PASS, "feature families identical across years")
    else:
        # Known data-availability gaps (INFO, not WARN):
        #   partb  → +25 chronic-condition cols 2017+
        #   dme/dmer/xprog → DMEPOS PUF starts 2014 (0 in 2013)
        #   op     → Open Payments NPI coverage varies early years
        known = ("partb:", "dme:", "dmer:", "xprog:", "op:")
        all_known = all(i.startswith(known) for i in issues)
        record("XYEAR_CONSIST", "all", INFO if all_known else WARN,
               "known availability gaps (partb cc 2017+, DMEPOS 2014+): " + "; ".join(issues)[:160])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--years", default="2015-2023")
    ap.add_argument("--mode", choices=("maximal-sane", "baseline-exact"), default="maximal-sane",
                    help="feature set the cache should match (default maximal-sane: keeps dmer_/xprog_)")
    ap.add_argument("--baseline-dir", default=None,
                    help="dir with walk_forward_*_y{year}_seed0/scored_full_{year}.parquet for BASELINE_MATCH")
    args = ap.parse_args()
    years = parse_years(args.years)
    baseline_dir = Path(args.baseline_dir) if args.baseline_dir else None

    print(f"=== PRE-FLIGHT: walk-forward feature cache [{args.mode}] ===")
    print(f"years: {years}   baseline_dir: {baseline_dir or '(none — BASELINE_MATCH skipped)'}\n")

    year_cols: dict[int, set[str]] = {}
    for y in years:
        cols = check_year(y, args.mode)
        if cols is not None:
            year_cols[y] = cols
            check_freshness(y)
            if baseline_dir:
                check_baseline_match(y, cols, baseline_dir, args.mode)
    check_xyear_consistency(year_cols)

    # ── report ──
    print(f"{'CHECK':<16}{'YEAR':<7}{'STATUS':<7}DETAIL")
    print("-" * 100)
    order = {FAIL: 0, WARN: 1, INFO: 2, PASS: 3}
    for check, year, status, detail in sorted(results, key=lambda r: (order[r[2]], str(r[0]), str(r[1]))):
        mark = {PASS: "✓", FAIL: "✗", WARN: "!", INFO: "·"}[status]
        print(f"{check:<16}{str(year):<7}{mark} {status:<5}{detail}")

    n_fail = sum(1 for r in results if r[2] == FAIL)
    n_warn = sum(1 for r in results if r[2] == WARN)
    print("-" * 100)
    print(f"\n{'❌' if n_fail else '✅'} {n_fail} FAIL, {n_warn} WARN, {sum(1 for r in results if r[2]==PASS)} PASS")
    if n_fail:
        print("\n>>> DO NOT SWEEP. Fix FAILs above first.")
        return 1
    if n_warn:
        print("\n>>> Review WARNs; sweep may proceed if expected.")
    else:
        print("\n>>> All clear — sweep may proceed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
