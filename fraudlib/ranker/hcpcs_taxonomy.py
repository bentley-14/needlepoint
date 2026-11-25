"""HCPCS code taxonomy for richer feature engineering.

This module catalogs HCPCS code families that are diagnostic for specific
fraud schemes, plus the "in-scope" code sets per provider specialty (used
to compute off-specialty code share), and OIG/CMS sentinel codes flagged
for repeated abuse.

The taxonomy is consumed by ``scripts/compute_hcpcs_rich_features.py``,
which joins it to ``data/panels/cms_partb_svc_<year>.parquet`` and
emits per-NPI feature columns.

Naming convention
-----------------
Code-family sets use uppercase tuple constants (e.g. ``DERM_FLAP_CODES``).
Specialty-scope sets are ``SPECIALTY_INSCOPE_CODES[<specialty_name>]``.
Sentinel codes (high-risk individual codes) are ``SENTINEL_CODES``.

Coverage philosophy
-------------------
Catch the documented fraud schemes we already have in the gold corpus
(Henghold-style flap upcoding, 16-cardio-radiopharm scheme, BLS
toxicology kickback, etc.) rather than try to enumerate every possible
billing irregularity. Add codes as we discover new schemes.
"""
from __future__ import annotations


# ─────────────────────────────────────────────────────────────────────────────
# Dermatology
# ─────────────────────────────────────────────────────────────────────────────
# Flap-repair codes — the "Henghold scheme": intermediate/complex/composite
# flaps billed when only simple wound closures were performed.
DERM_FLAP_CODES = (
    "14040", "14041", "14060", "14061",  # adjacent tissue transfer
    "14300", "14301", "14302",            # complex/composite flaps
    "14350",                              # filleted finger/toe flap
)
# Simple/intermediate wound repair (legitimate alternative to flap)
DERM_WOUND_REPAIR_CODES = (
    # Simple repair (12000-12057)
    "12001", "12002", "12004", "12005", "12006", "12007",
    "12011", "12013", "12014", "12015", "12016", "12017", "12018",
    "12031", "12032", "12034", "12035", "12036", "12037",
    "12041", "12042", "12044", "12045", "12046", "12047",
    "12051", "12052", "12053", "12054", "12055", "12056", "12057",
    # Intermediate/complex repair (13000s)
    "13100", "13101", "13102", "13120", "13121", "13122",
    "13131", "13132", "13133", "13151", "13152", "13153",
    "13160",
)
# Mohs micrographic surgery — staged tumor removal
DERM_MOHS_FIRST_STAGE = ("17311", "17313")  # initial stage
DERM_MOHS_ADDL_STAGE = ("17312", "17314", "17315")  # additional stages
DERM_BIOPSY_CODES = ("11102", "11103", "11104", "11105", "11106", "11107")
DERM_DESTRUCTION_CODES = (
    "17000", "17003", "17004",     # benign destruction
    "17110", "17111",              # benign by laser/cryo
    "17260", "17261", "17262", "17263", "17264", "17266",  # malignant destruction
    "17270", "17271", "17272", "17273", "17274", "17276",
    "17280", "17281", "17282", "17283", "17284", "17286",
)
DERM_EXCISION_BENIGN = tuple(f"114{i:02d}" for i in range(0, 47))   # 11400-11446
DERM_EXCISION_MALIG = tuple(f"116{i:02d}" for i in range(0, 47))    # 11600-11646


# ─────────────────────────────────────────────────────────────────────────────
# Cardiology
# ─────────────────────────────────────────────────────────────────────────────
CARD_STRESS_CODES = ("93016", "93017", "93018", "93350", "93351")
CARD_RADIOPHARM_J_CODES = (
    "A9555", "A9558", "A9560", "A9561",  # cardiac perfusion agents
    "A9500", "A9502", "A9503", "A9505",
)
CARD_CATH_CODES = (
    "93452", "93453", "93454", "93455", "93456", "93457", "93458", "93459",
    "93460", "93461", "93462", "93463", "93464",
)
CARD_PCI_STENT_CODES = (
    "92920", "92921", "92924", "92925", "92928", "92929",
    "92933", "92934", "92937", "92938", "92941", "92943", "92944",
)
CARD_ECHO_CODES = (
    "93303", "93304", "93306", "93307", "93308",
    "93312", "93313", "93314", "93315", "93316", "93317",
    "93350", "93351",
)
CARD_EP_STUDY_CODES = (
    "93619", "93620", "93621", "93622", "93623", "93624",
    "93653", "93654", "93655", "93656", "93657", "93662",
)
CARD_HOLTER_CODES = ("93224", "93225", "93226", "93227", "93228")


# ─────────────────────────────────────────────────────────────────────────────
# Ophthalmology
# ─────────────────────────────────────────────────────────────────────────────
OPHTH_CATARACT_SIMPLE = ("66984",)
OPHTH_CATARACT_COMPLEX = ("66982",)
OPHTH_YAG_CAPSULOTOMY = ("66821",)
OPHTH_ANTI_VEGF_INJECTION = ("67028",)
OPHTH_ANTI_VEGF_DRUGS = ("J0178", "J2778", "J3490", "J9035", "J0185", "Q5128")
OPHTH_VISUAL_FIELD = ("92081", "92082", "92083")
OPHTH_RETINAL_LASER = ("67210", "67220", "67228")
OPHTH_OCT_CODES = ("92133", "92134")
OPHTH_FUNDUS_PHOTO = ("92250",)


# ─────────────────────────────────────────────────────────────────────────────
# Orthopedics / Pain Management
# ─────────────────────────────────────────────────────────────────────────────
PAIN_NERVE_BLOCK_CODES = (
    *(f"644{i:02d}" for i in range(0, 100)),    # 64400-64499
    *(f"645{i:02d}" for i in range(0, 31)),     # 64500-64530
)
PAIN_RFA_CODES = (
    "64633", "64634", "64635", "64636",  # paravertebral facet
    "64640",                              # peripheral nerve
)
PAIN_ESI_CODES = (
    "62320", "62321", "62322", "62323",   # epidural injection
    "62324", "62325", "62326", "62327",
)
PAIN_TRIGGER_POINT = ("20552", "20553")
PAIN_SYNVISC_J_CODES = ("J7321", "J7322", "J7323", "J7324", "J7325", "J7326", "J7327", "J7328", "J7329", "J7331")
ORTHO_ARTHROSCOPY = tuple(f"298{i:02d}" for i in range(0, 82))  # 29800-29881
ORTHO_JOINT_INJECTION = ("20600", "20604", "20605", "20606", "20610", "20611")


# ─────────────────────────────────────────────────────────────────────────────
# Podiatry
# ─────────────────────────────────────────────────────────────────────────────
POD_NAIL_AVULSION = ("11730", "11732", "11750", "11765")
POD_DEBRIDE_MEDICAL = ("11042", "11043", "11044", "11045", "11046", "11047")
POD_ROUTINE_NAIL_CARE = ("11719", "11720", "11721", "G0127")
POD_DIABETIC_CALLUS = ("11055", "11056", "11057")
POD_LOPS_EXAM = ("G0245", "G0246", "G0247")


# ─────────────────────────────────────────────────────────────────────────────
# Lab / Pathology
# ─────────────────────────────────────────────────────────────────────────────
LAB_BMP_CMP_PANELS = (
    "80048",  # basic metabolic
    "80050",  # general health
    "80051",  # electrolyte
    "80053",  # comprehensive metabolic
    "80061",  # lipid
    "80069",  # renal function
    "80074",  # acute hepatitis
    "80076",  # hepatic function
)
LAB_TOXICOLOGY_CODES = (
    "80305", "80306", "80307",  # presumptive
    "80320", "80321", "80322", "80323",  # confirmation
    "G0480", "G0481", "G0482", "G0483",  # tier 1-4 drug confirmation
    "G0659",                              # presumptive immunoassay
)
LAB_GENETIC_CODES = (
    *(f"81{i:03d}" for i in range(100, 500)),  # 81100-81499 molecular pathology
)
LAB_PHARMACOGENOMIC = ("81225", "81226", "81227", "81418", "0070U", "0071U", "0072U", "0073U", "0074U", "0075U", "0076U", "0078U")
PATH_88305_INTENSITY = ("88305",)  # OIG sentinel: surgical path level IV


# ─────────────────────────────────────────────────────────────────────────────
# Mental Health / Addiction Treatment
# ─────────────────────────────────────────────────────────────────────────────
PSYCH_PSYCHOTHERAPY = ("90832", "90833", "90834", "90836", "90837", "90838")
PSYCH_60MIN = ("90837", "90838")  # upcoded from 90834 (45min)
PSYCH_E_AND_M = ("99202", "99203", "99204", "99205", "99213", "99214", "99215")
ADDICTION_TREATMENT_CODES = (
    "H0001", "H0002", "H0003", "H0004", "H0005",
    "H0010", "H0011", "H0012", "H0013", "H0014", "H0015",
    "H0020", "H0022", "H0033", "H0050",
    "G2086", "G2087", "G2088",  # office-based opioid use disorder
)


# ─────────────────────────────────────────────────────────────────────────────
# Radiology
# ─────────────────────────────────────────────────────────────────────────────
RAD_MRI_CODES = (
    *(f"705{i:02d}" for i in range(51, 60)),    # brain MRI 70551-70559
    *(f"715{i:02d}" for i in range(50, 53)),    # chest 71550-71552
    *(f"732{i:02d}" for i in range(18, 26)),    # extremity 73218-73225
    *(f"727{i:02d}" for i in range(0, 4)),      # spine 72700s
    *(f"741{i:02d}" for i in range(81, 84)),    # abdomen 74181-74183
    *(f"721{i:02d}" for i in range(41, 50)),    # spine 72141-72149
)
RAD_CT_CODES = (
    *(f"701{i:02d}" for i in range(50, 71)),
    *(f"711{i:02d}" for i in range(50, 71)),
    *(f"741{i:02d}" for i in range(50, 78)),
)


# ─────────────────────────────────────────────────────────────────────────────
# Drug / Infusion
# ─────────────────────────────────────────────────────────────────────────────
INFUSION_ADMIN = (
    "96365", "96366", "96367", "96368",  # infusion
    "96374", "96375", "96376",            # IV push
    "96360", "96361",                     # hydration
)
HYDRATION_ONLY = ("96360", "96361")
CHEMO_ADMIN = (
    "96401", "96402", "96405", "96406",
    "96409", "96411", "96413", "96415", "96416", "96417",
    "96420", "96422", "96423", "96425", "96440", "96446",
    "96450", "96521", "96522", "96523",
)
UNCLASSIFIED_J_CODES = ("J3490", "J3590", "J7799", "J7999", "J8499", "J8999", "J9999")


# ─────────────────────────────────────────────────────────────────────────────
# Hospice / HHA
# ─────────────────────────────────────────────────────────────────────────────
HOSPICE_CERT_CODES = ("G0179", "G0180", "G9473", "G9474", "G9475", "G9476")
HHA_FACE_TO_FACE = ("G0181", "G0182", "G0299", "G0300")


# ─────────────────────────────────────────────────────────────────────────────
# Telehealth (POS 02/10 are the primary signal; modifier 95 too)
# ─────────────────────────────────────────────────────────────────────────────
TELEHEALTH_POS = ("02", "10")  # check Place_Of_Srvc field, not HCPCS
TELEHEALTH_SPECIFIC_HCPCS = ("G2010", "G2012", "G2061", "G2062", "G2063", "G2086", "G2087", "G2088")


# ─────────────────────────────────────────────────────────────────────────────
# Sentinel high-risk codes — OIG/CMS audit history flags these as
# repeatedly abused, regardless of specialty.
# ─────────────────────────────────────────────────────────────────────────────
SENTINEL_CODES = (
    # Port/catheter insertion (phantom-billing common in NHs)
    "36561", "36562", "36563",
    "36571",
    # CPR (phantom in NHs)
    "92950",
    # Depression screening (over-screening for $)
    "G0444",
    # MH community-based services (sober-home schemes)
    "H2019", "H2017",
    # Skin substitutes (high-margin, recently flagged)
    "Q4101", "Q4106", "Q4107", "Q4151", "Q4153", "Q4154", "Q4163",
    # Orthotic L-codes (DME abuse)
    "L0631", "L0648", "L0650", "L1851", "L1852",  # back/knee
    # Tox MAAA / drug confirmation (BLS-style kickback ring)
    "0070U", "0071U", "0072U", "0073U", "0074U", "0075U", "0076U", "0078U",
    "G0480", "G0481", "G0482", "G0483",
    # Bauder pattern: YAG laser after cataract
    "66821",
    # Unclassified drugs (highest-abuse J-codes)
    "J3490", "J3590", "J9999",
    # Sleep study
    "95810", "95811",
    # Compounded drugs (era 2014-2017 fraud)
    "J7999", "Q9974", "Q9975",
)


# ─────────────────────────────────────────────────────────────────────────────
# Specialty-inscope mapping
# ─────────────────────────────────────────────────────────────────────────────
# For each specialty, what code families are "normal" / "in-scope". Used to
# compute off_specialty_share = fraction of billing in codes NOT in scope.
# Specialty strings match the values in cms_partb_svc *Rndrng_Prvdr_Type*.
#
# Rough guide: a Family Medicine doctor billing flap-repair codes is suspect;
# a Plastic Surgeon billing them is normal.

# Common "office-visit" E&M for ALL specialties
EM_OFFICE_VISIT = ("99201","99202","99203","99204","99205",
                   "99211","99212","99213","99214","99215",
                   "99381","99382","99383","99384","99385","99386","99387",
                   "99391","99392","99393","99394","99395","99396","99397",
                   "99401","99402","99403","99404","G0438","G0439")

# Institutional / non-office E&M — nursing-facility, domiciliary/rest-home, home
# visits, hospital inpatient + observation, discharge, prolonged, TCM, ACP, critical
# care. These are the bread-and-butter of COGNITIVE / primary-care practice (SNF
# rounds, home visits, hospitalist work) and must be in-scope for those specialties,
# else a PCP doing legitimate facility rounds reads as "100% off-specialty" — a
# false fraud signal (e.g. an IM billing 99310/99309/99233 nursing-facility codes
# was mis-flagged "100% off-IM" by the 2024 triage). Off-specialty is meant to catch
# PROCEDURE/DRUG billing outside scope, not E&M setting.
EM_INSTITUTIONAL = (
    # nursing facility
    "99304","99305","99306","99307","99308","99309","99310","99315","99316","99318",
    # domiciliary / rest home / assisted living
    "99324","99325","99326","99327","99328","99334","99335","99336","99337",
    # home visits
    "99341","99342","99343","99344","99345","99347","99348","99349","99350",
    # hospital inpatient (initial / subsequent / discharge)
    "99221","99222","99223","99231","99232","99233","99238","99239",
    # observation
    "99217","99218","99219","99220","99224","99225","99226",
    # prolonged services
    "99354","99355","99356","99357","99358","99359",
    # transitional care / advance care planning / critical care
    "99495","99496","99497","99498","99291","99292",
)

SPECIALTY_INSCOPE: dict[str, tuple[str, ...]] = {
    # Surgeons can do flaps legitimately
    "Plastic and Reconstructive Surgery": EM_OFFICE_VISIT + DERM_FLAP_CODES + DERM_WOUND_REPAIR_CODES,
    "General Surgery": EM_OFFICE_VISIT + DERM_WOUND_REPAIR_CODES + ORTHO_ARTHROSCOPY + ("47562","47563","49585"),
    # Dermatology — should be doing destruction/excision/biopsy/mohs, NOT flap repairs as the bread-and-butter
    "Dermatology": EM_OFFICE_VISIT + DERM_BIOPSY_CODES + DERM_DESTRUCTION_CODES + DERM_EXCISION_BENIGN + DERM_EXCISION_MALIG + DERM_MOHS_FIRST_STAGE + DERM_MOHS_ADDL_STAGE + DERM_WOUND_REPAIR_CODES[:20],
    # Cardiology in-scope: catheterization, echo, EP, holter — and stress
    "Cardiology": EM_OFFICE_VISIT + CARD_STRESS_CODES + CARD_CATH_CODES + CARD_ECHO_CODES + CARD_EP_STUDY_CODES + CARD_HOLTER_CODES,
    "Interventional Cardiology": EM_OFFICE_VISIT + CARD_CATH_CODES + CARD_PCI_STENT_CODES + CARD_ECHO_CODES,
    "Ophthalmology": EM_OFFICE_VISIT + OPHTH_CATARACT_SIMPLE + OPHTH_CATARACT_COMPLEX + OPHTH_YAG_CAPSULOTOMY + OPHTH_ANTI_VEGF_INJECTION + OPHTH_VISUAL_FIELD + OPHTH_RETINAL_LASER + OPHTH_OCT_CODES + OPHTH_FUNDUS_PHOTO,
    "Pain Management": EM_OFFICE_VISIT + PAIN_NERVE_BLOCK_CODES + PAIN_RFA_CODES + PAIN_ESI_CODES + PAIN_TRIGGER_POINT,
    "Anesthesiology": EM_OFFICE_VISIT + PAIN_NERVE_BLOCK_CODES + PAIN_ESI_CODES + tuple(f"002{i:02d}" for i in range(0,99)),
    "Orthopedic Surgery": EM_OFFICE_VISIT + ORTHO_ARTHROSCOPY + ORTHO_JOINT_INJECTION + PAIN_SYNVISC_J_CODES,
    "Podiatry": EM_OFFICE_VISIT + POD_NAIL_AVULSION + POD_DEBRIDE_MEDICAL + POD_ROUTINE_NAIL_CARE + POD_DIABETIC_CALLUS + POD_LOPS_EXAM,
    "Pathology": EM_OFFICE_VISIT + PATH_88305_INTENSITY + tuple(f"883{i:02d}" for i in range(0,15)),
    "Diagnostic Radiology": EM_OFFICE_VISIT + RAD_MRI_CODES + RAD_CT_CODES + tuple(f"742{i:02d}" for i in range(0,60)),
    "Interventional Radiology": EM_OFFICE_VISIT + RAD_MRI_CODES + RAD_CT_CODES + tuple(f"371{i:02d}" for i in range(0,99)),
    "Hematology/Oncology": EM_OFFICE_VISIT + CHEMO_ADMIN + INFUSION_ADMIN,
    "Internal Medicine": EM_OFFICE_VISIT + LAB_BMP_CMP_PANELS + INFUSION_ADMIN[:6],
    "Family Practice": EM_OFFICE_VISIT + LAB_BMP_CMP_PANELS + ("90471","90472"),
    "Family Medicine": EM_OFFICE_VISIT + LAB_BMP_CMP_PANELS + ("90471","90472"),
    "Geriatric Medicine": EM_OFFICE_VISIT + LAB_BMP_CMP_PANELS,
    "Nurse Practitioner": EM_OFFICE_VISIT + LAB_BMP_CMP_PANELS,
    "Physician Assistant": EM_OFFICE_VISIT + LAB_BMP_CMP_PANELS,
    "Psychiatry": EM_OFFICE_VISIT + PSYCH_PSYCHOTHERAPY + PSYCH_E_AND_M,
    "Clinical Psychologist": EM_OFFICE_VISIT + PSYCH_PSYCHOTHERAPY,
    "Hospice and Palliative Care": EM_OFFICE_VISIT + HOSPICE_CERT_CODES,
    "Home Health Agency": EM_OFFICE_VISIT + HHA_FACE_TO_FACE,
}

# Cognitive / primary-care specialties legitimately bill institutional + home +
# hospital E&M (SNF rounds, home visits, hospitalist work), so fold EM_INSTITUTIONAL
# into their in-scope sets. Without this, a PCP doing facility rounds reads as
# off-specialty. (Procedural specialties keep office-only E&M — their off-specialty
# signal targets procedures/drugs, and facility E&M is a negligible share for them.)
_COGNITIVE_SPECIALTIES = (
    "Internal Medicine", "Family Practice", "Family Medicine", "Geriatric Medicine",
    "Nurse Practitioner", "Physician Assistant", "Psychiatry",
    "Hospice and Palliative Care",
)
for _spec in _COGNITIVE_SPECIALTIES:
    if _spec in SPECIALTY_INSCOPE:
        SPECIALTY_INSCOPE[_spec] = SPECIALTY_INSCOPE[_spec] + EM_INSTITUTIONAL


def is_inscope(specialty: str, hcpcs: str) -> bool:
    """Return True if hcpcs is in-scope for the given specialty.

    Falls back to True for unknown specialties (don't accidentally flag everything).
    """
    inscope = SPECIALTY_INSCOPE.get(specialty)
    if inscope is None:
        return True
    return hcpcs in inscope


# Set view for fast membership tests (computed once)
def _as_set(*tuples: tuple[str, ...]) -> set[str]:
    out: set[str] = set()
    for t in tuples:
        out.update(t)
    return out


CODE_FAMILIES: dict[str, set[str]] = {
    "derm_flap": _as_set(DERM_FLAP_CODES),
    "derm_wound_repair": _as_set(DERM_WOUND_REPAIR_CODES),
    "derm_mohs_first": _as_set(DERM_MOHS_FIRST_STAGE),
    "derm_mohs_addl": _as_set(DERM_MOHS_ADDL_STAGE),
    "derm_biopsy": _as_set(DERM_BIOPSY_CODES),
    "derm_destruction": _as_set(DERM_DESTRUCTION_CODES),
    "derm_excision_benign": _as_set(DERM_EXCISION_BENIGN),
    "derm_excision_malig": _as_set(DERM_EXCISION_MALIG),
    "card_stress": _as_set(CARD_STRESS_CODES),
    "card_radiopharm": _as_set(CARD_RADIOPHARM_J_CODES),
    "card_cath": _as_set(CARD_CATH_CODES),
    "card_pci_stent": _as_set(CARD_PCI_STENT_CODES),
    "card_echo": _as_set(CARD_ECHO_CODES),
    "card_ep_study": _as_set(CARD_EP_STUDY_CODES),
    "card_holter": _as_set(CARD_HOLTER_CODES),
    "ophth_cataract_simple": _as_set(OPHTH_CATARACT_SIMPLE),
    "ophth_cataract_complex": _as_set(OPHTH_CATARACT_COMPLEX),
    "ophth_yag": _as_set(OPHTH_YAG_CAPSULOTOMY),
    "ophth_anti_vegf_proc": _as_set(OPHTH_ANTI_VEGF_INJECTION),
    "ophth_anti_vegf_drug": _as_set(OPHTH_ANTI_VEGF_DRUGS),
    "ophth_visual_field": _as_set(OPHTH_VISUAL_FIELD),
    "ophth_retinal_laser": _as_set(OPHTH_RETINAL_LASER),
    "pain_nerve_block": _as_set(PAIN_NERVE_BLOCK_CODES),
    "pain_rfa": _as_set(PAIN_RFA_CODES),
    "pain_esi": _as_set(PAIN_ESI_CODES),
    "pain_trigger_point": _as_set(PAIN_TRIGGER_POINT),
    "pain_synvisc": _as_set(PAIN_SYNVISC_J_CODES),
    "ortho_arthroscopy": _as_set(ORTHO_ARTHROSCOPY),
    "ortho_joint_injection": _as_set(ORTHO_JOINT_INJECTION),
    "pod_nail_avulsion": _as_set(POD_NAIL_AVULSION),
    "pod_debride_medical": _as_set(POD_DEBRIDE_MEDICAL),
    "pod_diabetic_callus": _as_set(POD_DIABETIC_CALLUS),
    "lab_panels": _as_set(LAB_BMP_CMP_PANELS),
    "lab_toxicology": _as_set(LAB_TOXICOLOGY_CODES),
    "lab_genetic": _as_set(LAB_GENETIC_CODES),
    "path_88305": _as_set(PATH_88305_INTENSITY),
    "psych_60min": _as_set(PSYCH_60MIN),
    "psych_all": _as_set(PSYCH_PSYCHOTHERAPY),
    "addiction_treatment": _as_set(ADDICTION_TREATMENT_CODES),
    "rad_mri": _as_set(RAD_MRI_CODES),
    "rad_ct": _as_set(RAD_CT_CODES),
    "infusion_admin": _as_set(INFUSION_ADMIN),
    "hydration": _as_set(HYDRATION_ONLY),
    "chemo_admin": _as_set(CHEMO_ADMIN),
    "unclassified_jcodes": _as_set(UNCLASSIFIED_J_CODES),
    "hospice_cert": _as_set(HOSPICE_CERT_CODES),
    "hha_face_to_face": _as_set(HHA_FACE_TO_FACE),
    "telehealth_specific": _as_set(TELEHEALTH_SPECIFIC_HCPCS),
    "sentinel": _as_set(SENTINEL_CODES),
}
