"""Tests for scripts/cluster_queue.py — practice-cluster grouping of the triage queue (M2).

Pure-local: synthetic Part B panel parquet + synthetic TriageOutput JSON in
tmp_path. No network, no repo data.

Run: pytest tests/test_cluster_queue.py -v
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pandas as pd
import pytest

REPO = Path(__file__).resolve().parents[1]

_spec = importlib.util.spec_from_file_location(
    "cluster_queue", REPO / "scripts" / "cluster_queue.py")
cq = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cq)

ADDR_A = ("123 MAIN ST STE 4", "90210")
ADDR_B = ("456 OAK AVE", "10001")


def _item(npi, rank, verdict, confidence, specialty="Family Practice"):
    it = {
        "npi": npi,
        "rank": rank,
        "verdict": verdict,
        "confidence": confidence,
        "matched_archetype": "BILLING_FRAUD" if verdict == "HIGH_RISK" else None,
        "signals_detected": (["peer_deviation", "billing_intensity"]
                             if verdict in ("HIGH_RISK", "MODERATE_RISK") else []),
        "reasoning": ("Synthetic test record: pay_per_bene_pct=0.99 and "
                      "opioid_clm_share=0.32 are both extreme for this specialty; "
                      "fabricated purely so the schema's 100-char floor is met."),
        "specialty": specialty,
        "follow_up_questions": [],
    }
    return it


@pytest.fixture()
def queue_path(tmp_path) -> Path:
    queue = {
        "score_year": 2023,
        "k_range": [1, 6],
        "items": [
            _item("1000000001", 1, "HIGH_RISK", 0.9),
            _item("1000000002", 2, "MODERATE_RISK", 0.5, specialty="Internal Medicine"),
            _item("1000000003", 3, "HIGH_RISK", 0.8),
            _item("1000000004", 4, "LOW_RISK", 0.7),
            _item("1000000005", 5, "DISMISS", 0.9),
            _item("1000000006", 6, "HIGH_RISK", 0.85),
        ],
        "batch_summary": "Synthetic queue for cluster_queue tests — six providers across two addresses.",
    }
    p = tmp_path / "queue.json"
    p.write_text(json.dumps(queue))
    return p


@pytest.fixture()
def panel_path(tmp_path) -> Path:
    df = pd.DataFrame({
        "Rndrng_NPI": ["1000000001", "1000000002", "1000000003",
                       "1000000004", "1000000005", "1000000006"],
        "Rndrng_Prvdr_St1": [ADDR_A[0], ADDR_A[0], ADDR_B[0],
                             ADDR_A[0], ADDR_B[0], ADDR_B[0]],
        "Rndrng_Prvdr_Zip5": [ADDR_A[1], ADDR_A[1], ADDR_B[1],
                              ADDR_A[1], ADDR_B[1], ADDR_B[1]],
        "Tot_Mdcr_Pymt_Amt": [100_000.0, 50_000.0, 400_000.0,
                              999_999.0, 999_999.0, 200_000.0],
    })
    p = tmp_path / "cms_partb_2023.parquet"
    df.to_parquet(p)
    return p


def _run(queue_path, panel_path, tmp_path):
    out = tmp_path / "clusters.json"
    rc = cq.main(["--queue", str(queue_path), "--year", "2023",
                  "--panel", str(panel_path), "--out", str(out)])
    assert rc == 0
    return json.loads(out.read_text())


def test_same_address_clusters_different_address_does_not(queue_path, panel_path, tmp_path):
    report = _run(queue_path, panel_path, tmp_path)
    clusters = report["clusters"]
    by_zip = {c["address"]["zip5"]: c for c in clusters}
    # Addr A: NPIs 1 + 2 (HIGH + MODERATE) cluster together.
    a = by_zip[ADDR_A[1]]
    assert sorted(m["npi"] for m in a["members"]) == ["1000000001", "1000000002"]
    # Addr B: NPIs 3 + 6 cluster; NPI 1 (different address) is NOT in it.
    b = by_zip[ADDR_B[1]]
    assert sorted(m["npi"] for m in b["members"]) == ["1000000003", "1000000006"]


def test_low_and_dismiss_excluded_but_counted(queue_path, panel_path, tmp_path):
    report = _run(queue_path, panel_path, tmp_path)
    all_member_npis = {m["npi"] for c in report["clusters"] for m in c["members"]}
    assert "1000000004" not in all_member_npis  # LOW_RISK at addr A
    assert "1000000005" not in all_member_npis  # DISMISS at addr B
    assert report["n_excluded_low_dismiss"] == 2
    assert report["n_clusterable"] == 4


def test_cluster_ranking_and_member_fields(queue_path, panel_path, tmp_path):
    report = _run(queue_path, panel_path, tmp_path)
    clusters = report["clusters"]
    # Ranked by combined Tot_Mdcr_Pymt_Amt desc: addr B (600k) before addr A (150k).
    assert [c["combined_payment"] for c in clusters] == sorted(
        (c["combined_payment"] for c in clusters), reverse=True)
    assert clusters[0]["address"]["zip5"] == ADDR_B[1]
    assert clusters[0]["combined_payment"] == 600_000.0
    m = clusters[0]["members"][0]
    assert set(m) == {"npi", "verdict", "confidence", "payment", "specialty"}
    # Members sorted by payment desc within the cluster.
    assert m["npi"] == "1000000003" and m["payment"] == 400_000.0


def test_suggested_subject_names_members_and_unit(queue_path, panel_path, tmp_path):
    report = _run(queue_path, panel_path, tmp_path)
    for c in report["clusters"]:
        s = c["suggested_subject"]
        for m in c["members"]:
            assert m["npi"] in s
        assert c["address"]["street"] in s
        assert "office_suite analysis unit" in s
        assert "co-billers are queue-flagged" in s
