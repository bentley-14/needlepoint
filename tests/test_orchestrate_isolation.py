"""The ranking stages must not take the investigation down with them.

A ranker crash or hang in `full` mode still lets a named NPI (--new-npi) be investigated;
without a named NPI the failure propagates. No network, no model calls.
"""
from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

import scripts.orchestrate_pipeline as op


class _Sink:
    def __init__(self):
        self.events = []

    def emit(self, stage, kind, payload):
        self.events.append((stage, kind, payload))


def _args(new_npi):
    return SimpleNamespace(new_npi=new_npi, year=2023, k_range=(1, 50), max_iterations=2,
                           max_pre_rebuttal_iters=2, max_adversarial=1, resume_case=None)


@pytest.fixture
def stage4(monkeypatch):
    calls = {}

    def fake_stage4(**kw):
        calls.update(kw)
        return 0

    monkeypatch.setattr(op, "run_stage4_investigator", fake_stage4)
    monkeypatch.setattr(op, "_build_subject_for_npi", lambda npi: (f"NPI {npi} ad-hoc", "bare"))
    return calls


def _ranker_crashes(*_a, **_k):
    raise RuntimeError("walk_forward_v4.py exited -11")


def test_ranker_crash_still_investigates_named_npi(monkeypatch, tmp_path, stage4):
    monkeypatch.setattr(op.rule_derivation_agent, "run", lambda **kw: None)
    monkeypatch.setattr(op, "run_stage2_ml", _ranker_crashes)
    manifest = {}
    rc = op.run_full(_args("1234567890"), tmp_path, _Sink(), manifest)
    assert rc == 0
    assert stage4["subject"] == "NPI 1234567890 ad-hoc"
    assert "exited -11" in manifest["ranking_error"]


def test_ranker_crash_without_named_npi_propagates(monkeypatch, tmp_path, stage4):
    monkeypatch.setattr(op.rule_derivation_agent, "run", lambda **kw: None)
    monkeypatch.setattr(op, "run_stage2_ml", _ranker_crashes)
    with pytest.raises(RuntimeError):
        op.run_full(_args(None), tmp_path, _Sink(), {})
    assert not stage4


def test_ranker_timeout_is_a_failure_not_a_hang(monkeypatch, tmp_path):
    def hang(cmd, **kw):
        assert kw["timeout"] == op.RANKER_TIMEOUT_SEC
        raise subprocess.TimeoutExpired(cmd, kw["timeout"])

    monkeypatch.setattr(op.subprocess, "run", hang)
    sink = _Sink()
    with pytest.raises(RuntimeError, match="timed out"):
        op.run_stage2_ml(run_dir=tmp_path, event_sink=sink, score_year=2023, filter_path=None)
    assert any(kind == op.EVENT_STAGE_FAILED for _, kind, _ in sink.events)
