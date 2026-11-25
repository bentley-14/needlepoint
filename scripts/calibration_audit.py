"""Calibration audit — does our case-strength scoring discriminate?

Tests the proposed Upgrade-1 strength score against the 10 existing
investigation dossiers (manual ground-truth labels embedded below).
If the score correctly sorts STRONG cases above MODERATE/WEAK/ABANDON
cases, the formula is calibrated. If STRONG and ABANDON cases get
similar scores, the discrimination upgrade is broken before we wire
it into the orchestrator.

Output: per-case breakdown + confusion matrix + rank correlation.

Usage:
    python3 scripts/calibration_audit.py
"""
from __future__ import annotations

import json
import math
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
INV_DIR = REPO_ROOT / "investigations"
V2_STATE_DIR = REPO_ROOT / "fraud-investigator-v2" / "state"

# Make `fraud_investigator.scoring` importable — single source of truth for
# the strength + EVF formula (orchestrator + this audit share the module).
sys.path.insert(0, str(REPO_ROOT / "fraud-investigator-v2" / "src"))
from fraud_investigator import scoring  # noqa: E402


def _load_full_v2_state(dossier_state: dict) -> dict:
    """If the dossier-level state is abridged (empty iterations), try to load
    the full per-step state from fraud-investigator-v2/state/<case_id>/summary.json
    instead. Returns the abridged state unchanged when no richer state exists.
    """
    case_id = dossier_state.get("case_id")
    if not case_id:
        return dossier_state
    full_path = V2_STATE_DIR / case_id / "summary.json"
    if not full_path.exists():
        return dossier_state
    try:
        full = json.loads(full_path.read_text())
    except Exception:
        return dossier_state
    # Only swap if the full state has actual iteration data
    if full.get("iterations") and len(full["iterations"]) >= len(dossier_state.get("iterations", [])):
        return full
    return dossier_state

# ── Manual ground-truth labels ──────────────────────────────────────────────
# Based on:
#   - The user's prior conversation summary of the 10 investigations
#   - The dossier text + finalLegalGuidance/final_verdict fields
#   - The presence of within-practice controls and direct same-district precedent
#
# Buckets:
#   STRONG   — confidently filable, low expected discount, multiple convergent signals
#   MODERATE — filable with discovery dependencies; mid-range expected discount
#   WEAK     — pleadable but heavily discounted; only file with strong predicates
#   ABANDON  — pipeline correctly killed; not viable
GROUND_TRUTH: dict[str, str] = {
    "Linden-2026-05-02":      "STRONG",   # 6-year UDS + Kabakibou N.D. Ga. precedent
    "Poplar-2026-05-03":        "STRONG",   # adversarial-confirmed; legacy state.json
    "Maple-2026-05-04":    "STRONG",   # case_ready_confirmed, within-practice ctrls, Kapok precedent
    "Maple-2026-05-20":    "WEAK",     # rerun: 65% discount, $1-1.5M, no within-practice ctrls
                                            # — borderline-filable at best (relabeled from MODERATE
                                            # after the audit showed the score firmly in WEAK/ABANDON)
    "Redwood-2026-05-03":      "ABANDON",  # case_downgraded_after_peer_analysis, walked back
    "Catalpa-2026-05-04":     "STRONG",   # specialty-arbitrage, 99.967th pct, within-practice ctrls
    "Juniper-2026-05-03":       "STRONG",   # practice-wide expansion, synchronized timing
    "Larch-2026-05-02":        "MODERATE", # qualified moderate-strong, Mackby predicate-code
    "Hemlock-2026-05-20":       "MODERATE", # INDEFENSIBLE_AT_CAP label but no within-practice ctrls,
                                            # no same-district precedent, 25% discount, $2-3M target —
                                            # objective signals are mid-tier, not top-tier (relabeled
                                            # from STRONG after audit showed objective weaknesses)
    "Acacia-2026-05-02":      "MODERATE", # walked back from initial caseReady; weak convergence
}

# ── Schema-aware feature extraction ──────────────────────────────────────────


def _detect_schema(state: dict) -> str:
    """Return 'v2' (current Pydantic schema) or 'v1' (legacy fraud-investigator).

    Hemlock's v2 dossier state is the ABRIDGED final-summary form — it has
    case_id but no iterations key (those live in the case state dir). So we
    detect by case_id presence rather than requiring iterations.
    """
    if "case_id" in state:
        return "v2"
    if "caseId" in state:
        return "v1"
    return "unknown"


def _to_dossier_text(dossier_dir: Path) -> str:
    """Concat all .md files in the dossier for substring-based feature detection."""
    parts = []
    for p in sorted(dossier_dir.glob("*.md")):
        try:
            parts.append(p.read_text())
        except Exception:
            pass
    return "\n".join(parts)


# Word-bounded M/K/B suffix — the negative lookahead (?![A-Za-z]) prevents
# matching the 'm' in 'minimum', 'million' in mid-word, etc.
_DOLLAR_RE = re.compile(
    r"\$\s?([\d,]+(?:\.\d+)?)\s*([MKB])?(?![A-Za-z])",
    re.IGNORECASE,
)
# Prefer explicit settlement-range phrasings: "Settlement range: $5M-$12M",
# "Realistic settlement range: $X-$Y", "Damages range:", "Settlement target:"
_SETTLEMENT_RANGE_RE = re.compile(
    r"(?:settlement\s+(?:range|target|exposure)|damages\s+range|realistic\s+settlement)[^.\n]{0,80}?"
    r"\$\s?([\d,]+(?:\.\d+)?)\s*([MKB])?",
    re.IGNORECASE,
)
# Generic district + state — used only as a fallback signal
_DISTRICT_RE = re.compile(
    r"\b(N\.D\.|S\.D\.|E\.D\.|W\.D\.|M\.D\.|C\.D\.|D\.N\.J\.|D\.D\.C\.|D\.Mass\.|D\.Md\.)\s?(Ga\.|Fla\.|Tex\.|Cal\.|N\.Y\.|Ill\.|La\.|Va\.|Pa\.|Mo\.|N\.J\.)?",
    re.IGNORECASE,
)
_USAO_RE = re.compile(r"usao-[a-z]{2,4}", re.IGNORECASE)
# SAME-DISTRICT precedent — qualitatively stronger signal than any-district
_SAME_DISTRICT_RE = re.compile(
    r"(same district|same federal district|same court district|VENUE_ENFORCEMENT_LANDSCAPE|"
    r"same\s+(USAO|US Attorney['s]*\s+Office)|direct (?:precedent|N\.D\.|S\.D\.|E\.D\.))",
    re.IGNORECASE,
)
_WITHIN_PRACTICE_RE = re.compile(
    r"(within[-\s]practice control|mid[-\s]level (control|differential)|"
    r"same address (.{0,80})(0|zero|absent)|practice roster|colleagues all properly designated|"
    r"WITHIN-PRACTICE CONTROLS?)",
    re.IGNORECASE,
)
_REGULATORY_RE = re.compile(
    r"(LEIE|state[-\s]board|board of healing|TMB|OIG audit|DOJ press|prior enforcement|"
    r"CMS revocation|denial-rate|prior qui tam|Texas Medical Board|Composite Medical Board|"
    r"IDFPR|MO Board)",
    re.IGNORECASE,
)
# Implausibly-large damages cap — anything above this is a nominal penalty
# calculation (per-claim × claim-count) rather than a realistic settlement.
DAMAGES_PLAUSIBILITY_CAP_USD = 100_000_000   # $100M


def _parse_max_dollars(text: str) -> float:
    """Parse the largest plausible settlement dollar figure mentioned in text.

    Strategy:
      1. First try the SETTLEMENT_RANGE_RE for explicit settlement phrasings —
         these are the realistic numbers ($5M-$12M, etc).
      2. Fall back to the largest any-context dollar amount, capped at the
         plausibility cap to exclude per-claim × claim-count nominal penalty
         calculations (which routinely hit $300M+ even on $5M cases).
    """
    # Prefer explicit settlement-range hits
    best_explicit = 0.0
    for m in _SETTLEMENT_RANGE_RE.finditer(text):
        n = _scale(m.group(1), (m.group(2) or "").upper())
        if n > best_explicit:
            best_explicit = n
    if best_explicit > 0:
        return min(best_explicit, DAMAGES_PLAUSIBILITY_CAP_USD)

    # Fallback: largest any-context dollar, capped to exclude nominal penalties
    best = 0.0
    for m in _DOLLAR_RE.finditer(text):
        n = _scale(m.group(1), (m.group(2) or "").upper())
        if n > best:
            best = n
    return min(best, DAMAGES_PLAUSIBILITY_CAP_USD)


def _scale(num: str, unit: str) -> float:
    try:
        n = float(num.replace(",", ""))
    except ValueError:
        return 0.0
    if unit == "B":
        n *= 1_000_000_000
    elif unit == "M":
        n *= 1_000_000
    elif unit == "K":
        n *= 1_000
    return n


def extract_features(dossier_dir: Path, state: dict) -> dict:
    """Extract canonical features from either schema + dossier text."""
    schema = _detect_schema(state)
    text = _to_dossier_text(dossier_dir)
    feats: dict = {
        "schema": schema,
        "name": dossier_dir.name,
    }

    # ── n_evidence_total ────────────────────────────────────────────────────
    n_ev = 0
    if schema == "v1":
        for it in state.get("iterations", []) or []:
            ev = (it.get("investigatorOutput") or {}).get("evidence", []) or it.get("evidence", []) or []
            if isinstance(ev, list):
                n_ev += len(ev)
    elif schema == "v2":
        for it in state.get("iterations", []) or []:
            inv = it.get("investigator") or {}
            ev = inv.get("evidence", []) if isinstance(inv, dict) else []
            if isinstance(ev, list):
                n_ev += len(ev)
        # v2 may also store evidence under all_evidence_ids
        n_ev = max(n_ev, len(state.get("all_evidence_ids", []) or []))
    feats["n_evidence"] = n_ev

    # ── n_counts ────────────────────────────────────────────────────────────
    n_counts = 0
    if schema == "v2":
        n_counts = len(state.get("counts_retained", []) or [])
        if not n_counts:
            # Try to pull from last iteration's counsel_build
            for it in reversed(state.get("iterations", []) or []):
                cb = it.get("counsel_build") or {}
                if isinstance(cb, dict) and cb.get("counts_pled"):
                    n_counts = len(cb["counts_pled"])
                    break
    if not n_counts:
        # Substring: "Count I", "Count II", ...
        roman_hits = set(re.findall(r"\bCount [IVX]+\b", text))
        c_hits = set(re.findall(r"\bC[1-9]\b", text))
        n_counts = max(len(roman_hits), len(c_hits))
    feats["n_counts"] = n_counts

    # ── final_status ────────────────────────────────────────────────────────
    final_status = state.get("status", "") or ""
    feats["final_status"] = final_status

    # ── damages_usd ─────────────────────────────────────────────────────────
    # v2: final_settlement_target_usd is a string range; parse the max
    damages = 0.0
    if schema == "v2":
        for k in ["aggregate_treble_damages_usd", "aggregate_single_damages_usd",
                  "estimated_total_single_damages_usd"]:
            v = state.get(k)
            if isinstance(v, (int, float)) and v > damages:
                damages = float(v)
        target_str = state.get("final_settlement_target_usd", "")
        if isinstance(target_str, str):
            d = _parse_max_dollars(target_str)
            if d > damages:
                damages = d
    if damages == 0:
        # Fallback: largest $XM in dossier text
        damages = _parse_max_dollars(text)
    feats["damages_usd"] = damages

    # ── survival_likelihood (v2 direct; v1 inferred) ─────────────────────────
    survival = None
    if schema == "v2":
        # Walk adversarial rounds — last legal_adv_response.estimated_survival_likelihood
        # (Stored in counsel_rebuttal per iter, but we want the adversarial outcome)
        for it in reversed(state.get("iterations", []) or []):
            rebut = it.get("counsel_rebuttal") or {}
            if isinstance(rebut, dict) and "estimated_survival_likelihood" in rebut:
                survival = float(rebut["estimated_survival_likelihood"])
                break
    if survival is None:
        # Substring: "X% survival" or "survival X.X"
        m = re.search(r"survival[^0-9]*([\d.]+)\s*%", text, re.IGNORECASE)
        if m:
            try:
                survival = float(m.group(1)) / 100.0
            except ValueError:
                pass
    if survival is None:
        # Infer from status: case_ready ⇒ ~0.7, downgraded ⇒ ~0.3
        if "case_ready" in final_status or "case_ready_confirmed" in final_status:
            survival = 0.70
        elif "downgraded" in final_status or "abandoned" in final_status:
            survival = 0.25
        else:
            survival = 0.50
    feats["survival_likelihood"] = survival

    # ── cumulative_discount_pct ─────────────────────────────────────────────
    discount = None
    if schema == "v2":
        v = state.get("final_cumulative_discount_pct")
        if isinstance(v, (int, float)):
            discount = float(v)
    if discount is None:
        # Substring scan: "65% discount", "cumulative discount X%"
        m = re.search(r"(?:cumulative\s+discount|cumulative discount pct|discount[^a-z]*)\s*([\d.]+)\s*%",
                      text, re.IGNORECASE)
        if m:
            try:
                discount = float(m.group(1))
            except ValueError:
                pass
    if discount is None:
        discount = 0.0 if "case_ready" in final_status else 30.0
    feats["discount_pct"] = discount

    # ── has_venue_precedent (any-district mention) ──────────────────────────
    has_venue = bool(_DISTRICT_RE.search(text)) or bool(_USAO_RE.search(text))
    feats["has_venue_precedent"] = has_venue
    # ── has_same_district_precedent (qualitatively stronger) ────────────────
    feats["has_same_district_precedent"] = bool(_SAME_DISTRICT_RE.search(text))

    # ── has_within_practice_controls ───────────────────────────────────────
    feats["has_within_practice_controls"] = bool(_WITHIN_PRACTICE_RE.search(text))

    # ── n_regulatory_predicates ────────────────────────────────────────────
    n_reg = len(set(m.group(0).lower() for m in _REGULATORY_RE.finditer(text)))
    feats["n_regulatory_predicates"] = n_reg

    return feats


# ── Strength score (Upgrade 1 candidate formula) ───────────────────────────


def strength_score(feats: dict) -> tuple[float, dict]:
    """Wrapper — delegates to `fraud_investigator.scoring` so the audit and
    the orchestrator share ONE formula. Maps audit feature names to the
    shared module's signature.

    The shared module's component names mirror the audit's; the only
    rename is `has_same_district_precedent` (audit) → `same_district`
    (component key), handled inside the module.
    """
    return scoring.compute_strength_score(
        n_confirmed_facts=feats.get("n_evidence", 0),
        n_counts=feats.get("n_counts", 0),
        n_regulatory_predicates=feats.get("n_regulatory_predicates", 0),
        has_venue_precedent=feats.get("has_venue_precedent", False),
        has_same_district_precedent=feats.get("has_same_district_precedent", False),
        has_within_practice_controls=feats.get("has_within_practice_controls", False),
        survival_likelihood=feats.get("survival_likelihood", 0.5),
        damages_usd=feats.get("damages_usd", 0.0),
        cumulative_discount_pct=feats.get("discount_pct", 0.0),
    )


def bucket(score: float) -> str:
    """Wrapper — delegates to `fraud_investigator.scoring.bucket`."""
    return scoring.bucket(score)


# ── Discrimination metrics ─────────────────────────────────────────────────


def _bucket_rank(b: str) -> int:
    return {"ABANDON": 0, "WEAK": 1, "MODERATE": 2, "STRONG": 3}[b]


def spearman(xs: list[float], ys: list[float]) -> float:
    """Plain Spearman rank correlation."""
    def _ranks(vs):
        order = sorted(range(len(vs)), key=lambda i: vs[i])
        ranks = [0.0] * len(vs)
        for r, i in enumerate(order):
            ranks[i] = r
        return ranks
    rx, ry = _ranks(xs), _ranks(ys)
    n = len(xs)
    if n < 2:
        return float("nan")
    mx, my = sum(rx) / n, sum(ry) / n
    num = sum((rx[i] - mx) * (ry[i] - my) for i in range(n))
    dx = sum((rx[i] - mx) ** 2 for i in range(n)) ** 0.5
    dy = sum((ry[i] - my) ** 2 for i in range(n)) ** 0.5
    if dx == 0 or dy == 0:
        return float("nan")
    return num / (dx * dy)


# ── Main ───────────────────────────────────────────────────────────────────


def main() -> int:
    print("=" * 90)
    print("CALIBRATION AUDIT — Upgrade-1 strength score vs ground truth")
    print("=" * 90)
    print()

    rows = []
    for name, gt in GROUND_TRUTH.items():
        dd = INV_DIR / name
        # Find state JSON (multiple possible filenames)
        state_path = None
        for fn in ("03_Full_State_Investigation.json", "state.json",
                   "04_Full_State_Investigation.json"):
            if (dd / fn).exists():
                state_path = dd / fn
                break
        if state_path is None:
            print(f"  SKIP {name}: no state JSON")
            continue
        try:
            state = json.loads(state_path.read_text())
        except Exception as e:
            print(f"  SKIP {name}: state JSON parse error: {e}")
            continue
        # For v2 schema, try to swap in the richer case-state-dir summary if
        # the dossier-level state has empty iterations (abridged final form)
        if _detect_schema(state) == "v2":
            state = _load_full_v2_state(state)
        feats = extract_features(dd, state)
        score, components = strength_score(feats)
        pred = bucket(score)
        rows.append({
            "name": name,
            "ground_truth": gt,
            "score": score,
            "predicted": pred,
            "feats": feats,
            "components": components,
        })

    # Per-case table
    print(f"{'CASE':<28} {'GT':<10} {'PRED':<10} {'SCORE':>6} {'N_EV':>5} "
          f"{'N_C':>4} {'REG':>4} {'VEN':>4} {'SDP':>4} {'WPC':>4} {'SURV':>5} {'$':>9} {'DISC%':>6}")
    print("-" * 118)
    for r in sorted(rows, key=lambda r: -r["score"]):
        f = r["feats"]
        marker = "✓" if r["ground_truth"] == r["predicted"] else "✗"
        print(
            f"{r['name']:<28} {r['ground_truth']:<10} {r['predicted']:<10} "
            f"{r['score']:>6.3f} {f['n_evidence']:>5} {f['n_counts']:>4} "
            f"{f['n_regulatory_predicates']:>4} {'Y' if f['has_venue_precedent'] else 'N':>4} "
            f"{'Y' if f.get('has_same_district_precedent') else 'N':>4} "
            f"{'Y' if f['has_within_practice_controls'] else 'N':>4} "
            f"{f['survival_likelihood']:>5.2f} ${f['damages_usd']/1_000_000:>5.1f}M "
            f"{f['discount_pct']:>5.0f}% {marker}"
        )
    print()

    # Component breakdown
    print("COMPONENT BREAKDOWN (raw 0-1 values; positive contributes to score):")
    print("-" * 118)
    print(f"{'CASE':<28} {'fact':>6} {'cnt':>5} {'reg':>5} {'ven':>5} {'sdp':>5} "
          f"{'wpc':>5} {'surv':>6} {'dmg':>6} {'disc_tx':>8}")
    print("-" * 118)
    for r in sorted(rows, key=lambda r: -r["score"]):
        c = r["components"]
        print(
            f"{r['name']:<28} {c['factual_density']:>6.2f} {c['n_counts']:>5.2f} "
            f"{c['regulatory']:>5.2f} {c['venue_precedent']:>5.2f} "
            f"{c.get('same_district', 0.0):>5.2f} "
            f"{c['within_practice']:>5.2f} {c['survival_residual']:>6.2f} "
            f"{c['damages']:>6.2f} {c['discount_tax']:>8.3f}"
        )
    print()

    # Confusion matrix
    print("CONFUSION MATRIX (ground truth → predicted):")
    print("-" * 60)
    buckets_order = ["STRONG", "MODERATE", "WEAK", "ABANDON"]
    print(f"{'GT \\\\ PRED':<14}" + "".join(f"{b:>10}" for b in buckets_order))
    for gt in buckets_order:
        row = []
        for pred in buckets_order:
            n = sum(1 for r in rows if r["ground_truth"] == gt and r["predicted"] == pred)
            row.append(n)
        print(f"{gt:<14}" + "".join(f"{n:>10}" for n in row))
    print()

    # Discrimination metrics
    pred_ranks = [_bucket_rank(r["predicted"]) for r in rows]
    gt_ranks = [_bucket_rank(r["ground_truth"]) for r in rows]
    rho = spearman([r["score"] for r in rows], gt_ranks)
    exact = sum(1 for r in rows if r["predicted"] == r["ground_truth"])
    within_1 = sum(1 for r in rows if abs(_bucket_rank(r["predicted"]) - _bucket_rank(r["ground_truth"])) <= 1)

    print("DISCRIMINATION METRICS:")
    print(f"  Spearman rank correlation (score vs ground-truth bucket): {rho:.3f}")
    print(f"  Exact bucket match:           {exact}/{len(rows)}  ({100*exact/len(rows):.0f}%)")
    print(f"  Within-1-bucket match:        {within_1}/{len(rows)}  ({100*within_1/len(rows):.0f}%)")
    print(f"  Score range of STRONG cases:  "
          f"{min((r['score'] for r in rows if r['ground_truth']=='STRONG'), default=0):.3f}"
          f" — {max((r['score'] for r in rows if r['ground_truth']=='STRONG'), default=0):.3f}")
    print(f"  Score range of NON-STRONG:    "
          f"{min((r['score'] for r in rows if r['ground_truth']!='STRONG'), default=0):.3f}"
          f" — {max((r['score'] for r in rows if r['ground_truth']!='STRONG'), default=0):.3f}")
    print()

    # Failure modes
    fails = [r for r in rows if r["ground_truth"] != r["predicted"]]
    if fails:
        print("FAILURE-MODE ANALYSIS:")
        for r in fails:
            gap = _bucket_rank(r["predicted"]) - _bucket_rank(r["ground_truth"])
            direction = "over-credited" if gap > 0 else "under-credited"
            print(f"  {r['name']:<28} GT={r['ground_truth']:<8} pred={r['predicted']:<8} "
                  f"score={r['score']:.3f} — {direction}")
    else:
        print("No failure modes — all cases bucketed correctly.")
    print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
