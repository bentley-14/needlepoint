"""Canonical statutory paragraphs for a qui tam complaint.

Each function returns a list of paragraph dicts:

    [{"text": "...", "kind": "numbered"}, ...]

Kinds:
  - ``numbered``: receives a sequential ¶ N in the final document
  - ``unnumbered``: rendered without a number (preamble, jury demand, etc.)
  - ``subsection``: an A./B./C. sub-section heading (not numbered)

Paragraph text is plain prose. Inline emphasis uses ``<b>``, ``<i>`` tags
(ReportLab Paragraph markup). Section headings (``INTRODUCTION``,
``JURISDICTION AND VENUE`` etc.) are emitted by the PDF builder, not here.

Statute language is faithful to the cited provisions; this module exists so
the same recitations can be reused across cases without LLM drift.
"""
from __future__ import annotations

from typing import Any

P = dict[str, Any]


# ────────────────────────────────────────────────────────────────────────────
# FCA elements (used in INTRODUCTION)
# ────────────────────────────────────────────────────────────────────────────

def fca_elements_paragraphs() -> list[P]:
    """FCA recitation paragraphs (mirrors Cerebral ¶¶ 5–6)."""
    return [
        {"text":
            "The FCA provides for the award of treble damages and civil penalties "
            "for a variety of false and fraudulent practices.",
         "kind": "numbered"},
        {"text":
            "Specifically, the FCA proscribes any person from “knowingly” "
            "presenting, or causing to be presented, any “false or fraudulent "
            "claim” or any “false record or statement material to a false "
            "or fraudulent claim” for payment from the United States. "
            "31 U.S.C. § 3729(a)(1)(A) and (B). In turn, the FCA defines "
            "“knowingly” as having “actual knowledge of the "
            "information;” acting with “deliberate ignorance of the truth "
            "or falsity of the information;” or acting “in reckless "
            "disregard of the truth or falsity of the information.” Moreover, "
            "“no proof of specific intent to defraud [the United States] is "
            "required.” 31 U.S.C. § 3729(b)(1).",
         "kind": "numbered"},
    ]


def disclosure_statement_compliance_paragraph(district: str) -> list[P]:
    """Confirms the relator served DOJ with the disclosure statement (¶ 7)."""
    return [
        {"text":
            f"As required by the False Claims Act, 31 U.S.C. § 3730(b)(2), "
            f"Relator has provided the Attorney General of the United States and "
            f"the United States Attorney for the {district} with a disclosure "
            f"statement of all material evidence and information related to this "
            f"Complaint. The disclosure statement is supported by material "
            f"evidence establishing the existence of Defendants’ false claims "
            f"known to Relator at the time of the filing of this Complaint. The "
            f"disclosure statement includes attorney-client communications and "
            f"work product of Relator’s attorneys, and is submitted to the "
            f"Attorney General and to the United States Attorney in their capacity "
            f"as potential co-counsel in the litigation. Therefore, Relator "
            f"understands the disclosure to be confidential.",
         "kind": "numbered"},
    ]


# ────────────────────────────────────────────────────────────────────────────
# Jurisdiction & venue
# ────────────────────────────────────────────────────────────────────────────

def jurisdiction_venue_paragraphs(district: str) -> list[P]:
    """Subject-matter, personal jurisdiction, venue (¶¶ 8, 10, 11)."""
    return [
        {"text":
            "This Court has jurisdiction over the subject matter of this action "
            "pursuant to 31 U.S.C. § 3732(a) and 28 U.S.C. § 1331.",
         "kind": "numbered"},
        {"text":
            f"This Court has personal jurisdiction over the Defendant pursuant to "
            f"28 U.S.C. §§ 1391(b) and 31 U.S.C. § 3732(a) because "
            f"Defendant transacts business in the {district}.",
         "kind": "numbered"},
        {"text":
            f"Venue is proper in this District pursuant to 31 U.S.C. § 3732(a) "
            f"and 28 U.S.C. § 1391(b), because Defendant transacts business in "
            f"the {district} and a substantial part of the events giving rise to "
            f"the claims occurred in this District.",
         "kind": "numbered"},
    ]


def public_disclosure_bar_disclaimer() -> list[P]:
    """Original-source / public-disclosure-bar paragraph (¶ 9)."""
    return [
        {"text":
            "In accordance with 31 U.S.C. § 3730(e)(4)(A), there has been no "
            "statutorily relevant public disclosure of the “allegations or "
            "transactions” in this Complaint. None of the allegations set "
            "forth in this Complaint are based on a public disclosure of "
            "allegations or transactions in a criminal, civil or administrative "
            "hearing, in a congressional, administrative or General Accounting "
            "Office report, hearing, audit or investigation, or from the news "
            "media. Relator has direct and independent knowledge of the "
            "information on which the allegations set forth in this Complaint are "
            "based. In accordance with 31 U.S.C. § 3730(e)(4)(B), Relator is "
            "an original source with direct and independent knowledge of the "
            "allegations contained herein.",
         "kind": "numbered"},
    ]


# ────────────────────────────────────────────────────────────────────────────
# Federally funded health insurance programs
# (sub-sections under FEDERALLY FUNDED HEALTH INSURANCE PROGRAMS heading)
# ────────────────────────────────────────────────────────────────────────────

def medicare_program_paragraphs() -> list[P]:
    """A. Medicare Program — mirrors Cerebral ¶¶ 12–26."""
    return [
        {"text": "<b>A. Medicare Program</b>", "kind": "subsection"},
        {"text":
            "In 1965, Congress enacted Title XVIII of the Social Security Act, "
            "which established the Medicare Program to provide health insurance "
            "for the elderly and disabled. Medicare is a health insurance program "
            "for: people age 65 or older; people under age 65 with certain "
            "disabilities; and people of all ages with end-stage renal disease "
            "(permanent kidney failure requiring dialysis or a kidney transplant).",
         "kind": "numbered"},
        {"text":
            "Medicare now has four parts: Part A, Part B, Part C, and Part D.",
         "kind": "numbered"},
        {"text":
            "Medicare Part A (Hospital Insurance) helps cover inpatient care in "
            "hospitals, including critical access hospitals, and skilled nursing "
            "facilities (not custodial or long-term care). Medicare Part A also "
            "helps cover hospice care and some home health care.",
         "kind": "numbered"},
        {"text":
            "Medicare Part B (Medical Insurance) helps cover doctors’ services "
            "and outpatient care, as well as other medical services not covered by "
            "Part A such as “durable medical equipment” including HME "
            "equipment and supplies. Part B also helps pay for covered health "
            "services and supplies when they are medically necessary.",
         "kind": "numbered"},
        {"text":
            "Medicare Part C, also known as “Medicare Advantage,” is a "
            "managed care program that can be utilized by Medicare beneficiaries "
            "as a replacement for benefits otherwise generally available under "
            "Medicare Parts A and B. Medicare Part C is federally funded through "
            "contracts with private commercial insurance carriers who pay claims "
            "on behalf of Medicare beneficiaries.",
         "kind": "numbered"},
        {"text":
            "Medicare Part D (Prescription Drug Plan) provides beneficiaries with "
            "assistance in paying for out-patient prescription drugs.",
         "kind": "numbered"},
        {"text":
            "Payments from the Medicare Program come from a trust fund, known as "
            "the Medicare Trust Fund, which is funded through payroll deductions "
            "taken from the work force, in addition to government contributions.",
         "kind": "numbered"},
        {"text":
            "The Medicare Program is administered through the United States "
            "Department of Health and Human Services (“HHS”) and, "
            "specifically, the Centers for Medicare and Medicaid Services "
            "(“CMS”), an agency of HHS.",
         "kind": "numbered"},
        {"text":
            "Much of the daily administration and operation of the Medicare "
            "Program is managed through private insurers under contract with the "
            "federal government, including Medicare Administrative Contractors "
            "(MACs), fiscal intermediaries, and Part C and Part D plan sponsors.",
         "kind": "numbered"},
        {"text":
            "To participate in Medicare, providers must assure that their services "
            "are provided economically and only when, and to the extent they are "
            "medically necessary. Medicare will only reimburse costs for medical "
            "services that are needed for the prevention, diagnosis, or treatment "
            "of a specific illness or injury. 42 U.S.C. § 1395y(a)(1)(A).",
         "kind": "numbered"},
    ]


def medicaid_program_paragraphs() -> list[P]:
    """B. Medicaid Program — mirrors Cerebral ¶¶ 27–31."""
    return [
        {"text": "<b>B. Medicaid Program</b>", "kind": "subsection"},
        {"text":
            "Medicaid was created in 1965, at the same time as Medicare, when "
            "Title XIX was added to the Social Security Act. The Medicaid program "
            "aids the states in furnishing medical assistance to eligible needy "
            "persons, including indigent and disabled people. Medicaid is the "
            "largest source of funding for medical and health-related services for "
            "America’s poorest people.",
         "kind": "numbered"},
        {"text":
            "Medicaid is a cooperative federal-state public assistance program "
            "which is administered by the states. Funding is shared between the "
            "federal government and the respective states.",
         "kind": "numbered"},
        {"text":
            "Title XIX of the Social Security Act allows considerable flexibility "
            "within the states’ Medicaid plans, but to receive federal "
            "matching funds, a state Medicaid program must meet certain minimum "
            "coverage and eligibility standards, including coverage of needy "
            "individuals and families in five broad groups: pregnant women; "
            "children and teenagers; seniors; people with disabilities; and people "
            "who are blind.",
         "kind": "numbered"},
    ]


def other_federal_programs_paragraphs() -> list[P]:
    """C. Other Federal Health Care Programs — mirrors Cerebral ¶¶ 32–33."""
    return [
        {"text": "<b>C. Other Federal Health Care Programs</b>", "kind": "subsection"},
        {"text":
            "In addition to Medicare and Medicaid, the federal government "
            "reimburses a portion of the cost of medical services, equipment, and "
            "prescription medication under several other federal health care "
            "programs, including but not limited to TRICARE, CHAMPVA, and the "
            "Federal Employees Health Benefit Program.",
         "kind": "numbered"},
        {"text":
            "TRICARE, administered by the United States Department of Defense, is "
            "a health care program for individuals and dependents affiliated with "
            "the armed forces. CHAMPVA, administered by the United States "
            "Department of Veterans Affairs, is a health care program for the "
            "families of veterans with a 100 percent service-connected disability. "
            "The Federal Employees Health Benefit Program, administered by the "
            "United States Office of Personnel Management, provides health "
            "insurance for hundreds of thousands of federal employees, retirees, "
            "and survivors.",
         "kind": "numbered"},
    ]


# ────────────────────────────────────────────────────────────────────────────
# Statutory frameworks (selected by count type)
# ────────────────────────────────────────────────────────────────────────────

def aks_framework_paragraphs() -> list[P]:
    """Anti-Kickback Statute framework — mirrors Cerebral ¶¶ 34–49."""
    return [
        {"text":
            "Enacted in 1972, the main purpose of the federal Anti-Kickback "
            "Statute, 42 U.S.C. § 1320a-7b, is to protect patients and "
            "federal health care programs from fraud and abuse by curtailing the "
            "corrupting influence of money and financial incentives on health care "
            "decisions.",
         "kind": "numbered"},
        {"text":
            "The federal Anti-Kickback Statute makes it a crime to knowingly and "
            "willfully offer, pay, solicit or receive any remuneration to induce a "
            "person: (1) to refer an individual to a person for the furnishing of "
            "any item or service covered under a federal health care program; or "
            "(2) to purchase, lease, order, arrange for or recommend any good, "
            "facility, service, or item covered under a federal health care "
            "program. 42 U.S.C. § 1320a-7b(b)(1) and (2).",
         "kind": "numbered"},
        {"text":
            "The term “any remuneration” encompasses any kickback, "
            "bribe, or rebate, direct or indirect, overt or covert, in cash or in "
            "kind. 42 U.S.C. § 1320a-7b(b)(1).",
         "kind": "numbered"},
        {"text":
            "The federal Anti-Kickback Statute has been interpreted by the United "
            "States Court of Appeals for the Third Circuit, as well as other "
            "federal courts, to cover any arrangement where one purpose of the "
            "remuneration was to obtain money for the referral of services or to "
            "induce further referrals. <i>See United States v. Greber</i>, 760 "
            "F.2d 68 (3d Cir.), <i>cert. denied</i>, 474 U.S. 988 (1985).",
         "kind": "numbered"},
        {"text":
            "Proof of an explicit quid pro quo is not required to show a violation "
            "of the Anti-Kickback Statute. Neither actual knowledge of the "
            "prohibitions of the Anti-Kickback Statute nor specific intent to "
            "commit a violation of the Anti-Kickback Statute is required. 42 "
            "U.S.C. § 1320a-7b(h).",
         "kind": "numbered"},
        {"text":
            "Compliance with the Anti-Kickback Statute is a condition of payment "
            "under the Medicare and Medicaid programs, and that condition applies "
            "regardless of which entity is submitting the claim to the government.",
         "kind": "numbered"},
        {"text":
            "Claims that result from a kickback scheme are <i>per se</i> false "
            "because the Anti-Kickback Statute prohibits the government from "
            "paying for services or items tainted by kickbacks. No further express "
            "or implied false statement is required to render such infected claims "
            "false, and none can render the claim legitimate.",
         "kind": "numbered"},
        {"text":
            "On March 23, 2010, as part of the Patient Protection and Affordable "
            "Care Act, the Anti-Kickback Statute was amended to clarify that all "
            "claims tainted by a violation of the Anti-Kickback Statute are a "
            "violation of the False Claims Act. 42 U.S.C. § 1320a-7b(g). That "
            "amendment codified long-standing case law. <i>See, e.g., United "
            "States ex rel. Schmidt v. Zimmer, Inc.</i>, 386 F.3d 235 (3d Cir. "
            "2004); <i>United States v. Rogan</i>, 517 F.3d 449 (7th Cir. 2008).",
         "kind": "numbered"},
    ]


def stark_framework_paragraphs() -> list[P]:
    """Physician Self-Referral Law (Stark) framework."""
    return [
        {"text":
            "The Physician Self-Referral Law, commonly known as the Stark Law, 42 "
            "U.S.C. § 1395nn, prohibits a physician from making referrals "
            "for designated health services (DHS) payable by Medicare to an "
            "entity with which the physician (or an immediate family member) has "
            "a financial relationship, unless an exception applies.",
         "kind": "numbered"},
        {"text":
            "Designated health services include, among other categories, "
            "clinical laboratory services; physical therapy, occupational therapy, "
            "and outpatient speech-language pathology services; radiology and "
            "certain other imaging services; durable medical equipment and "
            "supplies; parenteral and enteral nutrients, equipment, and supplies; "
            "prosthetics, orthotics, and prosthetic devices and supplies; home "
            "health services; outpatient prescription drugs; and inpatient and "
            "outpatient hospital services. 42 U.S.C. § 1395nn(h)(6).",
         "kind": "numbered"},
        {"text":
            "Unlike the Anti-Kickback Statute, the Stark Law is a strict-liability "
            "statute that does not require proof of intent. A physician violates "
            "Stark by making a referral that falls within the prohibition, "
            "regardless of whether the physician intended to do so.",
         "kind": "numbered"},
        {"text":
            "Medicare may not pay claims for DHS that result from a prohibited "
            "referral. 42 U.S.C. § 1395nn(g)(1). Claims for DHS submitted in "
            "violation of the Stark Law are false claims under the FCA.",
         "kind": "numbered"},
    ]


def upcoding_framework_paragraphs() -> list[P]:
    """Medical necessity / billing-code fraud framework."""
    return [
        {"text":
            "Healthcare providers participating in Medicare and Medicaid are "
            "required to submit claims that accurately identify the services "
            "furnished using Current Procedural Terminology (CPT) and Healthcare "
            "Common Procedure Coding System (HCPCS) codes promulgated by CMS and "
            "the American Medical Association. Each CPT/HCPCS code corresponds to "
            "a specific level of service intensity and a corresponding "
            "reimbursement amount.",
         "kind": "numbered"},
        {"text":
            "Medicare regulations require that services be reasonable and "
            "necessary for the diagnosis or treatment of illness or injury. 42 "
            "U.S.C. § 1395y(a)(1)(A). Submitting a claim for a service that "
            "is not medically necessary, or for a service at a higher level of "
            "intensity than was actually furnished (“upcoding”), is the "
            "submission of a false claim under the FCA.",
         "kind": "numbered"},
        {"text":
            "By enrolling in Medicare and by submitting each claim, the provider "
            "expressly certifies compliance with all applicable Medicare statutes, "
            "regulations, and program instructions, including the medical-necessity "
            "and accurate-coding requirements. <i>See</i> CMS-1500 claim form, "
            "Item 31 (provider certification). A claim that violates those "
            "requirements is materially false within the meaning of <i>Universal "
            "Health Services, Inc. v. United States ex rel. Escobar</i>, 579 U.S. "
            "176 (2016).",
         "kind": "numbered"},
    ]


# ────────────────────────────────────────────────────────────────────────────
# Terminal blocks (unnumbered)
# ────────────────────────────────────────────────────────────────────────────

def jury_demand_block() -> list[P]:
    """Demand for jury trial (unnumbered)."""
    return [
        {"text":
            "Relator, on behalf of herself and the United States, demands a jury "
            "trial on all claims alleged herein.",
         "kind": "unnumbered"},
    ]


def place_of_trial_block(city: str, state: str) -> list[P]:
    """Designated place of trial (unnumbered)."""
    return [
        {"text":
            f"Plaintiff hereby designates {city}, {state} as the place of trial.",
         "kind": "unnumbered"},
    ]


def prayer_for_relief_items(per_claim_penalty_usd: int = 27_894) -> list[P]:
    """Prayer for relief — lettered (a)–(f).

    ``per_claim_penalty_usd`` defaults to the 2024 DOJ-adjusted FCA per-claim
    civil penalty cap (28 C.F.R. § 85.5). Update annually when DOJ
    publishes a new inflation adjustment.
    """
    return [
        {"text":
            "Award the United States damages in the amount of three times the "
            "actual damages it sustained because of the false claims and fraud "
            "alleged within this Complaint, as provided by the False Claims Act, "
            "31 U.S.C. §§ 3729 <i>et seq.</i>;",
         "kind": "prayer", "letter": "a"},
        {"text":
            f"Impose civil penalties of $" f"{per_claim_penalty_usd:,}" + " for each "
            "and every false claim that Defendant presented to the United States "
            "and/or its agencies;",
         "kind": "prayer", "letter": "b"},
        {"text":
            "Award Relator pre- and post-judgment interest, reasonable "
            "attorneys’ fees, costs, and expenses necessarily incurred in "
            "bringing and pressing this case;",
         "kind": "prayer", "letter": "c"},
        {"text":
            "Grant permanent injunctive relief to prevent any recurrence of the "
            "False Claims Act violations for which redress is sought in this "
            "Complaint;",
         "kind": "prayer", "letter": "d"},
        {"text":
            "Award Relator the maximum amount allowable under the False Claims "
            "Act, 31 U.S.C. § 3730(d);",
         "kind": "prayer", "letter": "e"},
        {"text": "Such other relief the Court deems proper.",
         "kind": "prayer", "letter": "f"},
    ]


# ────────────────────────────────────────────────────────────────────────────
# Section-selection helpers
# ────────────────────────────────────────────────────────────────────────────

def select_framework_sections(count_descriptions: list[str]) -> list[str]:
    """Pick which statutory framework sections to include given count text.

    Returns a list of section keys in canonical order:
      ``aks``, ``stark``, ``upcoding``

    A count description containing kickback/AKS keywords selects AKS; one
    containing Stark/self-referral selects Stark; the default for any
    upcoding/medical-necessity/E&M/UDS scheme selects upcoding. If no
    keywords match, returns ``['upcoding']`` as a defensive default so the
    PDF always has at least one statutory framework section.
    """
    text = " ".join((d or "").lower() for d in count_descriptions)
    keys: list[str] = []
    if any(k in text for k in ("kickback", "aks", "anti-kickback",
                                "remuneration", "inducement")):
        keys.append("aks")
    if any(k in text for k in ("stark", "self-referral", "self referral",
                                "physician self")):
        keys.append("stark")
    if any(k in text for k in ("upcod", "e&m", "evaluation and management",
                                "medical necessity", "uds", "urine drug",
                                "billing code", "opioid", "phantom", "unbundl")):
        keys.append("upcoding")
    return keys or ["upcoding"]


_FRAMEWORK_HEADINGS = {
    "aks": "THE FEDERAL ANTI-KICKBACK STATUTE",
    "stark": "THE PHYSICIAN SELF-REFERRAL LAW",
    "upcoding": "MEDICAL NECESSITY AND ACCURATE CODING REQUIREMENTS",
}

_FRAMEWORK_FUNCS = {
    "aks": aks_framework_paragraphs,
    "stark": stark_framework_paragraphs,
    "upcoding": upcoding_framework_paragraphs,
}


def framework_section(key: str) -> tuple[str, list[P]]:
    """Return ``(heading, paragraphs)`` for one framework section key."""
    return _FRAMEWORK_HEADINGS[key], _FRAMEWORK_FUNCS[key]()
