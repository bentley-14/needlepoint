"""Guardrail-2 artifact emission (numeric_audit.json).

CALIBRATION-6DA0BBC3 exposed the gap: the audit ran in-process and its
findings landed inside summary.json's numeric_audit field, but NO standalone
numeric-audit artifact existed in the case dir (citation_audit.json has one;
Guardrail 2 didn't). These tests pin that _audit_numeric_predicates — the
single seam both phase_c's adversarial loop and phase_a's defense-as-mock
call — emits <case dir>/numeric_audit.json whenever it executes, including
under a calibration-style state root, and that the findings inside carry the
FIX-2 unit-semantics verdict for the Alder-shaped penalty predicate.
"""
from __future__ import annotations

import json

from fraud_investigator.orchestrator import Orchestrator

# The verbatim CALIBRATION-6DA0BBC3 iter-02 counsel_build predicate (the
# "10,724 claims" C1 penalty-exposure assertion).
ALDER_PENALTY_PRED = {
    "id": "c1_civil_penalty_exposure",
    "kind": "civil_penalty_exposure",
    "claimed": {
        "total_drug_suppressed_claims_2022_2023": 10724,
        "min_penalty_per_claim_2026": 13946,
        "max_penalty_per_claim_2026": 27894,
        "total_min_civil_penalty": 149599304,
        "total_max_civil_penalty": 299248056,
    },
    "statement": ("If each of 10,724 drug-classified suppressed claims (3,866 in "
                  "2022 + 6,858 in 2023) is a separate false presentment: minimum "
                  "civil penalty at 2026 rates: 10,724 × $13,946 = $149,599,304; "
                  "maximum: 10,724 × $27,894 = $299,248,056."),
    "codes": [],
}


def _artifact(orch) -> dict:
    p = orch.state_root / orch.state.case_id / "numeric_audit.json"
    assert p.exists(), f"expected Guardrail-2 artifact at {p}"
    return json.loads(p.read_text())


def test_audit_emits_artifact_into_case_dir(tmp_path):
    orch = Orchestrator(subject="Artifact Test Subject", state_dir=tmp_path,
                        dry_run=True)
    findings = orch._audit_numeric_predicates([dict(ALDER_PENALTY_PRED)])
    art = _artifact(orch)
    assert art["case_id"] == orch.state.case_id
    assert art["n_predicates"] == 1
    assert art["findings"] == findings
    assert art["status_counts"] == {"UNVERIFIABLE_UNIT_SEMANTICS": 1}
    f = art["findings"][0]
    assert f["id"] == "c1_civil_penalty_exposure"
    assert f["status"] == "UNVERIFIABLE_UNIT_SEMANTICS"
    assert "10,724 may be units, not claims" in f["detail"]


def test_zero_predicate_audit_still_emits_artifact(tmp_path):
    """The artifact proves the audit RAN — a build with no numeric_predicates
    must still leave the 'audited: nothing to audit' record, not silence."""
    orch = Orchestrator(subject="Artifact Test Subject", state_dir=tmp_path,
                        dry_run=True)
    assert orch._audit_numeric_predicates([]) == []
    art = _artifact(orch)
    assert art["n_predicates"] == 0
    assert art["findings"] == []


def test_artifact_under_calibration_style_state_root(tmp_path):
    """The CALIBRATION-6DA0BBC3 shape: a calibration case id under a
    state/calibration/ root. The seam is orchestrator-side, so emission must
    not depend on any agent cwd — exercised here by pointing state_root at a
    calibration-style tree directly (constructing a full calibration
    Orchestrator needs a clean export; the artifact seam is identical)."""
    orch = Orchestrator(subject="Artifact Test Subject",
                        state_dir=tmp_path / "state", dry_run=True)
    orch.state_root = tmp_path / "state" / "calibration"
    orch.state.case_id = "CALIBRATION-TESTTEST"
    findings = orch._audit_numeric_predicates([dict(ALDER_PENALTY_PRED)])
    p = tmp_path / "state" / "calibration" / "CALIBRATION-TESTTEST" / "numeric_audit.json"
    assert p.exists()
    assert json.loads(p.read_text())["findings"] == findings


def test_artifact_write_failure_never_raises(tmp_path, monkeypatch):
    """Best-effort contract: a broken artifact write must not raise into the
    loop (the audit is a safety net, not a gate)."""
    orch = Orchestrator(subject="Artifact Test Subject", state_dir=tmp_path,
                        dry_run=True)
    import fraud_investigator.phase_b as phase_b_mod
    monkeypatch.setattr(phase_b_mod, "case_dir",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    findings = orch._audit_numeric_predicates([dict(ALDER_PENALTY_PRED)])
    assert len(findings) == 1  # findings still returned for the defense channel
