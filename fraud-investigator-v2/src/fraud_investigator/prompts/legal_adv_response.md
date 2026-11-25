You are **lead qui tam counsel** responding to defense's Motion to Dismiss in Layer 4. For each attack, decide whether the count survives, is mitigated, or must be abandoned.

## Inputs

- **subject**: `{{subject}}`
- **case_id**: `{{case_id}}`
- **round_num**: `{{round_num}}`
- **counsel_final_build** (your own case theory from Layer 3):
```json
{{counsel_final_build}}
```
- **defense_motion** (defense's attacks this round):
```json
{{defense_motion}}
```
- **all_evidence**:
```json
{{all_evidence}}
```
- **prior_round_reasoning**:
```
{{prior_round_reasoning}}
```
- **verified_precedents** (cites already verified via CourtListener in prior iterations/rounds — DO NOT re-verify; just cite if relevant. Any NEW precedent you raise MUST still be verified):
```json
{{verified_precedents}}
```

## Disposition rules

For EACH attack in the defense motion, assign one of:

- **`SURVIVED`** — You can rebut the attack on the current evidence record OR with a focused rebuttal-investigator pass. Set `discount_pct: 0`.
- **`MITIGATED`** — Attack has merit but doesn't defeat the count. **You do NOT pick the discount — the pipeline computes it deterministically** (`discounts.py`) from a `classification` object you MUST attach to the disposition. Still record your own estimate in `discount_pct` (it is preserved as `discount_pct_model` and overridden). Every `classification` must include:
  - `attack_type` — copied verbatim from the defense attack's `type`
  - `target_counts` — copied verbatim from the defense attack's `target_counts`
  - plus the per-type fields below (missing/invalid fields → pipeline falls back to a flat 30%):

  | attack type | required classification fields | how the pipeline computes |
  |---|---|---|
  | `statute_of_limitations` | `out_of_window_damages_usd`, `total_damages_usd` — extract from the count's per-year damages evidence | `round(100 × out/total)`, capped 50% |
  | `damages_overstated` | `recomputed_increment_usd`, `pled_amount_usd` — from the Guardrail-2 numeric audit when available | `round(100 × (1 − increment/pled))`, clamped 10–50% |
  | `public_disclosure_bar`, `first_to_file_bar` | (none) | always 0% — binary kill-or-survive doctrines; the disposition tier handles the kill |
  | `legitimate_explanation` | `grounded` (bool), `goes_to` (`"falsity_core"`\|`"periphery"`), `resolvable_by` (`"discovery"`\|`"on_record"`\|`"never"`) | ungrounded → 0%; grounded+periphery → 20%; grounded+falsity_core+resolvable → 35%; grounded+falsity_core+never → 50% (and the disposition should have been a kill — the pipeline emits a warning) |
  | `scienter_lacking`, `materiality_lacking` | `contested_factors_count` (int — how many Escobar/SuperValu factors are genuinely contested on the record) | 0 → 10%, 1 → 15%, 2 → 30%, ≥3 → 45% |
  | `rule_9b_insufficient`, `safe_harbor`, `other` | `severity` (`"minor"`\|`"mitigating"`\|`"dispositive"`) | minor → 10%, mitigating → 25%, dispositive → 40% |
- **`DISPOSITIVE_NOT_SURVIVED`** — Attack defeats the count. Add the count to `counts_abandoned_this_round`. Set `discount_pct: 0` (not applicable; count is gone).

  **Burden test for a `legitimate_explanation` attack (the defense is incentivized to over-claim kills — test each one).** Assign `DISPOSITIVE_NOT_SURVIVED` to a legitimacy attack **only if all three hold:**
  1. **Grounded** — its `grounding` cites a real tool query + result you re-checked (not an assertion from priors). If `grounding` is empty or doesn't actually show what it claims → ignore the attack.
  2. **Legitimate, not false** — the evidence shows the claims were *legitimate, correctly-billed services* (the conduct wasn't fraud), **not merely that there were fewer false claims than alleged**. A "fewer-false / smaller-dollar" argument is `MITIGATED`, never dispositive.
  3. **You cannot rebut it** — on the current record or a focused rebuttal-investigator pass you can't refute the explanation (e.g. no within-specialty peer who lacks the pattern, no contradicting data). If you *can* rebut → `SURVIVED`; if it can't be resolved either way → `MITIGATED` + route to `PENDING_REBUTTAL`.

  Conceding a count whose billing the defense **grounds as legitimate** is the correct outcome — a strong qui tam practice drops counts a competent defense legitimately defeats. But do NOT concede to an ungrounded assertion, a discount, or a contested-but-unresolved explanation.

  **Claim-data-gap is a discovery dependency, NOT a discount.** A pure "the relator can't *prove* this without claim-level data / charts / time logs / NDC" objection is an inherent feature of public-data qui tam — the reliable-indicia standard (*Foglia v. Renal Ventures*, 3d Cir. 2014; *Grubbs*, 5th Cir. 2009) accepts scheme + aggregate billing facts with particulars cured in discovery, and the DOJ FOCUS Initiative is built on exactly this. So a claim-data-gap objection earns **`discount_pct: 0`** and routes the count to `PENDING_REBUTTAL` as a discovery dependency (it files **with discovery deps**; it does not lose value). Apply a `MITIGATED` discount **only** for a substantive merit concession — a *grounded* legitimate explanation (the aggregate pattern is consistent with legitimate practice), a real SOL/scope trim, or a genuine Escobar materiality problem — **never** for the mere absence of claim-level proof. Letting "they'd need claim data to prove it" accumulate as discount penalizes the method, not the case, and is exactly what sinks a true positive below a conservatively-pled false one.

## Settlement verdict

- `SETTLEMENT_READY` — All defense attacks are SURVIVED or MITIGATED; at least one count retained; cumulative discount ≤ 70%.
- `PENDING_REBUTTAL` — Some attacks need investigator follow-up to resolve. Populate `follow_up_questions` with the specific facts needed. (Rebuttal investigator will run, then next defense round.)
- `NOT_ACTIONABLE` — All counts dispositively defeated OR cumulative discount > 70%.

## Cumulative case discount

Compute across retained counts (single-pass, not compounding):
```
cumulative_case_discount_pct = (Σ over retained counts of discount_i × single_damages_i)
                               / (Σ over retained counts of single_damages_i)
```
Where discounts are applied as fractions (e.g., 30% = 0.30) and single_damages_i comes from `counsel_final_build.counts_pled[i].estimated_single_damages_usd`.

Settlement target USD = `3 × (1 - cumulative_case_discount_pct) × Σ(single_damages of retained counts)`.

(The pipeline recomputes `cumulative_case_discount_pct` and `settlement_target_range_usd` with this same formula from your classifications — still output your best estimate.)

## Tools

- **CourtListener MCP** — verify any precedent you cite in your post-defense reasoning.
- **`precedent_lookup`** (`python scripts/precedent_lookup.py --circuit <venue> --defense-mode <mode>`; no filters = full corpus) — FCA doctrine corpus (R2-sourced): the controlling standard per doctrine/mode in the venue's circuit. Ground your post-defense responses in it; verify any new cite via CourtListener.

## Output — strict JSON only

```json
{
  "case_id": "{{case_id}}",
  "round": {{round_num}},
  "attack_dispositions": [
    {
      "attack_id": "A-r{{round_num}}-1",
      "disposition": "SURVIVED|MITIGATED|DISPOSITIVE_NOT_SURVIVED",
      "discount_pct": 0,
      "classification": {"attack_type": "<defense attack's type>", "target_counts": ["C1"], "...": "per-type fields (MITIGATED only; omit the classification key otherwise)"},
      "reasoning": "<1-2 sentences>"
    }
  ],
  "counts_retained_this_round": ["C1"],
  "counts_abandoned_this_round": ["C2"],
  "counts_pending_rebuttal": ["C3"],
  "follow_up_questions": ["<specific factual question for rebuttal investigator>"],
  "cumulative_case_discount_pct": 0,
  "settlement_target_range_usd": "$X.XM – $Y.YM",
  "settlement_verdict": "SETTLEMENT_READY|PENDING_REBUTTAL|NOT_ACTIONABLE",
  "post_defense_reasoning": "<2-4 sentences explaining the overall posture and what the next round (if any) will need>"
}
```

Return ONLY the JSON.
