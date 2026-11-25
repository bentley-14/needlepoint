"""Falsity-anchor existence gate (failure_mode_taxonomy_2026-07.md #2).

Hermetic — monkeypatches lcd_lookup._load with tiny in-memory DataFrames so the
test never touches the real (R2-backed) LCD/NCD/article parquets.

Run: pytest tests/test_anchor_check.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pandas as pd

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import anchor_check as AC  # noqa: E402
import lcd_lookup  # noqa: E402


def _df(rows, id_col):
    if not rows:
        return pd.DataFrame(columns=[id_col, "title", "hcpcs_codes", "mac_jurisdictions"])
    return pd.DataFrame(rows)


_LCD_ROWS = [{"lcd_id": "34645", "lcd_version": "1", "title": "Reflex Definitive UDT",
              "hcpcs_codes": ["G0483"], "mac_jurisdictions": ["J6"],
              "status": "A", "coverage_text": "…", "orig_eff_date": "2020-01-01",
              "rev_eff_date": "2020-01-01", "end_date": None, "last_updated": "2024-01-01"}]
_NCD_ROWS = []
_ARTICLE_ROWS = [{"article_id": "53441", "article_version": "1",
                   "title": "Amniotic Membrane Billing Guidelines",
                   "hcpcs_codes": ["65778"], "mac_jurisdictions": ["J6"],
                   "status": "A", "eff_date": "2021-01-01", "end_date": None,
                   "last_updated": "2024-01-01"}]


def _fake_load(source, scope):
    if source == "lcd":
        return _df(_LCD_ROWS, "lcd_id")
    if source == "ncd":
        return _df(_NCD_ROWS, "ncd_id")
    return _df(_ARTICLE_ROWS, "article_id")


def test_lcd_exists_for_covered_code():
    with patch.object(lcd_lookup, "_load", side_effect=_fake_load):
        result = AC.check_anchor("G0483")
    assert result["lcd_exists"] is True
    assert result["ncd_exists"] is False
    assert result["article_only"] is False
    assert result["lcds"][0]["id"] == "34645"


def test_article_only_when_no_lcd_or_ncd():
    """The real Sohal/Eye Clinic Chestnut (CASE-6A4588BF) finding: 65778 has no LCD/NCD,
    only a billing-guidance article — article_only must be True."""
    with patch.object(lcd_lookup, "_load", side_effect=_fake_load):
        result = AC.check_anchor("65778")
    assert result["lcd_exists"] is False
    assert result["ncd_exists"] is False
    assert result["article_only"] is True
    assert result["articles"][0]["id"] == "53441"


def test_no_anchor_at_all_still_reports_per_se_candidates():
    with patch.object(lcd_lookup, "_load", side_effect=_fake_load):
        result = AC.check_anchor("99999")
    assert result["lcd_exists"] is False
    assert result["ncd_exists"] is False
    assert result["article_only"] is False
    assert result["per_se_candidates"], "per_se_candidates must always be non-empty (surfaced fallback)"


def test_mac_filter_narrows_lcd_hits():
    with patch.object(lcd_lookup, "_load", side_effect=_fake_load):
        in_jx = AC.check_anchor("G0483", mac="J6")
        out_jx = AC.check_anchor("G0483", mac="J3")
    assert in_jx["lcd_exists"] is True
    assert out_jx["lcd_exists"] is False


def test_effective_on_excludes_not_yet_in_force_determination():
    """Calibration/point-in-time safety: an LCD effective AFTER the as-of date
    must not count as an existing anchor (whether a rule exists can itself be
    a post-cutoff fact)."""
    with patch.object(lcd_lookup, "_load", side_effect=_fake_load):
        before = AC.check_anchor("G0483", effective_on="2019-06-01")
        after = AC.check_anchor("G0483", effective_on="2021-06-01")
    assert before["lcd_exists"] is False, "LCD effective 2020-01-01 must not exist as of 2019"
    assert after["lcd_exists"] is True
    assert before["effective_on"] == "2019-06-01"


def test_render_text_flags_missing_anchor():
    with patch.object(lcd_lookup, "_load", side_effect=_fake_load):
        result = AC.check_anchor("99999")
    text = AC.render_text(result)
    assert "NO LCD/NCD/article found" in text
