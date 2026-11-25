"""V3 data-driven rule registry (59 rules from set_cover_rules_v3.json).

The verified walk-forward pipeline (PRs #58–63) uses these rules via
``_apply_rules_get_fires`` in the training scripts. The agent layer
uses ``rule_hits()`` to fire rules against individual NPIs.

The prior 14 hand-authored rules are archived in stale/registry_rules/.
"""
from fraudlib.rules.registry import (  # noqa: F401
    RULES,
    RULES_RAW,
    Rule,
    FraudType,
    evaluate,
    filter_top,
    summarize,
    add_peer_percentiles,
    rule_hits,
)
