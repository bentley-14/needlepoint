"""Tests for R2 fallback wiring in investigator data-lookup tools.

Two layers:
  1. R2 fallback assertion — parquet_source("/nonexistent/x.parquet", key) returns s3:// URL.
  2. Per-tool CLI smoke tests — each tool on NPI 1900000075 (Cedar) exits 0 and produces
     non-empty output. Tests are skipped gracefully when the required local panel/year is
     absent (calibration machines with no local panels still pass).

Run: pytest tests/test_tools_r2_wiring.py -v
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
# Use the running interpreter by default (CI-safe — the targeted CI env has
# the tools' import-time deps). Override with FRAUD_TOOLS_PYTHON to exercise
# a different env (e.g. the anaconda env the agents actually run under).
PYTHON = os.environ.get("FRAUD_TOOLS_PYTHON", sys.executable)
CEDAR_NPI = "1900000075"


# ── 1. R2 fallback primitive assertion ───────────────────────────────────────

def test_parquet_source_returns_s3_url_on_miss():
    """parquet_source for a nonexistent local path must return an s3:// URL."""
    sys.path.insert(0, str(REPO))
    from fraudlib.r2 import parquet_source, R2_PANELS
    url = parquet_source("/nonexistent/cms_partb_2022.parquet",
                         f"{R2_PANELS}/cms_partb_2022.parquet")
    assert url.startswith("s3://"), f"Expected s3:// URL, got: {url!r}"
    assert "needlepoint-data" in url
    assert "cms_partb_2022.parquet" in url


def test_parquet_source_r2_first_returns_s3_even_when_local_exists(tmp_path, monkeypatch):
    """DEFAULT R2-first: with R2 configured, return the s3:// URL even when a local
    copy exists — R2 is the source of truth."""
    sys.path.insert(0, str(REPO))
    import fraudlib.r2 as r2
    monkeypatch.setattr(r2, "_r2_configured", lambda: True)
    monkeypatch.delenv("NP_R2_DISABLE", raising=False)
    monkeypatch.delenv("NP_R2_FIRST", raising=False)  # default ON
    f = tmp_path / "cms_partb_2022.parquet"
    f.write_bytes(b"stub")
    result = r2.parquet_source(f, f"{r2.R2_PANELS}/cms_partb_2022.parquet")
    assert result.startswith("s3://"), f"R2-first should return s3://, got: {result!r}"


def test_parquet_source_local_first_mode_returns_local(tmp_path, monkeypatch):
    """r2_first=False (and NP_R2_FIRST=0) → local path when present (local-first;
    the calibration builder's staging mode)."""
    sys.path.insert(0, str(REPO))
    import fraudlib.r2 as r2
    monkeypatch.setattr(r2, "_r2_configured", lambda: True)
    monkeypatch.delenv("NP_R2_DISABLE", raising=False)
    f = tmp_path / "cms_partb_2022.parquet"
    f.write_bytes(b"stub")
    assert r2.parquet_source(f, f"{r2.R2_PANELS}/x.parquet", r2_first=False) == str(f)
    monkeypatch.setenv("NP_R2_FIRST", "0")
    assert r2.parquet_source(f, f"{r2.R2_PANELS}/x.parquet") == str(f)


def test_parquet_source_r2_first_falls_back_to_local_when_unconfigured(tmp_path, monkeypatch):
    """R2-first but R2 not set up (no creds) → local path (fallback)."""
    sys.path.insert(0, str(REPO))
    import fraudlib.r2 as r2
    monkeypatch.setattr(r2, "_r2_configured", lambda: False)
    monkeypatch.delenv("NP_R2_DISABLE", raising=False)
    monkeypatch.setenv("NP_R2_FIRST", "1")
    f = tmp_path / "cms_partb_2022.parquet"
    f.write_bytes(b"stub")
    assert r2.parquet_source(f, f"{r2.R2_PANELS}/x.parquet") == str(f)


def test_fetch_local_r2_first_pulls_from_r2(tmp_path, monkeypatch):
    """R2-first fetch_local copies from R2 into the cache and returns it, even when a
    local copy exists (R2 = source of truth)."""
    sys.path.insert(0, str(REPO))
    import fraudlib.r2 as r2
    monkeypatch.setattr(r2, "_r2_configured", lambda: True)
    monkeypatch.delenv("NP_R2_DISABLE", raising=False)
    monkeypatch.delenv("NP_R2_FIRST", raising=False)
    cache = tmp_path / "cache"
    monkeypatch.setenv("NP_R2_TABLE_CACHE", str(cache))
    seen = {}

    def fake_copy(key, dest):
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Path(dest).write_bytes(b"r2-data")
        seen["key"] = key

    monkeypatch.setattr(r2, "r2_copy", fake_copy)
    local = tmp_path / "hcpcs_codes.parquet"
    local.write_bytes(b"local-data")
    got = r2.fetch_local(local, "r2:needlepoint-data/ground_truth/v1/hcpcs_codes.parquet")
    assert got == cache / "hcpcs_codes.parquet"
    assert seen["key"].endswith("hcpcs_codes.parquet")


def test_fetch_local_r2_first_falls_back_to_local_on_failure(tmp_path, monkeypatch):
    """If the R2 fetch raises, fetch_local returns the local copy (fallback)."""
    sys.path.insert(0, str(REPO))
    import fraudlib.r2 as r2
    monkeypatch.setattr(r2, "_r2_configured", lambda: True)
    monkeypatch.delenv("NP_R2_DISABLE", raising=False)
    monkeypatch.delenv("NP_R2_FIRST", raising=False)
    monkeypatch.setenv("NP_R2_TABLE_CACHE", str(tmp_path / "cache"))
    monkeypatch.setattr(r2, "r2_copy", lambda k, d: (_ for _ in ()).throw(RuntimeError("R2 down")))
    local = tmp_path / "hcpcs_codes.parquet"
    local.write_bytes(b"local-data")
    assert r2.fetch_local(local, "r2:needlepoint-data/ground_truth/v1/hcpcs_codes.parquet") == local


def test_fetch_local_disabled_wins_over_r2_first(tmp_path, monkeypatch):
    """NP_R2_DISABLE=1 overrides R2-first: local if present, else raise — never R2."""
    sys.path.insert(0, str(REPO))
    import fraudlib.r2 as r2
    monkeypatch.setattr(r2, "_r2_configured", lambda: True)
    monkeypatch.setenv("NP_R2_DISABLE", "1")
    monkeypatch.setattr(r2, "r2_copy", lambda k, d: (_ for _ in ()).throw(AssertionError("touched R2 while disabled")))
    local = tmp_path / "x.parquet"
    local.write_bytes(b"local-data")
    assert r2.fetch_local(local, "r2:needlepoint-data/x.parquet") == local
    with pytest.raises(FileNotFoundError):
        r2.fetch_local(tmp_path / "absent.parquet", "r2:needlepoint-data/absent.parquet")


# ── helper ───────────────────────────────────────────────────────────────────

def _run(*args: str, input_text: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [PYTHON, *args],
        capture_output=True,
        text=True,
        cwd=str(REPO),
        input=input_text,
        timeout=120,
        env={**__import__("os").environ, "PYTHONPATH": str(REPO)},
    )


def _skip_if_no_panels(family: str, year: int = 2022) -> None:
    """Skip this test when no local panel exists for the required family + year."""
    proc = REPO / "data" / "processed" / f"{family}_{year}.parquet"
    panels = REPO / "data" / "panels" / f"{family}_{year}.parquet"
    if not proc.exists() and not panels.exists():
        pytest.skip(f"No local panel: {family}_{year}.parquet")


# ── 2. Per-tool CLI smoke tests ───────────────────────────────────────────────

def test_provider_trajectory_cedar():
    _skip_if_no_panels("cms_partb", 2022)
    r = _run("scripts/provider_trajectory.py", "--npi", CEDAR_NPI)
    assert r.returncode == 0, f"stderr: {r.stderr}"
    assert CEDAR_NPI in r.stdout
    assert "B_paid" in r.stdout  # header present


def test_dump_hcpcs_breakdown_cedar():
    _skip_if_no_panels("cms_partb_svc", 2022)
    r = _run("scripts/dump_hcpcs_breakdown.py", "--npi", CEDAR_NPI, "--year", "2022")
    assert r.returncode == 0, f"stderr: {r.stderr}"
    assert "HCPCS BREAKDOWN" in r.stdout
    assert CEDAR_NPI in r.stdout


def test_opioid_metrics_cedar():
    _skip_if_no_panels("cms_partd_prescriber_drug", 2022)
    r = _run("scripts/opioid_metrics.py", "--npi", CEDAR_NPI)
    assert r.returncode == 0, f"stderr: {r.stderr}"
    assert "opioid" in r.stdout.lower()
    assert CEDAR_NPI in r.stdout


def test_address_sweep_cedar():
    _skip_if_no_panels("cms_partb", 2022)
    r = _run("scripts/address_sweep.py", "--npi", CEDAR_NPI)
    assert r.returncode == 0, f"stderr: {r.stderr}"
    assert CEDAR_NPI in r.stdout


def test_hcpcs_lookup_code():
    hcpcs = REPO / "data" / "ground_truth" / "hcpcs_codes.parquet"
    if not hcpcs.exists():
        pytest.skip("No local hcpcs_codes.parquet")
    r = _run("scripts/hcpcs_lookup.py", "--code", "J3304")
    assert r.returncode == 0, f"stderr: {r.stderr}"
    assert "J3304" in r.stdout
    assert "triamcinolone" in r.stdout.lower()


def test_lcd_lookup_keyword():
    lcd = REPO / "data" / "processed" / "lcd_current.parquet"
    panels_lcd = REPO / "data" / "panels" / "lcd_current.parquet"
    if not lcd.exists() and not panels_lcd.exists():
        pytest.skip("No local lcd_current.parquet")
    r = _run("scripts/lcd_lookup.py", "--keyword", "opioid")
    assert r.returncode == 0, f"stderr: {r.stderr}"
    # Either we get LCD rows or "(no matching determinations)" — both valid
    assert r.stdout.strip() != ""


def test_clia_lookup_state():
    clia = REPO / "data" / "processed" / "clia_disciplinary.parquet"
    panels_clia = REPO / "data" / "panels" / "clia_disciplinary.parquet"
    if not clia.exists() and not panels_clia.exists():
        pytest.skip("No local clia_disciplinary.parquet")
    r = _run("scripts/clia_lookup.py", "--state", "IN")
    assert r.returncode == 0, f"stderr: {r.stderr}"
    assert "CLIA" in r.stdout


def test_fsmb_lookup_cedar():
    fsmb = REPO / "data" / "processed" / "state_board_actions_fsmb.parquet"
    panels_fsmb = REPO / "data" / "panels" / "state_board_actions_fsmb.parquet"
    if not fsmb.exists() and not panels_fsmb.exists():
        pytest.skip("No local state_board_actions_fsmb.parquet")
    r = _run("scripts/fsmb_lookup.py", "--npi", CEDAR_NPI)
    assert r.returncode == 0, f"stderr: {r.stderr}"
    assert "FSMB" in r.stdout


def test_revoked_provider_lookup_cedar():
    rev = REPO / "data" / "processed" / "revoked_medicare_providers.parquet"
    panels_rev = REPO / "data" / "panels" / "revoked_medicare_providers.parquet"
    if not rev.exists() and not panels_rev.exists():
        pytest.skip("No local revoked_medicare_providers.parquet")
    r = _run("scripts/revoked_provider_lookup.py", "--npi", CEDAR_NPI)
    assert r.returncode == 0, f"stderr: {r.stderr}"
    assert "Revoked" in r.stdout


def test_ca_exclusion_lookup_cedar():
    si = REPO / "data" / "panels" / "ca_si_list.parquet"
    if not si.exists():
        pytest.skip("No local ca_si_list.parquet")
    r = _run("scripts/ca_exclusion_lookup.py", "--npi", CEDAR_NPI)
    assert r.returncode == 0, f"stderr: {r.stderr}"
    assert "California" in r.stdout


def test_check_billing_surface_cedar():
    _skip_if_no_panels("cms_partb", 2022)
    r = _run("scripts/check_billing_surface.py", "--npi", CEDAR_NPI, "--years", "2022,2023")
    assert r.returncode == 0, f"stderr: {r.stderr}"
    assert "SURFACE_FOUND" in r.stdout
    assert CEDAR_NPI in r.stdout
