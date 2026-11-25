"""Phase C (adversarial loop -> foreclosure -> strength/verdict finalize). Mixin for Orchestrator."""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from .agents import (
    defense,
    foreclosure_check,
    legal_adv_response,
    rebuttal_investigator,
)
from .tier_lint import (
    format_code_identity_flags,
    format_tier_flags,
    format_unit_claim_flags,
    lint_code_identity_claims,
    lint_evidence_tiers,
    lint_unit_claim_semantics,
)
from .checkpoint import detect_foreclosure_checkpoint, detect_jurisdiction_checkpoint
from .claude_p import ClaudePInvocationError
from . import scoring
from . import verdict
from .schemas import (
    AdversarialRoundRecord,
    FinalVerdict,
    SettlementVerdict,
)
from .state import (
    adv_dir,
    case_dir,
    write_summary,
)

LOG = logging.getLogger("fraud_investigator.orchestrator")


class PhaseCMixin:

    def _doctrine_corpus_cases(self) -> list[str]:
        """Controlling-case names from the FCA doctrine corpus (Guardrail 5 reads
        these alongside verified_precedents to confirm a count's precedent anchor).
        Fail-safe: returns [] if the corpus isn't present (e.g. calibration jail)."""
        try:
            import json as _json
            p = (Path(__file__).resolve().parents[3] / "data" / "ground_truth"
                 / "doctrine_corpus.jsonl")
            if not p.exists():
                return []
            out: list[str] = []
            for line in p.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                rec = _json.loads(line)
                cc = rec.get("controlling_case")
                if cc:
                    out.append(cc)
            return out
        except Exception as e:
            LOG.warning(f"Guardrail-5: could not load doctrine corpus ({e}); "
                        f"precedent coverage will use verified_precedents only")
            return []


    def _trim_adversarial_rounds_for_replay(self) -> int:
        """Resume guard for the adversarial loop (trim-and-replay).

        The loop has no per-round skip logic: on resume it restarts at round
        1 and APPENDS fresh round records, so persisted records from the
        crashed run would duplicate — corrupting ``prior_defense_calls``
        (continuation alignment), the scoring inputs, and the console
        timeline. Instead of skip-bookkeeping, we discard the persisted
        records and REPLAY the loop from round 1: every step whose
        ``parsed.json`` exists in its ``adv-rN/`` dir reloads from the
        per-step cache (zero agent spawns — both invoke paths have the
        cache-load primitive), records are rebuilt identically, and live
        execution resumes at the first step without a cached artifact.

        Returns the number of records trimmed (0 = fresh run, no-op).
        To force a from-scratch adversarial loop, delete the ``adv-r*/``
        dirs (the cache primitive's standard override).
        """
        n_prior = len(self.state.adversarial_rounds)
        if n_prior == 0:
            return 0
        LOG.info(
            f"Resume: trimming {n_prior} persisted adversarial round record(s); "
            f"replaying from round 1 via the per-step parsed.json cache "
            f"(completed steps reload instantly; live execution resumes at the "
            f"first uncached step). Delete adv-r*/ dirs to force a fresh loop."
        )
        self.state.adversarial_rounds = []
        write_summary(self.state, self.state_root)
        return n_prior


    def _run_adversarial_loop(self) -> None:
        # Resume guard — see _trim_adversarial_rounds_for_replay docstring.
        self._trim_adversarial_rounds_for_replay()

        # The "counsel final build" passed to defense is the last iteration's
        # counsel_build (which has counts, theory, Rule 9(b) elements).
        last_iter = self.state.iterations[-1]
        if not last_iter.counsel_build:
            LOG.error("No counsel_build at last iteration; cannot run defense.")
            return
        final_build_dict = last_iter.counsel_build.model_dump()

        # ── Guardrail 2: deterministic numeric-predicate audit ──
        # Re-derive every headline quantitative claim from canonical constants
        # BEFORE defense runs, so the defense stage surfaces any inflated/wrong
        # number (e.g. a 2x CPT-minute error) as a dispositive integrity defect.
        numeric_audit = self._audit_numeric_predicates(
            final_build_dict.get("numeric_predicates", []))
        self.state.numeric_audit = numeric_audit
        write_summary(self.state, self.state_root)

        prior_rounds_summary: list[dict] = []
        prior_round_reasoning = ""

        for r_num in range(1, self.max_adversarial + 1):
            LOG.info(f"=== Adversarial round {r_num}/{self.max_adversarial} ===")
            r_record = AdversarialRoundRecord(round=r_num, timestamp=datetime.utcnow())
            d = adv_dir(self.state.case_id, r_num, self.state_root)

            if self.dry_run:
                LOG.info(f"[dry-run] would call defense → legal_adv_response "
                         f"(→ rebuttal_investigator if PENDING_REBUTTAL) for adv-r{r_num}")
                self.state.adversarial_rounds.append(r_record)
                write_summary(self.state, self.state_root)
                continue

            # ── Defense ──
            # Summary mode for evidence — defense reasons about the case theory
            # in the counsel_final_build, not raw evidence per se. Session
            # continuation: defense retains its prior round's attacks in
            # memory; r2+ only gets the latest counsel build + plaintiff's
            # disposition of the prior attacks.
            prior_defense_calls = len(self.state.adversarial_rounds)
            # New evidence since last round = the rebuttal investigator's
            # output (if any) from the immediately-prior round
            new_evidence_since_last_round = []
            if (self.state.adversarial_rounds
                    and self.state.adversarial_rounds[-1].rebuttal_investigator):
                new_evidence_since_last_round = [
                    ev.model_dump() for ev in
                    self.state.adversarial_rounds[-1].rebuttal_investigator.evidence
                ]
            self._stage("defense")
            # R3: tier-inflation lint over the CURRENT evidence pool —
            # an over-tiered confirmed_fact is defense attack surface (the
            # relator's own admission), so the flags go to defense verbatim.
            tier_flags = lint_evidence_tiers(self._evidence_payload(full=True))
            if tier_flags:
                LOG.warning(f"  tier-lint: {len(tier_flags)} flagged confirmed_fact item(s)")
            # Code-identity lint (failure_mode_taxonomy_2026-07.md #4): same
            # evidence-emission seam as the tier-inflation lint above.
            code_identity_flags = lint_code_identity_claims(self._evidence_payload(full=True))
            if code_identity_flags:
                LOG.warning(f"  code-identity lint: {len(code_identity_flags)} flagged item(s)")
            # Unit-vs-claim semantics lint (the Alder 10,724 episode): additive
            # review flags on the same seam.
            unit_claim_flags = lint_unit_claim_semantics(self._evidence_payload(full=True))
            if unit_claim_flags:
                LOG.warning(f"  unit-claim lint: {len(unit_claim_flags)} flagged item(s)")
            defense_motion = defense.run(
                subject=self.subject,
                case_id=self.state.case_id,
                round_num=r_num,
                counsel_final_build=final_build_dict,
                all_evidence=self._evidence_payload(full=False),
                prior_rounds_summary=prior_rounds_summary,
                verified_precedents=list(self.state.verified_precedents),
                out_dir=d,
                prior_call_count=prior_defense_calls,
                new_evidence_since_last_round=new_evidence_since_last_round,
                numeric_audit=numeric_audit,
                tier_lint_block=(format_tier_flags(tier_flags)
                                 + format_code_identity_flags(code_identity_flags)
                                 + format_unit_claim_flags(unit_claim_flags)),
            )
            r_record.defense = defense_motion
            LOG.info(f"  defense: {len(defense_motion.attacks)} attacks, "
                     f"any_survives={defense_motion.any_attack_survives} "
                     f"[call #{prior_defense_calls + 1}, "
                     f"{'continuation' if prior_defense_calls > 0 else 'fresh session'}]")

            # Neutralize INHERENT-limitation attacks before scoring — ALWAYS (calibration
            # AND production). This tool surfaces fraud LEADS from public CMS data; it is
            # NOT itself the qui tam relator. The public-disclosure bar (factual basis is
            # public data + the relator isn't an original source) and the Rule 9(b)
            # representative-claim requirement (no claim-level data) are structural limits
            # that hit EVERY lead and carry no discriminating signal — assumed cured
            # downstream (an original-source relator + DOJ pre-suit production). Dropping
            # them keeps case-strength a measure of the fraud pattern, not of our data
            # access. Case-specific bars (first_to_file — a REAL prior qui tam) and every
            # merits attack (scienter, materiality, falsity, safe-harbor, SOL, damages)
            # are untouched.
            _inherent = ("public_disclosure_bar", "rule_9b_insufficient")
            _dropped = [a for a in defense_motion.attacks if a.type in _inherent]
            if _dropped:
                kept = [a for a in defense_motion.attacks if a.type not in _inherent]
                defense_motion.attacks = kept
                defense_motion.any_attack_survives = bool(kept)
                # If the inherent attacks were the ONLY survivors, the case has no
                # case-specific defect → seed retained counts from the build so the
                # "defense exhausted" path resolves SETTLEMENT_READY, not NOT_ACTIONABLE.
                if not kept and not self.state.counts_retained and last_iter.counsel_build:
                    self.state.counts_retained = [
                        c.count_id for c in (last_iter.counsel_build.counts_pled or [])
                    ]
                LOG.info(f"  dropped {len(_dropped)} inherent-limitation attack(s) "
                         f"{[str(a.type) for a in _dropped]} (public-disclosure / "
                         f"representative-claim assumed cured downstream); "
                         f"{len(kept)} case-specific attack(s) remain")

            if not defense_motion.any_attack_survives:
                # Defense exhausted
                self.state.adversarial_rounds.append(r_record)
                self.state.final_verdict = (
                    FinalVerdict.SETTLEMENT_READY if self.state.counts_retained
                    else FinalVerdict.NOT_ACTIONABLE
                )
                LOG.info(f"DEFENSE EXHAUSTED at round {r_num}; verdict {self.state.final_verdict}")
                write_summary(self.state, self.state_root)
                return

            # ── Legal adversarial response ──
            # Summary mode (same reasoning as defense — reasons about the build
            # + defense_motion JSON, not raw evidence text). Session
            # continuation: plaintiff's response counsel retains prior
            # dispositions in memory across rounds.
            prior_lar_calls = sum(
                1 for ar in self.state.adversarial_rounds
                if ar.legal_adv_response is not None
            )
            self._stage("legal_adv_response")
            legal_adv = legal_adv_response.run(
                subject=self.subject,
                case_id=self.state.case_id,
                round_num=r_num,
                counsel_final_build=final_build_dict,
                defense_motion=defense_motion.model_dump(),
                all_evidence=self._evidence_payload(full=False),
                prior_round_reasoning=prior_round_reasoning,
                verified_precedents=list(self.state.verified_precedents),
                out_dir=d,
                prior_call_count=prior_lar_calls,
            )
            r_record.legal_adv_response = legal_adv
            LOG.info(f"  legal_adv: verdict={legal_adv.settlement_verdict}, "
                     f"retained={len(legal_adv.counts_retained_this_round)}, "
                     f"abandoned={len(legal_adv.counts_abandoned_this_round)}, "
                     f"discount={legal_adv.cumulative_case_discount_pct}%")

            # Track retained/abandoned across rounds
            for c in legal_adv.counts_abandoned_this_round:
                if c not in self.state.counts_abandoned:
                    self.state.counts_abandoned.append(c)
            self.state.counts_retained = list(legal_adv.counts_retained_this_round)
            self.state.final_cumulative_discount_pct = legal_adv.cumulative_case_discount_pct
            self.state.final_settlement_target_usd = legal_adv.settlement_target_range_usd
            prior_round_reasoning = legal_adv.post_defense_reasoning

            # ── Possibly run rebuttal investigator ──
            if (legal_adv.settlement_verdict == SettlementVerdict.PENDING_REBUTTAL
                and legal_adv.follow_up_questions
                and r_num < self.max_adversarial):
                # Session continuation: rebuttal_investigator retains its prior
                # tool calls + evidence-ids across rounds.
                prior_ri_calls = sum(
                    1 for ar in self.state.adversarial_rounds
                    if ar.rebuttal_investigator is not None
                )
                prior_ri_evidence_ids = list(self.state.all_evidence_ids)
                self._stage("rebuttal_investigator")
                rebut_inv = rebuttal_investigator.run(
                    subject=self.subject,
                    case_id=self.state.case_id,
                    round_num=r_num,
                    follow_up_questions=list(legal_adv.follow_up_questions),
                    all_evidence=self._evidence_payload(),
                    out_dir=d,
                    prior_call_count=prior_ri_calls,
                    prior_evidence_ids=prior_ri_evidence_ids,
                )
                r_record.rebuttal_investigator = rebut_inv
                self._merge_evidence(rebut_inv.evidence)
                LOG.info(f"  rebuttal_investigator: +{len(rebut_inv.evidence)} evidence items "
                         f"[call #{prior_ri_calls + 1}, "
                         f"{'continuation' if prior_ri_calls > 0 else 'fresh session'}]")

            # Terminal checks
            if legal_adv.settlement_verdict == SettlementVerdict.NOT_ACTIONABLE:
                self.state.final_verdict = FinalVerdict.NOT_ACTIONABLE
                self.state.adversarial_rounds.append(r_record)
                LOG.info("NOT_ACTIONABLE; ending adversarial loop")
                write_summary(self.state, self.state_root)
                return

            # Update prior_rounds_summary for next round
            prior_rounds_summary.append({
                "round": r_num,
                "attack_ids": [a.attack_id for a in defense_motion.attacks],
                "attack_types": [a.type for a in defense_motion.attacks],
                "verdict": legal_adv.settlement_verdict,
                "counts_retained": list(legal_adv.counts_retained_this_round),
                "counts_abandoned": list(legal_adv.counts_abandoned_this_round),
            })

            self.state.adversarial_rounds.append(r_record)
            write_summary(self.state, self.state_root)

        # Hit cap
        if self.state.counts_retained:
            self.state.final_verdict = FinalVerdict.INDEFENSIBLE_AT_CAP
        else:
            self.state.final_verdict = FinalVerdict.PENDING_REBUTTAL_CAP
        LOG.info(f"Adversarial cap reached; verdict={self.state.final_verdict}")


    # ────────────────────────────────────────────────────────────────────────
    # Layer 5: Case writer
    # ────────────────────────────────────────────────────────────────────────

    def _run_foreclosure_check(self) -> None:
        """Guardrail 3 — run the Innocent-Explanation Foreclosure Check and
        persist the ledger to state. The deterministic cap (a LIVE high-
        plausibility innocent explanation blocks a clean FILE) is applied in
        _finalize_phase_c_strength_score, which runs next.

        Best-effort: a failure here must NOT erase a scored disposition, so it
        logs + returns rather than raising into run()."""
        if self.dry_run:
            LOG.info("[dry-run] would call foreclosure_check")
            return
        if self.state.foreclosure_ledger:
            # Resume guard: the ledger survived the crash on state — don't
            # re-run Guardrail 3 (the residual cap is recomputed from the
            # ledger rows in _finalize_phase_c_strength_score regardless).
            residual = scoring.foreclosure_residual(self.state.foreclosure_ledger)
            LOG.info(
                f"  foreclosure_check: ledger already on state (resume) — "
                f"skipping re-run; max-LIVE residual {residual:.2f}")
            return
        last_iter = next((it for it in reversed(self.state.iterations)
                          if it.counsel_build is not None), None)
        if last_iter is None:
            LOG.warning("  foreclosure_check: no counsel_build to test; skipping")
            return
        d = case_dir(self.state.case_id, self.state_root)
        self._stage("foreclosure_check")
        try:
            ledger = foreclosure_check.run(
                subject=self.subject,
                case_id=self.state.case_id,
                counsel_final_build=last_iter.counsel_build.model_dump(),
                all_evidence=self._evidence_payload(full=False),
                numeric_audit=self.state.numeric_audit,
                verified_precedents=list(self.state.verified_precedents),
                out_dir=d,
            )
        except ClaudePInvocationError as e:
            LOG.warning(f"  foreclosure_check failed ({e}); continuing without the ledger")
            return
        self.state.foreclosure_ledger = ledger.model_dump(mode="json")
        residual = scoring.foreclosure_residual(self.state.foreclosure_ledger)
        n_live = sum(1 for e in ledger.explanations if e.status == "LIVE")
        LOG.info(
            f"  foreclosure_check: {len(ledger.explanations)} explanation(s), {n_live} LIVE, "
            f"max-LIVE-plausibility={residual:.2f}, "
            f"fraud_more_plausible={ledger.fraud_is_more_plausible_inference}")
        if residual >= scoring.FORECLOSURE_LIVE_CAP:
            LOG.warning(
                f"  ⚠ Guardrail-3: a LIVE innocent explanation at plausibility {residual:.2f} "
                f"(≥{scoring.FORECLOSURE_LIVE_CAP}) is unrebutted on the record — flag for review. "
                f"The DECLINE gate is the boolean fraud_is_more_plausible_inference "
                f"(={ledger.fraud_is_more_plausible_inference}); a high residual alone does not "
                f"block filing — it surfaces in verdict predicate P2 (innocent_foreclosed).")
        write_summary(self.state, self.state_root)


    def _case_has_terminal_outcome(self) -> bool:
        """True if the case reached a real terminal state worth a dossier:
        case_ready/abandoned, OR any iteration recorded a counsel_disposition
        or a terminal first_chair action. Guards against writing a dossier for
        a case that merely crashed/exhausted Phase A on resume (status
        max_iterations/agent_error with nothing decided)."""
        if self.state.status in ("case_ready", "abandoned", "complete"):
            return True
        from .schemas import CounselFirstChairReviewAction as _A
        for it in self.state.iterations:
            if it.counsel_disposition is not None:
                return True
            fcr = it.counsel_first_chair_review
            if fcr is not None and fcr.action == _A.ABANDON_CASE:
                return True
        return False


    def _persist_strength_score(
        self, score: float, components: dict, bucket: str, evf: float,
    ) -> None:
        """Persist the computed score + bucket + EVF onto CaseState for
        downstream visibility (web UI, dossier, audit). Logged at INFO."""
        self.state.strength_score = score
        self.state.strength_bucket = bucket
        self.state.strength_components = components
        self.state.expected_value_factor = evf
        LOG.info(
            f"  strength_score={score:.3f} ({bucket}) | "
            f"EVF={evf:.3f} ({scoring.evf_recommendation(evf)})"
        )


    def _enforce_strength_floor(
        self,
        disp_out,
        build_out,
        rebut_out,
        *,
        phase: str,
        cumulative_discount_pct: float,
    ):
        """Production-only early-exit floor on the DISPOSITION ACTION — NOT the
        final verdict. `verdict.compute_verdict` (at finalize) owns file/decline;
        this only abandons/downgrades hopeless cases early to save the cost of the
        full loop, and is skipped entirely in calibration (the `calibration_as_of`
        short-circuit below). The strength score/EVF it computes are persisted as
        telemetry regardless.

        Apply Upgrades 1 + 2 — compute the strength score + EVF, downgrade
        the disposition action when the case fails to clear the bucket floor
        or the EVF floor.

        Gating logic (high false-negative tolerance — user-confirmed):
          score < THRESH_WEAK (0.18) → coerce to ABANDON_CASE
          score < THRESH_MODERATE (0.35) AND action == DECLARE_CASE_READY
            → downgrade to DECLARE_CASE_READY_WITH_DEPENDENCIES
          EVF < EVF_ABANDON_BELOW (0.20) → coerce to ABANDON_CASE
          EVF < EVF_FILE_WITH_DEPS_BELOW (0.40) AND action == DECLARE_CASE_READY
            → downgrade to DECLARE_CASE_READY_WITH_DEPENDENCIES

        Persists score/bucket/EVF onto CaseState regardless of action.
        """
        from .schemas import CounselDispositionAction as A

        # Extract features + score
        feats = scoring.extract_features_from_state(
            counsel_build=build_out,
            counsel_rebuttal=rebut_out,
            all_evidence=self._all_evidence_for_scoring(),
            investigator_notes_concat=self._concat_investigator_notes(),
            cumulative_discount_pct=cumulative_discount_pct,
        )
        score, components = scoring.compute_strength_score(**feats)
        case_bucket = scoring.bucket(score)
        evf = scoring.expected_value_factor(
            feats["survival_likelihood"], cumulative_discount_pct,
        )
        self._persist_strength_score(score, components, case_bucket, evf)

        if self.calibration_as_of:
            return disp_out  # CALIBRATION: keep the strength telemetry, skip the abandon/downgrade coercion

        # Skip the gate for action types that won't proceed to filing anyway
        if disp_out.action not in (A.DECLARE_CASE_READY, A.DECLARE_CASE_READY_WITH_DEPENDENCIES):
            return disp_out

        # ── Hard ABANDON triggers ──
        if score < scoring.THRESH_WEAK:
            LOG.warning(
                f"  ⚠ strength-floor [{phase}]: score={score:.3f} < THRESH_WEAK="
                f"{scoring.THRESH_WEAK} → coercing {disp_out.action.value} to ABANDON_CASE"
            )
            disp_out.action = A.ABANDON_CASE
            disp_out.reasoning = (
                f"[STRENGTH-FLOOR {phase}] {disp_out.reasoning} | Procedural "
                f"abandon: computed case-strength {score:.3f} < {scoring.THRESH_WEAK} "
                f"(WEAK threshold). Components: {components}."
            )
            return disp_out
        if evf < scoring.EVF_ABANDON_BELOW:
            LOG.warning(
                f"  ⚠ EVF-gate [{phase}]: EVF={evf:.3f} < {scoring.EVF_ABANDON_BELOW} → "
                f"coercing {disp_out.action.value} to ABANDON_CASE"
            )
            disp_out.action = A.ABANDON_CASE
            disp_out.reasoning = (
                f"[EVF-GATE {phase}] {disp_out.reasoning} | Procedural abandon: "
                f"expected-value factor {evf:.3f} < {scoring.EVF_ABANDON_BELOW} "
                f"(survival={feats['survival_likelihood']:.2f}, "
                f"discount={cumulative_discount_pct:.0f}%)."
            )
            return disp_out

        # ── Soft downgrades to FILE_WITH_DEPS ──
        if disp_out.action == A.DECLARE_CASE_READY:
            if score < scoring.THRESH_MODERATE:
                LOG.warning(
                    f"  ⚠ strength-floor [{phase}]: score={score:.3f} < THRESH_MODERATE="
                    f"{scoring.THRESH_MODERATE} → downgrading DECLARE_CASE_READY to "
                    f"DECLARE_CASE_READY_WITH_DEPENDENCIES"
                )
                disp_out.action = A.DECLARE_CASE_READY_WITH_DEPENDENCIES
                disp_out.reasoning = (
                    f"[STRENGTH-FLOOR {phase}] {disp_out.reasoning} | Procedural "
                    f"downgrade: strength {score:.3f} ({case_bucket}) below "
                    f"{scoring.THRESH_MODERATE} requires discovery-stage cures."
                )
            elif evf < scoring.EVF_FILE_WITH_DEPS_BELOW:
                LOG.warning(
                    f"  ⚠ EVF-gate [{phase}]: EVF={evf:.3f} < "
                    f"{scoring.EVF_FILE_WITH_DEPS_BELOW} → downgrading "
                    f"DECLARE_CASE_READY to DECLARE_CASE_READY_WITH_DEPENDENCIES"
                )
                disp_out.action = A.DECLARE_CASE_READY_WITH_DEPENDENCIES
                disp_out.reasoning = (
                    f"[EVF-GATE {phase}] {disp_out.reasoning} | Procedural "
                    f"downgrade: EVF {evf:.3f} requires discovery-stage cures."
                )

        return disp_out


    def _finalize_phase_c_strength_score(self) -> None:
        """Recompute the strength score + EVF after the adversarial loop has
        set ``final_cumulative_discount_pct``. If the post-discount EVF falls
        below the abandon threshold, override the FinalVerdict to
        NOT_ACTIONABLE so the case writer terminates cleanly instead of
        producing a dossier the user shouldn't file.
        """
        # Find the Phase B counsel_build + rebuttal (last iter with phase="rebuttal_closing"
        # OR the last iter overall if no Phase B record exists — back-compat)
        phase_b_iter = next(
            (it for it in reversed(self.state.iterations)
             if it.phase == "rebuttal_closing" and it.counsel_build is not None),
            None,
        )
        if phase_b_iter is None:
            phase_b_iter = next(
                (it for it in reversed(self.state.iterations) if it.counsel_build is not None),
                None,
            )
        if phase_b_iter is None or phase_b_iter.counsel_build is None:
            LOG.warning("  finalize_phase_c_strength_score: no counsel_build to score; skipping")
            return

        build_out = phase_b_iter.counsel_build
        rebut_out = phase_b_iter.counsel_rebuttal  # may be None for legacy/edge cases
        discount = float(self.state.final_cumulative_discount_pct or 0)

        feats = scoring.extract_features_from_state(
            counsel_build=build_out,
            counsel_rebuttal=rebut_out,
            all_evidence=self._all_evidence_for_scoring(),
            investigator_notes_concat=self._concat_investigator_notes(),
            cumulative_discount_pct=discount,
        )
        score, components = scoring.compute_strength_score(**feats)

        # ── Guardrail 5: per-count precedent-anchor coverage (soft cap) ──
        # A count whose precedent_anchor can't be matched to a VERIFIED precedent
        # (verified_precedents ∪ doctrine-corpus controlling cases) is still pled
        # but doesn't count as confirmed — it multiplies the strength down toward
        # PRECEDENT_CAP_FLOOR. Not a verdict flip; the strength→verdict gate below
        # carries any downstream effect.
        verified_names = list(self.state.verified_precedents) + self._doctrine_corpus_cases()
        cov = scoring.precedent_coverage(build_out.counts_pled, verified_names)
        pre_cap = score
        score *= scoring.precedent_strength_factor(cov["coverage"])
        components["precedent_coverage"] = round(cov["coverage"], 3)
        if cov["coverage"] < 1.0 and cov["n_counts"]:
            LOG.warning(
                f"  ⚠ Guardrail-5 precedent cap: {cov['n_covered']}/{cov['n_counts']} "
                f"counts have a verified precedent anchor ({cov['n_adverse_unconfronted']} "
                f"with no adverse precedent confronted) → strength {pre_cap:.3f} → {score:.3f}")

        case_bucket = scoring.bucket(score)
        evf = scoring.expected_value_factor(feats["survival_likelihood"], discount)
        self._persist_strength_score(score, components, case_bucket, evf)

        # ── Verdict: the single legal-predicate filing test (verdict.compute_verdict) ──
        # Replaces the old scattered ladder (cap-reconcile + foreclosure cap/decline
        # + EVF gate). The orchestrator builds the Scorecard from the SAME serialized
        # state the replay oracle reads (summary.json == state.model_dump_json()), so
        # the live decision and the millisecond oracle are identical by construction.
        # strength_score / strength_bucket are now REPORTING-only; EVF (persisted
        # above) only sets the SETTLEMENT_READY-vs-FILE_WITH_DISCOVERY_DEPS sub-tier.
        sc = verdict.build_scorecard(
            json.loads(self.state.model_dump_json()),
            calibration=bool(self.calibration_as_of),
        )
        decision = verdict.compute_verdict(sc)
        # Guardrail-4 (human-in-the-loop) — if a DECLINE rests on a premise the loop
        # cannot verify (a sealed-case jurisdiction bar, or a LIVE innocent explanation
        # closable only by an off-record fact), pause for a human instead of declining.
        rr = self._human_checkpoint_for_decline(decision)
        if rr is not None:
            self.state.review_request = rr
            self.state.final_verdict = FinalVerdict.NEEDS_HUMAN_CHECKPOINT
            self.state.status = "needs_human_checkpoint"
            self.state.verdict_rationale = (
                f"Paused for human review ({rr.checkpoint_type}): {rr.question[:160]}"
            )
            write_summary(self.state, self.state_root)
            LOG.warning(
                f"⏸ HITL CHECKPOINT ({rr.checkpoint_type}) at Phase C — pausing "
                f"instead of {decision.verdict.value}."
            )
            return
        self.state.final_verdict = decision.verdict
        self.state.status = decision.status
        self.state.verdict_rationale = decision.rationale
        self.state.verdict_predicates = [
            {"name": p.name, "status": p.status, "why": p.rationale}
            for p in decision.predicates
        ]
        LOG.info(f"  ⚖ verdict: {decision.verdict.value} — {decision.rationale}")
        write_summary(self.state, self.state_root)

    def _human_checkpoint_for_decline(self, decision):
        """Guardrail-4 — for a DECLINE-class verdict, return a ReviewRequest if the
        decline rests on a premise the loop cannot verify (a human should answer
        before the case is killed), else None.

        Two cases: a P6 jurisdiction bar resting on a sealed/unverifiable prior case
        (defense-in-depth — the primary jurisdiction seam is Phase B), and a P2
        foreclosure decline whose LIVE innocent explanation is closable only by an
        off-record fact a human could supply. FILE-class verdicts never pause.
        """
        if decision.verdict not in (
            FinalVerdict.NOT_ACTIONABLE,
            FinalVerdict.COUNSEL_ABANDONED,
            FinalVerdict.INDEFENSIBLE_AT_CAP,
        ):
            return None
        # Guardrail-4 — once a human has answered a checkpoint of a given type, trust
        # the answer + the re-run; do not pause again on the same premise (no loop).
        # Test the full SET of answered types, not just the latest review_request:
        # a case failing both predicates raises two checkpoints, and the second
        # overwrites the first, so a latest-only guard let the pair ping-pong
        # indefinitely (jurisdiction → foreclosure → jurisdiction → …).
        resolved_types = set(self.state.resolved_checkpoint_types or ())
        rr_state = self.state.review_request
        if rr_state is not None and rr_state.resolved:
            resolved_types.add(rr_state.checkpoint_type)   # in-flight cases predating the list
        failed = {p.name for p in decision.predicates if p.status == "FAILED"}
        if "jurisdiction_clear" in failed and "jurisdiction_bar_unverifiable" not in resolved_types:
            rr = detect_jurisdiction_checkpoint(
                case_id=self.state.case_id,
                evidence=self._evidence_payload(full=True),
                disposition_reasoning=self._dispositive_jurisdiction_reasoning(),
                jurisdiction_barred=True,
                provisional_verdict=decision.verdict.value,
            )
            if rr is not None:
                return rr
        if "innocent_foreclosed" in failed and "foreclosure_live_explanation" not in resolved_types:
            rr = detect_foreclosure_checkpoint(
                case_id=self.state.case_id,
                foreclosure_ledger=self.state.foreclosure_ledger,
                provisional_verdict=decision.verdict.value,
            )
            if rr is not None:
                return rr
        return None

    def _dispositive_jurisdiction_reasoning(self) -> str:
        """Concatenate the reasoning of every DISPOSITIVE_NOT_SURVIVED adversarial
        disposition, so the jurisdiction checkpoint detector can see a sealed-prior-case
        premise (nature OR scope) even when legal_adv left attack_type unset — the
        Spruce/Warren case, where a real first-to-file bar carried attack_type=None."""
        parts: list[str] = []
        for r in (self.state.adversarial_rounds or []):
            la = getattr(r, "legal_adv_response", None)
            if la is None:
                continue
            try:
                lad = la.model_dump()
            except Exception:
                continue
            for a in (lad.get("attack_dispositions") or []):
                if "DISPOSITIVE_NOT_SURVIVED" in str(a.get("disposition") or "") and a.get("reasoning"):
                    parts.append(a["reasoning"])
        return " ".join(parts)

