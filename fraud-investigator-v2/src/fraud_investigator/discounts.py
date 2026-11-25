"""Deterministic damage-discount schedule (Layer 4).

The LLM classifies, the code computes — the LLM never picks the number.

Historically ``legal_adv_response`` free-picked a ``discount_pct`` of
10/20/30/40/50 per MITIGATED attack, and the cumulative damages-weighted
discount drove terminal verdicts (>70% → NOT_ACTIONABLE). Case outcomes
therefore hinged on uncalibrated LLM point estimates (a confirmed-clean
calibration provider accumulated 69% discount instead of clean count
defeats — see docs/calibration/CYPRESS_FALSE_POSITIVE_ANALYSIS.md).

Now the model populates a per-attack-type ``classification`` payload on each
MITIGATED disposition (see prompts/legal_adv_response.md) and this module
maps it to a discount deterministically. The model's own pick is preserved
as ``discount_pct_model`` for audit.
"""
from __future__ import annotations

import copy

# Legacy default used when a classification is missing/invalid — matches the
# old prompt's mid-scale "partial scope reduction" anchor.
_FALLBACK_PCT = 30

# Attack types that never earn a discount: binary kill-or-survive doctrines
# (the disposition tier handles the kill; a surviving count is undiscounted).
_BINARY_DOCTRINES = {"public_disclosure_bar", "first_to_file_bar"}

# severity → discount for rule_9b_insufficient / safe_harbor / other
_SEVERITY_MAP = {"minor": 10, "mitigating": 25, "dispositive": 40}

# contested Escobar/SuperValu factor count → discount for
# scienter_lacking / materiality_lacking
_CONTESTED_FACTORS_MAP = {0: 10, 1: 15, 2: 30}  # ≥3 → 45


def _num(value) -> float | None:
    """A finite non-negative number, or None (bools are not numbers here)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value < 0:
        return None
    return float(value)


def _compute(attack_type: str, classification: dict) -> tuple[int, bool, str | None]:
    """Return (discount_pct, computed, warning).

    ``computed=False`` means the classification was missing/invalid for the
    type and the legacy default (30) was used.
    """
    if attack_type in _BINARY_DOCTRINES:
        return 0, True, None

    if attack_type == "statute_of_limitations":
        out = _num(classification.get("out_of_window_damages_usd"))
        total = _num(classification.get("total_damages_usd"))
        if out is None or total is None or total <= 0:
            return _FALLBACK_PCT, False, None
        return min(50, round(100 * out / total)), True, None

    if attack_type == "damages_overstated":
        inc = _num(classification.get("recomputed_increment_usd"))
        pled = _num(classification.get("pled_amount_usd"))
        if inc is None or pled is None or pled <= 0:
            return _FALLBACK_PCT, False, None
        raw = round(100 * (1 - inc / pled))
        return max(10, min(50, raw)), True, None

    if attack_type == "legitimate_explanation":
        grounded = classification.get("grounded")
        goes_to = classification.get("goes_to")
        resolvable = classification.get("resolvable_by")
        if not isinstance(grounded, bool):
            return _FALLBACK_PCT, False, None
        if not grounded:
            return 0, True, None
        if goes_to == "periphery":
            return 20, True, None
        if goes_to == "falsity_core":
            if resolvable in ("discovery", "on_record"):
                return 35, True, None
            if resolvable == "never":
                return 50, True, (
                    "grounded falsity-core explanation that can never be "
                    "resolved — disposition should have been "
                    "DISPOSITIVE_NOT_SURVIVED, not MITIGATED")
        return _FALLBACK_PCT, False, None

    if attack_type in ("scienter_lacking", "materiality_lacking"):
        n = classification.get("contested_factors_count")
        if isinstance(n, bool) or not isinstance(n, int) or n < 0:
            return _FALLBACK_PCT, False, None
        return _CONTESTED_FACTORS_MAP.get(n, 45), True, None

    # rule_9b_insufficient / safe_harbor / other (and any unrecognized type)
    pct = _SEVERITY_MAP.get(classification.get("severity"))
    if pct is None:
        return _FALLBACK_PCT, False, None
    return pct, True, None


def compute_discount_pct(attack_type: str, classification: dict) -> int:
    """Deterministic discount for one MITIGATED attack (see module docstring)."""
    return _compute(attack_type, classification)[0]


def apply_deterministic_discounts(
    adv_response: dict,
    counsel_build: dict,
    numeric_audit: list | None = None,
) -> dict:
    """Recompute every MITIGATED attack's discount deterministically.

    Walks ``attack_dispositions``; for each MITIGATED disposition carrying a
    ``classification`` (which must include ``attack_type`` and
    ``target_counts``, copied from the defense attack), the model's
    ``discount_pct`` is preserved as ``discount_pct_model`` and replaced with
    the schedule's value. SURVIVED and DISPOSITIVE_NOT_SURVIVED dispositions
    are untouched, as are MITIGATED dispositions without a classification
    (back-compat with old parsed.json — the model's pick stands).

    When every MITIGATED disposition was recomputable and at least one
    discount changed hands, ``cumulative_case_discount_pct`` is recomputed
    with the existing damages-weighted single-pass formula
    (Σ discount_i × single_i / Σ single_i over retained counts, per-count
    discount = max over targeting attacks — non-compounding) and
    ``settlement_target_range_usd`` is recomputed consistently
    (high = 3 × (1 − cum) × Σ retained singles; low = high/2).

    ``numeric_audit`` (Guardrail-2 findings) is accepted for signature
    stability with the orchestrator threading; the damages_overstated
    figures are expected inside the classification itself.

    Idempotent: re-applying never clobbers ``discount_pct_model``.
    """
    del numeric_audit  # classification carries the audit-derived figures
    out = copy.deepcopy(adv_response)
    dispositions = out.get("attack_dispositions") or []

    n_recomputed = 0
    all_mitigated_mapped = True  # every MITIGATED attack classified + count-mapped
    for disp in dispositions:
        if disp.get("disposition") != "MITIGATED":
            continue
        cls = disp.get("classification")
        if not isinstance(cls, dict) or not cls.get("attack_type"):
            all_mitigated_mapped = False
            continue
        pct, computed, warning = _compute(str(cls["attack_type"]), cls)
        if disp.get("discount_pct_model") is None:
            disp["discount_pct_model"] = int(disp.get("discount_pct") or 0)
        disp["discount_pct"] = pct
        cls["computed"] = computed
        if warning:
            cls["warning"] = warning
        n_recomputed += 1
        if not isinstance(cls.get("target_counts"), list):
            all_mitigated_mapped = False

    if n_recomputed == 0 or not all_mitigated_mapped:
        # Nothing recomputed (or an unmappable attack) — keep the model's
        # cumulative + settlement figures (back-compat / carry-forward).
        return out

    singles = {
        c.get("count_id"): _num(c.get("estimated_single_damages_usd")) or 0.0
        for c in (counsel_build.get("counts_pled") or [])
    }
    retained = out.get("counts_retained_this_round") or []
    total_single = sum(singles.get(cid, 0.0) for cid in retained)
    if total_single <= 0:
        return out  # no damages weights — leave the model's figures alone

    per_count: dict[str, int] = {cid: 0 for cid in retained}
    for disp in dispositions:
        if disp.get("disposition") != "MITIGATED":
            continue
        for cid in disp["classification"]["target_counts"]:
            if cid in per_count:
                per_count[cid] = max(per_count[cid], disp["discount_pct"])

    weighted = sum(
        (per_count[cid] / 100.0) * singles.get(cid, 0.0) for cid in retained
    )
    cum = max(0, min(100, round(100 * weighted / total_single)))
    out["cumulative_case_discount_pct"] = cum

    high = 3 * (1 - cum / 100.0) * total_single
    out["settlement_target_range_usd"] = (
        f"${high / 2 / 1e6:.1f}M – ${high / 1e6:.1f}M"
    )
    return out
