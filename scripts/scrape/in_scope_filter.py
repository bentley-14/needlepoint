"""In-scope filter for DOJ press releases (and analogous OIG/state pages).

Two layers, both cheap regex/keyword matchers — no LLM calls.

Layer 1 (slug_in_scope):  decide solely from the URL slug. Used pre-fetch by
                          the scraper to avoid HTTP requests on pages that are
                          clearly not enforcement actions (news, index, MOUs).
                          NEVER drops based on payer keywords in slug — too
                          noisy ("medical" inside a slug can match Medicaid
                          regex unintentionally).

Layer 2 (content_in_scope): decide from title + body. Drops only hard
                            negatives — pure regulatory/personnel pages,
                            non-healthcare federal-contract fraud, and pages
                            that explicitly state a non-Medicare payer is the
                            only victim. Calibrated to ≤1% false-negative rate
                            against our 677 confirmed-positive label-ready URLs.

Project scope rules (from MEMORY):
- Medicare-detectable fraud only (2013+).
- Medicaid-only, TRICARE-only, private-payer-only, FECA/OWCP are OUT_OF_SCOPE.
- Non-healthcare federal-contract fraud (DOE/NASA/SBIR/USAID/DBE) is OUT.
- Personnel honors / regulatory announcements / press-release index pages
  have no defendants — skip.
"""

from __future__ import annotations

import re
from typing import Optional


# ----------------------------------------------------------------------
# Layer 1 — URL slug filter (pre-fetch, very cheap)
# ----------------------------------------------------------------------

# Slug patterns indicating non-press-release pages (no enforcement action)
# Note: opaque docket-style slugs like "2016_0520_RMingle" turn out to be real
# press releases (DOJ uses them for some districts), so we keep those.
_SLUG_NON_CASE_EXACT = re.compile(
    r"^("
    r"news|index|home|archive|page-?\d+|search"
    r")$",
    re.IGNORECASE,
)

# Slug fragments indicating ROBUST non-enforcement content. We only drop pages
# whose slug contains UNAMBIGUOUS non-case markers — not anything that looks
# like a payer hint, since healthcare-fraud press releases routinely use
# payer terms in their slugs.
_SLUG_NON_CASE_FRAGMENT = re.compile(
    r"(?:^|[-_/])("
    r"investigative-teams?-honored"
    r"|teams?-honored-hhs-oig"
    r"|memorandum-of-understanding"
    r"|fiscal-year-\d{4}-statistics"
    r"|year-end-summary"
    r"|press-release-archive"
    r"|civil-monetary-penalty-authorities"
    r"|exclusion-authorities"
    r"|attorney-general-sworn-in"
    r"|attorney-general-confirmed"
    r"|attorney-general-nominated"
    r"|us-attorney-(?:sworn-in|nominated|confirmed|takes-office)"
    r"|public-information-officer"
    r"|speech-by-(?:attorney-general|inspector-general|u-s-attorney)"
    r"|remarks-by-(?:attorney-general|inspector-general|u-s-attorney)"
    r"|swearing-in-ceremony"
    r")(?:$|[-_/])",
    re.IGNORECASE,
)


def slug_in_scope(url: str) -> tuple[bool, Optional[str]]:
    """Layer 1 filter — slug only.

    Conservative: only drops slugs that UNAMBIGUOUSLY indicate non-case content
    (news listing pages, personnel announcements, regulatory MOUs). Does NOT
    use payer keywords in slug — too noisy.

    Returns (in_scope, reason_if_oos).
    """
    slug = url.rstrip("/").split("/")[-1].lower()

    if _SLUG_NON_CASE_EXACT.match(slug):
        return False, "slug_non_case_page"

    if _SLUG_NON_CASE_FRAGMENT.search(slug):
        return False, "slug_non_case_fragment"

    return True, None


# ----------------------------------------------------------------------
# Layer 2 — title + body content filter (post-fetch, still cheap)
# ----------------------------------------------------------------------

# Medicare nexus terms. Strict — only literal Medicare/Tricare references.
# NOTE: deliberately excludes "federal health care program(s)" — that statute
# phrase covers Medicare AND Medicaid AND Tricare, so it's not a reliable
# Medicare-nexus marker. Accepting that phrase would let Medicaid-only press
# releases through.
_MEDICARE_TERMS = re.compile(
    r"\b("
    r"medicare|tricare|"
    r"medicare advantage|medicare part [abcd]"
    r")\b",
    re.IGNORECASE,
)

# Medicaid / state-program terms
_MEDICAID_TERMS = re.compile(
    r"\b(medicaid|tenncare|medi-?cal|husky\s+(?:plan|health)|mainecare|"
    r"soonercare|kancare|healthchoice|fidelis|empire plan)\b",
    re.IGNORECASE,
)

# Private payer terms
_PRIVATE_PAYER_TERMS = re.compile(
    r"\b(blue cross|bcbs|aetna|cigna|united\s?health(?:care)?|humana|"
    r"amerihealth|optum|champva|fehb|federal employees? health|"
    r"nj state health benefits?|amtrak (?:health )?plan|"
    r"state health benefits program)\b",
    re.IGNORECASE,
)

# Non-healthcare federal-contract scheme types (strict — must be obvious)
_NON_HEALTHCARE_HARD_NEGATIVES = re.compile(
    r"\b("
    r"SBIR (?:grant|fraud)|"
    r"NASA (?:contract|grant|sbir)|"
    r"USAID contract|"
    r"DBE certification|"
    r"disadvantaged business enterprise|"
    r"DoD (?:contract|procurement) fraud|"
    r"CIA contract|"
    r"EPA grant fraud|"
    r"HUD Section 8 fraud|"
    r"nuclear (?:waste|cleanup) (?:contract|fraud)|"
    r"USDA school lunch fraud|"
    r"bridge construction fraud|"
    r"highway construction fraud"
    r")\b",
    re.IGNORECASE,
)

# Title patterns for pure regulatory / personnel announcements (no defendant)
_REGULATORY_TITLE = re.compile(
    r"^("
    r"(?:Justice Department|HHS|DOJ) (?:Announces|Releases|Issues|Adopts) "
    r"(?:New |Updated |Final )?(?:Rule|Regulation|Policy|Guidance|"
    r"Memorandum|Procedures|Statistics|Annual Report|Inspector General Recommendations)|"
    r"(?:U\.?S\.? Attorney|AUSA|Federal Judge) (?:Sworn In|Nominated|Confirmed|"
    r"Takes Oath|Appointed|Honored)$|"
    r"Inspector General (?:Testimony|Speech|Remarks|Statement) (?:Before|To)|"
    r"Fiscal Year \d{4} (?:Statistics|Summary|Report)|"
    r"Year-End Statistics"
    r")",
    re.IGNORECASE,
)


def content_in_scope(title: str, body: str) -> tuple[bool, Optional[str]]:
    """Layer 2 filter — title + body.

    Conservative: only drops hard negatives. Calibrated against the 677 URLs
    that source our 823 confirmed-positive label-ready rows; aims for ≤1% FN.

    Returns (in_scope, reason_if_oos).
    """
    title = (title or "").strip()
    body = body or ""

    # Scan title + first 16K of body for most checks. For the Medicare-nexus
    # check we scan the full body — multi-defendant takedown press releases
    # can bury the Medicare mention deep in the body for individual defendants.
    sample = (title + " " + body[:16000])
    full_text_for_medicare = title + " " + body

    # Drop pure regulatory / personnel-announcement titles
    if _REGULATORY_TITLE.match(title):
        return False, "regulatory_or_personnel_announcement"

    # Drop hard-negative non-healthcare federal-contract fraud (only when
    # there's no healthcare context anywhere in the sample)
    if _NON_HEALTHCARE_HARD_NEGATIVES.search(sample):
        if not re.search(
            r"\b(medicare|medicaid|tricare|health\s?care fraud|"
            r"physician|pharmac(?:y|ist)|clinic|hospital|"
            r"provider|patient)\b",
            sample,
            re.IGNORECASE,
        ):
            return False, "non_healthcare_federal_contract"

    # Medicare check uses the full body — takedown press releases often
    # mention specific payers per defendant deeper in the text.
    has_medicare = bool(_MEDICARE_TERMS.search(full_text_for_medicare))
    has_medicaid = bool(_MEDICAID_TERMS.search(sample))
    has_private = bool(_PRIVATE_PAYER_TERMS.search(sample))

    # Hard OOS: Medicaid mentioned + Medicare absent → Medicaid-only case
    if has_medicaid and not has_medicare:
        return False, "medicaid_only_no_medicare"

    # Hard OOS: private payer mentioned + Medicare absent + no Medicaid →
    # private-payer-only case (rarer but real)
    if has_private and not has_medicare and not has_medicaid:
        return False, "private_payer_only_no_medicare"

    # If no payer mentioned at all, we're cautious: keep unless other strong
    # OOS signals fired. Healthcare-fraud press releases that don't name a
    # payer usually still mention "healthcare fraud" or similar.
    if not has_medicare and not has_medicaid and not has_private:
        # Require at least some healthcare-fraud context to keep
        if not re.search(
            r"\b(health\s?care fraud|false claim|kickback|medical fraud|"
            r"unnecessary (?:procedure|test|service)|"
            r"phantom (?:billing|claim)|upcoding|"
            r"controlled substance|opioid|prescription fraud)\b",
            sample,
            re.IGNORECASE,
        ):
            return False, "no_healthcare_fraud_context"

    return True, None


# ----------------------------------------------------------------------
# CLI for quick testing
# ----------------------------------------------------------------------

if __name__ == "__main__":
    import json
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "test":
        samples = [
            ("https://www.justice.gov/opa/pr/21st-century-oncology-pay-26-million-settle-false-claims-act-allegations",
             "21st Century Oncology to Pay $26 Million to Settle False Claims Act Allegations",
             "Today, 21st Century Oncology agreed to pay $26 million to resolve allegations of Medicare false claims related to a physician physician kickbacks."),
            ("https://www.justice.gov/opa/pr/two-usao-edpa-civil-healthcare-fraud-investigative-teams-honored-hhs-oig",
             "Two USAO-EDPA Civil Healthcare Fraud Investigative Teams Honored by HHS-OIG",
             "Today, two investigative teams were honored for their work."),
            ("https://www.justice.gov/usao-ndfl/pr/florida-medicaid-fraud-conviction",
             "Florida Medicaid Fraud Conviction",
             "Defendant was convicted of Florida Medicaid fraud only. No mention of Medicare."),
            ("https://www.justice.gov/opa/pr/news", "DOJ News", ""),
            # Should KEEP (was a false negative under prior filter)
            ("https://www.justice.gov/usao-edtx/pr/former-hospital-cfo-charged-health-care-fraud",
             "Former Hospital CFO Charged with Health Care Fraud",
             "John Smith, former CFO of XYZ Hospital, was charged with submitting false claims to Medicare totaling $5 million."),
            # Should KEEP — podiatrist case
            ("https://www.justice.gov/usao-ct/pr/federal-jury-finds-brookfield-podiatrist-guilty-medicare-fraud",
             "Federal Jury Finds Brookfield Podiatrist Guilty of Medicare Fraud",
             "Dr. Smith was found guilty of Medicare fraud."),
        ]
        for url, title, body in samples:
            ok1, why1 = slug_in_scope(url)
            ok2, why2 = content_in_scope(title, body)
            verdict = "KEEP" if (ok1 and ok2) else "DROP"
            print(f"{verdict:5s} {url.rsplit('/',1)[-1][:55]:55s}  slug={ok1} ({why1 or 'ok'})  content={ok2} ({why2 or 'ok'})")
        sys.exit(0)

    print("usage: python -m scripts.extract.in_scope_filter test", file=sys.stderr)
