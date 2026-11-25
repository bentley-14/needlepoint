#!/usr/bin/env python3
"""Guardrail 2 — deterministic numeric-predicate audit.

Re-derives every headline quantitative claim a case makes from its PRIMARY
inputs + CANONICAL constants, and flags any discrepancy. This is the
deterministic check that would have caught the Birch 99489 error: the CMS PUF
descriptor wrongly said "60 minutes" (AMA-CPT is 30), which ~2x-ed an
impossibility-hours figure in attorney-facing output. A real defendant's expert
re-derives the number and catches a 2x error instantly; the simulated `defense`
stage must too. The orchestrator runs this BEFORE defense and injects any
MISMATCH as a dispositive integrity defect the defense must raise.

A numeric predicate is a structured statement of a claim, e.g.:

  {"id": "c1_hours", "kind": "time_impossibility",
   "codes": [{"code": "99489", "units": 118800, "year": 2023}],
   "claimed": {"total_hours": 118800}}          # <- the WRONG (60-min) figure

The auditor resolves minutes-per-unit through hcpcs_lookup.canonical_minutes_per_unit
(the single source of truth — authoritative override > CMS MPFS RVU > PUF),
recomputes 118800 units x 30 min / 60 = 59,400 h, and reports MISMATCH because
the claim says 118,800 h. Pure arithmetic + canonical lookups — NO LLM.

Predicate kinds:
  time_impossibility : sum(units x canonical_minutes_per_unit)/60  -> hours
  fte_equivalent     : total_hours / productive_hours_per_fte      -> FTE
  ratio / market_share : numerator / denominator (market_share is an alias)
  rank               : auto-detected within ratio/market_share when the
                       predicate is rank-shaped (a *_rank/*_percentile claimed
                       key, or top-level rank/of) -> recomputed as a
                       PERCENTILE = (cohort - rank + 1) / cohort * 100, NOT a
                       bare rank/cohort quotient (see _is_rank_predicate /
                       _rank_percentile — fixes the false MISMATCH a rank
                       claim like "#5 of 394" produced when compared literally
                       as numerator=5/denominator=394 against the claimed
                       rank number 5; failure_mode_taxonomy_2026-07.md #3)
  multiple           : value / baseline
  damages            : sum(units x unit_amount_usd)                -> $
  product            : product(factors)
  sum                : sum(addends)

Each finding: {id, kind, status (MATCH|MISMATCH|UNVERIFIABLE|
UNVERIFIABLE_UNIT_SEMANTICS), claimed, recomputed, rel_error, constants_used,
detail}. UNVERIFIABLE_UNIT_SEMANTICS (additive, 2026-07) = a per-claim
penalty-exposure predicate whose claims-count N carries a units≠claims signal
(sub-$5 avg per "claim" / J-code / drug-classified family) — a review signal,
never a dispositive defect. CLI exits 2 if any MISMATCH.

Usage:
  python scripts/audit_case_numerics.py --predicates preds.json
  cat preds.json | python scripts/audit_case_numerics.py --format json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# Repo root on path so hcpcs_lookup can load the real fraudlib.r2 (R2 fallback);
# falls back to local-only parquets if fraudlib is unavailable (calibration jail).
_REPO = Path(__file__).resolve().parents[1]
for _p in (str(_REPO), str(_REPO / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from hcpcs_lookup import canonical_minutes_per_unit  # noqa: E402

DEFAULT_REL_TOL = 0.02  # 2% — looser than float noise, tighter than a real error

MATCH = "MATCH"
MISMATCH = "MISMATCH"
UNVERIFIABLE = "UNVERIFIABLE"
# Additive status (2026-07): a penalty-exposure predicate whose claims-count N
# carries a units≠claims signal (sub-$5 average payment per "claim" and/or a
# per-unit-dosed J-code / drug-classified family). The arithmetic may be
# internally consistent, but N's UNIT SEMANTICS are unverifiable from public
# aggregate data — and per-claim penalty math on a units count inflates
# exposure ~100x (the Alder CALIBRATION-6DA0BBC3 10,724 episode). Deliberately
# NOT a MISMATCH: nothing is provably wrong, so it must never be injected as a
# dispositive defect (a false dispositive would tank a real case) — it is a
# resolve-before-pleading review signal, mirroring
# fraud_investigator.tier_lint.lint_unit_claim_semantics.
UNVERIFIABLE_UNIT_SEMANTICS = "UNVERIFIABLE_UNIT_SEMANTICS"


_CLAIMED_SKIP_KEYS = {"method", "classification", "year", "note", "notes", "source"}

# Keys that mark a `claimed` block as a RANK/PERCENTILE claim, not a plain ratio —
# single-sourced with _is_rank_predicate / _recompute's "rank" branch below so a
# predicate emitted as kind="ratio" with these keys (the real counsel_build shape,
# e.g. {"q4xx_rank": 5, "q4xx_cohort": 394, "q4xx_percentile": 98.7}) is recognized
# as rank-shaped instead of literally dividing rank/cohort and comparing the
# quotient against the raw rank number (the CASE-6A305421 false-MISMATCH bug:
# claimed=5 (the rank) vs recomputed=5/394=0.0127 -> 99.7% "error").
_RANK_KEY_RE = re.compile(r"(?:^|_)(rank)(?:_|$)", re.IGNORECASE)
_PCTILE_KEY_RE = re.compile(r"(?:^|_)(percentile|pctile|pct_ile)(?:_|$)", re.IGNORECASE)
_COHORT_KEY_RE = re.compile(r"(?:^|_)(cohort|of|n)(?:_|$)", re.IGNORECASE)


def _is_rank_predicate(pred: dict) -> bool:
    """True if a `ratio`/`market_share`-kind predicate is actually a rank claim
    (has a *_rank claimed key or a top-level rank field) — REQUIRES an explicit
    rank signal, not merely a key containing "percentile": a plain ratio can
    legitimately be claimed under a key literally named "percentile" (e.g. a
    fraction expressed as a 0-1 percentile-like value with no rank/cohort
    anywhere), and treating that alone as rank-shaped would wrongly route a
    genuine ratio into the rank branch, which requires rank+cohort and would
    report UNVERIFIABLE for a perfectly re-derivable ratio."""
    claimed_keys = [str(k) for k in (pred.get("claimed") or {})]
    if any(_RANK_KEY_RE.search(k) for k in claimed_keys):
        return True
    return "rank" in pred


def _rank_percentile(pred: dict) -> tuple[float | None, str, list[dict], str]:
    """Recompute a rank claim as a PERCENTILE (not a bare rank/cohort ratio):
    percentile = (cohort - rank + 1) / cohort * 100 — #1 of N is the (N-1/N)th+
    percentile, matching the conventional "Nth percentile" claim in these
    predicates. Requires an explicit rank + cohort size; never guesses one."""
    constants: list[dict] = []
    rank = pred.get("rank")
    cohort = pred.get("of") or pred.get("cohort") or pred.get("denominator")
    if rank is None or cohort is None:
        c = (pred.get("codes") or [{}])[0]
        rank = rank if rank is not None else c.get("rank")
        cohort = cohort if cohort is not None else (c.get("of") or c.get("cohort"))
    if rank is None or cohort is None:
        # Fall back to whichever *_rank / *_cohort pair is present in `claimed`.
        claimed = pred.get("claimed") or {}
        rank_key = next((k for k in claimed if _RANK_KEY_RE.search(str(k))), None)
        cohort_key = next((k for k in claimed if _COHORT_KEY_RE.search(str(k))), None)
        if rank_key is not None and cohort_key is not None:
            rank, cohort = claimed[rank_key], claimed[cohort_key]
    if rank is None or cohort is None:
        return None, "percentile", constants, (
            "rank predicate missing an explicit rank + cohort size — cannot re-derive")
    rank, cohort = float(rank), float(cohort)
    if cohort <= 0 or rank <= 0:
        return None, "percentile", constants, "rank/cohort must be positive"
    pctile = (cohort - rank + 1.0) / cohort * 100.0
    return pctile, "percentile", constants, f"(#{rank:g} of {cohort:g}) -> {pctile:.1f}th percentile"


def _claimed_value(claimed: dict, prefer_key_re: "re.Pattern | None" = None
                    ) -> tuple[str, float] | tuple[None, None]:
    """Pull the single claimed scalar out of a predicate's `claimed` block.
    Accepts a few conventional keys so prompts don't have to memorize one name.
    ``prefer_key_re`` (e.g. a percentile-key pattern) is checked FIRST so a
    rank-shaped predicate compares its percentile claim, not an unrelated
    scalar that happens to appear first in the dict (the CASE-6A305421 bug)."""
    if prefer_key_re is not None:
        for k, v in claimed.items():
            if prefer_key_re.search(str(k)) and isinstance(v, (int, float)) and not isinstance(v, bool):
                return k, float(v)
    for key in ("value", "total_hours", "total_minutes", "fte", "ratio",
                "multiple", "damages_usd", "amount_usd", "percentile"):
        if key in claimed and claimed[key] is not None:
            return key, float(claimed[key])
    # Fallback: the first numeric scalar in the block. The counsel_build prompt
    # emits free-form claimed keys (q4247_pct, tests_per_bene, sqcm_per_application,
    # …), not the conventional names above — without this every such predicate
    # came back UNVERIFIABLE with claimed=None.
    for k, v in claimed.items():
        if k in _CLAIMED_SKIP_KEYS:
            continue
        if isinstance(v, bool):
            continue
        if isinstance(v, (int, float)):
            return k, float(v)
        if isinstance(v, str):
            try:
                return k, float(v.replace(",", "").replace("$", "").rstrip("%").strip())
            except ValueError:
                continue
    return None, None


def _recompute(pred: dict) -> tuple[float | None, str, list[dict], str]:
    """Return (recomputed_value, claimed_key_hint, constants_used, detail).
    recomputed_value is None when a required canonical constant is missing."""
    kind = pred.get("kind")
    constants: list[dict] = []

    if kind in ("time_impossibility", "fte_equivalent") and pred.get("codes"):
        total_min = 0.0
        for c in pred["codes"]:
            code = c["code"]
            units = float(c["units"])
            res = canonical_minutes_per_unit(code, c.get("year"))
            if res is None:
                return None, "total_hours", constants, (
                    f"no canonical minutes-per-unit for {code} "
                    f"(year={c.get('year')}) — cannot re-derive")
            constants.append({"code": code, "year": c.get("year"),
                              "minutes_per_unit": res["minutes_per_unit"],
                              "source": res["source"], "units": units})
            total_min += units * res["minutes_per_unit"]
        hours = total_min / 60.0
        if kind == "time_impossibility":
            return hours, "total_hours", constants, f"{total_min:,.0f} staff-minutes / 60"
        # fte_equivalent derived from codes -> needs productive hours
        prod = float(pred.get("productive_hours_per_fte", 0) or 0)
        if prod <= 0:
            return None, "fte", constants, "missing productive_hours_per_fte"
        return hours / prod, "fte", constants, f"{hours:,.0f} h / {prod:,.0f} h-per-FTE"

    if kind == "fte_equivalent":
        hours = float(pred["total_hours"])
        prod = float(pred["productive_hours_per_fte"])
        return hours / prod, "fte", constants, f"{hours:,.0f} h / {prod:,.0f} h-per-FTE"

    if kind in ("ratio", "market_share"):
        # market_share is a semantic ALIAS of ratio (the exact counsel_build kind
        # named in failure_mode_taxonomy_2026-07.md #3) — same recompute path.
        #
        # RANK/PERCENTILE detour: a predicate emitted as kind="ratio" with
        # numerator=rank, denominator=cohort-size (e.g. {"numerator": 5,
        # "denominator": 394, "claimed": {"q4xx_rank": 5, "q4xx_percentile": 98.7}})
        # is NOT a plain ratio — dividing rank/cohort and comparing the quotient
        # against the raw rank number produced a false MISMATCH in production
        # (CASE-6A305421: claimed=5, recomputed=5/394=0.0127, "99.7% error", which
        # spawned 4 wasted adversarial attacks, §4d). Recompute as a percentile
        # instead when the predicate is rank-shaped.
        if _is_rank_predicate(pred):
            return _rank_percentile(pred)

        # Original schema: explicit numerator/denominator. Current counsel_build
        # schema instead carries the raw inputs in codes[] (units + a denominator
        # field) and the computed value in claimed{}.
        #
        # SAFETY: never INVENT a denominator and never GUESS the percent scale — a
        # wrong recompute produces a FALSE MISMATCH, which the loop injects into the
        # defense as a dispositive defect and would wrongly tank a real case. So we
        # only re-derive when a numerator AND an unambiguous denominator are present;
        # otherwise return UNVERIFIABLE (the honest "can't check this one").
        if pred.get("numerator") is not None and pred.get("denominator") is not None:
            num, den = float(pred["numerator"]), float(pred["denominator"])
        else:
            c = (pred.get("codes") or [{}])[0]
            num = next((c[k] for k in ("units", "numerator", "count", "value")
                       if c.get(k) is not None), None)
            # Concrete ratio bases only; NOT generic "total"/"base" (those pair
            # with unrelated numerators → false ratios). ring_size covers the
            # ring-enrichment "N of M members mismatched" shape.
            den = next((c[k] for k in ("national_services", "beneficiaries",
                                       "bene_day_pairs", "peer_services",
                                       "ring_size", "denominator")
                        if c.get(k) is not None), None)
            if num is None or den is None:
                return None, "ratio", constants, "ratio inputs not re-derivable from predicate (no explicit numerator/denominator)"
            num, den = float(num), float(den)
        if den == 0:
            return None, "ratio", constants, "denominator is zero"
        # Percent scale ONLY when a claimed key is unambiguously a percentage
        # (q4247_pct, oic_share, …) — and explicitly NOT 'percentile'.
        import re as _re
        keys = [str(k).lower() for k in (pred.get("claimed") or {})]
        is_pct = any(_re.search(r"(_pct\b|_share\b|\bpercent\b)", k) and "percentile" not in k
                     for k in keys)
        scale = 100.0 if is_pct else 1.0
        suffix = " × 100" if scale == 100.0 else ""
        return num / den * scale, "ratio", constants, f"{num:,g} / {den:,g}{suffix}"

    if kind == "multiple":
        val, base = float(pred["value"]), float(pred["baseline"])
        if base == 0:
            return None, "multiple", constants, "baseline is zero"
        return val / base, "multiple", constants, f"{val:,g} / {base:,g}"

    if kind == "damages":
        # Two shapes: explicit line_items[{units, unit_amount_usd}] (original), or
        # the counsel_build components[{est_usd|amount_usd|usd, …}] roll-up.
        items = pred.get("line_items") or pred.get("components")
        if items:
            total = 0.0
            for c in items:
                if c.get("units") is not None and c.get("unit_amount_usd") is not None:
                    total += float(c["units"]) * float(c["unit_amount_usd"])
                else:
                    amt = next((c[k] for k in ("est_usd", "amount_usd", "usd", "value")
                                if c.get(k) is not None), None)
                    if amt is None:
                        return None, "damages_usd", constants, "damages component missing amount"
                    total += float(amt)
            return total, "damages_usd", constants, f"sum of {len(items)} components = {total:,.2f}"
        if pred.get("units") is not None and pred.get("unit_amount_usd") is not None:
            return (float(pred["units"]) * float(pred["unit_amount_usd"]),
                    "damages_usd", constants, "units x unit_amount")
        return None, "damages_usd", constants, "damages missing line_items/components"

    if kind == "product":
        out = 1.0
        for f in pred["factors"]:
            out *= float(f)
        return out, "value", constants, f"product of {len(pred['factors'])} factors"

    if kind == "sum":
        # Bare numbers (original schema) or {label, amount} dicts (the real
        # counsel_build shape — components with a human-readable label attached).
        addends = pred["addends"]
        vals = [a["amount"] if isinstance(a, dict) else a for a in addends]
        out = sum(float(v) for v in vals)
        return out, "value", constants, f"sum of {len(addends)} addends"

    return None, "value", constants, f"unknown predicate kind: {kind!r}"


# ── Penalty-exposure unit-semantics check (units≠claims; additive) ──────────
# Regexes intentionally parallel fraud_investigator/tier_lint.py's
# lint_unit_claim_semantics (this script stays import-standalone for the
# calibration jail, so they are duplicated by design; the flag TEXT is pinned
# equal by tests/test_audit_case_numerics.py).

_UNIT_AVG_THRESHOLD_USD = 5.0
_CLAIMS_COUNT_TEXT_RE = re.compile(
    r"\b(?P<n>\d{1,3}(?:,\d{3})+|\d{2,})\s+(?:[\w-]+\s+){0,3}?claims?\b",
    re.IGNORECASE,
)
_AVG_PER_CLAIM_TEXT_RE = re.compile(
    r"(?:averag\w+[^.;$]{0,40}?)?"
    r"\$(?P<a>[\d.,]+)"
    r"(?:\s*[-–—]\s*\$?(?P<b>[\d.,]+))?"
    r"\s*(?:per\s+claim|/\s*claim)",
    re.IGNORECASE,
)
_J_CODE_TEXT_RE = re.compile(r"\bJ\d{4}\b")
# Suppressed-code drug families never name their J-codes — the counsel_build
# statement says "drug-classified" (the CMS PUF drug_indicator vocabulary)
# instead. Treated as the same per-unit-dosed family signal.
_DRUG_CLASSIFIED_RE = re.compile(r"\bdrug[- ]classified\b", re.IGNORECASE)


def _unit_flag_text(n) -> str:
    """EXACTLY the tier_lint.unit_claim_flag_text wording (pinned by test)."""
    return (
        f"possible units≠claims (per-unit J-code billing): {n} may be units, "
        f"not claims — resolve unit semantics before damages/penalty math "
        f"(cf. 99489-minutes + svcs/bene episodes)"
    )


def _penalty_unit_semantics(pred: dict) -> "dict | None":
    """UNVERIFIABLE_UNIT_SEMANTICS finding for a per-claim penalty-exposure
    predicate whose claims-count N fails the unit-semantics test, else None.

    Fires only when ALL THREE hold — conservative on purpose:
      1. the predicate is penalty-exposure-shaped ("penalt" in kind or in a
         claimed key),
      2. a claims-count N is present (a claimed key naming claims, or an
         "N ... claims" statement),
      3. a units≠claims signal is present IN THE PREDICATE: an explicit
         average payment per claim under $5 (statement text), a J-code
         (codes[] or statement), or a "drug-classified" family marker.
    A 99214-style E&M count at a normal $ average carries none of signal 3
    and falls through to the normal audit path unchanged.
    """
    kind = str(pred.get("kind") or "")
    claimed = pred.get("claimed") or {}
    statement = str(pred.get("statement") or "")
    is_penalty = "penalt" in kind.lower() or any(
        "penalt" in str(k).lower() for k in claimed)
    if not is_penalty:
        return None

    # N: a claimed key naming claims (never a *penalty*_per_claim rate key),
    # else the statement's "N ... claims" assertion.
    n_val = None
    for k, v in claimed.items():
        ks = str(k).lower()
        if "claim" in ks and "penalt" not in ks and isinstance(v, (int, float)) \
                and not isinstance(v, bool):
            n_val = float(v)
            break
    if n_val is None:
        m = _CLAIMS_COUNT_TEXT_RE.search(statement)
        if m:
            try:
                n_val = float(m.group("n").replace(",", ""))
            except ValueError:
                n_val = None
    if n_val is None:
        return None

    signals: list[str] = []
    avg_m = _AVG_PER_CLAIM_TEXT_RE.search(statement)
    if avg_m:
        vals = []
        for g in ("a", "b"):
            raw = avg_m.group(g)
            if raw:
                try:
                    vals.append(float(raw.replace(",", "")))
                except ValueError:
                    pass
        if vals and max(vals) < _UNIT_AVG_THRESHOLD_USD:
            signals.append(f"avg payment per claim ${max(vals):,.2f} < $5")
    j_codes = [str(c.get("code")) for c in (pred.get("codes") or [])
               if _J_CODE_TEXT_RE.fullmatch(str(c.get("code") or ""))]
    j_text = _J_CODE_TEXT_RE.search(statement)
    if j_codes or j_text:
        signals.append(f"per-unit J-code family ({(j_codes or [j_text.group(0)])[0]})")
    if _DRUG_CLASSIFIED_RE.search(statement):
        signals.append("drug-classified (per-unit dosed) family")
    if not signals:
        return None

    n_disp = f"{n_val:,.0f}" if float(n_val).is_integer() else f"{n_val:,}"
    return {
        "id": pred.get("id", "?"), "kind": pred.get("kind", "?"),
        "status": UNVERIFIABLE_UNIT_SEMANTICS,
        "claimed": n_val, "recomputed": None, "rel_error": None,
        "constants_used": [],
        "detail": _unit_flag_text(n_disp) + " [signals: " + "; ".join(signals) + "]",
    }


def audit_predicate(pred: dict, rel_tol: float = DEFAULT_REL_TOL) -> dict:
    # Penalty-exposure predicates get the units≠claims semantics check FIRST —
    # additive status, never MISMATCH (see UNVERIFIABLE_UNIT_SEMANTICS above).
    unit_finding = _penalty_unit_semantics(pred)
    if unit_finding is not None:
        return unit_finding
    pid = pred.get("id", "?")
    kind = pred.get("kind", "?")
    claimed = pred.get("claimed", {}) or {}
    # Rank/percentile predicates must compare against the *_percentile claim,
    # not whichever scalar happens to appear first in `claimed` (the
    # CASE-6A305421 false-MISMATCH bug — see _recompute's ratio/market_share branch).
    prefer = _PCTILE_KEY_RE if (kind in ("ratio", "market_share") and _is_rank_predicate(pred)) else None
    ckey, claimed_val = _claimed_value(claimed, prefer)

    try:
        recomputed, hint, constants, detail = _recompute(pred)
    except (KeyError, TypeError, ValueError) as e:
        return {"id": pid, "kind": kind, "status": UNVERIFIABLE,
                "claimed": claimed_val, "recomputed": None, "rel_error": None,
                "constants_used": [], "detail": f"malformed predicate: {e}"}

    finding = {"id": pid, "kind": kind, "claimed": claimed_val,
               "recomputed": recomputed, "constants_used": constants, "detail": detail}

    if recomputed is None:
        finding.update(status=UNVERIFIABLE, rel_error=None)
        return finding
    if claimed_val is None:
        finding.update(status=UNVERIFIABLE, rel_error=None,
                       detail=f"{detail}; no claimed value to compare against")
        return finding

    denom = max(abs(recomputed), abs(claimed_val), 1e-9)
    rel_err = abs(recomputed - claimed_val) / denom
    finding["rel_error"] = round(rel_err, 6)
    finding["status"] = MATCH if rel_err <= rel_tol else MISMATCH
    return finding


def audit_predicates(predicates: list[dict], rel_tol: float = DEFAULT_REL_TOL) -> list[dict]:
    return [audit_predicate(p, rel_tol) for p in predicates]


def _fmt(findings: list[dict]) -> str:
    icon = {MATCH: "✓", MISMATCH: "🛑", UNVERIFIABLE: "⚠",
            UNVERIFIABLE_UNIT_SEMANTICS: "⚠"}
    out = []
    for f in findings:
        c = (f"claimed={f['claimed']:,.4g}" if f["claimed"] is not None else "claimed=?")
        r = (f"recomputed={f['recomputed']:,.4g}" if f["recomputed"] is not None else "recomputed=?")
        re_ = f" (rel_err={f['rel_error']:.1%})" if f.get("rel_error") is not None else ""
        out.append(f"{icon.get(f['status'], '?')} [{f['status']}] {f['id']} ({f['kind']}): {c}  {r}{re_}")
        out.append(f"      {f['detail']}")
        for k in f["constants_used"]:
            out.append(f"      · {k['code']} (y{k.get('year')}): {k['minutes_per_unit']} min/unit "
                       f"x {k['units']:,.0f} units  [{k['source']}]")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--predicates", help="JSON file: a list of predicates, or {\"numeric_predicates\":[...]}. Omit to read stdin.")
    ap.add_argument("--rel-tol", type=float, default=DEFAULT_REL_TOL)
    ap.add_argument("--format", choices=("text", "json"), default="text")
    args = ap.parse_args()

    raw = Path(args.predicates).read_text() if args.predicates else sys.stdin.read()
    data = json.loads(raw)
    preds = data.get("numeric_predicates", data) if isinstance(data, dict) else data
    findings = audit_predicates(preds, args.rel_tol)

    if args.format == "json":
        print(json.dumps(findings, indent=2))
    else:
        print(_fmt(findings) or "(no predicates)")
        n_mis = sum(f["status"] == MISMATCH for f in findings)
        n_unv = sum(f["status"] == UNVERIFIABLE for f in findings)
        n_unit = sum(f["status"] == UNVERIFIABLE_UNIT_SEMANTICS for f in findings)
        print(f"\n{len(findings)} predicate(s): {n_mis} MISMATCH, {n_unv} UNVERIFIABLE, "
              f"{n_unit} UNVERIFIABLE_UNIT_SEMANTICS, "
              f"{len(findings) - n_mis - n_unv - n_unit} MATCH")

    return 2 if any(f["status"] == MISMATCH for f in findings) else 0


if __name__ == "__main__":
    sys.exit(main())
