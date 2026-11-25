# DOJ National Health Care Fraud Takedown

Domain primer for `pipeline/scrapers/doj_takedown.py` (discovery link 1b).
Schema reference lives in `docs/DATA_SCHEMAS.md`; this is for understanding.

## Plain-English description

Once a year (historically June/July) the DOJ Criminal Division's Health Care
Fraud Unit announces a coordinated **National Health Care Fraud Takedown** — a
synchronized sweep of criminal charges and civil actions across dozens of U.S.
Attorney's Offices and state Attorneys General. DOJ publishes two companion web
pages per takedown:

- **Court Documents** — a table of every case (jurisdiction, case name,
  civil/criminal, document type) linking the charging instrument PDF.
- **Case Summaries** — one prose paragraph per defendant: name, age, location,
  charge, scheme, dollar amounts, and the program defrauded.

This scraper turns those into gold_set discovery leads. The **Case Summaries**
paragraph is the curator's `raw_text` (same shape as a press-release body); the
**Court Documents** PDF link is the authoritative citation (`primary_source_url`).

## Why this source exists alongside the press-release API

The press-release path (`doj_press_releases.py`) sweeps the DOJ JSON API by
title. It covers a takedown only partially:

- DOJ announces the takedown as **district roundups** ("…455 Defendants
  Charged…"), not one press release per defendant — so per-defendant API
  recall is poor (defendant-name title searches return 0).
- The **state-AG cases never appear in the DOJ API at all** — they're state
  actions. The takedown index is the *only* machine-readable source for them.

## Acronym decode

- **HCF** — Health Care Fraud (Unit).
- **USAO** — U.S. Attorney's Office; the federal table is keyed by district
  (e.g. `SDFL` = Southern District of Florida, `EDMI` = Eastern District of
  Michigan, `CDCA` = Central District of California).
- **Information / Indictment / Complaint** — charging instruments. An
  *information* is filed by the prosecutor (often signals a plea); an
  *indictment* comes from a grand jury; a *complaint* (civil here) opens an
  FCA-type case. **Settlement Agreement** = civil resolution.

## Unit of observation

One **case** (row in the court-docs table) ≈ one defendant or co-defendant
group. `scope` ∈ {`federal`, `state`}. A handful of cases name multiple
co-defendants ("et al"); the curator splits those into per-NPI proposals
downstream. (2026 snapshot: ~131 federal + ~220 state and still growing.)

## Publication cadence + drift

- **Annual**, dropped all at once on takedown day, then **edited for weeks** —
  DOJ keeps adding cases ("additional materials posted as available"). The 2026
  state table grew from ~130 to ~224 within a day of the announcement.
- The `national-enforcement-actions` **index lags**: it doesn't link the current
  year's takedown until well after the pages are live. So the scraper **probes
  the current calendar year directly** and only falls back to the index's newest
  linked year if this year isn't posted yet.
- No suppression rules (this is court-record text, not aggregated billing data).

## Fraud signals (what the summaries surface)

The summary prose names the **scheme** and the **program** defrauded, which is
exactly what the curator needs to classify and what the gate cross-checks
against billing surface:

- Program: **Medicare** (Part B/D/DMEPOS — scoreable by the ranker) vs.
  **state Medicaid / TRICARE / DOL-FECA / private** (often *not* in the Medicare
  panels → the gate routes these `NO_SURFACE` / unscoreable).
- Scheme vocabulary: medically unnecessary services, kickbacks/bribes, phantom
  billing, telemedicine/genetic-testing/DME schemes, controlled-substance
  over-prescribing, amniotic/wound-allograft fraud, addiction-treatment
  ("sober home") fraud.

## Joins to other sources

- → **gold_set**: leads flow through the curator → deterministic gate → merge,
  same as the DOJ-direct path. Defendant name resolves to **NPI** via NPPES in
  the gate; the billing-surface check uses Part B/D/DMEPOS (over R2 httpfs).
- Overlaps the **press-release** path for federal cases (a defendant may appear
  in both). Leads dedup by URL-hash `lead_id`; same-defendant-different-URL
  duplicates collapse to one label at the gate's already-labeled dedup.

## Known limitations

- **State / non-Medicare cases are mostly unscoreable** by the Medicare-only
  ranker (no Part B surface) — valid case-library entries, weak training labels.
  They're ingested with a `scope` flag so they can be filtered either way.
- **Akamai bot-wall**: the HTML pages (unlike the JSON API) require a browser
  UA, and a datacenter/CI IP may still be blocked. The scraper degrades to an
  empty run on any fetch failure rather than breaking the shared weekly job.
- **Summary matching ≈ 90%**: a defendant whose name can't be located in the
  summaries prose, or whose slice is a co-defendant fragment below the length
  guard, is skipped (no `raw_text`) — the PDF citation remains for later.
- **No conduct dates**: summaries rarely state the fraud period, so most cases
  arrive with `active_years` = outcome-fallback and route to human review.
