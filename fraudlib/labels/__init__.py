"""Label loaders — read from disk only, never the network.

This package owns the y=1 side of the ML pipeline: who is a known
fraudster, by what source, with what split.

Modules:
  - ``ground_truth`` — case-based positives loader (delegates to v4_gold).
  - ``v4_gold`` — canonical reader for ``data/extracted/gold_set.jsonl``.
  - ``labels_v2`` — case-based label joiner (legacy; most callers should
    use ``v4_gold`` / ``ground_truth`` against gold_set instead).
  - ``nppes_lookup`` — per-NPI NPPES API helper used during corpus curation.
  - ``lint`` — schema/lint helpers for case JSONL rows.

This package replaces the old ``fraudlib.ingest.labels`` and the
loader-side bits of ``fraudlib.ingest.cases``. Producer code (fetching
gold_set from R2, corpus_curator, etc.) lives under ``pipeline/``.
"""
