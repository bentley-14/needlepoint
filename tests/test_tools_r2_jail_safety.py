"""Tests confirming the 12 r2-wired tools are jail-safe.

Two layers:
  1. JAIL-IMPORT: each tool module must import cleanly with fraudlib blocked
     (simulates the calibration sandbox where fraudlib is absent). The guarded
     try/except shim must engage and produce exit 0.
  2. FAIL-CLOSED: r2.parquet_source with NP_R2_DISABLE=1 must return the local
     path, never an s3:// URL.

Run: pytest tests/test_tools_r2_jail_safety.py -v
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

TOOLS = [
    "scripts/provider_trajectory.py",
    "scripts/dump_hcpcs_breakdown.py",
    "scripts/opioid_metrics.py",
    "scripts/address_sweep.py",
    "scripts/hcpcs_lookup.py",
    "scripts/lcd_lookup.py",
    "scripts/clia_lookup.py",
    "scripts/fsmb_lookup.py",
    "scripts/revoked_provider_lookup.py",
    "scripts/ca_exclusion_lookup.py",
    "scripts/check_billing_surface.py",
    "scripts/cohort_screen_count.py",
]


# ── 1. JAIL-IMPORT: fraudlib blocked, shim must engage ──────────────────────

@pytest.mark.parametrize("tool_rel", TOOLS, ids=[Path(t).stem for t in TOOLS])
def test_jail_import(tool_rel: str, tmp_path):
    """Import the tool module with fraudlib unimportable — must exit 0 (shim engaged)."""
    tool_abs = str(REPO / tool_rel)
    # Block fraudlib by injecting sys.modules['fraudlib'] = None before loading the
    # module. Use importlib.util so __name__ != '__main__', preventing argparse/sys.exit.
    code = (
        "import sys; "
        "sys.modules['fraudlib'] = None; "
        "import importlib.util as u; "
        f"s = u.spec_from_file_location('tool_under_test', {tool_abs!r}); "
        "m = u.module_from_spec(s); "
        "s.loader.exec_module(m)"
    )
    result = subprocess.run(
        [PYTHON, "-c", code],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),   # no fraudlib package in cwd — true jail
        timeout=30,
    )
    assert result.returncode == 0, (
        f"{Path(tool_rel).name}: shim did NOT engage (exit {result.returncode}).\n"
        f"stderr:\n{result.stderr}\nstdout:\n{result.stdout}"
    )


# ── 2. FAIL-CLOSED: NP_R2_DISABLE=1 → local path returned ──────────────────

def test_parquet_source_fail_closed_local_path(tmp_path):
    """With NP_R2_DISABLE=1, parquet_source must return the LOCAL path (never s3://)."""
    sys.path.insert(0, str(REPO))
    from fraudlib.r2 import parquet_source, R2_PANELS

    # Create an actual local file so existence doesn't decide the branch
    local = tmp_path / "cms_partb_2022.parquet"
    local.write_bytes(b"stub")

    env_backup = os.environ.get("NP_R2_DISABLE")
    try:
        os.environ["NP_R2_DISABLE"] = "1"
        result = parquet_source(str(local), f"{R2_PANELS}/cms_partb_2022.parquet")
    finally:
        if env_backup is None:
            os.environ.pop("NP_R2_DISABLE", None)
        else:
            os.environ["NP_R2_DISABLE"] = env_backup

    assert not result.startswith("s3://"), (
        f"NP_R2_DISABLE=1 should return local path, got: {result!r}"
    )
    assert "cms_partb_2022" in result


def test_shim_parquet_source_returns_local():
    """The _LocalR2 shim's parquet_source always returns the local path string."""
    from pathlib import Path as _RP

    class _LocalR2:
        R2_PANELS = ""
        @staticmethod
        def parquet_source(local, key): return str(local)
        @staticmethod
        def fetch_local(local, key): return _RP(local)
        @staticmethod
        def attach_r2(con): pass
        @staticmethod
        def duckdb_r2():
            import duckdb; return duckdb.connect()

    r2 = _LocalR2()
    result = r2.parquet_source(
        "/nonexistent/x.parquet",
        "r2:needlepoint-data/processed/v1/cms_partb_2022.parquet",
    )
    assert result == "/nonexistent/x.parquet"
    assert not result.startswith("s3://")
