---
doctrine: damages_methodology
citation: "United States v. Rogan, 517 F.3d 449 (7th Cir. 2008); United States v. Bornstein, 423 U.S. 303 (1976)"
court_year: "7th Cir. 2008 (Rogan); SCOTUS, 1976 (Bornstein)"
verification_status: verified
elements:
  - "Rogan (7th Cir.): where the entire claim is tainted by fraud (e.g., a claim would not have been paid AT ALL absent the fraudulent certification/eligibility condition), the government's full single damages ceiling is the ENTIRE amount paid on the claim — not merely a rate differential or increment. Rogan affirmed a damages award equal to the FULL Medicare/Medicaid reimbursement received on claims tied to a kickback scheme, rejecting the argument that damages should be limited to some 'benefit of the bargain' offset for services actually rendered."
  - "Bornstein (SCOTUS): where the falsity is an INCREMENT — the government received a real, valuable good/service but was defrauded on a component of the transaction (there, defective vacuum tubes substituted within otherwise-conforming radio kits) — single damages are measured by the increment/difference between what was promised and what was delivered, not the full contract price. Bornstein also held FCA per-claim (per-count) penalties attach to each false statement submitted, counted separately even within a single larger transaction."
  - "The Rogan/Bornstein distinction tracks the same increment-vs-full-claim axis as counsel_build.md's existing damages-altitude framework: false certification/ineligibility/kickback-tainted/services-not-rendered claims are FULL-CLAIM (Rogan ceiling); upcoding/unbundling where a real, lesser service was actually rendered are INCREMENT-based (Bornstein floor)."
our_application_rule: >
  Use Rogan and Bornstein as the doctrinal anchors for the floor/ceiling and increment/
  full-claim damages framework already required by counsel_build.md Step 6a. When the
  falsity taints ELIGIBILITY TO BILL AT ALL — kickback-tainted claims (AKS per-se per the
  Greber/1320a-7b(g) card), false certification of a condition of payment, services not
  rendered, or a scope-of-practice violation (1861(r)(4) card) — the FULL claim amount is
  the damages floor under Rogan; do not shrink it to a rate differential, and do not treat
  "the government got SOME value" as a basis for discounting below the full paid amount —
  Rogan rejected exactly that argument. When the falsity is a QUALITY/COMPONENT increment
  — a real service was rendered but coded at a higher level than warranted, or a cheaper
  substitute was billed as the more expensive item — Bornstein's increment measure
  applies: single damages are the DIFFERENCE between what was paid and what should have
  been paid for the service actually rendered, not the full billed amount. Also apply
  Bornstein's per-claim-penalty counting rule literally: each individual false claim
  submission carries its own statutory penalty ($13,946-$27,894 per claim at 2026 rates),
  counted per claim, not bundled into a single penalty for an entire multi-claim scheme —
  this affects the total-exposure calculation independent of the single-damages
  methodology chosen.
common_rebuttals:
  - "Defense: damages should be limited to the excess/differential because the government received SOME value (real services were rendered) — this argument is Rogan-foreclosed for claims where the falsity goes to ELIGIBILITY (kickback taint, false certification, services-not-rendered, scope violation) — cite Rogan directly; it is correct ONLY for genuine increment/upcoding theories, where it should be conceded as the (Bornstein) floor rather than resisted."
  - "Defense: the damages calculation double-counts by both claiming the full claim amount AND separately claiming a per-code overcharge — audit the methodology to ensure a single, internally consistent theory (full-claim under Rogan XOR increment under Bornstein) per count, not a blended calculation that claims both measures for the same claims."
  - "Defense: per-claim penalties should be aggregated/capped as a single scheme-wide penalty — rebut with Bornstein's holding that FCA penalties attach per individual false claim/statement, not per scheme; the correct exposure multiplies the per-claim penalty by the actual claim count."
---

# Rogan Damages Ceiling + Bornstein Increment Measure

## Holding

**United States v. Rogan**, 517 F.3d 449 (7th Cir. 2008), affirmed a damages judgment
equal to the FULL amount Medicare/Medicaid paid on claims tainted by a kickback scheme
and false certifications, rejecting the defendant's argument that damages should be
offset by the value of services patients actually received. Where the claim would not
have been payable AT ALL but for the fraudulent condition (there, illegal
remuneration/certification issues going to program eligibility), the government's
recoverable single damages are the full paid amount — a "benefit conferred" or
"quantum meruit" style offset does not apply.

**United States v. Bornstein**, 423 U.S. 303 (1976), is an older but still-controlling
Supreme Court FCA damages decision. The government contracted for radio kits
incorporating specified vacuum tubes; the subcontractor substituted cheaper, defective
tubes. The Court held (1) single damages are measured as the difference between what the
government paid and the value of what it actually received (there, the value differential
attributable to the substituted tubes) — an INCREMENT measure, not the full contract
price, because the government did receive a real, substantially conforming product; and
(2) separately, FCA penalties attach to each individual false statement/claim submitted,
counted claim-by-claim, not aggregated into a single scheme-wide penalty.

## Our application

This card supplies the doctrinal citations for the increment-vs-full-claim distinction
`counsel_build.md` Step 6a already requires counsel to apply when setting
`damages_floor_usd`/`damages_ceiling_usd`/`damages_basis`:

- **Full-claim (Rogan) applies** when the falsity taints ELIGIBILITY to bill the claim at
  all: kickback-tainted claims (AKS per-se falsity under 1320a-7b(g) — see the AKS/Greber
  card), false certification of a condition of payment that was not met, services not
  rendered, or a scope-of-practice violation making the billing practitioner ineligible
  to bill as a "physician" for that service (see the SSA 1861(r)(4) card). In these
  cases, plead the FULL billed/paid amount as the floor — Rogan forecloses a
  "government-got-some-value" discount argument, and conceding one is a Step-6a drafting
  error (counsel_build.md is explicit that this framing is "the defense's argument,"
  never the relator's floor).
- **Increment (Bornstein) applies** when a real, valuable service was rendered but the
  claim overstates its level, complexity, quantity, or component quality — classic
  upcoding/unbundling. Here, the floor is the differential between the amount paid and
  the amount that should have been paid for the service actually and legitimately
  rendered, consistent with counsel_build.md's existing floor/ceiling altitude framework.
- **Per-claim penalty counting.** Independent of which single-damages measure applies,
  Bornstein's per-claim penalty rule means statutory penalties must be calculated per
  individual false claim submission, not as one lump penalty for the scheme — verify the
  damages methodology reflects claim-count × per-claim-penalty, not a single flat
  penalty figure, when stating aggregate exposure.

## Common rebuttals / defense playbook

See frontmatter. The dominant defense move against a full-claim theory is arguing partial
value was conferred (Rogan-foreclosed for eligibility-taint theories); the dominant
counsel-side error to avoid is inadvertently blending increment and full-claim measures
for the same claims, or under-counting per-claim penalty exposure.

## Verification note

Both citations independently verified via CourtListener `verify`: Rogan, 517 F.3d 449
(7th Cir., decided 2008-02-20, exact-cite match — note a later, differently-cited 2011
7th Cir. opinion in the same underlying matter, 639 F.3d 1106, also exists; this card
cites the 2008 damages-methodology opinion specifically); Bornstein, 423 U.S. 303
(SCOTUS, decided 1976-01-19, exact-cite match). Holding language paraphrased from
widely-reported case summaries, not block-quoted from the opinions themselves.
