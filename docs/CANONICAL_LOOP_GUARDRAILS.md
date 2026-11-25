# Canonical-loop guardrails — verified-fact discipline

Lessons from the Birch / Agency Myrtle run (2026-06), where avoidable factual errors
reached attorney-facing output. The meta-principle:

> **Verify, don't assert, anything that is lookup-able — and grounding in a source is
> NOT enough if the source itself can be wrong. High-stakes constants must be
> cross-verified against the *authoritative* source, anomaly flags must *trigger*
> verification (not be printed and ignored), and the adversary must audit the numbers.**

Each error below maps to a concrete guardrail. All five are now implemented:
(1) authoritative-constant verification, (2) defense numeric-predicate audit,
(3) innocent-explanation foreclosure check, (4) human-in-the-loop checkpoint (PR #334),
(5) per-count precedent anchor.

---

## Failure 0 — what actually happened (so we fix the right thing)
The 99489 "60-minute" error was **not a free hallucination**. The agent grounded in
`hcpcs_lookup`, which faithfully surfaced **CMS's own published PUF descriptor** — which
is **itself wrong** for 99489 in 2021–2024 ("each additional 60 minutes"; the AMA-CPT
value is 30). Three failures stacked: (a) corrupt source descriptor, (b) the tool showed
the *most-recent* (wrong) value and *printed* an "identity VARIES across years" flag that
nothing acted on, (c) the `defense` stage never re-derived the impossibility number a
2× error would have exposed.

## Guardrail 1 — Authoritative-constant verification  ✅ IMPLEMENTED
- `scripts/ingest/hcpcs_authoritative_overrides.json` — curated AMA-CPT constants
  (minutes_per_unit + authoritative descriptor) for time-based codes (99487/99489/
  99490/99439/99491/99437; extend as needed).
- `scripts/hcpcs_lookup.py` (`_apply_override`) parses the PUF descriptor's minutes and
  **cross-checks against the authoritative value; on conflict it swaps in the
  authoritative descriptor and prints a 🛑 hard warning** ("PUF says 60, CPT is 30, use
  30; any unit→time/$/FTE math MUST use 30"). Verified: 99489 now returns 30 min + the
  warning; controls (99487, drug codes) unchanged.
- **Rule for agents:** any claim converting billing units → time/$/FTE must use the
  looked-up authoritative constant, and a 🛑 override note is dispositive over the PUF.

## Guardrail 2 — Defense numeric-predicate audit  ✅ IMPLEMENTED
The adversarial `defense` stage now, before attacking legal theories, **re-derives every
headline quantitative claim from primary inputs + canonical constants** — impossibility
hours, FTE, percentile multiples, ratios, damages — and any discrepancy is surfaced as a
dispositive defect. A real defendant's expert catches a 2× CPT error instantly; the
simulated defense now does too.
- `scripts/audit_case_numerics.py` — deterministic re-derivation (NO LLM). Resolves
  minutes-per-unit through `hcpcs_lookup.canonical_minutes_per_unit` (the **single source
  of truth**: authoritative override → CMS MPFS RVU → PUF). Emits one finding per claim:
  `{id, kind, status (MATCH|MISMATCH|UNVERIFIABLE), claimed, recomputed, rel_error,
  constants_used, detail}`; CLI exits 2 on any MISMATCH. Tested
  (`tests/test_audit_case_numerics.py`): the Birch 99489 case (118,800 units claimed as
  118,800 h on the 60-min PUF descriptor) is flagged MISMATCH; the canonical 59,400 h
  (30 min) passes.
- `counsel_build` emits `numeric_predicates` (new `CounselBuildOutput` field) for every
  headline figure; the orchestrator (`_audit_numeric_predicates`, before the defense loop)
  runs the audit, persists `CaseState.numeric_audit`, and **injects the findings into the
  `defense` prompt** as mandatory dispositive defects. The deterministic MISMATCH is
  authoritative regardless of whether the LLM defense raises it.

## Guardrail 3 — Innocent-Explanation Foreclosure Check  ✅ IMPLEMENTED (the new post-case-ready stage)
Operationalizes the Twombly/Iqbal/*Integra* survival test: **fraud must be the *more
plausible* inference, and every obvious innocent explanation must be foreclosed (with a
cited source) or flagged LIVE.** Built as a first-class pipeline stage:
- **Schemas** (`schemas.py`): `ForeclosureLedgerOutput` + `InnocentExplanation` —
  `{explanation, taxonomy_tag, steelman, plausibility_unrebutted, status:
  FORECLOSED|PARTIAL|LIVE, foreclosing_evidence:[cited], evidence_soundness_check,
  residual, closure_owner}`.
- **Stage**: `agents/foreclosure_check.py` + `prompts/foreclosure_check.md`, routed
  opus / xhigh (the plausibility test is the heart). Process per count: **generate**
  (reusable taxonomy + case-specific) → **steelman** → **seek foreclosure** (grounded;
  cite-or-stays-LIVE; **audit the constants behind the foreclosing evidence**, and a
  Guardrail-2 MISMATCH poisons any foreclosure built on that number) → **classify**.
- **Orchestrator**: `_run_foreclosure_check()` runs after the adversarial loop, before the
  final strength score, and persists `CaseState.foreclosure_ledger`.
- **The cap**: `scoring.foreclosure_residual()` RE-derives the max LIVE plausibility from
  the ledger rows (not the LLM's self-report). `≥ FORECLOSURE_LIVE_CAP` (0.5) caps the
  strength bucket (STRONG→MODERATE) and **blocks a clean FILE** (SETTLEMENT_READY →
  FILE_WITH_DISCOVERY_DEPS). Tested (`fraud-investigator-v2/tests/test_foreclosure_cap.py`).
- Reusable taxonomy includes `large_clinical_staff_workforce_410_26` — the §410.26 "a big
  incident-to staff furnished the time" explanation, **the one currently LIVE for Birch**
  (foreclosed only by a verified FTE count).

## Guardrail 4 — Human-in-the-loop checkpoint  ✅ IMPLEMENTED (PR #334)
The failure class G1–G3 don't catch: the loop reaches a **dispositive, case-killing**
conclusion that rests on a premise it **cannot verify**, and silently resolves it
*against* the case. The motivating incident is the same Birch run — but a *different* error.
The rebuttal investigator found a **sealed** docket (*Dogwood v. Agency Myrtle LLC*) and
asserted "this is a qui tam lawsuit" at `confirmed_fact` tier, triggering a first-to-file
bar (31 USC 3730(b)(5)) → ABANDON. But a sealed complaint's nature-of-suit cannot be read
from public dockets, and *Dogwood* was in fact **NOS 442 Employment**, not a qui tam. The
case was abandoned on a phantom bar. (This is the same *Dogwood* phantom the Guardrail-4
cardinal rule below warns about — Guardrail 4 now *enforces* it with a hard pause.)

Instead of guessing, the loop **PAUSES**, asks a human a specific question, takes the
answer as `human_attested` evidence, and resumes. This is the human-in-the-loop checkpoint.
- **Two pure detectors** (`fraud-investigator-v2/src/fraud_investigator/checkpoint.py`):
  each returns a `ReviewRequest` when a pause is warranted, else `None`.
  - `detect_jurisdiction_checkpoint` — fires when a first-to-file / public-disclosure bar
    is the dispositive driver **and** it rests on a `confirmed_fact` claim that a *sealed*
    prior case is a qui tam / FCA action (the exact unverifiable premise — you cannot read
    a sealed filing to confirm its nature-of-suit).
  - `detect_foreclosure_checkpoint` — fires when a foreclosure DECLINE
    (`fraud_is_more_plausible_inference is False`) rests on a **LIVE** innocent explanation
    whose `closure_owner` is `discovery:<item>` or `cannot_close_pre_filing` — i.e. closable
    only by a discrete off-record fact (an FTE count, "is this a legitimate place of service?").
  - **Selective by construction:** a checkpoint fires ONLY when the dispositive premise is
    (a) the actual driver of the decline AND (b) resolvable by a discrete fact a human could
    supply. Genuine merits weaknesses do **not** pause the loop.
- **The new verdict** (`schemas.py`): `FinalVerdict.NEEDS_HUMAN_CHECKPOINT` — a **RESUMABLE
  pause**, distinct from the terminal `NEEDS_HUMAN` ("the loop got stuck", consumed as
  `complete`, not resumable). The structured question lives on `CaseState.review_request`
  (`ReviewRequest`: `checkpoint_type`, `question`, `unverifiable_premise`, `why_unverifiable`,
  `provisional_verdict_if_yes/_if_no`, `what_would_resolve_it`). `EvidenceItem.provenance`
  (`agent` | `human_attested`) distinguishes a human's attested answer from agent-gathered evidence.
- **Wiring**: the detectors are called at the dispositive seams — the Phase-B disposition
  ABANDON seam (`phase_b.py`) and the Phase-C decline seam (`phase_c.py`). On a non-None
  result the case is parked at `NEEDS_HUMAN_CHECKPOINT`; `orchestrator.run()` exits **0**
  cleanly (no dossier) before `case_writer`/`_finalize`. `_reconcile_status_from_disk` +
  `_finalize` are checkpoint-aware, so an unresolved checkpoint never collapses to
  abandoned/complete.
- **Resume flow** (`scripts/answer_checkpoint.py`): `list` shows open checkpoints;
  `answer CASE --answer "…"` injects a `human_attested` `confirmed_fact` and marks the
  `ReviewRequest` resolved (carrying `resolution`, the human's direction). The rewind then
  depends on the checkpoint + direction: Phase-B ABANDON → drop the disposition + purge its
  cache; Phase-C `foreclosure_live_explanation` resolved `foreclose_explanation` (the human
  refuted the explanation) → clear the foreclosure ledger **and purge its on-disk step
  cache** so a resume genuinely re-derives it; resolved `affirm_decline` (the human confirmed
  the LIVE explanation still holds) → **preserve** the ledger + its cache instead — the
  ledger already records exactly what the human just endorsed, so the resume's
  `_run_foreclosure_check` resume-guard skips re-running the (non-deterministic) agent and
  `_finalize_phase_c_strength_score` commits `NOT_ACTIONABLE` off that same ledger. (Prior to
  this, both directions cleared + re-ran unconditionally — harmless for `foreclose_explanation`
  but a wasted, non-deterministic re-derivation for `affirm_decline` that could re-pause on
  the identical checkpoint instead of committing the human's decline.) Either way the command
  prints (or with `--resume` runs) `make investigate … RESUME=<case-id> SAFE_PERMS=1`. A
  read-only **Pending Review** panel in the console surfaces open `NEEDS_HUMAN_CHECKPOINT` cases.

## Guardrail 5 — Per-count precedent anchor  ✅ IMPLEMENTED (operationalizes the "precedent must be SIGNED" cardinal rule)
Every pled count must be a *theory of falsity* with precedent behind it — not a bare
statistic. Provider Birch post-mortem exposed that a count could be built on a 379× billing-ratio
outlier with **no check that such a count is pleadable**, which is exactly the data-mining
theory *Integra* dismisses.
- **Schema** (`LegalCount`): `precedent_anchor` (a prior case where a substantially-similar
  count/theory was pled and survived, or controlling doctrine) + `adverse_precedent_confronted`
  (the on-point adverse authority + why distinguishable).
- **`counsel_build`** must populate both per count; a statistical outlier is Rule 9(b)
  *corroboration*, not a count anchor.
- **Soft strength cap** (the maintainer chose cap-not-gate): `scoring.precedent_coverage()`
  matches each count's anchor against the VERIFIED set (`verified_precedents` ∪ doctrine-corpus
  controlling cases) by case-name signature — **not a trusted LLM flag**. Coverage drives a
  multiplier `PRECEDENT_CAP_FLOOR (0.70) + 0.30·coverage` applied to the strength score in
  `_finalize_phase_c_strength_score`; uncovered counts down-weight strength (and thus the
  verdict via the existing strength→FILE gate) but are still pled/visible. Tested
  (`fraud-investigator-v2/tests/test_precedent_cap.py`).
- **_Integra_ added to the corpus** (`doctrine_corpus.jsonl`, web-verified: *United States ex rel.
  Integra Med Analytics v. Baylor Scott & White Health*, 816 F. App'x 892 (5th Cir. 2020), cert.
  denied 2021) — so the builder/scorer reflect against the controlling adverse 5th-Cir authority,
  which was previously absent everywhere except the foreclosure prompt.
- Known gap for the rerun: we still lack a CCM-*specific* count-survival precedent in the corpus
  (closest is *Mackby*, incident-to); the investigator should pull + verify one off CourtListener.

## Cardinal rules — encode in agent prompts + scoring
The judgment rules behind the guardrails above, encoded into the `foreclosure_check` +
`defense` prompts. The **inaccessibility ≠ seal** rule is now also *enforced* — not just
advised — by the Guardrail-4 jurisdiction checkpoint (a sealed-case bar pauses rather than kills).
- **Inaccessibility ≠ seal.** "Can't access it in my free tools" is not evidence of a
  qui-tam seal (the *Dogwood* phantom). Only PACER's index marking it sealed counts. See
  `docs/COURT_RECORDS_LOOKUP.md`.
- **Clean LEIE / revocation is uninformative for a *novel-detection* target** — finding
  un-caught fraudsters is the premise; a clean exclusion record is expected, not a signal
  (either way). And it is **unrelated to Escobar materiality** (a claims-payment question).
  Scorer should ignore exclusion status for first-detection cases.
- **Precedent must be SIGNED.** Crediting "same-district precedent" +0.20 without checking
  whether it is *adverse* is a bug (e.g. *Integra* (5th Cir. 2020) is adverse, on-point,
  and should *subtract*). Score favorable vs. adverse separately.
- **Plausibility standard:** make fraud the *more plausible* inference and foreclose the
  obvious innocent explanations; do **not** chase a nonexistent "zero conceivable
  alternative" bar.

## Rerun gating
A clean rerun should be a **fresh full run** (not a resume of the error-baked state) and
should run on Guardrails 1–5. **Guardrails 1–5 are now implemented** (G1 authoritative
constants, G2 numeric audit, G3 foreclosure check, G4 human-in-the-loop checkpoint (PR #334),
G5 precedent anchor; the cardinal rules are encoded into the `foreclosure_check` + `defense`
prompts). Next: **rerun the Birch investigation on the corrected loop → verify `numeric_audit`
is all-MATCH and the Foreclosure Ledger's `large_clinical_staff_workforce_410_26` row is either
FORECLOSED (verified FTE count) or correctly capping the verdict.** A jurisdiction premise like
the *Dogwood* sealed-case bar now pauses at `NEEDS_HUMAN_CHECKPOINT` (answer + resume via
`scripts/answer_checkpoint.py`) rather than abandoning the case.
