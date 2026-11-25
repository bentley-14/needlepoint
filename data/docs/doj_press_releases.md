# DOJ Press Releases — healthcare-fraud discovery source

**Scraper:** not included in this repository; the data ships in the public dataset.
**Schema reference (columns + dtypes):** [docs/DATA_SCHEMAS.md](../../docs/DATA_SCHEMAS.md)
**Files:** `data/source/doj_api/curator_input.jsonl` (the curator's input
list) + `curator_input.manifest.json` (framework `RunResult` sidecar) ·
`data/source/doj_api/raw_press_releases.jsonl` (warm replay cache)

## 1. What this dataset is, in plain English

This is **not a billing dataset** — it's the **label-discovery source**. The
U.S. Department of Justice publishes a press release whenever a U.S.
Attorney's Office announces a healthcare-fraud charge, plea, conviction, or
civil settlement. Each release names the defendant(s), describes the scheme,
and usually states the fraud period and the dollar amount. Those press
releases are the raw material from which the **`gold_set.jsonl`** ground-truth
labels are built: a named defendant → resolved to an NPI → becomes a `y=1`
training label for the ranker.

This scraper is **link 1** of the four-link DOJ → gold_set pipeline:

```
1. DISCOVERY  (this scraper)  DOJ API title-sweep + body-gate → curator_input.jsonl + warm cache
2. EXTRACT    (gold_set_builder)  corpus_curator agent reads curator_input → structured proposals
3. POST-PROC  (gold_set_builder)  NPPES-verify NPI · outcome-cap · billing-surface check
4. MERGE      (gold_set_builder)  identity-merge into data/extracted/gold_set.jsonl (drift-guarded)
```

It does **discovery only** — it finds the relevant press releases and decides
which are worth handing to the (expensive) extraction agent. It does **not**
extract defendants, resolve NPIs, or touch `gold_set.jsonl`.

### Acronyms / terms

| Term | Meaning |
|---|---|
| `DOJ` | Department of Justice — publisher of the press releases |
| `USAO` | U.S. Attorney's Office — the district office that issues most releases (the `component` field) |
| `OPA` | Office of Public Affairs — main-DOJ releases (national takedowns) |
| `FCA` | False Claims Act — the civil statute behind most settlement cases |
| `AKS` | Anti-Kickback Statute — criminal kickback / bribe-for-referral cases |
| `NPI` | National Provider Identifier — the join key a release must ultimately resolve to |
| `doc_id` | deterministic 16-hex hash of the release URL — the dedup primary key |
| `body gate` | the local Medicare/FCA relevance filter applied to each release body |

## 2. Unit of observation

**One row = one DOJ press release** (one URL / `doc_id`), discovered in this
run and not previously in the warm cache. The row is a *pointer + discovery
decision*, not the release text:

- identity: `url`, `doc_id`, `uuid`, `published_date`, `title`, `component`
- discovery decision: `matched_terms` (which title terms hit), `passed_body_gate`
- `body_chars` (length of the cached body — a sanity / staleness check)

**The release body is not in `curator_input.jsonl`** — it lives in the warm
cache (`raw_press_releases.jsonl`), keyed by `url`. `curator_input.jsonl` is
the curator's **input list**; the curator filters it to
`passed_body_gate == True` and reads the body from the cache.

Two important non-1:1 mappings (handled downstream, not here):

- **One case spans multiple releases.** An indictment, a plea, and a
  sentencing are three releases (three rows) for one defendant. Dedup is by
  `doc_id`, so all three appear. Collapsing them to one case is a
  *merge*-time (link 4) concern.
- **One release names multiple defendants.** A takedown release can name
  dozens. Splitting a release into per-defendant cases is the *extraction*
  agent's job (link 2).

## 3. How DOJ publishes it (cadence + the API's quirks)

- **Continuous publication.** Releases drop daily but **lumpily** — strike-
  force "takedowns" dump a batch of cases at once. This is why the cron is
  **weekly** (frequency isn't the recall lever; DOJ's publishing rate is).
- **Source: the public JSON API**, `https://www.justice.gov/api/v1/press_releases.json`
  — unauthenticated, ~4 req/sec, 50 results/page, returns full `body` HTML
  inline. Fields: `uuid`, `title`, `date` (unix-epoch string), `body`,
  `teaser`, `url`, `component`, `topic`.
- **Akamai:** the JSON API is served by **nginx and is *not* behind the
  Akamai bot-wall** that guards the HTML press-release pages (verified
  2026-05-31: plain `requests` → 200 + full body). Because the API returns
  body inline we never scrape the Akamai-guarded HTML. (Fallback if DOJ ever
  walls the API: [`scripts/browser_fetch.py`](../../scripts/browser_fetch.py),
  a headless browser — slow, avoided by design.)
- **Two verified API bugs the scraper works around:**
  - `date_>=` / `date_<=` server-side filters are **silently ignored** (the
    API returns the all-time corpus regardless). The scraper sorts
    `date DESC` and **filters client-side**, stopping pagination once
    releases predate `since_date`.
  - `body=` search is **silently ignored** too — body filtering is local-only,
    which is exactly what the body gate does over fetched bodies.
- **"Suppression" equivalent — there is none.** Unlike CMS PUFs, DOJ does not
  redact low-count cells. The analogue here is *recall loss*: cases that exist
  but the title sweep never surfaces (see §7).

## 4. Fraud signals — what to look for

For a discovery source, "signals" = **which schemes the title vs. body
reveals**, because that drives what the term list + gate must catch. Measured
2026-05-30 against the existing 5,937-release cache and the gold_set:

### 4a. Title vocabulary ≠ fraud vocabulary (the core finding)

DOJ titles describe cases in **enforcement** language, not **fraud-scheme**
language. The schemes that leak past a naïve phrase-search title list:

| Scheme | How DOJ titles it (no "fraud"/"medicare" in title) |
|---|---|
| Lab kickback / bribe-for-referral | "Doctor Admits Taking **Bribes** in **Test-Referral** Scheme" |
| Pill-mill / diversion | "Illegally **Distributing** Drugs", "Dispensing **Oxycodone**" |
| Civil FCA settlement | "Pays $X to **Settle** False Claims **Allegations**" |
| Generic sentencing | "St. Louis Doctor **Sentenced** to 70 Months" (zero offense vocab) |

The token-based `TITLE_QUERY_TERMS` (e.g. `bribe`, `referral`,
`controlled substance`, `unlawful distribution`) recover most of these; the
generic-title cases are recoverable **only** because the API tokenizes title
queries loosely and the **body gate** then confirms "Medicare" in the body.

### 4b. The body gate (precision recovery)

A title term like `kickback` / `controlled substance` over-pulls
*non-healthcare* cases (public-corruption bribery, street-drug distribution).
The body gate (`passes_body_gate`)
requires **≥1 anchor** (`medicare`/`medicaid`/`tricare`/`false claims act`/…)
**AND ≥2 billing keywords**. Validated: retains **96.4%** of true gold-labeled
releases while dropping **~21%** of marginal over-pull. Live example: it keeps
"…Botox Fraud Scheme Targeting **Medicare**" (no literal title term) and
rejects "Elkhart **Cocaine** Distributors…**Controlled Substance** Offenses".

### 4c. `topic` tag (a precision booster, not a discovery axis)

The API returns a `topic` array; "Healthcare Fraud" is the dominant tag — but
only **46%** of relevant releases carry it and **34% are untagged**, so topic
filtering can *confirm* relevance but cannot *find* cases on its own. Not used
in v1.

## 5. Joins to other sources

This source is **text, keyed by URL** — it has no NPI yet. It joins to the
rest of the pipeline only *after* the extraction agent resolves a defendant
to an NPI:

| Join target | Key | Why |
|---|---|---|
| warm cache `raw_press_releases.jsonl` | `url` / `doc_id` | `curator_input` points here for the body text |
| `gold_set.jsonl` | `source_url` | dedup — skip releases whose URL is already a label |
| NPPES (via `scripts/nppes_lookup.py`) | defendant name + state | the extraction step resolves name → NPI |
| CMS panels (Part B/D/DMEPOS) | resolved `npi` × `active_years` | the billing-surface check + eventual label join |

Pitfall: the `component` (prosecution district) is **not** the provider's
practice state — don't use it for NPPES disambiguation. Use the
defendant's address as stated in the body.

## 6. Year coverage and drift

- The warm cache currently spans **2009-01-08 → 2026-04-28** (5,937
  healthcare-fraud releases), built one-time by
  `scripts/ingest/doj_api_ingest.py`.
  This scraper extends it forward on each weekly run.
- Cache distribution is roughly flat: ~450 releases/yr 2013–2019, ~355/yr
  2020–2023. The post-2019 dip is real (prosecution volume), not a search
  artifact.
- **State tracking:** the warm cache *is* the state. `since_date` defaults to
  `max(published_date in cache) − lookback_days` (~30-day overlap); `doc_id`
  dedup makes the overlap idempotent. No separate state file. Pass
  `since_date=` to backfill.

### 6a. Dtype contract (Pandera) and per-year dtype drift

As of 2026-06 the schema is a Pandera `DataFrameModel`
([`pipeline/base.py`](../../pipeline/base.py)). `_validate()` does three things
in one pass: **coerce** each column to its declared dtype, **reject** undeclared
columns (strict), and enforce **nullability + value checks**. So column *dtype*
drift — not just column presence — now fails at the producer instead of shipping
silently downstream.

The important difference from the numeric CMS panels: **this is not a numeric
parquet panel — it's a URL-keyed document store** validated against
`DocumentSchema` (the typed `url` / `doc_id` / `publisher` / `published_date`
base plus this source's own DOJ-extra fields). `curator_input.jsonl` is the
strict-validated artifact; the curator frame is coerced + checked at write-time.
There is **no per-year numeric dtype drift to backfill here** — there are no
float-vs-double columns and no NPI column at this stage (NPI is resolved
downstream in link 2/3, not present in this artifact). What the contract buys
this source is purely **column-presence + nullability + value-check** enforcement
on the typed document fields (e.g. `url` non-null, `published_date` parseable,
`passed_body_gate` boolean), surfaced at the producer.

| Column | R2 drift (pre-backfill) | Schema target | Cast |
|---|---|---|---|
| (none) | no observed per-year dtype drift — document store, not a numeric panel | — | — |

`raw_press_releases.jsonl` (the broad replay corpus, §8) is deliberately
**not** the strict-validated artifact, so the contract does not constrain it.

For the numeric CMS panels that *do* carry historical R2 dtype drift, the
backfill tool is
[`pipeline/scripts/recast_r2_dtypes.py`](../../pipeline/scripts/recast_r2_dtypes.py) (schema-derived
casts — int-hop `CAST(CAST(x AS BIGINT) AS VARCHAR)` for float→string targets,
plain `CAST` otherwise; `--execute` to swap, default dry-run). It has nothing to
recast for this source.

## 7. Known limitations

- **Structural recall ceiling (~25–35%).** Title-search covered only **64.6%**
  of known gold_set label URLs at last measurement. The token list + body gate
  recover most of the rest, but a residual remains unreachable by title search:
  - **generic titles** with no healthcare vocabulary (only the body reveals
    Medicare) — partly recovered via loose API tokenization + body gate, but
    not reliably;
  - **non-press-release-provenance** labels (LEIE upgrades, `United States v.`
    court captions) that never came from the press-release API at all.
  Pushing past the ceiling needs **topic+component union pulling** (pull all
  releases from healthcare-active USAOs, body-filter locally) — a separate,
  lower-precision workstream, plus the planned **exploratory web-search agent**
  (discovery source #2). Documented, not yet built.
- **`matched_terms` is a substring check;** the live API matches more loosely
  (tokenized), so a curator_input row can have `matched_terms` empty yet still be a
  legitimate discovery. Don't treat empty `matched_terms` as "shouldn't be here".
- **Body gate is heuristic.** It will let through the occasional non-Medicare
  case and (rarely) drop a real one (~3.6% of true labels). The extraction
  agent + human review are the real precision gate; this just bounds the
  worklist.
- **No outcome/active-years here.** Those are extracted downstream. This
  curator_input only says "this release looks like a Medicare-fraud case worth
  reading."

## 8. File contract

After running `DojPressReleasesScraper`:

```
data/source/doj_api/curator_input.jsonl          # curator's input list: 1 row per newly-discovered release
data/source/doj_api/curator_input.manifest.json  # framework RunResult sidecar: source, since_date, n_new, n_passed_gate, schema
data/source/doj_api/raw_press_releases.jsonl     # warm replay cache (full release JSON incl. body) — side-effect upsert
```

`curator_input.jsonl` columns (14): the base stamps (`source`, `fetched_at_utc`,
`schema_version`) + `DocumentSchema` (`url`, `doc_id`, `publisher`,
`published_date`) + DOJ extras (`title`, `component`, `teaser`, `uuid`,
`matched_terms`, `passed_body_gate`, `body_chars`). `RunResult` fields are
documented on [`pipeline.base.RunResult`](../../pipeline/base.py).

**Not validated by the schema:** `raw_press_releases.jsonl` is the broad replay
corpus (a mirror of the DOJ API payload), updated as a side-effect — it is
deliberately *not* the strict-validated artifact.
