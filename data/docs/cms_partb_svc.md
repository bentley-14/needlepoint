# CMS Part B — by Provider & Service (per-HCPCS line items)

**Scraper:** [`pipeline/scrapers/cms_partb_svc.py`](../../pipeline/scrapers/cms_partb_svc.py)
**Schema reference (columns + dtypes):** [docs/DATA_SCHEMAS.md](../../docs/DATA_SCHEMAS.md)
**Files:** `data/panels/cms_partb_svc_YYYY.parquet` + `cms_partb_svc_YYYY.manifest.json`

> ⚠️ **Multi-row per NPI.** Unlike every other source in this directory,
> ``cms_partb_svc`` is **not** one row per NPI — each row is one
> (NPI × HCPCS × Place_Of_Srvc) tuple. A typical provider repeats over
> tens to hundreds of rows. ``ProviderRecordSchema.npi`` is declared
> ``str`` and downstream consumers tend to treat it as a row key; that
> assumption is invalid here. Aggregate to NPI before joining to
> NPI-keyed sources. See §2 for the precise grain. The scraper still
> populates the canonical ``npi`` / ``provider_name`` / ``state`` /
> ``data_year`` columns on every row, but they are denormalized
> repeats, not unique keys.

## 1. What this dataset is, in plain English

This is the **per-line-item cut** of the same Medicare Physician &
Other Practitioners publication that ``cms_partb`` aggregates by
provider. Where ``cms_partb`` tells you "Dr. X billed Medicare $1.2 M
for 8,400 services to 1,300 beneficiaries this year", this PUF tells you
which **HCPCS codes** Dr. X billed, in which **place of service** they
billed each one, and the per-(code, POS) totals and per-service
averages.

```
   rendering physician  ──bills HCPCS code X at POS Y──▶   Medicare
        (Rndrng_NPI)             (HCPCS_Cd × Place_Of_Srvc)
```

This is **the** fraud-detection file. Most schemes show up as a
distribution of HCPCS codes — upcoding lives in the share of high-level
E&M codes (99214/99215, 99204/99205), J-code drug substitution lives in
the per-J-code dollar concentration, telehealth abuse lives in the
share of services billed at POS-02 or POS-10. The aggregated ``cms_partb``
file hides all of this because it sums across every code the provider
billed.

The cost: this is also the **biggest blast radius** of any CMS PUF in
the project — 5–10 GB CSV per year, ~10M+ rows. The whole pipeline
must respect that scale (see §6).

### Acronyms used in column names

| Acronym | Meaning |
|---|---|
| `Rndrng` | Rendering (the physician who personally provided the service) |
| `Prvdr` | Provider |
| `HCPCS` | Healthcare Common Procedure Coding System — the 5-char code for every billable line item (CPT is a subset; E&M codes are CPT; J-codes are HCPCS Level II drug codes) |
| `HCPCS_Cd` | The specific HCPCS code billed on this row |
| `HCPCS_Desc` | Plain-text description of the HCPCS code (denormalized) |
| `HCPCS_Drug_Ind` | `"Y"` if the HCPCS is a Part B drug (J-codes, infusions, chemo); blank otherwise |
| `Place_Of_Srvc` | Two-position bucket: `"F"` (facility — inpatient hospital, outpatient hospital, ASC) or `"O"` (non-facility — office, home, retail). The CMS payment rate differs per POS. See §4 for fraud-relevant POS-02/POS-10 (telehealth) — these are encoded as `"O"` here; the per-HCPCS POS detail at the 2-digit level (POS-02 vs POS-11 vs POS-22) is NOT in this file |
| `Tot_Benes` | Unique beneficiaries this provider billed this (HCPCS, POS) for |
| `Tot_Srvcs` | Total services billed for this (HCPCS, POS) line (a "service" is one billed line item; refills, multiple visits, multiple units of a drug each count) |
| `Tot_Bene_Day_Srvcs` | Distinct (bene, day) tuples — i.e. how many bene-day combinations this code was billed for. `Tot_Srvcs / Tot_Bene_Day_Srvcs > 1` signals multi-unit-per-day billing |
| `Avg_Sbmtd_Chrg` | Per-service average of what the provider's office billed |
| `Avg_Mdcr_Alowd_Amt` | Per-service average of what Medicare allowed under the fee schedule |
| `Avg_Mdcr_Pymt_Amt` | Per-service average of what Medicare actually paid (allowed minus bene cost-share) |
| `Avg_Mdcr_Stdzd_Amt` | Per-service average payment with geographic wage-index removed (use for cross-region comparison) |
| `Ent_Cd` | Entity code — `I` (individual) or `O` (organization). Same convention as ``cms_partb`` |
| `Mdcr_Prtcptg_Ind` | Medicare-participating indicator — `Y` if provider accepts assignment on every claim |
| `Crdntls` | Credentials (MD, DO, NP, PA, …) |
| `RUCA` | Rural-Urban Commuting Area — geographic classification code |
| `Prvdr_Type` | Specialty as self-declared in the PUF (free-text-ish; ~150 distinct values) |

**Dollar columns are averages, not totals.** To compute total Medicare
paid for a given row, multiply ``Avg_Mdcr_Pymt_Amt * Tot_Srvcs``.
Downstream feature code (`scripts/compute_hcpcs_rich_features.py`,
`fraudlib/ingest/cms/hcpcs_stats.py`) consistently derives ``_tot_paid``
this way.

## 2. Unit of observation

**One row = one (Rendering NPI × HCPCS_Cd × Place_Of_Srvc) tuple in a
calendar year.** Aggregated across every claim and every beneficiary
that fit the tuple.

A provider with `n_hcpcs_distinct = 80` who billed half their codes in
both facility and non-facility POS will produce ~120 rows in this file.
A high-volume oncologist or interventional radiologist can produce
500+ rows in a year. Across the population, **2023 has ~10M rows** for
~1.26M unique NPIs — i.e. ~8 rows per NPI on average.

The 28 raw CMS columns break into:

- 17 cols of provider identity (`Rndrng_*`) — **denormalized** onto
  every row of a given NPI (NPI, name, address, specialty,
  participation flag). Same column set and naming as ``cms_partb``
- 4 cols of HCPCS / place-of-service identifiers (`HCPCS_Cd`,
  `HCPCS_Desc`, `HCPCS_Drug_Ind`, `Place_Of_Srvc`)
- 7 cols of per-(NPI, HCPCS, POS) aggregates: 3 counts
  (`Tot_Benes`, `Tot_Srvcs`, `Tot_Bene_Day_Srvcs`) + 4 dollar averages
  (`Avg_Sbmtd_Chrg`, `Avg_Mdcr_Alowd_Amt`, `Avg_Mdcr_Pymt_Amt`,
  `Avg_Mdcr_Stdzd_Amt`)

After our scrape pass, parquets carry 7 additional canonical columns: 3
base stamps (`source`, `fetched_at_utc`, `schema_version`) + 4 NPI-keyed
canonicals (`npi`, `provider_name`, `state`, `data_year`) — see
[`pipeline.base.ProviderRecordSchema`](../../pipeline/base.py).

Total declared schema: **35 columns**.

**Why the canonical NPI columns are denormalized here.** Every other
NPI-keyed source has ~1 row per NPI, so ``ProviderRecordSchema``'s
``npi``/``provider_name``/``state`` reasonably look like row keys. In
this source they are duplicated on every line-item row for the same
provider. That keeps the schema uniform across producers and lets
``df.groupby("npi")`` work without re-joining, but it is wasted bytes —
~50–100 MB of redundant strings per year's parquet. Worth accepting for
schema consistency.

The scraper builds the ``provider_name`` map once per unique NPI and
broadcasts via ``.map()`` rather than calling
``format_provider_name`` per row — a row-by-row call would be brutal
at this scale.

## 3. How CMS publishes it (cadence + suppression)

- **Annual release.** Each PUF covers one calendar year.
- **Lag.** ~18 months from end-of-year to release. Data year 2023 was
  published in April 2025 (initial V10) and rolling years 2013–2022
  were republished in November 2025 under RY25 V20. Data year 2024
  was re-posted in May 2026 under RY26 V10 after the February release
  was retracted; that release **dropped the ``MUP_`` prefix from the
  filename** but kept the column headers unchanged. The URL catalog
  lives in
  [`pipeline/scrapers/cms_partb_svc.py:URLS`](../../pipeline/scrapers/cms_partb_svc.py).
- **Suppression rules — applied upstream by CMS, not by our code.**
  Our scraper does no row-level filtering; what's in the parquet is
  what CMS published.
  - **Per-(NPI, HCPCS, POS) tuple floor.** Each row's `Tot_Benes` has a
    hard floor of 11. (NPI, code, POS) combinations where the
    underlying unique-beneficiary count would be `< 11` are dropped by
    CMS before publication. Empirically: `Tot_Benes.min() == 11`, no
    nulls in this column.
  - **No cell-level redaction in this PUF.** Unlike ``cms_partb``,
    which carries category-level ``Drug_Sprsn_Ind``/``Med_Sprsn_Ind``
    flags, the by-Service file has no suppression indicator column.
    The whole row either exists or doesn't.
  - **What this means for fraud detection.** A specialist who bills 20
    HCPCS codes but has fewer than 11 unique benes per code is
    invisible at the per-HCPCS grain even when their aggregate Part B
    cut is published. Schemes of interest concentrate volume, so this
    is rarely a real loss — but it does mean a provider's
    ``cms_partb_svc`` row count is downward-biased relative to the
    true number of codes they billed.
- **HCPCS descriptions are versioned.** When a HCPCS code's description
  changes between PUF vintages, the same ``HCPCS_Cd`` can have
  different ``HCPCS_Desc`` strings in different years' rows. Always
  join on ``HCPCS_Cd``, never on ``HCPCS_Desc``.

## 4. Fraud signals — what to look for

This is the file where most Part B fraud schemes are diagnostically
visible. The ``cms_partb`` aggregate hides them by summing across all
codes; here each (HCPCS, POS) combination is a separate row and the
distribution itself is the signal.

### 4a. Upcoding (the canonical Part B scheme)

Billing E&M codes one or two levels higher than the visit complexity
justified — 99215 ("complex established-patient visit, ~40 min") when
99213 ("low-complexity, ~15 min") was actually rendered. Each step up
the established-patient E&M ladder roughly doubles per-visit payment;
sustained over a year, a 15-percentage-point shift in code mix moves
a million-dollar primary-care practice up by $200k+. Same pattern for
new-patient visits (99201–99205, retired 2021 → 99202–99205).

**Column signatures:**
- Within the E&M established-patient family (99211, 99212, 99213,
  99214, 99215), `Tot_Srvcs` concentrated in the top two codes
  (99214 + 99215) for a non-complex-specialty provider — e.g. Family
  Practice, Internal Medicine. The `em_high_share` and
  `em_distribution_entropy` features in
  [`fraudlib/ingest/cms/hcpcs_stats.py`](../../fraudlib/ingest/cms/hcpcs_stats.py)
  compute this directly
- Same for new-patient visits (99201–99205, pre-2021) and the
  replacement codes (99202–99205, 2021+)
- High `Avg_Mdcr_Pymt_Amt` on 99215 rows for the same provider —
  consistent with billing the highest-tier code rather than the
  modal tier

Canonical cases:
- **Farid Fata** (2013, oncology, Michigan) — diagnosed and "treated"
  hundreds of patients who didn't have cancer; billed extreme volumes
  of 99215 E&M visits in addition to the chemo. 99215 frequency is
  his fingerprint signature in this file.
- **Xiulu Ruan / John Couch** (Mobile, AL pain clinic) — both convicted
  2017 for distributing controlled substances + Medicare fraud; their
  E&M code mix shows the upcoding pattern alongside the Part D pill-mill
  prescribing.

### 4b. J-code drug substitution / oncology over-treatment

J-codes are HCPCS Level II codes for drugs administered in-office
(infusions, injections, chemotherapy, anti-VEGF for retinal disease,
biologics). The schemes vary:

- Billing an expensive J-code when a cheaper drug was administered (or
  nothing was given)
- Multi-dosing single-use vials to inflate Tot_Srvcs while reusing one
  vial's worth of drug
- Routine administration of high-cost biologics regardless of
  diagnosis

**Column signatures:**
- `HCPCS_Drug_Ind == "Y"` rows for a given provider; sum
  `Avg_Mdcr_Pymt_Amt * Tot_Srvcs` to get total drug payment
- Specific J-codes light up specialty-specific scheme patterns:
  J0178 (aflibercept), J0586 (abobotulinum), J2778 (ranibizumab/Lucentis),
  J9035 (bevacizumab) for ophthalmology; J9305+ chemo codes for oncology
- Bene-day-srvcs ratio: `Tot_Srvcs / Tot_Bene_Day_Srvcs` >> 1 on a
  J-code means many units per bene per day — multi-dosing
- `Rndrng_Prvdr_Type` in {`Hematology/Oncology`, `Medical Oncology`,
  `Rheumatology`, `Ophthalmology`, `Pain Management`, `Interventional
  Pain Management`}

Canonical cases:
- **Salomon Melgen** (2013–2015, ophthalmology, Florida) — billed
  massive volumes of intravitreal anti-VEGF injections, multi-dosing
  single-use vials of Lucentis/Eylea. His fingerprint in this file is
  extreme `Tot_Srvcs` on J2778 (and later J0178) at single
  ophthalmology NPIs, with bene-day-srvcs ratios way above peer
  ophthalmologists. (The often-cited "J67028 intravitreal injection
  code" is actually CPT 67028 — the *procedure* code that pairs with
  the J-code drug; both appear on Melgen's rows.)
- **Farid Fata** (above) — both the E&M upcoding and the chemo J-codes
  (J9305+) light up.
- **Wahid Qamar** — oncology infusion fraud, similar J-code
  concentration pattern.

### 4c. Telehealth abuse (2020+)

After the COVID Public Health Emergency, CMS waived the originating-site
restriction for telehealth. Volume of telehealth-eligible E&M codes
exploded. Schemes that surfaced include billing for telehealth visits
to beneficiaries the provider never spoke to, billing telehealth at
office-visit rates without a real audio-video encounter, and
cross-state-mill operations where a single provider bills telehealth to
thousands of patients in multiple states.

**Column signatures:**
- Place_Of_Srvc patterns: this PUF's `Place_Of_Srvc` is the two-bucket
  facility/non-facility flag, NOT the 2-digit POS code. POS-02
  (telehealth, distant site) and POS-10 (telehealth, home) both fall
  under `"O"` (non-facility) here, so the facility/non-facility flag
  alone doesn't isolate telehealth. The telehealth signal is in the
  **HCPCS code** — telehealth-eligible E&M codes paired with modifier
  95 (audio-video) or POS-02/POS-10 carry the same base CPT code; the
  underlying detail is only in CMS LDS/RIF claims data, not this PUF.
  The available proxy here is **HCPCS distribution shift**: a provider
  whose 2019 line-item count was 200 and whose 2020+ line-item count
  is 2,000+ across telehealth-eligible E&M codes
- `Tot_Srvcs / Tot_Benes` very low (one telehealth touch per bene)
  combined with high `Tot_Benes` per E&M code
- Cross-year join: same NPI's 2019 row count vs. 2020+ row count for
  E&M codes; a step-change is the proxy signal

⚠️ **The telehealth POS-02/POS-10 fraud signature is partially obscured
in this PUF.** Tools like `fraudlib/ranker/hcpcs_taxonomy.py` reference
the 2-digit POS codes for telehealth detection, but this PUF only
carries the F/O facility flag. The actual POS-02/POS-10 detail comes
from CMS LDS/RIF claims, which aren't in scope. Use the HCPCS-set
proxy above.

### 4d. Modifier-25 abuse

Modifier 25 ("significant, separately identifiable E&M service") lets a
provider bill an E&M visit *on the same day* as a procedure, when the
visit is genuinely separate from the procedure's pre/post evaluation.
Some specialties (dermatology, pain medicine, gastroenterology)
systematically attach modifier 25 to every procedure to double-bill.

**Column signatures:**
- ⚠️ **No modifier column in this PUF.** The schema-drift probe
  (2026-05-30) confirmed every year 2013–2024 ships the same 28
  columns; modifier is not among them. The same-day-E&M-plus-procedure
  pattern can be approximated via `Tot_Bene_Day_Srvcs` ratios — if a
  provider's E&M codes and procedure codes have correlated
  per-bene-day service counts, modifier-25 attachment is plausible —
  but the direct evidence is in CMS LDS/RIF claims, not this PUF.

### 4e. Anesthesia time-unit fraud

Anesthesia HCPCS (00100–01999) are billed in 15-minute time units. An
anesthesiologist who bills 60 units (15 hours) for a procedure that took
2 hours is fraudulent. The patterns surface as outliers in
`Tot_Srvcs / Tot_Bene_Day_Srvcs` for anesthesia codes (each bene-day
has one true case but the unit count inflates the service count) and as
extreme `Avg_Mdcr_Pymt_Amt` on anesthesia codes relative to specialty
peer median.

### 4f. Place-of-service mismatch

CMS pays more for office E&M when billed POS-11 (office) than POS-22
(outpatient hospital), because POS-22 splits the facility fee from the
professional fee while POS-11 bundles. Conversely, some procedure codes
pay more at POS-22. Schemes that miscode POS to maximize payment surface
as a provider whose **facility/non-facility share** for a given HCPCS
diverges from peer median.

**Column signatures:**
- For a given HCPCS code, a provider's share of `Tot_Srvcs` at
  `Place_Of_Srvc == "F"` vs `"O"` compared to the specialty median.
  The facility/non-facility flag at this granularity is the available
  signal; the 2-digit POS detail (POS-11 vs POS-22 vs POS-31 etc.) is
  not in this PUF.

### 4g. Specialty-mismatched billing

A `Rndrng_Prvdr_Type` of "Family Practice" with a code mix dominated by
cardiology, pain-management, or oncology codes. Or a "Dentist" billing
systemic-medication administration codes. The taxonomy-vs-code-mix
mismatch is computable cleanly here because the line-item HCPCS list IS
the code mix.

**Column signatures:**
- `Rndrng_Prvdr_Type` vs. HCPCS taxonomy distribution mismatch
  (compute via [`fraudlib/ranker/hcpcs_taxonomy.py`](../../fraudlib/ranker/hcpcs_taxonomy.py))
- Cross-source: `Rndrng_Prvdr_Type` vs NPPES primary taxonomy

## 5. Joins to other sources

| Join target | Key | Why |
|---|---|---|
| `cms_partb_YYYY.parquet` | `npi` | Same provider's aggregate. Reconcile: `sum(Tot_Srvcs)` over all rows for a given NPI in svc should approximate `Tot_Srvcs` in the aggregate cut (CMS suppression on either side can introduce drift) |
| `cms_partd_YYYY.parquet` | `npi` | Same prescriber's outpatient Rx. Pill-mill physicians have both a J-code/E&M footprint here and an opioid footprint in Part D |
| `cms_dmepos_YYYY.parquet` | `npi` | Same physician as the referring provider. Cross-source consistency check on volume vs. specialty |
| `cms_hcpcs_features_YYYY.parquet` | `npi` | **Derived from this source.** Re-run [`scripts/compute_hcpcs_rich_features.py`](../../scripts/compute_hcpcs_rich_features.py) after any rescrape — the ranker's HCPCS feature block is downstream of this file |
| `nppes_features_*.parquet` | `npi` | Specialty/taxonomy ground-truth. The §4g specialty mismatch needs NPPES |
| `gold_set.jsonl` | `npi` | Known fraud cases for label join |

Pitfalls when joining:

- **Aggregate to NPI before joining to NPI-keyed sources.** Joining
  the raw multi-row frame to a per-NPI source (NPPES, Part B aggregate,
  gold_set) creates a Cartesian-style explosion. Always
  ``df.groupby("npi").agg(...)`` first, or join after pre-aggregating
  the features you care about.
- **NPI must be string-zero-padded.** Native dtype of ``Rndrng_NPI`` is
  `int64`. After our scrape pass, the canonical `npi` column is `str`
  zero-padded to 10; use that.
- **HCPCS descriptions drift across years.** Join on `HCPCS_Cd`, not
  on `HCPCS_Desc`. Use a current HCPCS taxonomy table for the
  human-readable label.
- **Organizations (`Ent_Cd == "O"`).** Group-billing entities show up
  with one set of rows per HCPCS at the group NPI. For per-physician
  per-HCPCS analysis, filter to `Ent_Cd == "I"`.

## 6. Year coverage and schema drift

| Years | Columns | Notes |
|---|---|---|
| 2013–2024 | 28 native | Identical column set across every year (probed 2026-05-30) |

The schema-drift probe ran HEAD-range CSV header requests against all
12 catalogued URLs on 2026-05-30 and confirmed every year — including
the 2024 RY26 V10 release that dropped the ``MUP_`` filename prefix —
ships the same 28 columns with identical casing. **No
``_align_to_schema`` call and no ``_RENAMES`` dict are needed today.**
The base ABC's strict ``_validate()`` will surface any future drift at
the producer (the contract is uniform with ``cms_partb`` and
``cms_partd`` here).

If CMS ever adds modifier columns (mod-25, mod-50, mod-59, …) to this
PUF, that's a major fraud-signal upgrade — see §4d/§4e. The strict
validator will flag the new columns at first run; declare them on the
schema and rebump ``schema_version`` to ``v1.1``.

Row counts (approximate):

| Year | Rows | Unique NPIs |
|---|---|---|
| 2013 | ~9.4M | ~950k |
| 2019 | ~9.8M | ~1.0M |
| 2023 | ~10M | ~1.26M |
| 2024 | ~10M | ~1.27M |

CSV file size: 5–10 GB per year. Parquet output: 1–3 GB per year.

### 6a. Dtype contract (Pandera) and observed per-year dtype drift

As of 2026-06 the schema is a Pandera `DataFrameModel`
([`pipeline/base.py`](../../pipeline/base.py)). `_validate()` now does
three things in one pass: **coerce** each column to its declared dtype,
**reject** undeclared columns (strict), and enforce **nullability + value
checks**. So column *dtype* drift — not just column presence — fails at
the producer instead of shipping silently to R2.

The §6 column-set probe (2026-05-30) confirmed the same 28 native columns
across every year; what still drifted was the *dtype* of one column on the
historical R2 parquets:

| Column | R2 drift | Schema target | Fix |
|---|---|---|---|
| `Rndrng_Prvdr_State_FIPS` | DOUBLE (2013–2019) → VARCHAR (2020–2024) | `str` | **RE-INGEST** (not castable) |

⚠️ **Correction (2026-06): FIPS is NOT int-hop-fixable.** An earlier version
of this section claimed the `BIGINT` int-hop was "safe" because FIPS is an
"integer-valued code." That is wrong: FIPS is a *zero-padded, sometimes
alphanumeric* code, not an integer. The DOUBLE years already lost the
leading zero (`"06"`→`6.0`) and coerced the foreign/unknown codes `9A`–`9E`
to `NaN` — and the int-hop only makes it worse (`6.0`→`"6"`, not `"06"`).
There is no cast that recovers it. The 2013–2019 DOUBLE years must be
**re-ingested** from source (and `Rndrng_Prvdr_State_FIPS` is listed in
`recast_r2_dtypes.REINGEST_ONLY`, so the recast correctly skips it).

**Going forward** the scraper reads `Rndrng_Prvdr_State_FIPS` and
`Rndrng_Prvdr_Zip5` with `dtype="string"`, so pandas can no longer infer
them as numeric — new writes preserve the leading zeros and alphanumerics.
The usable substitute for the damaged historical years is
`Rndrng_Prvdr_State_Abrvtn` (clean VARCHAR every year).

**NPI handling.** The canonical lowercase `npi` column is clean across all
years (it was built `int64 → str`, zero-padded to 10) — **always join on
`npi`, never the raw `Rndrng_NPI`**. The raw NPI column is declared `str`
with the `^(\d{10})?$` check, so a `DOUBLE`-typed NPI with a `.0` tail
would now be rejected at write-time rather than silently matching nothing.

**Backfilling the historical parquets:** the dtype-only columns are handled
by [`pipeline/scripts/recast_r2_dtypes.py`](../../pipeline/scripts/recast_r2_dtypes.py)
(dry-run by default). `Rndrng_Prvdr_State_FIPS` is **not** among them — it is
`REINGEST_ONLY`, so the 2013–2019 years must be re-pulled by re-running the
scraper's `fetch(year=…)` (now that the read forces `string`), not cast.

## 7. Known limitations

- **No modifier columns.** Mod-25, mod-50, mod-59 — the workhorse
  modifiers for unbundling and double-billing — are not in this PUF.
  Direct modifier-attachment evidence requires CMS LDS/RIF claims (not
  in project scope).
- **POS bucket is binary.** This PUF carries `Place_Of_Srvc` as F/O
  (facility / non-facility), not the 2-digit POS code (POS-02 vs
  POS-11 vs POS-22 vs POS-10). Telehealth-specific (POS-02, POS-10)
  fraud needs the 2-digit detail, which only lives in CMS LDS/RIF.
- **Dollar columns are averages.** ``Avg_Sbmtd_Chrg``,
  ``Avg_Mdcr_Alowd_Amt``, ``Avg_Mdcr_Pymt_Amt``, ``Avg_Mdcr_Stdzd_Amt``
  are per-service averages. For totals, multiply by ``Tot_Srvcs``.
- **No per-beneficiary detail.** A row aggregates across the (NPI,
  HCPCS, POS) tuple; individual claims and individual benes are not
  visible. Patient-level fraud patterns (doctor-shopping, cross-state
  beneficiary recruitment) need a different data source.
- **Suppression-floor invisibility at the line level.** (NPI, HCPCS,
  POS) tuples below 11 unique benes are dropped — a code a provider
  billed only a handful of times is invisible. The schemes of interest
  bill at scale, so this is rarely a real loss for fraud detection,
  but it means a provider's "true" HCPCS diversity is upward-biased
  vs. what's visible here.
- **Disk and memory pressure.** 5–10 GB per year CSV; the legacy
  pipeline lost a Part D backfill file mid-pass when free disk ran
  out. The backfill loop (deferred to a follow-up agent) must do
  one-year-at-a-time pull → process → push → delete.
- **HCPCS description drift across years.** Join on code, not on
  description.

## 8. File contract

After running [`PartBSvcScraper`](../../pipeline/scrapers/cms_partb_svc.py):

```
data/panels/cms_partb_svc_YYYY.parquet        # 35 cols, ~9–10M rows
data/panels/cms_partb_svc_YYYY.manifest.json  # RunResult: source, schema_version, columns, fetch URL
```

Manifest fields are documented on
[`pipeline.base.RunResult`](../../pipeline/base.py). Loaders read the
manifest at load time, compare `schema_version`, and warn on mismatch.

**Downstream rebuild trigger:** any rescrape of this source invalidates
`cms_hcpcs_features_*` — re-run
[`scripts/compute_hcpcs_rich_features.py`](../../scripts/compute_hcpcs_rich_features.py)
afterward.
