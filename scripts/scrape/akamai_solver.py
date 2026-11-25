"""
Akamai bot-mitigation challenge solver for justice.gov.

DOJ press releases are protected by Akamai bot mitigation that returns a
~3KB JS interstitial on the first request. The challenge is:

1. Server returns interstitial HTML containing:
   - A `bm-verify` token (~400-character base64 blob)
   - JS computing `pow = i + Number("s1" + "s2")`
   - A `<meta http-equiv="refresh">` and JS that POSTs the answer
2. Client must POST `{bm-verify, pow}` to `/_sec/verify?provider=interstitial`
3. Server responds `{"reload": true}` and sets/refreshes ak_bmsc cookie
4. Same session can now fetch the original URL → real content

The solver is implemented in pure Python (no JS execution needed). The
proof-of-work is just integer arithmetic on values embedded in the
interstitial source.

Reverse-engineered from observation 2026-05-07 of justice.gov interstitials.
DOJ may rotate the challenge format; the solver detects unsolvable
challenges and returns the interstitial unchanged for caller to handle.

Requires: curl_cffi (for browser TLS impersonation; plain requests gets
403 even after solving the challenge in some cases).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

try:
    from curl_cffi import requests as cffi_requests

    _CFFI_AVAILABLE = True
except ImportError:
    _CFFI_AVAILABLE = False
    cffi_requests = None  # type: ignore[assignment]


VERIFY_ENDPOINT = "https://www.justice.gov/_sec/verify?provider=interstitial"
INTERSTITIAL_BODY_THRESHOLD = 8000  # real pages are 50KB+; interstitial is 3KB


@dataclass
class AkamaiSolveResult:
    body: str
    status: int
    final_url: str
    solved_challenge: bool
    cookies: dict[str, str]
    error: Optional[str] = None


def is_interstitial(body: str) -> bool:
    """Return True if the response body looks like an Akamai interstitial."""
    if len(body) > INTERSTITIAL_BODY_THRESHOLD:
        return False
    lower = body.lower()
    return ("bm-verify" in body) or ("akamai" in lower and "interstitial" in lower)


def is_akamai_block(body: str, status: int) -> bool:
    """
    Return True if the response is an Akamai edge block (hard 403 or similar).

    Different from interstitial: edge blocks are returned when the request
    looks too bot-like for the JS challenge to even fire. Body is short and
    contains 'edgesuite' or 'access denied'.
    """
    if status not in (403, 406):
        return False
    if len(body) > 2000:
        return False
    lower = body.lower()
    return (
        "edgesuite" in lower
        or "access denied" in lower
        or "errors.edgesuite.net" in lower
    )


def parse_challenge(body: str) -> Optional[tuple[str, int]]:
    """
    Parse the bm-verify token and proof-of-work from interstitial HTML.

    Returns (bm_verify_token, pow_value) or None if parsing fails.
    """
    bm = re.search(r'"bm-verify":\s*"([^"]+)"', body)
    i_match = re.search(r"var\s+i\s*=\s*(\d+)", body)
    j_match = re.search(
        r'var\s+j\s*=\s*i\s*\+\s*Number\(\s*"(\d+)"\s*\+\s*"(\d+)"\s*\)', body
    )
    if not (bm and i_match and j_match):
        return None
    pow_value = int(i_match.group(1)) + int(j_match.group(1) + j_match.group(2))
    return bm.group(1), pow_value


def make_session(impersonate: str = "chrome120"):
    """Create a curl_cffi session with browser TLS impersonation."""
    if not _CFFI_AVAILABLE:
        raise RuntimeError(
            "curl_cffi not available — install with `pip install curl_cffi` "
            "to enable Akamai bypass"
        )
    return cffi_requests.Session(impersonate=impersonate)


def fetch_with_akamai(
    url: str,
    session=None,
    timeout_s: int = 20,
    max_solve_attempts: int = 2,
) -> AkamaiSolveResult:
    """
    Fetch a URL, solving the Akamai challenge if encountered.

    If `session` is provided, it's reused (prevents re-solving on every
    request — Akamai cookie persists for the session). If None, a fresh
    session is created (caller should reuse for batch scraping).
    """
    if not _CFFI_AVAILABLE:
        return AkamaiSolveResult(
            body="",
            status=0,
            final_url=url,
            solved_challenge=False,
            cookies={},
            error="curl_cffi_not_installed",
        )

    own_session = session is None
    if own_session:
        session = make_session()

    try:
        # First attempt
        r = session.get(url, timeout=timeout_s)
        body = r.text

        if not is_interstitial(body):
            return AkamaiSolveResult(
                body=body,
                status=r.status_code,
                final_url=str(r.url),
                solved_challenge=False,
                cookies={c.name: c.value for c in session.cookies.jar},
            )

        # Solve up to max_solve_attempts times (the challenge can rotate)
        solved = False
        for _ in range(max_solve_attempts):
            parsed = parse_challenge(body)
            if not parsed:
                return AkamaiSolveResult(
                    body=body,
                    status=r.status_code,
                    final_url=str(r.url),
                    solved_challenge=False,
                    cookies={c.name: c.value for c in session.cookies.jar},
                    error="challenge_unparseable",
                )
            bm_verify, pow_val = parsed
            verify_resp = session.post(
                VERIFY_ENDPOINT,
                json={"bm-verify": bm_verify, "pow": pow_val},
                timeout=timeout_s,
            )
            if verify_resp.status_code != 200:
                return AkamaiSolveResult(
                    body=body,
                    status=r.status_code,
                    final_url=str(r.url),
                    solved_challenge=False,
                    cookies={c.name: c.value for c in session.cookies.jar},
                    error=f"verify_endpoint_status_{verify_resp.status_code}",
                )

            # Re-fetch original URL with the now-cookied session
            r = session.get(url, timeout=timeout_s)
            body = r.text
            if not is_interstitial(body):
                solved = True
                break

        return AkamaiSolveResult(
            body=body,
            status=r.status_code,
            final_url=str(r.url),
            solved_challenge=solved,
            cookies={c.name: c.value for c in session.cookies.jar},
            error=None if solved else "still_interstitial_after_solve",
        )
    except Exception as e:  # noqa: BLE001
        return AkamaiSolveResult(
            body="",
            status=0,
            final_url=url,
            solved_challenge=False,
            cookies={},
            error=f"{type(e).__name__}: {e}",
        )
