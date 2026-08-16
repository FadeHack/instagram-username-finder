"""Response classification and HTTP behaviour of the checker.

Requests are served by a throwaway aiohttp server bound to localhost, so the
transport is exercised for real without ever contacting Instagram.
"""

from __future__ import annotations

import asyncio
import socket
from collections import defaultdict, deque
from collections.abc import AsyncIterator, Iterator

import aiohttp
import pytest
from aiohttp import web

from instagram_username_finder.checker import (
    InstagramChecker,
    build_session,
    classify_response,
)
from instagram_username_finder.models import CheckStatus
from instagram_username_finder.rate_limiter import RateLimiter
from instagram_username_finder.retry import RetryPolicy

PROFILE_HTML = '<html><script>{"@type":"ProfilePage","edge_followed_by":1}</script></html>'
NOT_FOUND_HTML = "<html><body>Sorry, this page isn't available.</body></html>"
LOGIN_HTML = '<html><form id="loginForm"></form></html>'


class Reply:
    """One canned HTTP response."""

    def __init__(
        self,
        status: int = 200,
        body: str = "",
        headers: dict[str, str] | None = None,
        sleep: float = 0.0,
    ) -> None:
        self.status = status
        self.body = body
        self.headers = headers or {}
        self.sleep = sleep


class FakeInstagram:
    """A local stand-in for the public profile endpoint."""

    def __init__(self) -> None:
        self.replies: defaultdict[str, deque[Reply]] = defaultdict(deque)
        self.requests: list[str] = []
        self.default = Reply(status=404)

    def queue(self, username: str, *replies: Reply) -> None:
        self.replies[username].extend(replies)

    async def handle(self, request: web.Request) -> web.Response:
        username = request.match_info["username"]
        self.requests.append(username)
        queued = self.replies.get(username)
        reply = queued.popleft() if queued else self.default
        if reply.sleep:
            await asyncio.sleep(reply.sleep)
        return web.Response(
            status=reply.status,
            text=reply.body,
            headers=reply.headers,
            content_type="text/html",
        )


@pytest.fixture
async def instagram() -> AsyncIterator[tuple[FakeInstagram, str]]:
    fake = FakeInstagram()
    app = web.Application()
    app.router.add_get("/{username}/", fake.handle)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    try:
        yield fake, f"http://127.0.0.1:{port}"
    finally:
        await runner.cleanup()


@pytest.fixture
async def session() -> AsyncIterator[aiohttp.ClientSession]:
    created = build_session(user_agent="tests/1.0", concurrency=4, timeout=5)
    try:
        yield created
    finally:
        await created.close()


@pytest.fixture
def closed_port() -> Iterator[int]:
    """A port that is guaranteed to refuse connections."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = int(probe.getsockname()[1])
    yield port


def make_checker(
    session: aiohttp.ClientSession,
    base_url: str,
    *,
    max_retries: int = 0,
    max_rate_limit_retries: int = 0,
    timeout: float = 5.0,
    limiter: RateLimiter | None = None,
) -> InstagramChecker:
    return InstagramChecker(
        session=session,
        rate_limiter=limiter or RateLimiter(concurrency=4, delay=0.0, cooldown=0.0),
        retry_policy=RetryPolicy(
            max_retries=max_retries,
            base_delay=0.001,
            max_delay=0.002,
            jitter=0.0,
            max_rate_limit_retries=max_rate_limit_retries,
        ),
        base_url=base_url,
        timeout=timeout,
    )


class TestClassifier:
    def test_profile_markers_mean_taken(self) -> None:
        assert classify_response(200, PROFILE_HTML) is CheckStatus.TAKEN

    def test_not_found_body_means_possibly_available(self) -> None:
        assert classify_response(200, NOT_FOUND_HTML) is CheckStatus.POSSIBLY_AVAILABLE

    def test_login_wall_is_unknown_not_available(self) -> None:
        assert classify_response(200, LOGIN_HTML) is CheckStatus.UNKNOWN

    def test_404_means_possibly_available(self) -> None:
        assert classify_response(404) is CheckStatus.POSSIBLY_AVAILABLE

    @pytest.mark.parametrize("status", [403, 429])
    def test_throttling_statuses_are_rate_limited(self, status: int) -> None:
        assert classify_response(status) is CheckStatus.RATE_LIMITED

    @pytest.mark.parametrize("status", [500, 502, 503, 418])
    def test_other_statuses_are_unknown(self, status: int) -> None:
        assert classify_response(status) is CheckStatus.UNKNOWN

    def test_unknown_is_never_reported_as_available(self) -> None:
        for status in (301, 418, 451, 500, 503):
            assert classify_response(status) is not CheckStatus.POSSIBLY_AVAILABLE


class TestRequests:
    async def test_200_profile_is_taken(
        self, session: aiohttp.ClientSession, instagram: tuple[FakeInstagram, str]
    ) -> None:
        fake, base_url = instagram
        fake.queue("taken", Reply(200, PROFILE_HTML))
        result = await make_checker(session, base_url).check("taken")

        assert result.status is CheckStatus.TAKEN
        assert result.http_status == 200
        assert result.latency_ms is not None
        assert fake.requests == ["taken"]

    async def test_404_is_possibly_available(
        self, session: aiohttp.ClientSession, instagram: tuple[FakeInstagram, str]
    ) -> None:
        fake, base_url = instagram
        fake.queue("qzx", Reply(404))
        result = await make_checker(session, base_url).check("qzx")

        assert result.status is CheckStatus.POSSIBLY_AVAILABLE
        assert result.is_candidate

    async def test_200_without_profile_markers_is_not_taken(
        self, session: aiohttp.ClientSession, instagram: tuple[FakeInstagram, str]
    ) -> None:
        fake, base_url = instagram
        fake.queue("empty", Reply(200, NOT_FOUND_HTML))
        result = await make_checker(session, base_url).check("empty")
        assert result.status is CheckStatus.POSSIBLY_AVAILABLE

    async def test_login_wall_response_is_unknown(
        self, session: aiohttp.ClientSession, instagram: tuple[FakeInstagram, str]
    ) -> None:
        fake, base_url = instagram
        fake.queue("walled", Reply(200, LOGIN_HTML))
        result = await make_checker(session, base_url).check("walled")

        assert result.status is CheckStatus.UNKNOWN
        assert not result.is_candidate

    async def test_429_pauses_the_limiter_and_honours_retry_after(
        self, session: aiohttp.ClientSession, instagram: tuple[FakeInstagram, str]
    ) -> None:
        fake, base_url = instagram
        fake.queue("hot", Reply(429, headers={"Retry-After": "12"}))
        limiter = RateLimiter(concurrency=2, delay=0.0, cooldown=30.0)
        result = await make_checker(session, base_url, limiter=limiter).check("hot")

        assert result.status is CheckStatus.RATE_LIMITED
        assert limiter.consecutive_rate_limits == 1
        # Retry-After (12s) is used in preference to the 30s default cooldown.
        assert 11 <= limiter.pause_remaining <= 12

    async def test_403_is_rate_limited(
        self, session: aiohttp.ClientSession, instagram: tuple[FakeInstagram, str]
    ) -> None:
        fake, base_url = instagram
        fake.queue("blocked", Reply(403))
        result = await make_checker(session, base_url).check("blocked")
        assert result.status is CheckStatus.RATE_LIMITED

    async def test_500_is_retried_then_reported_unknown(
        self, session: aiohttp.ClientSession, instagram: tuple[FakeInstagram, str]
    ) -> None:
        fake, base_url = instagram
        fake.queue("flaky", Reply(500), Reply(500), Reply(500))
        result = await make_checker(session, base_url, max_retries=2).check("flaky")

        assert result.status is CheckStatus.UNKNOWN
        assert result.attempts == 3
        assert fake.requests == ["flaky"] * 3

    async def test_502_then_success_resolves(
        self, session: aiohttp.ClientSession, instagram: tuple[FakeInstagram, str]
    ) -> None:
        fake, base_url = instagram
        fake.queue("blip", Reply(502), Reply(404))
        result = await make_checker(session, base_url, max_retries=2).check("blip")

        assert result.status is CheckStatus.POSSIBLY_AVAILABLE
        assert result.attempts == 2

    async def test_timeout_is_reported_as_timeout(
        self, session: aiohttp.ClientSession, instagram: tuple[FakeInstagram, str]
    ) -> None:
        fake, base_url = instagram
        fake.queue("slow", Reply(200, PROFILE_HTML, sleep=1.0))
        result = await make_checker(session, base_url, timeout=0.05).check("slow")

        assert result.status is CheckStatus.TIMEOUT
        assert result.error == "request timed out"

    async def test_timeout_is_retried_when_budget_allows(
        self, session: aiohttp.ClientSession, instagram: tuple[FakeInstagram, str]
    ) -> None:
        fake, base_url = instagram
        fake.queue("twice", Reply(200, PROFILE_HTML, sleep=1.0), Reply(404))
        result = await make_checker(session, base_url, max_retries=1, timeout=0.05).check(
            "twice"
        )

        assert result.status is CheckStatus.POSSIBLY_AVAILABLE
        assert result.attempts == 2

    async def test_connection_failure_is_a_network_error(
        self, session: aiohttp.ClientSession, closed_port: int
    ) -> None:
        checker = make_checker(session, f"http://127.0.0.1:{closed_port}")
        result = await checker.check("down")

        assert result.status is CheckStatus.NETWORK_ERROR
        assert result.error

    async def test_connection_failure_is_retried_then_surrendered(
        self, session: aiohttp.ClientSession, closed_port: int
    ) -> None:
        checker = make_checker(session, f"http://127.0.0.1:{closed_port}", max_retries=2)
        result = await checker.check("down")

        assert result.status is CheckStatus.NETWORK_ERROR
        assert result.attempts == 3

    async def test_transport_failures_are_never_treated_as_availability(
        self,
        session: aiohttp.ClientSession,
        closed_port: int,
        instagram: tuple[FakeInstagram, str],
    ) -> None:
        fake, base_url = instagram
        fake.queue("slow", Reply(200, PROFILE_HTML, sleep=1.0))
        fake.queue("boom", Reply(500))

        timed_out = await make_checker(session, base_url, timeout=0.05).check("slow")
        server_error = await make_checker(session, base_url).check("boom")
        refused = await make_checker(session, f"http://127.0.0.1:{closed_port}").check(
            "down"
        )

        for result in (timed_out, server_error, refused):
            assert not result.is_candidate
            assert not result.is_conclusive

    async def test_rate_limit_retry_budget_is_bounded(
        self, session: aiohttp.ClientSession, instagram: tuple[FakeInstagram, str]
    ) -> None:
        fake, base_url = instagram
        fake.queue("lim", Reply(429), Reply(429), Reply(429))
        checker = make_checker(session, base_url, max_rate_limit_retries=1)
        result = await checker.check("lim")

        # One retry, then surrender: the scanner backs off rather than hammering.
        assert result.status is CheckStatus.RATE_LIMITED
        assert result.attempts == 2
        assert fake.requests == ["lim", "lim"]

    async def test_a_successful_check_resets_the_throttle_streak(
        self, session: aiohttp.ClientSession, instagram: tuple[FakeInstagram, str]
    ) -> None:
        fake, base_url = instagram
        fake.queue("hot", Reply(429))
        fake.queue("cool", Reply(404))
        limiter = RateLimiter(concurrency=2, delay=0.0, cooldown=0.0)
        checker = make_checker(session, base_url, limiter=limiter)

        await checker.check("hot")
        assert limiter.consecutive_rate_limits == 1
        await checker.check("cool")
        assert limiter.consecutive_rate_limits == 0

    async def test_the_session_identifies_the_tool(
        self, session: aiohttp.ClientSession
    ) -> None:
        assert session.headers["User-Agent"] == "tests/1.0"

    async def test_one_session_is_reused_across_checks(
        self, session: aiohttp.ClientSession, instagram: tuple[FakeInstagram, str]
    ) -> None:
        fake, base_url = instagram
        checker = make_checker(session, base_url)
        for name in ("one", "two", "three"):
            fake.queue(name, Reply(404))
            await checker.check(name)

        assert fake.requests == ["one", "two", "three"]
        assert not session.closed

    def test_url_uses_the_configured_base(self, session: aiohttp.ClientSession) -> None:
        checker = make_checker(session, "https://example.test/")
        assert checker.url_for("abc") == "https://example.test/abc/"
