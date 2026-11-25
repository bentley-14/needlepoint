"""Transform producers — read one or more local parquets/JSONLs, produce
a new one.

Every transform will inherit from :class:`pipeline.base.BaseTransform`
(added when the first transform lands). Initial members of this package
will be:

- ``curator/`` — the DOJ → ``gold_set.jsonl`` chain (LLM-assisted case
  extraction, NPI resolution, enrichment, outcome cap).
- Derived feature builders that consume CMS panels (e.g. HCPCS-rich,
  DMEPOS-rich).

Empty until the first transform is implemented.
"""
