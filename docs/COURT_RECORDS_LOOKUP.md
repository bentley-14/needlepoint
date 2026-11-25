# Court-records lookup — agent procedure

How to verify whether a related docket is a sealed qui tam before treating it as a first-to-file
(31 USC 3730(b)(5)) bar.

## THE CARDINAL RULE
> **Inaccessibility in your free tools is NOT evidence of a seal.** A sealed federal qui tam has
> **no public docket entries and no public answer**. If you can see a nature-of-suit code, a
> scheduling order, or a defendant's **Answer**, the case is **NOT sealed**. Only treat a case as a
> possible sealed qui tam if PACER's own index **affirmatively** marks it sealed — never infer a seal
> from "CourtListener has no documents" or "PacerMonitor returned a login banner."

## LOOKUP ORDER (federal docket)
1. CourtListener (authenticated): `python3 scripts/docket_lookup.py --cl-docket 9:99-cv-00001 --court txnd`
   Confirms existence + judge + date. Empty `nature_of_suit`/`cause` = "shell only", NOT "sealed".
2. Find the PacerMonitor public URL with your **WebSearch** tool: `PacerMonitor <case name>`
   -> https://www.pacermonitor.com/public/case/<id>/<slug>
3. Read it with your **WebFetch** tool (preferred — PacerMonitor 429-rate-limits direct script fetches,
   but WebFetch works). Targeted prompt: extract nature-of-suit/cause, parties, counsel, judge, docket
   entries; a login/pricing banner does NOT mean the summary is unavailable.
   `scripts/docket_lookup.py --url <URL>` is a best-effort parser of the same page (emits seal_assessment).
4. Document TEXT (e.g., does an employment complaint also allege billing fraud) needs a paid PACER pull
   (CourtListener RECAP Fetch API + PACER creds) — flag for human approval; do not fabricate.

## First-to-file classification
- Title VII / FLSA / employment, a public answer, or any open docket -> NOT a qui tam -> 3730(b)(5) N/A.
- Affirmatively sealed (per PACER index) + same defendant + same fraud -> real risk; human PACER pull before filing.
