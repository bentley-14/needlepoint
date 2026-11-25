---
doctrine: aks_perse_falsity
citation: "42 U.S.C. § 1320a-7b(g); United States v. Greber, 760 F.2d 68 (3d Cir. 1985)"
court_year: "3d Cir. 1985 (Greber); AKS-FCA link codified 2010 (1320a-7b(g))"
verification_status: verified
elements:
  - "1320a-7b(g): 'a claim that includes items or services resulting from a violation of this section constitutes a false or fraudulent claim for purposes of' the FCA — the 2010 ACA amendment that made the AKS-to-FCA link EXPLICIT and STATUTORY (per-se), not merely inferential (the 'Regal nexus' theory courts used pre-2010)."
  - "Greber (AKS criminal 'one purpose' test): if ANY purpose of a payment — even where the payment also compensates a legitimate service — is to induce referrals, the payment violates the Anti-Kickback Statute. A legitimate purpose does NOT immunize a payment that also has an inducement purpose."
  - "AKS safe harbors (42 CFR 1001.952) — employment, personal services/management contracts, space/equipment rental, investment interests, GPOs — provide an affirmative defense ONLY if ALL elements of the specific safe harbor are satisfied; partial compliance does not qualify."
our_application_rule: >
  Since the 2010 amendment (1320a-7b(g)), a proven AKS violation makes the resulting
  claims per-se false under the FCA — this is a STATUTORY shortcut around Escobar
  materiality analysis for the FALSITY element (though scienter and damages must still be
  separately established). Plead an AKS-predicated FCA count as: (1) identify the
  remuneration (payment, referral fee, above-FMV lease/services, free/discounted item)
  flowing between a referral source and a provider of Medicare-reimbursable items/services;
  (2) apply Greber's one-purpose test — even a MIXED-purpose payment (partly for a
  legitimate service, partly to induce referrals) violates the AKS if inducement is ANY
  purpose, so do not let a "but the consulting agreement had real duties" defense
  automatically defeat the count; (3) affirmatively address the applicable safe harbors —
  if the defendant's fact pattern is close to a safe harbor, either show a specific
  element is unmet (e.g., payment not at FMV, no written agreement, term under a year for
  a services agreement, aggregate compensation not set in advance) or concede the count is
  vulnerable and downgrade to a lead rather than a pled count. Do not plead an AKS-taint
  count on referral-relationship-plus-payment alone without addressing safe harbors, since
  `defense.md`'s `safe_harbor` attack type is the most severe form the taxonomy records
  (a single safe_harbor attack in the corpus killed a Stark count outright — the AKS
  analog is equally severe if unaddressed).
common_rebuttals:
  - "Defense: the payment fits an AKS safe harbor (employment, personal services, space/equipment rental, GPO, investment interest) — this is DISPOSITIVE unless the count's own pleading shows a specific unmet element of that safe harbor; address safe harbors affirmatively at build time, not reactively."
  - "Defense: the payment had a legitimate business purpose (real services were rendered) — rebut with Greber's one-purpose test: a legitimate purpose does not immunize a payment where inducement of referrals was ALSO a purpose; plead facts suggesting a referral-volume/timing correlation with the payment."
  - "Defense: no proven AKS violation exists, so 1320a-7b(g)'s per-se taint doesn't apply — correct if true; without an underlying AKS violation established, the count must rest on ordinary Escobar materiality/scienter analysis rather than the per-se shortcut, and should be pled accordingly (do not assert per-se falsity you haven't earned)."
---

# Anti-Kickback Statute Per-Se Taint (42 U.S.C. § 1320a-7b(g)) + Greber

## Holding

**42 U.S.C. § 1320a-7b(g)**, added by the 2010 Affordable Care Act, provides: a claim for
items or services "resulting from" a violation of the Anti-Kickback Statute (42 U.S.C. §
1320a-7b(b)) "constitutes a false or fraudulent claim" for FCA purposes. This codified
what courts had previously reached only through an inferential "Regal nexus" theory
(named for an earlier line of cases implying the AKS-FCA link) — post-2010, a proven AKS
violation makes the tainted claims false PER SE, without a separate Escobar-style
materiality showing on the falsity element specifically (scienter and causation/damages
are still independently required).

**United States v. Greber**, 760 F.2d 68 (3d Cir. 1985), a leading AKS criminal-liability
opinion, established the "one purpose" test: a payment or fee arrangement violates the
AKS if inducing referrals was **any** purpose behind it — even where the payment also
compensates genuine, valuable services. The existence of a legitimate business
justification does not immunize a payment that also functions (in part) to induce
referrals.

The AKS safe harbors, codified at 42 CFR § 1001.952 (employment; personal
services/management contracts; space/equipment rental; investment interests; group
purchasing organizations, among others), provide an affirmative defense — but only where
**every** element of the specific harbor invoked is satisfied. Partial or approximate
compliance does not qualify for protection.

## Our application

An AKS-predicated FCA count should be built in three steps:

1. **Identify the remuneration.** Payment, referral fee, above-fair-market-value lease or
   services arrangement, or free/discounted item or service flowing between a referral
   source and a Medicare-reimbursable provider.
2. **Apply Greber's one-purpose test.** A defense that "the consulting agreement covered
   real duties" or "the lease was for actual office space" does not defeat the count if
   inducement of referrals was ALSO a purpose — look for a referral-volume or
   referral-timing correlation with the payment/agreement as the corroborating fact.
3. **Confront safe harbors affirmatively.** Before pleading the count, walk through the
   safe harbors that could plausibly apply on these facts and identify the specific unmet
   element (not at FMV; no written/signed agreement; term under one year for a personal
   services arrangement; aggregate compensation not set in advance of the term). If no
   specific gap can be identified, the count is safe-harbor-vulnerable and should be
   downgraded to a lead rather than pled as ready — the taxonomy records a single
   `safe_harbor` attack killing a count outright (the most severe per-attack lethality
   in the corpus), and an AKS-taint count left unaddressed on safe harbors is equally
   exposed.

## Common rebuttals / defense playbook

See frontmatter. Safe harbor is the dispositive defense to pre-empt at build time;
legitimate-business-purpose is defeated by Greber's one-purpose framing when a referral
correlation is pled; absence of a proven underlying AKS violation forecloses the per-se
shortcut and forces the count back onto ordinary materiality/scienter analysis.

## Verification note

Citation verified via CourtListener `verify "Greber" --reporter "760 F.2d 68"` — exact
match, *United States v. Greber*, 3d Cir., decided 1985-05-15. 42 U.S.C. § 1320a-7b(g) is
statutory text (2010 ACA amendment); not a case citation to verify via CourtListener, but
its text and 2010 enactment are well-documented in AKS/FCA secondary sources and CMS/OIG
guidance. Greber's "one purpose" holding language is paraphrased, not block-quoted.
