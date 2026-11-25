"""Year enumeration from R2 — clean-machine support for the year-loop tools.

Year-loop tools (provider_trajectory, dump_hcpcs_breakdown, opioid_metrics) used to
derive their year list from a LOCAL glob, so on a box with no local mirror they found
zero years. They now union the local glob with `r2.list_v1_years(family)` (R2 = source
of truth). These tests pin: R2 enumeration works, it's fail-closed in calibration, and
a tool with no local panels falls back to the full R2 span.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))


def test_list_v1_years_fail_closed(monkeypatch):
    """NP_R2_DISABLE=1 (calibration) and unconfigured R2 both return [] — so callers
    union with local and the jail stays local-only. Deterministic (no network)."""
    import fraudlib.r2 as r2
    monkeypatch.setenv("NP_R2_DISABLE", "1")
    assert r2.list_v1_years("cms_partb") == []
    monkeypatch.delenv("NP_R2_DISABLE", raising=False)
    monkeypatch.setattr(r2, "_r2_configured", lambda: False)
    assert r2.list_v1_years("cms_partb") == []


def test_list_v1_years_enumerates_r2(monkeypatch):
    """R2 lists the canonical year span for a family (and the substring guard keeps
    cms_partb_svc out of cms_partb)."""
    import fraudlib.r2 as r2
    monkeypatch.delenv("NP_R2_DISABLE", raising=False)
    if not r2._r2_configured() or not r2._v1_listing():
        pytest.skip("no reachable bucket with processed/v1 panels configured on this machine")
    yrs = r2.list_v1_years("cms_partb")
    assert yrs, "expected a non-empty R2 year list for cms_partb"
    assert min(yrs) <= 2015 and max(yrs) >= 2021, f"unexpected R2 years: {yrs}"
    # every entry is a plain year int (svc files excluded by the digit guard)
    assert all(2000 < y < 2100 for y in yrs)


def test_clean_machine_year_enum_falls_back_to_r2(monkeypatch, tmp_path):
    """With NO local panels (PROC -> empty dir), a year-loop tool enumerates the full
    span from R2 — the clean-machine path."""
    import fraudlib.r2 as r2
    monkeypatch.delenv("NP_R2_DISABLE", raising=False)
    if not r2._r2_configured() or not r2._v1_listing():
        pytest.skip("no reachable bucket with processed/v1 panels configured on this machine")
    sys.path.insert(0, str(REPO / "scripts"))
    pt = importlib.import_module("provider_trajectory")
    monkeypatch.setattr(pt, "PROC", tmp_path)  # simulate a box with no local mirror
    yrs = pt._years("cms_partb")
    assert yrs == r2.list_v1_years("cms_partb"), "clean-machine _years should equal the R2 span"
    assert yrs, "clean-machine year enumeration must be non-empty"
