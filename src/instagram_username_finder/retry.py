"""Retry decisions and backoff computation.

This module is pure: it decides *whether* and *how long* to wait, but never
sleeps and never touches the network. That keeps the policy exhaustively
testable without patching timers.
"""

from __future__ import annotations

import contextlib
import random
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from enum import Enum
from typing import Final

#: Server-side failures that are worth retrying unchanged.
RETRYABLE_STATUSES: Final = frozenset({408, 425, 500, 502, 503, 504})

#: Statuses that indicate throttling rather than a per-request failure.
RATE_LIMIT_STATUSES: Final = frozenset({403, 429})


class RetryReason(str, Enum):
    """Why a retry decision was made."""

    TRANSPORT = "transport"
    SERVER_ERROR = "server_error"
    RATE_LIMIT = "rate_limit"
    EXHAUSTED = "exhausted"
    NOT_RETRYABLE = "not_retryable"

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class RetryDecision:
    """The outcome of consulting the policy for one failed attempt."""

    should_retry: bool
    delay: float
    reason: RetryReason
    rate_limited: bool = False


@dataclass(slots=True)
class RetryPolicy:
    """Exponential backoff with full jitter and rate-limit awareness.

    Rate-limited responses get their own, smaller retry budget: repeatedly
    hammering a throttled endpoint is exactly the behaviour this project must
    not exhibit. Once that budget is spent the request is surrendered so the
    scanner can back off, persist and pause.
    """

    max_retries: int = 3
    base_delay: float = 1.0
    max_delay: float = 60.0
    jitter: float = 0.5
    max_rate_limit_retries: int = 1
    rng: Callable[[], float] = random.random

    def __post_init__(self) -> None:
        if self.max_retries < 0:
            raise ValueError("max_retries must be >= 0")
        if self.base_delay <= 0:
            raise ValueError("base_delay must be > 0")
        if self.max_delay < self.base_delay:
            raise ValueError("max_delay must be >= base_delay")
        if not 0.0 <= self.jitter <= 1.0:
            raise ValueError("jitter must be between 0 and 1")
        if self.max_rate_limit_retries < 0:
            raise ValueError("max_rate_limit_retries must be >= 0")

    # ------------------------------------------------------------------
    def backoff(self, attempt: int) -> float:
        """Delay before ``attempt`` + 1, in seconds (``attempt`` is 1-based)."""
        if attempt < 1:
            raise ValueError("attempt must be >= 1")
        raw = float(min(self.base_delay * (2 ** (attempt - 1)), self.max_delay))
        if self.jitter == 0:
            return raw
        # Full jitter within the configured fraction of the window.
        spread = raw * self.jitter
        return float(max(0.0, raw - spread + spread * 2 * self.rng()))

    def decide(
        self,
        attempt: int,
        *,
        status: int | None = None,
        exception: BaseException | None = None,
        retry_after: float | None = None,
    ) -> RetryDecision:
        """Decide what to do after attempt number ``attempt`` failed."""
        if status is not None and status in RATE_LIMIT_STATUSES:
            return self._decide_rate_limited(attempt, retry_after)

        retryable = exception is not None or (
            status is not None and status in RETRYABLE_STATUSES
        )
        if not retryable:
            return RetryDecision(False, 0.0, RetryReason.NOT_RETRYABLE)

        reason = (
            RetryReason.TRANSPORT if exception is not None else RetryReason.SERVER_ERROR
        )
        if attempt > self.max_retries:
            return RetryDecision(False, 0.0, RetryReason.EXHAUSTED)
        return RetryDecision(True, self.backoff(attempt), reason)

    def _decide_rate_limited(
        self, attempt: int, retry_after: float | None
    ) -> RetryDecision:
        if attempt > self.max_rate_limit_retries:
            return RetryDecision(False, 0.0, RetryReason.EXHAUSTED, rate_limited=True)
        delay = self.backoff(attempt) if retry_after is None else retry_after
        return RetryDecision(
            True,
            min(delay, self.max_delay),
            RetryReason.RATE_LIMIT,
            rate_limited=True,
        )


def parse_retry_after(value: str | None) -> float | None:
    """Parse a ``Retry-After`` header value into seconds.

    Supports the delta-seconds form and the HTTP-date form. Returns ``None``
    when the header is absent or unparseable, and never returns a negative
    value.
    """
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    # The delta-seconds form is by far the common case.
    with contextlib.suppress(ValueError):
        return max(0.0, float(text))

    try:
        deadline = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None

    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=UTC)
    return max(0.0, (deadline - datetime.now(UTC)).total_seconds())
