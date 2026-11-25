"""Contract guard: every orchestrator stage role is a registered STAGE.

The orchestrator calls ``self._stage("<role>")`` for each pipeline step, and
``EventSink.emit`` raises ``ValueError: Unknown stage`` for any role not in
``event_sink.STAGES``. PR #247 added the ``foreclosure_check`` stage to the
orchestrator but never registered it here — so EVERY run that completed the
adversarial loop crashed at the Guardrail-3 stage (caught 2026-06-12 on the
Birch run). This test statically scans the orchestrator for ``_stage("…")``
calls and asserts each role is registered, so the next stage added can't
ship the same latent crash.
"""
from __future__ import annotations

import re
from pathlib import Path

from fraud_investigator import event_sink

# The orchestrator was split into phase mixins (phase_a/b/c, dossier), so the
# _stage("…") calls live across those files too — scan the whole family.
_PKG = Path(event_sink.__file__).parent
_ORCH_FILES = [_PKG / f for f in (
    "orchestrator.py", "phase_a.py", "phase_b.py", "phase_c.py", "dossier.py")]
_STAGE_CALL_RE = re.compile(r'_stage\(\s*"([a-z_]+)"\s*\)')


def test_every_orchestrator_stage_role_is_registered():
    text = "\n".join(f.read_text() for f in _ORCH_FILES if f.exists())
    roles = set(_STAGE_CALL_RE.findall(text))
    assert roles, "found no _stage(\"…\") calls — did the orchestrator move?"
    missing = sorted(r for r in roles if r not in event_sink.STAGES)
    assert not missing, (
        f"orchestrator emits _stage() roles absent from event_sink.STAGES: "
        f"{missing}. Register each in event_sink.STAGES or EventSink.emit will "
        f"raise 'Unknown stage' at runtime (this is exactly the foreclosure_check "
        f"crash from 2026-06-12)."
    )


def test_foreclosure_check_specifically_registered():
    # Explicit regression pin for the PR #247 omission.
    assert "foreclosure_check" in event_sink.STAGES
