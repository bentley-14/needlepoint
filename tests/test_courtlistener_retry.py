"""Tests for courtlistener_search.py retry / backoff / unreachable behavior.

All tests are hermetic — no real network calls.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

# Patch time.sleep globally so tests don't actually wait
import courtlistener_search as CL  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _mock_response(status_code: int, json_data: dict | None = None) -> MagicMock:
    r = MagicMock()
    r.status_code = status_code
    r.json.return_value = json_data or {}
    r.text = ""
    if status_code >= 400:
        from requests.exceptions import HTTPError
        r.raise_for_status.side_effect = HTTPError(response=r)
    else:
        r.raise_for_status.return_value = None
    return r


# ─────────────────────────────────────────────────────────────────────────────
# Retry: Timeout twice then succeed
# ─────────────────────────────────────────────────────────────────────────────

def test_retry_succeeds_after_two_timeouts():
    """Two Timeouts followed by a 200 → result returned normally."""
    from requests.exceptions import Timeout

    success_response = _mock_response(200, {"results": [{"caseName": "Foo v Bar"}]})
    side_effects = [Timeout("read timeout"), Timeout("read timeout"), success_response]

    with patch("requests.get", side_effect=side_effects), \
         patch("time.sleep"):
        result = CL._get("search", {"q": "test"})

    assert "_error" not in result
    assert result["results"][0]["caseName"] == "Foo v Bar"


# ─────────────────────────────────────────────────────────────────────────────
# Final failure → COURTLISTENER_UNREACHABLE, NOT "0 results"
# ─────────────────────────────────────────────────────────────────────────────

def test_all_timeouts_returns_unreachable():
    """Three Timeouts (exhausted retries) → _unreachable=True + correct message."""
    from requests.exceptions import Timeout

    with patch("requests.get", side_effect=Timeout("read timed out")), \
         patch("time.sleep"):
        result = CL._get("search", {"q": "test"})

    assert result.get("_unreachable") is True
    assert "COURTLISTENER_UNREACHABLE" in result["_error"]
    assert "doj_pr_lookup.py" in result["_error"]
    # Specifically must NOT look like a zero-results response
    assert "results" not in result


def test_unreachable_message_not_zero_results(capsys):
    """A COURTLISTENER_UNREACHABLE result exits 1 and prints the sentinel,
    not '0 results'."""
    from requests.exceptions import Timeout

    # Use cmd_opinions as the representative handler
    mock_args = MagicMock()
    mock_args.query = "incident-to PT fraud"
    mock_args.court = None
    mock_args.judge = None
    mock_args.limit = 10

    with patch("requests.get", side_effect=Timeout("read timed out")), \
         patch("time.sleep"):
        rc = CL.cmd_opinions(mock_args)

    captured = capsys.readouterr()
    assert rc == 1
    assert "COURTLISTENER_UNREACHABLE" in captured.err
    assert "0 results" not in captured.out
    assert "0 results" not in captured.err


# ─────────────────────────────────────────────────────────────────────────────
# 200 with empty results → exit 0, prints "0 hits"
# ─────────────────────────────────────────────────────────────────────────────

def test_200_zero_results_exits_0(capsys):
    """A real 200 response with an empty results list → exit 0 and '0 hits'."""
    success_empty = _mock_response(200, {"results": []})
    mock_args = MagicMock()
    mock_args.query = "nobody"
    mock_args.court = None
    mock_args.judge = None
    mock_args.limit = 10

    with patch("requests.get", return_value=success_empty):
        rc = CL.cmd_opinions(mock_args)

    captured = capsys.readouterr()
    assert rc == 0
    assert "0 hits" in captured.out
    assert "COURTLISTENER_UNREACHABLE" not in captured.out
    assert "COURTLISTENER_UNREACHABLE" not in captured.err


# ─────────────────────────────────────────────────────────────────────────────
# 503 retry then succeed
# ─────────────────────────────────────────────────────────────────────────────

def test_retry_on_503():
    """Two 503s followed by a 200 → returns the successful response."""
    r503 = _mock_response(503)
    r503.raise_for_status.return_value = None  # retryable, not raised
    success = _mock_response(200, {"results": []})

    with patch("requests.get", side_effect=[r503, r503, success]), \
         patch("time.sleep"):
        result = CL._get("search", {"q": "test"})

    assert "_error" not in result


def test_all_503_returns_unreachable():
    """Three 503s → _unreachable."""
    r503 = _mock_response(503)
    r503.raise_for_status.return_value = None

    with patch("requests.get", return_value=r503), \
         patch("time.sleep"):
        result = CL._get("search", {"q": "test"})

    assert result.get("_unreachable") is True
    assert "COURTLISTENER_UNREACHABLE" in result["_error"]


# ─────────────────────────────────────────────────────────────────────────────
# ConnectionError retry
# ─────────────────────────────────────────────────────────────────────────────

def test_retry_on_connection_error():
    """ConnectionError on first two attempts, then success."""
    from requests.exceptions import ConnectionError as ReqConnError

    success = _mock_response(200, {"results": [{"caseName": "Test"}]})

    with patch("requests.get", side_effect=[ReqConnError("conn refused"),
                                            ReqConnError("conn refused"),
                                            success]), \
         patch("time.sleep"):
        result = CL._get("search", {"q": "test"})

    assert "_error" not in result
    assert result["results"][0]["caseName"] == "Test"


# ─────────────────────────────────────────────────────────────────────────────
# Non-retryable HTTP (404) — returns immediately, no retry
# ─────────────────────────────────────────────────────────────────────────────

def test_404_not_retried():
    """A 404 is not retryable — returns error immediately without retry."""
    r404 = _mock_response(404)

    call_count = 0
    orig_get = __builtins__  # noqa

    with patch("requests.get", return_value=r404) as mock_get:
        result = CL._get("opinions/9999", {})

    # Only one attempt
    assert mock_get.call_count == 1
    assert "_error" in result
    assert "404" in result["_error"]
    assert result.get("_unreachable") is not True
