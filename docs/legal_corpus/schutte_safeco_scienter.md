---
doctrine: scienter
citation: "United States ex rel. Schutte v. SuperValu Inc., 598 U.S. 739 (2023); Safeco Ins. Co. of America v. Burr, 551 U.S. 47 (2007)"
court_year: "SCOTUS, 2023 (Schutte); SCOTUS, 2007 (Safeco)"
verification_status: verified
elements:
  - "FCA scienter (31 USC 3729(b)(1)) is knowledge, deliberate ignorance, or reckless disregard — a SUBJECTIVE standard measured by what the defendant actually thought/believed at the time it acted."
  - "Schutte holds that a defendant's subjective belief that its claims might be fraudulent is what counts, even if a court later finds the underlying legal question was objectively ambiguous and a reasonable person could have read the rule the defendant's way."
  - "Safeco (decided in the FCRA willfulness context but broadly cited for the objective-reasonableness safe harbor) holds that a defendant does not act with reckless disregard if it acted on an OBJECTIVELY reasonable interpretation of an ambiguous legal requirement and no authoritative guidance warned it away from that interpretation — but Schutte confirms this defense fails once a court determines the defendant did NOT subjectively believe its own reasonable-sounding interpretation at the time."
  - "The relevant time for the scienter inquiry is when the claims were submitted, not with the benefit of the ambiguity a court later identifies in litigation."
our_application_rule: >
  Treat the Safeco objective-reasonableness defense as the pre-discovery practical shield
  it functions as, while pleading affirmatively into Schutte's subjective-belief holding to
  blunt it. At the pleading stage, defendant will invoke Safeco: "the rule was ambiguous,
  a reasonable provider could read it our way, so there is no scienter." Pre-empt this by
  pleading SUBJECTIVE-belief evidence wherever it exists — internal communications,
  billing-consultant warnings, denied prior claims for the same code/pattern, compliance
  audit findings, or a documented change in billing behavior correlated with a compliance
  warning (the "did they know and do it anyway" fact pattern). Where no such document is
  in hand pre-filing (the common case on PUF-only evidence), plead scienter via
  circumstantial subjective-knowledge indicia: an abrupt volume/pattern shift with no
  clinical explanation, sustained billing at percentile extremes inconsistent with
  inadvertence, or exclusion/audit history putting the provider on notice. Do NOT
  concede the objective-ambiguity framing by default — Schutte is explicitly pro-relator
  IN DISCOVERY: it forecloses the defendant from winning on a paper motion by pointing to
  a plausible legal reading, because the subjective-belief question is inherently a fact
  question for the factfinder once there is ANY evidence the defendant doubted its own
  compliance. This means the "ambiguity tax" should be priced into round-1 damages
  (25-30% discount observed empirically) but should never be treated as a scienter kill
  at the pleading stage — the correct target is get-to-discovery, where Schutte's
  subjective standard, not Safeco's objective one, controls.
common_rebuttals:
  - "Defense: our interpretation of [ambiguous LCD/regulation] was objectively reasonable and no authoritative guidance warned us otherwise (Safeco safe harbor) — counter with Schutte: reasonableness of the LEGAL POSITION is irrelevant if the defendant subjectively knew or suspected the claims were false when submitted; plead subjective-knowledge indicia."
  - "Defense: the ambiguity was genuine and industry-wide, so no single provider could have known — counter that Schutte requires only evidence of THIS defendant's subjective doubt, not industry-wide certainty of illegality; a documented compliance warning specific to this provider defeats an industry-ambiguity defense."
  - "Defense: relator has no smoking-gun document showing actual knowledge — counter that reckless disregard and deliberate ignorance independently satisfy scienter; an extreme, sustained, unexplained pattern plus a provider sophisticated enough to know the applicable billing rules supports an inference of at least reckless disregard sufficient to survive a motion to dismiss, with the subjective question reserved for summary judgment/trial under Schutte."
---

# Schutte Subjective Scienter + Safeco Objective-Reasonableness Shield

## Holding

*United States ex rel. Schutte v. SuperValu Inc.*, 598 U.S. 739 (2023), unanimously
resolved a circuit split over whether the FCA's scienter element (31 U.S.C. § 3729(b)(1):
actual knowledge, deliberate ignorance, or reckless disregard) is judged by an
**objective** standard (would a reasonable person in the defendant's position have known)
or a **subjective** one (what did this defendant actually believe). The Court held
scienter is **subjective**: "What matters for an FCA case is whether the defendant knew
the claims were false. Thus, if [a defendant]'s subjective belief was that its practice
was not okay… then this too is sufficient for a finding of scienter." A defendant cannot
escape liability merely because, in hindsight, a court finds an ambiguous regulation
could have been read the defendant's way — if the defendant did not actually believe its
own favorable reading at the time.

*Safeco Ins. Co. of America v. Burr*, 551 U.S. 47 (2007), decided in the Fair Credit
Reporting Act "willfulness" context, supplies the objective-reasonableness framework that
FCA defendants have long imported by analogy (e.g., the pre-*Schutte* circuit split
*Schutte* resolved): a defendant does not act recklessly if it followed an objectively
reasonable interpretation of an ambiguous requirement and no authoritative guidance (a
court of appeals decision, a formal agency ruling) had warned it away from that reading.
*Schutte* did not overrule *Safeco*'s framework outright but confined it: objective
reasonableness can negate scienter only where it reflects the defendant's own actual,
good-faith belief — it is not a free-standing defense available regardless of what the
defendant subjectively thought.

## Our application — the pre-discovery practical shield, but pro-relator in discovery

This is a nuanced doctrine to plead against, and the nuance matters at different stages:

- **At the motion-to-dismiss / pleading stage**, *Safeco*-style objective-reasonableness
  arguments remain the single most common defense-counsel move on an ambiguous LCD,
  billing rule, or supervision requirement, and they function as a practical shield —
  defendants raise them early and often, and courts sometimes credit them absent
  contrary allegations. The failure-mode taxonomy shows this attack landing in 24 of 278
  recorded attacks, 0 kills, but a reliable 25-30% discount every round it appears —
  it should be pre-priced in `counsel_build`, not re-litigated from scratch each
  adversarial round.
- **But *Schutte* is explicitly pro-relator once the case reaches discovery.** *Schutte*
  forecloses defendants from winning on the papers by pointing to *a* reasonable reading
  of the rule; the question is what THIS defendant subjectively believed, which is
  quintessentially a fact question. A complaint that pleads even modest
  subjective-knowledge indicia (an abrupt pattern shift, a documented warning, prior
  denied claims, exclusion/audit history) should survive dismissal on scienter and shift
  the fight to discovery, where *Schutte*'s subjective standard controls and the
  defendant's internal documents (compliance memos, denied-claim correspondence, coding
  consultant advice) become the relevant evidence — not the after-the-fact reasonableness
  of the legal position.

**Practical drafting rule**: plead the objective-ambiguity discount into round-1 damages
expectations (don't overstate the headline number assuming zero scienter friction), but
do not treat a bare Safeco-style objective-reasonableness argument as dispositive at the
pleading stage — cite *Schutte* affirmatively to route the case to discovery on the
subjective question.

## Common rebuttals / defense playbook

- Objectively-reasonable-interpretation defense (the Safeco shield) → rebut with Schutte's
  subjective-belief holding plus any circumstantial subjective-knowledge indicia in hand.
- Industry-wide-ambiguity defense → rebut that Schutte asks about THIS defendant's belief,
  not industry consensus.
- No-smoking-gun-document defense → rebut with reckless disregard / deliberate ignorance as
  independently sufficient, reserving full subjective resolution for discovery.

## Verification note

Both citations verified via CourtListener: `Schutte v. SuperValu Inc., 598 U.S. 739`
(SCOTUS, decided 2023-06-01, exact-cite match); `Safeco Ins. Co. of America v. Burr, 551
U.S. 47` (SCOTUS, decided 2007-06-04, exact-cite match). Holding language above is
paraphrased from the widely-reported opinion summary; the bracketed quote from Schutte
("What matters for an FCA case…") reflects the Court's own stated formulation as
reported in secondary sources and should be re-quoted from the slip opinion directly
before use in a filing.
