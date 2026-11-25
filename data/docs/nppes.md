# NPPES — National Plan and Provider Enumeration System

**Scraper:** [`pipeline/scrapers/nppes.py`](../../pipeline/scrapers/nppes.py)
**Schema reference (columns + dtypes):** [docs/DATA_SCHEMAS.md](../../docs/DATA_SCHEMAS.md)
**Files:** `data/registries/nppes_bulk.parquet` + `nppes_bulk.manifest.json`

## 1. What this dataset is, in plain English

**NPPES** is CMS's master registry of every healthcare provider with an
NPI — the 10-digit National Provider Identifier that every Medicare-
billable entity in the United States must have. Where the PUFs
(Part B / Part D / DMEPOS) tell you what a provider *billed*, NPPES
tells you who they *are*: their legal name, the year they were issued
an NPI, their declared specialty (in NUCC taxonomy code form), their
credentials, and whether they're an individual or an organization.

This is **registry data, not claims data**:

```
   provider applies for NPI  ──CMS issues NPI──▶   NPPES record
        (one-time event)                             (lives forever)
```

NPPES is updated continuously — a provider can amend their address,
credentials, or taxonomy at any time, and CMS publishes a fresh full-
registry zip once a month. The file is the universal join target for
every NPI-keyed source in this project (Part B, Part D, DMEPOS,
gold_set). It's how we get from "this NPI billed $2M" to "this NPI is
an individual MD enumerated in 2018 with primary taxonomy 207R00000X
(internal medicine), practicing in Florida."

NPPES on its own is not a fraud-detection dataset. Its value is as the
**ground-truth registry** against which the PUF-derived attributes are
checked. The fraud signals here are *mismatch signals* — a Family
Practice billing surface paired with an oncology NUCC code, or a high-
volume DMEPOS referrer whose NPI was enumerated 18 months before they
started signing orders.

### Acronyms used in column names

| Acronym | Meaning |
|---|---|
| `NPI` | National Provider Identifier — 10-digit primary key, issued once per entity (individual or organization) |
| `NPPES` | National Plan and Provider Enumeration System — the registry holding all NPI records |
| `NUCC` | National Uniform Claim Committee — the body that maintains the healthcare-provider taxonomy code set (10-character codes like `207R00000X`) |
| `Entity Type Code` | `1` = individual provider, `2` = organization. **Different from the PUF convention `"I"`/`"O"`** — the scraper maps before calling `format_provider_name` |
| `Enumeration Date` | The date CMS issued this NPI. Phantom-billing signal when paired with the first big-billing PUF year |
| `Provider Credential Text` | Free-text credentials field (`MD`, `DO`, `NP, PA`, `RN, MSN`, …). Lossy — typically captures the entity's self-reported degrees |
| `Healthcare Provider Taxonomy Code_1` | Primary NUCC taxonomy code (10 chars). NPPES allows up to 15 secondary entries (`_2` through `_15`); the projection keeps only `_1` |
| `Is Sole Proprietor` | `Y` / `N` / `X` (not disclosed). True iff the individual NPI bills as a one-person practice rather than under a group NPI |
| `Replacement NPI` | If this NPI was reassigned/replaced, the successor NPI. Rare; not in the projection |
| `Last Update Date` | The most recent NPPES record edit (address change, taxonomy add, etc.) — *not* the same as `Provider Enumeration Date` |
| `NPI Deactivation Reason Code` | `DT` = death, `FR` = fraud, `OT` = other, `RR` = reassigned. Populated only for deactivated NPIs |
| `NPI Deactivation Date` | The date CMS deactivated this NPI. Empty for active records |

## 2. Unit of observation

**One row = one NPI.** Every NPI ever issued is present — currently
~7M individuals + ~2M organizations, totaling ~9M rows.

There is no time dimension on the row. Each monthly snapshot is a
full re-pull of the entire registry; deactivated NPIs are kept in the
file (with `NPI_Deactivation_Date` populated) and active NPIs reflect
their current attributes. **There is no historical state** — you can't
ask "what was provider X's taxonomy in 2014" from this file. For that,
the per-NPI API (`fraudlib.labels.nppes_lookup`) returns the same
fields but for a single NPI, and historical NPPES snapshots are
preserved in Wayback Machine archives.

CMS publishes ~330 columns per NPI; the canonical projection reads 13 but
**emits 12** (the raw `NPI` is read to build the canonical `npi`, then
dropped — see §6a):

- **identifier:** the source `NPI` is read but not emitted; the canonical
  zero-padded string `npi` (below) is the only NPI column shipped (v2.0)
- **5 identity:** `Entity_Type_Code`, `Provider_Organization_Name`,
  `Provider_Last_Name_Legal_Name`, `Provider_First_Name`,
  `Provider_Credential_Text`
- **2 structural:** `Healthcare_Provider_Taxonomy_Code_1`,
  `Is_Sole_Proprietor`
- **4 dates:** `Enumeration_Date`, `Last_Update_Date`,
  `NPI_Deactivation_Reason_Code`, `NPI_Deactivation_Date`
- **1 address:** `Provider_Business_Practice_Location_Address_State`

After our scrape pass, parquets carry 7 additional canonical columns:
3 base stamps (`source`, `fetched_at_utc`, `schema_version`) + 4 NPI-
keyed canonicals (`npi`, `provider_name`, `state`, `data_year`) — see
[`pipeline.base.ProviderRecordSchema`](../../pipeline/base.py).
`data_year` is always `None` for NPPES — there is no year-cohort
interpretation.

Total declared schema: **19 columns** (v2.0; was 20 before the raw `NPI`
was dropped).

## 3. How CMS publishes it (cadence + suppression)

- **Monthly snapshot, evergreen.** Unlike Part B / Part D / DMEPOS,
  there is no annual "data year" concept. Each monthly file is a
  complete full-registry dump.
- **Published ~2nd week of each month** at
  `https://download.cms.gov/nppes/NPI_Files.html`. The scraper probes
  the current month, then the prior two, before raising.
- **URL pattern:** `NPPES_Data_Dissemination_{Month}_{Year}.zip` where
  Month is the full English name (e.g. `May`, `April`).
- **Raw zip size:** ~2 GB compressed, ~7 GB extracted. The scraper has
  a 3 GB compressed-size cap — files above that trigger a fall-back
  recommendation (per-NPI API enrichment for short lists).
- **Suppression: none.** NPPES is a registry, not a claims aggregate.
  There are no cell-suppression flags, no row-count floors, no
  redaction codes. Every NPI ever issued is in the file — including
  ones deactivated decades ago (`NPI_Deactivation_Date` is the marker
  for those). The "What this means for fraud detection" caveat that
  the PUF docs flag for `Sprsn_Ind` doesn't apply: this file is
  exhaustive at the NPI level.
- **Drift cadence.** Schema drift is rare in NPPES (the column list is
  governed by the NPI Application Form, which CMS amends slowly).
  When CMS *does* add a column (most recent additions: practice-
  location telephone, deactivation reason codes), the scraper's strict
  validation will surface it as `RuntimeError: NPPES schema drift:
  missing expected columns […]` so the maintainer can decide whether
  to expand the projection. See `schema_version` policy in the scraper
  module docstring.

## 4. Fraud signals — what to look for

NPPES on its own doesn't tell you who's committing fraud. The signal
comes from **comparing NPPES to PUF activity** — a provider whose
registered identity contradicts how they're billing. The full
mismatch suite:

### 4a. Specialty mismatch

The PUF specialty (`Rndrng_Prvdr_Type` in Part B, `Prscrbr_Type` in
Part D, `Rfrg_Prvdr_Spclty_Desc` in DMEPOS) is the provider's self-
reported specialty *on their claims*. The NPPES taxonomy code is what
they registered with CMS at NPI enumeration. The two should agree.
When they don't, it usually means either (a) the provider is billing
out-of-scope (a Family Practice physician billing oncology J-codes)
or (b) a rented NPI is being used by a billing service that bills
whatever the scheme needs.

**Column signatures:**

- `Healthcare_Provider_Taxonomy_Code_1` prefix vs. PUF specialty string
  — e.g. NUCC `207RX0202X` (Cardiology — Sports Medicine) paired with
  PUF `"Family Practice"` is structurally implausible.
- Cross-source bucketing tool:
  [`fraudlib/ingest/providers/nppes_ingest.py`](../../fraudlib/ingest/providers/nppes_ingest.py)
  `_bucket_taxonomy()` collapses the 800+ NUCC codes into 25 specialty
  buckets you can compare directly against the PUF specialty.

### 4b. Credential mismatch

PUF `Rndrng_Prvdr_Crdntls` (Part B) or `Prscrbr_Crdntls` (Part D) vs.
NPPES `Provider_Credential_Text`. Less load-bearing than specialty —
both fields are free text and the noise level is high — but when the
PUF claims an MD and NPPES says only "RN, BSN", that's a flag.

### 4c. New-provider, high-volume signal

A short window between `Enumeration_Date` and the first big-billing
PUF year is a fraud precursor. Two patterns:

- **Fresh shell entity.** An organization NPI enumerated in 2022
  showing $5M of DMEPOS referrals in 2023 — no operating history, no
  ramp-up. Almost always a phantom DME mill.
- **Rented physician NPI.** An individual physician enumerated in 2020
  who suddenly produces 2,000+ DMEPOS referrals in their first
  reporting year, often from a state they don't live in.

**Column signatures:**

- `nppes_years_enumerated_at_<year>` (derived from `Enumeration_Date`
  in `compute_nppes_features`) < 2 paired with high `Suplr_Mdcr_Pymt_Amt`
  in DMEPOS or high `Tot_Mdcr_Pymt_Amt` in Part B.
- Canonical case: **Operation Brace Yourself** (2019, DMEPOS) — many
  defendants' physicians had `Enumeration_Date` < 2 years before the
  first big DMEPOS year.

### 4d. Deactivated NPI still billing

CMS deactivates NPIs for death (`DT`), fraud finding (`FR`), other
(`OT`), or reassignment (`RR`). A deactivated NPI showing up in a PUF
year that *post-dates* the deactivation is at minimum a data-quality
red flag, and not infrequently a fraud signal (the scheme is billing
under a deceased physician's NPI).

**Column signatures:**

- `NPI_Deactivation_Date.notna()` AND that date precedes the most
  recent PUF year where the NPI appears.
- `NPI_Deactivation_Reason_Code == "DT"` (death) paired with any post-
  deactivation PUF activity is the cleanest signal.
- The legacy `nppes_ingest.py` projection didn't include these two
  columns; this scraper adds them so the signal is computable.

### 4e. Sole-proprietor vs. organization mismatch

`Is_Sole_Proprietor == "Y"` means the individual NPI bills as a one-
person practice with no group affiliation. A "sole proprietor" who
appears to bill from many different addresses across the PUF years is
structurally suspicious — a real solo practice operates from one place.
Conversely, an organization NPI (`Entity_Type_Code == "2"`) that bills
*only* one rendering physician is the inverse pattern (a shell
incorporation around a single doctor).

## 5. Joins to other sources

NPPES is the universal join target. Every NPI-keyed source in the
project joins to it via `npi`, never the other way around. Join key
is the canonical zero-padded `npi` column (string, 10 digits).

| Join target | Why |
|---|---|
| `cms_partb_YYYY.parquet` | Specialty / credential ground-truth; enumeration-date phantom-billing flag |
| `cms_partd_YYYY.parquet` | Same, for prescribing-side signals (pill mill, opioid over-prescribing) |
| `cms_dmepos_YYYY.parquet` | Enumeration-date "new-provider high-volume" flag (load-bearing for brace mills) |
| `cms_partb_svc_YYYY.parquet` | Code-mix vs. taxonomy mismatch — cross with [`fraudlib/ranker/hcpcs_taxonomy.py`](../../fraudlib/ranker/hcpcs_taxonomy.py) |
| `pecos_ordering_referring.parquet` | PECOS enrollment overlay (which Medicare programs the NPI is enrolled in) |
| `leie.parquet` (legacy) | OIG exclusion roster |
| `gold_set.jsonl` | Label join via `npi` |

Pitfalls when joining:

- **NPI must be string-zero-padded.** Raw `NPI` is int64; the canonical
  `npi` column on the stamped parquet is `str` zero-padded to 10
  characters. Use `npi`, not `NPI`, for any cross-source join.
- **Entity code is `"1"`/`"2"` here, not `"I"`/`"O"`.** Code that
  expects the PUF convention needs the same mapping
  `NppesScraper._add_canonical_columns` does internally.
- **`state` is a 2-letter abbreviation derived from the practice-
  location field.** Roughly ~10% of NPIs (mostly organizations and
  some foreign-address individuals) have a blank practice-location
  state in NPPES; those rows carry `state=None`. The raw column
  `Provider_Business_Practice_Location_Address_State` carries the full
  upper-case state name (e.g. `"CALIFORNIA"`) and is also preserved on
  the parquet for fallback parsing.
- **There is no time axis.** Joining NPPES (a single current snapshot)
  against a PUF year means you're asking "what did NPPES look like
  today, for an NPI that billed in 2014?" — fine for most signals
  (credentials and taxonomy are quasi-stable), but for "fresh
  enumeration" checks the `Enumeration_Date` is the truth source
  regardless of snapshot date.

## 6. Year coverage and schema drift

NPPES is evergreen — there is no annual cohort. One snapshot at
`data/registries/nppes_bulk.parquet`, refreshed monthly. The
`data_year` column is always `None`.

CMS has been adding columns to NPPES slowly over the past decade —
the practice-location telephone, deactivation reason codes, and
endpoint-pfile cross-references were all added between 2018 and 2024.
None of those affect the projected schema in v1.0; future expansions
should follow the schema-version rules in
[`pipeline/scrapers/nppes.py`](../../pipeline/scrapers/nppes.py)
(adding a column → minor bump; renaming or removing a column → major
bump).

Strict validation in `BaseScraper._validate()` will fail loudly if
CMS renames a projected column upstream — the renamer goes in the
parser, not the schema.

### 6a. Dtype contract (Pandera) and observed per-year dtype drift

As of 2026-06 the schema is a Pandera `DataFrameModel`
([`pipeline/base.py`](../../pipeline/base.py)). `_validate()` now does
three things in one pass: **coerce** each column to its declared dtype,
**reject** undeclared columns (strict), and enforce **nullability + value
checks**. So column *dtype* drift — not just column presence — fails at
the producer instead of shipping to R2.

Unlike the year-partitioned PUFs, NPPES is a single rolling monthly
snapshot, so there is no *per-year* dtype drift. But a 2026-06 audit of
the R2 artifact found a different defect: it carried **both** a raw `NPI`
(stored `BIGINT`) and the canonical `npi` (UTF8 string). Parquet is
case-sensitive so both coexisted, but **case-insensitive SQL engines
(DuckDB, Spark, BigQuery) fold `NPI` and `npi` together** and disambiguate
the second by suffix — DuckDB surfaces the canonical column as `npi_1`, so
`SELECT npi` silently binds to the raw `BIGINT` instead of the intended
zero-padded string. No values were lost (NPIs never lead with a zero), but
the join key was ambiguous.

**Fix (schema v2.0): the raw `NPI` is dropped.** The producer reads the
source `NPI` to derive the canonical `npi`, then drops it before
validation; `npi` is now the only NPI column. `Config.strict` rejects any
stray `NPI`, so the ambiguity can't reappear.

| Column | R2 state (pre-v2.0) | Schema target | Action |
|---|---|---|---|
| `NPI` (raw) | `BIGINT` (collided with `npi` → shown as `npi_1` in DuckDB) | — (removed in v2.0) | **drop** from R2 + re-ingest with v2.0 |
| `npi` (canonical) | UTF8 string, clean (built `int64 → str → zfill(10)`) | `str` `^(\d{10})?$` | keep — the sole NPI key |
| all other projected columns | as declared | as declared | none |

**NPI handling.** **Always join on `npi`.** Post-v2.0 it is the only NPI
column and resolves unambiguously in every engine.

**Backfilling the existing R2 artifact:** the raw `NPI` must be dropped
from `nppes_bulk.parquet`. Note `recast_r2_dtypes.py` does **not** reach
this file — its `_list_years()` requires a `_<YYYY>` filename, which the
year-less `nppes_bulk.parquet` lacks — so the drop is handled separately
(a `SELECT * EXCLUDE (NPI)` rewrite, or simply re-running
`NppesScraper().fetch()`, which now produces the v2.0 shape directly).
Consumers were updated in lockstep:
[`load_nppes_features`](../../fraudlib/ingest/providers/nppes_ingest.py)
now prefers `npi` and falls back to the legacy raw `NPI`, so it reads both
pre- and post-v2.0 parquets.

## 7. Known limitations

- **No historical state.** Each monthly snapshot is current-only; you
  can't ask "what was this provider's taxonomy in 2014" from the
  current parquet. For historical NPPES, fall back to the per-NPI
  API (`fraudlib.labels.nppes_lookup`) which returns the same fields
  + a `last_update_date`, or pull a Wayback-Machine snapshot.
- **Free-text credentials are noisy.** `Provider_Credential_Text` is
  whatever the provider typed at enumeration. Common forms: `MD`,
  `M.D.`, `MD, FACS`, `MD, FACS, FACOG`. The `_bucket_credential`
  helper in `nppes_ingest.py` regex-maps to 7 buckets; mismatch rates
  against `_bucket_credential(PUF_Crdntls)` are non-trivial false-
  positive sources.
- **Taxonomy 2–15 are not projected.** A real provider can register up
  to 15 NUCC taxonomies (one primary + 14 secondaries) and switch
  among them at any time. This projection keeps only the primary
  (`Healthcare_Provider_Taxonomy_Code_1`). For most providers the
  secondaries are blank; for cross-discipline ones (an MD also
  registered as a dentist; a physician group with multiple
  specialties) the secondaries matter. Expand if a downstream consumer
  needs cross-taxonomy comparison.
- **Replacement NPI is not projected.** When an NPI is reassigned
  (common after a fraud finding and re-enumeration under a new entity),
  `Replacement_NPI` points at the successor. The projection currently
  doesn't include it — re-add if the "deactivated NPI still billing"
  signal needs to walk the replacement chain.
- **State coverage is ~90%.** Roughly 10% of NPIs (mostly
  organizations with mailing-only addresses, plus some foreign-address
  individuals) have a blank practice-location state. The canonical
  `state` column carries `None` for those rows; the raw upper-case
  full-name field
  `Provider_Business_Practice_Location_Address_State` is preserved on
  the parquet for fallback parsing if needed.
- **`Is_Sole_Proprietor` is self-reported and frequently `X` (not
  disclosed).** For ~30% of individual NPIs this column carries no
  information.
- **Organization NPIs don't have a `Provider_First_Name`** — the
  org-name column carries the legal business name and the individual-
  name columns are blank. `format_provider_name` handles this by
  switching on the mapped entity code.

## 8. File contract

After running [`NppesScraper`](../../pipeline/scrapers/nppes.py):

```
data/registries/nppes_bulk.parquet              # 20 cols, ~9M rows (~189 MB)
data/registries/nppes_bulk.manifest.json        # RunResult: source, schema_version, columns, fetch URL, fetched_at_utc
```

Manifest fields are documented on
[`pipeline.base.RunResult`](../../pipeline/base.py). Loaders read the
manifest at load time, compare `schema_version`, and warn on mismatch.

Refresh cadence: the scraper is safe to run monthly. Each run
overwrites the parquet + manifest with the latest CMS snapshot. If
month-over-month auditing matters (rare — most consumers want
"current"), archive the prior month's pair before re-running.
