"""Tests for the entity-resolution layer (all-payer entity upgrade, P2):

  - fraudlib/entity/resolve.py::resolve_components  -- connected components,
    the address-megacluster guard, singleton handling.
  - scripts/compute_entity_features.py's pure aggregation functions --
    enumeration_cadence / cross_member_code_uniformity / pct_members_anomalous
    math.

Pure-function tests only: synthetic in-memory pandas DataFrames, no real (or
fake) parquet, no DuckDB, no network. Both modules are structured with a
"pure derivation, no I/O" layer specifically so this is possible -- see each
module's own docstring.

Run: pytest tests/test_entity_resolution.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fraudlib.entity.resolve import ADDRESS_MEGACLUSTER_CAP, resolve_components
from scripts.compute_entity_features import (
    entity_code_uniformity,
    entity_enumeration_cadence,
    entity_pct_anomalous,
)


# ─────────────────────────────────────────────────────────────────────────────
# resolve_components -- connected components / megacluster guard / singletons
# ─────────────────────────────────────────────────────────────────────────────
def test_three_npis_one_address_resolve_to_one_entity_isolated_npi_stays_separate():
    # 3 NPIs share ADDR; a 4th panel NPI has no edges at all.
    panel_npis = ["1000000001", "1000000002", "1000000003", "1000000004"]
    edge_rows = pd.DataFrame({
        "npi": ["1000000001", "1000000002", "1000000003"],
        "edge_type": "address",
        "key": "123 MAIN ST|90210",
    })
    out, megaclusters = resolve_components(panel_npis, edge_rows)

    assert megaclusters == []
    clustered = out[out["npi"] != "1000000004"]
    assert clustered["entity_id"].nunique() == 1
    assert set(clustered["npi"]) == {"1000000001", "1000000002", "1000000003"}
    # deterministic id: "ENT_" + min member npi
    assert clustered["entity_id"].iloc[0] == "ENT_1000000001"
    assert (clustered["edge_types_joined"] == "address").all()

    isolated = out[out["npi"] == "1000000004"].iloc[0]
    assert isolated["entity_id"] == "ENT_1000000004"
    assert isolated["entity_id"] != clustered["entity_id"].iloc[0]


def test_singleton_npi_with_zero_edge_rows_gets_singleton_entity():
    out, megaclusters = resolve_components(["9999999999"], pd.DataFrame(columns=["npi", "edge_type", "key"]))
    assert len(out) == 1
    row = out.iloc[0]
    assert row["entity_id"] == "ENT_9999999999"
    assert row["edge_types_joined"] == ""
    assert megaclusters == []


def test_singleton_resolution_also_works_with_none_edge_rows():
    out, megaclusters = resolve_components(["1234567890"], None)
    assert len(out) == 1 and out.iloc[0]["entity_id"] == "ENT_1234567890"
    assert megaclusters == []


def test_solo_unshared_key_does_not_count_as_a_connected_edge_type():
    # NPI's address is unique in the panel -- nobody else shares it, so it
    # stays a singleton entity. The docstring contract says edge_types_joined
    # is "" for a singleton; a key that never matched anyone else's must not
    # be reported as having "connected" this NPI to anything.
    panel_npis = ["3000000001", "3000000002"]
    edge_rows = pd.DataFrame({
        "npi": ["3000000001"],
        "edge_type": "address",
        "key": "1 LONELY LN|00001",
    })
    out, megaclusters = resolve_components(panel_npis, edge_rows)
    assert megaclusters == []
    row = out.set_index("npi").loc["3000000001"]
    assert row["entity_id"] == "ENT_3000000001"
    assert row["edge_types_joined"] == ""


def test_edge_types_joined_excludes_unshared_key_for_a_real_multi_member_entity():
    # B shares an org_pac group with A (a real connection); B ALSO holds an
    # address key nobody else in the panel has (not a real connection, just
    # incidental). B's edge_types_joined must report only "org_pac" -- the
    # address row never matched a peer, so it shouldn't look like a second
    # corroborating signal for this entity.
    panel_npis = ["4000000001", "4000000002"]
    edge_rows = pd.DataFrame({
        "npi": ["4000000001", "4000000002", "4000000002"],
        "edge_type": ["org_pac", "org_pac", "address"],
        "key": ["PAC1", "PAC1", "1 LONELY LN|00001"],
    })
    out, megaclusters = resolve_components(panel_npis, edge_rows)
    assert megaclusters == []
    row_b = out.set_index("npi").loc["4000000002"]
    assert row_b["entity_id"] == "ENT_4000000001"
    assert row_b["edge_types_joined"] == "org_pac"


def test_megacluster_over_address_cap_flagged_not_collapsed():
    # One more NPI than the real production cap all sharing one office-tower
    # address: must NOT collapse into one fake entity -- the guard should
    # exclude that key and flag it, leaving every member its OWN singleton.
    n = ADDRESS_MEGACLUSTER_CAP + 1
    npis = [f"2000000{i:03d}" for i in range(n)]
    edge_rows = pd.DataFrame({"npi": npis, "edge_type": "address", "key": "SHARED TOWER|90001"})

    out, megaclusters = resolve_components(npis, edge_rows)

    assert out["entity_id"].nunique() == n            # NOT one giant entity
    assert (out["edge_types_joined"] == "").all()      # the oversized key never contributed
    assert megaclusters == [{"edge_type": "address", "key": "SHARED TOWER|90001", "n_members": n}]


# ─────────────────────────────────────────────────────────────────────────────
# entity_enumeration_cadence
# ─────────────────────────────────────────────────────────────────────────────
def test_enumeration_cadence_math_even_spacing():
    entity_df = pd.DataFrame({
        "npi": ["A", "B", "C"],
        "entity_id": ["E1", "E1", "E1"],
        "edge_types_joined": ["address"] * 3,
    })
    enum_dates = pd.DataFrame({
        "npi": ["A", "B", "C"],
        "enum_date": ["2020-01-01", "2020-01-11", "2020-01-21"],  # 20-day span
    })
    out = entity_enumeration_cadence(entity_df, enum_dates)
    row = out.set_index("entity_id").loc["E1"]
    assert row["enumeration_cadence"] == pytest.approx(10.0)   # 20 days / (3-1)


def test_enumeration_cadence_null_for_singleton_and_insufficient_dates():
    entity_df = pd.DataFrame({
        "npi": ["D", "E", "F"],
        "entity_id": ["E2", "E3", "E3"],   # E2 singleton; E3 has 2 members
        "edge_types_joined": ["", "address", "address"],
    })
    enum_dates = pd.DataFrame({
        "npi": ["D", "E"],                 # D (the singleton) has a date; F (E3's 2nd member) does not
        "enum_date": ["2019-06-01", "2021-03-01"],
    })
    out = entity_enumeration_cadence(entity_df, enum_dates).set_index("entity_id")
    assert pd.isna(out.loc["E2", "enumeration_cadence"])   # only 1 member total -- nothing to spread
    assert pd.isna(out.loc["E3", "enumeration_cadence"])   # only 1 of 2 members has a usable date


def test_enumeration_cadence_empty_source_returns_empty_frame():
    entity_df = pd.DataFrame({"npi": ["A"], "entity_id": ["E1"], "edge_types_joined": [""]})
    out = entity_enumeration_cadence(entity_df, pd.DataFrame(columns=["npi", "enum_date"]))
    assert out.empty
    assert list(out.columns) == ["entity_id", "enumeration_cadence"]


# ─────────────────────────────────────────────────────────────────────────────
# entity_code_uniformity
# ─────────────────────────────────────────────────────────────────────────────
def test_code_uniformity_math():
    entity_df = pd.DataFrame({
        "npi": ["A", "B", "C", "D", "E", "F", "G"],
        "entity_id": ["E1", "E1", "E1", "E2", "E3", "E3", "E3"],
    })
    dominant_hcpcs = pd.DataFrame({
        "npi": ["A", "B", "C", "D", "E", "F", "G"],
        "dominant_code": ["99214", "99214", "99213", "11721", "36415", "36415", "99213"],
    })
    out = entity_code_uniformity(entity_df, dominant_hcpcs).set_index("entity_id")
    assert out.loc["E1", "cross_member_code_uniformity"] == pytest.approx(2 / 3)   # 2x 99214 of 3
    assert out.loc["E3", "cross_member_code_uniformity"] == pytest.approx(2 / 3)   # 2x 36415 of 3
    assert pd.isna(out.loc["E2", "cross_member_code_uniformity"])                  # singleton -- NULL


def test_code_uniformity_perfect_match_is_one():
    entity_df = pd.DataFrame({"npi": ["A", "B", "C"], "entity_id": ["E1"] * 3})
    dominant_hcpcs = pd.DataFrame({"npi": ["A", "B", "C"], "dominant_code": ["G0483"] * 3})
    out = entity_code_uniformity(entity_df, dominant_hcpcs)
    assert out.set_index("entity_id").loc["E1", "cross_member_code_uniformity"] == 1.0


def test_code_uniformity_empty_source_returns_empty_frame():
    entity_df = pd.DataFrame({"npi": ["A"], "entity_id": ["E1"]})
    out = entity_code_uniformity(entity_df, pd.DataFrame(columns=["npi", "dominant_code"]))
    assert out.empty
    assert list(out.columns) == ["entity_id", "cross_member_code_uniformity"]


# ─────────────────────────────────────────────────────────────────────────────
# entity_pct_anomalous
# ─────────────────────────────────────────────────────────────────────────────
def test_pct_anomalous_math_denominator_is_all_members_not_just_ranked():
    entity_df = pd.DataFrame({
        "npi": ["A", "B", "C", "D"],
        "entity_id": ["E1", "E1", "E1", "E2"],
    })
    # C has no rank at all (e.g. dropped by the ranker's agent filter) --
    # must count as not-anomalous, not be excluded from the denominator.
    anomaly_ranks = pd.DataFrame({
        "npi": ["A", "B", "D"],
        "pct": [0.01, 0.50, 0.01],
    })
    out = entity_pct_anomalous(entity_df, anomaly_ranks, anomaly_pct=0.02).set_index("entity_id")
    assert out.loc["E1", "pct_members_anomalous"] == pytest.approx(1 / 3)  # only A of A,B,C
    assert out.loc["E2", "pct_members_anomalous"] == pytest.approx(1.0)    # D alone, anomalous


def test_pct_anomalous_empty_ranks_returns_empty_frame():
    entity_df = pd.DataFrame({"npi": ["A"], "entity_id": ["E1"]})
    out = entity_pct_anomalous(entity_df, pd.DataFrame(columns=["npi", "pct"]))
    assert out.empty
    assert list(out.columns) == ["entity_id", "pct_members_anomalous"]
