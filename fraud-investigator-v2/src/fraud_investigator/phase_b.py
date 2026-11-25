"""Phase B (rebuttal -> disposition) + its progression floors. Mixin for Orchestrator."""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from .agents import (
    counsel_build,
    counsel_disposition,
    counsel_rebuttal,
    investigator,
)
from .checkpoint import detect_jurisdiction_checkpoint
from .floors import compute_floor_audit
from .tier_lint import (
    lint_code_identity_claims,
    lint_evidence_tiers,
    lint_unit_claim_semantics,
)
from .event_sink import (
    STAGE_INVESTIGATOR,
    EVENT_STAGE_LOG_LINE,
)
from .schemas import (
    CounselDispositionAction,
    FinalVerdict,
    IterationRecord,
)
from .state import (
    case_dir,
    iter_dir,
    write_summary,
)

LOG = logging.getLogger("fraud_investigator.orchestrator")


class PhaseBMixin:

    def _run_phase_b(self) -> None:
        """Run the Phase B rebuttal-closing single pass.

        Sequence: counsel_rebuttal → investigator (targeting weak_links) →
        counsel_build (final revision) → counsel_disposition. Phase B fires
        exactly once; if disposition returns NEED_MORE_INVESTIGATION (which
        is anomalous post-rebuttal), we coerce to ABANDON_CASE — Phase A had
        its chance.
        """
        # Resume: if a Phase B iter already exists with disposition, terminal
        existing_phase_b = next(
            (it for it in self.state.iterations if it.phase == "rebuttal_closing"), None
        )
        if existing_phase_b and existing_phase_b.counsel_disposition is not None:
            LOG.info("Phase B: already completed in prior run — skipping")
            return

        # The "counsel final build" from Phase A is the last Phase A iter's build
        last_phase_a = next(
            (it for it in reversed(self.state.iterations)
             if it.phase == "build" and it.counsel_build is not None), None
        )
        if last_phase_a is None:
            LOG.error("Phase B: no Phase A counsel_build found; cannot run rebuttal")
            return
        phase_a_build = last_phase_a.counsel_build

        # Create or reuse the Phase B iteration record
        if existing_phase_b is None:
            it_num = len(self.state.iterations) + 1
            it_record = IterationRecord(iteration=it_num, timestamp=datetime.utcnow(),
                                        phase="rebuttal_closing")
            self.state.iterations.append(it_record)
        else:
            it_record = existing_phase_b
            it_num = it_record.iteration
        d = iter_dir(self.state.case_id, it_num, self.state_root)

        LOG.info(f"=== Phase B — Rebuttal-closing iter-{it_num} ===")
        if self.event_sink is not None:
            self.event_sink.emit(
                STAGE_INVESTIGATOR, EVENT_STAGE_LOG_LINE,
                {"level": "info", "msg": f"=== Phase B — Rebuttal-closing iter-{it_num} ===",
                 "iter": it_num, "phase": "rebuttal_closing"},
            )

        if self.dry_run:
            LOG.info(f"[dry-run] would call counsel_rebuttal → investigator → "
                     f"counsel_build → counsel_disposition for Phase B iter-{it_num}")
            write_summary(self.state, self.state_root)
            return

        # ── (1) Mock-defense red team ──
        # Two modes (PR7): default = counsel_rebuttal (relator self-critique).
        # defense_as_mock = the REAL defense round 1, fired early into
        # adv-r1/ — the adversarial loop later replays the cached motion
        # (PR11 trim-and-replay) and proceeds with legal_adv_response onward,
        # so the defense budget is unchanged while Phase B's fix-up loop is
        # driven by the genuine attack set (better tools + numeric audit +
        # tier-lint flags than the self-critique has).
        if self.defense_as_mock:
            rebut_out = self._run_defense_as_mock(phase_a_build, it_num)
        else:
            self._stage("counsel_rebuttal")
            rebut_out = counsel_rebuttal.run(
                subject=self.subject,
                case_id=self.state.case_id,
                iteration=it_num,
                counsel_build_output=phase_a_build.model_dump(),
                verified_precedents=list(self.state.verified_precedents),
                out_dir=d,
            )
        it_record.counsel_rebuttal = rebut_out
        LOG.info(f"  {'defense-as-mock' if self.defense_as_mock else 'counsel_rebuttal'}: "
                 f"{len(rebut_out.weak_links)} weak links, "
                 f"survival={rebut_out.estimated_survival_likelihood:.0%}")
        write_summary(self.state, self.state_root)

        # ── (2) Investigator — targeted at rebuttal weak_links ──
        # Synthesize a prior_counsel_disposition-shaped dict so the investigator
        # prompt's `prior_counsel_disposition.follow_up_questions_for_investigator`
        # slot carries the rebuttal's investigator_questions.
        rebuttal_questions: list[str] = []
        for wl in rebut_out.weak_links:
            for q in (wl.investigator_questions or []):
                rebuttal_questions.append(f"[{wl.weakness_id}] {q}")
        synthetic_prior_disp = {
            "case_id": self.state.case_id,
            "iteration": it_num,
            "action": "NEED_MORE_INVESTIGATION",
            "reasoning": (
                "Phase B rebuttal-closing — counsel_rebuttal flagged "
                f"{len(rebut_out.weak_links)} weak link(s) that may survive defense. "
                f"Address each with targeted evidence."
            ),
            "counts_to_drop": [],
            "follow_up_questions_for_investigator": rebuttal_questions,
            "gaps_to_fill": [wl.description for wl in rebut_out.weak_links],
        }
        # Phase B investigator continues the SAME session as Phase A's
        # investigator (same session_id), so it remembers all prior tool
        # calls + evidence and only needs the rebuttal follow-up questions.
        prior_inv_calls = sum(
            1 for it in self.state.iterations[:-1] if it.investigator is not None
        )
        prior_inv_evidence_ids = list(self.state.all_evidence_ids)
        prior_inv_working_hypotheses, prior_inv_unexpected_absences = (
            self._collect_prior_investigator_threads()
        )
        self._stage("investigator")
        inv_out = investigator.run(
            subject=self.subject,
            case_id=self.state.case_id,
            iteration=str(it_num),
            prior_counsel_disposition=synthetic_prior_disp,
            existing_evidence=self._evidence_payload(),
            out_dir=d,
            prior_call_count=prior_inv_calls,
            prior_evidence_ids=prior_inv_evidence_ids,
            prior_working_hypotheses=prior_inv_working_hypotheses,
            prior_unexpected_absences=prior_inv_unexpected_absences,
        )
        it_record.investigator = inv_out
        self._merge_human_context(inv_out)
        self._merge_evidence(inv_out.evidence)
        LOG.info(f"  investigator (phase B): +{len(inv_out.evidence)} evidence items "
                 f"[call #{prior_inv_calls + 1}, "
                 f"{'continuation' if prior_inv_calls > 0 else 'fresh session'}]")
        write_summary(self.state, self.state_root)

        # ── (3) Counsel build — final revision ──
        # Phase B counsel_build is the SAME session_id as the Phase A
        # counsel_build calls. After 3 Phase A iters + this Phase B call,
        # the session has 4 turns of build history; the model retains its
        # case theory across all of them.
        prior_cb_calls = sum(
            1 for it in self.state.iterations[:-1]
            if it.counsel_build is not None
        )
        # Delta evidence = the Phase B investigator's targeted rebuttal evidence.
        phase_b_inv_evidence = (
            [ev.model_dump() for ev in inv_out.evidence] if inv_out else []
        )
        self._stage("counsel_build")
        # Same full-evidence policy as Phase A counsel_build — see
        # comment at the Phase A call site for rationale.
        build_out = counsel_build.run(
            subject=self.subject,
            case_id=self.state.case_id,
            iteration=it_num,
            all_evidence=self._evidence_payload(full=True),
            prior_disposition=synthetic_prior_disp,
            verified_precedents=list(self.state.verified_precedents),
            out_dir=d,
            prior_call_count=prior_cb_calls,
            new_evidence_since_last_call=phase_b_inv_evidence,
            verified_precedents_added_since_last_call=None,
        )
        it_record.counsel_build = build_out
        LOG.info(f"  counsel_build (phase B): strength={build_out.current_strength}, "
                 f"counts={len(build_out.counts_pled)} "
                 f"(call #{prior_cb_calls + 1}, "
                 f"{'continuation' if prior_cb_calls > 0 else 'fresh session'})")
        self._merge_verified_precedents(build_out.precedents)

        # ── (4) Counsel disposition — managing-partner sign-off ──
        self._stage("counsel_disposition")
        floor_audit = compute_floor_audit(
            build_out.model_dump(),
            venue_token_found=self._venue_token_documented(inv_out),
        )
        floor_audit["tier_lint_flags"] = lint_evidence_tiers(
            self._evidence_payload(full=True))
        # Code-identity lint (failure_mode_taxonomy_2026-07.md #4): same
        # evidence-emission seam as the tier-inflation lint above.
        floor_audit["code_identity_flags"] = lint_code_identity_claims(
            self._evidence_payload(full=True))
        # Unit-vs-claim semantics lint (the Alder 10,724 episode): additive
        # review flags on the same seam — sub-$5 avg-per-"claim" / J-code
        # counts may be UNITS, which muddies falsity + inflates per-claim
        # penalty exposure ~100x. Never auto-discounts.
        floor_audit["unit_claim_flags"] = lint_unit_claim_semantics(
            self._evidence_payload(full=True))
        # Guardrail-4 — on a resumed run, surface any human-review answer to the
        # disposition as an authoritative resolution so it does not re-abandon on a
        # premise the human already refuted.
        human_resolutions = None
        rr_state = self.state.review_request
        if rr_state is not None and rr_state.resolved and rr_state.human_answer:
            human_resolutions = [{"question": rr_state.question, "answer": rr_state.human_answer}]
        disp_out = counsel_disposition.run(
            subject=self.subject,
            case_id=self.state.case_id,
            iteration=it_num,
            counsel_build_output=build_out.model_dump(),
            counsel_rebuttal_output=rebut_out.model_dump(),
            out_dir=d,
            floor_audit=floor_audit,
            human_review_resolutions=human_resolutions,
        )
        it_record.counsel_disposition = disp_out
        LOG.info(f"  counsel_disposition: action={disp_out.action} (model-asserted)")

        # Floors — convergence + min-iter + venue-landscape.
        disp_out = self._enforce_convergence_floor(disp_out, build_out)
        disp_out = self._enforce_min_iterations_floor(disp_out, it_num)
        disp_out = self._enforce_venue_landscape_floor(disp_out, inv_out)
        # Upgrade 1 + 2 — compute provisional strength score + EVF (no
        # discount yet) and gate the disposition action.
        disp_out = self._enforce_strength_floor(
            disp_out, build_out, rebut_out, phase="B", cumulative_discount_pct=0.0,
        )
        # NEED_MORE_INVESTIGATION at Phase B is meaningless (Phase A had its
        # chance). Coerce to ABANDON_CASE — UNLESS the convergence + damages
        # floors are ACTUALLY met (failure_mode_taxonomy_2026-07.md #5:
        # CASE-6A3014F0 — the action space at the iteration cap has no way to
        # express READY_WITH_DEPENDENCIES when one non-gating count fails the
        # mechanical floor, even though the pled counts individually clear it
        # and damages vastly exceed the floor). In that case this is an
        # iteration-cap ACTION-SPACE limitation, not a merits decline — route
        # to PARKED (self.state.final_verdict set by the caller) instead of
        # forcing ABANDON_CASE on a filable case.
        if disp_out.action == CounselDispositionAction.NEED_MORE_INVESTIGATION:
            if self._floors_actually_met(build_out, disp_out.counts_to_drop):
                LOG.warning(
                    "  Phase B disposition returned NEED_MORE_INVESTIGATION, but "
                    "convergence + damages floors are met — parking instead of "
                    "abandoning (iteration-cap action-space limit, not a merits decline)"
                )
                disp_out.action = CounselDispositionAction.PARK_CASE
                disp_out.reasoning = (
                    "[PHASE-B PARK] " + (disp_out.reasoning or "") +
                    " | NEED_MORE_INVESTIGATION cannot fire post-rebuttal, but the "
                    "convergence + damages floors are met on the retained counts — "
                    "parked rather than abandoned; a human should review whether to "
                    "file on the current record or resume investigation."
                )
            else:
                LOG.warning(
                    "  Phase B disposition returned NEED_MORE_INVESTIGATION; "
                    "coercing to ABANDON_CASE (Phase A is closed)"
                )
                disp_out.action = CounselDispositionAction.ABANDON_CASE
                disp_out.reasoning = (
                    "[PHASE-B COERCION] " + (disp_out.reasoning or "") +
                    " | NEED_MORE_INVESTIGATION cannot fire post-rebuttal; case abandoned."
                )
        it_record.counsel_disposition = disp_out
        LOG.info(f"  counsel_disposition: action={disp_out.action} (post-floor)")

        write_summary(self.state, self.state_root)

        if disp_out.action in (
            CounselDispositionAction.DECLARE_CASE_READY,
            CounselDispositionAction.DECLARE_CASE_READY_WITH_DEPENDENCIES,
        ):
            self.state.status = "case_ready"
            LOG.info(f"CASE READY ({disp_out.action.value}) declared at Phase B iter-{it_num}")
            return
        if disp_out.action == CounselDispositionAction.PARK_CASE:
            # Iteration-cap action-space limitation, not a merits decline — the
            # convergence + damages floors ARE met (see _floors_actually_met).
            # Distinct terminal status from "abandoned" so the sunk build spend
            # is legible as filable-but-capped, not merits-declined.
            self.state.status = "parked"
            self.state.final_verdict = FinalVerdict.PARKED
            LOG.info(f"CASE PARKED at Phase B iter-{it_num} (floors met; "
                     f"iteration-cap action-space limit, not a merits decline)")
            write_summary(self.state, self.state_root)
            return
        # ABANDON path
        # Guardrail-4 (human-in-the-loop) — before finalizing the abandon, check
        # whether it rests on a DISPOSITIVE premise the loop cannot verify (e.g. a
        # sealed prior case asserted as a qui tam → first-to-file bar; the Birch/Dogwood
        # failure). If so, pause for a human answer instead of killing the case on a
        # phantom. The disposition stays recorded; the terminal mapping is skipped.
        already_resolved = (
            rr_state is not None and rr_state.resolved
            and rr_state.checkpoint_type == "jurisdiction_bar_unverifiable"
        )
        rr = None if already_resolved else detect_jurisdiction_checkpoint(
            case_id=self.state.case_id,
            evidence=self._evidence_payload(full=True),
            disposition_reasoning=disp_out.reasoning,
            provisional_verdict="COUNSEL_ABANDONED",
        )
        if rr is not None:
            self.state.review_request = rr
            self.state.status = "needs_human_checkpoint"
            self.state.final_verdict = FinalVerdict.NEEDS_HUMAN_CHECKPOINT
            write_summary(self.state, self.state_root)
            LOG.warning(
                f"⏸ HITL CHECKPOINT ({rr.checkpoint_type}) at Phase B disposition — "
                f"pausing instead of abandoning. Q: {rr.question[:120]}…"
            )
            return
        self.state.status = "abandoned"
        self.state.final_verdict = FinalVerdict.COUNSEL_ABANDONED
        LOG.info("CASE ABANDONED at Phase B disposition")
        self._maybe_park_case(build_out, inv_out)


    # ────────────────────────────────────────────────────────────────────────
    # Layer 4: Adversarial loop
    # ────────────────────────────────────────────────────────────────────────

    def _audit_numeric_predicates(self, predicates: list[dict]) -> list[dict]:
        """Guardrail 2 — deterministically re-derive each headline quantitative
        claim from canonical constants (scripts/audit_case_numerics.py), so the
        defense stage surfaces any inflated/wrong number as a dispositive defect.

        Pure arithmetic + canonical HCPCS lookups; runs in-process
        ORCHESTRATOR-side (repo paths resolved from this module's location, so
        a calibration run whose agent cwd is the out-of-repo export still
        reaches scripts/ + the hcpcs_lookup data). Never raises into the loop —
        on any failure it returns [] (the run continues; the audit is a safety
        net, not a gate). A MISMATCH is logged with a 🛑 and persisted to
        state.numeric_audit for the dossier + scoring.

        Whenever the audit executes (including the zero-predicate no-op) it
        also emits ``numeric_audit.json`` into the case dir — the standalone
        Guardrail-2 artifact (CALIBRATION-6DA0BBC3 had none: findings lived
        only inside summary.json's numeric_audit field, invisible as an
        artifact). Emission is write-only telemetry: the ONLY injection into
        agent context remains the existing defense-prompt numeric_audit /
        MISMATCH channel, and calibration agents cannot Read the state dir
        (export-mode Read allowlist is anchored to the export)."""
        if not predicates:
            self._write_numeric_audit_artifact([])
            return []
        try:
            import sys as _sys
            scripts_dir = str(Path(__file__).resolve().parents[3] / "scripts")
            if scripts_dir not in _sys.path:
                _sys.path.insert(0, scripts_dir)
            import audit_case_numerics as _audit
            findings = _audit.audit_predicates([dict(p) for p in predicates])
        except Exception as e:
            LOG.warning(f"Guardrail-2 numeric audit unavailable ({e}); continuing without it")
            return []
        n_mis = sum(f.get("status") == "MISMATCH" for f in findings)
        n_unv = sum(f.get("status") in ("UNVERIFIABLE", "UNVERIFIABLE_UNIT_SEMANTICS")
                    for f in findings)
        if n_mis:
            LOG.warning(
                f"🛑 Guardrail-2: {n_mis} numeric MISMATCH(es) re-deriving case claims "
                f"from canonical constants — injecting into defense as dispositive defects")
            for f in findings:
                if f.get("status") == "MISMATCH":
                    LOG.warning(f"   · {f['id']} ({f['kind']}): claimed={f['claimed']} "
                                f"recomputed={f['recomputed']} — {f['detail']}")
        else:
            LOG.info(f"Guardrail-2: {len(findings)} numeric predicate(s) audited "
                     f"({n_unv} unverifiable, {len(findings) - n_unv} match)")
        self._write_numeric_audit_artifact(findings)
        return findings


    def _write_numeric_audit_artifact(self, findings: list[dict]) -> None:
        """Persist the Guardrail-2 findings as ``<case dir>/numeric_audit.json``
        (sibling to citation_audit.json). Best-effort: a write failure logs and
        continues — the artifact is audit telemetry, never a gate. Re-runs
        (adversarial-loop trim-and-replay) rewrite it deterministically."""
        try:
            d = case_dir(self.state.case_id, self.state_root)
            d.mkdir(parents=True, exist_ok=True)
            status_counts: dict[str, int] = {}
            for f in findings:
                s = str(f.get("status"))
                status_counts[s] = status_counts.get(s, 0) + 1
            (d / "numeric_audit.json").write_text(json.dumps({
                "case_id": self.state.case_id,
                "generated_at": datetime.utcnow().isoformat() + "Z",
                "n_predicates": len(findings),
                "status_counts": status_counts,
                "findings": findings,
            }, indent=2))
            LOG.info(f"Guardrail-2: numeric_audit.json written "
                     f"({len(findings)} finding(s)) → {d / 'numeric_audit.json'}")
        except Exception as e:
            LOG.warning(f"Guardrail-2: could not write numeric_audit.json ({e}); continuing")


    def _floors_actually_met(self, build_out, counts_to_drop: list[str] | None) -> bool:
        """True iff at least one retained count clears the convergence
        discrimination thresholds (extremity/convergent/refutation — slot 3
        optional) AND aggregate trebled damages clear the $1M floor.

        Used ONLY at the Phase-B NEED_MORE_INVESTIGATION coercion seam
        (failure_mode_taxonomy_2026-07.md #5) to distinguish "the case is
        genuinely weak" from "the model's action space at the iteration cap
        has no legal way to express READY_WITH_DEPENDENCIES when ONE
        non-gating count (e.g. an info-and-belief AKS count) fails the
        mechanical floor" — the CASE-6A3014F0 artifact, where C1+C2 both
        individually clear the floor and $32.6M trebled damages vastly exceed
        $1M, yet the coercion forced ABANDON_CASE on the whole case. Mirrors
        the per-count classification in _enforce_convergence_floor without
        mutating disp_out — a read-only probe."""
        fb_by_count = {fb.count_id: fb for fb in (build_out.factual_basis_per_count or [])}
        retained_count_ids = [c.count_id for c in build_out.counts_pled
                              if c.count_id not in (counts_to_drop or [])]
        any_pass = False
        for cid in retained_count_ids:
            fb = fb_by_count.get(cid)
            if fb is None:
                continue
            if (len(fb.extremity_evidence_ids) >= 1
                    and len(fb.convergent_signal_evidence_ids) >= 2
                    and len((fb.innocent_explanations_refuted_narrative or "").strip()) >= 40):
                any_pass = True
                break
        if not any_pass:
            return False
        agg_damages = build_out.estimated_aggregate_damages_usd or 0.0
        return (agg_damages * 3) > 1_000_000

    def _enforce_convergence_floor(self, disp_out, build_out):
        """Mechanically downgrade READY-class actions if the build's
        factual_basis_per_count doesn't clear the discrimination thresholds.

        Floor for full READY:
          - extremity_evidence_ids        ≥ 1
          - convergent_signal_evidence_ids ≥ 2
          - slot 3: falsity_anchor_evidence_ids ≥ 1 OR
            regulatory_corroboration_evidence_ids ≥ 1 (F1 — a confirmed-fact
            falsity anchor grounds a count for novel-detection targets whose
            regulatory record is clean BY PREMISE; regulatory corroboration
            remains the alternative grounding + a scoring bonus)
          - innocent_explanations_refuted_narrative non-empty (≥ 40 chars)

        Floor for READY_WITH_DEPENDENCIES (slot 3 may be empty both ways):
          - extremity ≥ 1, convergent ≥ 2, refutation non-empty

        Anything below either floor → NEED_MORE_INVESTIGATION (do not silently
        declare ready). The model's gaps_to_fill / follow_up_questions are
        preserved; we only override the action + log the reason.
        """
        if self.calibration_as_of:
            return disp_out  # CALIBRATION: worthwhile/economics floors OFF (run to completion); see prompt banner
        READY = CounselDispositionAction.DECLARE_CASE_READY
        READY_DEP = CounselDispositionAction.DECLARE_CASE_READY_WITH_DEPENDENCIES
        if disp_out.action not in (READY, READY_DEP):
            return disp_out

        fb_by_count = {fb.count_id: fb for fb in (build_out.factual_basis_per_count or [])}
        retained_count_ids = [c.count_id for c in build_out.counts_pled
                              if c.count_id not in (disp_out.counts_to_drop or [])]

        passes_full: list[str] = []
        passes_dep: list[str] = []
        fails: list[tuple[str, list[str]]] = []  # (count_id, reasons)

        for cid in retained_count_ids:
            fb = fb_by_count.get(cid)
            reasons: list[str] = []
            if fb is None:
                fails.append((cid, ["no factual_basis_per_count entry"]))
                continue
            if len(fb.extremity_evidence_ids) < 1:
                reasons.append("extremity_evidence_ids < 1")
            if len(fb.convergent_signal_evidence_ids) < 2:
                reasons.append(f"convergent_signal_evidence_ids = {len(fb.convergent_signal_evidence_ids)} (need ≥2)")
            refutation_len = len((fb.innocent_explanations_refuted_narrative or "").strip())
            if refutation_len < 40:
                reasons.append(f"innocent_explanations_refuted_narrative too short ({refutation_len} chars)")
            # Slot 3 (F1): falsity anchor OR regulatory corroboration.
            anchor_n = len(getattr(fb, "falsity_anchor_evidence_ids", []) or [])
            regulatory_n = len(fb.regulatory_corroboration_evidence_ids)

            if reasons:
                fails.append((cid, reasons))
            elif anchor_n >= 1 or regulatory_n >= 1:
                passes_full.append(cid)
            else:
                passes_dep.append(cid)

        # Aggregate damages floor — FCA exposure is single damages × 3 (trebled)
        # plus per-claim civil penalties ($13,946-$27,894 in 2026). Cases below
        # ~$1M TOTAL EXPOSURE generally don't justify the cost of filing. We
        # compute floor on TREBLED damages (closest defensible proxy without
        # claim-count data); per-claim penalties on top push real exposure
        # higher but trebling is the conservative-but-defensible threshold.
        #
        # Bug fix 2026-05-21: previously this checked `agg_damages > 1_000_000`
        # against SINGLE damages, which auto-abandoned cases where the model
        # correctly estimated single damages ~$500K-$900K but total exposure
        # (trebled + penalties) was well above $1M. Provider Juniper opus rerun was
        # the canonical example — model said CASE_READY_WITH_DEPENDENCIES,
        # floor downgraded to NEED_MORE_INVESTIGATION, Phase-B coercion cascaded
        # to ABANDON_CASE. Floor should be on TOTAL EXPOSURE (closest available
        # proxy: trebled), not single damages.
        agg_damages = build_out.estimated_aggregate_damages_usd or 0.0
        trebled_damages = agg_damages * 3
        FLOOR_USD = 1_000_000

        # ── Drop-not-abandon: if SOME counts clear the floor but others fail,
        # DROP the failing counts and proceed on the grounded ones — do NOT sink
        # a filable case because one count lacks a factual basis. A firm drops a
        # speculative / deliberately discovery-held count (e.g. an info-and-belief
        # AKS count with no public falsity anchor) and files the strong counts; it
        # does not abandon C1+C2 because C3 is unground-able on public data. Only
        # abandon (below) when NO count clears the floor.
        # (2026-06-15: Bradley CASE-6A3014F0 — C1+C2 PASS_FULL, C3 had no
        # factual_basis entry → the whole case was coerced ABANDON → the
        # COUNSEL_ABANDONED-vs-FILE mislabel despite strength 0.91 / EVF 0.60.)
        if fails and (passes_full or passes_dep) and trebled_damages > FLOOR_USD:
            dropped = [cid for cid, _ in fails]
            disp_out.counts_to_drop = list(
                dict.fromkeys((disp_out.counts_to_drop or []) + dropped))
            reasons_str = "; ".join(f"{cid}: {', '.join(rs)}" for cid, rs in fails)
            disp_out.reasoning = (
                f"[FLOOR-DROP] {disp_out.reasoning} | Convergence floor dropped "
                f"{len(dropped)} below-floor count(s) ({reasons_str}); proceeding "
                f"on {len(passes_full) + len(passes_dep)} grounded count(s)."
            )
            LOG.warning(
                f"  ⚠ convergence-floor: dropped {dropped} (below floor); "
                f"{len(passes_full) + len(passes_dep)} count(s) clear it → "
                f"proceeding rather than abandoning the case"
            )
            # Failing counts are now dropped; re-evaluate the model's action on the
            # SURVIVORS below. We never UPGRADE the model's choice (a deliberate
            # READY_WITH_DEPENDENCIES stays that), only confirm/down-grade it.
            fails = []

        # Decide effective action
        if disp_out.action == READY:
            if passes_full and not passes_dep and not fails and trebled_damages > FLOOR_USD:
                return disp_out  # passes as declared
            if (passes_full or passes_dep) and not fails and trebled_damages > FLOOR_USD:
                LOG.warning(
                    f"  ⚠ convergence-floor downgrade: model said READY but "
                    f"{len(passes_dep)} count(s) only meet READY_WITH_DEPENDENCIES "
                    f"(no falsity anchor AND no regulatory corroboration)"
                )
                disp_out.action = READY_DEP
                disp_out.reasoning = (
                    f"[FLOOR-OVERRIDE] {disp_out.reasoning} | Convergence floor "
                    f"downgrade: {len(passes_dep)} count(s) have neither a "
                    f"falsity anchor nor regulatory corroboration (slot 3)."
                )
                return disp_out
            # Has failures or damages too low → NEED_MORE_INVESTIGATION
            return self._downgrade_to_more_investigation(disp_out, fails, agg_damages)

        if disp_out.action == READY_DEP:
            if (passes_full or passes_dep) and not fails and trebled_damages > FLOOR_USD:
                return disp_out  # passes
            return self._downgrade_to_more_investigation(disp_out, fails, agg_damages)

        return disp_out


    def _downgrade_to_more_investigation(self, disp_out, fails, agg_damages):
        """Helper: downgrade a READY-class action to NEED_MORE_INVESTIGATION
        with the convergence gaps surfaced as follow-up questions.

        Damages floor checks TREBLED damages (single × 3) against $1M, the
        closest defensible proxy for FCA total exposure absent claim-count
        data. See _enforce_strength_floor for the rationale.
        """
        from .schemas import CounselDispositionAction as A
        trebled_damages = agg_damages * 3
        FLOOR_USD = 1_000_000
        gap_questions = []
        for cid, reasons in fails:
            for r in reasons:
                gap_questions.append(f"[{cid}] {r} — investigate to close this gap")
        if trebled_damages <= FLOOR_USD:
            gap_questions.append(
                f"Aggregate single damages ${agg_damages:,.0f} → trebled "
                f"${trebled_damages:,.0f} ≤ $1M total-exposure floor — verify "
                f"damages calculation, find additional damaged claims, or "
                f"reconsider the case scope."
            )
        LOG.warning(
            f"  ⚠ convergence-floor downgrade: model said {disp_out.action.value} but "
            f"{len(fails)} count(s) fail floor; routing to NEED_MORE_INVESTIGATION"
        )
        disp_out.action = A.NEED_MORE_INVESTIGATION
        disp_out.reasoning = (
            f"[FLOOR-OVERRIDE] {disp_out.reasoning} | Convergence floor downgrade: "
            f"{len(fails)} count(s) fail the discrimination threshold."
        )
        disp_out.follow_up_questions_for_investigator = list(
            (disp_out.follow_up_questions_for_investigator or []) + gap_questions
        )
        return disp_out


    def _enforce_min_iterations_floor(self, disp_out, it_num: int):
        """Refuse DECLARE_CASE_READY* before ``MIN_ITERS_FOR_READY`` iterations.

        iter-1 surfaces identity + first-pass evidence; iter-2+ is when the
        investigator (a) answers counsel_disposition's follow-up questions,
        (b) deepens the venue-axis precedent search, and (c) closes innocent-
        explanation gaps the rebuttal flagged. Disposition firing READY after
        a single iteration consistently shipped under-supported scienter
        pleadings (e.g., May 20 Maple rerun missed the E.D. Mo. Kapok
        precedent because iter-1 only ran scheme-axis searches).
        """
        from .schemas import CounselDispositionAction as A
        if disp_out.action not in (A.DECLARE_CASE_READY, A.DECLARE_CASE_READY_WITH_DEPENDENCIES):
            return disp_out
        if it_num >= self.MIN_ITERS_FOR_READY:
            return disp_out
        LOG.warning(
            f"  ⚠ min-iterations floor: model said {disp_out.action.value} at "
            f"iter-{it_num} but minimum is iter-{self.MIN_ITERS_FOR_READY}; "
            f"downgrading to NEED_MORE_INVESTIGATION"
        )
        disp_out.action = A.NEED_MORE_INVESTIGATION
        disp_out.reasoning = (
            f"[MIN-ITER FLOOR] {disp_out.reasoning} | Procedural downgrade: "
            f"disposition fired at iter-{it_num}; minimum iterations before "
            f"READY is {self.MIN_ITERS_FOR_READY}. Use iter-{it_num + 1} to "
            f"deepen precedent landscape (venue-axis), close innocent-"
            f"explanation gaps, and verify any thin scienter facts."
        )
        existing = list(disp_out.follow_up_questions_for_investigator or [])
        if not any(("VENUE_ENFORCEMENT_LANDSCAPE" in q) or ("venue-axis" in q.lower())
                   for q in existing):
            existing.insert(0, self._VENUE_SEARCH_FOLLOWUP)
        disp_out.follow_up_questions_for_investigator = existing
        return disp_out


    def _enforce_venue_landscape_floor(self, disp_out, current_inv_out):
        """Refuse DECLARE_CASE_READY* if the investigator's notes_for_counsel
        does not contain a ``VENUE_ENFORCEMENT_LANDSCAPE:`` subsection.

        The investigator prompt (Step 8b + output-schema section) requires
        this token. Its absence indicates the venue-axis precedent search
        was skipped — the most common scienter pleading gap in qui tam
        complaints, particularly for FCA cases filed in districts with
        active healthcare-fraud enforcement (E.D. Mo., M.D. Fla., S.D.N.Y.).

        The search only needs to be on record ONCE per case (any iteration
        that produces the section satisfies the floor for subsequent
        iterations). We check every iteration's investigator notes plus the
        just-produced ``current_inv_out`` (which may not yet be in
        ``self.state.iterations`` when called).
        """
        from .schemas import CounselDispositionAction as A
        if disp_out.action not in (A.DECLARE_CASE_READY, A.DECLARE_CASE_READY_WITH_DEPENDENCIES):
            return disp_out
        TOKEN = "VENUE_ENFORCEMENT_LANDSCAPE"
        found = False
        for it in self.state.iterations:
            if it.investigator and TOKEN in (it.investigator.notes_for_counsel or ""):
                found = True
                break
        if not found and current_inv_out is not None:
            if TOKEN in (current_inv_out.notes_for_counsel or ""):
                found = True
        if found:
            return disp_out
        LOG.warning(
            f"  ⚠ venue-landscape floor: model said {disp_out.action.value} "
            f"but no {TOKEN}: section in any iteration's investigator notes; "
            f"downgrading to NEED_MORE_INVESTIGATION"
        )
        disp_out.action = A.NEED_MORE_INVESTIGATION
        disp_out.reasoning = (
            f"[VENUE-LANDSCAPE FLOOR] {disp_out.reasoning} | Procedural "
            f"downgrade: investigator notes_for_counsel does not document a "
            f"venue-axis precedent search (Step 8b). Same-district "
            f"enforcement is the strongest scienter fact available at "
            f"pleading stage; the search must be on record before READY can "
            f"fire."
        )
        existing = list(disp_out.follow_up_questions_for_investigator or [])
        if not any((TOKEN in q) or ("venue-axis" in q.lower()) for q in existing):
            existing.insert(0, self._VENUE_SEARCH_FOLLOWUP)
        disp_out.follow_up_questions_for_investigator = existing
        return disp_out

