---
doctrine: conspiracy_pleading
citation: "Bell Atlantic Corp. v. Twombly, 550 U.S. 544 (2007)"
court_year: "SCOTUS, 2007"
verification_status: verified
elements:
  - "A Rule 8/12(b)(6) conspiracy claim (including FCA 3729(a)(1)(C) conspiracy) must plead 'enough factual matter (taken as true) to suggest that an agreement was made' — plausibility, not mere possibility."
  - "Parallel conduct among independent actors is, standing alone, EQUALLY consistent with independent self-interested action as with an unlawful agreement — it does not, by itself, plausibly suggest a conspiracy."
  - "A conspiracy claim must plead some further factual enhancement — a 'plus factor' — beyond parallel conduct: e.g., communications, shared infrastructure used for coordination (not mere co-location), synchronized timing that independent actors would not replicate absent an agreement, or direct evidence of an agreement."
our_application_rule: >
  Do NOT plead a conspiracy or practice-wide count on ownership-plus-parallel-billing
  alone. Same-address, same-timeframe billing pattern similarity among independent NPIs is
  the textbook Twombly-barred inference: it is equally consistent with independent
  providers responding to the same market/reimbursement incentives (a common and
  INNOCENT explanation) as with an agreement. Before pleading a multi-defendant
  conspiracy or office_suide/multi_office_network count, identify a genuine plus factor:
  a documented communication, a shared TIN/PECOS reassignment (structural, not
  coincidental), a common billing agent/clearinghouse identifiable in claim-level data,
  or synchronized adoption of an anomalous pattern in a way that independent
  decision-making would not produce (e.g., multiple physicians' E&M complexity mix
  jumping in lockstep in the same calendar month with no clinical or regulatory trigger).
  Absent a plus factor, record the co-location/parallel-pattern finding as a
  `follow_up_questions_for_investigator` lead — not a pled count — per the case-search
  co-located-fusion-test discipline already in counsel_build.md. This card exists because
  the taxonomy shows exactly one conspiracy count killed at round 2 precisely on this
  ground (agreement element negated once the "coordination" evidence was rebutted) —
  avoidable if pled correctly the first time.
common_rebuttals:
  - "Defense: the alleged co-conspirators are independent providers who happen to bill similarly because of common reimbursement incentives, not agreement (the core Twombly defense) — this attack is DISPOSITIVE unless the complaint pleads a genuine plus factor; do not attempt to argue around it with restated parallel-conduct facts."
  - "Defense: shared address/phone/fax is co-location infrastructure, not evidence of an agreement — correct; do not relabel co-location as a substantive conspiracy link (see counsel_build.md's co-defendant_link_basis discipline: shared_tin / shared_beneficiaries / shared_billing_agent / documented_referral only)."
  - "Defense: synchronized timing is coincidental market response (e.g., a coding-rule change affecting the whole specialty) — rebut by showing the synchronization is NOT specialty-wide (a within-specialty control group did NOT move) or is not explained by any contemporaneous regulatory/clinical trigger."
---

# Twombly Parallel-Conduct vs. Conspiracy Pleading

## Holding

*Bell Atlantic Corp. v. Twombly*, 550 U.S. 544 (2007), is the foundational plausibility
pleading case (also the source of the modern Rule 8/12(b)(6) plausibility standard
generally), decided in the antitrust Sherman Act §1 context but controlling for any
conspiracy-style claim, including FCA §3729(a)(1)(C) conspiracy counts. The Court held
that a bare allegation of **parallel conduct** among independent economic actors — without
more — does not plausibly suggest an agreement, because parallel conduct is "consistent
with conspiracy, but just as much in line with a wide swath of rational and competitive
business strategy unilaterally prompted by common perceptions of the market." A complaint
must plead a **"plausible grounds to infer an agreement"** — factual enhancement beyond
the parallelism itself.

## Our application

The relevant failure mode from the taxonomy: a conspiracy count pled on ownership +
parallel-billing alone is "identifiable the moment they're drafted" as Twombly-vulnerable
— this doctrine does not require adversarial discovery to spot; it is a build-time
pleading discipline. One conspiracy count in the corpus (a coordination theory) was killed
at adversarial round 2 specifically because the "agreement" element was negated once the
relator's own rebuttal evidence failed to corroborate coordination beyond parallel timing.

**Drafting rule**: before pleading any multi-defendant conspiracy or coordinated-scheme
count, require an affirmative plus factor beyond parallelism:
- A documented communication (email, meeting, shared consultant instructing the pattern).
- A structural link that could not arise independently: common TIN/PECOS reassignment,
  a shared billing agent/clearinghouse visible in claim-level remittance data, or a
  documented referral relationship.
- A synchronization pattern that a plausible independent-action story cannot explain —
  e.g., multiple physicians' billing mix jumping in lockstep in the same month with
  no contemporaneous coding-rule change, reimbursement update, or clinical trigger, AND
  a within-specialty or within-practice control group that did NOT move the same way
  (isolating the synchronization from a market-wide explanation).

Absent one of these, treat the co-location/parallel-pattern finding as a
`follow_up_questions_for_investigator` lead, not a pled count — consistent with the
existing co-located-fusion-test discipline in `counsel_build.md` (shared address +
temporal coincidence alone is a coincidence generator, not a conspiracy).

## Common rebuttals / defense playbook

See frontmatter. The core defense move is citing *Twombly* directly against a
parallel-conduct-only theory; it is largely unrebuttable without a genuine plus factor,
so the correct posture is to never plead into it in the first place.

## Verification note

Citation verified via CourtListener `verify "Twombly" --reporter "550 U.S. 544"` — exact
match, *Bell Atlantic Corp. v. Twombly*, SCOTUS, decided 2007-05-21. Holding language
paraphrased/partially quoted from the widely-reported opinion text ("consistent with
conspiracy, but just as much in line with...").
