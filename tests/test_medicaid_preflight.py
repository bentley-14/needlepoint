"""Pure-function tests for the Medicaid org pre-flight."""
from __future__ import annotations

import pandas as pd

from scripts.medicaid_preflight import (
    name_tokens,
    pctile_of,
    unit_minutes_from_descriptor,
)


def test_unit_minutes_parsed_from_official_descriptors():
    # real descriptors from data/ground_truth/hcpcs_level2_full.parquet
    assert unit_minutes_from_descriptor("A/d tx program, per hour") == 60.0
    assert unit_minutes_from_descriptor(
        "Community-based wrap-around services, per 15 minutes") == 15.0
    assert unit_minutes_from_descriptor("Personal care ser per 15 min") == 15.0
    # non-time-denominated codes get NO hours math
    assert unit_minutes_from_descriptor("N-et; per diem") is None
    assert unit_minutes_from_descriptor("Adult day care per half day") is None
    assert unit_minutes_from_descriptor(None) is None


def test_name_tokens_strip_generic_words():
    a = name_tokens("COLORADO FAMILY CAREGIVERS LLC")
    b = name_tokens("CAREGIVERS VILLAGE COLORADO LLC")
    assert "CAREGIVERS" in (a & b)          # the distinctive shared token
    assert "LLC" not in a and "COLORADO" not in a   # generic/state tokens dropped
    assert name_tokens(None) == set()


def test_pctile_of():
    pop = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0])
    assert pctile_of(10.0, pop) == 1.0
    assert pctile_of(5.0, pop) == 0.5
    assert pctile_of(0.5, pop) == 0.0
    import math
    assert math.isnan(pctile_of(1.0, pd.Series([], dtype=float)))
