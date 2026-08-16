"""Retry decisions, backoff growth and Retry-After parsing."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import pytest

from instagram_username_finder.retry import (
    RetryPolicy,
    RetryReason,
    parse_retry_after,
)


def policy(**kwargs: float | int) -> RetryPolicy:
    options: dict[str, object] = {
        "max_retries": 3,
        "base_delay": 1.0,
        "max_delay": 60.0,
        "jitter": 0.0,
    }
    options.update(kwargs)
    return RetryPolicy(**options)  # type: ignore[arg-type]


class TestValidation:
    @pytest.mark.parametrize(
        "kwargs",
        [
            {"max_retries": -1},
            {"base_delay": 0.0},
            {"max_delay": 0.5},
            {"jitter": 1.5},
            {"max_rate_limit_retries": -1},
        ],
    )
    def test_rejects_invalid_settings(self, kwargs: dict[str, float]) -> None:
        with pytest.raises(ValueError):
            policy(**kwargs)


class TestBackoff:
    def test_grows_exponentially(self) -> None:
        retry = policy(base_delay=1.0)
        assert [retry.backoff(n) for n in (1, 2, 3, 4)] == [1.0, 2.0, 4.0, 8.0]

    def test_is_capped_at_max_delay(self) -> None:
        retry = policy(base_delay=1.0, max_delay=5.0)
        assert retry.backoff(10) == 5.0

    def test_jitter_stays_within_the_window(self) -> None:
        retry = RetryPolicy(base_delay=4.0, jitter=0.5, rng=lambda: 0.0)
        assert retry.backoff(1) == pytest.approx(2.0)
        retry = RetryPolicy(base_delay=4.0, jitter=0.5, rng=lambda: 1.0)
        assert retry.backoff(1) == pytest.approx(6.0)

    def test_rejects_attempt_zero(self) -> None:
        with pytest.raises(ValueError):
            policy().backoff(0)


class TestDecisions:
    def test_transport_errors_are_retried(self) -> None:
        decision = policy().decide(1, exception=ConnectionResetError())
        assert decision.should_retry
        assert decision.reason is RetryReason.TRANSPORT
        assert decision.delay == 1.0

    @pytest.mark.parametrize("status", [408, 500, 502, 503, 504])
    def test_server_errors_are_retried(self, status: int) -> None:
        decision = policy().decide(1, status=status)
        assert decision.should_retry
        assert decision.reason is RetryReason.SERVER_ERROR

    @pytest.mark.parametrize("status", [200, 301, 404, 418])
    def test_non_retryable_statuses_are_not_retried(self, status: int) -> None:
        decision = policy().decide(1, status=status)
        assert not decision.should_retry
        assert decision.reason is RetryReason.NOT_RETRYABLE

    def test_budget_is_exhausted_after_max_retries(self) -> None:
        retry = policy(max_retries=2)
        assert retry.decide(2, status=500).should_retry
        exhausted = retry.decide(3, status=500)
        assert not exhausted.should_retry
        assert exhausted.reason is RetryReason.EXHAUSTED

    def test_zero_retries_means_never_retry(self) -> None:
        assert not policy(max_retries=0).decide(1, status=500).should_retry


class TestRateLimitDecisions:
    @pytest.mark.parametrize("status", [403, 429])
    def test_throttling_is_flagged(self, status: int) -> None:
        decision = policy(max_rate_limit_retries=1).decide(1, status=status)
        assert decision.rate_limited
        assert decision.reason is RetryReason.RATE_LIMIT

    def test_retry_after_is_honoured(self) -> None:
        decision = policy(max_rate_limit_retries=1).decide(1, status=429, retry_after=17.0)
        assert decision.delay == pytest.approx(17.0)

    def test_retry_after_is_capped_at_max_delay(self) -> None:
        decision = policy(max_rate_limit_retries=1, max_delay=30.0).decide(
            1, status=429, retry_after=3600.0
        )
        assert decision.delay == pytest.approx(30.0)

    def test_rate_limits_get_their_own_smaller_budget(self) -> None:
        retry = policy(max_retries=5, max_rate_limit_retries=1)
        assert retry.decide(1, status=429).should_retry
        surrendered = retry.decide(2, status=429)
        assert not surrendered.should_retry
        assert surrendered.rate_limited

    def test_persistent_throttling_is_never_retried_forever(self) -> None:
        retry = policy(max_rate_limit_retries=0)
        assert not retry.decide(1, status=429).should_retry


class TestParseRetryAfter:
    def test_parses_delta_seconds(self) -> None:
        assert parse_retry_after("120") == pytest.approx(120.0)

    def test_parses_http_dates(self) -> None:
        future = datetime.now(UTC) + timedelta(seconds=60)
        parsed = parse_retry_after(format_datetime(future, usegmt=True))
        assert parsed is not None
        assert 50 <= parsed <= 65

    def test_past_dates_clamp_to_zero(self) -> None:
        past = datetime.now(UTC) - timedelta(seconds=60)
        assert parse_retry_after(format_datetime(past, usegmt=True)) == 0.0

    def test_negative_seconds_clamp_to_zero(self) -> None:
        assert parse_retry_after("-5") == 0.0

    @pytest.mark.parametrize("value", [None, "", "   ", "soon", "not-a-date"])
    def test_unparseable_values_return_none(self, value: str | None) -> None:
        assert parse_retry_after(value) is None
