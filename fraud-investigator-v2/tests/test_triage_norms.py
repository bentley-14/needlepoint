"""Computed triage norms — scripts/compute_triage_norms.py + the triage agent's
{computed_norms} prompt substitution.

Synthetic parquet only (no real data, no network, no subprocess): compute_norms is
driven with a tiny tmp_path panel; the prompt substitution is driven through
_build_batch_prompt with NP_TRIAGE_NORMS_DIR pointed at tmp_path.
"""
from __future__ import annotations

import json

import duckdb
import pandas as pd
import pytest

from fraud_investigator.agents.triage import (
    _NO_NORMS_FALLBACK,
    _build_batch_prompt,
    _render_computed_norms,
)
from scripts.compute_triage_norms import compute_norms


def _mk_panel(path):
    """Synthetic feature panel with known per-specialty stats.

    - Podiatry: 250 providers — 200 at opioid 0.0 + 50 at 0.5 (median 0.0; p95 = 0.5,
      since quantile_cont index 0.95*(250-1)=236.55 lands inside the 0.5 block);
      dual 0.2 everywhere.
    - Cardiology: 250 providers — 100 with has_partd=0 and opioid 0.9 (MUST be
      excluded from opioid stats) + 150 with has_partd=1 and opioid 0.1; dual 0.1.
    - Dermatology: 50 providers — below the 200-provider floor, excluded.
    - '0.0': 300 rows with a numeric-fill specialty, excluded.
    """
    rows = []
    for i in range(250):
        rows.append(dict(npi=f"1{i:09d}", partb_provider_type="Podiatry",
                         partd_opioid_clm_share=0.0 if i < 200 else 0.5,
                         partb_dual_pct=0.2, has_partd=1))
    for i in range(250):
        rows.append(dict(npi=f"2{i:09d}", partb_provider_type="Cardiology",
                         partd_opioid_clm_share=0.9 if i < 100 else 0.1,
                         partb_dual_pct=0.1, has_partd=0 if i < 100 else 1))
    for i in range(50):
        rows.append(dict(npi=f"3{i:09d}", partb_provider_type="Dermatology",
                         partd_opioid_clm_share=0.3, partb_dual_pct=0.3, has_partd=1))
    for i in range(300):
        rows.append(dict(npi=f"4{i:09d}", partb_provider_type="0.0",
                         partd_opioid_clm_share=0.7, partb_dual_pct=0.7, has_partd=1))
    pd.DataFrame(rows).to_parquet(path)


def test_compute_norms_per_specialty_stats(tmp_path):
    panel = tmp_path / "features_full_2099.parquet"
    _mk_panel(panel)
    norms = compute_norms(str(panel), 2099, con=duckdb.connect())

    specs = norms["specialties"]
    # ≥200-provider floor + numeric-fill specialty exclusion
    assert set(specs) == {"Podiatry", "Cardiology"}

    pod = specs["Podiatry"]
    assert pod["n_providers"] == 250
    assert pod["n_opioid"] == 250
    assert pod["opioid_share_median"] == pytest.approx(0.0)
    assert pod["opioid_share_p95"] == pytest.approx(0.5)
    assert pod["dual_share_median"] == pytest.approx(0.2)

    # has_partd=0 rows (opioid 0.9) are excluded from the opioid stats
    card = specs["Cardiology"]
    assert card["n_providers"] == 250
    assert card["n_opioid"] == 150
    assert card["opioid_share_median"] == pytest.approx(0.1)
    assert card["opioid_share_p95"] == pytest.approx(0.1)
    assert card["dual_share_median"] == pytest.approx(0.1)

    assert norms["_meta"]["year"] == 2099
    assert norms["_meta"]["min_providers"] == 200
    assert "has_partd" in norms["_meta"]["opioid_basis"]


def test_compute_norms_floor_is_parameterized(tmp_path):
    panel = tmp_path / "features_full_2099.parquet"
    _mk_panel(panel)
    norms = compute_norms(str(panel), 2099, min_providers=40, con=duckdb.connect())
    assert "Dermatology" in norms["specialties"]
    assert norms["specialties"]["Dermatology"]["n_providers"] == 50


def _norms_json():
    return {
        "_meta": {"year": 2099, "source": "synthetic.parquet", "min_providers": 200,
                  "opioid_basis": "Part D-active providers (has_partd = 1)"},
        "specialties": {
            "Podiatry": {"n_providers": 250, "n_opioid": 250,
                         "opioid_share_median": 0.0, "opioid_share_p95": 0.5,
                         "dual_share_median": 0.2},
        },
    }


def test_build_batch_prompt_substitutes_computed_norms(tmp_path, monkeypatch):
    monkeypatch.setenv("NP_TRIAGE_NORMS_DIR", str(tmp_path))
    (tmp_path / "norms_2099.json").write_text(json.dumps(_norms_json()))
    base = "NORMS:\n{computed_norms}\nYEAR={score_year}"
    prompt = _build_batch_prompt(base, [], "panel.parquet", 2099, 1, 50)
    assert "{computed_norms}" not in prompt
    assert "| Podiatry | 250 | 0.0000 | 0.5000 | 0.2000 |" in prompt
    assert "compute_triage_norms.py" in prompt
    assert _NO_NORMS_FALLBACK not in prompt


def test_build_batch_prompt_fallback_without_norms_file(tmp_path, monkeypatch):
    monkeypatch.setenv("NP_TRIAGE_NORMS_DIR", str(tmp_path))  # empty dir — no norms file
    base = "NORMS:\n{computed_norms}"
    prompt = _build_batch_prompt(base, [], "panel.parquet", 2099, 1, 50)
    assert "{computed_norms}" not in prompt
    assert _NO_NORMS_FALLBACK in prompt


def test_render_computed_norms_explicit_path_and_bad_json(tmp_path):
    # explicit norms_path parameter (production lookup stays overridable)
    p = tmp_path / "custom.json"
    p.write_text(json.dumps(_norms_json()))
    out = _render_computed_norms(2099, norms_path=p)
    assert "Podiatry" in out
    # unreadable/corrupt file → fallback, never a crash
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert _render_computed_norms(2099, norms_path=bad) == _NO_NORMS_FALLBACK
