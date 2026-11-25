#!/usr/bin/env python3
"""Innocent-explanation pre-screen — per-se selection gate BEFORE loop spend.

Maintainer framing (verbatim intent): "we can have these high-billing
individuals, but if their high billing CANNOT survive any innocent
explanation, then that is a good case; if there IS a reasonable explanation,
then we fall." Two consecutive fully-anchored loop runs (Eye Clinic Chestnut Care
CASE-6A4588BF-2026, Rowan/Pain Clinic Mango CASE-6A46FB87-2026) each burned
a multi-hour run only to DECLINE at the end because a LIVE innocent
explanation (widespread-tolerated-practice; standard-of-care monitoring)
could only be foreclosed with patient charts we don't have.

This script runs the innocent-explanation test at the FRONT, cheaply, so loop
runs are reserved for patterns where NO benign story survives public data:

  1. EXCLUSION/REVOCATION (per-se, fully reliable) — an excluded/revoked
     provider billing Medicare is false regardless of clinical context.
     Checked via leie_lookup.py (federal OIG-LEIE) + ca_exclusion_lookup.py
     (CA Medi-Cal S&I list, best-effort).
  2. IMPOSSIBLE-DAY (secondary, APPROXIMATE — annual aggregate, not per-day
     claims) — a hand-curated CPT-typical-time map for well-known time-typed
     codes ONLY, cross-multiplied against annual Tot_Srvcs. This is a SCREEN,
     not proof.

Medical-necessity "does an innocent story exist" is NOT auto-classified here
— that is inherently a reasoning/loop/human judgment (Integra-defect class
per the taxonomy doc). This script only answers the narrower, mechanical
question: did a per-se (no-innocent-explanation) fact fire. If not, the
pattern is flagged ambiguous_needs_judgment — likely referral-track, per the
Ridge/Rowan precedent — and left to a human or the full loop.

Usage:
    python scripts/innocent_explanation_prescreen.py --npi 1900000053
    python scripts/innocent_explanation_prescreen.py --npi 1900000053 --year 2024 --format json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent          # scripts/
_ROOT = _HERE.parent                             # repo root
for _p in (str(_HERE), str(_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import anchor_check                               # noqa: E402  (same dir)
import ca_exclusion_lookup                         # noqa: E402
import leie_lookup                                 # noqa: E402
import ring_enrichment                             # noqa: E402
from fraudlib import r2                            # noqa: E402

# ── time-typed code -> CPT typical minutes (hand-curated, well-known codes ──
# ONLY). Authority = AMA CPT typical/total time, NOT the CMS PUF HCPCS_Desc
# field, which has been independently confirmed WRONG for at least 4 codes
# (99489 flipped 30->60 min for 2021-2024 — see hcpcs_authoritative_overrides
# .json + docs/CANONICAL_LOOP_GUARDRAILS.md). This map is deliberately small
# and separate from hcpcs_lookup.canonical_minutes_per_unit (which resolves
# CCM/health-behavior add-on codes via RVU/PUF-by-year) because none of the
# codes below are in that override table; keeping this pre-screen
# self-contained avoids a second data dependency for a tool meant to be cheap.
#   - 90832/90834/90837: psychotherapy, 30/45/60 min (AMA CPT time ranges;
#     90832 16-37min typically billed as "30", 90834 38-52min as "45", 90837
#     53+min as "60").
#   - 99213/99214/99215: established-patient office E&M, 2021+ CPT total-time
#     ranges (20-29 / 30-39 / 40-54 min) — using the range FLOOR (20/30/40) is
#     the conservative choice for an impossibility screen (understates implied
#     hours rather than overstates them).
#   - 90791/90792: psychiatric diagnostic evaluation, 60 min (AMA CPT — no
#     explicit time range published, but 60 min is the universally-cited
#     typical duration for a full diagnostic interview).
_TIME_TYPED_MINUTES: dict[str, int] = {
    "90832": 30, "90834": 45, "90837": 60,
    "99213": 20, "99214": 30, "99215": 40,
    "90791": 60, "90792": 60,
}

# Conservative working-day assumptions for the impossible-day screen. This is
# an ANNUAL-AGGREGATE approximation (Tot_Srvcs / working_days), not a
# per-claim-date reconstruction — it cannot prove a single day was
# impossible, only that the annual volume, spread evenly, implies more
# clinical minutes per working day than physically exist. Flag threshold is
# deliberately loose (16 of 24h) so it only fires on genuine extremity.
_WORKING_DAYS_PER_YEAR = 240
_IMPOSSIBLE_DAY_HOURS_THRESHOLD = 16.0


# ── 1. exclusion / revocation (per-se) ──────────────────────────────────────
def _leie_check(npis: list[str]) -> dict:
    """Federal OIG-LEIE exclusion check for every NPI. Degrades gracefully —
    a lookup failure for one NPI does not crash the whole screen."""
    hits: list[dict] = []
    errors: list[str] = []
    for npi in npis:
        try:
            r = leie_lookup.lookup(npi)
        except Exception as e:  # R2/cache unavailable, malformed row, etc.
            errors.append(f"LEIE lookup failed for {npi}: {e}")
            continue
        if r.get("excluded"):
            hits.append({"npi": npi, "source": "LEIE", "excldate": r.get("excldate"),
                        "excltype": r.get("excltype"), "reinstated": r.get("reinstated")})
    out: dict = {"hits": hits}
    if errors:
        out["_error"] = "; ".join(errors)
    return out


def _ca_si_check(npis: list[str]) -> dict:
    """California Medi-Cal Suspended & Ineligible list — best-effort (state-
    scoped, R2-backed; only ~13% of S&I rows carry an extractable NPI, so a
    miss here is NOT evidence of no exclusion). Degrades gracefully."""
    try:
        con = r2.duckdb_r2()
    except Exception as e:
        return {"hits": [], "_error": f"CA S&I lookup unavailable: {e}"}
    hits: list[dict] = []
    errors: list[str] = []
    for npi in npis:
        try:
            rows = ca_exclusion_lookup._check_si_by_npi(con, npi)
        except Exception as e:
            errors.append(f"CA S&I lookup failed for {npi}: {e}")
            continue
        for row in rows:
            hits.append({"npi": npi, "source": "CA S&I",
                        "date_of_suspension": row.get("date_of_suspension"),
                        "active_period": row.get("active_period"),
                        "provider_type": row.get("provider_type")})
    out: dict = {"hits": hits}
    if errors:
        out["_error"] = "; ".join(errors)
    return out


def check_exclusions(npis: list[str]) -> dict:
    """Combine LEIE + CA S&I into one per-se signal. An excluded/revoked
    provider billing Medicare is false regardless of clinical context —
    no innocent explanation survives this fact."""
    leie = _leie_check(npis)
    ca = _ca_si_check(npis)
    all_hits = leie["hits"] + ca["hits"]
    errors = [e for e in (leie.get("_error"), ca.get("_error")) if e]
    signal = None
    if all_hits:
        by_npi: dict[str, list[dict]] = {}
        for h in all_hits:
            by_npi.setdefault(h["npi"], []).append(h)
        detail_parts = []
        for npi, hs in by_npi.items():
            for h in hs:
                window = ""
                if h.get("excldate"):
                    window = f" (excluded {h['excldate']}"
                    window += f", reinstated {h['reinstated']})" if h.get("reinstated") else ", not reinstated)"
                elif h.get("date_of_suspension"):
                    window = f" (suspended {h['date_of_suspension']}, active_period {h.get('active_period')})"
                detail_parts.append(f"NPI {npi} — {h['source']}{window}")
        signal = {
            "type": "excluded_or_revoked_provider_billing",
            "npis": sorted(by_npi),
            "detail": "; ".join(detail_parts),
        }
    return {"signal": signal, "raw_hits": all_hits, "notes": errors}


# ── 2. impossible-day (approximate, annual-aggregate) ───────────────────────
def _dominant_time_typed_codes(npi: str, year: int) -> dict:
    """Pull this NPI's Tot_Srvcs for any code in _TIME_TYPED_MINUTES from the
    CMS Part-B service-level PUF for `year`. Returns {rows: [...], _error}."""
    basename = f"cms_partb_svc_{year}.parquet"
    src = r2.parquet_source(_ROOT / "data/panels" / basename,
                            f"{r2.R2_PANELS}/{basename}")
    codes = ",".join(f"'{c}'" for c in _TIME_TYPED_MINUTES)
    try:
        con = r2.duckdb_r2()
        df = con.execute(
            f"SELECT HCPCS_Cd, Tot_Srvcs FROM read_parquet('{src}') "
            f"WHERE CAST(Rndrng_NPI AS VARCHAR) = '{npi}' AND HCPCS_Cd IN ({codes})"
        ).df()
    except Exception as e:
        return {"rows": [], "_error": f"impossible-day PUF query failed for {npi}/{year}: {e}"}
    rows = [{"code": str(r["HCPCS_Cd"]), "tot_srvcs": float(r["Tot_Srvcs"])}
            for _, r in df.iterrows()]
    return {"rows": rows}


def _implied_hours_per_day(code: str, tot_srvcs: float) -> float:
    minutes = _TIME_TYPED_MINUTES[code]
    return (minutes * tot_srvcs / _WORKING_DAYS_PER_YEAR) / 60.0


def check_impossible_day(npi: str, year: int) -> dict:
    """APPROXIMATE screen — annual Tot_Srvcs spread evenly over
    _WORKING_DAYS_PER_YEAR working days, using CPT-typical minutes for
    well-known time-typed codes. Flags only when a single code (or the sum
    across codes) implies more than _IMPOSSIBLE_DAY_HOURS_THRESHOLD hours of
    clinical time per working day. This is a screen, not proof: it says
    nothing about any specific claim date, only that the annual volume is
    incompatible with a working day's real duration if billed as described."""
    pulled = _dominant_time_typed_codes(npi, year)
    if pulled.get("_error"):
        return {"signal": None, "per_code": [], "notes": [pulled["_error"]]}
    per_code = []
    total_hours = 0.0
    for row in pulled["rows"]:
        code, tot = row["code"], row["tot_srvcs"]
        hours = _implied_hours_per_day(code, tot)
        per_code.append({"code": code, "tot_srvcs": tot,
                         "minutes_per_unit": _TIME_TYPED_MINUTES[code],
                         "implied_hours_per_day": round(hours, 2)})
        total_hours += hours
    signal = None
    worst = max(per_code, key=lambda r: r["implied_hours_per_day"], default=None)
    if worst and worst["implied_hours_per_day"] > _IMPOSSIBLE_DAY_HOURS_THRESHOLD:
        signal = {"type": "impossible_day", "npis": [npi],
                  "detail": (f"APPROXIMATE (annual aggregate, not per-day claims): "
                             f"code {worst['code']} implies {worst['implied_hours_per_day']:.1f} "
                             f"clinical hours/working-day at {worst['tot_srvcs']:.0f} svcs/yr "
                             f"over {_WORKING_DAYS_PER_YEAR} working days "
                             f"({_TIME_TYPED_MINUTES[worst['code']]} min/unit).")}
    elif total_hours > _IMPOSSIBLE_DAY_HOURS_THRESHOLD:
        signal = {"type": "impossible_day", "npis": [npi],
                  "detail": (f"APPROXIMATE (annual aggregate, not per-day claims): "
                             f"sum across time-typed codes implies {total_hours:.1f} "
                             f"clinical hours/working-day over {_WORKING_DAYS_PER_YEAR} working days.")}
    return {"signal": signal, "per_code": per_code, "notes": []}


# ── anchor context (reported, not adjudicated) ──────────────────────────────
def anchor_context(dominant_code: str | None) -> dict:
    """Report whether a coverage rule exists for the dominant code, as
    context. Does NOT adjudicate medical necessity — that's chart-dependent,
    inherently a reasoning/loop/human judgment (see module docstring)."""
    if not dominant_code:
        return {"code": None, "lcd_exists": None, "ncd_exists": None,
                "notes": ["no dominant code identified"]}
    try:
        res = anchor_check.check_anchor(dominant_code)
    except Exception as e:
        return {"code": dominant_code, "lcd_exists": None, "ncd_exists": None,
                "_error": f"anchor_check failed for {dominant_code}: {e}"}
    return {"code": dominant_code, "lcd_exists": res["lcd_exists"],
            "ncd_exists": res["ncd_exists"], "article_only": res["article_only"]}


# ── orchestration ────────────────────────────────────────────────────────────
def screen(npi: str, year: int = 2024) -> dict:
    npi = str(npi).strip()
    ring = ring_enrichment.enrich_ring(npi, year)
    ring_npis = [m["npi"] for m in ring["ring_members"]]
    if npi not in ring_npis:
        ring_npis = [npi] + ring_npis

    exclusions = check_exclusions(ring_npis)
    impossible = check_impossible_day(npi, year)
    # Ring members also get the impossible-day screen — an innocent-explanation
    # foreclosure fact for any member is a foreclosure fact for the ring.
    for member_npi in ring_npis:
        if member_npi == npi:
            continue
        member_result = check_impossible_day(member_npi, year)
        if member_result.get("signal"):
            impossible["signal"] = member_result["signal"]
            break

    per_se_signals = [s for s in (exclusions.get("signal"), impossible.get("signal")) if s]

    seed = ring.get("seed") or {}
    dominant_code = None
    if impossible["per_code"]:
        dominant_code = max(impossible["per_code"], key=lambda r: r["tot_srvcs"])["code"]
    anchor = anchor_context(dominant_code)

    notes = list(exclusions.get("notes") or []) + list(impossible.get("notes") or [])
    if per_se_signals:
        track = "file_track"
    else:
        track = "ambiguous_needs_judgment"
        notes.append("no per-se (no-innocent-explanation) fact fired — this pattern is a "
                     "coverable-service utilization outlier whose innocent explanation is "
                     "chart-dependent (likely referral-track, per the Ridge/Rowan precedent). "
                     "Leave the medical-necessity judgment to a human or the full loop.")

    return {
        "npi": npi,
        "year": year,
        "entity": (ring.get("collectible_entity") or {}).get("name"),
        "ring_member_npis": ring_npis,
        "per_se_signals": per_se_signals,
        "anchor_context": anchor,
        "track": track,
        "notes": notes,
    }


def _fmt(res: dict) -> str:
    L = [f"═══ INNOCENT-EXPLANATION PRE-SCREEN — NPI {res['npi']} ({res['year']}) ═══"]
    if res.get("entity"):
        L.append(f"  entity: {res['entity']}")
    L.append(f"  ring members screened: {len(res['ring_member_npis'])}")
    L.append("\n— PER-SE (no-innocent-explanation) SIGNALS —")
    if not res["per_se_signals"]:
        L.append("  (none fired)")
    for s in res["per_se_signals"]:
        L.append(f"  [{s['type']}] NPIs {s['npis']}")
        L.append(f"    {s['detail']}")
    ac = res["anchor_context"]
    L.append(f"\n— ANCHOR CONTEXT (reported, not adjudicated) —")
    L.append(f"  dominant code: {ac.get('code')}  lcd_exists={ac.get('lcd_exists')} "
             f"ncd_exists={ac.get('ncd_exists')}")
    if ac.get("_error"):
        L.append(f"  {ac['_error']}")
    L.append(f"\n— TRACK: {res['track']} —")
    for n in res["notes"]:
        L.append(f"  note: {n}")
    return "\n".join(L)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Innocent-explanation pre-screen: per-se selection gate before loop spend")
    ap.add_argument("--npi", required=True, help="10-digit rendering-provider NPI")
    ap.add_argument("--year", type=int, default=2024, help="billing year to screen (default 2024)")
    ap.add_argument("--format", choices=["text", "json"], default="text")
    args = ap.parse_args(argv)
    if not (len(args.npi.strip()) == 10 and args.npi.strip().isdigit()):
        print(f"ERROR: --npi must be a 10-digit NPI; got {args.npi!r}", file=sys.stderr)
        return 2
    res = screen(args.npi, args.year)
    print(json.dumps(res, indent=2, default=str) if args.format == "json" else _fmt(res))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
