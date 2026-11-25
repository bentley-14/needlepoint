"""Data pipeline — produces files on disk consumed by fraudlib.

The arrow is one-way: ``pipeline`` writes parquets / JSONL / scraped
HTML to ``data/`` (and R2 via ``make push-data``); ``fraudlib`` reads
them. Nothing under ``fraudlib`` should call out to the network — that's
this package's job.

Layout:
  - ``pipeline.base`` — :class:`BaseScraper` ABC + schema hierarchy.
    Every producer subclasses one of the schema classes here.
  - ``pipeline.scrapers`` — one module per external source. Each is a
    :class:`BaseScraper` subclass that fetches + validates + writes one
    parquet plus a sibling manifest.
  - ``pipeline.transforms`` — local file → local file producers
    (e.g. the DOJ ``gold_set.jsonl`` curator chain, derived feature
    builders). Empty until the first transform lands.
  - ``pipeline.extract`` — shared HTML / PDF / text utilities used
    inside ``DocumentSchema`` scrapers (trafilatura, pymupdf4llm,
    section-aware chunking, unified policy-corpus normalization).
"""
