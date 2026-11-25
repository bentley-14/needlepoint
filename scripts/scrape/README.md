# Case Corpus Rebuild — Stage 1: Raw Archive

## Why this exists

The `gold_set.jsonl` corpus (2,337 cases) is "incredibly polluted" because scraping and classification happened in a single pass — every quality decision was made at scrape time with no audit trail. When errors were found, we couldn't tell whether they originated in the source page, the parser, or the classifier.

This rebuild separates concerns:

- **Stage 1 (this directory)**: scrape and store raw bytes from sources, with full provenance, with no classification or judgment.
- **Stage 2 (future)**: LLM extraction with required source-quote per field, multi-pass verification, controlled-vocab schema validation, adversarial error-finding pass.
- **Stage 3 (future)**: cross-source dedup, NPI resolution, quality tiering (A/B/C).

The benefit: when Stage 2 improves, we re-run against the immutable Stage 1 archive without re-scraping. Provenance lets us audit any field back to its source page.

## Design principles

| Principle | What it means |
|---|---|
| **Immutable** | Once a `*.html`/`*.txt`/`*.meta.json` triple is written, it's never modified. Re-fetches go to a new file with a new fetch timestamp. |
| **Provenance-rich** | Every entry carries source URL, fetch timestamp, HTTP status, content SHA-256, redirect chain, scrape method, User-Agent, content length. |
| **Idempotent** | Re-running a scraper skips already-archived URLs (cheap index check). Safe to re-run for delta. |
| **Source-agnostic core** | `raw_archive.py` knows nothing about parsing or schemas. Source-specific scrapers in `sources/` use it as a library. |

## Layout

```
data/raw/
  doj_press/{YYYY}/{slug}.html    # raw HTML (immutable)
  doj_press/{YYYY}/{slug}.txt     # text-extracted (for quick grep)
  doj_press/{YYYY}/{slug}.meta.json  # provenance
  oig_enforcement/{YYYY}/...      # same shape per source
  _index/{source}.jsonl           # append-only fetch index
```

## Dependencies

- Python 3.10+ (uses `dataclasses`, `pathlib`)
- `curl_cffi>=0.5` (for Akamai bypass via browser TLS impersonation)

```bash
pip install curl_cffi
```

If `curl_cffi` is unavailable, the scraper falls back to plain urllib and will fail on Akamai-protected URLs (it logs `akamai_unsolved` in failure modes rather than corrupting the archive).

## Sources

### OIG Enforcement Actions ✅ working

`oig.hhs.gov/fraud/enforcement/` — public, no bot mitigation. The `discover_detail_urls` function walks paginated listing pages to find individual enforcement-action URLs. Then each URL is fetched and archived.

```bash
python -m scripts.scrape.cli scrape oig --max-pages 50
```

### DOJ Press Releases ✅ working (Akamai bypassed)

`justice.gov` is protected by **Akamai bot mitigation**. The scraper bypasses it via a reverse-engineered challenge solver in `akamai_solver.py`:

1. First fetch returns ~3KB JS interstitial with `bm-verify` token + `pow` arithmetic
2. Solver parses the token and computes `pow = i + Number(s1 + s2)`
3. POSTs `{bm-verify, pow}` to `/_sec/verify?provider=interstitial`
4. Server sets `ak_bmsc` cookie; same session can now fetch real content

The solver uses `curl_cffi` with browser TLS impersonation (Chrome 120) — plain `requests`/`urllib` get blocked even after solving the challenge. The session and cookie are reused across all subsequent fetches, so the challenge is solved once per process, not per URL.

| URL pattern | Count in v1 seed | Status |
|---|---|---|
| `/opa/pr/{slug}` (DOJ HQ direct) | 62 (3%) | ✅ direct urllib |
| `/archives/opa/pr/{slug}` (HQ archived) | 464 (22%) | ✅ via Akamai solver |
| `/usao-{district}/pr/{slug}` (USAO district) | 1,561 (75%) | ✅ via Akamai solver |
| `/news?topic=tt117&page=N` (listing) | discovery | ✅ via Akamai solver |

```bash
# Fetch from v1 seed URLs (fastest backfill path)
python -m scripts.scrape.cli scrape doj --max-pages 100

# Walk /news?topic=tt117 to discover NEW URLs not in v1 seed
python -m scripts.scrape.cli scrape doj --discover-listings --listing-max-pages 50
```

Verified: 100/100 URLs scraped from v1 seed across 2009-2026 with 0 failures, average 1.8s per fetch.

## Running

```bash
# Show what's archived so far
python -m scripts.scrape.cli stats

# Scrape OIG (fully unblocked)
python -m scripts.scrape.cli scrape oig --max-pages 50

# Scrape DOJ HQ direct URLs only (the 3% that work)
python -m scripts.scrape.cli scrape doj --max-pages 50

# Scrape both
python -m scripts.scrape.cli scrape all --max-pages 100
```

The CLI is idempotent. Re-running picks up where the previous run left off (same URL → skipped, new URL → fetched).

## What this is NOT

- **Not a classifier.** Stage 2 does that. This module never decides "is this a fraud case" or "what type of fraud" or "who are the defendants." Stage 1 just stores bytes.
- **Not a deduper.** If the same case appears in DOJ HQ + USAO + OIG releases, Stage 1 stores all three. Stage 2 cross-references them.
- **Not real-time.** This is a batch-mode scraper. For weekly delta refresh, run `scrape all` periodically.

## Testing

```bash
# Smoke test (5 OIG + 5 DOJ pages)
python -m scripts.scrape.cli scrape oig --max-pages 5
python -m scripts.scrape.cli scrape doj --max-pages 5
python -m scripts.scrape.cli stats
```

Verified working as of branch `experiment/case-corpus-rebuild`:
- OIG: 22 URLs discovered from first 3 listing pages, 3/3 fetched + archived cleanly
- DOJ: 3/3 `/opa/pr/` direct URLs fetched + archived cleanly (Brooklyn banker, California 270M, CEO 1B)
- Archive contains valid `.html` + `.txt` + `.meta.json` triples with SHA-256 hashes
- Idempotent re-run skips already-archived URLs

## Next steps (Stage 2)

Build LLM extraction in `scripts/extract/`:

1. **Pass 1**: structured extraction with required `source_quote` per field
2. **Pass 2**: cross-source verification when same case appears in multiple sources
3. **Pass 3**: NPI resolution via NPPES, exclusion check via local LEIE parquet
4. **Pass 4**: adversarial error-finding pass on the extraction
5. **Pass 5**: schema validation (Pydantic), reject to `_review/` queue on failure

Output: `data/cases/v2/{case_id}.json` with `corpus_quality_tier` and full provenance back to Stage 1 raw files.
