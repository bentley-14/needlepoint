"""Floor-audit (R2/F1) — pure-function tests for compute_floor_audit."""
from __future__ import annotations

from fraud_investigator.floors import compute_floor_audit


def _fb(count_id="C1", ext=1, conv=2, anchor=0, reg=0, innocent=1, refut=80):
    return {
        "count_id": count_id,
        "extremity_evidence_ids": [f"ev-e{i}" for i in range(ext)],
        "convergent_signal_evidence_ids": [f"ev-c{i}" for i in range(conv)],
        "falsity_anchor_evidence_ids": [f"ev-a{i}" for i in range(anchor)],
        "regulatory_corroboration_evidence_ids": [f"ev-r{i}" for i in range(reg)],
        "innocent_explanations_considered": ["group practice"][:innocent],
        "innocent_explanations_refuted_narrative": "x" * refut,
    }


def _build(*fbs, damages=2_000_000.0):
    return {
        "counts_pled": [{"count_id": fb["count_id"]} for fb in fbs],
        "factual_basis_per_count": list(fbs),
        "estimated_aggregate_damages_usd": damages,
    }


def test_pass_via_falsity_anchor_only():
    """F1: a confirmed-fact falsity anchor grounds slot 3 with ZERO regulatory
    corroboration — the novel-detection case must be able to PASS clean."""
    audit = compute_floor_audit(_build(_fb(anchor=1, reg=0)),
                                venue_token_found=True)
    assert audit["counts"][0]["floor"] == "PASS"
    assert audit["all_retained_pass"] is True


def test_pass_via_regulatory_only():
    audit = compute_floor_audit(_build(_fb(anchor=0, reg=1)),
                                venue_token_found=True)
    assert audit["counts"][0]["floor"] == "PASS"


def test_neither_anchor_nor_regulatory_is_dependency_not_fail():
    audit = compute_floor_audit(_build(_fb(anchor=0, reg=0)),
                                venue_token_found=True)
    c = audit["counts"][0]
    assert c["floor"] == "PASS_NO_SLOT3"
    assert any("slot 3" in r for r in c["reasons"])
    assert audit["all_retained_pass"] is False


def test_fail_on_thin_convergence():
    audit = compute_floor_audit(_build(_fb(conv=1, anchor=1)),
                                venue_token_found=True)
    c = audit["counts"][0]
    assert c["floor"] == "FAIL"
    assert any("convergent" in r for r in c["reasons"])


def test_fail_on_short_refutation_and_missing_fb():
    build = _build(_fb(refut=10))
    build["counts_pled"].append({"count_id": "C9"})  # no factual basis entry
    audit = compute_floor_audit(build, venue_token_found=False)
    by_id = {c["count_id"]: c for c in audit["counts"]}
    assert by_id["C1"]["floor"] == "FAIL"
    assert any("narrative too short" in r for r in by_id["C1"]["reasons"])
    assert by_id["C9"]["floor"] == "FAIL"
    assert by_id["C9"]["reasons"] == ["no factual_basis_per_count entry"]
    assert audit["venue_landscape_documented"] is False


def test_dropped_counts_excluded():
    audit = compute_floor_audit(
        _build(_fb("C1", anchor=1), _fb("C2", conv=0)),
        venue_token_found=True, dropped_count_ids=["C2"])
    assert [c["count_id"] for c in audit["counts"]] == ["C1"]
    assert audit["all_retained_pass"] is True


def test_back_compat_build_without_anchor_field():
    """Old parsed.json builds predate falsity_anchor_evidence_ids — the audit
    must treat the missing key as zero anchors, not crash."""
    fb = _fb(reg=1)
    del fb["falsity_anchor_evidence_ids"]
    audit = compute_floor_audit(_build(fb), venue_token_found=True)
    assert audit["counts"][0]["floor"] == "PASS"
    assert audit["counts"][0]["slots"]["falsity_anchor_or_regulatory"]["n_anchor"] == 0
