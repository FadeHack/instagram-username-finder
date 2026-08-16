"""Pacing, cooldown and circuit-breaker behaviour.

Time is simulated: a fake clock advances only when the limiter sleeps, so the
tests assert on scheduling decisions rather than on wall-clock timing.
"""

from __future__ import annotations

import asyncio

import pytest

from instagram_username_finder.rate_limiter import RateLimiter


class FakeClock:
    """A monotonic clock that only moves when the limiter sleeps."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def time(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds
        await asyncio.sleep(0)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


def make_limiter(clock: FakeClock, **kwargs: float | int) -> RateLimiter:
    options: dict[str, object] = {
        "concurrency": 2,
        "delay": 1.0,
        "cooldown": 30.0,
        "circuit_threshold": 3,
    }
    options.update(kwargs)
    return RateLimiter(clock=clock.time, sleeper=clock.sleep, **options)  # type: ignore[arg-type]


class TestValidation:
    @pytest.mark.parametrize(
        "kwargs",
        [
            {"concurrency": 0},
            {"delay": -1.0},
            {"cooldown": -1.0},
            {"circuit_threshold": 0},
        ],
    )
    def test_rejects_invalid_settings(self, kwargs: dict[str, float]) -> None:
        with pytest.raises(ValueError):
            RateLimiter(**kwargs)  # type: ignore[arg-type]


class TestDelay:
    async def test_first_request_does_not_wait(self, clock: FakeClock) -> None:
        limiter = make_limiter(clock)
        async with limiter.slot():
            pass
        assert clock.sleeps == []

    async def test_requests_are_spaced_by_the_delay(self, clock: FakeClock) -> None:
        limiter = make_limiter(clock, delay=0.5)
        for _ in range(3):
            async with limiter.slot():
                pass
        assert clock.sleeps == [0.5, 0.5]
        assert clock.now == pytest.approx(1.0)

    async def test_zero_delay_never_sleeps(self, clock: FakeClock) -> None:
        limiter = make_limiter(clock, delay=0.0)
        for _ in range(5):
            async with limiter.slot():
                pass
        assert clock.sleeps == []

    async def test_concurrency_is_bounded_by_the_semaphore(self, clock: FakeClock) -> None:
        limiter = make_limiter(clock, concurrency=2, delay=0.0)
        in_flight = 0
        peak = 0

        async def worker() -> None:
            nonlocal in_flight, peak
            async with limiter.slot():
                in_flight += 1
                peak = max(peak, in_flight)
                await asyncio.sleep(0)
                in_flight -= 1

        await asyncio.gather(*(worker() for _ in range(8)))
        assert peak <= 2


class TestRateLimitFeedback:
    async def test_rate_limit_pauses_for_the_cooldown(self, clock: FakeClock) -> None:
        limiter = make_limiter(clock, cooldown=30.0)
        pause = limiter.record_rate_limited()
        assert pause == pytest.approx(30.0)
        assert limiter.paused
        assert limiter.pause_remaining == pytest.approx(30.0)

    async def test_retry_after_takes_precedence_over_the_cooldown(
        self, clock: FakeClock
    ) -> None:
        limiter = make_limiter(clock, cooldown=30.0)
        assert limiter.record_rate_limited(retry_after=5.0) == pytest.approx(5.0)

    async def test_repeated_throttling_widens_the_pause(self, clock: FakeClock) -> None:
        limiter = make_limiter(clock, cooldown=10.0, circuit_threshold=5)
        first = limiter.record_rate_limited()
        limiter._paused_until = 0.0
        second = limiter.record_rate_limited()
        assert second > first

    async def test_acquiring_during_a_pause_waits_it_out(self, clock: FakeClock) -> None:
        limiter = make_limiter(clock, delay=0.0, cooldown=20.0)
        limiter.record_rate_limited()
        async with limiter.slot():
            pass
        assert clock.sleeps == [20.0]
        assert not limiter.paused

    async def test_success_resets_the_streak(self, clock: FakeClock) -> None:
        limiter = make_limiter(clock)
        limiter.record_rate_limited()
        limiter.record_success()
        assert limiter.consecutive_rate_limits == 0


class TestCircuitBreaker:
    async def test_opens_after_persistent_throttling(self, clock: FakeClock) -> None:
        limiter = make_limiter(clock, circuit_threshold=3)
        for _ in range(2):
            limiter.record_rate_limited()
            assert not limiter.circuit_open
        limiter.record_rate_limited()
        assert limiter.circuit_open

    async def test_success_before_the_threshold_keeps_it_closed(
        self, clock: FakeClock
    ) -> None:
        limiter = make_limiter(clock, circuit_threshold=3)
        limiter.record_rate_limited()
        limiter.record_rate_limited()
        limiter.record_success()
        limiter.record_rate_limited()
        assert not limiter.circuit_open

    async def test_reset_closes_the_breaker(self, clock: FakeClock) -> None:
        limiter = make_limiter(clock, circuit_threshold=1)
        limiter.record_rate_limited()
        assert limiter.circuit_open
        limiter.reset_circuit()
        assert not limiter.circuit_open
        assert not limiter.paused
