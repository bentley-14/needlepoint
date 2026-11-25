"""Leak-safe Medicaid cutoff helper — pure-function tests."""
from __future__ import annotations

import importlib

import pytest

MOD = "fraudlib.medicaid_cutoff"


@pytest.fixture
def mc(monkeypatch):
    """Fresh module each test so ENV reads are deterministic."""
    monkeypatch.delenv("FRAUD_INV_MEDICAID_CUTOFF", raising=False)
    import fraudlib.medicaid_cutoff as m
    return importlib.reload(m)


def test_unset_is_inert(mc):
    assert mc.cutoff_iso() is None
    assert mc.cutoff_ym() is None
    assert mc.cutoff_year() is None
    assert mc.claim_clause() == ""              # production: no gating
    assert mc.feature_year_ok(2024) is True     # everything admissible
    assert mc.clamp_feature_year(2024) == 2024  # unchanged
    assert mc.banner() is None


def test_set_gates(monkeypatch, mc):
    monkeypatch.setenv("FRAUD_INV_MEDICAID_CUTOFF", "2024-01-01")
    m = importlib.reload(mc)
    assert m.cutoff_ym() == "2024-01"
    assert m.cutoff_year() == 2024
    assert m.claim_clause() == " AND CLAIM_FROM_MONTH < '2024-01'"
    assert m.claim_clause("t.CLAIM_FROM_MONTH", prefix="WHERE") == \
        " WHERE t.CLAIM_FROM_MONTH < '2024-01'"


def test_feature_year_gate_and_clamp(monkeypatch, mc):
    monkeypatch.setenv("FRAUD_INV_MEDICAID_CUTOFF", "2024-01-01")
    m = importlib.reload(mc)
    assert m.feature_year_ok(2023) is True      # Y < cutoff_year -> ok
    assert m.feature_year_ok(2024) is False     # Y == cutoff_year -> describes through Dec 2024
    assert m.feature_year_ok(2025) is False
    assert m.clamp_feature_year(2024) == 2023   # requested post-cutoff -> newest admissible
    assert m.clamp_feature_year(2022) == 2022   # already pre-cutoff -> unchanged


def test_blank_env_treated_as_unset(monkeypatch, mc):
    monkeypatch.setenv("FRAUD_INV_MEDICAID_CUTOFF", "   ")
    m = importlib.reload(mc)
    assert m.cutoff_iso() is None and m.claim_clause() == ""
