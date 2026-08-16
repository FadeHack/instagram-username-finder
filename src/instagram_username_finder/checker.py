"""HTTP username checking and response classification.

The checker owns exactly one responsibility: turn a username into a
:class:`~instagram_username_finder.models.CheckResult`. It does not persist
anything, does not format anything, and does not decide what to scan next.

Classification is deliberately conservative. An ambiguous response is reported
as ``UNKNOWN``; it is never upgraded into an availability claim.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Final, Protocol, runtime_checkable

import aiohttp

from .models import CheckResult, CheckStatus
from .rate_limiter import RateLimiter
from .retry import RetryPolicy, parse_retry_after

logger = logging.getLogger(__name__)

#: Markers that only appear on a rendered public profile.
PROFILE_INDICATORS: Final = (
    "instagram://user?username=",
    '"@type":"ProfilePage"',
    '"profile_pic_url"',
    '"edge_followed_by"',
)

#: Markers Instagram renders when a profile does not exist.
NOT_FOUND_INDICATORS: Final = (
    "Sorry, this page isn't available.",
    "the link you followed may be broken",
    "page may have been removed",
)

#: Markers for a login/checkpoint interstitial - informative about our session,
#: not about the username, so these must resolve to UNKNOWN.
LOGIN_WALL_INDICATORS: Final = (
    "/accounts/login/",
    "loginForm",
    "challenge_required",
    "Please wait a few minutes before you try again",
)

#: A bare ``<title>Instagram</title>``. Instagram answers a request for a
#: non-existent profile with HTTP 200 and a generic client-rendered shell, so
#: the *absence* of profile metadata alongside this title is the only
#: server-side signal that no profile exists.
GENERIC_TITLE_RE: Final = re.compile(r"<title[^>]*>\s*Instagram\s*</title>", re.IGNORECASE)

#: Open Graph metadata Instagram emits only for a real profile.
PROFILE_META_RE: Final = re.compile(
    r'property="(?:og:title|og:description|al:ios:url)"', re.IGNORECASE
)

#: Enough body to classify; profile markers appear early in the document.
MAX_BODY_BYTES: Final = 262_144

DEFAULT_HEADERS: Final = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


@runtime_checkable
class UsernameChecker(Protocol):
    """Anything that can resolve a username to a result."""

    async def check(self, username: str) -> CheckResult: ...


def classify_body(body: str) -> CheckStatus:
    """Classify the body of an HTTP 200 profile response.

    Every verdict needs positive evidence. An unrecognised page resolves to
    ``UNKNOWN`` rather than being assumed available: Instagram serves HTTP 200
    for both real and non-existent profiles, so "nothing matched" is a
    statement about our markers, not about the username.
    """
    # Checked first: a login or checkpoint interstitial describes our session,
    # and can otherwise satisfy the weaker not-found signal below.
    if any(marker in body for marker in LOGIN_WALL_INDICATORS):
        return CheckStatus.UNKNOWN
    if any(marker in body for marker in PROFILE_INDICATORS):
        return CheckStatus.TAKEN
    if any(marker in body for marker in NOT_FOUND_INDICATORS):
        return CheckStatus.POSSIBLY_AVAILABLE
    # The generic shell: a bare "Instagram" title and no profile metadata.
    if GENERIC_TITLE_RE.search(body) and not PROFILE_META_RE.search(body):
        return CheckStatus.POSSIBLY_AVAILABLE
    return CheckStatus.UNKNOWN


def classify_response(status: int, body: str | None = None) -> CheckStatus:
    """Map an HTTP status (plus body, for 200) onto a check status.

    ``404`` means no publicly accessible profile was served - which is what
    ``POSSIBLY_AVAILABLE`` claims, and nothing more.
    """
    if status == 200:
        return classify_body(body or "")
    if status == 404:
        return CheckStatus.POSSIBLY_AVAILABLE
    if status in (403, 429):
        return CheckStatus.RATE_LIMITED
    return CheckStatus.UNKNOWN


class InstagramChecker:
    """Checks usernames against public Instagram profile URLs.

    A single :class:`aiohttp.ClientSession` is reused for every request so
    connections are pooled and TLS handshakes are not repeated per username.
    """

    def __init__(
        self,
        *,
        session: aiohttp.ClientSession,
        rate_limiter: RateLimiter,
        retry_policy: RetryPolicy,
        base_url: str = "https://www.instagram.com",
        timeout: float = 10.0,
    ) -> None:
        self._session = session
        self._limiter = rate_limiter
        self._retry = retry_policy
        self._base_url = base_url.rstrip("/")
        self._timeout = aiohttp.ClientTimeout(total=timeout)

    def url_for(self, username: str) -> str:
        return f"{self._base_url}/{username}/"

    async def check(self, username: str) -> CheckResult:
        """Resolve one username, retrying transient failures."""
        attempt = 0
        last: CheckResult | None = None

        while True:
            attempt += 1
            started = time.perf_counter()
            try:
                async with self._limiter.slot():
                    outcome = await self._request(username)
            except TimeoutError:
                last = self._failure(
                    username, CheckStatus.TIMEOUT, started, attempt, "request timed out"
                )
                decision = self._retry.decide(attempt, exception=TimeoutError())
            except aiohttp.ClientError as exc:
                last = self._failure(
                    username, CheckStatus.NETWORK_ERROR, started, attempt, str(exc)
                )
                decision = self._retry.decide(attempt, exception=exc)
            else:
                status, body, retry_after = outcome
                latency = (time.perf_counter() - started) * 1000
                check_status = classify_response(status, body)

                if check_status is CheckStatus.RATE_LIMITED:
                    pause = self._limiter.record_rate_limited(retry_after)
                    logger.warning(
                        "rate limited on %s (HTTP %s); pausing %.1fs",
                        username,
                        status,
                        pause,
                    )
                    last = CheckResult(
                        username=username,
                        status=CheckStatus.RATE_LIMITED,
                        http_status=status,
                        latency_ms=latency,
                        error=f"HTTP {status}",
                        attempts=attempt,
                    )
                    decision = self._retry.decide(
                        attempt, status=status, retry_after=retry_after
                    )
                    # The limiter is already holding the cooldown; retrying
                    # immediately would only queue behind it, so do not sleep.
                    if decision.should_retry:
                        continue
                    return last

                if status >= 500:
                    last = CheckResult(
                        username=username,
                        status=CheckStatus.UNKNOWN,
                        http_status=status,
                        latency_ms=latency,
                        error=f"HTTP {status}",
                        attempts=attempt,
                    )
                    decision = self._retry.decide(attempt, status=status)
                else:
                    self._limiter.record_success()
                    return CheckResult(
                        username=username,
                        status=check_status,
                        http_status=status,
                        latency_ms=latency,
                        attempts=attempt,
                    )

            if not decision.should_retry:
                return last
            logger.debug(
                "retrying %s in %.2fs (attempt %d, %s)",
                username,
                decision.delay,
                attempt,
                decision.reason,
            )
            if decision.delay > 0:
                await asyncio.sleep(decision.delay)

    async def _request(self, username: str) -> tuple[int, str | None, float | None]:
        async with self._session.get(
            self.url_for(username),
            timeout=self._timeout,
            allow_redirects=True,
        ) as response:
            retry_after = parse_retry_after(response.headers.get("Retry-After"))
            body: str | None = None
            if response.status == 200:
                raw = await response.content.read(MAX_BODY_BYTES)
                body = raw.decode("utf-8", errors="replace")
                if "/accounts/login" in str(response.url):
                    # Redirected to a login wall; body markers would mislead.
                    body = "loginForm"
            return response.status, body, retry_after

    @staticmethod
    def _failure(
        username: str,
        status: CheckStatus,
        started: float,
        attempt: int,
        error: str,
    ) -> CheckResult:
        return CheckResult(
            username=username,
            status=status,
            latency_ms=(time.perf_counter() - started) * 1000,
            error=error,
            attempts=attempt,
        )


def build_session(
    *,
    user_agent: str,
    concurrency: int,
    timeout: float,
) -> aiohttp.ClientSession:
    """Create the single pooled session used for an entire scan."""
    connector = aiohttp.TCPConnector(
        limit=concurrency,
        limit_per_host=concurrency,
        ttl_dns_cache=300,
        enable_cleanup_closed=True,
    )
    headers = {**DEFAULT_HEADERS, "User-Agent": user_agent}
    return aiohttp.ClientSession(
        connector=connector,
        headers=headers,
        timeout=aiohttp.ClientTimeout(total=timeout),
    )
