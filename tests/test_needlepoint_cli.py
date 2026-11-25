"""needlepoint.py: knob -> environment mapping, setup checks, NPI subject formatting."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("needlepoint_cli", REPO / "needlepoint.py")
np_cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(np_cli)


def _s(**kw):
    s = {"backend": "api", "model": None, "tool_model": "anthropic/claude-sonnet-5",
         "reason_model": "anthropic/claude-opus-5", "effort": None, "depth": "quick", "year": 2024, "api_base": None}
    s.update(kw)
    return s


def test_api_backend_sets_per_step_models():
    env = np_cli.run_env(_s())
    assert env["FRAUD_INV_EXEC_BACKEND"] == "api"
    assert env["FRAUD_INV_API_MODEL_INVESTIGATOR"] == "anthropic/claude-sonnet-5"
    assert env["FRAUD_INV_API_MODEL_DEFENSE"] == "anthropic/claude-opus-5"


def test_single_model_overrides_split_and_effort_applies():
    env = np_cli.run_env(_s(model="openai/gpt-5", effort="high"))
    assert env["FRAUD_INV_API_MODEL_INVESTIGATOR"] == env["FRAUD_INV_API_MODEL_DEFENSE"] == "openai/gpt-5"
    assert env["FRAUD_INV_EFFORT_DEFENSE"] == "high"


def test_claude_backend_strips_provider_prefix():
    env = np_cli.run_env(_s(backend="claude"))
    assert env["FRAUD_INV_EXEC_BACKEND"] == "claude_p"
    assert env["FRAUD_INV_MODEL_INVESTIGATOR"] == "claude-sonnet-5"
    assert "FRAUD_INV_API_MODEL_INVESTIGATOR" not in env or env.get("FRAUD_INV_API_MODEL_INVESTIGATOR") is None


def test_claude_backend_rejects_non_claude_models(monkeypatch):
    monkeypatch.setattr(np_cli, "claude_cli", lambda: "/usr/bin/claude")
    monkeypatch.setattr(np_cli, "data_status", lambda y: {"cms_partb": "local"})
    issues = np_cli.problems(_s(backend="claude", model="openai/gpt-5"))
    assert any("needs --backend api" in i for i in issues)


def test_missing_data_and_key_are_reported(monkeypatch):
    monkeypatch.setattr(np_cli, "available_providers", lambda: [])
    monkeypatch.setattr(np_cli, "data_status", lambda y: {"cms_partb": "missing", "cms_partb_svc": "missing"})
    issues = " ".join(np_cli.problems(_s()))
    assert "no model key" in issues and "fetch --year 2024" in issues


def test_subject_string():
    p = {"npi": "1234567890", "name": "Jane Doe", "credential": "MD", "specialty": "Internal Medicine",
         "city": "Anytown", "state": "OH", "is_org": False}
    assert np_cli.subject_string(p) == "Jane Doe, MD -- Internal Medicine -- Anytown, OH -- NPI 1234567890"


@pytest.mark.parametrize("depth,iters", [("quick", 2), ("standard", 3), ("full", 3)])
def test_depth_never_below_two_build_iterations(depth, iters):
    assert np_cli.DEPTHS[depth][0] == iters >= 2
