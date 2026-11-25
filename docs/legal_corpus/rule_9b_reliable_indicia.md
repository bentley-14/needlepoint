---
doctrine: rule_9b_particularity
citation: "United States ex rel. Ebeid v. Lungwitz, 616 F.3d 993 (9th Cir. 2010); United States ex rel. Grubbs v. Kanneganti, 565 F.3d 180 (5th Cir. 2009); Corsello v. Lincare, Inc., 428 F.3d 1008 (11th Cir. 2005); United States ex rel. Clausen v. Laboratory Corp. of America, 290 F.3d 1301 (11th Cir. 2002)"
court_year: "9th Cir. 2010; 5th Cir. 2009; 11th Cir. 2005 & 2002"
verification_status: verified
elements:
  - "Rule 9(b) requires pleading the who/what/when/where/how of the fraud with particularity, but circuits split on whether a relator must identify at least one REPRESENTATIVE actual false claim."
  - "Relaxed circuits (5th, 9th, 7th): pleading the fraudulent SCHEME in detail plus 'reliable indicia that lead to a strong inference that claims were actually submitted' satisfies Rule 9(b) — no representative claim required."
  - "Strict circuits (11th, historically 1st/4th/6th): a relator generally must plead the who/what/when/where/how of an ACTUAL submitted false claim, not just the scheme — Clausen and Corsello are the leading 11th Circuit statements of this requirement."
  - "The circuit split is about WHAT satisfies 9(b), not whether 9(b) applies to FCA claims (it does, universally)."
our_application_rule: >
  Plead to the VENUE's controlling standard, not a generic middle ground. In relaxed
  circuits (5th: Grubbs; 9th: Ebeid), plead the scheme with granular billing facts
  (specific HCPCS/CPT/J-codes, drug NDC, date ranges, service volumes, beneficiary counts,
  percentile extremity vs. the correct peer cohort) as the "reliable indicia" — this is
  sufficient without identifying one representative claim, which is exactly what our
  aggregate CMS Part B/D PUF evidence base supports. Do NOT default to pleading a
  representative claim we do not have; do NOT concede the strict standard applies when
  venue is 5th/9th/7th. In the 11th Circuit (and other strict-representative-claim
  circuits), Clausen/Corsello control: pleading only a fraudulent scheme plus statistical
  extremity, without at least the temporal/factual specificity of an actually-submitted
  claim, risks dismissal. For strict-circuit venues, lead with whichever counts have the
  closest thing to transaction-level specificity in our data (Open Payments dated
  kickback transactions; specific NDC/drug/date Part D prescriber records) and flag
  pure-aggregate-only counts as more vulnerable / discovery-dependent in that venue.
  Always confirm venue BEFORE drafting the 9(b) section of a count — the same evidence
  pleads adequately in the 5th/9th and risks a 9(b) dismissal in the 11th.
common_rebuttals:
  - "Defense: relator has not identified a single actual false claim (Corsello/Clausen strict standard) — in a relaxed circuit (5th/9th/7th), rebut directly with Grubbs/Ebeid: scheme + reliable indicia is sufficient, no representative claim required. In an 11th-Cir or strict venue, this attack has real teeth — counter by pointing to the most transaction-specific evidence available and by pleading additional reliable indicia (percentile extremity, date-bounded volume, drug/dose specificity) to approximate particularity."
  - "Defense: percentile rankings and statistical outliers are not 'reliable indicia,' they're just numbers — rebut that reliable indicia means SPECIFIC billing facts (codes, dates, volumes, dollar amounts) tied to a described mechanism, not bare abstract percentiles; a percentile alone is weaker indicia than the underlying code/volume/date facts and should never be pled as the sole indicium."
  - "Defense: the scheme allegations are generic industry practice, not this defendant's specific conduct — rebut with the defendant-specific facts (NPI, PECOS enrollment, specific years, specific code combinations unique to this billing pattern)."
---

# Rule 9(b) Reliable-Indicia Pleading — Circuit Split

## Holding

Rule 9(b) requires FCA complaints to plead fraud "with particularity" — but courts have
split sharply on what particularity means when a relator has aggregate statistical/billing
evidence rather than a specific claim form.

**Relaxed circuits — scheme + reliable indicia is enough:**

- *United States ex rel. Ebeid v. Lungwitz*, 616 F.3d 993 (9th Cir. 2010): a relator need
  not identify a specific claim by date/amount if the complaint pleads "particular details
  of a scheme to submit false claims paired with reliable indicia that lead to a strong
  inference that claims were actually submitted."
- *United States ex rel. Grubbs v. Kanneganti*, 565 F.3d 180 (5th Cir. 2009): same
  formulation — the "particular details of a scheme... paired with reliable indicia"
  standard, expressly rejecting a rigid representative-claim requirement.

**Strict circuits — representative claim generally required:**

- *United States ex rel. Clausen v. Laboratory Corp. of America*, 290 F.3d 1301 (11th
  Cir. 2002): the leading strict-pleading opinion — a complaint alleging only a general
  scheme, without the who/what/when/where/how of an actually submitted false claim, "says
  that a fraud must have occurred" rather than pleading one with particularity.
- *Corsello v. Lincare, Inc.*, 428 F.3d 1008 (11th Cir. 2005): reaffirms Clausen —
  conclusory allegations that fraudulent conduct "must have" resulted in false claims,
  without identifying an actual claim, do not satisfy 9(b).

## Our application

The pleading posture must be venue-specific, drafted at count-build time, not discovered
adversarially. Per the failure-mode taxonomy, Rule 9(b) attacks are "knowable at build" —
a statistical-aggregate-only count predictably draws this attack, and it is entirely
avoidable to be caught flat-footed by it.

**Drafting checklist per count:**
1. Determine controlling venue/circuit before drafting the 9(b) subsection.
2. Relaxed circuit (5th/9th/7th, per *Grubbs*/*Ebeid*/*Lusby*): plead the scheme with the
   most granular reliable indicia available — specific codes, dates, drug NDCs, volumes,
   dollar amounts, percentile extremity vs. correct peer cohort. State affirmatively that
   the circuit does not require a representative claim; do not concede otherwise.
3. Strict circuit (11th, per *Clausen*/*Corsello*; historically some 1st/4th/6th
   formulations): identify the count(s) with the closest available approximation of an
   actual submitted claim (dated, itemized transaction-level data — e.g., Open Payments
   kickback records, Part D prescriber-drug-year records) and lead with those; flag
   PUF-aggregate-only counts as comparatively higher-risk in that venue and consider
   whether claim-level discovery is a pleading precondition rather than a post-filing
   nicety.
4. Never plead an abstract percentile rank as the sole reliable indicium — it is
   corroboration for indicia built from the underlying code/volume/date facts, not a
   substitute for them.

## Common rebuttals / defense playbook

See frontmatter `common_rebuttals`. The central move in a relaxed circuit is citing
*Grubbs*/*Ebeid* directly against a Clausen-style "no representative claim" attack. In a
strict circuit, the same attack is a genuine risk and should be met by maximizing
transaction-level specificity rather than by argument alone.

## Verification note

All four citations independently verified via CourtListener `verify`: Ebeid, 616 F.3d 993
(9th Cir., decided 2010-08-09); Grubbs, 565 F.3d 180 (5th Cir., decided 2009-04-08) —
also present in the existing production doctrine corpus
(`data/ground_truth/doctrine_corpus.jsonl`, entry `rule9b-5th-grubbs`,
verification_status: landmark), consistent with this independent check; Corsello, 428
F.3d 1008 (11th Cir., decided 2005-10-20); Clausen, 290 F.3d 1301 (11th Cir., decided
2002-05-09). Holding language paraphrased, not block-quoted, except where noted.
