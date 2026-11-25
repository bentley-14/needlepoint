# CMS Part D — by Provider (aggregated)

**Scraper:** [`pipeline/scrapers/cms_partd.py`](../../pipeline/scrapers/cms_partd.py)
**Schema reference (columns + dtypes):** [docs/DATA_SCHEMAS.md](../../docs/DATA_SCHEMAS.md)
**Files:** `data/panels/cms_partd_YYYY.parquet` + `cms_partd_YYYY.manifest.json`

## 1. What this dataset is, in plain English

**Part D** is the slice of Medicare that pays for outpatient prescription
drugs filled at retail pharmacies. (Part A is hospital stays; Part B is
physician services and in-office drugs administered by infusion or
injection — chemo, J-codes; DMEPOS is durable equipment; see those
docs.) This PUF aggregates **one calendar year of Part D prescribing
per prescriber**, summing every claim a pharmacy filled where this NPI
signed the prescription.

The supply chain has three parties:

```
   prescribing provider  ──writes script──▶   pharmacy   ──bills──▶   Medicare Part D plan
        (PRSCRBR_NPI)                      (Rx filler)
```

The NPI on each row is the person who wrote the prescription. The
pharmacy that filled it is not visible — only the prescriber's annual
totals across all pharmacies and all drugs they prescribed.

This is the dataset where **pill mills**, **opioid over-prescribing**,
**nursing-home antipsychotic abuse**, and **brand-vs-generic kickback
patterns** show up. The dedicated opioid block (`Opioid_*`) was added
specifically because DEA/OIG enforcement priorities centered on it.

### Acronyms used in column names

| Acronym | Meaning |
|---|---|
| `Prscrbr` (or `PRSCRBR`) | Prescriber. NPI column is the all-caps `PRSCRBR_NPI`; everything else uses the mixed-case `Prscrbr_*` prefix |
| `Tot_Clms` | Total prescription claims (one claim = one fill, including refills) |
| `Tot_30day_Fills` | Total fills normalized to 30-day equivalents (a 90-day Rx counts as 3) |
| `Tot_Drug_Cst` | Total cost — what Medicare + plan + patient paid combined for the drug |
| `Tot_Day_Suply` | Total days of supply dispensed |
| `Tot_Benes` | Unique beneficiaries who filled a script from this prescriber |
| `Bene` | Beneficiary |
| `GE65` | "Greater than or equal to 65" — claims/cost for beneficiaries ≥65 (the Medicare-eligibility-by-age cohort, excluding disability-eligibility under-65s) |
| `Brnd` / `Gnrc` / `Othr` | Brand / Generic / Other drug class. "Other" includes vaccines + non-prescription items billed through Part D |
| `MAPD` | Medicare Advantage Prescription Drug plan (drug benefit bundled into a Part C plan) |
| `PDP` | Prescription Drug Plan (standalone Part D plan, no medical benefit) |
| `LIS` / `NonLIS` | Low-Income Subsidy (Medicare's "Extra Help" — premium/copay subsidy for low-income beneficiaries) vs. those without it |
| `Opioid` | Schedule II–IV opioids (oxycodone, hydrocodone, fentanyl, …). The block is the DEA-scrutinized fraud signal |
| `Opioid_LA` | Long-acting opioids — extended-release/oxycontin-style. Higher abuse potential, tighter scrutiny |
| `Antbtc` | Antibiotics |
| `Antpsyct_GE65` | Antipsychotics prescribed to ≥65 beneficiaries — flagged because nursing-home patients are sometimes chemically restrained off-label |
| `Sprsn_Flag` | Suppression flag (`*` or `#`). **Note:** one column uses the typo `Suprsn` — preserved to match the upstream header |
| `Ent_Cd` | Entity code — `I` (individual) or `O` (organization). Part D is overwhelmingly `I` (only 1 org in 2023) |
| `Crdntls` | Credentials (MD, DO, NP, PA, PharmD, DDS, …) |
| `RUCA` | Rural-Urban Commuting Area code |

## 2. Unit of observation

**One row = one prescribing NPI × one calendar year.** Aggregated across
every claim every pharmacy filled where this NPI was the prescriber, and
across every drug.

This is *not* a per-claim or per-drug dataset. There is no
per-pharmacy breakdown, no per-NDC detail. The Drug-vs-Brnd-vs-Gnrc
split tells you the prescriber's mix at a high level but not which
specific drugs they wrote. For per-drug detail use the sibling source
`cms_partd_drug` (by Provider & Drug, multi-row per NPI — not yet
scraped).

The 84 raw CMS columns break into:

- 17 cols of prescriber identity (`Prscrbr_*`, plus the all-caps
  `PRSCRBR_NPI`) — NPI, name, address, specialty, specialty source
- 5 cols of total prescribing aggregates (`Tot_*`)
- 7 cols of age-65+ subset (`GE65_*`)
- 3 cols × 3 drug classes (`Brnd_*`, `Gnrc_*`, `Othr_*`) = 9 cols
- 3 cols × 2 plan types (`MAPD_*`, `PDP_*`) = 6 cols
- 3 cols × 2 LIS slices (`LIS_*`, `NonLIS_*`) = 6 cols
- 10 cols of opioid prescribing (5 base `Opioid_*` + 5 long-acting `Opioid_LA_*`)
- 3 cols of antibiotic prescribing (`Antbtc_*`)
- 5 cols of antipsychotic-65+ prescribing (`Antpsyct_GE65_*`)
- 15 cols of beneficiary demographics + 1 risk score

After our scrape pass, parquets carry 7 additional canonical columns: 3
base stamps (`source`, `fetched_at_utc`, `schema_version`) + 4 NPI-keyed
canonicals (`npi`, `provider_name`, `state`, `data_year`) — see
[`pipeline.base.ProviderRecordSchema`](../../pipeline/base.py).

Total declared schema: **91 columns**.

## 3. How CMS publishes it (cadence + suppression)

- **Annual release.** Each PUF covers one calendar year.
- **Lag.** ~18 months from end-of-year to release. Data year 2023 was
  published in April 2025 under the RY25 V10 release; older years were
  republished in November 2025 under RY25 V20 alongside Part B. URL
  catalog lives in
  [`pipeline/scrapers/cms_partd.py:URLS`](../../pipeline/scrapers/cms_partd.py);
  expect URLs to rotate per RY release.
- **Suppression rules — applied upstream by CMS, not by our code.**
  Our scraper does no row-level filtering; what's in the parquet is what
  CMS published. Two suppression layers:
  - **Row-level floor.** `Tot_Benes` has a hard floor of 11 for the
    prescriber to be in the file at all. Empirically on 2023:
    `Tot_Benes.min() == 11`, ~10% nulls for prescribers where total
    bene count was below the floor but other columns still met it.
  - **Cell-level numeric suppression.** When a subcategory count would
    be `< 11`, CMS nulls the corresponding count + dollar cells. The
    row survives.
  - **Category-level flags `*_Sprsn_Flag`.** Each subcategory (GE65,
    Brnd, Gnrc, Othr, MAPD, PDP, LIS, NonLIS, Antpsyct_GE65) carries a
    flag with two suppression codes:
    - `*` — cell `n < 11`, redacted for privacy
    - `#` — value was unreliable or low-quality, redacted for data quality
    Same convention as Part B and DMEPOS, but Part D uses the suffix
    `_Sprsn_Flag` (not `_Sprsn_Ind`).
  - **The opioid block has no Sprsn flag** — opioid columns are nulled
    when suppressed but there is no per-row indicator. Detect via
    `Opioid_Tot_Clms.isna()` directly.
  - **What this means for fraud detection.** Pill mills are by
    definition high-volume operations and clear the floor easily. The
    floor blinds the data to small-scale doctor-shopping enablers
    (the doc who writes 30 opioid scripts a year for friends and
    family), which isn't the fraud population of interest here anyway.
  - **Do not** assume `fraudlib/ingest/cms/partd_stats.py`'s treatment
    of suppression is correct — see the DMEPOS doc §7 for the
    analogous bug, and partd_stats.py has not been audited against this
    schema.

## 4. Fraud signals — what to look for

Part D is where some of the highest-volume, highest-profile DOJ
enforcement actions concentrate. The schemes here are categorically
different from Part B (which is upcoding and J-code fraud) — Part D is
**prescription-pad fraud**.

### 4a. Opioid pill mills (the canonical Part D scheme)

A physician sees patients for 60-second "visits" and writes high-dose
opioid prescriptions, often the same regimen repeated across hundreds
of patients. Cash-pay clinic out front for the visit; legitimate
pharmacy fills the script and bills Medicare for the drug. The doctor's
income comes from the visit volume; Medicare pays for the drugs.

**Column signatures:**
- High `Opioid_Tot_Clms` relative to `Tot_Clms` (opioids dominate)
- High `Opioid_Prscrbr_Rate` for a specialty whose median rate is low
  (Family Practice, Internal Medicine, Emergency Medicine)
- High `Opioid_LA_Tot_Clms` — long-acting opioids (oxycontin, MS
  Contin, fentanyl patches) are the DEA's highest-scrutiny class
- `Opioid_LA_Tot_Drug_Cst / Opioid_Tot_Drug_Cst` close to 1 (cost
  concentrated in LA) for non-pain-medicine specialties is a strong
  signal

Canonical cases: **Xiulu Ruan + John Couch** (Mobile, AL pain clinic —
both in `gold_set.jsonl`; convicted 2017 for distributing controlled
substances + Medicare fraud), **Paul Volkman** (Ohio "pill mill"
physician — top opioid prescriber in country at his peak), **Joel
Smithers** (Virginia — 500k+ pills over 2.5 years).

### 4b. Antipsychotic abuse in nursing homes

Off-label use of antipsychotics (Risperdal, Seroquel, Zyprexa) to
sedate elderly nursing-home patients with dementia. FDA black-boxed
this use; CMS publishes the `Antpsyct_GE65_*` block partly to surface
it.

**Column signatures:**
- High `Antpsyct_GE65_Tot_Clms` / `Antpsyct_GE65_Tot_Benes` (extreme
  concentration of antipsychotic Rx on elderly patients)
- Prescriber type doesn't match psychiatry (Family Practice,
  Geriatrics, Internal Medicine writing high antipsychotic volumes)
- Cross-source: prescriber's address near a nursing facility (join
  with `cms_facility_snf`)

### 4c. Brand-vs-generic kickback schemes

Pharma manufacturer pays a prescriber (via Speakers Bureau, consulting,
"research") to write brand-name prescriptions when a chemically
equivalent generic exists. The cost differential is enormous (a brand
statin can cost 20× the generic), and Medicare absorbs it.

**Column signatures:**
- `Brnd_Tot_Drug_Cst / Tot_Drug_Cst` extreme for the specialty — high
  brand share is a red flag, especially for specialties with cheap
  generics (cardiology statins, GI PPIs)
- Cross-source: large OpenPayments payments to the same NPI from the
  manufacturer whose drug shows up here

Canonical case: pattern visible in many DOJ Anti-Kickback Statute cases
(e.g., **Insys Therapeutics** — Subsys fentanyl spray prescribers got
"speakers bureau" payments; several of those prescribers are in the
gold_set).

### 4d. Compounding pharmacy fraud (proxy signal)

Schemes where a compounding pharmacy bills Medicare for high-cost
custom pain creams or other compounded drugs that weren't actually
medically necessary, with prescribers paid kickbacks per script. The
pharmacy is the biller (not visible in this PUF), but the prescriber
shows up with:

**Column signatures:**
- Extreme `Tot_Drug_Cst / Tot_Clms` (cost per claim very high — single
  scripts billed at $10k+)
- `Othr_Tot_Drug_Cst / Tot_Drug_Cst` high (compounds often classify
  as "Other")

### 4e. Specialty-mismatched prescribing

A "Dentist" or "Optometrist" with high volumes of systemic prescriptions
(cardiovascular, psychiatric), or a "Family Practice" prescriber with
oncology-grade chemo regimens. Mostly catches pill-mill specialties
masquerading as general practice.

**Column signatures:**
- `Prscrbr_Type` vs. drug-class distribution mismatch
- Cross-source: NPPES primary taxonomy disagrees with `Prscrbr_Type`

### 4f. Trivial dollar baseline

For reference: sorting prescribers by `Tot_Drug_Cst` descending puts a
lot of legitimate high-volume specialists (oncologists, rheumatologists
on expensive biologics) at the top alongside fraudsters. The
opioid-specific signal (`Opioid_LA_Prscrbr_Rate`) is the cleaner Part D
baseline — anything above the 99th percentile for the specialty is
worth investigating.

## 5. Joins to other sources

| Join target | Key | Why |
|---|---|---|
| `cms_partb_YYYY.parquet` | `npi` | Same physician's office-visit / procedure activity. Pill-mill physicians have a Part B footprint (the visits that produce the Rx) — the ratio of Part D drug cost to Part B payment is itself a feature |
| `cms_dmepos_YYYY.parquet` | `npi` | Same physician's equipment referrals. Some scheme overlap (a pain-management doc may also refer braces) |
| `cms_partd_drug_YYYY.parquet` | `npi` | Per-NDC detail. Required for any drug-specific analysis (which exact opioid? which brand?) — not yet scraped |
| `openpayments_*.parquet` | `npi` | Pharma manufacturer payments to this physician. Direct kickback signal for §4c |
| `nppes_features_*.parquet` | `npi` | Specialty/taxonomy ground-truth |
| `cms_facility_snf.parquet` | (geographic) | For §4b — identifying prescribers near nursing facilities |
| `leie.parquet` (legacy) | `npi` | OIG exclusion already on record |
| `gold_set.jsonl` | `npi` | Known fraud cases for label join |

Pitfalls when joining:

- **NPI column name.** Raw CMS uses `Prscrbr_NPI` (V20) or
  `PRSCRBR_NPI` (V10) depending on release; our scraper aliases the V10
  variant to V20 at parse time. The canonical `npi` column (zero-padded
  string) is what to join on across sources; don't reach for the raw
  prescriber column directly.
- **Prescriber ≠ dispenser.** The pharmacy that filled the script is
  invisible. A prescriber writing a million opioid claims doesn't mean
  one pharmacy filled them — they could be spread across dozens of
  pharmacies.
- **"Other" drugs include vaccines.** `Othr_Tot_Drug_Cst` can be high
  for pediatricians (vaccines) — not a fraud signal per se. Disambiguate
  by specialty.
- **`Tot_Benes` is patient count, not script count.** `Tot_Clms` is
  scripts. A high `Tot_Clms / Tot_Benes` ratio means many refills per
  patient (chronic management — or doctor-shopping enabler).

## 6. Year coverage and schema drift

| Years | Native cols | Notes |
|---|---|---|
| 2013–2022 | 84 (V20 release) | `Prscrbr_NPI`, `Prscrbr_Type_Src` (mixed-case) |
| 2023 | 84 (V10 release) | `PRSCRBR_NPI` (all-caps), `Prscrbr_Type_src` (lowercase `src`) |

Unlike Part B and DMEPOS, Part D didn't add or drop column blocks
across years — the opioid / antibiotic / antipsychotic blocks have been
present since 2013. **But CMS renamed two columns between the V10 and
V20 RY25 releases**, breaking trivial year-over-year column alignment:

- `PRSCRBR_NPI` (V10) ↔ `Prscrbr_NPI` (V20) — case change only
- `Prscrbr_Type_src` (V10) ↔ `Prscrbr_Type_Src` (V20) — case change on `S`

We canonicalize on the V20 mixed-case form (used by 10 of 11 years).
`PartDScraper._RENAMES_FROM_V10` aliases the V10 headers at parse time
so every year materializes at the canonical 91-column shape. When CMS
publishes V20 D23 (expected late 2026), the V10 aliases stop firing in
practice but stay in the code as a safety net.

CMS's strict `_validate()` will still reject any parquet with unknown
columns — intentional, so upstream drift surfaces at the producer
instead of as a silent `KeyError` deep in the ranker.

Row counts grow steadily as Medicare Part D enrollment grows: ~1.05 M
prescribers in 2013 → ~1.38 M in 2023.

### 6a. Dtype contract (Pandera) and observed per-year dtype drift

As of 2026-06 the schema is a Pandera `DataFrameModel`
([`pipeline/base.py`](../../pipeline/base.py)). `_validate()` now does
three things in one pass: **coerce** each column to its declared dtype,
**reject** undeclared columns (strict), and enforce **nullability + value
checks**. So column *dtype* drift — not just column presence — fails at
the producer instead of shipping to R2.

This matters because the historical R2 parquets carried real per-year
dtype drift (a `DOUBLE`-typed NPI silently breaks an exact string match —
the bug that motivated this contract). The **2024 program-year release**
drove a large dtype flip versus 2013–2023, touching ~60 columns. Observed
drift on `r2:needlepoint-data/processed/v1/cms_partd_*.parquet`, grouped:

| Column(s) | R2 drift (pre-backfill) | Schema target | Cast |
|---|---|---|---|
| Suppression flags (`*_Sprsn_Flag` / `*_Suprsn_Flag`: `GE65_`, `Brnd_`, `Gnrc_`, `Othr_`, `MAPD_`, `PDP_`, `LIS_`, `NonLIS_`, `Antpsyct_GE65_`, `GE65_Bene_`) | VARCHAR (2013–23) → **INTEGER** (2024) | `str` | plain `→ VARCHAR` (INTEGER→VARCHAR is clean, no `.0` risk) |
| Provider text cols (`Prscrbr_MI`, `Prscrbr_Crdntls`, `Prscrbr_Ent_Cd`, `Prscrbr_St1`, `Prscrbr_St2`, `Prscrbr_RUCA_Desc`, `Prscrbr_Cntry`) | VARCHAR (2013–23) → **INTEGER** (2024) | `str` | plain `→ VARCHAR` |
| Claim counts / costs / bene counts (`*_Tot_Clms`, `*_Tot_Drug_Cst`, `*_Drug_Cst`, `Opioid_*`/`Antbtc_*`/`Antpsyct_*` totals, all `Bene_*_Cnt`, `Bene_Avg_Age`, `Bene_Avg_Risk_Scre`) | DOUBLE (2013–23) → **BIGINT/INTEGER** (2024) | `float` (or `int` for counts) | plain numeric `CAST` |
| `Prscrbr_State_FIPS`, `Prscrbr_zip5` | DOUBLE (2013–23) | **`str`** | **RE-INGEST** (not castable — see correction) |
| `Prscrbr_RUCA` | DOUBLE | `float` | plain `CAST` (genuinely decimal — stays float) |
| `Tot_Benes` | partd R2 stores `DOUBLE` | `Int64` (declaration corrected float→`Int64` to match `cms_partb` / `cms_partb_svc`) | plain `→ Int64` |

⚠️ **Correction (2026-06).** Two claims in an earlier version of this
section were wrong:

1. **`Prscrbr_State_FIPS` / `Prscrbr_zip5` are `str`, not "numeric", and are
   NOT castable.** The schema declares them `str`, and they are in
   `recast_r2_dtypes.REINGEST_ONLY`. The DOUBLE years (2013–23) already lost
   the ZIP leading zeros (`"02134"`→`2134.0`, ~9.5% of rows) and the FIPS
   leading zeros + `9A`–`9E` codes — no cast recovers it; they must be
   **re-ingested**. Going forward the scraper reads both with
   `dtype="string"` so new writes preserve them. (`Prscrbr_RUCA` is the only
   genuinely-numeric one of the three — it stays `float`.)
2. **The "2024 → INTEGER/VARCHAR dtype flip" rows above describe a *stale
   parquet*, not real CMS behavior.** The 2024 panel on R2 was built from the
   *retracted* Feb-2024 release: a coherent block (`Prscrbr_zip5`,
   `Prscrbr_MI`, `Prscrbr_Crdntls`, `Prscrbr_RUCA`, `Bene_Avg_Age`, all
   suppression flags) is entirely NULL on R2, yet the **current** RY26 source
   CSV has them fully populated under their exact canonical names (headers
   map 1:1; `_RENAMES_FROM_V10` needs no extension). Fix = clear the cached
   `cms_partd_2024.csv` and re-ingest 2024 from the current URL. This is a
   stale-build/`_download`-cache issue, not a dtype or rename problem.

**NPI handling.** The canonical lowercase `npi` column is clean across
every year (it was built `int64 → str`) — **always join on `npi`**, never
the raw prescriber column. The source's raw NPI column (`Prscrbr_NPI`) is
declared `str` with the `^(\d{10})?$` check; that check rejects a
stray-`.0` float NPI at write-time.

**Backfilling the historical parquets** to this contract:
[`pipeline/scripts/recast_r2_dtypes.py`](../../pipeline/scripts/recast_r2_dtypes.py)
derives the per-column cast from this schema (int-hop applied
automatically for any float→string target, none in partd), rewrites R2 in
place via DuckDB, and re-verifies. `--execute` to swap; default is a
dry-run.

## 7. Known limitations

- **Aggregate only.** No per-NDC, per-pharmacy, or per-claim detail.
  Drug-specific fraud (which exact opioid? which brand pharma paid for?)
  requires the by-Provider-and-Drug PUF, which isn't scraped yet.
- **Suppression-floor invisibility.** Prescribers with `< 11` unique
  beneficiaries don't make the file. The fraud population of interest
  all clears this floor; the floor blinds the data to micro-scale
  prescribers (not a real loss for fraud detection).
- **One column has a typo in the upstream schema.**
  `Antpsyct_GE65_Bene_Suprsn_Flag` uses "Suprsn" while every other
  suppression flag uses "Sprsn". The typo is preserved in our schema
  to match the published CMS header — don't "fix" it without also
  re-parsing the CSV.
- **Pharmacy is invisible.** The prescriber→pharmacy edge that
  encodes most kickback patterns isn't reconstructable from this
  alone. Pair with NPPES + DEA registration data if you need to
  surface that.
- **"Type" is self-declared and lossy.** `Prscrbr_Type` distinguishes
  Nurse Practitioners as a single bucket regardless of clinical focus;
  pharmacists, dentists, optometrists all appear too. For taxonomic
  specialty, prefer NPPES.
- **No organizations to speak of.** 2023 has 1,380,664 individual NPIs
  and 1 organization. Part D scripts are nearly always written by an
  individual prescriber; the org column is effectively dead.

## 8. File contract

After running [`PartDScraper`](../../pipeline/scrapers/cms_partd.py):

```
data/panels/cms_partd_YYYY.parquet              # 91 cols, ~1M–1.38M rows
data/panels/cms_partd_YYYY.manifest.json        # RunResult: source, schema_version, columns, fetch URL
```

Manifest fields are documented on
[`pipeline.base.RunResult`](../../pipeline/base.py). Loaders read the
manifest at load time, compare `schema_version`, and warn on mismatch.
