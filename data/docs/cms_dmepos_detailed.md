# CMS DMEPOS — by Referring Provider & Service (per-HCPCS line items)

**Scraper:** not included in this repository; the data ships in the public dataset.
**Schema reference (columns + dtypes):** [docs/DATA_SCHEMAS.md §2.6](../../docs/DATA_SCHEMAS.md)
**Files:** `data/panels/cms_dmepos_detailed_YYYY.parquet` + `cms_dmepos_detailed_YYYY.manifest.json`

> All 10 years (2014–2023) are built
> and in R2 (`r2:needlepoint-data/processed/v1/cms_dmepos_detailed_*.parquet`; pull
> with `rclone copy r2:needlepoint-data/processed/v1/ data/panels/ --include
> 'cms_dmepos_detailed_*.parquet'`, or re-fetch via `python -m
> pipeline.scripts.reingest_r2 --source cms_dmepos_detailed --year <Y> --execute`).
> **Per-NPI features:** `scripts/compute_dmepos_detailed_rich_features.py` →
> `dmepos_detailed_rich_<year>.parquet` (in R2 `cache/v1/feature_frames/`).
> **Investigator tool:** `scripts/dump_dme_breakdown.py --npi <N> --years <…>`
> (wired into the investigator prompt + the calibration export allowlist).

> ⚠️ **Multi-row per NPI.** Like `cms_partb_svc` (and unlike the
> aggregated `cms_dmepos`), this source is **not** one row per NPI — each
> row is one (Referring NPI × HCPCS_CD × Suplr_Rentl_Ind) tuple. A typical
> referrer repeats over a handful to a few dozen rows. `ProviderRecordSchema.npi`
> is declared `str` and downstream consumers tend to treat it as a row
> key; that assumption is invalid here. Aggregate to NPI before joining to
> NPI-keyed sources. See §2 for the precise grain. The scraper still
> populates the canonical `npi` / `provider_name` / `state` / `data_year`
> columns on every row, but they are denormalized repeats, not unique keys.

## 1. What this dataset is, in plain English

**DMEPOS** = **Durable Medical Equipment, Prosthetics, Orthotics, and
Supplies** — the physical products Medicare pays for that a patient takes
home or wears (wheelchairs, hospital beds, oxygen, CPAP, glucose monitors,
back/knee braces, prosthetic limbs, ostomy supplies, inhalation drugs), as
opposed to services rendered in a clinic. The supply chain has three
parties:

```
   referring physician  ──signs the order──▶   supplier   ──bills HCPCS X──▶   Medicare
        (Rfrg_NPI)                          (DME company)        (HCPCS_CD)
```

This is the **per-line-item cut** of the same publication that
`cms_dmepos` aggregates by referring provider. Where `cms_dmepos` tells
you "Dr. X had $612k of DMEPOS referrals this year, $580k of it durable
equipment", this PUF tells you **which HCPCS codes** Dr. X referred — 800
K0001 standard wheelchairs, 90 L0650 back braces, 50 A4253 glucose-strip
orders — and the per-code counts and per-service averages.

That HCPCS-level breakout is the whole point for DME fraud. A brace-mill
or wheelchair-mill scheme is a *distribution* over a handful of high-margin
codes; the aggregated cut sums across every code the referrer touched and
hides the fingerprint. Here each (HCPCS, rental) combination is its own row
and the distribution itself is the signal (see §4).

### Acronyms used in column names

| Acronym | Meaning |
|---|---|
| `Rfrg` | Referring (the physician who signed the order) |
| `Suplr` | Supplier (the DME company billing the claim) |
| `HCPCS` | Healthcare Common Procedure Coding System — the 5-char code for every billable item. For DMEPOS the meaningful prefixes are **A** (medical/surgical supplies — e.g. A4253 glucose strips), **E** (durable equipment — E0260 hospital bed, E1390 oxygen concentrator), **K** (DMERC temporary — K0001 standard wheelchair, K0823 power wheelchair), **L** (orthotics/prosthetics — L0650 lumbar-sacral orthosis "back brace") |
| `HCPCS_CD` | The specific HCPCS code referred on this row (note: upper-case `CD`, unlike `cms_partb_svc`'s `HCPCS_Cd`) |
| `HCPCS_Desc` | Plain-text description of the HCPCS code (denormalized; drifts across vintages — join on the code, not the text) |
| `Suplr_Rentl_Ind` | Rental indicator — `"Y"` the item was rented (capped monthly rental, e.g. oxygen, CPAP, hospital beds), `"N"` purchased. **Part of the row grain**: a code billed both ways produces two rows for one referrer |
| `RBCS` | Restructured BETOS Classification System — CMS's service-category taxonomy that **replaced** the legacy BETOS codes. `RBCS_Id` / `RBCS_Desc` / `RBCS_Lvl` group DMEPOS into families (DME, supplies, orthotics, …). There is **no `BETOS_Cd` column** |
| `Tot_Suplrs` | Distinct suppliers that billed on this referrer's orders for this (HCPCS, rental) |
| `Tot_Suplr_Benes` | Unique beneficiaries for this (HCPCS, rental) line |
| `Tot_Suplr_Clms` | Total supplier claims for this line |
| `Tot_Suplr_Srvcs` | Total services/units (a rented item billed monthly counts each month; multi-unit supplies count each unit) |
| `Avg_Suplr_Sbmtd_Chrg` | Per-service average of what the supplier billed |
| `Avg_Suplr_Mdcr_Alowd_Amt` | Per-service average of what Medicare allowed under the fee schedule |
| `Avg_Suplr_Mdcr_Pymt_Amt` | Per-service average of what Medicare actually paid |
| `Avg_Suplr_Mdcr_Stdzd_Amt` | Per-service average payment with geographic wage-index removed (use for cross-region comparison) |
| `Mdcr` / `Sbmtd` / `Alowd` / `Pymt` / `Stdzd` | Medicare / Submitted / Allowed / Payment / Standardized |
| `Ent_Cd` | Entity code — `I` (individual) or `O` (organization) |
| `RUCA` | Rural-Urban Commuting Area — geographic classification code |
| `Spclty` | Specialty (self-declared) |

**Dollar columns are averages, not totals.** To compute total Medicare
paid for a given (NPI, HCPCS, rental) row, multiply
`Avg_Suplr_Mdcr_Pymt_Amt * Tot_Suplr_Srvcs`. Same gotcha as
`cms_partb_svc` — downstream feature code should derive `_tot_paid` this
way rather than reading a total that doesn't exist.

## 2. Unit of observation

**One row = one (Referring NPI × HCPCS_CD × Suplr_Rentl_Ind) tuple in a
calendar year.** Aggregated across every supplier and beneficiary that fit
the tuple.

A referrer who orders 20 distinct HCPCS codes, a few of them billed both
rented and purchased, produces ~25 rows. A genuine brace mill that pumps a
narrow set of orthotic codes may produce only a handful of (very large)
rows — the *concentration* is the tell. The population is far smaller than
`cms_partb_svc` (DME referrers are a subset of all billing providers, and
each touches far fewer codes than a busy physician's full HCPCS list).

The 33 raw CMS columns break into:

- **19 cols of referring-provider identity** (`Rfrg_*`) — NPI, name,
  address, RUCA, specialty — **denormalized** onto every row of a given NPI
- **3 cols of RBCS taxonomy** (`RBCS_Lvl`, `RBCS_Id`, `RBCS_Desc`)
- **3 cols of HCPCS / rental identifiers** (`HCPCS_CD`, `HCPCS_Desc`,
  `Suplr_Rentl_Ind`)
- **8 cols of per-(NPI, HCPCS, rental) aggregates** — 4 counts (`Tot_Suplrs`,
  `Tot_Suplr_Benes`, `Tot_Suplr_Clms`, `Tot_Suplr_Srvcs`) + 4 dollar
  averages (`Avg_Suplr_Sbmtd_Chrg`, `Avg_Suplr_Mdcr_Alowd_Amt`,
  `Avg_Suplr_Mdcr_Pymt_Amt`, `Avg_Suplr_Mdcr_Stdzd_Amt`)

After our scrape pass, parquets carry 7 additional canonical columns: 3
base stamps (`source`, `fetched_at_utc`, `schema_version`) + 4 NPI-keyed
canonicals (`npi`, `provider_name`, `state`, `data_year`) — see
[`pipeline.base.ProviderRecordSchema`](../../pipeline/base.py).

Total declared schema: **40 columns.**

The scraper builds the `provider_name` map once per unique NPI and
broadcasts via `.map()` rather than calling `format_provider_name` per row
— the same denormalization approach `cms_partb_svc` uses, for the same
reason (avoid a per-row format call at multi-million-row scale).

## 3. How CMS publishes it (cadence + suppression)

- **Annual release.** Each PUF covers one calendar year.
- **Lag.** ~18 months from end-of-year to release. All ten catalogued
  years (2014–2023) were published under the **RY25** release: data year
  2023 at V10 (June 2025), 2014–2022 republished at V20 (November 2025).
  The URL catalog lives in
  `pipeline/scrapers/cms_dmepos_detailed.py:URLS`;
  expect URLs to rotate per RY release (re-resolve from the data.json
  catalog, dataset id `86b4807a-d63a-44be-bfdf-ffd398d5e623`).
- **Suppression rules — applied upstream by CMS, not by our code.** CMS
  redacts (blanks) any element representing **fewer than 11 claims** to
  protect beneficiary privacy. Our scraper does no row-level filtering;
  a suppressed count/amount arrives **null**, which is why every count and
  dollar column is declared `nullable=True`. The schema's `ge=0` value
  checks still hold on the non-null cells.
- **HCPCS descriptions are versioned.** The same `HCPCS_CD` can carry
  different `HCPCS_Desc` strings across vintages — always join on the code.
- **2013 was never published** for the referring-provider series (same as
  the aggregated `cms_dmepos`). Coverage is 2014–2023.

## 4. Fraud signals — what to look for

DMEPOS supports some of the highest-dollar Medicare fraud schemes because
(a) the referring physician can be miles from the supplier (rented-NPI
schemes) and (b) volume per referrer is bounded by signature throughput,
not clinical capacity. The aggregated `cms_dmepos` surfaces *that* a
referrer is high-volume; this file surfaces *which codes* — which is what
turns a high-volume flag into a specific, chargeable scheme hypothesis.

### 4a. Brace mills (the canonical scheme)

A "telemedicine" company gets beneficiaries on a 60-second call with a
contracted physician, produces a "visit" that justifies an orthotic-brace
order, and the supplier bills Medicare ~$700–$1,500 per brace and kicks
back a per-order fee. The aggregated cut shows it as high `POS_*` payment;
**this file shows the actual orthotic L-codes**.

**Column signatures:**
- `Tot_Suplr_Srvcs` and `Avg_Suplr_Mdcr_Pymt_Amt × Tot_Suplr_Srvcs`
  concentrated in a small set of **L-codes** (e.g. L0650 lumbar-sacral
  orthosis, L1833 knee orthosis, L3960 shoulder orthosis)
- A handful of distinct HCPCS codes carrying nearly all the referrer's
  DMEPOS dollars (low HCPCS diversity, high per-code concentration)
- Specialty mismatch: `Rfrg_Prvdr_Spclty_Desc` is "Family Practice",
  "Internal Medicine", "Emergency Medicine", or "Hospitalist" rather than
  "Orthopedic Surgery" / "Physical Medicine"

Canonical case: **"Operation Brace Yourself" (2019)** — DOJ indicted 24
defendants across an alleged $1.2B brace-mill conspiracy; several are in
`gold_set.jsonl`. Their fingerprint here is extreme service counts on a few
L-codes from a non-orthopedic referrer.

### 4b. Power-wheelchair / high-cost DME schemes

Billing motorized wheelchairs (~$5k–$10k) for patients who don't need one.
Historically clustered (Miami, Houston, Brooklyn).

**Column signatures:**
- High `Tot_Suplr_Srvcs` on **K-codes** — K0823 (power wheelchair, Group 2
  standard) or K0001 (standard wheelchair) at implausible volume for one
  referrer (the ticket's "10,000 K0001" example)
- High `Avg_Suplr_Mdcr_Pymt_Amt` per `Tot_Suplr_Benes` on those codes
- Single-supplier dependency: `Tot_Suplrs ≈ 1–2` on a high-payment code
  suggests a captive supplier relationship
- ZIP-code clustering of referrers in known hotspots (`Rfrg_Prvdr_Zip5`)

### 4c. Rental-cycle abuse

Capped-rental items (oxygen concentrators E1390, CPAP E0601, hospital beds
E0260) bill monthly. Schemes keep billing rentals after the item was
returned, the patient died, or the 13-month cap should have converted to
purchase.

**Column signatures:**
- `Suplr_Rentl_Ind == "Y"` rows where `Tot_Suplr_Srvcs / Tot_Suplr_Benes`
  (months-per-bene) sits well above the cap for that code
- High `Tot_Suplr_Clms / Tot_Suplr_Benes` on rented E-codes (repeated
  monthly rebills per patient)

### 4d. Specialty / credential mismatch

A Hospitalist (inpatient-only, no continuity relationship) as a top DMEPOS
referrer is structurally implausible — discharged patients are referred to
community DME by their PCP, not the hospitalist. Here you can see *what*
they referred, sharpening the implausibility.

**Column signatures:**
- `Rfrg_Prvdr_Spclty_Desc IN ("Hospitalist","Emergency Medicine","Anesthesiology")`
  AND high total paid concentrated in mill-typical K/L codes
- Cross-source: NPPES credential/taxonomy disagrees with the code mix

## 5. Joins to other sources

| Join target | Key | Why |
|---|---|---|
| `cms_dmepos_YYYY.parquet` | `npi` | The aggregated cut of **this same source**. Reconcile: summing `Avg_Suplr_Mdcr_Pymt_Amt × Tot_Suplr_Srvcs` over a referrer's rows here should approximate `Suplr_Mdcr_Pymt_Amt` in the aggregate (CMS suppression on either side introduces drift) |
| `cms_partb_YYYY.parquet` | `npi` | Referrer's own clinical activity. $2M in DMEPOS referrals but ~$80k of own Part B is mismatched |
| `cms_partb_svc_YYYY.parquet` | `npi` | Referrer's own per-HCPCS billing (the analogous line-item cut on the physician-services side) |
| `nppes_features_*.parquet` | `npi` | Specialty/taxonomy ground-truth for the §4d mismatch |
| `gold_set.jsonl` | `npi` | Known fraud cases for label join |

Pitfalls when joining:

- **Aggregate to NPI before joining to NPI-keyed sources.** Joining the raw
  multi-row frame to a per-NPI source creates a Cartesian-style explosion.
  `df.groupby("npi").agg(...)` first.
- **Referrer ≠ supplier.** This dataset is referrer-keyed; the supplier
  (the kickback partner) has its own NPI not in this PUF.
- **NPI must be string-zero-padded.** Native `Rfrg_NPI` is numeric-looking;
  use the canonical `npi` column (`str`, zero-padded to 10).
- **HCPCS description drift.** Join on `HCPCS_CD`, not `HCPCS_Desc`.
- **`Suplr_Rentl_Ind` is part of the grain.** A code billed both rented and
  purchased is two rows; sum across the rental indicator for a code-total.

## 6. Year coverage and schema drift

| Years | Columns | Notes |
|---|---|---|
| 2013 | n/a | Never published by CMS for the referring-provider series |
| 2014–2023 | 33 native | All published under the RY25 release; homogeneous 33-column header (2023 at V10, 2014–2022 at V20) |

All ten years ship under the single RY25 release, so the column set is
expected to be stable. The scraper still calls `BaseScraper._align_to_schema`
defensively (NaN-pads any declared column an older year happens to drop),
and the strict `_validate()` rejects any *unknown* column — surfacing
upstream drift at the producer rather than as a silent `KeyError` in the
ranker.

### 6a. Dtype contract (Pandera) and zero-padded codes

The schema is a Pandera `DataFrameModel` ([`pipeline/base.py`](../../pipeline/base.py)).
`_validate()` coerces each column to its declared dtype, rejects undeclared
columns (strict), and enforces nullability + `ge=0` value checks.

The scraper reads every schema-declared string column with `dtype="string"`
(via `BaseScraper.string_read_dtypes`), so pandas can't numericize a
zero-padded / alphanumeric code (`Rfrg_Prvdr_State_FIPS` `"06"`,
`Rfrg_Prvdr_Zip5` `"02134"`) before validation — the loss no later cast can
undo. `Rfrg_NPI` is declared `str` with the `^(\d{10})?$` check, so a
DOUBLE-derived `.0`-suffixed NPI fails loudly at write-time.

**NPI handling.** The canonical lowercase `npi` column is clean across every
year (built `int64 → str`, zero-padded to 10) — **always join on `npi`,
never the raw `Rfrg_NPI`** (see §5).

## 7. Known limitations

- **No per-supplier, no per-patient detail.** A row aggregates across the
  (NPI, HCPCS, rental) tuple. You can't see which supplier billed the most
  on a referrer's orders, or which patients.
- **`Rfrg_*` is the referrer, not necessarily the treating physician.**
  Brace/wheelchair mills specifically exploit this — the referrer never saw
  the patient.
- **State is the referrer's billing address, not the beneficiary's.**
  Cross-state schemes (referrer in FL, patients nationwide) appear as a
  single-state row.
- **Suppression-floor invisibility at the line level.** (NPI, HCPCS, rental)
  elements below the 11-claim floor are nulled — a code referred only a
  handful of times is invisible. Mill schemes bill at scale, so this is
  rarely a real loss, but a referrer's "true" HCPCS diversity is biased.
- **Dollar columns are averages.** For totals, multiply by `Tot_Suplr_Srvcs`.
- **Disk / memory pressure.** Multi-GB CSV per year; run the backfill on a
  CI runner one year at a time, not on the dev box.

## 8. File contract

After running `DMEPOSDetailedScraper`:

```
data/panels/cms_dmepos_detailed_YYYY.parquet        # 40 cols, multi-row per NPI
data/panels/cms_dmepos_detailed_YYYY.manifest.json  # RunResult: source, schema_version, columns, fetch URL
```

Manifest fields are documented on
[`pipeline.base.RunResult`](../../pipeline/base.py). Loaders read the
manifest at load time, compare `schema_version`, and warn on mismatch.

**Note on the aggregated-DMEPOS glob:**
`pipeline/scripts/backfill_cms_dmepos_manifest.py`
matches `cms_dmepos_[0-9][0-9][0-9][0-9].parquet` (four-digit year anchor)
specifically so these `cms_dmepos_detailed_*` files are **not** swept into
the aggregated-panel manifest stamper — the detailed scraper writes its own
manifest.
