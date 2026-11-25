"""FRAUD_INV_PROFILE resolution (draft / battery / production).

Pins the battery profile added for calibration-battery runs: production
MODEL_BY_STEP / EFFORT_BY_STEP split at highest precedence (a diverged
state/model_overrides.json or stray env override must not downgrade the
reasoning gates), plus the REAL-defense Phase-B red team default
(Orchestrator.defense_as_mock). Draft and no-profile behavior are pinned
UNCHANGED — the battery profile is additive.
"""
from __future__ import annotations

import pytest

from fraud_investigator import claude_p
from fraud_investigator.claude_p import (
    EFFORT_BY_STEP,
    MODEL_BY_STEP,
    _resolve_effort_for_step,
    _resolve_model_for_step,
)

ALL_STEPS = sorted(set(MODEL_BY_STEP) | set(EFFORT_BY_STEP))

# Envs that feed the resolvers — cleared per-test so the developer's shell
# (or a prior test) can't bleed into resolution.
_RESOLVER_ENVS = (
    "FRAUD_INV_PROFILE",
    "FRAUD_INV_MODEL_DEFAULT",
    "FRAUD_INV_EFFORT_DEFAULT",
    "FRAUD_INV_DEFENSE_AS_MOCK",
) + tuple(f"FRAUD_INV_MODEL_{s.upper()}" for s in ALL_STEPS) \
  + tuple(f"FRAUD_INV_EFFORT_{s.upper()}" for s in ALL_STEPS)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in _RESOLVER_ENVS:
        monkeypatch.delenv(var, raising=False)
    # Hermetic: don't let the repo's real state/model_overrides.json leak in.
    monkeypatch.setattr(claude_p, "_OVERRIDES_CACHE", {})
    yield


# ── battery: the production split, pinned ───────────────────────────────────

def test_battery_matches_production_model_split_per_step(monkeypatch):
    monkeypatch.setenv("FRAUD_INV_PROFILE", "battery")
    for step in ALL_STEPS:
        assert _resolve_model_for_step(step) == MODEL_BY_STEP.get(step), step
        assert _resolve_effort_for_step(step) == EFFORT_BY_STEP.get(step), step


def test_battery_exact_expected_values(monkeypatch):
    """The split the battery must exercise, spelled out (not just map-equality):
    Sonnet-5/max on the querying + synthesis steps; Opus on every reasoning
    gate (legal_adv_response plain opus; first_chair/disposition high)."""
    monkeypatch.setenv("FRAUD_INV_PROFILE", "battery")
    expected = {
        "investigator_discover":      ("claude-sonnet-5[1m]", "max"),
        "investigator":               ("claude-sonnet-5[1m]", "max"),
        "rebuttal_investigator":      ("claude-sonnet-5[1m]", "max"),
        "case_writer":                ("claude-sonnet-5[1m]", "high"),
        "counsel_build":              ("claude-opus-5[1m]", "max"),
        "counsel_first_chair_review": ("claude-opus-5[1m]", "high"),
        "counsel_rebuttal":           ("claude-opus-5[1m]", "max"),
        "counsel_disposition":        ("claude-opus-5[1m]", "high"),
        "defense":                    ("claude-opus-5[1m]", "max"),
        "foreclosure_check":          ("claude-opus-5[1m]", "max"),
        "legal_adv_response":         ("claude-opus-5", "max"),
    }
    for step, (model, effort) in expected.items():
        assert _resolve_model_for_step(step) == model, step
        assert _resolve_effort_for_step(step) == effort, step


def test_battery_wins_over_env_and_overrides_json(monkeypatch):
    """Highest precedence, like draft: a diverged overrides.json or stray env
    override must not downgrade a battery run's reasoning gates."""
    monkeypatch.setenv("FRAUD_INV_PROFILE", "battery")
    monkeypatch.setenv("FRAUD_INV_MODEL_DEFENSE", "haiku")
    monkeypatch.setenv("FRAUD_INV_EFFORT_DEFENSE", "low")
    monkeypatch.setenv("FRAUD_INV_MODEL_DEFAULT", "haiku")
    monkeypatch.setattr(claude_p, "_OVERRIDES_CACHE", {
        "_default": {"model": "haiku", "effort": "low"},
        "defense": {"model": "haiku", "effort": "low"},
    })
    assert _resolve_model_for_step("defense") == "claude-opus-5[1m]"
    assert _resolve_effort_for_step("defense") == "max"


def test_battery_unknown_step_inherits_cli(monkeypatch):
    monkeypatch.setenv("FRAUD_INV_PROFILE", "battery")
    assert _resolve_model_for_step("not_a_step") is None
    assert _resolve_effort_for_step("not_a_step") is None


# ── draft: unchanged ─────────────────────────────────────────────────────────

def test_draft_unchanged_all_steps_sonnet_medium(monkeypatch):
    monkeypatch.setenv("FRAUD_INV_PROFILE", "draft")
    for step in ALL_STEPS + ["not_a_step"]:
        assert _resolve_model_for_step(step) == "sonnet", step
        assert _resolve_effort_for_step(step) == "medium", step


# ── no profile: unchanged ────────────────────────────────────────────────────

def test_no_profile_unchanged_defaults(monkeypatch):
    for step in ALL_STEPS:
        assert _resolve_model_for_step(step) == MODEL_BY_STEP.get(step), step
        assert _resolve_effort_for_step(step) == EFFORT_BY_STEP.get(step), step


def test_no_profile_env_override_still_wins(monkeypatch):
    """Without a profile, the normal resolution ladder is untouched: per-step
    env > overrides.json > ... (the pre-battery behavior)."""
    monkeypatch.setenv("FRAUD_INV_MODEL_DEFENSE", "haiku")
    assert _resolve_model_for_step("defense") == "haiku"


# ── battery ⇒ the REAL defense red team (Phase-B defense_as_mock default) ────

def _orch(tmp_path, **kw):
    from fraud_investigator.orchestrator import Orchestrator
    return Orchestrator(subject="Profile Test Subject", state_dir=tmp_path,
                        dry_run=True, **kw)


def test_battery_defaults_defense_as_mock_on(monkeypatch, tmp_path):
    monkeypatch.setenv("FRAUD_INV_PROFILE", "battery")
    assert _orch(tmp_path).defense_as_mock is True


def test_production_default_defense_as_mock_off(monkeypatch, tmp_path):
    assert _orch(tmp_path).defense_as_mock is False


def test_draft_default_defense_as_mock_off(monkeypatch, tmp_path):
    monkeypatch.setenv("FRAUD_INV_PROFILE", "draft")
    assert _orch(tmp_path).defense_as_mock is False


def test_explicit_env_beats_battery_default(monkeypatch, tmp_path):
    monkeypatch.setenv("FRAUD_INV_PROFILE", "battery")
    monkeypatch.setenv("FRAUD_INV_DEFENSE_AS_MOCK", "0")
    assert _orch(tmp_path).defense_as_mock is False


def test_explicit_kwarg_beats_battery_default(monkeypatch, tmp_path):
    monkeypatch.setenv("FRAUD_INV_PROFILE", "battery")
    assert _orch(tmp_path, defense_as_mock=False).defense_as_mock is False


def test_env_flag_still_enables_without_profile(monkeypatch, tmp_path):
    monkeypatch.setenv("FRAUD_INV_DEFENSE_AS_MOCK", "1")
    assert _orch(tmp_path).defense_as_mock is True
