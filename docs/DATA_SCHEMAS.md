# Data Schemas and Data Model

Every schema, row count, and column type below is read directly from the
parquet files on disk (see `data/panels/`) and the code that ingests
them ([fraudlib/ingest/download.py](../pipeline/scrapers/download.py),
[fraudlib/ranker/features.py](../fraudlib/ranker/features.py),
fraudlib/ingest/labels/leie.py,
[fraudlib/ingest/cases/labels_v2.py](../fraudlib/labels/labels_v2.py)). Refresh by
re-running the snippets in §6.

> **LEIE is not a truth label** (labels are DOJ cases only). Sections below
> describing `label_providers_with_leie`, the four-way `label_bucket`,
> or LEIE-as-label joins are **historical**. Active pipeline:
> `fraud_case` is the sole training target; `label_bucket` is binary
> `{"case_only", "neither"}` derived from `fraud_case` alone. LEIE
> retains exactly one role: as an NPI repository for case-scrape
> candidates (see `fraudlib/ingest/cases/sources/leie_upgrade.py`).

---

## 1. Source inventory

| Source | File | Rows | Cols | Grain | Ingestor |
|---|---|---:|---:|---|---|
| Part B by Provider | `cms_partb_{year}.parquet`, years 2013–2016, 2019–2023 | 1,259,343 (2023) | 81 | one row per NPI-year | [download.fetch_cms_partb](../pipeline/scrapers/download.py) |
| Part D by Provider | `cms_partd_2023.parquet` | 1,380,665 | 84 | one row per prescriber NPI | [download.fetch_cms_partd](../pipeline/scrapers/download.py) |
| DMEPOS by Referring Provider | `cms_dmepos_2023.parquet` | 387,352 | 97 | one row per referring NPI | [download.fetch_cms_dmepos](../pipeline/scrapers/download.py) |
| LEIE (current) | `leie.parquet` | 83,001 | 18 | one row per excluded party | [download.fetch_leie](../pipeline/scrapers/download.py) |
| LEIE (2019 snapshot) | `leie_2019.parquet` | 83,001 | 18 | same, historical | [download.fetch_leie_historical](../pipeline/scrapers/download.py) |
| Cases v0 (ours) | `gold_set.jsonl` | 25 | n/a | one row per case | cases.build_ground_truth |

All CMS PUFs ingested from CMS direct-CSV permalinks catalogued in
[fraudlib/ingest/download.py:CMS_DIRECT_CSV](../pipeline/scrapers/download.py). LEIE
pulled from the OIG live feed; Wayback Machine mirrors used for
historical snapshots.

---

## 2. Source schemas (real columns, real values)

### 2.1 Part B by Provider — `cms_partb_2023.parquet`

1,259,343 rows × 81 columns. Grouped by theme:

**Identity (7 cols)**
```
Rndrng_NPI                     int64     1900000001
Rndrng_Prvdr_Last_Org_Name     object    "Enkeshafi"
Rndrng_Prvdr_First_Name        object    "Ardalan"
Rndrng_Prvdr_MI                object
Rndrng_Prvdr_Crdntls           object    "M.D."
Rndrng_Prvdr_Ent_Cd            object    "I" (individual) or "O" (org)
Rndrng_Prvdr_Type              object    "Hospitalist"
```

**Location (10 cols)**
```
Rndrng_Prvdr_St1, St2, City, State_Abrvtn, State_FIPS, Zip5, RUCA,
RUCA_Desc, Cntry, Mdcr_Prtcptg_Ind
```

**Volume & payment — all services (7 cols)**
```
Tot_HCPCS_Cds                  int64     11        distinct HCPCS billed
Tot_Benes                      int64     344       beneficiary count
Tot_Srvcs                      float64   814.0     total services
Tot_Sbmtd_Chrg                 float64             submitted charges
Tot_Mdcr_Alowd_Amt             float64             Medicare allowed
Tot_Mdcr_Pymt_Amt              float64   62198.36  Medicare paid
Tot_Mdcr_Stdzd_Amt             float64             geography-normalized
```

**Drug-service split (8 cols, prefix `Drug_`)** — drug administration J-codes.
**Medical-service split (8 cols, prefix `Med_`)** — everything else.

**Beneficiary demographics (13 cols)** — age bins, sex, race, dual-eligible.

**Beneficiary clinical mix (27 cols, prefix `Bene_CC_`)** — per-beneficiary %
with each chronic condition (`_BH_` = behavioral health,
`_PH_` = physical health), plus `Bene_Avg_Risk_Scre` (HCC-like risk score).

**Example row (truncated)**:
```
Rndrng_NPI                1900000001
Rndrng_Prvdr_Type         Hospitalist
Rndrng_Prvdr_State_Abrvtn MD
Tot_HCPCS_Cds             11
Tot_Benes                 344
Tot_Srvcs                 814.0
Tot_Mdcr_Pymt_Amt         62198.36
Bene_Avg_Risk_Scre        2.7545
```

### 2.2 Part D by Provider — `cms_partd_2023.parquet`

1,380,665 rows × 84 columns. Similar identity/location shape, then:

**Overall rx volume (5 cols)**
```
Tot_Clms, Tot_30day_Fills, Tot_Drug_Cst, Tot_Day_Suply, Tot_Benes
```

**Senior subset (6 cols, prefix `GE65_`)** — beneficiaries aged ≥65.

**Brand/generic/other split** (`Brnd_*`, `Gnrc_*`, `Othr_*`).

**Plan split** — MAPD (Medicare Advantage Drug) vs PDP (stand-alone
Part D) vs LIS/NonLIS (low-income subsidy).

**Class-specific flags** — opioids (5 cols), long-acting opioids
(5 cols), antibiotics (3 cols), senior antipsychotics (5 cols). These
are the most fraud-relevant Part D columns.

**Beneficiary demographics (18 cols)** — same shape as Part B.

**Example row (truncated)**:
```
PRSCRBR_NPI          1900000001
Prscrbr_Type         Hospitalist
Tot_Clms             222
Tot_Drug_Cst         18399.98
Opioid_Tot_Clms      13.0
```

### 2.3 DMEPOS by Referring Provider — `cms_dmepos_2023.parquet`

387,352 rows × 97 columns. Joined on the referring physician's NPI
(not the supplier's). Four service-category splits:

- **All DMEPOS** (9 cols, `Tot_Suplr_*` prefix)
- **Durable Medical Equipment** (9 cols, `DME_*`)
- **Prosthetics/Orthotics/Supplies** (9 cols, `POS_*`)
- **Drug/nutrition DMEPOS** (9 cols, `Drug_*`)

Plus the same beneficiary-demographics + clinical-mix block (35 cols).

**Example row (truncated)**:
```
Rfrg_NPI                  1900000001
Rfrg_Prvdr_Spclty_Desc    Hospitalist
Tot_Suplrs                6
Tot_Suplr_Clms            76
Suplr_Mdcr_Pymt_Amt       9796.78
```

### 2.4 LEIE — `leie.parquet`

83,001 rows × 18 columns.

```
LASTNAME       object
FIRSTNAME      object
MIDNAME        object
BUSNAME        object     # populated iff the excluded party is an org
GENERAL        object     # "GENERAL" vs specific-program flag
SPECIALTY      object     # free-text, e.g. "SOBER HOME", "PHARMACY"
UPIN           object     # legacy pre-NPI ID
NPI            object     # "0000000000" if missing
DOB            object     # YYYYMMDD
ADDRESS        object
CITY           object
STATE          object
ZIP            object
EXCLTYPE       object     # statutory section, e.g. "1128a1"
EXCLDATE       object     # YYYYMMDD
REINDATE       object     # reinstatement date (rare)
WAIVERDATE     object
WVRSTATE       object
```

**Real NPI coverage**: only 8,551 of 83,001 rows (10.3%) have a valid
10-digit NPI. The remaining 74,450 (89.7%) are placeholder `0000000000`
— primarily pre-NPI-era exclusions, non-clinician exclusions (sober
homes, home-health agencies that never held NPIs), and records where OIG
could not resolve an NPI. This is the structural reason the LEIE label
under-counts fraud.

**Top exclusion types** (of 83,001):

| EXCLTYPE | Count | Meaning |
|---|---:|---|
| 1128b4 | 33,085 | license revocation/surrender |
| 1128a1 | 25,618 | conviction for program-related crimes |
| 1128a2 | 8,051 | conviction for patient-abuse-related crimes |
| 1128a3 | 5,791 | felony conviction, healthcare fraud |
| 1128a4 | 3,545 | felony conviction, controlled substance |
| 1128b14 | 2,167 | default on health-education-loan |
| 1128b8 | 1,497 | ownership by sanctioned person |
| 1128b1 | 935 | misdemeanor conviction |

Only `1128a1`, `1128a3`, and subsets of `1128a2`, `1128b4` map to
billing-fraud schemes. The remainder are non-billing exclusions that
still carry a positive label in the naive join — a further source of
label noise.

### 2.5 Cases v0 — `gold_set.jsonl`

JSON Schema at .
Required fields: `case_id`, `fraud_types`, `active_years`, `outcome`,
`sources`, `tier`.

Denormalized shape (per `load_cases` in
[labels_v2.py](../fraudlib/labels/labels_v2.py)):

```
case_id               str     "DOJ-2015-ginkgo-oncology"
case_name             str     "Provider Ginkgo oncology fraud"
fraud_types           list    ["medically_unnecessary", "phantom_billing"]
primary_fraud_type    str     "medically_unnecessary"
active_years          list    [2013, 2014]
tier                  str     "A" | "B" | "C"
detectability         str     "strong" | "moderate" | "out_of_panel"
outcome_type          str     "conviction" | "settlement" | ...
outcome_date          str     "2015-07-10"
amount_usd            float   5790000
defendant_name        str     "Provider Ginkgo"
defendant_role        str     "physician"
npi                   str     "1952357413"
npi_confidence        str     "high" | "medium" | "low" | "corporate_na"
```

### 2.6 DMEPOS by Referring Provider & Service — `cms_dmepos_detailed_2023.parquet`

The per-HCPCS cut of §2.3. **Multi-row per NPI**: one row per
(Referring NPI × HCPCS_CD × Suplr_Rentl_Ind), so the referring NPI repeats
across the distinct items it ordered (≈ a handful to a few dozen rows per
referrer). 33 native CMS columns → **40 after the canonical/stamp columns**.
Domain primer: [data/docs/cms_dmepos_detailed.md](../data/docs/cms_dmepos_detailed.md).

Column groups: 19 `Rfrg_*` provider-identity (denormalized) · 3 RBCS
taxonomy (`RBCS_Lvl`/`RBCS_Id`/`RBCS_Desc` — replaces legacy BETOS; **no
`BETOS_Cd` column**) · 3 HCPCS/rental (`HCPCS_CD` — note upper-case `CD`;
`HCPCS_Desc`; `Suplr_Rentl_Ind` `Y`/`N`, part of the grain) · 8 aggregates.

**Dollar columns are per-service averages, not totals** (same as §2.1's
by-service file): total paid = `Avg_Suplr_Mdcr_Pymt_Amt * Tot_Suplr_Srvcs`.
CMS redacts (nulls) any element with <11 claims, so all counts/amounts are
nullable.

**Example rows (truncated — one referrer, two items)**:
```
npi          Rfrg_Prvdr_Spclty_Desc  HCPCS_CD  Suplr_Rentl_Ind  Tot_Suplr_Srvcs  Avg_Suplr_Mdcr_Pymt_Amt
1900000050   Family Practice         K0001     N                800.0            368.75   # standard wheelchairs
1900000050   Family Practice         L0650     N                 90.0           1444.40   # lumbar-sacral "back brace"
```

Years 2014–2023 (2013 never published; same as §2.3). All ten ship under
the RY25 release with a homogeneous 33-column header.

### 2.7 Physician Compare — DAC `physician_compare_2026-05.parquet`

CMS Doctors and Clinicians National Downloadable File (dataset
`mj5m-pzi6`) — the clinician→group-practice roster. **Multi-row per NPI**:
one row per enrollment (NPI × `Org_PAC_ID` × address). Snapshot cadence
(quarterly-ish), keyed by `YYYY-MM`, **not** an annual cohort (`data_year`
is null). The same file the ranker-side
[`care_compare.py`](../fraudlib/ingest/providers/care_compare.py) reads as a
single current snapshot; this producer writes **dated, historical**
snapshots at the raw enrollment grain. Domain primer:
[data/docs/physician_compare.md](../data/docs/physician_compare.md).

Headline field: **`Org_PAC_ID`** (Group PAC ID) — the group practice's
billing-entity / TIN-level identity, which NPPES (address-only) lacks.
Curated canonical columns (the layout drifts across snapshots, so the
scraper projects to these and pads missing): `Ind_PAC_ID`, `Org_PAC_ID`,
`Org_Lgl_Nm` (← `Facility Name` on recent files), `Num_Org_Mem`, `pri_spec`,
`Provider_Type`, `adr_ln_1`/`adr_ln_2`/`City`/`state`/`ZIP_Code`,
`Hosp_Afl_1..5` (CCNs; older snapshots only), `snapshot` (`YYYY-MM`).

**Example rows (truncated — two NPIs under one billing entity = shared-TIN)**:
```
snapshot   npi          Org_PAC_ID    Org_Lgl_Nm                  Num_Org_Mem
2026-05    1900000007   9921000123    VALLEY PAIN ASSOCIATES PC   6
2026-05    1558xxxxxx   9921000123    VALLEY PAIN ASSOCIATES PC   6
```

Files: `physician_compare_<YYYY-MM>.parquet` (dated) +
`physician_compare.parquet` (relative symlink → most-recent snapshot).

---

## 3. Data model — join keys and relationships

```
                               ┌──────────────────────────────────────┐
                               │          NPI (10-digit string)       │
                               │  canonical join key across sources   │
                               └────────────────┬─────────────────────┘
                                                │
          ┌──────────────┬────────────┬─────────┴─────────┬──────────────────┐
          │              │            │                   │                  │
┌─────────▼─────┐ ┌──────▼──────┐ ┌───▼────────┐ ┌────────▼─────┐ ┌─────────▼──────┐
│ cms_partb_YYYY│ │cms_partd_YYYY│ │cms_dmepos_ │ │  leie.parquet│ │ gold_set.jsonl │
│ Rndrng_NPI    │ │ PRSCRBR_NPI  │ │YYYY Rfrg_  │ │  NPI (10-dig)│ │ defendants[].npi│
│ 1 row/year    │ │ 1 row/year   │ │NPI 1/year  │ │ 1 row/party  │ │ 1 row/case     │
│ 81 cols       │ │ 84 cols      │ │ 97 cols    │ │ 18 cols      │ │ schema below   │
└───────┬───────┘ └──────┬───────┘ └────┬───────┘ └──────┬───────┘ └────────┬───────┘
        │                │              │                │                  │
        │                │              │                │                  │
        └──────────┬─────┴────┬─────────┘                │                  │
                   │          │                          │                  │
            ┌──────▼──────────▼──────┐                   │                  │
            │   concat_multisource   │                   │                  │
            │   (features.py:157)    │                   │                  │
            │   outer-join on NPI    │                   │                  │
            │   partb_*, partd_*,    │                   │                  │
            │   dmepos_*, has_partd  │                   │                  │
            │   has_dmepos flags     │                   │                  │
            └──────────┬─────────────┘                   │                  │
                       │                                 │                  │
                       │  left-join on NPI               │                  │
                       ├─────────────────────────────────┤                  │
                       │                                 │                  │
                ┌──────▼──────────────────────────────────▼────────┐        │
                │ label_providers_with_leie  (labels.py:49)        │        │
                │ adds: fraud (0/1),  excldate (YYYYMMDD)          │        │
                │ filter:   EXCLDATE >= Jan 1 of puf_year          │        │
                └──────────┬───────────────────────────────────────┘        │
                           │                                                │
                           │  left-join on NPI                              │
                           ├────────────────────────────────────────────────┤
                           │                                                │
                    ┌──────▼─────────────────────────────────────────┐
                    │ label_providers_with_cases (labels_v2.py:57)   │
                    │ adds: fraud_case (0/1), case_ids, case_tier,   │
                    │       case_fraud_types, label_bucket           │
                    │ filter:  puf_year ∈ case.active_years AND      │
                    │          npi_confidence ∈ {high}               │
                    └──────────┬─────────────────────────────────────┘
                               │
                 ┌─────────────▼──────────────┐
                 │ provider_matrix  or        │
                 │ multisource_matrix         │
                 │ (features.py:227 / :271)   │
                 │ returns (X, y) for sklearn │
                 └─────────────┬──────────────┘
                               │
                 ┌─────────────▼──────────────┐
                 │ GroupKFold by NPI          │
                 │ (sampling.py → paper 10's  │
                 │  honest CV)                │
                 └────────────────────────────┘
```

### 3.1 Canonical join key

Every source emits NPI under a different column name. The normalizer
([features.py:_RENAME_MAP](../fraudlib/ranker/features.py)) maps them all
to `npi`:

| Source column | Normalized |
|---|---|
| `Rndrng_NPI` (Part B) | `npi` |
| `PRSCRBR_NPI` (Part D) | `npi` |
| `Rfrg_NPI` (DMEPOS) | `npi` |
| `NPI` (LEIE) | `npi` |
| `defendants[].npi` (cases) | `npi` |

NPI normalization rule: coerce to string, strip non-digits, require
exactly 10 digits, else → NaN. See
labels._to_npi_series.

### 3.2 Temporal join (multi-year panel)

For paper 10-style honest CV, the panel is built with
[features.multi_year_provider_panel](../fraudlib/ranker/features.py):

```python
multi_year_provider_panel(
    fetch_fn=fetch_cms_partb,
    years=[2019, 2020, 2021, 2022, 2023],
)
# → long frame with an added `puf_year` column, one row per (NPI, year)
```

Years currently on disk: Part B `{2013, 2014, 2015, 2016, 2019, 2020,
2021, 2022, 2023}`; Part D and DMEPOS 2023 only.

### 3.3 Label joins

Two label sources, joined separately, combined via `label_bucket`:

```
LEIE join:     provider.npi   =  leie.npi
  filter:      leie.EXCLDATE >= Jan 1 of puf_year   (default: during_or_after)
  output col:  fraud  (0/1)

Cases join:    provider.npi   =  case.defendants[].npi
  filter:      puf_year ∈ case.active_years
               AND case.defendants[].npi_confidence = "high"
  output cols: fraud_case (0/1), case_ids, case_fraud_types, case_tier

Combined:      label_bucket ∈ {neither, leie_only, case_only, both}
```

Year-by-year result of the combined join (run in §6):

| Year | LEIE positives | Case positives | Both | Case-only |
|---:|---:|---:|---:|---:|
| 2013 | 2,095 | 7 | 6 | 1 |
| 2014 | 1,927 | 6 | 5 | 1 |
| 2015 | 1,756 | 5 | 4 | 1 |
| 2016 | 1,577 | 0 | 0 | 0 |
| 2019 | 906 | 0 | 0 | 0 |
| 2023 | 196 | 0 | 0 | 0 |

LEIE decreases over panel years because `during_or_after` filters more
future exclusions out as `puf_year` gets later. Case positives peak in
2013–2015 because our v0 resolved-NPI defendants (Ginkgo, Melgen, Ruan,
Couch, Qamar, Reinstein, Alfonso) all billed in that window.

---

## 4. Feature engineering

[features.engineer](../fraudlib/ranker/features.py) operates on a single
source. It does three things:

1. **Column rename**: 2020-schema `Rndrng_*` → snake_case baseline
   (see `_RENAME_MAP`).
2. **Numeric coercion**: the 9 `BASE_NUMERIC` columns →
   `pd.to_numeric(errors='coerce')`.
3. **Derived ratios** (Part B has all four; Part D/DMEPOS subset):
   ```
   submitted_to_allowed_ratio  =  total_submitted_charge / total_medicare_allowed
   payment_per_service         =  total_medicare_payment / n_services
   services_per_beneficiary    =  n_services / n_beneficiaries
   hcpcs_diversity             =  n_hcpcs_codes / n_services
   ```

[features.concat_multisource](../fraudlib/ranker/features.py) outer-joins
engineered Part B / D / DMEPOS on `npi` with per-source column prefixes
and `has_partd`, `has_dmepos` presence indicators. Smoke-test shape
(first 5,000 rows of each source): 10,119 rows × 265 columns, with 5,000
having both Part D and DMEPOS.

[features.provider_matrix](../fraudlib/ranker/features.py) returns `(X, y)`
for single-source papers. `BASE_NUMERIC` (9 cols) + 4 derived ratios +
one-hot top-50 levels of `provider_type`, `state`, `entity_code`, etc.

[features.multisource_matrix](../fraudlib/ranker/features.py) is the
equivalent for multi-source: selects every numeric column except
`fraud`/`npi`/`excldate` across the prefixed columns.

---

## 5. Final train/test materialization

Composed per paper. The
canonical pipeline:

```
┌──────────────────────┐
│ fetch_cms_partb(2023)│   ~1.26M × 81
└──────────┬───────────┘
           │ engineer()
           ▼
┌──────────────────────┐
│ provider panel, normalized │
└──────────┬───────────┘
           │ label_providers_with_leie(puf_year=2023, during_or_after)
           ▼
┌──────────────────────┐
│ + fraud column       │   1.26M × 82;  positives=196 (0.016%)
└──────────┬───────────┘
           │ [optional] label_providers_with_cases(puf_year=2023, high-conf)
           ▼
┌──────────────────────┐
│ + fraud_case column  │   for ground-truth audit (2023: +0)
└──────────┬───────────┘
           │ provider_matrix() → (X, y)
           ▼
┌──────────────────────┐
│ GroupKFold(5) on NPI │   paper 10's honest CV
│ train/test split     │
└──────────┬───────────┘
           ▼
      sklearn / xgboost / torch
```

Scale drops:
paper_15, paper_10, paper_06, paper_03 subsample to 80k–200k providers
so SMOTE/LOF/kNN/stacked CV run in tractable time.

---

## 6. How to regenerate any number in this doc

```bash
# Source inventory + shapes:
python3 -c "
import pandas as pd
for f in ['cms_partb_2023','cms_partd_2023','cms_dmepos_2023','leie','leie_2019']:
    df = pd.read_parquet(f'data/panels/{f}.parquet')
    print(f, df.shape)
"

# LEIE NPI coverage:
python3 -c "
import pandas as pd
df = pd.read_parquet('data/legacy/leie/leie.parquet')
v = df[df['NPI'].str.match(r'^\d{10}$') & df['NPI'].ne('0000000000')]
print(f'valid NPI: {len(v):,} of {len(df):,} ({100*len(v)/len(df):.1f}%)')
print(df['EXCLTYPE'].value_counts().head(8))
"

# Label-bucket audit across years:
python3 -c "
import sys, pandas as pd
sys.path.insert(0, '.')
from fraudlib.ingest.download import fetch_leie
from fraudlib.ingest.labels.leie import label_providers_with_leie
from fraudlib.ingest.cases.labels_v2 import load_cases, label_providers_with_cases, summarize_buckets
leie = fetch_leie()
cases = load_cases('data/extracted/gold_set.jsonl')
for y in (2013, 2014, 2015, 2016, 2019, 2023):
    partb = pd.read_parquet(f'data/panels/cms_partb_{y}.parquet', columns=['Rndrng_NPI'])
    labeled = label_providers_with_leie(partb, leie, puf_year=y)
    merged = label_providers_with_cases(labeled, cases, puf_year=y,
                                         min_confidence='high', leie_fraud_col='fraud')
    print(y, summarize_buckets(merged))
"

# Multisource merge shape:
python3 -c "
import pandas as pd
from fraudlib.ranker.features import engineer, concat_multisource
b = engineer(pd.read_parquet('data/panels/cms_partb_2023.parquet').head(5000))
d = engineer(pd.read_parquet('data/panels/cms_partd_2023.parquet').head(5000))
m = engineer(pd.read_parquet('data/panels/cms_dmepos_2023.parquet').head(5000))
print(concat_multisource(b, d, m).shape)
"
```

---

## 7. Known schema gaps

1. **Part B by Service** (HCPCS grain): ingested URLs exist in
   `CMS_DIRECT_CSV` for 2019–2023 but aren't parsed into
   `data/panels/`. Required for E&M-level-mix and modifier-59 rates.
2. **NPI deactivation file** (NPPES): not ingested. Required for
   post-deactivation-billing rule.
3. **SSA Death Master File**: not ingested. Required for
   deceased-beneficiary phantom-billing rule.
4. **Open Payments** (industry payments to providers): not ingested.
   Required for kickback-proxy features.
5. **NPPES full registry** (name→NPI resolution): not ingested. Required
   to scale case-defendant NPI resolution past the ~8 physician cases
   resolved manually.

All five are downstream-compatible with the existing NPI-keyed
data-model — adding them does not change the join topology, only
extends the per-NPI feature vector.

---

## 8. Facility PUFs (ingest-only, not yet agent-visible)

Added 2026-04-22 as scaffolding for future facility-level fraud rules
(SNF kickbacks, hospice-eligibility fraud, HHA plan-of-care
falsification). These are **facility-grain**, not provider-grain —
joined to the provider-keyed panel via the facility's CMS
Certification Number (CCN) or via Care Compare's hospital-affiliation
list on a provider's row.

No `open_partb` flag is wired yet; the agent can already read them via
`run_sql` by registering the parquet with DuckDB, but no rule is
authored so none of the columns are whitelisted in `_ALLOWED_COLS`.
When the first facility-grain rule lands, that changes.

| Source | Parquet | Grain | Fraud signal | Ingestor |
|---|---|---|---|---|
| Hospital General Info | `cms_facility_hospital.parquet` | one row per CCN (~5K) | Ownership concentration, overall rating | [download.fetch_cms_facility_hospital](../pipeline/scrapers/download.py) |
| Nursing Home / SNF Provider Info | `cms_facility_snf.parquet` | one row per CCN (~15K) | For-profit chain over-indexing on SNF fraud; staffing-hour outliers | [download.fetch_cms_facility_snf](../pipeline/scrapers/download.py) |
| Home Health Agency General Info | `cms_facility_hha.parquet` | one row per CCN (~12K) | Proprietary HHAs have higher POC-falsification rates | [download.fetch_cms_facility_hha](../pipeline/scrapers/download.py) |
| Hospice General Info | `cms_facility_hospice.parquet` | one row per CCN (~5K) | Hospice-eligibility fraud — billing for patients not actually end-of-life | [download.fetch_cms_facility_hospice](../pipeline/scrapers/download.py) |

All four fetchers follow the same shape (`fetch_cms_facility_*(local_csv=None, url=None)`), write `data/panels/<source>.parquet`, and accept a manual URL override since the CMS download link rotates per refresh. URL candidates live in `download.FACILITY_URL_CANDIDATES`; update by visiting the landing pages linked from the fetcher docstrings and copying the current "Download CSV" target.

Acceptance for this ingest-only PR: all four parquets written, row counts match CMS-published totals within 1%. Row-count + join-to-provider spot-checks get logged in the sprint journal after the first bulk run.
