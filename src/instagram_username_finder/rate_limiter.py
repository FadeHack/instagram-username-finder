"""Outbound request pacing, cooldown and circuit breaking.

The limiter is the single place that decides *when* a request may leave the
process. It combines three mechanisms:

* a semaphore, bounding how many requests are in flight;
* a paced schedule, guaranteeing a minimum gap between request starts;
* a cooldown/circuit breaker, which halts traffic after throttling responses.

The circuit breaker never tries to work around a rate limit. When it opens, the
scanner's job is to persist state and stop.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

Clock = Callable[[], float]
Sleeper = Callable[[float], Awaitable[None]]


class RateLimiter:
    """Paces requests and pauses when the remote host pushes back."""

    def __init__(
        self,
        *,
        concurrency: int = 5,
        delay: float = 0.5,
        cooldown: float = 60.0,
        circuit_threshold: int = 5,
        clock: Clock = time.monotonic,
        sleeper: Sleeper | None = None,
    ) -> None:
        if concurrency < 1:
            raise ValueError("concurrency must be >= 1")
        if delay < 0:
            raise ValueError("delay must be >= 0")
        if cooldown < 0:
            raise ValueError("cooldown must be >= 0")
        if circuit_threshold < 1:
            raise ValueError("circuit_threshold must be >= 1")

        self.delay = delay
        self.cooldown = cooldown
        self.circuit_threshold = circuit_threshold
        self._clock = clock
        self._sleep: Sleeper = sleeper if sleeper is not None else asyncio.sleep
        self._semaphore = asyncio.Semaphore(concurrency)
        self._lock = asyncio.Lock()
        self._next_slot = 0.0
        self._paused_until = 0.0
        self._consecutive_rate_limits = 0
        self._circuit_open = False

    # ------------------------------------------------------------------
    # state
    # ------------------------------------------------------------------
    @property
    def circuit_open(self) -> bool:
        """True once throttling has persisted past the configured threshold."""
        return self._circuit_open

    @property
    def consecutive_rate_limits(self) -> int:
        return self._consecutive_rate_limits

    @property
    def paused(self) -> bool:
        return self._clock() < self._paused_until

    @property
    def pause_remaining(self) -> float:
        return max(0.0, self._paused_until - self._clock())

    # ------------------------------------------------------------------
    # acquisition
    # ------------------------------------------------------------------
    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        """Reserve capacity for exactly one outbound request."""
        async with self._semaphore:
            await self._wait_turn()
            yield

    async def _wait_turn(self) -> None:
        while True:
            async with self._lock:
                now = self._clock()
                paused = now < self._paused_until
                if paused:
                    wait_for = self._paused_until - now
                else:
                    start_at = max(now, self._next_slot)
                    self._next_slot = start_at + self.delay
                    wait_for = start_at - now
            # Sleeping happens outside the lock so waiting workers do not
            # serialise behind whichever task happens to hold it.
            if wait_for > 0:
                await self._sleep(wait_for)
            if not paused:
                return

    # ------------------------------------------------------------------
    # feedback
    # ------------------------------------------------------------------
    def record_success(self) -> None:
        """Reset throttling state after a clean response."""
        self._consecutive_rate_limits = 0

    def record_rate_limited(self, retry_after: float | None = None) -> float:
        """Register a 403/429 and enter cooldown. Returns the pause length."""
        self._consecutive_rate_limits += 1
        pause = self.cooldown if retry_after is None else max(retry_after, 0.0)
        # Successive throttles widen the cooldown window.
        pause *= min(self._consecutive_rate_limits, self.circuit_threshold)
        self._paused_until = max(self._paused_until, self._clock() + pause)
        if self._consecutive_rate_limits >= self.circuit_threshold:
            self._circuit_open = True
        return pause

    def reset_circuit(self) -> None:
        """Close the breaker and clear the cooldown (used when resuming)."""
        self._circuit_open = False
        self._consecutive_rate_limits = 0
        self._paused_until = 0.0
