"""
HTTP fetcher with rate limiting, retries, and Akamai-aware fallback.

Two paths:
1. PRIMARY: stdlib urllib (fast, no deps). Works for unprotected sources
   like OIG and for the ~3% of DOJ URLs that pass straight through.
2. FALLBACK: curl_cffi + Akamai challenge solver. Triggered when the
   primary returns an Akamai interstitial. Reuses a shared session
   across calls so the ak_bmsc cookie persists (one challenge solve
   per session, not per URL).

Single entry point: fetch_url(url) → FetchResult.
"""

from __future__ import annotations

import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Optional

from .akamai_solver import (
    _CFFI_AVAILABLE,
    fetch_with_akamai,
    is_akamai_block,
    is_interstitial,
    make_session,
)
from .raw_archive import USER_AGENT


@dataclass
class FetchResult:
    url: str
    final_url: str
    http_status: int
    body: str
    redirect_chain: list[str]
    duration_ms: int
    method: str = "urllib"  # "urllib" | "curl_cffi+akamai"
    error: Optional[str] = None


class RateLimiter:
    """Token-bucket-ish rate limiter — min interval between requests."""

    def __init__(self, requests_per_second: float = 1.0):
        self.min_interval_s = 1.0 / requests_per_second
        self._last_request_t: float = 0.0

    def wait(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_request_t
        if elapsed < self.min_interval_s:
            time.sleep(self.min_interval_s - elapsed)
        self._last_request_t = time.monotonic()


# Default: 1 request per second. Government sites are tolerant but we don't
# want to look like an aggressive scraper.
DEFAULT_LIMITER = RateLimiter(requests_per_second=1.0)


@dataclass
class FetcherConfig:
    """Per-source fetcher configuration."""

    rate_limiter: RateLimiter = field(default_factory=lambda: DEFAULT_LIMITER)
    timeout_s: int = 30
    max_retries: int = 3
    backoff_base_s: float = 2.0
    use_akamai_fallback: bool = True  # set False to skip even on interstitial


# Module-level shared curl_cffi session for Akamai-protected hosts.
# Reuse keeps the ak_bmsc cookie alive across calls — solve once, fetch many.
_AKAMAI_SESSION = None


def _get_akamai_session():
    global _AKAMAI_SESSION
    if _AKAMAI_SESSION is None and _CFFI_AVAILABLE:
        _AKAMAI_SESSION = make_session()
    return _AKAMAI_SESSION


def _fetch_with_urllib(
    url: str, config: FetcherConfig
) -> tuple[Optional[str], Optional[int], list[str], Optional[str], str]:
    """
    Plain urllib fetch. Returns (body, status, redirects, error, final_url).
    body=None on hard failure.
    """
    config.rate_limiter.wait()
    redirect_chain: list[str] = []
    final_url = url

    class TrackingHandler(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
            redirect_chain.append(newurl)
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    opener = urllib.request.build_opener(TrackingHandler())
    opener.addheaders = [
        ("User-Agent", USER_AGENT),
        (
            "Accept",
            "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        ),
        ("Accept-Language", "en-US,en;q=0.9"),
        ("Accept-Encoding", "identity"),
        ("Connection", "keep-alive"),
        ("Upgrade-Insecure-Requests", "1"),
    ]

    last_error: Optional[str] = None
    for attempt in range(config.max_retries):
        try:
            with opener.open(url, timeout=config.timeout_s) as resp:
                final_url = resp.geturl()
                body_bytes = resp.read()
                body = body_bytes.decode("utf-8", errors="replace")
                return body, resp.status, redirect_chain, None, final_url
        except urllib.error.HTTPError as e:
            last_error = f"HTTPError {e.code}: {e.reason}"
            if e.code != 429 and 400 <= e.code < 500:
                # 4xx (other than 429) is terminal — don't retry, return body
                try:
                    body = (
                        e.read().decode("utf-8", errors="replace")
                        if hasattr(e, "read")
                        else ""
                    )
                except Exception:  # noqa: BLE001
                    body = ""
                return body, e.code, redirect_chain, last_error, final_url
        except (urllib.error.URLError, TimeoutError) as e:
            last_error = f"URLError: {e}"
        except Exception as e:  # noqa: BLE001
            last_error = f"{type(e).__name__}: {e}"

        if attempt < config.max_retries - 1:
            time.sleep(config.backoff_base_s * (2**attempt))

    return None, 0, redirect_chain, last_error, final_url


def fetch_url(
    url: str,
    rate_limiter: Optional[RateLimiter] = None,
    timeout_s: int = 30,
    max_retries: int = 3,
    backoff_base_s: float = 2.0,
    use_akamai_fallback: bool = True,
) -> FetchResult:
    """
    Fetch a single URL with Akamai-aware fallback.

    Path:
    1. Try urllib (fast, unprotected sources).
    2. If response is an Akamai interstitial AND use_akamai_fallback=True
       AND curl_cffi is installed, retry via the solver.
    3. Return whichever succeeded; record the method used.
    """
    config = FetcherConfig(
        rate_limiter=rate_limiter or DEFAULT_LIMITER,
        timeout_s=timeout_s,
        max_retries=max_retries,
        backoff_base_s=backoff_base_s,
        use_akamai_fallback=use_akamai_fallback,
    )

    t0 = time.monotonic()
    body, status, redirects, error, final_url = _fetch_with_urllib(url, config)

    # Hard failure — no body at all
    if body is None:
        return FetchResult(
            url=url,
            final_url=final_url,
            http_status=status or 0,
            body="",
            redirect_chain=redirects,
            duration_ms=int((time.monotonic() - t0) * 1000),
            method="urllib",
            error=error or "no_body",
        )

    # Akamai detected — fall back to solver. Two cases:
    # (a) JS interstitial (200 with ~3KB body containing bm-verify)
    # (b) Edge block (403/406 with "edgesuite" / "Access Denied")
    needs_akamai = is_interstitial(body) or is_akamai_block(
        body, status or 0
    )
    if (
        config.use_akamai_fallback
        and _CFFI_AVAILABLE
        and needs_akamai
    ):
        config.rate_limiter.wait()
        akamai_result = fetch_with_akamai(
            url, session=_get_akamai_session(), timeout_s=config.timeout_s
        )
        return FetchResult(
            url=url,
            final_url=akamai_result.final_url,
            http_status=akamai_result.status,
            body=akamai_result.body,
            redirect_chain=redirects,
            duration_ms=int((time.monotonic() - t0) * 1000),
            method="curl_cffi+akamai",
            error=akamai_result.error,
        )

    return FetchResult(
        url=url,
        final_url=final_url,
        http_status=status or 200,
        body=body,
        redirect_chain=redirects,
        duration_ms=int((time.monotonic() - t0) * 1000),
        method="urllib",
        error=error,
    )
