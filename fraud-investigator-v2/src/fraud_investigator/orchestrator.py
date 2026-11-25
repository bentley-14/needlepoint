"""Main loop: Layer 3 (Investigator ↔ Counsel{build, rebuttal, disposition}) →
Layer 4 (Defense ↔ Legal-Adv ↔ Rebuttal-Investigator) → Layer 5 (CaseWriter).

The orchestrator holds all state in Python; each agent step is a one-shot
``claude -p`` subprocess with naive context (only sees the rendered prompt).
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import date as _date, datetime
from pathlib import Path
import uuid as _uuid

from . import calibration as _calibration
from .evidence_pack import ensure_evidence_pack, extract_npi
from .floors import compute_floor_audit
from .human_context import new_human_evidence
from .claude_p import ClaudePHardStopError, ClaudePInvocationError, set_default_cwd, set_event_sink
from .event_sink import (
    EventSink,
    STAGE_INVESTIGATOR,
    EVENT_STAGE_COMPLETE,
    EVENT_STAGE_FAILED,
    EVENT_STAGE_START,
)
from .schemas import (
    CaseState,
    CounselDispositionAction,
    CounselFirstChairReviewAction,
    FinalVerdict,
)
from .state import (
    case_dir,
    load_summary,
    write_summary,
    write_transcript,
    build_transcript_from_disk,
    STATE_ROOT_DEFAULT,
)

LOG = logging.getLogger("fraud_investigator.orchestrator")

DEFAULT_MAX_ITERATIONS = 10
DEFAULT_MAX_ADVERSARIAL = 4

# Phase A (build) is capped — at this many iterations, counsel_first_chair_review
# MUST choose either PROCEED_TO_REBUTTAL or ABANDON_CASE; CONTINUE_BUILD is
# forbidden. Real firms don't loop indefinitely on "almost there" cases; they
# either send to mock-defense or kill it. 3 is the sweet spot: enough depth for
# the venue-axis precedent search + innocent-explanation refutation, but tight
# enough that "build forever" isn't a failure mode.
DEFAULT_MAX_PRE_REBUTTAL_ITERS = 3


from .phase_a import PhaseAMixin
from .phase_b import PhaseBMixin
from .phase_c import PhaseCMixin
from .dossier import DossierMixin


class Orchestrator(PhaseAMixin, PhaseBMixin, PhaseCMixin, DossierMixin):
    def __init__(
        self,
        *,
        subject: str,
        case_id: str | None = None,
        max_iterations: int = DEFAULT_MAX_ITERATIONS,
        max_adversarial: int = DEFAULT_MAX_ADVERSARIAL,
        max_pre_rebuttal_iters: int = DEFAULT_MAX_PRE_REBUTTAL_ITERS,
        state_dir: Path | None = None,
        working_dir: Path | None = None,
        dry_run: bool = False,
        event_sink: EventSink | None = None,
        calibration_as_of: str | None = None,
        calibration_export: "Path | None" = None,
        defense_as_mock: "bool | None" = None,
    ) -> None:
        self.subject = subject
        self.max_iterations = max_iterations
        self.max_adversarial = max_adversarial
        self.max_pre_rebuttal_iters = max_pre_rebuttal_iters
        # CALIBRATION mode: route state under <root>/calibration/ so calibration
        # runs are cleanly separated from production case states (they live in
        # sibling dirs; the agent's Read allowlist is anchored to ONE run dir,
        # so cross-case state leakage is impossible regardless).
        self.calibration_as_of = calibration_as_of
        # Leak-safe Medicaid gate: export the cutoff so EVERY Medicaid tool
        # subprocess (medicaid_lookup / entity_lookup / medicaid_preflight,
        # invoked by the Step-0 pack AND by the agent mid-run) restricts its
        # T-MSIS/feature reads to strictly before the cutoff — the all-payer
        # analog of the Part-B clean export (fraudlib/medicaid_cutoff.py). Inert
        # in production (no calibration_as_of => unset).
        if calibration_as_of:
            os.environ["FRAUD_INV_MEDICAID_CUTOFF"] = calibration_as_of
        # PR7 (flag-gated, default OFF): replace counsel_rebuttal with the
        # REAL defense round 1 fired early during Phase B. The motion lands in
        # adv-r1/ where the adversarial loop later replays it from the
        # per-step cache (PR11 trim-and-replay) and proceeds with
        # legal_adv_response onward — same defense budget, one fewer Opus
        # stage, and the attacks driving the pre-file fix-up are the better
        # attacks (Cypress finding: the binding constraint is defense
        # quality). Enable per-run via env FRAUD_INV_DEFENSE_AS_MOCK=1 or the
        # --defense-as-mock CLI flag. A/B on calibration cases before
        # flipping the default.
        # FRAUD_INV_PROFILE=battery flips the DEFAULT to ON (an un-red-teamed
        # FILE is unvalidated — the battery must exercise the REAL defense as
        # the Phase-B red team, not the counsel_rebuttal self-critique). An
        # explicit CLI flag or FRAUD_INV_DEFENSE_AS_MOCK env still wins;
        # production / draft defaults are unchanged (OFF).
        if defense_as_mock is None:
            env_flag = os.environ.get("FRAUD_INV_DEFENSE_AS_MOCK")
            if env_flag is not None:
                defense_as_mock = env_flag == "1"
            else:
                defense_as_mock = (
                    os.environ.get("FRAUD_INV_PROFILE", "").strip().lower() == "battery"
                )
        self.defense_as_mock = bool(defense_as_mock)
        # Data-rich clean-export mode (optional): a fail-closed export dir holding only
        # pre-cutoff panels + answer-key-free tools. Requires --calibration-as-of.
        # Calibration runs in exactly ONE mode: the fail-closed clean export
        # (pre-cutoff panels + answer-key-free tools, out of repo). --calibration-as-of
        # and --calibration-export are required together; the empty-sandbox /
        # Wayback-only mode has been removed to keep a single calibration setup.
        self.calibration_export = Path(calibration_export).resolve() if calibration_export else None
        if calibration_export and not calibration_as_of:
            raise ValueError("--calibration-export requires --calibration-as-of")
        if calibration_as_of and not calibration_export:
            raise ValueError(
                "--calibration-as-of requires --calibration-export (the fail-closed clean export). "
                "Easiest path: scripts/calibration/run_calibration_smoke.sh <NPI> <YYYY-MM-DD> \"<SUBJECT>\" — it "
                "builds the export and runs the loop. Or build it yourself: "
                "python3 scripts/calibration/build_calibration_workspace.py --npi <NPI> --as-of "
                f"{calibration_as_of} --with-panels"
            )
        if self.calibration_export and not self.calibration_export.exists():
            raise FileNotFoundError(
                f"--calibration-export does not exist: {self.calibration_export}. "
                f"Build it first: python3 scripts/calibration/build_calibration_workspace.py "
                f"--npi <NPI> --as-of {calibration_as_of} --with-panels"
            )
        if calibration_as_of:
            default_root = (Path(state_dir) if state_dir else STATE_ROOT_DEFAULT) / "calibration"
            self.state_root = default_root
        else:
            self.state_root = Path(state_dir) if state_dir else STATE_ROOT_DEFAULT
        self.working_dir = Path(working_dir).resolve() if working_dir else None
        self.dry_run = dry_run
        self.event_sink = event_sink

        # CALIBRATION mode guards (must happen BEFORE case_id minting so the
        # synthetic case_id is enforced if calibration is on).
        if calibration_as_of:
            # Validate cutoff date format eagerly; fail-fast beats a partially
            # initialized orchestrator.
            try:
                _date.fromisoformat(calibration_as_of)
            except ValueError as e:
                raise ValueError(
                    f"--calibration-as-of must be YYYY-MM-DD, got {calibration_as_of!r}: {e}"
                )
            # If a case_id was passed AND it does not look synthetic, refuse —
            # caller likely passed a real DOJ case_id, which would be a leak
            # (case_id string itself can name the defendant).
            if case_id and not case_id.startswith("CALIBRATION-"):
                raise ValueError(
                    f"In calibration mode, --case-id (if given) must start with "
                    f"'CALIBRATION-'; got {case_id!r}. Omit --case-id to auto-mint a synthetic one."
                )

        # Wire CWD for all claude -p subprocess spawns (e.g., cleanroom isolation)
        if self.working_dir is not None:
            if not self.working_dir.exists():
                raise FileNotFoundError(f"--working-dir does not exist: {self.working_dir}")
            set_default_cwd(self.working_dir)

        # Wire event sink: when set, every claude -p invocation transparently
        # delegates to invoke_agent_streaming, forwarding tool calls + text
        # turns as STAGE_* events. The stage name is rotated to the specific
        # agent role (investigator, counsel_build, defense, ...) via
        # _stage(...) immediately before each .run() call, so the Firm page
        # can light up just the agent that's actually executing right now
        # instead of all 8 main-floor sprites at once.
        if self.event_sink is not None:
            set_event_sink(self.event_sink, STAGE_INVESTIGATOR)

        # Resume or create
        if case_id:
            existing = load_summary(case_id, self.state_root)
            if existing is None:
                raise FileNotFoundError(f"No state found for case_id={case_id!r}")
            self.state = existing
            # On resume, the persisted state.status may have been clobbered by
            # the prior run's exception handler (set to "agent_error" before
            # the case_ready / abandoned terminal could be recorded). Re-derive
            # the TRUE status from the last iteration's counsel_disposition
            # parsed.json so the main loop doesn't pointlessly redo iter-N+1
            # when the case was already declared case_ready.
            self._reconcile_status_from_disk()
            LOG.info(
                f"Resuming case {case_id} | "
                f"iterations={len(self.state.iterations)} | "
                f"status={self.state.status}"
            )
        else:
            # CALIBRATION: synthetic case_id (NOT derived from subject) so the
            # state-dir path + session-IDs don't encode any identifying info
            # about the defendant. Format: CALIBRATION-<8-hex-uuid>.
            if self.calibration_as_of:
                new_id = f"CALIBRATION-{_uuid.uuid4().hex[:8].upper()}"
            else:
                new_id = self._mint_case_id(subject)
            self.state = CaseState(
                case_id=new_id,
                subject=subject,
                start_time=datetime.utcnow(),
                last_updated=datetime.utcnow(),
            )
            LOG.info(f"New case: {new_id}")

        # Write the case_id marker into the run dir EAGERLY at __init__ end,
        # not lazily at finalize. Previously _collect_execution_trace wrote
        # this marker only when it ran (i.e., at case-writer / finalize time);
        # if a run crashed or was interrupted before finalize, the marker
        # was never written, and subsequent resume-runs' bundles could not
        # find the original run's events.jsonl — so execution_trace[] in the
        # bundle was missing every tool call / reasoning turn from the run
        # that did the actual work. Writing the marker at __init__ end makes
        # every run discoverable by case_id regardless of how it terminates.
        if self.event_sink is not None:
            try:
                (self.event_sink.path.parent / "case_id.txt").write_text(
                    self.state.case_id
                )
            except Exception:
                pass  # best-effort; finalize path still re-writes it

    def _reconcile_status_from_disk(self) -> None:
        """Re-derive ``self.state.status`` from the most recent disposition.

        Background: the exception handler in ``run()`` writes
        ``state.status = "agent_error"`` whenever any agent fails. That
        clobbers the intermediate ``case_ready`` / ``abandoned`` terminal
        that may have been declared earlier (e.g., Hemlock's iter-01
        disposition was DECLARE_CASE_READY_WITH_DEPENDENCIES; then adv-r1
        crashed, overwriting status to agent_error). On resume we want
        the orchestrator to honor the original case_ready decision and
        SKIP the main loop, picking up at the adversarial loop instead.

        Strategy: walk the iterations in reverse; the first one whose
        counsel_disposition OR counsel_first_chair_review has a terminal
        action wins. (Phase A's first_chair_review can ABANDON_CASE; Phase B's
        counsel_disposition can DECLARE_CASE_READY / ABANDON_CASE.)
        """

        # Guardrail-4 — an UNRESOLVED human checkpoint pins the status. The checkpoint
        # deliberately did NOT finalize the recorded ABANDON disposition, so do not let
        # the walk below reconcile the case to 'abandoned'. It stays parked until a
        # human answers (answer_checkpoint.py clears review_request + the stale
        # disposition before resuming).
        rr = self.state.review_request
        if rr is not None and not rr.resolved:
            self.state.status = "needs_human_checkpoint"
            return

        for it in reversed(self.state.iterations):
            # Phase B disposition takes priority (later than first_chair_review)
            disp = it.counsel_disposition
            if disp is not None:
                action = disp.action
                if action in (
                    CounselDispositionAction.DECLARE_CASE_READY,
                    CounselDispositionAction.DECLARE_CASE_READY_WITH_DEPENDENCIES,
                ):
                    if self.state.status != "case_ready":
                        LOG.info(
                            f"Reconciling status: persisted={self.state.status!r} "
                            f"but iter-{it.iteration} disposition was {action.value} → "
                            f"overriding to 'case_ready' so main loop is skipped"
                        )
                        self.state.status = "case_ready"
                    return
                if action == CounselDispositionAction.ABANDON_CASE:
                    if self.state.status != "abandoned":
                        LOG.info(
                            f"Reconciling status: persisted={self.state.status!r} "
                            f"but iter-{it.iteration} disposition was ABANDON → "
                            f"overriding to 'abandoned'"
                        )
                        self.state.status = "abandoned"
                    return
            # Phase A first-chair ABANDON is also terminal
            review = it.counsel_first_chair_review
            if review is not None and review.action == CounselFirstChairReviewAction.ABANDON_CASE:
                if self.state.status != "abandoned":
                    LOG.info(
                        f"Reconciling status: persisted={self.state.status!r} "
                        f"but iter-{it.iteration} first_chair_review was ABANDON → "
                        f"overriding to 'abandoned'"
                    )
                    self.state.status = "abandoned"
                return

    @staticmethod
    def _mint_case_id(subject: str) -> str:
        """Short, sortable id like CASE-LK7TMX-2026."""
        ts = int(time.time())
        return f"CASE-{ts:X}-{datetime.utcnow().year}"

    def _stage(self, role: str) -> None:
        """Rotate the active stage name so subsequent events carry this role.

        Called immediately before each agent's ``.run()`` so the Firm UI can
        light up only the agent that's currently executing instead of all 8
        main-floor sprites simultaneously. No-op when no event sink is
        wired (bare-CLI runs without the orchestrate_pipeline plumbing).

        Also emits a STAGE_LOG_LINE under the new role so the UI knows the
        orchestrator has rotated into this step BEFORE any tool calls fire
        (otherwise the firm can take 30-60s to "see" the new active agent
        while claude -p is composing its first turn).
        """
        if self.event_sink is not None:
            set_event_sink(self.event_sink, role)
            from .event_sink import EVENT_STAGE_LOG_LINE
            self.event_sink.emit(role, EVENT_STAGE_LOG_LINE, {
                "level": "info",
                "msg": f"orchestrator entered {role}",
                "orchestrator_step": role,
            })

    # ────────────────────────────────────────────────────────────────────────
    # Main entry
    # ────────────────────────────────────────────────────────────────────────

    def run(self) -> int:
        # CALIBRATION CLEAN-ROOM lifecycle: set the module-level calibration
        # context BEFORE any agent invocation, clear it in `finally` so a
        # subsequent run in the same Python process (e.g., the Streamlit GUI)
        # cannot inherit calibration restrictions. The context being set is
        # what `claude_p.invoke_agent()` reads to inject hardened launch flags
        # + prompt prefix.
        #
        # Phase 1.6 (2026-06-03): the spawned claude -p subprocess MUST have
        # its cwd OUTSIDE the repo. Verified empirically: when cwd is inside
        # the repo, the agent's cwd-implicit Read scope grants access to
        # the entire repo tree (gold_set.jsonl, docs/calibration/, sibling
        # case-state dirs), bypassing the --allowed-tools jail. The sandbox
        # cwd is created fresh per run under $HOME so it can never accidentally
        # contain anything sensitive.
        import os
        _cal_set = False
        _prior_default_cwd = None
        if self.calibration_as_of:
            run_dir = self.state_root / self.state.case_id
            run_dir.mkdir(parents=True, exist_ok=True)
            # ONE mode: cwd = the fail-closed clean export (pre-cutoff panels +
            # answer-key-free tools), Read anchored there. The agent runs the real
            # billing tools but the answer keys / post-cutoff data / .git aren't in
            # the tree to reach. Guaranteed present by the __init__ guard.
            export_root = self.calibration_export
            ctx = _calibration.CalibrationContext(
                as_of=_date.fromisoformat(self.calibration_as_of),
                run_dir=run_dir.resolve(),
                repo_root=Path(__file__).resolve().parents[3],
                sandbox_cwd=export_root.resolve(),
                export_root=export_root.resolve(),
            )
            _calibration.set_calibration_context(ctx)
            # Point claude -p subprocesses at the out-of-repo export cwd.
            # Save the previous _DEFAULT_CWD so finally can restore it.
            from .claude_p import get_default_cwd
            _prior_default_cwd = get_default_cwd()
            set_default_cwd(export_root.resolve())
            os.environ["INVESTIGATOR_AS_OF"] = self.calibration_as_of
            _cal_set = True
            LOG.info(
                f"CALIBRATION mode ACTIVE — as_of={self.calibration_as_of}, "
                f"case_id={self.state.case_id}, run_dir={run_dir}, "
                f"export={export_root} (data-rich, real tools, fail-closed). "
                f"Every claude -p spawn will use hardened launch flags + out-of-repo cwd."
            )

        if self.event_sink is not None:
            self.event_sink.emit(
                STAGE_INVESTIGATOR, EVENT_STAGE_START,
                {"subject": self.subject, "case_id": self.state.case_id,
                 "max_iterations": self.max_iterations,
                 "max_adversarial": self.max_adversarial,
                 "calibration_as_of": self.calibration_as_of},
            )
        try:
            self._run_main_loop()
            if self.state.status == "case_ready":
                self._run_adversarial_loop()
                # Guardrail 3 — Innocent-Explanation Foreclosure Check. Runs
                # after the adversarial loop, before the final strength score,
                # so a LIVE high-plausibility innocent explanation caps strength
                # + blocks a clean FILE in the finalize step below.
                self._run_foreclosure_check()
                # Upgrade 2 — recompute the strength score + EVF with the
                # final adversarial discount so case_writer + downstream
                # consumers see the post-discount picture, and override
                # the final verdict if EVF is below the abandon threshold.
                self._finalize_phase_c_strength_score()
            # Guardrail-4 — the loop paused at a human-in-the-loop checkpoint (a
            # dispositive call rested on a premise it cannot verify; review_request is
            # set). There is no real verdict / dossier yet: exit cleanly (0) BEFORE
            # case_writer + _finalize and wait for a human to answer
            # (scripts/answer_checkpoint.py) + resume. The `finally` teardown still runs.
            if self.state.status == "needs_human_checkpoint":
                write_summary(self.state, self.state_root)
                if self.event_sink is not None:
                    self.event_sink.emit(
                        STAGE_INVESTIGATOR, EVENT_STAGE_COMPLETE,
                        {"case_id": self.state.case_id,
                         "status": self.state.status,
                         "final_verdict": "NEEDS_HUMAN_CHECKPOINT",
                         "awaiting_human": True},
                    )
                ctype = (self.state.review_request.checkpoint_type
                         if self.state.review_request else "?")
                LOG.warning(
                    f"⏸ Run paused at a human-in-the-loop checkpoint ({ctype}). "
                    f"Resolve: python scripts/answer_checkpoint.py answer "
                    f"{self.state.case_id} --answer '…'"
                )
                return 0
            if self._case_has_terminal_outcome():
                self._run_case_writer()
            else:
                LOG.warning(
                    "Skipping case_writer: case has no terminal verdict "
                    f"(status={self.state.status!r}, no disposition) — writing a "
                    "dossier here would be misleading. Resume to complete the loop."
                )
            self._finalize()
            if self.event_sink is not None:
                self.event_sink.emit(
                    STAGE_INVESTIGATOR, EVENT_STAGE_COMPLETE,
                    {"case_id": self.state.case_id,
                     "status": self.state.status,
                     "final_verdict": str(self.state.final_verdict) if self.state.final_verdict else None},
                )
            return 0
        except ClaudePHardStopError as e:
            # Org spend limit hit — an EXPECTED billing wall, not a crash. Log
            # it as a clean one-liner (no traceback) with the resume hint, and
            # preserve any verdict already reached. The run is resumable once an
            # admin raises the cap. Distinct return code (2) so a wrapper can
            # tell "raise the cap and resume" apart from a genuine agent_error.
            LOG.error(str(e))
            self.state.status = "spend_limit"
            if self.state.final_verdict is None:
                self.state.final_verdict = FinalVerdict.NEEDS_HUMAN
            write_summary(self.state, self.state_root)
            if self.event_sink is not None:
                self.event_sink.emit(
                    STAGE_INVESTIGATOR, EVENT_STAGE_FAILED,
                    {"error": str(e), "case_id": self.state.case_id,
                     "hard_stop": "org_spend_limit"},
                )
            return 2
        except ClaudePInvocationError as e:
            LOG.exception("Agent invocation error")
            self.state.status = "agent_error"
            # Preserve an already-computed verdict. A failure in a downstream/cosmetic step
            # (e.g. a 429 in the case-writer's long-form render) must NOT erase a fully-scored
            # disposition (e.g. the Phase-C reconciled verdict). Only fall to NEEDS_HUMAN when
            # no verdict was reached. status=agent_error still records that an error occurred.
            if self.state.final_verdict is None:
                self.state.final_verdict = FinalVerdict.NEEDS_HUMAN
            write_summary(self.state, self.state_root)
            if self.event_sink is not None:
                self.event_sink.emit(
                    STAGE_INVESTIGATOR, EVENT_STAGE_FAILED,
                    {"error": str(e), "case_id": self.state.case_id},
                )
            return 1
        except KeyboardInterrupt:
            LOG.warning("Interrupted; state preserved")
            if self.event_sink is not None:
                self.event_sink.emit(
                    STAGE_INVESTIGATOR, EVENT_STAGE_FAILED,
                    {"error": "interrupted by user", "case_id": self.state.case_id},
                )
            return 130
        finally:
            # CALIBRATION CLEAN-ROOM teardown — ALWAYS clear the module-level
            # context (and INVESTIGATOR_AS_OF env var) so a subsequent run in
            # the same Python process (Streamlit GUI, eval batch) doesn't
            # inherit calibration restrictions. Also restore _DEFAULT_CWD to
            # whatever was set before (None in normal usage). Idempotent on
            # the un-set case.
            if _cal_set:
                _calibration.set_calibration_context(None)
                os.environ.pop("INVESTIGATOR_AS_OF", None)
                set_default_cwd(_prior_default_cwd)
                LOG.info(
                    f"CALIBRATION mode cleared for case_id={self.state.case_id}"
                )

    # ────────────────────────────────────────────────────────────────────────
    # Layer 3: Build phase A (loop) + Build phase B (rebuttal-closing single pass)
    # ────────────────────────────────────────────────────────────────────────

    def _run_main_loop(self) -> None:
        """Run Phase A (build loop) and, if the case proceeds, Phase B (rebuttal-closing).

        Two-phase workflow that mirrors real qui tam firm practice:

        - Phase A: investigator → counsel_build → counsel_first_chair_review,
          looping up to ``max_pre_rebuttal_iters``. First-chair partner decides
          whether the draft is mature enough for internal mock-defense.
          Actions: CONTINUE_BUILD / DROP_COUNTS_AND_CONTINUE / PROCEED_TO_REBUTTAL
          / ABANDON_CASE.

        - Phase B: counsel_rebuttal (mock defense) → investigator (targeted at
          rebuttal weak_links) → counsel_build (final revision) →
          counsel_disposition (managing-partner sign-off).
          Actions: DECLARE_CASE_READY{,_WITH_DEPENDENCIES} / ABANDON_CASE.

        Resume guard: if the case is already terminal (case_ready/abandoned/
        max_iterations) skip both phases.
        """
        if self.state.status in ("case_ready", "abandoned", "max_iterations"):
            LOG.info(
                f"Main loop: status={self.state.status!r} from prior run → "
                f"skipping Phase A + Phase B"
            )
            return

        try:
            proceeded_to_rebuttal = self._run_phase_a()
        except ClaudePInvocationError as e:
            # Graceful degradation: an agent step failed after all retries. A
            # FOLLOW-UP enrichment pass choking should not discard a complete prior
            # build — if one exists, proceed to Phase B with the last good build
            # (Phase B already falls back to the last build-iter with a counsel_build).
            # Only a FOUNDATIONAL failure (no build yet) aborts the run.
            if self._has_complete_build():
                LOG.warning(
                    f"Phase A aborted by an agent failure "
                    f"({type(e).__name__}: {str(e).splitlines()[0][:160]}), but a "
                    f"complete prior build exists — degrading gracefully: proceeding "
                    f"to Phase B with the last good build instead of discarding the run."
                )
                write_summary(self.state, self.state_root)
                proceeded_to_rebuttal = True
            else:
                LOG.error(
                    "Phase A failed with no complete build yet (foundational failure) "
                    "— cannot degrade; aborting."
                )
                raise
        if proceeded_to_rebuttal:
            self._run_phase_b()

    # ────────────────────────────────────────────────────────────────────────
    # Helpers
    # ────────────────────────────────────────────────────────────────────────

    def _finalize(self) -> None:
        # Guardrail-4 — never finalize an UNRESOLVED human checkpoint to 'complete'.
        # run() already early-returns before reaching here for a fresh checkpoint;
        # this guards any other path (e.g. a resume that re-derived the checkpoint).
        rr = self.state.review_request
        if rr is not None and not rr.resolved:
            self.state.status = "needs_human_checkpoint"
            write_summary(self.state, self.state_root)
            return
        if self.state.final_verdict is None:
            # No adversarial loop ran (e.g., max_iterations exit) → mark
            if self.state.status == "abandoned":
                pass
            elif self.state.status == "max_iterations":
                self.state.final_verdict = FinalVerdict.NEEDS_HUMAN
            else:
                self.state.final_verdict = FinalVerdict.NEEDS_HUMAN
        self.state.status = "complete"
        write_summary(self.state, self.state_root)
        transcript = build_transcript_from_disk(self.state.case_id, self.state_root)
        write_transcript(self.state.case_id, transcript, self.state_root)
        LOG.info(f"COMPLETE — case {self.state.case_id} | verdict={self.state.final_verdict}")
        LOG.info(f"State:  {case_dir(self.state.case_id, self.state_root)}")
        # Publish the 4 standard deliverables to investigations/ so the GUI's
        # Investigations page surfaces this case as a finished dossier
        # (matches the Larch / Linden / etc. convention). Skipped in calibration
        # mode: a calibration run is an eval artifact, not a real investigation, and
        # publishing would drop a defendant-named dir into the production tree.
        if self.calibration_as_of:
            LOG.info("CALIBRATION mode — skipping publish to investigations/ (eval artifact).")
        else:
            self._publish_to_investigations()

    def _publish_to_investigations(self) -> None:
        """Copy the case dossier to investigations/<LastName>-<YYYY-MM-DD>/.

        Best-effort — if the case has no readable subject or the deliverables
        aren't on disk yet, logs a warning and continues. Idempotent: if the
        target dir already has these files (e.g., a prior run published), the
        files are overwritten (the latest run wins).
        """
        import re
        import shutil
        from datetime import date

        src = case_dir(self.state.case_id, self.state_root)
        # Required deliverables — written by case_writer.py:render_deliverables
        sources = {
            "00_README.md": src / "00_README.md",
            "01_Case_Report.md": src / "01_Case_Report.md",
            "02_Layperson_Summary.md": src / "02_Layperson_Summary.md",
            # Note: case_writer writes 03_Full_State.json; the legacy
            # investigations/ convention uses 03_Full_State_Investigation.json
            "03_Full_State_Investigation.json": src / "03_Full_State.json",
        }
        # Optional: the 04_Full_Investigation_Bundle.json is the larger
        # per-agent dump (1-3 MB) emitted by _emit_full_state_bundle. Include
        # it when present; absent for legacy runs that pre-date the bundle
        # emit (which is fine — older dossiers just don't have it).
        bundle = src / "04_Full_Investigation_Bundle.json"
        if bundle.exists():
            sources["04_Full_Investigation_Bundle.json"] = bundle
        # Optional: the 05_Counsel_Memorandum.pdf — emitted by
        # _emit_counsel_memorandum_pdf. Same best-effort handling.
        pdf = src / "05_Counsel_Memorandum.pdf"
        if pdf.exists():
            sources["05_Counsel_Memorandum.pdf"] = pdf
        # Optional: the 06_Qui_Tam_Complaint.pdf — emitted by
        # _emit_qui_tam_complaint_pdf. Same best-effort handling.
        qt_pdf = src / "06_Qui_Tam_Complaint.pdf"
        if qt_pdf.exists():
            sources["06_Qui_Tam_Complaint.pdf"] = qt_pdf

        missing = [name for name, p in sources.items()
                   if name in ("00_README.md", "01_Case_Report.md",
                               "02_Layperson_Summary.md",
                               "03_Full_State_Investigation.json")
                   and not p.exists()]
        if missing:
            LOG.warning(
                f"_publish_to_investigations: missing deliverables {missing} "
                f"in {src} — skipping. Run case_writer first."
            )
            return

        # Derive directory name from the subject. Format:
        # "Firstname M. Lastname, Credential -- Specialty -- City, State -- NPI ... -- ..."
        # → "Lastname-YYYY-MM-DD" (matching investigations/Larch-2026-05-02/ etc).
        last_name = self._extract_last_name(self.state.subject) or self.state.case_id
        # Sanitize for filesystem (alphanumeric + hyphen only)
        last_name = re.sub(r"[^A-Za-z0-9\-]", "", last_name)
        if not last_name:
            last_name = self.state.case_id

        # Repo root is 4 levels up from this file (fraud-investigator-v2/src/
        # fraud_investigator/orchestrator.py → ../../../ → repo)
        from pathlib import Path as _Path
        repo_root = _Path(__file__).resolve().parents[3]
        dst = repo_root / "investigations" / f"{last_name}-{date.today().isoformat()}"
        dst.mkdir(parents=True, exist_ok=True)
        for dst_name, src_path in sources.items():
            shutil.copy2(src_path, dst / dst_name)
        LOG.info(f"PUBLISHED → {dst} ({len(sources)} deliverables)")

    @staticmethod
    def _extract_last_name(subject: str) -> str | None:
        """Extract the surname from a subject string like
        'Provider Hemlock -- Family Practice -- Anytown, FL -- ...'

        Strategy: take the first ' -- '-separated segment (the name+credential),
        strip the credential after ',', then take the last whitespace-separated
        word.  Returns None on malformed input.
        """
        if not subject:
            return None
        # Take everything before the first ' -- '
        name_part = subject.split(" -- ", 1)[0]
        # Strip credential (everything from the first comma onward)
        name_only = name_part.split(",", 1)[0].strip()
        if not name_only:
            return None
        # Last whitespace-separated word is the surname
        parts = name_only.split()
        return parts[-1] if parts else None

    # ────────────────────────────────────────────────────────────────────────
    # Convergence-floor enforcement (discrimination layer)
    # ────────────────────────────────────────────────────────────────────────

    def _venue_token_documented(self, inv_out) -> bool:
        """True when ANY iteration's investigator notes (or the just-finished
        ``inv_out``) carry the literal VENUE_ENFORCEMENT_LANDSCAPE section."""
        token = "VENUE_ENFORCEMENT_LANDSCAPE"
        for it in self.state.iterations:
            if it.investigator and token in (it.investigator.notes_for_counsel or ""):
                return True
        return bool(inv_out is not None
                    and token in (inv_out.notes_for_counsel or ""))

    def _maybe_park_case(self, build_out, inv_out) -> None:
        """M3 — sidecar PARKED registry for retriggerable abandons.

        A case abandoned ONLY because slot 3 was empty (floor audit
        ``PASS_NO_SLOT3``: no falsity anchor AND no regulatory corroboration,
        with no count outright ``FAIL``) becomes viable the day a revocation /
        LEIE / S&I refresh hits the target or a co-biller. Park it at
        ``<state_root>/parked/<case_id>.json`` so the sunk build spend is
        recoverable; ``scripts/check_parked_cases.py`` scans the registry for
        re-trigger hits. Pure sidecar — no state-schema fields, no status
        change, and NEVER raises into the abandon path.
        """
        try:
            audit = compute_floor_audit(
                build_out.model_dump(),
                venue_token_found=self._venue_token_documented(inv_out),
            )
            counts = audit.get("counts") or []
            floors = [c.get("floor") for c in counts]
            if "PASS_NO_SLOT3" not in floors or "FAIL" in floors:
                return
            npi = extract_npi(self.subject)
            watch_npis = [npi] if npi else []
            pack_path = self.state_root / self.state.case_id / "evidence_pack.json"
            if pack_path.exists():
                try:
                    cobillers = json.loads(pack_path.read_text()).get("cobiller_npis") or []
                    for cob in cobillers:
                        if cob and cob not in watch_npis:
                            watch_npis.append(str(cob))
                except Exception as e:
                    LOG.warning(f"park-case: cobiller read failed ({e}); watching target only")
            parked_dir = self.state_root / "parked"
            parked_dir.mkdir(parents=True, exist_ok=True)
            record = {
                "case_id": self.state.case_id,
                "subject": self.subject,
                "parked_at": datetime.utcnow().isoformat() + "Z",
                "npi": npi,
                "watch_npis": watch_npis,
                "missing": "slot3: no falsity anchor and no regulatory corroboration",
                "count_floors": {c.get("count_id"): c.get("floor") for c in counts},
                "resume_cmd": (
                    f"PYTHONPATH=fraud-investigator-v2/src python3 -m fraud_investigator "
                    f"\"{self.subject}\" --case-id {self.state.case_id}"
                ),
            }
            out_path = parked_dir / f"{self.state.case_id}.json"
            out_path.write_text(json.dumps(record, indent=2))
            LOG.info(f"CASE PARKED (slot-3-only abandon) → {out_path} | "
                     f"watching {len(watch_npis)} NPI(s)")
        except Exception as e:
            LOG.warning(f"park-case registry write failed (non-fatal): {e}")

    # ────────────────────────────────────────────────────────────────────────
    # Procedural floors (independent of factual convergence)
    # ────────────────────────────────────────────────────────────────────────

    MIN_ITERS_FOR_READY: int = 2

    _VENUE_SEARCH_FOLLOWUP: str = (
        "Run Step 8b — venue-axis precedent search. Derive the federal "
        "district from the defendant's primary practice address, then run "
        "all three: (a) doj_pr_lookup.py search '<scheme keyword>' --field "
        "body, filtered to that district's USAO slug (e.g., usao-edmo, "
        "usao-mdfl); (b) CourtListener RECAP/opinions search for the scheme "
        "keyword scoped to the district; (c) WebSearch for '<district name> "
        "<scheme keyword> (settlement OR indictment OR qui tam)'. Populate "
        "the VENUE_ENFORCEMENT_LANDSCAPE: subsection of notes_for_counsel "
        "with the queries run, the same-district analogues found (with full "
        "citations + DOJ press release URLs), the cross-district analogues "
        "found, and the pleading-stage scienter implication. If no same-"
        "district analogue exists after the search, say so explicitly — "
        "the absence is itself a recordable finding."
    )

    # ────────────────────────────────────────────────────────────────────────
    # Upgrade 1 + 2 — computed strength score & EVF gating
    # ────────────────────────────────────────────────────────────────────────

    def _all_evidence_for_scoring(self) -> list:
        """Flatten all iterations' investigator evidence into one list (for
        the strength-score's factual-density component). Returns Pydantic
        EvidenceItem objects so the scoring module can read .tier directly.
        """
        items = []
        seen: set[str] = set()
        for it in self.state.iterations:
            if it.investigator and it.investigator.evidence:
                for ev in it.investigator.evidence:
                    if ev.id in seen:
                        continue
                    seen.add(ev.id)
                    items.append(ev)
        for adv in self.state.adversarial_rounds:
            if adv.rebuttal_investigator and adv.rebuttal_investigator.evidence:
                for ev in adv.rebuttal_investigator.evidence:
                    if ev.id in seen:
                        continue
                    seen.add(ev.id)
                    items.append(ev)
        return items

    def _concat_investigator_notes(self) -> str:
        """Concatenate every iteration's investigator notes_for_counsel +
        adversarial-round rebuttal_investigator notes. Used by the scoring
        module's text-based feature detectors (venue precedent, within-
        practice controls, regulatory predicates).
        """
        parts: list[str] = []
        for it in self.state.iterations:
            if it.investigator and it.investigator.notes_for_counsel:
                parts.append(it.investigator.notes_for_counsel)
        for adv in self.state.adversarial_rounds:
            if adv.rebuttal_investigator and adv.rebuttal_investigator.notes_for_counsel:
                parts.append(adv.rebuttal_investigator.notes_for_counsel)
        return "\n\n".join(parts)

    def _collect_prior_investigator_threads(self) -> tuple[list[str], list[str]]:
        """Aggregate working_hypotheses + unexpected_absences from all prior
        investigator outputs in this case.

        Returns (working_hypotheses, unexpected_absences) lists, capped at
        5 + 3 respectively (most-recent-first). These thread into the next
        investigator iter's continuation prompt, forcing the agent to
        explicitly pursue or retire each open thread instead of silently
        dropping it.

        Defensive against older parsed.json files that lack these fields:
        Pydantic defaults to empty lists if the field is missing.
        """
        working_hypotheses: list[str] = []
        unexpected_absences: list[str] = []
        # Walk in reverse so most-recent threads come first
        for it in reversed(self.state.iterations):
            if it.investigator is None:
                continue
            # getattr() because older parsed.json may not have these fields
            wh = getattr(it.investigator, "working_hypotheses", []) or []
            ua = getattr(it.investigator, "unexpected_absences", []) or []
            for h in wh:
                if h not in working_hypotheses and len(working_hypotheses) < 5:
                    working_hypotheses.append(h)
            for a in ua:
                if a not in unexpected_absences and len(unexpected_absences) < 3:
                    unexpected_absences.append(a)
            if len(working_hypotheses) >= 5 and len(unexpected_absences) >= 3:
                break
        # Also include rebuttal_investigator outputs (same schema, different role)
        for adv in reversed(self.state.adversarial_rounds):
            if adv.rebuttal_investigator is None:
                continue
            wh = getattr(adv.rebuttal_investigator, "working_hypotheses", []) or []
            ua = getattr(adv.rebuttal_investigator, "unexpected_absences", []) or []
            for h in wh:
                if h not in working_hypotheses and len(working_hypotheses) < 5:
                    working_hypotheses.append(h)
            for a in ua:
                if a not in unexpected_absences and len(unexpected_absences) < 3:
                    unexpected_absences.append(a)
        return working_hypotheses, unexpected_absences

    def _evidence_pack_md(self) -> str:
        """Build-or-load the Step-0 deterministic evidence pack (E1).

        Idempotent: the pack is built once per case and cached at
        ``<state>/<case_id>/evidence_pack.{json,md}`` (resume-safe). Returns
        '' when the subject carries no NPI or the build fails — the prompts
        degrade gracefully to the agent running the lookups itself.
        Calibration runs pass ``as_of`` so the pack is panel-only +
        year-gated with every live-registry section redacted (a current
        exclusion/enrollment status is a post-cutoff leak vector).
        """
        if self.dry_run:
            return ""
        npi = extract_npi(self.subject)
        if not npi:
            LOG.info("evidence pack: no NPI in subject — skipping Step-0 pack")
            return ""
        root = self.working_dir or Path.cwd()
        case_dir = self.state_root / self.state.case_id
        md = ensure_evidence_pack(
            npi=npi, case_dir=case_dir, root=root,
            as_of=self.calibration_as_of, logger=LOG,
        )
        if md:
            LOG.info(f"evidence pack: ready ({len(md):,} chars) at "
                     f"{case_dir / 'evidence_pack.md'}")
        return md

    def _evidence_payload(self, *, full: bool = True) -> list[dict]:
        """Reconstruct the accumulated evidence list from iteration records.

        Modes:
          - full=True  (default): every evidence item with all fields. Used by
            investigator (for dedup-by-id) and rebuttal_investigator (sees full
            context to target follow-up questions). ~1.6KB/item; by iter-5
            with 50+ items the payload is ~80KB.
          - full=False (summary): drops citation, rationale, supports_counts
            redundancy. Keeps id + tier + fact + confidence. ~250 bytes/item;
            ~80% size reduction. Used by counsel_build / counsel_disposition
            where downstream reasoning needs the gist, not the full text — the
            agent can read parsed.json from disk via Bash for any specific
            full-text lookup.
        """
        items: list[dict] = []
        seen: set[str] = set()
        for it in self.state.iterations:
            if it.investigator:
                for ev in it.investigator.evidence:
                    if ev.id not in seen:
                        d = ev.model_dump()
                        if not full:
                            d = {
                                "id": d["id"],
                                "tier": d["tier"],
                                # truncate fact to keep prompt bounded
                                "fact": d["fact"][:280],
                                "confidence": d["confidence"],
                                "supports_counts": d.get("supports_counts", []),
                            }
                        items.append(d)
                        seen.add(ev.id)
        for adv in self.state.adversarial_rounds:
            if adv.rebuttal_investigator:
                for ev in adv.rebuttal_investigator.evidence:
                    if ev.id not in seen:
                        d = ev.model_dump()
                        if not full:
                            d = {
                                "id": d["id"],
                                "tier": d["tier"],
                                "fact": d["fact"][:280],
                                "confidence": d["confidence"],
                                "supports_counts": d.get("supports_counts", []),
                            }
                        items.append(d)
                        seen.add(ev.id)
        return items

    def _merge_evidence(self, new_items: list) -> None:
        seen = set(self.state.all_evidence_ids)
        for ev in new_items:
            if ev.id not in seen:
                self.state.all_evidence_ids.append(ev.id)
                seen.add(ev.id)

    def _merge_human_context(self, inv_out) -> None:
        """Fold any not-yet-merged ``human_context.jsonl`` notes into this
        iteration's investigator evidence, IN PLACE.

        Called right after an ``investigator.run()`` call, before
        ``_merge_evidence(inv_out.evidence)`` — so the new human_attested
        items ride the same merge + get picked up by every downstream
        ``_evidence_payload()`` / ``_all_evidence_for_scoring()`` read for
        free (counsel_build, counsel_first_chair_review, counsel_rebuttal,
        counsel_disposition, and the next iteration's investigator).
        Idempotent (see ``human_context.new_human_evidence``): a note
        already merged in a prior iteration is skipped.
        """
        new_items = new_human_evidence(self.state, self.state_root)
        if not new_items:
            return
        inv_out.evidence.extend(new_items)
        LOG.info(f"  human context: +{len(new_items)} operator-supplied evidence item(s)")

    def _merge_verified_precedents(self, new_cites: list[str]) -> None:
        """Append new counsel_build precedent cites to the case-level cache,
        deduplicating by case-insensitive string match.

        The cache is passed into subsequent counsel_build / counsel_rebuttal /
        defense / legal_adv_response prompts so each agent's naive-context
        subprocess can skip re-verifying cites already verified upstream. Cuts
        roughly half the redundant CourtListener lookups across iterations.
        """
        seen_lower = {p.lower().strip() for p in self.state.verified_precedents}
        added = 0
        for cite in (new_cites or []):
            key = cite.lower().strip()
            if key and key not in seen_lower:
                self.state.verified_precedents.append(cite)
                seen_lower.add(key)
                added += 1
        if added:
            LOG.info(f"  cached {added} new verified precedent(s) "
                     f"(cache size: {len(self.state.verified_precedents)})")
