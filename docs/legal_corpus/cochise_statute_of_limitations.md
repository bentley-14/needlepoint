---
doctrine: statute_of_limitations
citation: "31 U.S.C. § 3731(b); Cochise Consultancy, Inc. v. United States ex rel. Hunt, 587 U.S. 262, 139 S. Ct. 1507 (2019)"
court_year: "SCOTUS, 2019"
verification_status: verified
elements:
  - "31 USC 3731(b) sets TWO limitations windows, and a claim is timely if it satisfies EITHER: (1) 6 years from the date of the FCA violation, OR (2) 3 years from the date the RESPONSIBLE GOVERNMENT OFFICIAL knew or should have known the material facts — but never more than 10 years from the violation in any event."
  - "Cochise held the 3-year 'government knowledge' clock in 3731(b)(2) is available in a NON-INTERVENED (relator-only) qui tam action too — it is not limited to cases where the government has intervened — and that the relevant 'official' is a government official, not the relator, so a relator's own knowledge does not start the 3-year clock."
  - "Because the 3-year clock runs from GOVERNMENT knowledge (not the relator's), and because that knowledge is often not established until well after the violation, 3731(b)(2)'s 3-year/10-year combination frequently extends the effective limitations period well beyond the straightforward 6-year window in 3731(b)(1) — Cochise confirmed this applies even in relator-only suits filed after intervention is declined."
our_application_rule: >
  Do not default to pleading only the 6-year window from 3731(b)(1) and conceding the rest
  of a multi-year scheme as time-barred — affirmatively plead the LATER of the two windows
  under 3731(b), capped at 10 years from the violation. For each count, state which prong
  applies: if it has been under 6 years since the claim was submitted, the 6-year window
  alone covers it and no government-knowledge analysis is needed. If more than 6 years
  (but less than 10) have elapsed, plead 3731(b)(2)'s 3-year-from-government-knowledge
  prong affirmatively — this requires identifying (or, pre-filing, flagging as a
  discovery-dependent fact) WHEN a responsible government official first knew or should
  have known the material facts, which is frequently LATER than when the relator or the
  public first had access to the same information. Do NOT assume government knowledge
  attaches merely because CMS's own PUF data reflects the pattern — publication of
  aggregate claims data is not the same as an identified government official's actual or
  constructive knowledge of the SPECIFIC fraud allegation. Per the failure-mode taxonomy,
  SOL attacks are the most mechanically pre-computable doctrine in the corpus (13 attacks,
  0 kills, but a reliable 20-35% discount every time it lands) — compute the date-math
  explicitly (claim date, filing date, elapsed time, applicable window) in
  `counsel_build` rather than leaving it to be re-derived adversarially every round.
common_rebuttals:
  - "Defense: claims older than 6 years are time-barred — rebut by pleading 3731(b)(2)'s 3-year-from-government-knowledge prong (up to the 10-year outer cap) where applicable; do not concede the 6-year window is the only available measure."
  - "Defense: the relator/public had access to this information more than 3 years before filing, so the 3-year clock ran — rebut with Cochise: the clock runs from a GOVERNMENT OFFICIAL's knowledge, not the relator's or the public's; absent evidence a specific responsible official actually knew, the 3-year clock has not necessarily started."
  - "Defense: the 10-year outer cap bars claims older than 10 years regardless of government knowledge — this is correct and not rebuttable; confirm no pled claim falls outside the 10-year outer boundary from the date of violation."
---

# Cochise Consultancy — FCA Statute of Limitations (31 U.S.C. § 3731(b))

## Holding

31 U.S.C. § 3731(b) provides FCA claims are timely under EITHER of two alternative
windows: **(1)** 6 years after the date the violation was committed, or **(2)** 3 years
after the date the responsible U.S. government official charged with responsibility to
act knew or reasonably should have known the material facts, but in no event more than
**10 years** after the violation.

*Cochise Consultancy, Inc. v. United States ex rel. Hunt*, 587 U.S. 262, 139 S. Ct. 1507
(2019), resolved a circuit split by holding that the 3-year/government-knowledge
provision in 3731(b)(2) is available in a **non-intervened** qui tam action (i.e., where
the relator proceeds alone after the government declines to intervene) — it is not
limited to government-initiated or intervened suits. The Court also confirmed the
relevant "official" whose knowledge starts the 3-year clock is a **government** official,
not the relator — meaning a relator's own early knowledge of the fraud does not itself
trigger the shorter clock.

## Our application

The failure-mode taxonomy identifies SOL as the most purely mechanical doctrine in the
corpus — 13 recorded attacks, zero kills, but a reliable 20-35% discount every round,
because the date math is deterministic and should be computed once at build time rather
than re-litigated adversarially each round.

**Drafting checklist per count:**
1. Compute elapsed time from each claim's submission date to the anticipated filing date.
2. If under 6 years, the 3731(b)(1) window alone covers it — no further analysis needed.
3. If between 6 and 10 years, affirmatively plead the 3731(b)(2) prong: identify (or
   flag as a discovery-dependent fact) when a responsible government official knew or
   should have known — this is frequently later than when the relator's own analysis or
   public data made the pattern visible, per Cochise's holding that the relator's
   knowledge does not start this clock.
4. Do not conflate "CMS published PUF data reflecting this pattern" with "a responsible
   government official knew the specific fraud allegation" — publication of aggregate
   data is not equivalent to identified official knowledge of a fraud theory against a
   specific defendant.
5. Confirm no claim in the count falls outside the absolute 10-year outer cap from the
   date of violation — this bar is not rebuttable under any theory.

## Common rebuttals / defense playbook

See frontmatter. The central relator move against a 6-year-only SOL attack is
affirmatively invoking 3731(b)(2)'s government-knowledge prong per Cochise; the defense's
counter is arguing the relator's own (not the government's) early knowledge should count,
which Cochise forecloses.

## Verification note

Citation verified via CourtListener `verify "Cochise Consultancy Hunt" --reporter "139 S.
Ct. 1507"` — exact match, *Cochise Consultancy, Inc. v. United States ex rel. Hunt*,
SCOTUS, decided 2019-05-13 (parallel cite 587 U.S. 262). Also present, verification_status
`landmark`, in the existing production doctrine corpus
(`data/ground_truth/doctrine_corpus.jsonl`, entry `sol-cochise`), consistent with this
independent verification. Holding language paraphrased, not block-quoted.
