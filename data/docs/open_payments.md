# Open Payments — General Payments

**Scraper:** [`pipeline/scrapers/open_payments.py`](../../pipeline/scrapers/open_payments.py)
**Schema reference (columns + dtypes):** [docs/DATA_SCHEMAS.md](../../docs/DATA_SCHEMAS.md)
**Files:** `data/panels/open_payments_general_YYYY.parquet` + `open_payments_general_YYYY.manifest.json`

## 1. What this dataset is, in plain English

**Open Payments** (officially the Sunshine Act / Physician Payments
Sunshine Act) is CMS's mandatory disclosure of payments and transfers
of value flowing **from pharmaceutical manufacturers and group-purchasing
organizations (GPOs) to physicians and teaching hospitals**. Drug-maker
"Acme Pharma" pays Dr. X $20,000 for a speaker series in 2023 — that
shows up here as 1+ rows under Dr. X's NPI.

Open Payments has three publication cuts. **This scraper covers only
the first.** The other two ship separately.

| Cut | What it discloses | This scraper? |
|---|---|---|
| **General Payments** | Meals, travel, consulting, speaker fees, royalties, gifts, grants | ✅ yes |
| **Research Payments** | Clinical-trial sponsorship + research grants | ❌ separate scraper |
| **Ownership/Investment** | Physician-held equity in drug/device companies | ❌ separate scraper |

The supply chain has two parties:

```
   manufacturer / GPO   ──pays──▶   covered recipient
   (Applicable_Manufacturer_…)     (Covered_Recipient_NPI)
```

For the fraud pipeline this is the **Anti-Kickback-Statute (AKS)
proximity** dimension. Outsized manufacturer→physician cash flow
co-occurring with that manufacturer's product appearing in the
physician's Part D prescribing or Part B J-code billing is the
near-diagnostic fingerprint of a kickback scheme. Insys/Subsys
(fentanyl, "speaker bureau" payments to high-volume prescribers),
Novartis (cardiology speaker programs), and Mallinckrodt (Acthar) are
the canonical cases — and several of those defendants appear in
`gold_set.jsonl`.

### Acronyms used in column names

| Acronym | Meaning |
|---|---|
| `Covered_Recipient` | The paid party — a physician, a non-physician practitioner (PA/NP/CRNA/CNS/CNM since 2021), or a teaching hospital |
| `Covered_Recipient_NPI` | NPI of the paid party. **This is the recipient's NPI, not a billing NPI** — every row represents money flowing TO this NPI |
| `Covered_Recipient_Type` | Categorical: "Covered Recipient Physician", "Covered Recipient Non-Physician Practitioner", "Covered Recipient Teaching Hospital". Mapped to `I` / `O` for `format_provider_name` |
| `Covered_Recipient_Specialty_1` | Recipient's primary self-declared specialty |
| `Recipient_State` | 2-letter state of the recipient's listed address (not the manufacturer's) |
| `Applicable_Manufacturer_or_Applicable_GPO_Making_Payment_Name` | The payer's legal entity name. The column lives up to its length |
| `Total_Amount_of_Payment_USDollars` | Dollar amount of this single payment. ~$0.50 for a meal, ~$100k+ for royalty checks |
| `Date_of_Payment` | When the payment was made (string `MM/DD/YYYY` in the raw CSV) |
| `Nature_of_Payment_or_Transfer_of_Value` | ~25 categories: Food and Beverage, Travel and Lodging, Consulting Fee, Compensation for services other than consulting (speaker fees), Royalty or License, Grant, Honoraria, Gift, Charitable Contribution, Current or Prospective Ownership or Investment Interest, … |
| `Name_of_Drug_…_Supply_1` | The product the payment is tied to. Multiple `_1` / `_2` / `_3` slots exist in the raw CSV; we keep only `_1` (the primary association) |
| `Program_Year` | The PY this row is published under. Useful only when a parquet bundles multiple years — our scraper writes one PY per parquet so it's a constant per file |
| `Record_ID` | Unique row identifier per program year. Stable across re-publications of the same PY; safe to use as a dedupe key when merging June + January refreshes |
| `Teaching_Hospital_Name` | Org-name field populated when `Covered_Recipient_Type == "Teaching Hospital"`; null on individual rows |

## 2. Unit of observation

**One row = one disclosed payment.** A single NPI typically appears on
hundreds to thousands of rows per program year (one row per meal, one
row per consulting check, one row per speaking engagement). This is
fundamentally different from Part B / Part D / DMEPOS, which are 1
row per NPI per year.

The 15 raw CMS columns we keep in the projection break into:

- 9 cols of record + recipient identity (`Record_ID`,
  `Covered_Recipient_NPI`, `Covered_Recipient_Type`, first/middle/last
  name, `Teaching_Hospital_Name`, `Covered_Recipient_Specialty_1`,
  `Recipient_State`)
- 4 cols of payer + payment (manufacturer/GPO name, amount, date,
  nature)
- 2 cols of product + program year (`Name_of_Drug_…_Supply_1`,
  `Program_Year`)

After our scrape pass, parquets carry 7 additional canonical columns:
3 base stamps (`source`, `fetched_at_utc`, `schema_version`) + 4
NPI-keyed canonicals (`npi`, `provider_name`, `state`, `data_year`) —
see [`pipeline.base.ProviderRecordSchema`](../../pipeline/base.py).

Total declared schema: **22 columns**.

The raw CMS CSV publishes ~80 columns per row; we drop the rest at
parse time because the per-NPI fraud-feature aggregator (in
`fraudlib/ranker/openpayments_features.py`) doesn't read them. If a
new feature needs e.g. dispute-status, payment-form, or the secondary
product slots, widen `KEEP_COLUMNS` and bump the scraper's
`schema_version`.

## 3. How CMS publishes it (cadence + completeness)

- **Annual release.** Each program year is one PUF.
- **Lag.** ~6 months from end-of-year to first publication (much
  shorter than the 18-month CMS PUF lag). PY2023 was first published
  June 2024 and re-published with dispute resolutions in January 2025
  under a new filename.
- **Twice-yearly refresh cycle.** Each PY gets re-published once or
  twice after its first release as disputes get resolved. The data is
  additive — newer publications include the older content. The URL
  catalog in [`pipeline/scrapers/open_payments.py:URLS`](../../pipeline/scrapers/open_payments.py)
  lists candidate filenames per year, ordered newest first; the
  fetcher probes them in order.
- **No row-level suppression.** Unlike the beneficiary-keyed PUFs
  (Part B, Part D, DMEPOS), Open Payments does not apply a
  small-cell-count floor. Every reportable payment is published.
  Aggregating to per-NPI totals doesn't lose rows to suppression.
- **The data is complete; what's missing is the recipient's claims
  side.** Open Payments shows you the money flowing in; the actual
  prescribing / ordering pattern that money was meant to influence
  is in Part D + Part B + DMEPOS (see §5 for joins).
- **`Delay_in_Publication_Indicator` column.** A small subset of
  payments — those tied to ongoing research or clinical-trial
  confidentiality agreements — can be delayed up to 4 years. The
  indicator column lives in the raw CSV but is not in our projection;
  if a downstream consumer needs it, widen `KEEP_COLUMNS`. Most
  delayed payments are in the Research Payments cut, not General,
  so the effect on this dataset is small.
- **2021 recipient-type expansion.** Before PY2021, "Covered
  Recipients" were physicians + teaching hospitals only. From PY2021
  onward, five additional non-physician practitioner types (Physician
  Assistant, Nurse Practitioner, Certified Registered Nurse
  Anesthetist, Clinical Nurse Specialist, Certified Nurse-Midwife)
  became reportable. This expanded the recipient population but did
  not add new columns to the CSV — the new recipients show up under
  the existing `Covered_Recipient_Non-Physician_Practitioner` type.

## 4. Fraud signals — what to look for

Open Payments is the kickback-proximity dimension. None of the
signals below are diagnostic on their own — a $50k consulting check
to a hand surgeon for actually consulting on a device design is
legal. The signal becomes load-bearing when *cross-referenced* with
the recipient's prescribing/ordering of that manufacturer's product
(§5).

### 4a. Speaker-bureau abuse

A manufacturer pays a physician an outsized fee labeled as "speaker
honoraria" or "compensation for services other than consulting,
including serving as faculty or as a speaker." The recipient is
ostensibly delivering CME lectures on the manufacturer's drug; in
practice the speaker engagements are sparse and the payment is
really a kickback per Rx written. Insys/Subsys is the canonical case
— prescribers received $50k-$300k/year in "speaker" payments and
billed Medicare for Subsys (sublingual fentanyl spray) prescriptions
at thousands of times the median prescriber rate.

**Column signatures:**
- `Nature_of_Payment_or_Transfer_of_Value` is one of the
  "Compensation for services other than consulting…" categories
  AND `Total_Amount_of_Payment_USDollars` aggregated per NPI per
  manufacturer per year > $50k
- The same manufacturer's flagship drug appears in the same NPI's
  Part D `Brnd_Tot_Drug_Cst` at an extreme share of `Tot_Drug_Cst`
- Specialty (`Covered_Recipient_Specialty_1`) matches the drug's
  indication (pain management for opioids, cardiology for statins,
  psychiatry for atypical antipsychotics)

Canonical cases: **Insys Therapeutics / Subsys** (fentanyl spray —
several defendants in `gold_set.jsonl`), **Novartis** (cardiology
speaker programs), **Mallinckrodt / Acthar** (high-amount speaker
fees correlated with Part D Acthar share).

### 4b. Royalty schemes (surgical devices)

A device manufacturer pays a surgeon a royalty for licensing the
surgeon's design contribution; the surgeon then implants the device
extensively in their own practice. Spine and orthopedics are the
hot spots. Some royalty payments are legitimate (Charité
disc-replacement contributors really did invent the device); the
fraud signal is when the implant volume far exceeds peer surgeons
who don't collect royalties.

**Column signatures:**
- `Nature_of_Payment_or_Transfer_of_Value == "Royalty or License"`
  with per-NPI per-manufacturer per-year totals > $25k
- Specialty in {Orthopedic Surgery, Neurosurgery, Spine Surgery}
- Cross-source: Part B HCPCS code mix dominated by the implant's
  associated CPT (visible only in `cms_partb_svc_*`)

### 4c. Consulting concentration

A single manufacturer paying the same physician > $25k/year as
"Consulting fee." That threshold is empirically where DOJ has
historically scrutinized — below that level "consulting" is too
plausible to single out; above it requires the physician to be
demonstrably *not* actually consulting (which discovery sometimes
shows is the case).

**Column signatures:**
- `Nature_of_Payment_or_Transfer_of_Value == "Consulting fee"` AND
  per-NPI per-manufacturer per-year aggregated > $25k
- Concentration on one manufacturer (peer percentile of "share of
  total OP receipts from top manufacturer" > 95th)

### 4d. Food-and-beverage cohort effects

Individual meals are low-dollar (~$50 each) and not load-bearing
for individual-NPI fraud. They become useful as a **peer-percentile
feature**: physicians in the top 1% of total annual food-and-beverage
receipts from a single manufacturer are over-represented in DOJ
detailing-related cases. Use as a categorical feature alongside the
high-dollar nature-of-payment buckets.

**Column signatures:**
- `Nature_of_Payment_or_Transfer_of_Value == "Food and Beverage"`
  AND row count per NPI per manufacturer > peer 99th percentile
- The drug named in the rows (`Name_of_Drug_…_Supply_1`) matches the
  recipient's Part D top brand

### 4e. Travel + lodging anomalies

`Nature_of_Payment_or_Transfer_of_Value == "Travel and Lodging"`
where the cumulative annual per-manufacturer total > $5k AND the
travel was cross-state. Co-occurs with the manufacturer's product
share in the recipient's prescribing.

### 4f. Investment / equity disclosures

Disclosed ownership stakes in manufacturers whose drugs the
physician prescribes. Rare but near-diagnostic when present
(legitimate investment in a competitor's drug is fine; investment
*in* the drug you prescribe extensively isn't).

**Column signatures:**
- `Nature_of_Payment_or_Transfer_of_Value == "Current or Prospective
  Ownership or Investment Interest"` AND the manufacturer's product
  appears in the recipient's Part D top-10 drugs

Note: this nature-of-payment value is rare in the General Payments
cut — most ownership disclosures live in the separate Ownership cut
(not scraped here). The few that surface in General are typically
secondary ownership disclosures attached to a primary payment.

## 5. Joins to other sources

NPI (`npi`, zero-padded to 10) is the universal join key. The
interesting joins are **reconciliation joins** — proving a kickback
by showing the manufacturer→physician payment co-occurs with that
manufacturer's product appearing in the physician's
prescribing/ordering.

| Join target | Why |
|---|---|
| `cms_partd_YYYY.parquet` | Brand-vs-generic share + opioid concentration. The strongest reconciliation lane — manufacturer payments combined with the physician's brand-share for that manufacturer's drug is the direct AKS signal |
| `cms_partb_YYYY.parquet` | J-code drugs administered in office (oncology, rheumatology, ophthalmology, pain management). Manufacturer payments + Part B J-code volume = office-administered AKS pattern |
| `cms_partb_svc_YYYY.parquet` | Per-HCPCS detail; required to identify the *specific* J-code that ties to the manufacturer's product |
| `cms_dmepos_YYYY.parquet` | Device-royalty schemes + DMEPOS referrals for that device class |
| `nppes_features_*.parquet` | Specialty / taxonomy ground-truth (more reliable than the self-declared `Covered_Recipient_Specialty_1`) |
| `gold_set.jsonl` | Known-fraud label join |

Pitfalls when joining:

- **The NPI is the recipient's, not a biller's.** A row with
  NPI=1234567890 means money flowed *to* that NPI. The biller (the
  manufacturer) is not in NPPES. Don't try to join the manufacturer
  name to NPPES.
- **NDC → manufacturer mapping is not in this dataset.** Open
  Payments tells you the manufacturer name and the product name; the
  drug appearing in the recipient's Part D `cms_partd_drug_*`
  per-NDC file (when that scraper exists) is identified by NDC
  (National Drug Code). To bridge the two you need a separate
  NDC→manufacturer lookup (the FDA's NDC Directory). That mapping
  is out of scope for this PR; flag downstream consumers that the
  reconciliation join is incomplete without it.
- **Drop NPI-less rows before joining.** Teaching-hospital rows
  without an NPI are kept out of the projection at parse time
  because they can't join. If a downstream consumer cares about
  hospital-level aggregation, use `Teaching_Hospital_Name` to roll
  up; that's still in the projection.
- **Same NPI, multiple rows.** Always aggregate to per-NPI totals
  before joining onto a 1-row-per-NPI source like Part B / Part D.
  Naive joins fan out enormously (one Part D row × thousands of OP
  rows = millions of result rows).

## 6. Year coverage and schema drift

| Years | Rows (approx) | Notes |
|---|---|---|
| 2013 | ~4.4M | **Partial year — Aug-Dec only**; first program year post-Sunshine-Act |
| 2014 | ~11M | First full PY |
| 2015-2020 | ~11-13M each | Steady-state |
| 2021-2024 | ~13-15M each | Recipient-type expansion (PA/NP/CRNA/CNS/CNM disclosure) widened the recipient pool |

The 15 columns we keep have been stable across all 12 program years:
no rename, no addition, no removal of any column in our projection
set. The 2021 recipient-type expansion added *recipients* but did
not add or rename columns in the General Payments CSV header — the
new non-physician practitioners flow into the existing
`Covered_Recipient_Type` column with new categorical values.

**Schema-drift probe gap.** The planned union-of-columns probe across
2013-2024 was not runnable in the scraper PR because no
`open_payments_general_*.parquet` files existed locally at the time
(only an unstamped 2024 file in the bucket, not pulled). The
projection is based on the existing
`fraudlib/ingest/providers/openpayments.py` codebase and CMS
methodology PDF; if the post-merge backfill surfaces a missing
column for a given year, `_align_to_schema` will NaN-pad it. Unknown
columns at parse time will print a warning but not fail — they're
filtered by `usecols` at `read_csv` time.

**PY2013 partial-year normalization.** PY2013 covers only Aug-Dec
2013. Any aggregator that produces "annualized payment receipts" must
either (a) scale PY2013 by 12/5, or (b) drop PY2013 from
year-comparison features. The raw parquet leaves the partial-year
condition as a data fact (a small `Date_of_Payment` range); the
downstream feature module is responsible for normalization.

CMS's strict `_validate()` will still reject any parquet with
unknown columns — intentional, so upstream drift surfaces at the
producer instead of as a silent `KeyError` deep in the ranker.

### 6a. Dtype contract (Pandera) and observed per-year dtype drift

As of 2026-06 the schema is a Pandera `DataFrameModel`
([`pipeline/base.py`](../../pipeline/base.py)). `_validate()` now does
three things in one pass: **coerce** each column to its declared dtype,
**reject** undeclared columns (strict), and enforce **nullability + value
checks**. So column *dtype* drift — not just column presence — fails at
the producer instead of shipping to R2.

This matters because the historical R2 parquets carried real per-year
dtype drift (a `DOUBLE`-typed NPI silently breaks an exact string match —
the bug that motivated this contract). Observed drift on
`r2:needlepoint-data/processed/v1/open_payments_general_*.parquet`:

| Column | R2 drift (pre-backfill) | Schema target | Cast |
|---|---|---|---|
| `Covered_Recipient_NPI` | VARCHAR (2013–17) → **DOUBLE** (2018–23) → VARCHAR (2024) | `str` `^(\d{10})?$` | **int-hop** `CAST(CAST(x AS BIGINT) AS VARCHAR)` |
| `Total_Amount_of_Payment_USDollars` | VARCHAR (2013–17) → DOUBLE (2018–24) | `float` ≥ 0 | plain `→ DOUBLE` |
| `Record_ID`, `Program_Year` | VARCHAR ↔ BIGINT | `str` | plain `→ VARCHAR` |
| `Date_of_Payment` | VARCHAR → TIMESTAMP (2024) | `str` | plain `→ VARCHAR` |
| `Name_of_Drug_..._Supply_1` | INTEGER (2013–15) → VARCHAR | `str` | plain `→ VARCHAR` |

**NPI handling.** The canonical `npi` column is clean `VARCHAR` on every
year (it was derived `int64 → str`, i.e. the int-hop, at build time) —
**always join on `npi`, never the raw `Covered_Recipient_NPI`**, which
drifted to `DOUBLE`. The `.0` trap: a float NPI cast straight to text
becomes `"1234567890.0"` and silently matches nothing; the schema's
`^(\d{10})?$` check now rejects that at write-time, and the backfill
casts through `BIGINT` first.

**Backfilling the historical parquets** to this contract:
[`pipeline/scripts/recast_r2_dtypes.py`](../../pipeline/scripts/recast_r2_dtypes.py)
derives the per-column cast from this schema (int-hop applied
automatically for float→string targets), rewrites R2 in place via DuckDB,
and re-verifies. `--execute` to swap; default is a dry-run.

## 7. Known limitations

- **Multi-row, not 1 row per NPI.** Naive joins onto 1-row-per-NPI
  sources fan out. Always aggregate first.
- **NDC → manufacturer mapping is out of scope.** Without it the
  reconciliation join from Open Payments (manufacturer name) to
  Part D Drug (NDC) is incomplete. The FDA NDC Directory is the
  bridge; landing it in `data/registries/` is a separate workstream.
- **PY2013 is 5 months, not 12.** Don't compare PY2013 totals to
  later years without scaling. See §6.
- **Three sibling cuts not scraped here.** Research Payments and
  Ownership/Investment ship as separate scrapers. Some kickback
  patterns (especially equity-based) live in the Ownership cut, not
  General.
- **Teaching-hospital rows without NPI are dropped.** Hospital-level
  AKS investigation is possible via `Teaching_Hospital_Name` (kept
  in the projection) but the join doesn't go through the canonical
  `npi` column.
- **Recipient specialty is self-declared.** Same caveat as Part B
  / Part D — `Covered_Recipient_Specialty_1` is provider-reported and
  has the usual long-tail noise. Prefer NPPES taxonomy for downstream
  features that key off specialty.
- **`Delay_in_Publication_Indicator` not in projection.** A small
  subset of payments tied to research/clinical-trial confidentiality
  can be delayed up to 4 years. If a downstream feature wants to
  flag these, widen `KEEP_COLUMNS` and bump `schema_version`. Most
  delayed payments are in the Research cut, not General.
- **No suppression.** Unlike beneficiary-keyed PUFs, OP doesn't
  suppress small cells. Don't write code that expects a suppression
  flag.
- **`Nature_of_Payment_or_Transfer_of_Value` has long-form values
  that CMS rewords across years.** "Compensation for services other
  than consulting, including serving as faculty or as a speaker at
  a venue other than a continuing education program" is one of
  several speaker-related variants. The downstream feature module
  (`openpayments_features.py:_NATURE_BUCKETS`) maps to short keys;
  add to that map when CMS introduces a new wording, not here.

## 8. File contract

After running [`OpenPaymentsScraper`](../../pipeline/scrapers/open_payments.py):

```
data/panels/open_payments_general_YYYY.parquet              # 22 cols, ~4.4M-15M rows
data/panels/open_payments_general_YYYY.manifest.json        # RunResult: source, schema_version, columns, fetch URL
```

Manifest fields are documented on
[`pipeline.base.RunResult`](../../pipeline/base.py). Loaders read the
manifest at load time, compare `schema_version`, and warn on mismatch.

Per-row size is dominated by the manufacturer name + drug name +
nature-of-payment string columns. Compressed parquet size: ~80 MB
(PY2013, partial) → ~500 MB (PY2023, full).
