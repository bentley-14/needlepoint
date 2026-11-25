"""Mechanical convergence-floor arithmetic (R2) — computed once, injected
into the counsel gating prompts.

The convergence floor's slot-counting is pure arithmetic over evidence-id
lists. Having each gating agent (first_chair, disposition) re-derive it from
the raw build wastes reasoning budget and occasionally miscounts — and the
orchestrator then silently overrides, leaving incoherent reasoning text in
the record. This module computes the audit ONCE; the agents receive it as
input and spend their effort on the parts arithmetic can't do: signal
INDEPENDENCE, refutation quality, the missed-thread audit.

Floor definition (post-F1, falsity-anchor era):

  slot 1  extremity        ≥ 1 evidence id
  slot 2  convergent       ≥ 2 evidence ids (independence judged by the agent)
  slot 3  falsity_anchor ≥ 1  OR  regulatory_corroboration ≥ 1
          — a confirmed-fact falsity anchor (impossibility math vs verified
          workforce, closed-date billing, predicate-code absence,
          specialty-incompatible service, contemporaneous LCD violation)
          makes the claims FALSE, not merely extreme. Novel-detection
          targets have clean regulatory records BY PREMISE, so regulatory
          corroboration is an alternative grounding, not a gate — it remains
          a strength bonus in scoring.
  slot 4  innocent_explanations_considered ≥ 1
  slot 5  refutation narrative ≥ 40 chars (quality judged by the agent)
"""
from __future__ import annotations

MIN_REFUTATION_CHARS = 40


def compute_floor_audit(build: dict, *, venue_token_found: bool,
                        dropped_count_ids: list[str] | None = None) -> dict:
    """Per-count slot arithmetic over a counsel_build output dict.

    Pure + deterministic: returns, per retained count, the slot counts, the
    per-slot pass flags, and an overall ``floor`` verdict:

      - ``PASS``            — all five slots clear, slot 3 via anchor or regulatory
      - ``PASS_NO_SLOT3``   — slots 1/2/4/5 clear but NEITHER falsity anchor NOR
                              regulatory corroboration (dependency territory)
      - ``FAIL``            — a non-slot-3 slot is under-filled (reasons listed)

    The agents still judge what arithmetic can't: convergent-signal
    independence, refutation quality, anchor genuineness.
    """
    dropped = set(dropped_count_ids or [])
    fb_by_count = {fb.get("count_id"): fb
                   for fb in (build.get("factual_basis_per_count") or [])}
    counts = []
    for c in build.get("counts_pled") or []:
        cid = c.get("count_id")
        if cid in dropped:
            continue
        fb = fb_by_count.get(cid)
        if fb is None:
            counts.append({
                "count_id": cid, "floor": "FAIL",
                "reasons": ["no factual_basis_per_count entry"],
                "slots": {},
            })
            continue
        n_ext = len(fb.get("extremity_evidence_ids") or [])
        n_conv = len(fb.get("convergent_signal_evidence_ids") or [])
        n_anchor = len(fb.get("falsity_anchor_evidence_ids") or [])
        n_reg = len(fb.get("regulatory_corroboration_evidence_ids") or [])
        n_innocent = len(fb.get("innocent_explanations_considered") or [])
        refut_len = len((fb.get("innocent_explanations_refuted_narrative") or "").strip())

        slots = {
            "extremity":   {"n": n_ext, "ok": n_ext >= 1},
            "convergent":  {"n": n_conv, "ok": n_conv >= 2},
            "falsity_anchor_or_regulatory": {
                "n_anchor": n_anchor, "n_regulatory": n_reg,
                "ok": (n_anchor >= 1) or (n_reg >= 1),
            },
            "innocent_considered": {"n": n_innocent, "ok": n_innocent >= 1},
            "refutation_narrative": {"chars": refut_len,
                                     "ok": refut_len >= MIN_REFUTATION_CHARS},
        }
        reasons = []
        if not slots["extremity"]["ok"]:
            reasons.append("extremity_evidence_ids < 1")
        if not slots["convergent"]["ok"]:
            reasons.append(f"convergent_signal_evidence_ids = {n_conv} (need ≥2)")
        if not slots["innocent_considered"]["ok"]:
            reasons.append("innocent_explanations_considered empty")
        if not slots["refutation_narrative"]["ok"]:
            reasons.append(f"refutation narrative too short ({refut_len} chars, "
                           f"need ≥{MIN_REFUTATION_CHARS})")
        if reasons:
            floor = "FAIL"
        elif slots["falsity_anchor_or_regulatory"]["ok"]:
            floor = "PASS"
        else:
            floor = "PASS_NO_SLOT3"
            reasons.append("no falsity anchor AND no regulatory corroboration "
                           "(slot 3) — dependency territory, not a clean PASS")
        counts.append({"count_id": cid, "floor": floor,
                       "reasons": reasons, "slots": slots})

    return {
        "venue_landscape_documented": bool(venue_token_found),
        "estimated_aggregate_damages_usd":
            build.get("estimated_aggregate_damages_usd") or 0.0,
        "counts": counts,
        "all_retained_pass": bool(counts) and all(c["floor"] == "PASS" for c in counts),
        "note": ("Slot arithmetic is mechanical and authoritative; what it CANNOT "
                 "judge — convergent-signal INDEPENDENCE, refutation QUALITY, "
                 "whether a claimed falsity anchor is genuinely a confirmed-fact "
                 "anchor — is the gating agent's job."),
    }
