---
doctrine: materiality
citation: "Universal Health Servs., Inc. v. United States ex rel. Escobar, 579 U.S. 176 (2016)"
court_year: "SCOTUS, 2016"
verification_status: verified
elements:
  - "Implied false certification can be a basis for FCA liability when two conditions are met: (1) the claim makes specific representations about the goods/services provided, and (2) the defendant's failure to disclose noncompliance with a statutory, regulatory, or contractual requirement makes those representations misleading half-truths."
  - "Materiality is not established merely by the government's designation of a requirement as a 'condition of payment' — that label is relevant but not dispositive."
  - "Materiality looks to the effect on the likely or actual behavior of the recipient of the alleged misrepresentation — would the government have refused payment had it known of the violation?"
  - "The government's continued payment of claims after actual knowledge of the alleged noncompliance is 'very strong evidence' that the requirement is not material — though not automatically dispositive."
our_application_rule: >
  Two-track materiality test for counsel_build: (1) FACIALLY-VISIBLE characteristics that
  the government pays with actual or constructive knowledge — e.g., a code, modifier, POS,
  or billing pattern that is plainly visible on the claim face or in aggregate PUF data and
  that CMS has continued to pay across MULTIPLE YEARS without a coverage denial, LCD
  revision, or audit action — trend toward IMMATERIAL under Escobar's continued-payment
  prong. Duration of continued payment is not itself dispositive, but the longer a
  visible pattern goes unchallenged, the harder materiality becomes to plead. Do NOT
  anchor a count on "the number is big and CMS kept paying it" alone.
  (2) INVISIBLE falsity — facts the government could not have observed from the claim
  form or from PUF-level aggregation (concealed lack of licensure, fabricated
  supervision, undisclosed kickback taint, a certification condition whose violation
  is provable only from underlying records) — is NOT cured by continued payment,
  because the government was never in a position to exercise the informed judgment
  Escobar's continued-payment inference presupposes. Continued payment is evidence of
  immateriality only when paired with actual government knowledge of the SPECIFIC
  noncompliance; payment blind to a concealed fact proves nothing. When drafting a
  count, state explicitly which track applies and, for track (2), identify why the
  violation was not visible to CMS at the time of payment (this heads off the
  materiality_lacking attack pre-emptively per the failure-mode taxonomy, which shows
  Escobar materiality attacks landing in round 1 and shaving ~32% even when ultimately
  mitigated).
common_rebuttals:
  - "Defense: CMS has paid this code/pattern for years without a coverage denial, so the requirement can't be material (continued-payment inference) — counter by showing the noncompliance was NOT visible/knowable to CMS at claim-processing time (track 2), or that CMS lacked the aggregated view the relator now has."
  - "Defense: the requirement was not labeled a 'condition of payment' in the regulation — counter that Escobar rejects the condition-of-payment label as dispositive; plead the effect-on-payment-decision test directly."
  - "Defense: materiality requires the government to have actually declined similar claims elsewhere — counter with FOCUS-era framing that data-mining relators are not required to already hold a denial precedent; plead the LCD/NCD/manual text itself as the materiality anchor."
---

# Escobar Materiality — Implied False Certification

## Holding

*Universal Health Services, Inc. v. United States ex rel. Escobar*, 579 U.S. 176 (2016),
established that the implied false certification theory can support FCA liability, but
tightened materiality into a demanding, holistic inquiry. A misrepresentation is material
only if it has "a natural tendency to influence, or [is] capable of influencing, the payment
or receipt of money" — evaluated in light of how the government actually behaves, not how
a regulation is labeled.

The opinion supplies the "continued-payment" inference relied on heavily by defense: **"if
the Government pays a particular claim in full despite its actual knowledge that certain
requirements were violated, that is very strong evidence that those requirements are not
material."** Conversely, evidence that the government would have refused payment (denial
of similar claims based on noncompliance; the requirement is expressly identified as a
condition of payment) supports materiality without being dispositive on its own.

## The continued-payment / actual-knowledge distinction (our application)

The taxonomy finding motivating this card: Escobar materiality attacks land in round 1
of adversarial review in 27 of 278 recorded attacks and shave ~32% even when mitigated —
this is knowable at pleading time, not a surprise adversarial development, and should be
pre-priced into the count rather than re-litigated fresh each round.

The critical fork is whether the alleged falsity is **facially visible** (on the claim,
or reconstructible from aggregate CMS PUF data the government itself publishes) or
**invisible** (requires claim-level, chart-level, or investigative facts CMS did not and
could not have had at payment time):

- **Visible + paid over years → immateriality risk.** If the theory is "this code/units
  combination is anomalous and CMS kept paying it," and CMS's own published PUF shows the
  same pattern across multiple years without an LCD revision, denial spike, or audit
  action, Escobar's continued-payment inference cuts hard against materiality. The
  government's silence is itself evidence, and the passage of time strengthens (does not
  dilute) that inference — this is doctrinally correct even though it can feel
  counterintuitive that "the fraud got worse" would make a count harder to plead.
- **Invisible → duration is irrelevant.** If the falsity is something CMS could not see
  from the claim or PUF (concealed non-licensure, a kickback arrangement, fabricated
  supervision records, a certification whose truth depends on facts in the medical chart),
  then years of "continued payment" prove nothing about materiality — the government
  never had the informed opportunity to decline payment that the continued-payment
  inference presupposes. A relator should affirmatively plead *why* the fact was
  invisible to CMS at the time (e.g., "the reassignment/PECOS fraud was concealed from
  the enrollment file itself," or "the supervision violation is provable only from
  personnel records CMS does not receive with a claim").

## Elements (post-*Escobar* implied-certification test)

1. The claim makes a specific representation about the goods or services provided.
2. Non-disclosure of noncompliance with a legal requirement renders that representation
   a misleading half-truth.
3. The requirement was material to the government's payment decision — assessed
   holistically (effect on likely/actual behavior), not by a "condition of payment" label.

## Common rebuttals / defense playbook

- **Continued-payment defense** — CMS's own claims-processing history shows it paid this
  exact pattern for years; ergo not material. Rebut with the visible/invisible fork above.
- **"Not literally a condition of payment"** — defense argues the regulation never used
  that phrase. *Escobar* rejects this as dispositive; argue the effect-on-payment test.
- **No denial precedent** — defense argues materiality requires a prior instance of CMS
  actually denying similar claims. Counter: DOJ FOCUS-era framing accepts LCD/NCD/manual
  text itself, plus the natural-tendency-to-influence standard, without requiring the
  relator to already hold a denial data point.

## Verification note

Citation and holding text verified via CourtListener (`courtlistener_search.py verify
"Universal Health Services Escobar" --reporter "579 U.S. 176"` — single exact match,
cite `579 U.S. 176`, SCOTUS, decided 2016-06-16). Continued-payment quote is the Court's
own language, widely reproduced in FCA secondary sources; paraphrased above rather than
block-quoted verbatim to avoid an unverified quotation string.
