"""Phase A (discover -> build -> first-chair review) + its floor. Mixin for Orchestrator."""
from __future__ import annotations

import logging
from datetime import datetime
from .agents import (
    counsel_build,
    counsel_first_chair_review,
    defense,
    investigator,
)
from .checkpoint import detect_iteration1_jurisdiction_screen
from .floors import compute_floor_audit
from .mock_defense import synthesize_rebuttal_from_defense
from .tier_lint import (
    format_code_identity_flags,
    format_tier_flags,
    format_unit_claim_flags,
    lint_code_identity_claims,
    lint_evidence_tiers,
    lint_unit_claim_semantics,
)
from .event_sink import (
    STAGE_INVESTIGATOR,
    EVENT_STAGE_LOG_LINE,
)
from .schemas import (
    CounselFirstChairReviewAction,
    FinalVerdict,
    IterationRecord,
)
from .state import (
    adv_dir,
    iter_dir,
    write_summary,
)

LOG = logging.getLogger("fraud_investigator.orchestrator")


class PhaseAMixin:

    # ────────────────────────────────────────────────────────────────────────
    # Phase A — Build loop (investigator + counsel_build + first_chair_review)
    # ────────────────────────────────────────────────────────────────────────

    def _run_phase_a(self) -> bool:
        """Run the Phase A build loop.

        Returns ``True`` if Phase A exited via PROCEED_TO_REBUTTAL (good case,
        proceed to Phase B). Returns ``False`` if it exited via ABANDON_CASE
        or hit the iteration cap without proceeding (in which case the state
        status is already set appropriately).
        """
        prior_review_dict: dict | None = None

        # ── Resume support — trim incomplete Phase A iters ──
        # A Phase A iter is "complete" only when ALL three sub-steps wrote
        # their parsed output: investigator, counsel_build, and
        # counsel_first_chair_review. Iters where the orchestrator died
        # mid-build (e.g., Juniper 2026-05-20: investigator succeeded but
        # counsel_build hung on a slow API and was killed) leave a
        # half-populated IterationRecord in state.iterations — counting
        # those as "complete" causes the next run to skip directly to the
        # following iter, silently abandoning the build/review work for
        # that iter. Fix: trim any trailing incomplete build iter so it
        # gets re-attempted (the file-level checkpoint will still reuse
        # the existing investigator.parsed.json on disk, so we only pay
        # for counsel_build + first_chair_review on the redo).
        before_trim = len(self.state.iterations)
        while self.state.iterations:
            last = self.state.iterations[-1]
            if last.phase != "build":
                break
            if (last.investigator is not None
                    and last.counsel_build is not None
                    and last.counsel_first_chair_review is not None):
                break   # last iter is complete; stop trimming
            LOG.info(
                f"Resume: trimming incomplete Phase A iter {last.iteration} "
                f"(inv={'Y' if last.investigator else '.'} "
                f"build={'Y' if last.counsel_build else '.'} "
                f"review={'Y' if last.counsel_first_chair_review else '.'}) "
                f"so it gets re-attempted with file-level checkpoint reuse."
            )
            self.state.iterations.pop()
        if before_trim != len(self.state.iterations):
            write_summary(self.state, self.state_root)

        # Count completed Phase A iterations (resume support) — after trim
        completed_phase_a = sum(1 for it in self.state.iterations if it.phase == "build")
        start_iter = completed_phase_a + 1

        # Carry the last Phase A first-chair review dict (resume support)
        last_phase_a = None
        if completed_phase_a > 0:
            last_phase_a = next((it for it in reversed(self.state.iterations)
                                 if it.phase == "build"), None)
            if last_phase_a and last_phase_a.counsel_first_chair_review:
                prior_review_dict = last_phase_a.counsel_first_chair_review.model_dump()

        # RESUME GUARD: if the last completed Phase A iter ALREADY decided
        # PROCEED_TO_REBUTTAL, go straight to Phase B. Without this, a resume
        # where all build iters are done (start_iter > cap) falls into the
        # empty `range(start_iter, cap+1)` loop, never re-sees the proceed
        # decision, and hits the post-loop "cap reached → max_iterations →
        # return False" trap — which skips Phase B + the entire adversarial
        # loop and writes an incomplete dossier. (Caught 2026-06-12 resuming
        # Maple, which crashed in Phase B counsel_rebuttal.)
        if (last_phase_a is not None
                and last_phase_a.counsel_first_chair_review is not None
                and last_phase_a.counsel_first_chair_review.action
                    == CounselFirstChairReviewAction.PROCEED_TO_REBUTTAL):
            LOG.info(
                "Resume: last Phase A iter already returned PROCEED_TO_REBUTTAL "
                "— skipping the (empty) build loop and proceeding to Phase B."
            )
            return True

        for it_num in range(start_iter, self.max_pre_rebuttal_iters + 1):
            LOG.info(f"=== Phase A — Iteration {it_num}/{self.max_pre_rebuttal_iters} ===")
            if self.event_sink is not None:
                self.event_sink.emit(
                    STAGE_INVESTIGATOR, EVENT_STAGE_LOG_LINE,
                    {"level": "info",
                     "msg": f"=== Phase A — Iteration {it_num}/{self.max_pre_rebuttal_iters} ===",
                     "iter": it_num, "max_iter": self.max_pre_rebuttal_iters,
                     "phase": "build"},
                )
            it_record = IterationRecord(iteration=it_num, timestamp=datetime.utcnow(),
                                        phase="build")
            d = iter_dir(self.state.case_id, it_num, self.state_root)

            if self.dry_run:
                LOG.info(f"[dry-run] would call investigator → counsel_build → "
                         f"counsel_first_chair_review for iter {it_num}")
                self.state.iterations.append(it_record)
                write_summary(self.state, self.state_root)
                continue

            # ── Step-0 deterministic evidence pack (E1) ──
            # Built ONCE per case (idempotent via file presence — resume-safe)
            # and injected into the discover + fresh-verify prompts. Mechanical
            # lookups (identity, PECOS target+co-billers, exclusions, Part B
            # profile, top codes, co-biller characterization) are pre-computed
            # so agent turns go to web work + thread-chasing. Calibration runs
            # get a panel-only, year-gated pack (live registries redacted).
            evidence_pack_md = self._evidence_pack_md()

            # ── Two-pass investigator (DISCOVER → VERIFY) ──
            # Pass A (DISCOVER): fires ONCE at Phase A iter-1 only. No checklist,
            # pure exploration. Outputs surprises + working_hypotheses + analysis_unit
            # that the VERIFY pass uses as priors.
            discover_out = None
            if it_num == 1 and prior_review_dict is None:
                self._stage("investigator_discover")
                LOG.info(
                    "=== Pass A — DISCOVER (open exploration) — fires once at iter-1 ==="
                )
                discover_out = investigator.run_discover(
                    subject=self.subject,
                    case_id=self.state.case_id,
                    out_dir=d,
                    evidence_pack=evidence_pack_md,
                )
                LOG.info(
                    f"  discover pass: {len(discover_out.evidence)} preliminary evidence items, "
                    f"{len(discover_out.surprises)} surprises, "
                    f"{len(discover_out.working_hypotheses)} working hypotheses, "
                    f"analysis_unit={discover_out.analysis_unit_chosen}"
                )

            # Pass B (VERIFY): the existing rigorous investigation. Session
            # continuation across iters: investigator retains tool-use memory +
            # evidence-ids it produced. Subsequent iters only get the
            # iter-specific delta + counsel disposition.
            prior_inv_calls = sum(
                1 for it in self.state.iterations if it.investigator is not None
            )
            prior_inv_evidence_ids = list(self.state.all_evidence_ids)
            # Scratchpad threading: prior iter's working_hypotheses + unexpected_absences
            # carry forward so this iter must explicitly pursue/retire each one.
            prior_inv_working_hypotheses, prior_inv_unexpected_absences = (
                self._collect_prior_investigator_threads()
            )
            # On iter-1, if discover ran, splice its surprises + hypotheses
            # into the verify pass's priors. The discover pass's working_hypotheses
            # take precedence over any inherited ones (it's the first iter).
            if discover_out is not None:
                if discover_out.working_hypotheses:
                    prior_inv_working_hypotheses = (
                        list(discover_out.working_hypotheses)
                        + prior_inv_working_hypotheses
                    )[:5]
                if discover_out.unexpected_absences:
                    prior_inv_unexpected_absences = (
                        list(discover_out.unexpected_absences)
                        + prior_inv_unexpected_absences
                    )[:3]
            self._stage("investigator")
            inv_out = investigator.run(
                osint_mandate=getattr(self, "_osint_mandate_pending", False),
                subject=self.subject,
                case_id=self.state.case_id,
                iteration=str(it_num),
                prior_counsel_disposition=prior_review_dict,
                existing_evidence=self._evidence_payload(),
                out_dir=d,
                prior_call_count=prior_inv_calls,
                prior_evidence_ids=prior_inv_evidence_ids,
                prior_working_hypotheses=prior_inv_working_hypotheses,
                prior_unexpected_absences=prior_inv_unexpected_absences,
                discover_surprises=(
                    list(discover_out.surprises) if discover_out else None
                ),
                discover_analysis_unit=(
                    discover_out.analysis_unit_chosen.value if discover_out else None
                ),
                evidence_pack=evidence_pack_md,
            )
            it_record.investigator = inv_out
            self._merge_human_context(inv_out)
            # OSINT contract (2026-08-18): the real-world floor. An empty
            # osint_findings on a PRODUCTION run means the investigator never
            # left the structured datasets — warn and force the next iteration
            # to complete the web scan (prose guidance alone demonstrably
            # loses to turn pressure; see CASE-6A842598-2026 citation census).
            if not getattr(inv_out, "osint_findings", None):
                LOG.warning(
                    "  ⚠ Guardrail-OSINT: investigator returned ZERO open-web "
                    "findings/attempts — the real-world scan (practice website, "
                    "reviews, state board) was skipped. Re-injecting the mandate "
                    "next iteration."
                )
                self._osint_mandate_pending = True
            else:
                self._osint_mandate_pending = False
            self._merge_evidence(inv_out.evidence)
            # Append immediately so subsequent counsel agents see this iter's
            # investigator evidence via _evidence_payload().
            self.state.iterations.append(it_record)
            LOG.info(f"  investigator: +{len(inv_out.evidence)} evidence items "
                     f"(total {len(self.state.all_evidence_ids)})")
            write_summary(self.state, self.state_root)

            # ── Counsel build ──
            # Session continuation: count prior counsel_build calls so the
            # agent driver can send a delta message (just the new evidence
            # + first-chair feedback) instead of re-injecting accumulated
            # context. First call gets the full prompt; iter-2+ use
            # claude -p --resume <session_id> with just the delta.
            prior_cb_calls = sum(
                1 for it in self.state.iterations[:-1]
                if it.counsel_build is not None
            )
            # Delta evidence = this-iter investigator's evidence only (NOT the
            # accumulated all_evidence — the model already has earlier iters'
            # evidence in session memory).
            this_iter_inv_evidence = (
                [ev.model_dump() for ev in inv_out.evidence] if inv_out else []
            )
            # Delta verified precedents = nothing new yet; counsel_build will
            # add to it. Leave empty.
            self._stage("counsel_build")
            # counsel_build gets the FULL evidence package (citation +
            # rationale + untruncated fact). It's the gatekeeper for all
            # downstream counsel_* agents — its curation of which evidence
            # anchors which count propagates through first_chair_review,
            # rebuttal, and disposition. Summary mode was justified by
            # "the agent can drill into parsed.json via Bash" but knob C
            # disabled Bash for counsel_build, so the escape hatch is
            # gone. Anchoring-bias concern (which keeps defense on
            # summary) doesn't apply here: counsel_build is on plaintiff's
            # team and benefits from the investigator's rationale.
            build_out = counsel_build.run(
                subject=self.subject,
                case_id=self.state.case_id,
                iteration=it_num,
                all_evidence=self._evidence_payload(full=True),
                prior_disposition=prior_review_dict,
                verified_precedents=list(self.state.verified_precedents),
                out_dir=d,
                prior_call_count=prior_cb_calls,
                new_evidence_since_last_call=this_iter_inv_evidence,
                verified_precedents_added_since_last_call=None,
            )
            it_record.counsel_build = build_out
            LOG.info(f"  counsel_build: strength={build_out.current_strength}, "
                     f"counts={len(build_out.counts_pled)} "
                     f"(call #{prior_cb_calls + 1}, "
                     f"{'continuation' if prior_cb_calls > 0 else 'fresh session'})")
            self._merge_verified_precedents(build_out.precedents)

            # ── Iter-1 blocking jurisdiction screen (failure_mode_taxonomy_2026-07.md #1) ──
            # first_to_file_concerns / public_disclosure_concerns are advisory-only in
            # counsel_build's schema and were never checked before — real runs (Spruce/
            # Warren, Birch/Dogwood) burned 3+ iterations discovering a PACER-discoverable prior
            # qui tam the hard way. Screen ONCE, at iter-1, before iteration 2 starts: a
            # confirmed-open docket routes to the existing Guardrail-4 checkpoint machinery
            # instead of continuing to burn iterations.
            if it_num == 1:
                already_resolved = (
                    self.state.review_request is not None
                    and self.state.review_request.resolved
                    and self.state.review_request.checkpoint_type == "jurisdiction_bar_unverifiable"
                )
                rr = None if already_resolved else detect_iteration1_jurisdiction_screen(
                    case_id=self.state.case_id,
                    first_to_file_concerns=build_out.first_to_file_concerns,
                    public_disclosure_concerns=build_out.public_disclosure_concerns,
                    subject=self.subject,
                )
                if rr is not None:
                    self.state.review_request = rr
                    self.state.status = "needs_human_checkpoint"
                    self.state.final_verdict = FinalVerdict.NEEDS_HUMAN_CHECKPOINT
                    write_summary(self.state, self.state_root)
                    LOG.warning(
                        f"⏸ HITL CHECKPOINT ({rr.checkpoint_type}) at Phase A iter-1 "
                        f"jurisdiction screen — pausing instead of burning further "
                        f"iterations. Q: {rr.question[:120]}…"
                    )
                    return False

            # ── Counsel first-chair review (Phase A judgement) ──
            # Session continuation: partner retains review history across iters.
            prior_fcr_calls = sum(
                1 for it in self.state.iterations[:-1]
                if it.counsel_first_chair_review is not None
            )
            self._stage("counsel_first_chair_review")
            # Mechanical floor audit (R2): slot arithmetic computed ONCE here
            # and handed to the gating agent — it adjudicates the semantic
            # residue (independence, refutation quality, anchor genuineness)
            # instead of re-counting evidence ids.
            floor_audit = compute_floor_audit(
                build_out.model_dump(),
                venue_token_found=self._venue_token_documented(inv_out),
            )
            # R3: mechanical tier-inflation flags ride the same audit block.
            floor_audit["tier_lint_flags"] = lint_evidence_tiers(
                self._evidence_payload(full=True))
            # Code-identity lint (failure_mode_taxonomy_2026-07.md #4): flags
            # HCPCS/drug-code identity claims that don't match hcpcs_lookup.py's
            # ground truth (the C4E2135E 64612/64615, E8A553FE J9299 class).
            floor_audit["code_identity_flags"] = lint_code_identity_claims(
                self._evidence_payload(full=True))
            # Unit-vs-claim semantics lint (the Alder 10,724 episode): additive
            # review flags on the same seam — never auto-discounts.
            floor_audit["unit_claim_flags"] = lint_unit_claim_semantics(
                self._evidence_payload(full=True))
            review_out = counsel_first_chair_review.run(
                subject=self.subject,
                case_id=self.state.case_id,
                iteration=it_num,
                counsel_build_output=build_out.model_dump(),
                prior_first_chair_review=prior_review_dict,
                max_pre_rebuttal_iters=self.max_pre_rebuttal_iters,
                out_dir=d,
                prior_call_count=prior_fcr_calls,
                floor_audit=floor_audit,
            )
            it_record.counsel_first_chair_review = review_out
            LOG.info(f"  counsel_first_chair_review: action={review_out.action} (model-asserted) "
                     f"[call #{prior_fcr_calls + 1}, "
                     f"{'continuation' if prior_fcr_calls > 0 else 'fresh session'}]")

            # Procedural floors on first_chair_review's PROCEED_TO_REBUTTAL
            review_out = self._enforce_first_chair_floors(
                review_out, build_out, inv_out, it_num)
            it_record.counsel_first_chair_review = review_out
            LOG.info(f"  counsel_first_chair_review: action={review_out.action} (post-floor)")

            write_summary(self.state, self.state_root)

            if review_out.action == CounselFirstChairReviewAction.PROCEED_TO_REBUTTAL:
                LOG.info(f"Phase A complete — proceeding to Phase B (rebuttal-closing)")
                return True
            if review_out.action == CounselFirstChairReviewAction.ABANDON_CASE:
                self.state.status = "abandoned"
                self.state.final_verdict = FinalVerdict.COUNSEL_ABANDONED
                LOG.info(f"CASE ABANDONED by first-chair review at iter-{it_num}")
                self._maybe_park_case(build_out, inv_out)
                return False

            prior_review_dict = review_out.model_dump()

        # Hit cap without PROCEED — force abandon (real firms don't loop forever)
        self.state.status = "max_iterations"
        LOG.warning(
            f"Phase A: max_pre_rebuttal_iters ({self.max_pre_rebuttal_iters}) "
            f"reached without PROCEED_TO_REBUTTAL or ABANDON_CASE"
        )
        return False


    def _has_complete_build(self) -> bool:
        """True if any Phase-A iteration produced a ``counsel_build`` — i.e., a
        usable case exists to fall back on. Lets a failed follow-up enrichment pass
        proceed to Phase B with the last good build (which Phase B selects via the
        last build-iter with a non-None counsel_build) rather than discarding the
        whole run. A partially-populated current iter (investigator-only, no build)
        does not satisfy this — we require a real counsel_build."""
        return any(
            it.phase == "build" and it.counsel_build is not None
            for it in self.state.iterations
        )


    # ────────────────────────────────────────────────────────────────────────
    # Phase B — Rebuttal-closing (single pass)
    # ────────────────────────────────────────────────────────────────────────

    def _run_defense_as_mock(self, phase_a_build, it_num: int):
        """PR7: fire the REAL defense round 1 during Phase B (into adv-r1/)
        and synthesize the CounselRebuttalOutput shape Phase B consumes.

        The motion's parsed.json lands exactly where the adversarial loop's
        round-1 defense call writes — so the loop later reloads it from the
        per-step cache (zero spawns) and proceeds with legal_adv_response
        onward against the REVISED build. Guardrail 2 (numeric audit) + the
        tier-lint flags are computed here so the early round 1 gets the same
        integrity inputs it would get inside the loop.
        """
        final_build_dict = phase_a_build.model_dump()
        numeric_audit = self._audit_numeric_predicates(
            final_build_dict.get("numeric_predicates", []))
        self.state.numeric_audit = numeric_audit
        tier_flags = lint_evidence_tiers(self._evidence_payload(full=True))
        code_identity_flags = lint_code_identity_claims(self._evidence_payload(full=True))
        unit_claim_flags = lint_unit_claim_semantics(self._evidence_payload(full=True))
        d1 = adv_dir(self.state.case_id, 1, self.state_root)
        self._stage("defense")
        LOG.info("=== Phase B mock = REAL defense round 1 (defense_as_mock) ===")
        motion = defense.run(
            subject=self.subject,
            case_id=self.state.case_id,
            round_num=1,
            counsel_final_build=final_build_dict,
            all_evidence=self._evidence_payload(full=False),
            prior_rounds_summary=[],
            verified_precedents=list(self.state.verified_precedents),
            out_dir=d1,
            prior_call_count=0,
            new_evidence_since_last_round=None,
            numeric_audit=numeric_audit,
            tier_lint_block=(format_tier_flags(tier_flags)
                             + format_code_identity_flags(code_identity_flags)
                             + format_unit_claim_flags(unit_claim_flags)),
        )
        # Deliberately NOT appended to state.adversarial_rounds — the loop
        # rebuilds round 1 from the cached parsed.json with correct
        # continuation alignment (prior_defense_calls counts records).
        return synthesize_rebuttal_from_defense(
            motion.model_dump(), case_id=self.state.case_id, iteration=it_num)


    # ────────────────────────────────────────────────────────────────────────
    # Phase A floors — first-chair review's PROCEED_TO_REBUTTAL gate
    # ────────────────────────────────────────────────────────────────────────

    def _enforce_first_chair_floors(self, review_out, build_out, inv_out, it_num: int):
        """Apply convergence + min-iter + venue-landscape + iteration-cap floors
        to a Phase A first-chair review. Refuses PROCEED_TO_REBUTTAL when any
        floor fails; downgrades to CONTINUE_BUILD with the gap surfaced as a
        follow-up question. Mirrors the disposition floors but checks the
        first-chair action enum.
        """
        from .schemas import CounselFirstChairReviewAction as A
        PROCEED = A.PROCEED_TO_REBUTTAL
        DROP = A.DROP_COUNTS_AND_CONTINUE
        CONTINUE = A.CONTINUE_BUILD
        ABANDON = A.ABANDON_CASE
        # At the iteration cap there is no "next iteration" to gather more, so the
        # PROCEED-gating floors below must NOT downgrade to CONTINUE_BUILD — that
        # dead-ends the case at max_iterations → NEEDS_HUMAN even when a viable
        # subset of counts exists. At the cap they instead proceed-with-a-warning
        # (venue) or drop the offending counts and proceed on survivors / abandon
        # (convergence), routing the thin-count call to mock-defense + disposition.
        at_cap = it_num >= self.max_pre_rebuttal_iters

        # Cap enforcement: at iter-MAX, CONTINUE_BUILD / DROP_COUNTS_AND_CONTINUE
        # are forbidden — must PROCEED or ABANDON. If the model picked CONTINUE
        # at the cap, downgrade to PROCEED_TO_REBUTTAL (forced exit). This
        # respects the user's "real firms don't loop forever" principle.
        if it_num >= self.max_pre_rebuttal_iters and review_out.action in (CONTINUE, DROP):
            LOG.warning(
                f"  ⚠ iteration-cap floor: model said {review_out.action.value} at "
                f"iter-{it_num} (cap = {self.max_pre_rebuttal_iters}); forcing "
                f"PROCEED_TO_REBUTTAL"
            )
            review_out.action = PROCEED
            review_out.reasoning = (
                f"[ITER-CAP FLOOR] {review_out.reasoning} | Phase A iteration cap "
                f"({self.max_pre_rebuttal_iters}) reached; forced exit to Phase B."
            )
            # fall through so we still check convergence/venue at this forced action

        # The remaining floors only constrain PROCEED_TO_REBUTTAL
        if review_out.action != PROCEED:
            return review_out

        # ── Min-iter floor — refuse PROCEED before iter-2 ──
        if it_num < self.MIN_ITERS_FOR_READY:
            LOG.warning(
                f"  ⚠ min-iter floor: model said PROCEED_TO_REBUTTAL at iter-{it_num} "
                f"but minimum is iter-{self.MIN_ITERS_FOR_READY}; downgrading to "
                f"CONTINUE_BUILD"
            )
            review_out.action = CONTINUE
            review_out.reasoning = (
                f"[MIN-ITER FLOOR] {review_out.reasoning} | Procedural downgrade: "
                f"PROCEED_TO_REBUTTAL fired at iter-{it_num}; minimum is "
                f"iter-{self.MIN_ITERS_FOR_READY}. Use iter-{it_num + 1} to deepen "
                f"precedent landscape and close innocent-explanation gaps."
            )
            existing = list(review_out.follow_up_questions_for_investigator or [])
            if not any(("VENUE_ENFORCEMENT_LANDSCAPE" in q) or ("venue-axis" in q.lower())
                       for q in existing):
                existing.insert(0, self._VENUE_SEARCH_FOLLOWUP)
            review_out.follow_up_questions_for_investigator = existing
            return review_out

        # ── Venue-landscape floor — refuse PROCEED without VENUE_ENFORCEMENT_LANDSCAPE ──
        TOKEN = "VENUE_ENFORCEMENT_LANDSCAPE"
        found = False
        for it in self.state.iterations:
            if it.investigator and TOKEN in (it.investigator.notes_for_counsel or ""):
                found = True
                break
        if not found and inv_out is not None:
            if TOKEN in (inv_out.notes_for_counsel or ""):
                found = True
        if not found and not at_cap:
            LOG.warning(
                f"  ⚠ venue-landscape floor: model said PROCEED_TO_REBUTTAL but no "
                f"{TOKEN}: section in any iteration's investigator notes; "
                f"downgrading to CONTINUE_BUILD"
            )
            review_out.action = CONTINUE
            review_out.reasoning = (
                f"[VENUE-LANDSCAPE FLOOR] {review_out.reasoning} | Procedural "
                f"downgrade: investigator notes_for_counsel does not document a "
                f"venue-axis precedent search (Step 8b). Same-district enforcement "
                f"is the strongest scienter fact at pleading stage."
            )
            existing = list(review_out.follow_up_questions_for_investigator or [])
            if not any((TOKEN in q) or ("venue-axis" in q.lower()) for q in existing):
                existing.insert(0, self._VENUE_SEARCH_FOLLOWUP)
            review_out.follow_up_questions_for_investigator = existing
            return review_out
        if not found and at_cap:
            LOG.warning(
                f"  ⚠ venue-landscape floor at iteration cap: no {TOKEN} documented, "
                f"but the cap is reached — proceeding to Phase B anyway (cannot loop). "
                f"The venue-axis gap carries into mock-defense as a flagged weakness."
            )
            review_out.reasoning = (
                f"[VENUE-LANDSCAPE FLOOR @ CAP] {review_out.reasoning} | Cap reached "
                f"without a documented venue-axis search; proceeding to rebuttal with "
                f"this as a flagged weakness rather than dead-ending."
            )
            # fall through to the convergence floor (do NOT return/downgrade)

        # CALIBRATION: worthwhile/economics floors OFF — skip the convergence/anchor
        # abandon so a banner-directed PROCEED runs to completion. The iteration-cap,
        # min-iter, and venue floors above still apply (thoroughness, not "worthwhile").
        if self.calibration_as_of:
            return review_out

        # ── Convergence floor — refuse PROCEED if retained counts don't clear it ──
        fb_by_count = {fb.count_id: fb for fb in (build_out.factual_basis_per_count or [])}
        retained_count_ids = [c.count_id for c in build_out.counts_pled
                              if c.count_id not in (review_out.counts_to_drop or [])]
        fails: list[tuple[str, list[str]]] = []
        for cid in retained_count_ids:
            fb = fb_by_count.get(cid)
            reasons: list[str] = []
            if fb is None:
                fails.append((cid, ["no factual_basis_per_count entry"]))
                continue
            if len(fb.extremity_evidence_ids) < 1:
                reasons.append("extremity_evidence_ids < 1")
            if len(fb.convergent_signal_evidence_ids) < 2:
                reasons.append(f"convergent_signal_evidence_ids = "
                               f"{len(fb.convergent_signal_evidence_ids)} (need ≥2)")
            refutation_len = len((fb.innocent_explanations_refuted_narrative or "").strip())
            if refutation_len < 40:
                reasons.append(f"innocent_explanations_refuted_narrative too short "
                               f"({refutation_len} chars)")
            if reasons:
                fails.append((cid, reasons))

        if fails and not at_cap:
            LOG.warning(
                f"  ⚠ convergence floor: model said PROCEED_TO_REBUTTAL but "
                f"{len(fails)} retained count(s) fail discrimination thresholds; "
                f"downgrading to CONTINUE_BUILD"
            )
            review_out.action = CONTINUE
            review_out.reasoning = (
                f"[CONVERGENCE FLOOR] {review_out.reasoning} | Procedural "
                f"downgrade: {len(fails)} count(s) fail the convergence floor "
                f"(extremity ≥1, convergent ≥2, refutation non-empty)."
            )
            gap_questions = []
            for cid, reasons in fails:
                for r in reasons:
                    gap_questions.append(f"[{cid}] {r} — investigate to close this gap")
            existing = list(review_out.follow_up_questions_for_investigator or [])
            review_out.follow_up_questions_for_investigator = existing + gap_questions
            return review_out

        if fails and at_cap:
            # Iteration cap: cannot loop to gather more. Drop the non-converging
            # count(s) and PROCEED on the survivors; ABANDON only if NONE survive.
            # This sends the thin-count decision to the proper downstream gates
            # (mock-defense + managing-partner disposition) instead of silently
            # dead-ending at max_iterations → NEEDS_HUMAN with the adversarial
            # phase never running.
            failing_ids = [cid for cid, _ in fails]
            review_out.counts_to_drop = list(
                dict.fromkeys((review_out.counts_to_drop or []) + failing_ids)
            )
            survivors = [cid for cid in retained_count_ids if cid not in failing_ids]
            if survivors:
                review_out.action = PROCEED
                review_out.reasoning = (
                    f"[CONVERGENCE FLOOR @ CAP] {review_out.reasoning} | Cap reached: "
                    f"dropped {len(failing_ids)} non-converging count(s) ({failing_ids}) "
                    f"and PROCEEDING to mock-defense on {len(survivors)} surviving "
                    f"count(s) ({survivors})."
                )
                LOG.warning(
                    f"  ⚠ convergence floor @ cap: dropped {len(failing_ids)} "
                    f"non-converging count(s) {failing_ids}; PROCEEDING on survivors "
                    f"{survivors}."
                )
            else:
                review_out.action = ABANDON
                review_out.reasoning = (
                    f"[CONVERGENCE FLOOR @ CAP] {review_out.reasoning} | Cap reached "
                    f"and NO count clears the convergence floor (extremity ≥1, "
                    f"convergent ≥2, refutation non-empty); abandoning rather than "
                    f"proceeding on counts that won't survive defense."
                )
                LOG.warning(
                    "  ⚠ convergence floor @ cap: NO count clears thresholds; "
                    "ABANDON_CASE."
                )
            return review_out

        return review_out

