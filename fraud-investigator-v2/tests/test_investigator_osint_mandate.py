"""Regression: the OSINT re-injection block lives in run(), not run_discover().

It was once pasted into run_discover(), which has no ``osint_mandate`` parameter, so every
case crashed with NameError at the iter-1 discovery pass and the mandate never fired in run().
"""
from __future__ import annotations

from fraud_investigator.agents import investigator


def _capture(monkeypatch):
    seen = {}
    monkeypatch.setattr(investigator, "invoke_agent", lambda **kw: seen.update(kw) or "ok")
    return seen


def test_run_discover_does_not_crash(monkeypatch, tmp_path):
    seen = _capture(monkeypatch)
    investigator.run_discover(subject="Jane Doe NPI 1234567890", case_id="CASE-TEST", out_dir=tmp_path)
    assert "MANDATORY THIS ITERATION" not in seen["prompt"]


def test_run_injects_mandate_only_when_requested(monkeypatch, tmp_path):
    seen = _capture(monkeypatch)
    kw = dict(subject="Jane Doe", case_id="CASE-TEST", iteration="2", prior_counsel_disposition=None,
              existing_evidence=[], out_dir=tmp_path)
    investigator.run(**kw)
    assert "MANDATORY THIS ITERATION" not in seen["prompt"]
    investigator.run(**kw, osint_mandate=True)
    assert "MANDATORY THIS ITERATION" in seen["prompt"]
