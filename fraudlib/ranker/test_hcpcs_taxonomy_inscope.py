"""Specialty-in-scope: institutional/facility E&M must not read as off-specialty
for primary-care specialties (the 2024-triage false-positive that flagged an IM's
nursing-facility rounds as a "100% off-IM mill"). The off-specialty signal must
still fire on genuine off-scope PROCEDURE/DRUG billing.
"""
from fraudlib.ranker.hcpcs_taxonomy import is_inscope


# ── the fix: primary-care facility/home/hospital E&M is in-scope ─────────────

def test_internal_medicine_nursing_facility_em_in_scope():
    for code in ("99310", "99309", "99307", "99233", "99232", "99318"):
        assert is_inscope("Internal Medicine", code), code


def test_family_practice_home_and_facility_em_in_scope():
    for code in ("99350", "99348", "99336", "99232"):
        assert is_inscope("Family Practice", code), code


def test_geriatrics_and_np_pa_facility_em_in_scope():
    assert is_inscope("Geriatric Medicine", "99310")
    assert is_inscope("Nurse Practitioner", "99309")
    assert is_inscope("Physician Assistant", "99233")


def test_office_em_still_in_scope():
    assert is_inscope("Internal Medicine", "99214")


# ── no regression: real off-specialty PROCEDURE/DRUG still flags ────────────

def test_im_off_specialty_procedure_still_flagged():
    # an IM billing a chemo-admin / skin-substitute code is still off-scope
    assert not is_inscope("Internal Medicine", "96413")   # chemo IV infusion
    assert not is_inscope("Internal Medicine", "Q4188")   # amniotic skin substitute


def test_procedural_specialty_not_broadened():
    # we only folded facility E&M into COGNITIVE specialties; a dermatologist
    # billing nursing-facility E&M is still off-scope (their signal targets procedures)
    assert not is_inscope("Dermatology", "99310")


def test_unknown_specialty_defaults_in_scope():
    assert is_inscope("Some Unlisted Specialty", "99999")
