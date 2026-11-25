# CMS DMEPOS — by Referring Provider

**Scraper:** [`pipeline/scrapers/cms_dmepos.py`](../../pipeline/scrapers/cms_dmepos.py)
**Schema reference (columns + dtypes):** [docs/DATA_SCHEMAS.md §2.3](../../docs/DATA_SCHEMAS.md)
**Files:** `data/panels/cms_dmepos_YYYY.parquet` + `cms_dmepos_YYYY.manifest.json`

## 1. What this dataset is, in plain English

**DMEPOS** stands for **Durable Medical Equipment, Prosthetics, Orthotics,
and Supplies** — the catch-all CMS category for physical products
Medicare pays for that a patient takes home or wears, as opposed to
services rendered in a clinic. Think: wheelchairs, hospital beds, oxygen
concentrators, CPAP machines, blood-glucose monitors, back braces, knee
braces, prosthetic limbs, ostomy supplies, inhalation drugs.

The supply chain has three parties:

```
   referring physician  ──signs the order──▶   supplier   ──bills──▶   Medicare
        (Rfrg_NPI)                          (DME company)
```

The **supplier** is the company that delivers the brace or wheelchair
and submits the claim. The **referring physician** is the doctor whose
NPI appears on the supplier's claim as having "ordered" the item. Medicare
pays the supplier, but the physician's signature on the order is what
makes the claim payable.

This PUF aggregates by **referring physician**, *not* by supplier. One
row = one physician's full year of DMEPOS referral activity, summed
across every supplier who billed on their orders. This framing is the
whole point of the dataset for fraud detection (see §4).

### Acronyms used in column names

| Acronym | Meaning |
|---|---|
| `DME` | Durable Medical Equipment — wheelchairs, hospital beds, oxygen, CPAP, glucose monitors |
| `POS` | Prosthetics, Orthotics, Supplies — braces, prosthetic limbs, ostomy. **Not** "Place of Service" |
| `Drug` | Drug-DMEPOS — inhalation drugs, infusion drugs administered with DME. **Not** Part D prescriptions |
| `Suplr` | Supplier (the DME company billing the claim) |
| `Rfrg` | Referring (the physician who signed the order) |
| `Mdcr` | Medicare |
| `Sbmtd` | Submitted (what the supplier billed) |
| `Alowd` | Allowed (what Medicare's fee schedule says is reasonable) |
| `Pymt` | Payment (what Medicare actually paid after deductible/coinsurance) |
| `Stdzd` | Standardized — payment with geographic wage-index adjustment removed |
| `Sprsn_Ind` | Suppression indicator — `*` flag when the cell was dropped for low N |
| `CC` | Chronic Condition — the `Bene_CC_*` block tags beneficiary prevalence rates |
| `RUCA` | Rural-Urban Commuting Area — geographic classification code |
| `Spclty` | Specialty |
| `Crdntls` | Credentials (MD, DO, NP, PA, …) |
| `Ent_Cd` | Entity code — `I` (individual) or `O` (organization) |
| `Sbmtd_Chrgs` | Submitted charges |
| `Bene` | Beneficiary |

## 2. Unit of observation

**One row = one referring NPI × one calendar year.** Aggregated across
all suppliers and all HCPCS codes those suppliers billed on referrals
from that physician.

This is *not* a claim-level dataset. There is no per-supplier breakdown,
no per-HCPCS detail, and no per-patient row. You can't see "which
supplier billed the most on Dr. X's referrals" from this PUF — only the
totals.

The 97 columns break into:

- 17 cols of physician identity (`Rfrg_*`) — NPI, name, address, specialty
- 9 cols of total supplier activity (`Tot_*`, `Suplr_*`)
- 10 cols × 3 subcategories (`DME_*`, `POS_*`, `Drug_*`) = 30 cols of
  service-class splits — same nine aggregate metrics, suppression-flagged
- 15 cols of beneficiary demographics (age buckets, sex, race, dual-eligible)
- 25 cols of beneficiary chronic-condition prevalence (`Bene_CC_*`) —
  *only present for 2017+ (see §6)*
- 1 col average risk score

After our scrape pass, parquets carry 7 additional canonical columns: 3
base stamps (`source`, `fetched_at_utc`, `schema_version`) + 4 NPI-keyed
canonicals (`npi`, `provider_name`, `state`, `data_year`) — see
[`pipeline.base.ProviderRecordSchema`](../../pipeline/base.py).

## 3. How CMS publishes it (cadence + suppression)

- **Annual release.** Each PUF covers one calendar year.
- **Lag.** ~18 months from end-of-year to release. Data year 2023 was
  published in June 2025 under the RY25 V10 release; 2022 was
  republished in November 2025 under RY25 V20 alongside earlier years.
- **CMS reissues older years under newer release versions** when they
  refresh the standardized-payment methodology or add columns. The URL
  catalog in [`pipeline/scrapers/download.py`](../../pipeline/scrapers/download.py)
  pins one URL per year; expect URLs to rotate per RY release.
- **Suppression rules — applied upstream by CMS, not by our code.**
  Our scraper does no row-level filtering; what's in the parquet is what
  CMS published. Two suppression layers:
  - **Cell-level numeric suppression.** When a count would be `< 11`,
    CMS nulls the cell (the row stays in the file). Empirically on 2023:
    `Tot_Suplr_Benes` has no values below 11 (the floor is hard), but
    **174,447 of 387,352 rows (45%) have a null `Tot_Suplr_Benes`**
    because the total fell under the floor and was suppressed. The row
    survives for the provider's identity columns and any per-category
    aggregates that *did* clear the floor.
  - **Category-level flags `*_Sprsn_Ind`.** The columns
    `DME_Sprsn_Ind`, `POS_Sprsn_Ind`, `Drug_Sprsn_Ind` carry two
    suppression codes; both mean "no published data" and the
    corresponding count and dollar columns are nulled:
    - `*` — cell `n < 11`, redacted for privacy
    - `#` — value was negative or unreliable, redacted for data quality
  - **What this means for fraud detection.** The dataset is naturally
    tuned to **high-volume schemes** — small operators are largely
    invisible. The *presence* of suppression on a category for an
    otherwise high-volume referrer is itself a feature: it means that
    category was small for them, which can disambiguate "pure DME mill"
    from "general DMEPOS referrer with some DME". The existing rich
    features in `scripts/compute_dmepos_rich_features.py` lift the flags
    into binary signals on that logic.
- **2013 was never published** for the referring-provider cut. Coverage
  is 2014–2023.

## 4. Fraud signals — what to look for

This is the dataset's load-bearing role in this project. DMEPOS supports
some of the highest-dollar Medicare fraud schemes because (a) the referring
physician can be miles from the supplier, enabling rented-NPI schemes,
and (b) volume per referrer is bounded mostly by signature throughput,
not clinical capacity. The agent's "DMEPOS playbook" should look for
these patterns:

### 4a. Brace mills (the canonical scheme)

A "telemedicine" company contacts Medicare beneficiaries via call centers,
gets them on a 60-second call with a contracted physician, and produces
a "telehealth visit" that justifies an order for one or more orthotic
braces (back, knee, shoulder). The supplier ships the brace, bills
Medicare ~$700–$1,500 per item, and kicks back a per-order fee to the
physician.

**Column signatures:**
- High `POS_Suplr_Mdcr_Pymt_Amt` (often >$1M for a single referrer/year)
- High `POS_Tot_Suplr_Benes` (hundreds to thousands of unique beneficiaries)
- `POS_*` payments dominate `DME_*` and `Drug_*` — i.e.
  `POS_Suplr_Mdcr_Pymt_Amt / Suplr_Mdcr_Pymt_Amt > 0.8`
- Geographic mismatch: referrer in one state, beneficiaries spread across
  many (visible after joining `Bene_*` cols with beneficiary state via
  a separate join, since this PUF only has the referrer's state)
- Specialty mismatch: `Rfrg_Prvdr_Spclty_Desc` is "Family Practice",
  "Internal Medicine", "Emergency Medicine", or "Hospitalist" rather than
  "Orthopedic Surgery" or "Physical Medicine" — orthopedics referrers
  with POS-dominant billing aren't necessarily suspicious, but family
  medicine ones often are

Canonical case: **"Operation Brace Yourself" (2019)** — DOJ indicted 24
defendants across an alleged $1.2B brace-mill conspiracy. Several of the
defendants are in `gold_set.jsonl`.

### 4b. Power-wheelchair / high-cost DME schemes

Suppliers bill for motorized wheelchairs (~$5k–$10k each) on patients
who don't medically need one. Often clustered geographically (historically
Miami, Houston, Brooklyn).

**Column signatures:**
- High `DME_Suplr_Mdcr_Pymt_Amt` per `DME_Tot_Suplr_Benes` (high payment
  per beneficiary)
- Single-supplier dependency: `Tot_Suplrs` ≈ 1–2 for a high-payment
  referrer suggests a captive supplier relationship
- ZIP-code clustering of referrers in known hotspots (`Rfrg_Prvdr_Zip5`)

### 4c. Ghost / dead-patient bills

Supplier bills for equipment that was never delivered, or delivered to
patients who had already died. Not visible directly from PUF aggregates
(no patient-level detail) but the referring physician shows up as
high-volume.

**Column signatures (proxy):**
- Very high `Tot_Suplr_Clms / Tot_Suplr_Benes` ratio (many claims per
  patient — supplier rebilling)
- `Suplr_Sbmtd_Chrgs / Suplr_Mdcr_Pymt_Amt` ratio at extreme ends (gaming
  the fee schedule or fully reimbursed at the cap)

### 4d. Specialty/credential mismatch

The PUF includes `Rfrg_Prvdr_Spclty_Desc` and `Rfrg_Prvdr_Crdntls`. A
Hospitalist (inpatient-only care, no continuity relationship with the
patient) being a top-100 DMEPOS referrer is structurally implausible — a
patient discharged from the hospital is referred to community DME by
their PCP, not the hospitalist who saw them for three days.

**Column signatures:**
- `Rfrg_Prvdr_Spclty_Desc IN ("Hospitalist", "Emergency Medicine",
  "Anesthesiology")` AND high `Suplr_Mdcr_Pymt_Amt`
- Cross-source: NPPES credential disagrees with what's claimed here

### 4e. Locality gaming

Standardized payment (`Suplr_Mdcr_Stdzd_Pymt_Amt`) is the wage-index-
removed version of payment. A large gap between standardized and
non-standardized payment can indicate that a supplier is billing through
a high-cost-locality address while serving patients in a low-cost
locality.

**Column signature:** `Suplr_Mdcr_Pymt_Amt / Suplr_Mdcr_Stdzd_Pymt_Amt`
significantly off 1.0 for a non-standardized adjustment factor expected
in that zip.

## 5. Joins to other sources

| Join target | Key | Why |
|---|---|---|
| `cms_partb_YYYY.parquet` | `npi` | Referrer's own clinical activity — E&M visits, procedures. If a doc has $2M in DMEPOS referrals but ~$80k in their own Part B activity, that's mismatched |
| `cms_partd_YYYY.parquet` | `npi` | Same referrer's prescription pattern. Cross-checks specialty plausibility (does the prescribing match the DMEPOS mix?) |
| `nppes_features_*.parquet` | `npi` | Specialty/taxonomy ground-truth, credential, enumeration date. Catches mismatch between what CMS PUF lists vs. what NPPES has |
| `leie.parquet` (legacy) | `npi` | OIG exclusion already on record — eliminates the obvious false negatives |
| `gold_set.jsonl` | `npi` | Known fraud cases for label join |

Pitfalls when joining:

- **Referrer ≠ supplier.** This dataset is referrer-keyed. The supplier
  is invisible. Don't assume a high-DMEPOS-referrer is in business with
  themselves — the kickback partner is the supplier company, which has
  its own NPI not in this PUF.
- **NPI must be string-zero-padded.** Native dtype is `int64`. After our
  scrape pass, the canonical `npi` column is `str` zero-padded to 10;
  use that, not `Rfrg_NPI`.
- **`POS` ≠ Place of Service.** In any cross-reference with Part B
  claim data, "POS" means place-of-service. In this PUF, `POS_*` columns
  mean Prosthetics/Orthotics/Supplies.

## 6. Year coverage and schema drift

| Years | Columns | Notes |
|---|---|---|
| 2013 | n/a | Never published by CMS for the referring-provider cut |
| 2014–2016 | 79 native | No `Bene_CC_*` chronic-condition block |
| 2017–2023 | 104 native | Full schema; CC columns ~33–43% nonnull (rest cell-suppressed) |

The pre-2017 years have 25 fewer columns. Our scraper handles this via
`BaseScraper._align_to_schema(df)`, which NaN-pads the missing
`Bene_CC_*` columns so every year materializes with the same 104
columns. Consumers can detect "this is a pre-CC year" by checking
`df["Bene_CC_PH_Diabetes_V2_Pct"].notna().any()`.

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
the producer instead of shipping to R2.

This matters because the historical R2 parquets carried real per-year
dtype drift. Observed drift on
`r2:needlepoint-data/processed/v1/cms_dmepos_*.parquet`:

| Column | R2 drift (pre-backfill) | Schema target | Cast |
|---|---|---|---|
| `Bene_CC_*_Pct` (24 chronic-condition prevalence cols) | INTEGER (2014–2016) → **DOUBLE** (2017–2023) | `float` ≥ 0 | plain `→ DOUBLE` |
| `Rfrg_NPI` | — (declared) | `str` `^(\d{10})?$` | (no observed drift; declared as string) |

No int-hop columns: every `Bene_CC_*_Pct` column has a float schema
target, so a plain `CAST(... AS DOUBLE)` is sufficient. The int-hop
`CAST(CAST(x AS BIGINT) AS VARCHAR)` cast is only needed for float-source
→ string-target columns, which this source does not have.

**NPI handling.** The canonical lowercase `npi` column is clean across
every year (built `int64 → str`, zero-padded to 10) — **always join on
`npi`, never the raw `Rfrg_NPI`** (see §5). `Rfrg_NPI` is declared `str`
with the `^(\d{10})?$` check, which would reject any `DOUBLE`-derived
`.0`-suffixed text at write-time.

**Zero-padded code columns.** `Rfrg_Prvdr_State_FIPS` and `Rfrg_Prvdr_Zip5`
are clean VARCHAR on R2 for every DMEPOS year — DMEPOS's source CSV happens
to preserve them as text. But they were a *latent* gap: they are zero-padded
codes not previously listed in `recast_r2_dtypes.REINGEST_ONLY`, so a future
year that drifted to DOUBLE would have been silently int-hopped and damaged.
The scraper now reads both with `dtype="string"` so inference can't ever
numericize them; add the two `Rfrg_*` names to `REINGEST_ONLY` as a belt-
and-suspenders guard.

**Backfilling the historical parquets** to this contract:
[`pipeline/scripts/recast_r2_dtypes.py`](../../pipeline/scripts/recast_r2_dtypes.py)
derives the per-column cast from this schema (int-hop applied
automatically for float→string targets), rewrites R2 in place via DuckDB,
and re-verifies. `--execute` to swap; default is a dry-run.

## 7. Known limitations

- **Aggregate only.** No claim-level, no per-supplier, no per-HCPCS
  detail. For per-HCPCS DMEPOS work, you'd need the Part B by Provider
  & Service file (`cms_partb_svc_*`) or supplier-level cuts CMS doesn't
  release publicly.
- **Suppression-floor invisibility.** Cells where the count would be
  `< 11` are nulled by CMS upstream (see §3). The row stays in the
  file, but its count and dollar columns for that aggregate are missing.
  Small-volume fraud schemes can hide in these blanks; the dataset is
  naturally tuned to the high-volume schemes worth investigating.
- **`Rfrg_*` is the referrer, not necessarily the treating physician.**
  Brace-mill schemes specifically exploit this — the referrer never saw
  the patient.
- **State is the referrer's billing address, not the beneficiary's.**
  Cross-state schemes (referrer in FL, patients nationwide) appear as
  a single-state row even though the actual claim geography is broad.
- **Known downstream bug:** [`fraudlib/ingest/cms/dmepos_stats.py:108`](../../fraudlib/ingest/cms/dmepos_stats.py)
  treats `Sprsn_Ind == "Y"` as the suppression marker. The actual CMS
  codes are `*` and `#` (see §3). That feature builder therefore reports
  every row as un-suppressed → the suppression-flag features it emits
  are silently zero for the whole dataset. The companion script
  [`scripts/compute_dmepos_rich_features.py:148-160`](../../scripts/compute_dmepos_rich_features.py)
  gets it right. Fix belongs in the planned move of `dmepos_stats.py`
  → `pipeline/transforms/`.

## 8. File contract

After running [`DMEPOSScraper`](../../pipeline/scrapers/cms_dmepos.py):

```
data/panels/cms_dmepos_YYYY.parquet              # 104 cols, ~370k–390k rows
data/panels/cms_dmepos_YYYY.manifest.json        # RunResult: source, sha-of-bytes, schema_version, columns, fetch URL
```

Manifest fields are documented on
[`pipeline.base.RunResult`](../../pipeline/base.py). Loaders read the
manifest at load time, compare `schema_version`, and warn on mismatch.
