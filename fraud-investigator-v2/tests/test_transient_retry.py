"""Transient API-failure retry — a network blip must not kill a multi-hour run.

Pins the 2026-06-12 Maple failure: counsel_rebuttal hit "API Error: the
socket connection was closed unexpectedly", claude -p exited 1, and the run
hard-raised — losing ~1h40m. Transient upstream failures (socket drop,
overloaded, rate-limit) are now retried by resuming the session; they are
distinct from error_max_turns (focused finalize) and parse errors (re-prompt).
"""
from __future__ import annotations

from fraud_investigator.claude_p import (
    is_hard_stop_failure,
    is_transient_api_failure,
    transient_backoff_sec,
)


def test_maple_exact_error_is_transient():
    assert is_transient_api_failure(
        "API Error: The socket connection was closed unexpectedly")


def test_transient_markers():
    for s in ["connection reset by peer", "ECONNRESET", "Overloaded",
              "HTTP 529", "Error 503", "rate_limit exceeded",
              "Internal Server Error", "service temporarily unavailable",
              "read timed out", "broken pipe", "connection error"]:
        assert is_transient_api_failure(s), s


def test_non_transient_not_matched():
    # these have their OWN handling paths — must NOT be treated as transient
    for s in ["error_max_turns", "error_during_execution",
              "ValidationError: field required", "exit 1", "",
              "no 'result' event in stream", "command not found"]:
        assert not is_transient_api_failure(s), s


def test_multi_arg_any_match():
    assert is_transient_api_failure("clean stderr", "", "overloaded")
    assert not is_transient_api_failure("a", "b", "c")


def test_backoff_grows_and_caps():
    assert 3.0 <= transient_backoff_sec(1) <= 4.0
    assert 6.0 <= transient_backoff_sec(2) <= 7.0
    assert 12.0 <= transient_backoff_sec(3) <= 13.0
    assert transient_backoff_sec(10) <= 31.0   # capped at 30 + jitter


# ── Hard stop: org monthly spend limit (billing wall) ───────────────────────
# Bradley CASE-6A2D49E8-2026 (2026-06-13): the org spend-limit kill surfaced as
# a result event with subtype="error_during_execution" (a "recoverable" subtype)
# and was NOT matched by the transient detector — so the streaming path burned
# all 3 retries re-hitting the same cap before failing. A hard stop must fail
# FAST and be classified distinctly from transient/max-turns.


def test_bradley_exact_spend_limit_is_hard_stop():
    assert is_hard_stop_failure(
        "You've hit your org's monthly spend limit · ask your admin to "
        "raise it at claude.ai/admin")


def test_hard_stop_markers():
    for s in ["monthly spend limit", "spend limit reached",
              "raise it at claude.ai/admin",
              "your organization has reached its monthly limit"]:
        assert is_hard_stop_failure(s), s


def test_hard_stop_not_transient_and_transient_not_hard_stop():
    # The two classifiers must be disjoint on each other's signatures so the
    # spend-limit can't be mis-routed into the retry loop, and a socket drop
    # can't be mis-routed into a fail-fast.
    spend = "You've hit your org's monthly spend limit — claude.ai/admin"
    assert is_hard_stop_failure(spend)
    assert not is_transient_api_failure(spend)
    for transient in ["API Error: socket connection was closed unexpectedly",
                      "Overloaded", "HTTP 529", "rate_limit exceeded"]:
        assert not is_hard_stop_failure(transient), transient


def test_hard_stop_multi_arg_any_match():
    assert is_hard_stop_failure("clean stderr", "", "monthly spend limit")
    assert not is_hard_stop_failure("a", "b", "c")
