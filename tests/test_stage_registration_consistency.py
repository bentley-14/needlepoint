"""Every routed pipeline step must be registered as an emittable event-sink stage.

Regression guard for the 2026-06-12 crash: `foreclosure_check` (Guardrail 3) was
added to claude_p.MODEL_BY_STEP + the orchestrator + the GUI CONFIGURABLE_STEPS,
but NOT to event_sink.STAGES — so the first sidecar/GUI-launched run that reached
`_stage("foreclosure_check")` blew up with `ValueError: Unknown stage`. The
GUI-contract guard checks MODEL_BY_STEP↔CONFIGURABLE_STEPS but NOT event_sink, so
nothing caught it. This pins the missing invariant.

Invariant: the orchestrator calls `self._stage(step_name)` (which emits to the
event sink) for every per-agent step it routes via MODEL_BY_STEP. Therefore every
MODEL_BY_STEP key MUST be a member of event_sink.STAGES, or a sidecar run crashes
at that stage.

Run: pytest tests/test_stage_registration_consistency.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "fraud-investigator-v2" / "src"))

from fraud_investigator.claude_p import MODEL_BY_STEP  # noqa: E402
from fraud_investigator import event_sink  # noqa: E402


def test_every_routed_step_is_an_emittable_stage():
    missing = set(MODEL_BY_STEP) - set(event_sink.STAGES)
    assert not missing, (
        f"steps routed by claude_p.MODEL_BY_STEP but missing from event_sink.STAGES "
        f"(a sidecar/GUI run will crash at _stage() for these): {sorted(missing)}"
    )


def test_foreclosure_check_specifically_registered():
    # the exact stage that crashed the 2026-06-12 rerun
    assert "foreclosure_check" in event_sink.STAGES
    assert "foreclosure_check" in MODEL_BY_STEP
