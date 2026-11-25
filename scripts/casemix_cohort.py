#!/usr/bin/env python3
"""Case-mix-matched peer cohort: forecloses (or confirms) "my patients are sicker."

The single most common case-killer is the panel-acuity explanation, and Integra
kills any comparison that is not conditioned on it. This tool builds the
comparison the defense claims cannot exist: peers of the SAME specialty whose
panels are AT LEAST as young (disabled, under-65) and AT LEAST as dual-eligible
(poor) as the subject's, from the public CMS by-provider PUF. It then ranks the
subject's per-code beneficiary penetration inside that cohort.

Validated 2026-08-02 on CALIBRATION-B3C661FF (Aspen, 99215): among 1,810
Family Practice peers with u65>=50% and dual>=50%, median 99215 penetration was
0.0% and the subject's 81.1% ranked 6th (p99.7). Checkpoint resolved
foreclose_explanation; the case proceeded. Both directions matter: a subject
that regresses into the matched pack is a cheap DECLINE before a full loop run.

Mode guidance (from the 2026-08-02 validation):
  * FORECLOSURE CLAIM: use band floors (--u65-min 0.5 --dual-min 0.5) — large,
    stable cohort (Aspen: n=1,810, rank 6, p99.7, median 0.0%).
  * SENSITIVITY CHECK: default subject-anchored floors (peers at least as
    extreme as the subject). Cohorts get small (n~260) and the extreme tail is
    CONTAMINATED by other anomalous billers (Aspen-anchored: the 4 above
    him bill L5 on 82-96% of equally-sick panels — screen candidates, not
    baselines). A "does NOT foreclose" here does not undo a band-mode
    foreclosure; read the numbers, not just the advisory tag.

Usage:
    python scripts/casemix_cohort.py --npi 1900000007 --year 2013 --code 99215
    python scripts/casemix_cohort.py --npi ... --year 2013 --u65-min 0.5 --dual-min 0.5
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import duckdb

REPO_ROOT = Path(__file__).resolve().parent.parent
PANELS = REPO_ROOT / "data" / "panels"
R2_CACHE = Path(os.path.expanduser("~/.cache/fraud-detection/r2_tables"))


def _panel(kind: str, year: int) -> Path:
    for base in (PANELS, R2_CACHE):
        p = base / f"cms_partb{'_svc' if kind == 'svc' else ''}_{year}.parquet"
        if p.exists():
            return p
    raise FileNotFoundError(f"cms_partb{'_svc' if kind == 'svc' else ''}_{year}.parquet "
                            f"not in {PANELS} or {R2_CACHE}")


def run(npi: str, year: int, code: str, min_benes: int,
        u65_min: float | None, dual_min: float | None) -> dict:
    con = duckdb.connect()
    prov, svc = _panel("prov", year), _panel("svc", year)

    subj = con.execute(f"""
        SELECT Rndrng_Prvdr_Type spec, Tot_Benes benes,
               Bene_Age_LT_65_Cnt*1.0/NULLIF(Tot_Benes,0) u65,
               Bene_Dual_Cnt*1.0/NULLIF(Tot_Benes,0) dual,
               Bene_Avg_Risk_Scre risk
        FROM read_parquet('{prov}') WHERE Rndrng_NPI='{npi}'""").fetchone()
    if subj is None:
        raise SystemExit(f"NPI {npi} not in by-provider PUF {year}")
    spec, s_benes, s_u65, s_dual, s_risk = subj

    # Default thresholds: the subject's OWN shares, so the cohort is "panels at
    # least as young and as poor as the subject's" and the claim is conditioned
    # on the exact innocence story being tested. Overridable for looser bands.
    # FLOOR to 4dp, never round: round() can land above the subject's exact
    # share and exclude the subject from its own cohort (hit on 0.66359->0.6636).
    import math
    t_u65 = u65_min if u65_min is not None else math.floor(float(s_u65 or 0) * 1e4) / 1e4
    t_dual = dual_min if dual_min is not None else math.floor(float(s_dual or 0) * 1e4) / 1e4

    df = con.execute(f"""
        WITH cohort AS (
          SELECT Rndrng_NPI npi, Tot_Benes benes
          FROM read_parquet('{prov}')
          WHERE Rndrng_Prvdr_Type='{spec}' AND Tot_Benes>={min_benes}
            AND Bene_Age_LT_65_Cnt*1.0/NULLIF(Tot_Benes,0) >= {t_u65}
            AND Bene_Dual_Cnt*1.0/NULLIF(Tot_Benes,0) >= {t_dual}),
        cd AS (
          SELECT Rndrng_NPI npi, SUM(Tot_Benes) code_benes, SUM(Tot_Srvcs) code_srvcs
          FROM read_parquet('{svc}') WHERE HCPCS_Cd='{code}' GROUP BY 1)
        SELECT c.npi, c.benes,
               COALESCE(cd.code_benes,0)*1.0/c.benes pen,
               COALESCE(cd.code_srvcs,0)*1.0/NULLIF(cd.code_benes,0) svc_per_bene
        FROM cohort c LEFT JOIN cd ON cd.npi=c.npi""").df()

    if npi not in set(df.npi):
        raise SystemExit(f"subject fell outside its own cohort (u65>={t_u65}, dual>={t_dual}) "
                         f"— subject shares: u65={s_u65:.3f} dual={s_dual:.3f}; pass looser --u65-min/--dual-min")
    df = df.sort_values("pen", ascending=False).reset_index(drop=True)
    rank = int(df.index[df.npi == npi][0]) + 1
    srow = df[df.npi == npi].iloc[0]
    n = len(df)
    return {
        "npi": npi, "year": year, "code": code, "specialty": spec,
        "cohort_definition": {"min_benes": min_benes, "u65_share_min": t_u65,
                              "dual_share_min": t_dual, "anchored_to_subject": u65_min is None},
        "cohort_n": n,
        "subject": {"benes": int(s_benes), "u65_share": round(float(s_u65), 4),
                    "dual_share": round(float(s_dual), 4),
                    "risk_score": float(s_risk) if s_risk is not None else None,
                    "penetration": round(float(srow.pen), 4),
                    "svc_per_bene": round(float(srow.svc_per_bene), 3) if srow.svc_per_bene == srow.svc_per_bene else None},
        "cohort_penetration": {"median": round(float(df.pen.median()), 4),
                               "p90": round(float(df.pen.quantile(.9)), 4),
                               "p99": round(float(df.pen.quantile(.99)), 4),
                               "max": round(float(df.pen.max()), 4)},
        "subject_rank": rank,
        "subject_percentile": round(100 * (1 - rank / n), 2),
        # Foreclosure needs extremity AND separation: tight subject-anchored
        # cohorts can be small (n~260), where pure rank-percentile is granular.
        # Subject at/above the cohort's own p99 penetration, or in the top 1% by
        # rank, is "extreme among equally-sick panels". Advisory only; the
        # numbers above are the evidence.
        "reading": ("FORECLOSES panel-acuity: subject is extreme among equally-sick panels"
                    if (rank <= max(1, n // 100)
                        or float(srow.pen) >= float(df.pen.quantile(.99)))
                    else "does NOT foreclose: subject is inside the matched pack"),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--npi", required=True)
    ap.add_argument("--year", type=int, required=True)
    ap.add_argument("--code", default="99215")
    ap.add_argument("--min-benes", type=int, default=100)
    ap.add_argument("--u65-min", type=float, default=None,
                    help="override cohort under-65-share floor (default: subject's own share)")
    ap.add_argument("--dual-min", type=float, default=None,
                    help="override cohort dual-share floor (default: subject's own share)")
    args = ap.parse_args(argv)
    print(json.dumps(run(args.npi, args.year, args.code, args.min_benes,
                         args.u65_min, args.dual_min), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
