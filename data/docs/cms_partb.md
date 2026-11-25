# CMS Part B — by Provider (aggregated)

**Scraper:** [`pipeline/scrapers/cms_partb.py`](../../pipeline/scrapers/cms_partb.py)
**Schema reference (columns + dtypes):** [docs/DATA_SCHEMAS.md](../../docs/DATA_SCHEMAS.md)
**Files:** `data/panels/cms_partb_YYYY.parquet` + `cms_partb_YYYY.manifest.json`

## 1. What this dataset is, in plain English

**Part B** is the slice of Medicare that covers physician services — the
visits, procedures, and surgeries a doctor performs and bills for
directly. (Part A is hospital stays; Part D is outpatient prescription
drugs filled at a pharmacy; DMEPOS is durable equipment a patient takes
home; see the separate docs for each.) This PUF aggregates **one
calendar year of Part B billing per rendering provider**, summing every
HCPCS code that provider personally billed against beneficiaries.

Unlike DMEPOS — where the physician *referred* an order to a supplier
who actually billed — Part B is the physician's *own* clinical work.
The NPI on each row is the person who saw the patient and submitted
the claim:

```
   rendering physician  ──bills directly──▶   Medicare
        (Rndrng_NPI)
```

This is the project's spine. Most ML ranker features touch this file
(payment, services-per-bene, payment-per-service, HCPCS diversity);
the fraud schemes captured here cover **upcoding**, **phantom billing**,
**J-code drug substitution**, and the post-COVID telehealth boom.

### Acronyms used in column names

| Acronym | Meaning |
|---|---|
| `Rndrng` | Rendering (the physician who personally provided the service) |
| `Prvdr` | Provider |
| `Mdcr` | Medicare |
| `Bene` | Beneficiary |
| `HCPCS` | Healthcare Common Procedure Coding System — the code set for every billable line item (5-character codes; CPT is a subset) |
| `Tot_HCPCS_Cds` | Count of distinct HCPCS codes the provider billed |
| `Tot_Srvcs` | Total services rendered (a service is one billed line item; can be > beneficiaries because one patient gets multiple services) |
| `Tot_Benes` | Total unique beneficiaries seen |
| `Sbmtd_Chrg` | Submitted charge — what the provider's office billed |
| `Alowd` | Allowed — what the Medicare fee schedule says the service is worth |
| `Pymt` | Payment — what Medicare actually paid after deductible/coinsurance |
| `Stdzd` | Standardized — payment with geographic wage-index adjustment removed |
| `Drug_*` | Part B drugs — drugs administered in-office (J-code drugs, infusions, chemo). **Not** Part D prescriptions filled at retail pharmacies |
| `Med_*` | Medical services — everything that isn't a Part B drug (E&M visits, procedures, imaging, surgery) |
| `Sprsn_Ind` | Suppression indicator — `*` (cell `n < 11`) or `#` (negative/unreliable). Same convention as DMEPOS |
| `Ent_Cd` | Entity code — `I` (individual) or `O` (organization, e.g. group practice billing under a group NPI) |
| `Mdcr_Prtcptg_Ind` | Medicare-participating indicator — `Y` if provider accepts assignment on every claim |
| `Crdntls` | Credentials (MD, DO, NP, PA, …) |
| `CC` | Chronic Condition — the `Bene_CC_*` block tags beneficiary prevalence rates |
| `RUCA` | Rural-Urban Commuting Area — geographic classification code |
| `Prvdr_Type` | Specialty as self-declared in the PUF (free-text-ish; see §7) |

## 2. Unit of observation

**One row = one rendering NPI × one calendar year.** Aggregated across
every HCPCS code the provider billed and every beneficiary they saw.

This is *not* a claim-level dataset. There is no per-HCPCS detail, no
per-beneficiary row. You can't see "which CPT codes Dr. X billed most"
from this PUF — only the totals (broken down only by the Drug-vs-Med
split). For per-HCPCS detail, the sibling source `cms_partb_svc` (by
Provider & Service, multi-row per NPI) is required.

The 81 raw CMS columns break into:

- 17 cols of provider identity (`Rndrng_*`) — NPI, name, address,
  specialty, Medicare-participation flag
- 7 cols of total billing aggregates (`Tot_*`) — HCPCS count, bene
  count, service count, submitted/allowed/paid/standardized payment
- 8 cols × 2 subcategories (`Drug_*`, `Med_*`) = 16 cols of
  Drug-vs-Med splits, with a per-category `Sprsn_Ind`
- 15 cols of beneficiary demographics (age buckets, sex, race,
  dual-eligible)
- 25 cols of beneficiary chronic-condition prevalence (`Bene_CC_*`) —
  *only present for 2017+ (see §6)*
- 1 col average risk score (`Bene_Avg_Risk_Scre`)

After our scrape pass, parquets carry 7 additional canonical columns: 3
base stamps (`source`, `fetched_at_utc`, `schema_version`) + 4 NPI-keyed
canonicals (`npi`, `provider_name`, `state`, `data_year`) — see
[`pipeline.base.ProviderRecordSchema`](../../pipeline/base.py).

Total declared schema: **88 columns**.

## 3. How CMS publishes it (cadence + suppression)

- **Annual release.** Each PUF covers one calendar year.
- **Lag.** ~18 months from end-of-year to release. Data year 2023 was
  published in April 2025 (initial V10) and rolling years 2013–2022
  were republished in November 2025 under the RY25 V20 release. The
  URL catalog in [`pipeline/scrapers/download.py`](../../pipeline/scrapers/download.py)
  pins one URL per year; expect URLs to rotate per RY release.
- **CMS reissues older years under newer release versions** when they
  refresh the standardized-payment methodology or add columns. Re-fetch
  by pointing `_download` at the current data.cms.gov URL.
- **Suppression rules — applied upstream by CMS, not by our code.**
  Our scraper does no row-level filtering; what's in the parquet is what
  CMS published. Two suppression layers:
  - **Row-level floor.** `Tot_Benes` has a hard floor of 11 — rows
    where a provider's annual unique-beneficiary count would have been
    `< 11` are dropped by CMS before publication. Empirically on 2023:
    `Tot_Benes.min() == 11`, no nulls. This is **the** reason small
    operators are invisible in this dataset.
  - **Cell-level numeric suppression.** Inside a published row, any
    cell whose underlying count would be `< 11` gets nulled. The row
    survives. Empirically on 2023: **146,440 of 1,259,343 rows (11.6%)
    have null `Drug_Tot_Benes` / `Med_Tot_Benes`** because one of the
    two subcategories fell under the floor.
  - **Category-level flags `Drug_Sprsn_Ind` / `Med_Sprsn_Ind`.** Two
    suppression codes; both mean "no published data" and the
    corresponding count and dollar columns are nulled:
    - `*` — cell `n < 11`, redacted for privacy
    - `#` — value was negative or unreliable, redacted for data quality
  - **What this means for fraud detection.** The dataset is tuned to
    **provider-volume schemes** — anyone billing under 11 beneficiaries
    a year is invisible, but no real-world fraud scheme of interest
    operates at that scale. The *presence* of suppression on Drug vs.
    Med can disambiguate "pure pill-pusher" from "general internist
    with some J-code drugs".
  - **Do not** assume the existing `fraudlib/ingest/cms/partd_stats.py`
    or `dmepos_stats.py` treatment of `Sprsn_Ind == "Y"` is correct —
    see the DMEPOS doc §7 for the bug.

## 4. Fraud signals — what to look for

Part B captures the **physician's own clinical activity** (visits,
procedures, in-office surgeries, administered drugs — billed by them,
not on referral). The fraud schemes that surface here differ from
DMEPOS, where the physician is a signature-throughput bottleneck. Here
the physician's own time is the constraint, so the schemes typically
involve inflating the per-encounter dollar value rather than the volume.

### 4a. Upcoding (most common Part B scheme)

Billing 99215 ("complex visit, ~40 min") when 99213 ("routine,
~15 min") was actually rendered. Each step up the E&M ladder roughly
doubles the per-visit payment. Sustained over a year, a 15-percentage-
point shift in code mix can move a million-dollar practice up by $200k+
in revenue.

**Column signatures:**
- High `Med_Mdcr_Pymt_Amt / Med_Tot_Srvcs` (payment-per-service) for a
  specialty whose typical complexity is lower (Family Practice,
  Internal Medicine, Geriatrics)
- High `Tot_Mdcr_Alowd_Amt / Tot_Mdcr_Stdzd_Amt` ratio when normalized
  against the specialty median
- Cross-source check: in `cms_partb_svc_*` the same NPI's code mix is
  skewed to high-level E&M codes (99214/99215, 99204/99205)

Canonical case: **Farid Fata** (2013, oncology — diagnosed and "treated"
hundreds of patients who didn't have cancer; billed extreme volumes of
99215 E&M visits in addition to the chemo).

### 4b. Phantom billing

Services billed for patients never seen. The row stays plausible because
the schemer pads with low-cost line items, but the per-bene service
count explodes.

**Column signatures:**
- Extreme `Tot_Srvcs / Tot_Benes` ratio for the specialty
- Low `Tot_HCPCS_Cds` (same one or two codes billed over and over)
- High `Tot_Sbmtd_Chrg / Tot_Mdcr_Pymt_Amt` ratio — the office is
  billing aggressively against a fixed fee schedule, dollar in / dollar
  out, with no clinical signal

### 4c. J-code drug substitution / oncology over-treatment

Billing an expensive J-code drug (often a chemotherapy or
rheumatology infusion) when a cheaper one was administered, or billing
for the drug at all when nothing was given. Concentrates in oncology,
rheumatology, ophthalmology (intravitreal injections), and pain
management.

**Column signatures:**
- High `Drug_Mdcr_Pymt_Amt` as a share of `Tot_Mdcr_Pymt_Amt`
- High `Drug_Mdcr_Pymt_Amt / Drug_Tot_Benes` (dollar-per-bene on the
  drug side)
- `Rndrng_Prvdr_Type` in {`Hematology/Oncology`, `Medical Oncology`,
  `Rheumatology`, `Ophthalmology`, `Pain Management`, `Interventional
  Pain Management`}

Canonical cases: **Salomon Melgen** (2013–2015, ophthalmology — billed
massive volumes of intravitreal anti-VEGF injections; multi-dosing
single-use vials), **Wahid Qamar** (oncology infusion fraud), **Farid
Fata** (above — both the E&M and the chemo J-codes light up for him).

### 4d. Telehealth abuse (2020+)

After COVID, CMS waived the originating-site restriction for telehealth
under the Public Health Emergency. Volume of telehealth-eligible E&M
codes exploded. Patterns that signal abuse:

**Column signatures:**
- A provider whose 2019 `Tot_Benes` was ~500 and whose 2020+ `Tot_Benes`
  is suddenly 5,000+ across many states — visible by joining year-over-
  year on `npi` and watching for step-changes
- `Med_Tot_Srvcs / Med_Tot_Benes` very low (one telehealth touch per
  bene)
- This PUF lacks an explicit `Place_Of_Srvc` (POS) column; the
  POS-02/POS-10 telehealth signal lives in `cms_partb_svc_*` at the
  per-HCPCS level

### 4e. Specialty-mismatched billing

A provider whose `Rndrng_Prvdr_Type` is "Family Practice" but whose
code mix (visible only in `cms_partb_svc_*`) is dominated by
cardiology, pain-management, or oncology codes. Or the specialty
itself disagrees with what NPPES has on file.

**Column signatures:**
- `Rndrng_Prvdr_Type` vs. NPPES primary taxonomy mismatch
- Specialty + code-mix mismatch (cross-source with HCPCS taxonomy —
  see [`fraudlib/ranker/hcpcs_taxonomy.py`](../../fraudlib/ranker/hcpcs_taxonomy.py))

### 4f. Trivial dollar baseline

For reference: simply sorting providers by `Tot_Mdcr_Pymt_Amt`
descending is a surprisingly strong baseline on the 2013 known cases
(P@100 = 28.6% vs. ML ranker 14.3%). Any feature engineering must beat
this dumb baseline at the P@K the agent budget allows.

## 5. Joins to other sources

| Join target | Key | Why |
|---|---|---|
| `cms_partb_svc_YYYY.parquet` | `npi` | Per-HCPCS detail. Required for upcoding (E&M code mix), J-code drug identification, and telehealth POS analysis — none of which is visible in this aggregated cut |
| `cms_partd_YYYY.parquet` | `npi` | Same physician's outpatient prescribing. Pill-mill physicians often have a Part B footprint too (the office visits that justify the scripts) |
| `cms_dmepos_YYYY.parquet` | `npi` | Same physician's equipment referrals. A doc with $2M in DMEPOS referrals but ~$80k in Part B is mismatched (signing orders without doing the underlying clinical work) |
| `nppes_features_*.parquet` | `npi` | Specialty/taxonomy ground-truth, credential, enumeration date. Catches mismatch between `Rndrng_Prvdr_Type` and what NPPES has |
| `leie.parquet` (legacy) | `npi` | OIG exclusion already on record |
| `gold_set.jsonl` | `npi` | Known fraud cases for label join |

Pitfalls when joining:

- **NPI must be string-zero-padded.** Native dtype is `int64`. After
  our scrape pass, the canonical `npi` column is `str` zero-padded to
  10; use that, not `Rndrng_NPI`. (Same convention as DMEPOS's `npi`
  derived from `Rfrg_NPI`.)
- **Identity column names differ from DMEPOS.** Part B is `Rndrng_*`
  (rendering); DMEPOS is `Rfrg_*` (referring). Joins on raw column
  names don't work — go through the canonical `npi` instead.
- **`Drug_*` ≠ Part D.** In Part B, `Drug_*` columns mean drugs the
  physician *administered in-office* (J-codes, IV infusions). For
  prescription drugs filled at a retail pharmacy, use `cms_partd_*`.
- **Organizations (`Ent_Cd == "O"`) are group billing entities.** A
  practice that bills under a group NPI shows up as one Part B row for
  the org plus zero individual-NPI rows for its physicians. Aggregating
  to "individual physician activity" needs to join through PECOS
  group/individual reassignment data, which is out of scope here.

## 6. Year coverage and schema drift

| Years | Columns | Notes |
|---|---|---|
| 2013–2016 | 56 native | No `Bene_CC_*` chronic-condition block |
| 2017–2023 | 81 native | Full schema; CC columns ~80% nonnull (rest cell-suppressed) |

The pre-2017 years have 25 fewer columns. Our scraper handles this via
`BaseScraper._align_to_schema(df)`, which NaN-pads the missing
`Bene_CC_*` columns so every year materializes with the same 81
columns. Consumers can detect "this is a pre-CC year" by checking
`df["Bene_CC_PH_Diabetes_V2_Pct"].notna().any()`.

Row counts grow steadily as Medicare enrollment grows: ~956k in 2013 →
~1.26M in 2023. Total Medicare payment in 2023 across all rows:
~$113 B.

CMS occasionally renames a column when the methodology changes. The
scraper's strict `_validate()` will reject any parquet with unknown
columns — which is intentional: it surfaces upstream drift at the
producer, not as a silent `KeyError` deep in the ranker.

### 6a. Dtype contract (Pandera) and observed per-year dtype drift

As of 2026-06 the schema is a Pandera `DataFrameModel`
([`pipeline/base.py`](../../pipeline/base.py)). `_validate()` now does
three things in one pass: **coerce** each column to its declared dtype,
**reject** undeclared columns (strict), and enforce **nullability + value
checks**. So column *dtype* drift — not just column presence — fails at
the producer instead of shipping silently to R2.

The historical R2 parquets carried real per-year dtype drift in the
chronic-condition block: the 25 `Bene_CC_*_Pct` prevalence columns landed
as `INTEGER` in the early CC years and widened to `DOUBLE` later. Observed
drift on `r2:needlepoint-data/processed/v1/cms_partb_*.parquet`:

| Column | R2 drift (pre-backfill) | Schema target | Cast |
|---|---|---|---|
| `Bene_CC_*_Pct` (all 25, e.g. `Bene_CC_PH_Diabetes_V2_Pct`, `Bene_CC_BH_Depress_V1_Pct`) | INTEGER (2013–2016) → **DOUBLE** (2017–2024) | `float` ≥ 0 | plain `→ DOUBLE` (widening; no int-hop, no `.0` risk) |
| `Rndrng_NPI` | (none — stable `str` on R2) | `str` `^(\d{10})?$` | no recast needed |

No columns require the int-hop for this source — the only drift is the
widening `INTEGER → DOUBLE` on the CC block, and the join key never
drifted.

**NPI handling.** The canonical lowercase `npi` column is clean across all
years — it was built `int64 → str`, zero-padded to 10 — so **always join on
`npi`, never the raw `Rndrng_NPI`**. The raw `Rndrng_NPI` is declared
`str` with the `^(\d{10})?$` check and showed no per-year drift on R2; it
needs no recast and no int-hop for this source. (Other sources whose raw
NPI drifted to `DOUBLE` need the int-hop `CAST(CAST(x AS BIGINT) AS
VARCHAR)` to avoid the `.0` trap; Part B does not.)

**Zero-padded code columns.** `Rndrng_Prvdr_State_FIPS` and
`Rndrng_Prvdr_Zip5` are clean VARCHAR on R2 for every Part B year (they
never drifted to numeric here, unlike `cms_partb_svc`/`cms_partd`). As a
guard, the scraper still reads both with `dtype="string"` so a future
release can't have them inferred as float and lose leading zeros (`"06"`,
`"02134"`) or the alphanumeric FIPS codes (`9A`–`9E`).

**Backfilling the historical parquets** to this contract:
[`pipeline/scripts/recast_r2_dtypes.py`](../../pipeline/scripts/recast_r2_dtypes.py)
derives the per-column cast from this schema (int-hop applied
automatically only for float→string targets, which Part B has none of),
rewrites R2 in place via DuckDB, and re-verifies. `--execute` to swap;
default is a dry-run.

## 7. Known limitations

- **Aggregate only.** No per-HCPCS detail. Upcoding, code-mix
  mismatches, J-code drug identification, and telehealth POS analysis
  all require the by-Provider-and-Service cut (`cms_partb_svc_*`).
  That file is 5–10 GB per year and is a separate scraper.
- **Suppression-floor invisibility.** Providers with `< 11` unique
  beneficiaries in the year are not in the file at all (see §3). For
  fraud purposes this is fine — schemes of interest operate well above
  that floor — but it means this PUF can't be used to study
  micro-scale billing or cold-NPI patterns.
- **Specialty is self-declared and lossy.** `Rndrng_Prvdr_Type` is the
  primary specialty as reported on the provider's claims; it has ~150
  distinct values, some of which conflate clinical and billing
  conventions (e.g. "Nurse Practitioner" is by far the most common,
  out-numbering any single physician specialty, because NPs of any
  clinical focus all map to one bucket). For taxonomic specialty,
  prefer NPPES.
- **State is the provider's billing address.** Patients are not
  resolved here, so cross-state telehealth schemes appear as a
  single-state row. The 2020+ telehealth signal needs `cms_partb_svc_*`
  + POS code.
- **`Med_*` is "everything that's not a Part B drug".** It's not
  "evaluation and management" specifically — it includes procedures,
  imaging, surgery, lab. To isolate E&M billing you need the per-HCPCS
  file.
- **Organizations.** ~5% of rows are `Ent_Cd == "O"` (group billing).
  These should usually be filtered out for per-physician ranking, but
  kept for facility-level aggregation.

## 8. File contract

After running [`PartBScraper`](../../pipeline/scrapers/cms_partb.py):

```
data/panels/cms_partb_YYYY.parquet              # 88 cols, ~956k–1.26M rows
data/panels/cms_partb_YYYY.manifest.json        # RunResult: source, sha-of-bytes, schema_version, columns, fetch URL
```

Manifest fields are documented on
[`pipeline.base.RunResult`](../../pipeline/base.py). Loaders read the
manifest at load time, compare `schema_version`, and warn on mismatch.
