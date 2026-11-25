---
doctrine: reasonable_and_necessary_coverage
citation: "42 U.S.C. § 1395y(a)(1)(A)"
court_year: "statutory"
verification_status: verified
elements:
  - "1395y(a)(1)(A): Medicare does NOT pay for items or services that 'are not reasonable and necessary for the diagnosis or treatment of illness or injury or to improve the functioning of a malformed body member' — the foundational coverage-exclusion provision underlying essentially every medical-necessity FCA theory."
  - "Reasonable-and-necessary is operationalized downstream through National Coverage Determinations (NCDs), Local Coverage Determinations (LCDs), and CMS manual provisions (e.g., MBPM ch. 15) that specify concrete clinical criteria for specific HCPCS/CPT codes — the statute itself is a general standard; the LCD/NCD/manual text is the CONTEMPORANEOUS, code-specific rule that actually governs a given claim."
  - "For emergency services under 1395dd, reasonable-and-necessary determinations are made based on the information available to the treating provider AT THE TIME (presenting symptoms), not the eventual diagnosis — a narrow statutory carve-out, not the general rule for non-emergency billing."
our_application_rule: >
  1395y(a)(1)(A) is the STATUTORY ANCHOR that gives an LCD/NCD/manual-provision violation
  its FCA teeth — plead the specific, contemporaneous LCD/NCD/manual text as the
  regulatory_predicate_evidence_ids anchor (per counsel_build.md's existing convergence-
  floor requirement), and cite 1395y(a)(1)(A) as the statutory hook that makes violating
  that specific coverage criterion a "reasonable and necessary" failure rather than a
  bare policy preference. CRITICAL execution discipline (per the failure-mode taxonomy):
  verify the LCD/NCD was IN FORCE, under the CORRECT MAC's jurisdiction, for the state
  where the claim was rendered, AS OF THE CLAIM DATE — not today's rule, and not a
  facially-similar LCD issued by the wrong Medicare Administrative Contractor. A
  documented taxonomy failure pled a Novitas LCD (L34892) that NEVER GOVERNED the state
  in question; the correct WPS LCD (L38841) did not even exist until 2021, after the
  claims at issue. Run `scripts/lcd_lookup.py --hcpcs CODE --effective-on YYYY-MM-DD`
  against the MAC that actually has jurisdiction over the claim's service state, not
  the provider's mailing address or a default/assumed MAC. A wrong-MAC citation is not a
  harmless error — it is a dead anchor that survives nothing on cross-examination and
  can singlehandedly kill a count that was otherwise well-evidenced.
common_rebuttals:
  - "Defense: the cited LCD/NCD was not the controlling authority for this state/MAC/date (the wrong-MAC or wrong-effective-date attack) — this is DISPOSITIVE if true and is entirely avoidable; always confirm MAC jurisdiction + effective-date window via lcd_lookup.py before citing an LCD as an anchor, never assume based on a facially similar rule from an adjacent MAC."
  - "Defense: reasonable-and-necessary is a matter of clinical judgment, not a bright-line rule, and defendant's clinical judgment was reasonable — where the LCD/NCD states an objective, checkable criterion (a frequency limit, a diagnosis-code requirement, an equipment/certification prerequisite), rebut that this is not a discretionary judgment call but a literal rule-text failure; where the criterion is genuinely a clinical-judgment standard, see the AseraCare/Winter card for the applicable framework instead."
  - "Defense: the LCD's frequency/utilization figures are a guideline, not a rigid cap (the 'not a rigid cap' defense, documented in the taxonomy as a 30% discount driver even when it does not fully defeat the count) — assess whether the specific LCD text uses mandatory ('shall not exceed') vs. advisory ('generally,' 'in most cases') language before pleading a frequency violation as an unqualified per-se breach; where the language is advisory, plead it as strong corroborating evidence rather than an unqualified per-se anchor to avoid an easily-won partial discount."
---

# Medicare "Reasonable and Necessary" Coverage Standard (42 U.S.C. § 1395y(a)(1)(A))

## Statutory text and structure

42 U.S.C. § 1395y(a)(1)(A) is the general Medicare coverage-exclusion provision: Medicare
does not pay for items or services that "are not reasonable and necessary for the
diagnosis or treatment of illness or injury or to improve the functioning of a malformed
body member." This is a broad, general statutory standard — it is operationalized on a
code-by-code and condition-by-condition basis through National Coverage Determinations
(NCDs), Local Coverage Determinations (LCDs) issued by individual Medicare Administrative
Contractors (MACs), and CMS manual provisions (e.g., Medicare Benefit Policy Manual ch.
15). These downstream instruments — not the bare statutory text — supply the concrete,
checkable clinical/administrative criteria that make a "not reasonable and necessary"
theory pleadable with particularity.

A narrow statutory carve-out applies to emergency services under § 1395dd: for those,
reasonable-and-necessary determinations are assessed based on information available to
the treating provider at the time (presenting symptoms), not the eventual diagnosis —
this is the exception, not the general framework for non-emergency billing theories.

## Our application

The statute is the ANCHOR that gives an LCD/NCD violation FCA significance — a medical-
necessity count should always cite both: (1) 1395y(a)(1)(A) as the statutory basis for
the coverage exclusion, and (2) the SPECIFIC, contemporaneous LCD/NCD/manual provision
governing the HCPCS/CPT code(s) at issue, as the regulatory_predicate_evidence_ids anchor
counsel_build.md already requires per its convergence-floor discipline.

**The dominant execution failure to avoid** (directly documented in the failure-mode
taxonomy): citing the WRONG MAC's LCD, or an LCD that was not yet in force as of the
claim date. One case pled a Novitas LCD (L34892) that never governed the state where the
claims were rendered; the correct WPS LCD (L38841) for that state did not exist until
2021-04-25 — after the claims at issue. This killed the count on verification, not on
the merits. The fix is entirely mechanical and should be applied every time a
reasonable-and-necessary count is built:

1. Determine the state where the SERVICE was rendered (not the provider's business
   address if different).
2. Determine which MAC has jurisdiction over Medicare Part B claims in that state as of
   the claim date (MAC jurisdiction assignments have changed over time; do not assume
   today's MAC map applies retroactively).
3. Run `scripts/lcd_lookup.py --hcpcs <CODE> --effective-on <YYYY-MM-DD>` against that
   specific MAC/date combination, not a facially similar LCD from an adjacent MAC.
4. Confirm the LCD's effective date window actually covers the claim date range — an LCD
   that postdates the claims cannot anchor them.

**Mandatory vs. advisory language.** Before pleading an LCD frequency/utilization limit
as an unqualified per-se violation, check whether the LCD's own text is mandatory
("shall not exceed," "will be denied") or advisory ("generally," "in the majority of
cases," framed as a utilization guideline rather than a cap). The taxonomy documents a
"frequency framework is not a rigid cap" defense reducing a count by 30% even where the
LCD language was ambiguous on this point — pleading precision here avoids an
easily-conceded discount.

## Common rebuttals / defense playbook

See frontmatter. Wrong-MAC/wrong-date citation is dispositive and entirely
build-time-avoidable; the clinical-judgment defense applies where the LCD criterion is
genuinely discretionary (route to the AseraCare/Winter card) rather than a bright-line
rule; the rigid-cap-vs-guideline distinction should be assessed from the LCD's actual
text before pleading a frequency count as unqualified per-se.

## Verification note

42 U.S.C. § 1395y(a)(1)(A) statutory text independently verified via WebSearch against
Cornell LII / govinfo.gov sources. This is a statutory citation, not case law, so
CourtListener case-verification does not apply; the operative text quoted above
("not reasonable and necessary for the diagnosis or treatment of illness or injury or to
improve the functioning of a malformed body member") matches the codified language as
reported by these sources. Recommend re-confirming against govinfo.gov/uscode.house.gov
directly before filing use, per standard statutory-citation practice.
