---
doctrine: first_to_file
citation: "31 U.S.C. § 3730(b)(5); Kellogg Brown & Root Servs., Inc. v. United States ex rel. Carter, 575 U.S. 650 (2015)"
court_year: "SCOTUS, 2015"
verification_status: verified
elements:
  - "3730(b)(5): 'When a person brings an action under this subsection, no person other than the Government may intervene or bring a related action based on the facts underlying the pending action.'"
  - "The bar applies only while the first-filed action is PENDING — Carter holds that once the first action is dismissed (for any reason), it no longer counts as 'pending' and the first-to-file bar no longer bars a later-filed related action."
  - "Related action = based on the SAME material elements of fraud, even if not identical facts or defendants (the 'same material elements' / 'essential facts' test, from Branch Consultants and its progeny in the circuits, not displaced by Carter)."
  - "Carter separately held the Wartime Suspension of Limitations Act (WSLA) tolls only criminal statutes of limitations, not civil FCA claims — a related but distinct holding from the same case."
our_application_rule: >
  Run a PACER/CourtListener docket check for a prior qui tam against the SAME defendant on
  SUBSTANTIALLY THE SAME scheme at INTAKE (iteration 1), before counsel_build invests —
  this is the single highest-value pre-check in the failure-mode taxonomy: two full case
  runs were burned (one 3 iterations + 2 adversarial rounds; one 3 iterations) discovering
  a first-to-file bar that a docket search at intake would have surfaced immediately. When
  a prior action is found, the FIRST and ONLY question is whether it is still PENDING
  (Carter): dismissed-for-any-reason means the bar has lifted and a later relator may
  proceed on the same facts. A SEALED docket is the hard case — do not presume a sealed
  matter is pending-and-barring; also do not presume it is safely resolved. If PACER's
  own docket does not affirmatively show dismissal/termination, treat pendency as
  UNRESOLVED (a Guardrail-4 human-checkpoint candidate), not as an automatic bar — "I
  can't see inside the seal" is not evidence the matter is still pending, and "inaccessible"
  is not "sealed" (see the Cardinal Rule: inaccessibility ≠ seal). When a genuine pending
  related action is confirmed, apply the same-material-elements test narrowly: different
  defendants at the same location, or overlapping-but-distinct fraud mechanisms, may not
  be "based on the facts underlying" the pending action — do not concede the whole case is
  barred without confirming the elements actually overlap.
common_rebuttals:
  - "Defense: a prior qui tam was filed against this defendant/scheme and remains sealed/pending — do not concede the bar without confirming PENDENCY on the docket; a dismissed prior action (Carter) does not bar."
  - "Defense: the prior action covers 'the same material elements' — test this narrowly; a materially different fraud mechanism, time period, or defendant set may not be 'based on the facts underlying' the earlier action even if there is topical overlap (e.g., same specialty, same general billing category)."
  - "Defense invokes WSLA tolling for civil claims — Carter forecloses this; WSLA tolls only criminal statutes of limitations, not civil FCA claims, so it cannot be used to argue our claims are somehow still 'pending' or revived by wartime tolling."
---

# First-to-File Bar (31 U.S.C. § 3730(b)(5)) + KBR v. Carter

## Holding

31 U.S.C. § 3730(b)(5) provides that once a qui tam action is brought, "no person other
than the Government may intervene or bring a related action based on the facts underlying
the pending action." *Kellogg Brown & Root Servs., Inc. v. United States ex rel. Carter*,
575 U.S. 650 (2015), resolved two distinct questions arising under this provision (and
the Wartime Suspension of Limitations Act) in a single opinion:

1. **First-to-file bars only while the earlier action is PENDING.** The Court held the
   word "pending" means exactly that — a first-filed action that has been dismissed no
   longer bars a later, related qui tam. Once dismissal occurs (for any reason, including
   on the merits, for jurisdictional defects, or voluntary dismissal), a subsequent
   relator may refile on the same underlying facts without running into 3730(b)(5).
2. **WSLA tolls criminal, not civil, statutes of limitations.** The Wartime Suspension of
   Limitations Act does not extend the FCA's civil limitations period — a separate holding
   from the same case, relevant to statute-of-limitations analysis but distinct from the
   first-to-file question.

The "related action based on the facts underlying" language is generally construed via
the circuit-level "same material elements" test (the *Branch Consultants* line and its
progeny) — a later action is barred if it alleges essentially the same fraudulent scheme,
even without identical parties or claim-by-claim overlap, but is NOT barred merely because
it involves the same general subject-matter or industry.

## Our application

This is the single largest documented waste in the failure-mode taxonomy: **two full case
runs consumed 3+ iterations and up to 2 full adversarial rounds each** discovering a
first-to-file bar that was resolvable by a PACER/CourtListener docket query available at
intake. The fix is structural, not doctrinal — run the check FIRST, before investing in
build/adversarial cycles:

1. At iteration 1, before `counsel_build` runs, query CourtListener RECAP/dockets for any
   prior qui tam naming the same defendant (and, where possible, co-defendants/affiliated
   entities) filed under seal or otherwise.
2. If a prior action is found, determine PENDENCY first: is there a docket entry showing
   dismissal, judgment, or termination? If yes, Carter lifts the bar — proceed. If the
   docket shows no resolution (including "still sealed, no visible terminal entry"), do
   NOT presume dismissal — but also do NOT presume the matter is still pending merely
   because it is inaccessible. Absence of visibility is not evidence either way; this is
   exactly the scenario the Guardrail-4 human-checkpoint exists for (see
   `checkpoint.py` / `docs/CANONICAL_LOOP_GUARDRAILS.md`).
3. If pendency is confirmed, apply the same-material-elements test narrowly before
   conceding the whole case is barred — a topically adjacent but mechanism-distinct
   scheme, or a materially different defendant, may survive.

## Common rebuttals / defense playbook

See frontmatter. The core relator move against a first-to-file attack is establishing
non-pendency (Carter) or non-identity-of-material-elements; the core defense move is
establishing pendency and scheme-identity. Sealed dockets require the human-checkpoint
path rather than a unilateral presumption in either direction.

## Verification note

Citation verified via CourtListener `verify "Kellogg Brown Root Carter" --reporter "575
U.S. 650"` — exact match, *Kellogg Brown & Root Services, Inc. v. United States ex rel.
Carter*, SCOTUS, decided 2015-05-26. Both holdings (pendency requirement; WSLA
criminal-only tolling) are the well-reported dual holdings of this case; also present
verification_status `landmark` in the existing production corpus
(`data/ground_truth/doctrine_corpus.jsonl`, entry `ftf-kbr-carter`), consistent with this
independent verification. "Same material elements" phrasing is the general circuit
formulation (not itself a Carter holding) — flagged here as doctrine-adjacent context,
not a direct quote from Carter.
