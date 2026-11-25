"""Tests for scripts/browser_fetch.py — retry/backoff + WAF exit semantics.

No real network calls. All session.get calls are monkeypatched.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

requests = pytest.importorskip("requests")

# Ensure scripts/ is importable when run from repo root
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import browser_fetch  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_response(status: int, body: str = "OK", ctype: str = "text/html") -> MagicMock:
    r = MagicMock()
    r.status_code = status
    r.text = body
    r.headers = {"Content-Type": ctype}
    return r


# ---------------------------------------------------------------------------
# fetch() unit tests
# ---------------------------------------------------------------------------

class TestFetch:
    def test_200_ok_no_retry(self):
        """Single 200 is returned immediately, no retry."""
        resp = _make_response(200, body="<html>hello</html>")
        call_count = 0

        def fake_get(self_session, url, **kw):
            nonlocal call_count
            call_count += 1
            return resp

        with patch.object(requests.Session, "get", fake_get):
            status, ctype, body = browser_fetch.fetch("https://example.com/")

        assert status == 200
        assert "hello" in body
        assert call_count == 1

    def test_403_then_200_retry_succeeds(self):
        """First attempt 403 → second attempt 200 → success, no error."""
        responses = [_make_response(403, body="Forbidden"), _make_response(200, body="Good")]
        idx = 0

        def fake_get(self_session, url, **kw):
            nonlocal idx
            r = responses[idx]
            idx += 1
            return r

        with patch("time.sleep"), patch.object(requests.Session, "get", fake_get):
            status, ctype, body = browser_fetch.fetch("https://example.com/")

        assert status == 200
        assert body == "Good"

    def test_persistent_403_returns_403_after_retries(self):
        """Persistent 403 returns status=403 after all retry attempts."""
        call_count = 0

        def fake_get(self_session, url, **kw):
            nonlocal call_count
            call_count += 1
            return _make_response(403, body="WAF block")

        with patch("time.sleep"), patch.object(requests.Session, "get", fake_get):
            status, ctype, body = browser_fetch.fetch("https://example.com/")

        assert status == 403
        assert call_count == browser_fetch._MAX_ATTEMPTS

    def test_timeout_then_200_retry_succeeds(self):
        """Timeout on first attempt then 200 on second — retry recovers."""
        attempts = 0

        def fake_get(self_session, url, **kw):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise requests.Timeout("timed out")
            return _make_response(200, body="recovered")

        with patch("time.sleep"), patch.object(requests.Session, "get", fake_get):
            status, ctype, body = browser_fetch.fetch("https://example.com/")

        assert status == 200
        assert body == "recovered"

    def test_non_retryable_network_error_returns_0(self):
        """ConnectionError is non-retryable — returns immediately with status 0."""
        call_count = 0

        def fake_get(self_session, url, **kw):
            nonlocal call_count
            call_count += 1
            raise requests.ConnectionError("DNS fail")

        with patch.object(requests.Session, "get", fake_get):
            status, ctype, body = browser_fetch.fetch("https://example.com/")

        assert status == 0
        assert "REQUEST FAILED" in body
        assert call_count == 1  # no retry on ConnectionError


# ---------------------------------------------------------------------------
# CLI exit-code + STATUS line tests
# ---------------------------------------------------------------------------

class TestCLI:
    def _run(self, argv: list[str], mock_fetch_return: tuple) -> tuple[int, str]:
        """Run main() with a patched fetch(); return (exit_code, stdout)."""
        import io
        out = io.StringIO()
        with patch("browser_fetch.fetch", return_value=mock_fetch_return), \
             patch("sys.stdout", out):
            code = browser_fetch.main(argv)
        return code, out.getvalue()

    def test_200_exit_0_status_ok(self):
        code, out = self._run(["https://example.com/"], (200, "text/html", "<html>hi</html>"))
        assert code == 0
        assert out.startswith("STATUS: OK 200")

    def test_persistent_403_exit_3_blocked_line(self):
        code, out = self._run(["https://example.com/"], (403, "text/html", "Forbidden"))
        assert code == 3
        assert "STATUS: BLOCKED 403" in out
        assert "does NOT mean absence" in out

    def test_404_exit_4_not_found_line(self):
        code, out = self._run(["https://example.com/not-there"], (404, "text/html", "Not found"))
        assert code == 4
        assert "STATUS: NOT_FOUND 404" in out

    def test_network_error_exit_5_error_line(self):
        code, out = self._run(["https://example.com/"], (0, "", "REQUEST FAILED: DNS fail"))
        assert code == 5
        assert "STATUS: ERROR" in out

    def test_head_mode_reachable(self):
        """--head with no N: prints STATUS line and exits 0 on 200."""
        code, out = self._run(["https://example.com/", "--head"], (200, "text/html", "<html/>"))
        assert code == 0
        assert "STATUS: OK 200" in out

    def test_head_mode_blocked(self):
        """--head with 403: exits 3 and prints BLOCKED line."""
        code, out = self._run(["https://example.com/", "--head"], (403, "text/html", "Forbidden"))
        assert code == 3
        assert "STATUS: BLOCKED 403" in out
        assert "does NOT mean absence" in out

    def test_429_treated_as_blocked(self):
        """429 (rate-limited) uses the same BLOCKED path as 403."""
        code, out = self._run(["https://example.com/"], (429, "text/html", "Too Many Requests"))
        assert code == 3
        assert "STATUS: BLOCKED 429" in out
        assert "does NOT mean absence" in out

    def test_text_mode_strips_html(self):
        """--text strips tags from the body."""
        code, out = self._run(
            ["https://example.com/", "--text"],
            (200, "text/html", "<html><body><p>Hello world</p></body></html>"),
        )
        assert code == 0
        assert "<html>" not in out
        assert "Hello world" in out
