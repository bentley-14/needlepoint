---
doctrine: scope_of_practice_physician_definition
citation: "42 U.S.C. § 1395x(r)(4) (Social Security Act § 1861(r)(4))"
court_year: "statutory; codified 1986 (OBRA), effective 1987"
verification_status: verified
elements:
  - "42 USC 1395x(r) defines 'physician' for Medicare purposes to include several practitioner categories at different subparagraphs — (r)(4) covers doctors of optometry."
  - "A doctor of optometry is a 'physician' under Medicare ONLY 'with respect to the provision of items or services... which he is legally authorized to perform as a doctor of optometry by the State in which he performs such functions or actions' — i.e., Medicare's physician status for optometrists is conditioned on and coextensive with STATE scope-of-practice authorization, not a freestanding federal grant of authority."
  - "This means an optometrist's Medicare billing authority for any given procedure/service is only as broad as the STATE licensing board's scope-of-practice rules for optometry in the state where the service was rendered — a service outside state-authorized scope is not 'physician services' under Medicare regardless of what CPT/HCPCS code is billed."
  - "Certain services are categorically excluded from Medicare coverage for optometrists regardless of state scope (e.g., routine eye exams/refraction for eyeglass prescription) — a separate coverage exclusion, distinct from (and layered on top of) the scope-of-practice gate."
our_application_rule: >
  When a provider's specialty is a limited-license category whose Medicare "physician"
  status is scope-conditioned (optometry is the paradigm case; other allied-health/limited-
  license categories may have analogous state-authorization gates), and the falsity theory
  turns on the provider having billed a service beyond what a state licensing board
  authorizes optometrists (or the relevant category) to perform, PULL THE ACTUAL STATE
  BOARD SCOPE-OF-PRACTICE STATUTE/REGULATION for the state(s) where claims were rendered —
  do not assume a national or "common sense" scope. This is a per-se falsity anchor
  independent of any LCD/NCD: billing under a physician-equivalent NPI for a service the
  state does not authorize this license category to perform is a scope-of-practice
  violation and a 1861(r)(4) falsity anchor, not merely a magnitude/medical-necessity
  dispute. This card exists directly because of a taxonomy-documented failure: an
  optometry-ring case (Eye Clinic Chestnut Care / Sohal) died specifically because "no 65778 LCD/NCD
  utilization limit exists" and the CA scope-of-practice statute text was never
  successfully retrieved in-run (blocked by Cloudflare/403 on law.justia.com and other
  fetch failures across four independent attempts) — the falsity theory needed the STATE
  STATUTE, not a coverage-utilization limit, and the tool chain never got there. Build a
  redundant retrieval path (official state legislature/board site, not just
  law.justia.com) for scope-of-practice text BEFORE relying on this anchor, and treat an
  unretrieved scope statute as a blocking gap, not a skippable one — the case-taxonomy
  evidence is that no other anchor substitutes for it once the LCD/NCD path is exhausted.
common_rebuttals:
  - "Defense: the procedure billed is within the state's optometry scope-of-practice statute — this is DISPOSITIVE if true; verify the actual current (and, critically, the AS-OF-CLAIM-DATE) state statute text before pleading a scope violation, since scope-of-practice statutes are amended periodically and expand optometric authority over time in many states."
  - "Defense: 1861(r)(4) is a Medicare enrollment/coverage technicality, not a basis for FCA falsity — rebut that a claim submitted under a physician-equivalent NPI for a service outside the practitioner's federally-recognized scope is a false certification of eligibility to bill, independent of medical necessity; this is a per-se-style anchor, not a discretionary coverage judgment."
  - "Defense: CMS paid the claims for years without objection (Escobar continued-payment inference) — assess whether the scope violation was VISIBLE to CMS from the claim form/PUF (in which case the continued-payment inference has force) or whether it required cross-referencing state licensing board records CMS does not routinely check (in which case duration is not dispositive of immateriality — see the Escobar card's visible/invisible fork)."
---

# SSA § 1861(r)(4) — Scope-of-Practice Hook for Optometrists (and Analogous Limited-License Categories)

## Statutory text and structure

42 U.S.C. § 1395x(r) (codified from Social Security Act § 1861(r)) defines "physician"
for Medicare purposes across several subparagraphs covering different practitioner types.
Subparagraph (r)(4) extends physician status to a doctor of optometry, but **only** "with
respect to the provision of items or services... which he is legally authorized to
perform as a doctor of optometry by the State in which he performs such functions or
actions." This is a **conditional, state-law-referential grant**: Medicare does not
independently define what an optometrist may bill for — it borrows the state licensing
board's scope-of-practice definition wholesale. An optometrist billing Medicare for a
service that state law does not authorize optometrists to perform is, by the statute's
own terms, not acting as a "physician" for that claim, regardless of what CPT/HCPCS code
is used to describe it.

This scope-conditioning structure is not unique to optometry — other limited-license
categories recognized as "physicians" or independently billing practitioners under
Medicare (e.g., certain non-physician practitioners with state-authorization-dependent
scopes) may have analogous statutory or regulatory scope gates; verify the specific
enabling provision for the category at issue before relying on this card's optometry-specific
text as a template.

Separately, certain services are categorically EXCLUDED from Medicare coverage for
optometrists regardless of state scope — most notably routine eye examinations and
refraction performed for the purpose of prescribing, fitting, or changing eyeglasses or
contact lenses. This is a coverage exclusion layered on top of (not a substitute for) the
scope-of-practice gate; a service can pass the scope-of-practice test yet still be
categorically non-covered, and vice versa is not possible (an out-of-scope service is
never covered no matter its exclusion status).

## Our application — why this card exists

The direct motivation is a documented taxonomy failure: an optometry-ring case
(Sohal/Eye Clinic Chestnut Care) reached a `confirmed_false_claim = FAILED` verdict predicate with
the explicit human-checkpoint finding "no 65778 LCD/NCD utilization limit exists" — the
investigation looked for a coverage/utilization anchor and found none, when the correct
anchor all along was the STATE SCOPE-OF-PRACTICE STATUTE under 1861(r)(4), which the
run never successfully retrieved (blocked by Cloudflare/403 on the primary fetch source
across four independent attempts within the run).

**Drafting/investigation checklist:**
1. Identify the state(s) where the claims were rendered (not the practitioner's mailing
   address — the state whose scope-of-practice law governs is where the service was
   performed).
2. Retrieve the ACTUAL current state optometry scope-of-practice statute/regulation from
   an authoritative primary source (state legislature website, state board of optometry
   regulations) — do not rely on a single secondary aggregator (e.g., law.justia.com)
   as the only fetch path; build in a redundant source given documented fetch failures.
3. Confirm the AS-OF-CLAIM-DATE version of the statute — scope-of-practice statutes are
   amended periodically (often expanding optometric authority), so the current text may
   not reflect what was authorized during the claim period.
4. If the billed procedure falls outside the state's authorized scope as of the claim
   date, this is a per-se falsity anchor independent of medical necessity or LCD/NCD
   coverage — plead it as a scope-of-practice violation under 1861(r)(4), not as a
   medical-necessity dispute.
5. Treat failure to retrieve the scope statute as a BLOCKING gap requiring escalation
   (additional fetch attempts via a different source, or a human-checkpoint if genuinely
   unretrievable) — not as a basis to fall back on a weaker LCD/NCD search that may not
   exist for the code at issue.

## Common rebuttals / defense playbook

See frontmatter. The central defense move is showing the service IS within state scope
(verify the as-of-claim-date text before conceding); the continued-payment analysis
should ask whether the scope violation was visible to CMS from the claim form alone
(unlikely — CMS claims processing does not routinely cross-reference state licensing
board scope definitions against billed codes) or required the kind of state-record
cross-reference this card describes (in which case duration of payment does not weaken
materiality, per the Escobar card's visible/invisible fork).

## Verification note

Statutory text and structure independently verified via WebSearch against multiple
sources (govinfo.gov USC Title 42, Cornell LII, AOA advocacy summary of the physician
definition) — direct quote "but only with respect to the provision of items or services
described in subsection (s) which he is legally authorized to perform as a doctor of
optometry by the State in which he performs them" is the Act's own operative language as
reported by these sources. This is a statutory citation, not a case citation, so
CourtListener case-verification does not apply; recommend confirming the current codified
text directly against govinfo.gov or uscode.house.gov before filing use, and independently
confirming the as-of-claim-date scope-of-practice statute for the specific state at issue
in any given case (see application notes above — this is case-specific and must be
re-verified per matter).
