"""Coverage-anchor registry tests — the fileability map must answer correctly
for the calibration-validated families."""
from __future__ import annotations

from fraudlib.anchors import anchor_for_code, families, fileable


def test_registry_loads_and_has_verified_families():
    fams = families()
    assert "definitive_udt" in fams and "em_office_visits" in fams
    # every entry must carry the verified_by provenance (the 99489 rule)
    for name, fam in fams.items():
        assert fam.get("verified_by"), f"{name} lacks verified_by provenance"
        assert "fileable_band" in fam, f"{name} lacks fileable_band"


def test_udt_is_fileable_with_lcd_anchor():
    e = anchor_for_code("G0483")
    assert e["family"] == "definitive_udt"
    assert e["fileable_band"] is True
    assert "L36707" in e["anchor_ids"]
    assert fileable("G0481") is True


def test_em_uniformity_is_not_fileable():
    # the MDCareNow lesson: convicted at 94% uniformity, still un-pleadable
    assert fileable("99214") is False
    assert fileable("99309") is False


def test_personal_care_and_sud_hours_not_fileable():
    # the CO-cluster + Edgewater lessons
    assert fileable("H2021") is False
    assert fileable("T1019") is False
    assert fileable("H2035") is False


def test_prefix_matching_and_unknown():
    assert anchor_for_code("Q4276")["family"] == "skin_substitutes"   # prefix Q42
    assert anchor_for_code("A0427")["family"] == "nemt"               # prefix A0
    assert anchor_for_code("XXXXX") is None                            # silent
    assert fileable("XXXXX") is None
    assert anchor_for_code(None) is None


def test_exact_code_beats_prefix():
    # 0242U is exact in genetic_panels even though '0' prefix would also match
    e = anchor_for_code("0242U")
    assert e["family"] == "genetic_panels" and e["matched_via"] == "code"
