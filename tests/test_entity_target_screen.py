"""Target-screen gate tests — novelty exclusion + collectibility filters."""
from __future__ import annotations

import json

import pandas as pd

from scripts.entity_target_screen import apply_gates, known_npis


def _scores(rows):
    base = {"year": 2024, "score": 0.5, "entity_type": "2", "service_class": "personal_care",
            "medicaid_total_pay": 1_000_000.0, "entity_id": None, "labeled": False,
            "enforcement_year": None}
    df = pd.DataFrame([{**base, **r} for r in rows])
    df["rank"] = range(1, len(df) + 1)
    return df


def test_known_npis_reads_all_sources(tmp_path):
    labels = tmp_path / "labels.jsonl"
    labels.write_text(json.dumps({"org_npi": "1111111111"}) + "\n")
    cur = tmp_path / "grounded_candidates_x.jsonl"
    cur.write_text(json.dumps({"org_npi": "2222222222"}) + "\n"
                   + json.dumps({"npi": "3333333333"}) + "\n")
    gold = tmp_path / "gold.jsonl"
    gold.write_text(json.dumps({"npi": "4444444444"}) + "\n")
    got = known_npis(labels_path=labels, curator_globs=[str(cur)], gold_set_path=gold)
    assert got == {"1111111111", "2222222222", "3333333333", "4444444444"}


def test_gates_drop_labeled_known_type_and_small():
    df = _scores([
        {"npi": "1000000001"},                                   # survivor
        {"npi": "1000000002", "labeled": True},                  # labeled -> out
        {"npi": "1000000003"},                                   # in known set -> out
        {"npi": "1000000004", "entity_type": "1"},               # individual -> out
        {"npi": "1000000005", "medicaid_total_pay": 40_000.0},   # below floor -> out
    ])
    out = apply_gates(df, known={"1000000003"}, min_pay=500_000.0, orgs_only=True)
    assert out["npi"].tolist() == ["1000000001"]
    f = out.attrs["funnel"]
    assert (f["dropped_labeled"], f["dropped_government_known"],
            f["dropped_type"], f["dropped_below_pay_floor"]) == (1, 1, 1, 1)
    assert f["n_survivors"] == 1


def test_gates_include_individuals_flag():
    df = _scores([{"npi": "1000000001", "entity_type": "1"}])
    assert len(apply_gates(df, known=set(), min_pay=0, orgs_only=False)) == 1
    assert len(apply_gates(df, known=set(), min_pay=0, orgs_only=True)) == 0
