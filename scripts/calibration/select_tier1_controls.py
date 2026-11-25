"""Select Tier-1 negative controls — statistically TYPICAL, clean providers.

A genuinely-clean control can't be labelled from billing alone (unprosecuted ≠
clean). Tier 1 sidesteps that: pick providers who are *boringly typical* within
their peer group — near-median on every anomaly axis the loop scores on, NOT a
known fraud case, and with a real Medicare surface so they're scoreable. Fraud
concentrates in the tails, so a provider sitting at the 50th percentile across
the board is overwhelmingly likely legitimate. These test the precision FLOOR:
the loop should cleanly ABANDON them (no anomaly to build a theory on).

Source: the ranker's scored_full_<year>.parquet (per-NPI features + percentile
ranks + the fraud_case label). Emits CANDIDATES for human review — you still run
a model-recognition pre-flight before adding any to validation/battery/battery.json
(the sandbox can't stop training-data recall; see docs/INVESTIGATOR_SANDBOX_EVAL).

Usage:
    python scripts/calibration/select_tier1_controls.py \
        --scored results/walk_forward_v4_top5_y2024_*/scored_full_2024.parquet \
        --per-specialty 1 --min-payment 75000
"""
from __future__ import annotations

import argparse
import glob
import json
import sys

import duckdb

# The anomaly axes the loop keys on — a Tier-1 control must be near-median on ALL
# of them (per-specialty percentile in [lo, hi]). These are the within-specialty
# (not _global_) percentiles, so "typical" means typical *for the specialty*.
PCT_COLS = [
    "partb_pay_per_bene_pct",
    "partb_services_per_beneficiary_pct",
    "partb_total_medicare_payment_pct",
    "partb_hcpcs_diversity_pct",
    "hcpcs_em_distribution_entropy_pct",
    "hcpcs_off_specialty_dollar_share_pct",
]


def select(scored: str, per_specialty: int, lo: float, hi: float,
           min_payment: float, specialties: list[str] | None) -> list[dict]:
    band = " AND ".join(f"{c} BETWEEN {lo} AND {hi}" for c in PCT_COLS)
    # deviation-from-median score: most-typical first
    dev = " + ".join(f"abs({c} - 0.5)" for c in PCT_COLS)
    spec_filter = ""
    if specialties:
        joined = ",".join("'" + s.replace("'", "''") + "'" for s in specialties)
        spec_filter = f"AND partb_provider_type IN ({joined})"
    sql = f"""
      WITH clean AS (
        SELECT npi,
               partb_rndrng_prvdr_last_org_name AS last_org,
               partb_rndrng_prvdr_first_name    AS first,
               partb_provider_type              AS specialty,
               partb_state                      AS state,
               partb_total_medicare_payment     AS payment,
               ({dev})                          AS median_dev,
               row_number() OVER (PARTITION BY partb_provider_type ORDER BY ({dev})) AS rk
        FROM read_parquet('{scored}')
        WHERE COALESCE(fraud_case, 0) = 0            -- not a known fraud label
          AND partb_total_medicare_payment >= {min_payment}  -- scoreable surface
          AND partb_rndrng_prvdr_last_org_name IS NOT NULL
          AND {band}                                  -- near-median on every axis
          {spec_filter}
      )
      SELECT npi, last_org, first, specialty, state, payment, median_dev
      FROM clean WHERE rk <= {per_specialty}
      ORDER BY specialty, median_dev
    """
    return duckdb.sql(sql).df().to_dict("records")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--scored", required=True, help="scored_full_<year>.parquet (glob ok)")
    ap.add_argument("--per-specialty", type=int, default=1)
    ap.add_argument("--lo", type=float, default=0.40, help="lower percentile band")
    ap.add_argument("--hi", type=float, default=0.60, help="upper percentile band")
    ap.add_argument("--min-payment", type=float, default=75000.0)
    ap.add_argument("--specialty", action="append", help="restrict to specialty (repeatable); default = all")
    ap.add_argument("--as-of", default=None, help="as_of for battery.json entries (default: derive from filename year+1)")
    args = ap.parse_args()

    matches = glob.glob(args.scored)
    if not matches:
        print(f"no scored panel matched {args.scored!r}", file=sys.stderr)
        return 1
    scored = sorted(matches)[-1]

    rows = select(scored, args.per_specialty, args.lo, args.hi,
                  args.min_payment, args.specialty)
    if not rows:
        print("no typical-and-clean providers matched — widen --lo/--hi or lower --min-payment",
              file=sys.stderr)
        return 1

    # as_of: the year AFTER the scored year (point-in-time cutoff before any conduct)
    as_of = args.as_of
    if not as_of:
        import re
        m = re.search(r"(\d{4})", scored)
        as_of = f"{int(m.group(1)) + 1}-01-01" if m else "2025-01-01"

    print(f"# Tier-1 candidate controls from {scored.split('/')[-1]} "
          f"(band [{args.lo},{args.hi}], min ${args.min_payment:,.0f})\n")
    for r in rows:
        name = f"{r['first']} {r['last_org']}".strip().title()
        print(f"  {r['npi']}  {name:32s} {str(r['specialty'])[:24]:24s} "
              f"{r['state']}  ${r['payment']:,.0f}  dev={r['median_dev']:.3f}")
    print("\n# NEXT: model-recognition pre-flight each, then add as negative_control to "
          "validation/battery/battery.json with as_of=" + as_of)
    return 0


if __name__ == "__main__":
    sys.exit(main())
