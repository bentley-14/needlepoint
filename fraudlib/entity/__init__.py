"""Entity resolution — panel-wide NPI -> entity/ring clustering.

Generalizes ``scripts/ring_enrichment.py``'s per-NPI, on-demand entity lookup
into a panel-wide graph so rings/orgs/suites become first-class objects
(``entity_id``) rather than something re-derived per investigation.

Modules:
  - ``resolve`` — connected-components entity graph (shared Org_PAC_ID +
    shared practice address; PECOS reassignment deferred, see module docstring).
"""
