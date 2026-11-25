"""Resume coverage — no run is unrecoverable after a crash.

Invariant: a crash never costs more than the step that was executing.
Layers pinned here:

  1. The per-step parsed.json cache primitive — BOTH invocation paths
     (plain ``invoke_agent`` and ``invoke_agent_streaming``, the path every
     EventSink/console run takes) must reload a valid cached output without
     spawning ``claude -p``.
  2. The adversarial loop's trim-and-replay guard — persisted round records
     are discarded on resume and rebuilt via the cache (the loop has no
     per-round skip logic; without the trim, records duplicate and corrupt
     continuation alignment + scoring).
  3. The foreclosure-check skip guard — a ledger already on state is not
     re-run.

(Phase A iter-trim, Phase B partial-record reuse, and the evidence-pack
file-presence guard are the pre-existing layers; the cache tests here cover
the mechanism they all rely on.)
"""
from __future__ import annotations

import json
from datetime import datetime

import pytest
from pydantic import BaseModel

import fraud_investigator.claude_p as claude_p
from fraud_investigator.agents import foreclosure_check as _foreclosure_check
from fraud_investigator.orchestrator import Orchestrator
from fraud_investigator.schemas import AdversarialRoundRecord


class _Mini(BaseModel):
    x: int


class _SinkStub:
    def emit(self, *a, **k):
        pass


# ── 1a. cache primitive — plain path ─────────────────────────────────────────

def test_invoke_agent_reloads_cached_parsed_json(tmp_path, monkeypatch):
    monkeypatch.setattr(claude_p, "_EVENT_SINK", None)
    (tmp_path / "mystep.parsed.json").write_text(json.dumps({"x": 7}))
    out = claude_p.invoke_agent(
        prompt="irrelevant", schema=_Mini, out_dir=tmp_path,
        step_name="mystep",
        claude_bin="/nonexistent/claude-must-never-be-spawned",
    )
    assert out.x == 7


def test_invoke_agent_cache_bypass_env(tmp_path, monkeypatch):
    monkeypatch.setattr(claude_p, "_EVENT_SINK", None)
    monkeypatch.setenv("FRAUD_INV_NO_CACHE_LOAD", "1")
    (tmp_path / "mystep.parsed.json").write_text(json.dumps({"x": 7}))
    # With the cache bypassed, it must try to spawn — and fail on the bogus
    # binary rather than silently returning the cached value.
    with pytest.raises(Exception):
        claude_p.invoke_agent(
            prompt="irrelevant", schema=_Mini, out_dir=tmp_path,
            step_name="mystep",
            claude_bin="/nonexistent/claude-must-never-be-spawned",
            timeout_sec=5,
        )


# ── 1b. cache primitive — streaming path (what console runs use) ────────────

def test_streaming_invoke_reloads_cached_parsed_json(tmp_path):
    from fraud_investigator.streaming import invoke_agent_streaming
    (tmp_path / "mystep.parsed.json").write_text(json.dumps({"x": 9}))
    out = invoke_agent_streaming(
        prompt="irrelevant", schema=_Mini, out_dir=tmp_path,
        step_name="mystep", event_sink=_SinkStub(), stage_name="investigator",
        claude_bin="/nonexistent/claude-must-never-be-spawned",
    )
    assert out.x == 9


# ── 2. adversarial trim-and-replay ───────────────────────────────────────────

def _mk_orch(tmp_path, **kw):
    return Orchestrator(
        subject="Dr. Test Subject (NPI 1234567893) — resume-guard test",
        state_dir=tmp_path, dry_run=True, **kw,
    )


def test_adversarial_trim_discards_persisted_rounds(tmp_path):
    orch = _mk_orch(tmp_path)
    orch.state.adversarial_rounds = [
        AdversarialRoundRecord(round=1, timestamp=datetime.utcnow()),
        AdversarialRoundRecord(round=2, timestamp=datetime.utcnow()),
    ]
    n = orch._trim_adversarial_rounds_for_replay()
    assert n == 2
    assert orch.state.adversarial_rounds == []
    # Persisted summary reflects the trim (no stale rounds for the console).
    summary = json.loads(
        (tmp_path / orch.state.case_id / "summary.json").read_text())
    assert summary.get("adversarial_rounds") in ([], None)


def test_adversarial_trim_noop_on_fresh_run(tmp_path):
    orch = _mk_orch(tmp_path)
    assert orch._trim_adversarial_rounds_for_replay() == 0


# ── 3. foreclosure skip guard ────────────────────────────────────────────────

def test_foreclosure_check_skips_when_ledger_on_state(tmp_path, monkeypatch):
    orch = _mk_orch(tmp_path)
    orch.dry_run = False  # exercise the real guard, not the dry-run exit
    orch.state.foreclosure_ledger = {
        "explanations": [
            {"id": "IE1", "status": "LIVE", "plausibility_unrebutted": 0.7},
        ],
    }

    def _must_not_run(**kw):  # pragma: no cover — failure mode
        raise AssertionError("foreclosure_check agent re-ran despite ledger on state")

    # _run_foreclosure_check moved to phase_c; patch the agent module at its
    # source so the patch lands regardless of which module references it.
    monkeypatch.setattr(_foreclosure_check, "run", _must_not_run)
    orch._run_foreclosure_check()  # returns without raising
    assert orch.state.foreclosure_ledger  # untouched
