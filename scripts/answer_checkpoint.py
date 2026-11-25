#!/usr/bin/env python3
"""HITL — list + answer human-review checkpoints, then resume the run.

A NeedlePoint run pauses (``final_verdict=NEEDS_HUMAN_CHECKPOINT``) when it would
otherwise make a dispositive, case-killing call on a premise it cannot verify
(see ``fraud_investigator.checkpoint``). This CLI lets a human answer the question;
the answer is recorded as a ``human_attested`` fact, the stale decision + its
on-disk step cache are cleared, and the run is rewound so a resume re-decides with
the new fact in hand.

    python scripts/answer_checkpoint.py list
    python scripts/answer_checkpoint.py answer CASE-XXXX-2026 \
        --answer "Dogwood v. Agency Myrtle (9:99-cv-00001) is NOS 442 Employment, not a qui tam" \
        [--citation "investigations/Birch-rerun/dogwood_v_myrtle/DOGWOOD_SUMMARY.md"] [--resume]

For a ``foreclosure_live_explanation`` checkpoint, ``--resolution`` is REQUIRED
so the resume knows which direction to take (a free-text ``--answer`` alone is
not enough — see the Guardrail-3 rewind note below):
    python scripts/answer_checkpoint.py answer CASE-XXXX-2026 \
        --answer "Confirmed — the community-oncology referral pattern is benign." \
        --resolution affirm_decline    # the innocent explanation holds; decline stands
    python scripts/answer_checkpoint.py answer CASE-XXXX-2026 \
        --answer "Refuted — no co-located referring specialists in any year." \
        --resolution foreclose_explanation   # premise refuted; resume re-decides

By default ``answer`` only mutates state + prints the resume command (safe — no
spawn). Pass ``--resume`` to also launch the resume (FRAUD_INV_SAFE_PERMS=1).
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "fraud-investigator-v2" / "src"
sys.path.insert(0, str(_SRC))

from fraud_investigator.schemas import (  # noqa: E402
    CounselDispositionAction,
    EvidenceItem,
    EvidenceTier,
)
from fraud_investigator.state import (  # noqa: E402
    STATE_ROOT_DEFAULT,
    case_dir,
    iter_dir,
    load_summary,
    write_summary,
)


def _extract_npi(subject: str) -> str | None:
    m = re.search(r"\b([12]\d{9})\b", subject or "")
    return m.group(1) if m else None


def _purge_step_cache(d: Path, step_name: str) -> int:
    """Delete the on-disk cache for one agent step so a resume re-runs it
    (state-level null alone replays the cached output). Returns files removed."""
    n = 0
    for p in d.glob(f"{step_name}.*"):
        try:
            p.unlink()
            n += 1
        except OSError:
            pass
    return n


def list_open(state_root: Path = STATE_ROOT_DEFAULT) -> list[dict]:
    """Every case with an OPEN (unresolved) human-review checkpoint."""
    out: list[dict] = []
    root = Path(state_root)
    if not root.exists():
        return out
    for summ in sorted(root.glob("*/summary.json")):
        try:
            state = load_summary(summ.parent.name, root)
        except Exception:
            continue
        rr = state.review_request if state else None
        if rr is not None and not rr.resolved:
            out.append({
                "case_id": state.case_id,
                "subject": state.subject,
                "checkpoint_type": rr.checkpoint_type,
                "question": rr.question,
                "what_would_resolve_it": rr.what_would_resolve_it,
                "npi": _extract_npi(state.subject),
            })
    return out


def resolve_checkpoint(
    case_id: str,
    answer: str,
    *,
    citation: str | None = None,
    resolution: str | None = None,
    reviewer: str = "human",
    state_root: Path = STATE_ROOT_DEFAULT,
) -> dict:
    """Record the human answer + rewind the case so a resume re-decides.

    Pure state mutation (no subprocess) — testable. Steps:
      1. append a human_attested confirmed_fact EvidenceItem (the answer),
      2. mark the ReviewRequest resolved (carrying ``resolution``, the human's
         DIRECTION on the provisional verdict),
      3. rewind the stale decision: Phase-B (ABANDON) → drop the disposition +
         its cache, status='in_progress'; Phase-C (CASE_READY) → status=
         'case_ready', and either:
           - resolution='affirm_decline' (the LIVE innocent explanation the
             loop found still holds) → PRESERVE the existing foreclosure
             ledger + its cache. The ledger already records exactly what the
             human just confirmed; clearing it would force a fresh, non-
             deterministic re-derivation of the same answer (the confirm-
             decline infinite-loop bug — the resume's _run_foreclosure_check
             resume-guard + _human_checkpoint_for_decline's resolved-type
             guard then commit NOT_ACTIONABLE off the SAME ledger, no re-run).
           - otherwise (resolution='foreclose_explanation', or a non-
             foreclosure Phase-C checkpoint) → clear the foreclosure ledger +
             purge its cache so the resume re-derives for real, as before.
      4. clear the verdict fields + persist.

    ``resolution`` is REQUIRED when the open checkpoint is
    ``foreclosure_live_explanation`` (the two directions need different
    rewinds — see step 3); it is ignored for other checkpoint types.

    Returns {case_id, checkpoint_type, npi, resume_make}.
    """
    state = load_summary(case_id, state_root)
    if state is None:
        raise SystemExit(f"no such case: {case_id}")
    rr = state.review_request
    if rr is None or rr.resolved:
        raise SystemExit(f"{case_id} has no OPEN checkpoint to answer.")
    fact = (answer or "").strip()
    if len(fact) < 10:
        raise SystemExit("--answer must be a substantive sentence (>= 10 chars).")
    _valid_resolutions = ("affirm_decline", "foreclose_explanation")
    if rr.checkpoint_type == "foreclosure_live_explanation":
        if resolution not in _valid_resolutions:
            raise SystemExit(
                f"{case_id}: a foreclosure_live_explanation checkpoint requires "
                f"--resolution {{{','.join(_valid_resolutions)}}} — 'affirm_decline' "
                "if the answer confirms the innocent explanation holds (decline "
                "stands), 'foreclose_explanation' if it refutes the explanation "
                "(the case proceeds on the merits)."
            )
    elif resolution is not None and resolution not in _valid_resolutions:
        raise SystemExit(f"--resolution must be one of {_valid_resolutions}, got {resolution!r}.")

    inv_iter = next((it for it in reversed(state.iterations) if it.investigator is not None), None)
    if inv_iter is None:
        raise SystemExit(f"{case_id}: no investigator iteration to attach the answer to.")

    ev_id = f"human-{rr.checkpoint_type}-{int(time.time())}"
    inv_iter.investigator.evidence.append(EvidenceItem(
        id=ev_id,
        tier=EvidenceTier.CONFIRMED_FACT,
        fact=fact,
        citation=citation or f"human review by {reviewer} on {datetime.now(timezone.utc).date().isoformat()}",
        confidence=1.0,
        provenance="human_attested",
        rationale=f"Resolves HITL checkpoint ({rr.checkpoint_type}): {rr.question[:180]}",
    ))
    if ev_id not in state.all_evidence_ids:
        state.all_evidence_ids.append(ev_id)

    rr.resolved = True
    rr.human_answer = fact
    rr.resolved_at = datetime.now(timezone.utc)
    rr.resolution = resolution
    # Remember the TYPE permanently — review_request holds only the latest
    # checkpoint, so without this a case that raises two different checkpoints
    # ping-pongs between them forever (each overwrite erases the other's guard).
    if rr.checkpoint_type not in state.resolved_checkpoint_types:
        state.resolved_checkpoint_types.append(rr.checkpoint_type)

    # Rewind the stale decision so the resume genuinely re-runs it.
    latest_disp_iter = next(
        (it for it in reversed(state.iterations) if it.counsel_disposition is not None), None)
    action = latest_disp_iter.counsel_disposition.action if latest_disp_iter else None
    if action == CounselDispositionAction.ABANDON_CASE:
        latest_disp_iter.counsel_disposition = None
        _purge_step_cache(iter_dir(case_id, latest_disp_iter.iteration, state_root), "counsel_disposition")
        state.status = "in_progress"
    elif rr.checkpoint_type == "foreclosure_live_explanation" and resolution == "affirm_decline":
        # The human's answer IS the dispositive fact — the LIVE innocent
        # explanation the loop already found still holds. Preserve the
        # existing ledger (don't clear it / don't purge its cache) so the
        # resume's foreclosure_check resume-guard skips re-running the gate
        # and the verdict commits NOT_ACTIONABLE off the SAME, human-affirmed
        # ledger instead of re-deriving the same answer from a fresh agent call.
        state.status = "case_ready"
    else:
        # Phase-C checkpoint resolved as foreclose_explanation (or a non-
        # foreclosure Phase-C checkpoint) — the premise has changed (or isn't
        # foreclosure-specific); re-run foreclosure + verdict for real.
        state.foreclosure_ledger = None
        _purge_step_cache(case_dir(case_id, state_root), "foreclosure_check")
        state.status = "case_ready"
    state.final_verdict = None
    state.verdict_rationale = None
    state.verdict_predicates = []
    write_summary(state, state_root)

    npi = _extract_npi(state.subject)
    return {
        "case_id": case_id,
        "checkpoint_type": rr.checkpoint_type,
        "npi": npi,
        "resume_make": f"make investigate NPI={npi or '<NPI>'} RESUME={case_id} SAFE_PERMS=1",
    }


def _cmd_list(args) -> int:
    rows = list_open(Path(args.state_root))
    if not rows:
        print("No open human-review checkpoints.")
        return 0
    print(f"{len(rows)} open checkpoint(s):\n")
    for r in rows:
        print(f"● {r['case_id']}  [{r['checkpoint_type']}]  NPI={r['npi'] or '?'}")
        print(f"    subject: {r['subject'][:90]}")
        print(f"    Q: {r['question']}")
        print(f"    resolves with: {r['what_would_resolve_it']}")
        print(f"    answer:  python scripts/answer_checkpoint.py answer {r['case_id']} --answer \"...\"\n")
    return 0


def _cmd_answer(args) -> int:
    info = resolve_checkpoint(
        args.case_id, args.answer, citation=args.citation,
        resolution=args.resolution, reviewer=args.reviewer,
        state_root=Path(args.state_root),
    )
    print(f"✓ Recorded human answer for {info['case_id']} ({info['checkpoint_type']}); "
          "stale decision + cache cleared.")
    if args.resume:
        npi = info["npi"]
        if not npi:
            print("⚠ could not extract NPI from the case subject; run the resume manually:")
            print(f"    {info['resume_make']}")
            return 1
        env = {**os.environ, "FRAUD_INV_SAFE_PERMS": "1"}
        print(f"▶ resuming: np_launch.py investigate {npi} --resume {info['case_id']} (SAFE_PERMS)")
        return subprocess.run(
            [sys.executable, "scripts/np_launch.py", "investigate", npi, "--resume", info["case_id"]],
            env=env, check=False,
        ).returncode
    print(f"▶ resume when ready:\n    {info['resume_make']}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="List + answer human-in-the-loop checkpoints.")
    ap.add_argument("--state-root", default=str(STATE_ROOT_DEFAULT),
                    help="NeedlePoint state root (default: fraud-investigator-v2/state).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="Show all open checkpoints.")
    pa = sub.add_parser("answer", help="Answer a checkpoint + rewind for resume.")
    pa.add_argument("case_id")
    pa.add_argument("--answer", required=True, help="The authoritative human answer (>=10 chars).")
    pa.add_argument("--citation", default=None, help="Source backing the answer (URL/file/cite).")
    pa.add_argument("--resolution", default=None,
                    choices=["affirm_decline", "foreclose_explanation"],
                    help="Required for a foreclosure_live_explanation checkpoint: "
                         "'affirm_decline' (the innocent explanation holds, decline "
                         "stands) or 'foreclose_explanation' (refuted; resume re-decides).")
    pa.add_argument("--reviewer", default=os.environ.get("USER", "human"))
    pa.add_argument("--resume", action="store_true",
                    help="Also launch the resume now (FRAUD_INV_SAFE_PERMS=1).")
    args = ap.parse_args()
    if args.cmd == "list":
        return _cmd_list(args)
    if args.cmd == "answer":
        return _cmd_answer(args)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
