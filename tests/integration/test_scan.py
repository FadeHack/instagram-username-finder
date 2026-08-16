"""End-to-end scans against fake transports.

These exercise the scanner wired to real collaborators (generator, limiter,
state store, output) with only the HTTP layer replaced.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path

import pytest

from instagram_username_finder.config import Config
from instagram_username_finder.generator import UsernameGenerator
from instagram_username_finder.models import (
    Charset,
    CheckResult,
    CheckStatus,
    OutputFormat,
    ScanState,
    StopReason,
)
from instagram_username_finder.output import render_csv, write_report
from instagram_username_finder.persistence import StateStore
from instagram_username_finder.progress import NullProgressReporter
from instagram_username_finder.rate_limiter import RateLimiter
from instagram_username_finder.scanner import Scanner

pytestmark = pytest.mark.integration


class FakeChecker:
    """A checker whose verdict is decided by a caller-supplied function."""

    def __init__(self, verdict: Callable[[str], CheckResult]) -> None:
        self._verdict = verdict
        self.calls: list[str] = []

    async def check(self, username: str) -> CheckResult:
        self.calls.append(username)
        await asyncio.sleep(0)
        return self._verdict(username)


def taken(username: str) -> CheckResult:
    return CheckResult(username, CheckStatus.TAKEN, http_status=200, latency_ms=10.0)


def available(username: str) -> CheckResult:
    return CheckResult(
        username, CheckStatus.POSSIBLY_AVAILABLE, http_status=404, latency_ms=10.0
    )


def only_available(*targets: str) -> Callable[[str], CheckResult]:
    wanted = set(targets)
    return lambda username: available(username) if username in wanted else taken(username)


def build_scanner(
    config: Config,
    checker: FakeChecker,
    *,
    limiter: RateLimiter | None = None,
    state: ScanState | None = None,
) -> Scanner:
    return Scanner(
        config=config,
        checker=checker,
        generator=UsernameGenerator(config.alphabet),
        rate_limiter=limiter or RateLimiter(concurrency=config.concurrency, delay=0.0),
        state_store=StateStore(config.state_file),
        reporter=NullProgressReporter(),
        state=state,
    )


def scan_config(tmp_path: Path, **overrides: object) -> Config:
    options: dict[str, object] = {
        "min_length": 2,
        "max_length": 2,
        "charset": Charset.CUSTOM,
        "characters": "abc",
        "concurrency": 3,
        "batch_size": 3,
        "delay": 0.0,
        "max_retries": 0,
        "state_file": tmp_path / "state.json",
        "no_progress": True,
        "stop_on_first": False,
    }
    options.update(overrides)
    return Config(**options)  # type: ignore[arg-type]


class TestFullScan:
    async def test_covers_the_whole_space_exactly_once(self, tmp_path: Path) -> None:
        checker = FakeChecker(taken)
        config = scan_config(tmp_path)
        report = await build_scanner(config, checker).run()

        assert len(checker.calls) == 9  # 3 characters, length 2
        assert sorted(checker.calls) == sorted(set(checker.calls))
        assert report.stop_reason is StopReason.COMPLETED
        assert report.stats.checked == 9
        assert report.stats.taken == 9
        assert report.candidates == []

    async def test_searches_shortest_length_first(self, tmp_path: Path) -> None:
        checker = FakeChecker(taken)
        config = scan_config(tmp_path, min_length=1, max_length=2)
        await build_scanner(config, checker).run()

        lengths = [len(name) for name in checker.calls]
        assert lengths == sorted(lengths)
        assert lengths[0] == 1 and lengths[-1] == 2

    async def test_collects_every_candidate_with_collect_all(self, tmp_path: Path) -> None:
        checker = FakeChecker(only_available("ab", "cb"))
        config = scan_config(tmp_path, stop_on_first=False)
        report = await build_scanner(config, checker).run()

        assert {result.username for result in report.candidates} == {"ab", "cb"}
        assert report.stats.checked == 9

    async def test_stops_at_the_first_candidate(self, tmp_path: Path) -> None:
        checker = FakeChecker(only_available("ab"))
        config = scan_config(tmp_path, stop_on_first=True, batch_size=1, concurrency=1)
        report = await build_scanner(config, checker).run()

        assert report.stop_reason is StopReason.FOUND
        assert [result.username for result in report.candidates] == ["ab"]
        assert len(checker.calls) < 9  # stopped early

    async def test_stops_at_the_shortest_length_that_has_a_candidate(
        self, tmp_path: Path
    ) -> None:
        checker = FakeChecker(only_available("a", "bb"))
        config = scan_config(
            tmp_path, min_length=1, max_length=2, stop_on_first=True, batch_size=1
        )
        report = await build_scanner(config, checker).run()

        assert [result.username for result in report.candidates] == ["a"]
        assert all(len(name) == 1 for name in checker.calls)

    async def test_skips_structurally_invalid_usernames(self, tmp_path: Path) -> None:
        checker = FakeChecker(taken)
        config = scan_config(tmp_path, characters="a.", min_length=2, max_length=2)
        await build_scanner(config, checker).run()
        assert checker.calls == ["aa"]

    async def test_concurrency_is_respected(self, tmp_path: Path) -> None:
        in_flight = 0
        peak = 0

        class CountingChecker(FakeChecker):
            async def check(self, username: str) -> CheckResult:
                nonlocal in_flight, peak
                in_flight += 1
                peak = max(peak, in_flight)
                await asyncio.sleep(0)
                in_flight -= 1
                return await super().check(username)

        config = scan_config(tmp_path, concurrency=2, batch_size=9)
        await build_scanner(config, CountingChecker(taken)).run()
        assert peak <= 2


class TestStopsMidBatch:
    """Stop conditions must not wait for a batch boundary.

    A paused rate limiter can stretch one batch across hours, so a scan that
    only re-checks its stop conditions between batches would keep issuing
    requests long after it decided to stop. Each test here uses a batch large
    enough to cover the whole search space, so a batch-boundary-only check
    would let the run continue to the end.
    """

    async def test_open_circuit_breaker_stops_within_the_batch(
        self, tmp_path: Path
    ) -> None:
        limiter = RateLimiter(concurrency=1, delay=0.0, cooldown=0.0, circuit_threshold=2)

        def throttled(username: str) -> CheckResult:
            limiter.record_rate_limited(retry_after=0.0)
            return CheckResult(username, CheckStatus.RATE_LIMITED, http_status=429)

        checker = FakeChecker(throttled)
        config = scan_config(tmp_path, batch_size=9, concurrency=1)
        report = await build_scanner(config, checker, limiter=limiter).run()

        # Two throttled responses open the breaker; nothing after it is sent.
        assert limiter.circuit_open
        assert len(checker.calls) == 2
        assert report.stop_reason is StopReason.RATE_LIMITED

    async def test_max_checks_stops_within_the_batch(self, tmp_path: Path) -> None:
        checker = FakeChecker(taken)
        config = scan_config(tmp_path, max_checks=2, batch_size=9, concurrency=1)
        report = await build_scanner(config, checker).run()

        assert len(checker.calls) == 2
        assert report.stats.checked == 2
        assert report.stop_reason is StopReason.MAX_CHECKS

    async def test_time_limit_stops_within_the_batch(self, tmp_path: Path) -> None:
        async def slow(username: str) -> CheckResult:
            await asyncio.sleep(0.02)
            return taken(username)

        class SlowChecker(FakeChecker):
            async def check(self, username: str) -> CheckResult:
                self.calls.append(username)
                return await slow(username)

        checker = SlowChecker(taken)
        config = scan_config(tmp_path, time_limit=0.01, batch_size=9, concurrency=1)
        report = await build_scanner(config, checker).run()

        assert len(checker.calls) < 9
        assert report.stop_reason is StopReason.TIME_LIMIT

    async def test_stop_on_first_stops_within_the_batch(self, tmp_path: Path) -> None:
        checker = FakeChecker(only_available("ab"))
        config = scan_config(tmp_path, stop_on_first=True, batch_size=9, concurrency=1)
        report = await build_scanner(config, checker).run()

        # "aa", "ab" -> candidate found; "ac" onwards is never requested.
        assert checker.calls == ["aa", "ab"]
        assert report.stop_reason is StopReason.FOUND


class TestLimits:
    async def test_max_checks_bounds_the_run(self, tmp_path: Path) -> None:
        checker = FakeChecker(taken)
        config = scan_config(tmp_path, max_checks=3, batch_size=3)
        report = await build_scanner(config, checker).run()

        assert report.stop_reason is StopReason.MAX_CHECKS
        assert report.stats.checked == 3

    async def test_rate_limiting_stops_the_scan_and_saves_progress(
        self, tmp_path: Path
    ) -> None:
        limiter = RateLimiter(concurrency=1, delay=0.0, cooldown=0.0, circuit_threshold=2)

        def throttled(username: str) -> CheckResult:
            limiter.record_rate_limited(retry_after=0.0)
            return CheckResult(username, CheckStatus.RATE_LIMITED, http_status=429)

        config = scan_config(tmp_path, batch_size=2, concurrency=1)
        report = await build_scanner(config, FakeChecker(throttled), limiter=limiter).run()

        assert report.stop_reason is StopReason.RATE_LIMITED
        assert report.candidates == []
        assert StateStore(config.state_file).load().current_index > 0

    async def test_interruption_stops_at_the_next_checkpoint(self, tmp_path: Path) -> None:
        config = scan_config(tmp_path, batch_size=3, concurrency=1)
        scanner = build_scanner(config, FakeChecker(taken))

        original = scanner._checker.check

        async def check_then_interrupt(username: str) -> CheckResult:
            result = await original(username)
            if len(scanner._checker.calls) == 3:
                scanner.request_stop(StopReason.INTERRUPTED)
            return result

        scanner._checker.check = check_then_interrupt  # type: ignore[method-assign]
        report = await scanner.run()

        assert report.stop_reason is StopReason.INTERRUPTED
        assert report.stats.checked == 3
        assert StateStore(config.state_file).load().current_index == 3


class TestResume:
    async def test_resumed_scan_does_not_recheck_earlier_usernames(
        self, tmp_path: Path
    ) -> None:
        config = scan_config(tmp_path, batch_size=3, max_checks=3)
        first = FakeChecker(taken)
        await build_scanner(config, first).run()
        assert len(first.calls) == 3

        resumed_state = StateStore(config.state_file).load()
        second_config = scan_config(tmp_path, batch_size=3)
        second = FakeChecker(taken)
        report = await build_scanner(second_config, second, state=resumed_state).run()

        assert set(first.calls).isdisjoint(second.calls)
        assert sorted(first.calls + second.calls) == sorted(
            {a + b for a in "abc" for b in "abc"}
        )
        assert report.stats.checked == 9  # counters carry across the resume

    async def test_completed_lengths_are_not_rescanned(self, tmp_path: Path) -> None:
        config = scan_config(tmp_path, min_length=1, max_length=2)
        state = ScanState(
            completed_lengths=[1], fingerprint=config.fingerprint(), search_length=2
        )
        checker = FakeChecker(taken)
        await build_scanner(config, checker, state=state).run()
        assert all(len(name) == 2 for name in checker.calls)

    async def test_candidates_survive_a_resume(self, tmp_path: Path) -> None:
        config = scan_config(tmp_path)
        state = ScanState(
            fingerprint=config.fingerprint(),
            found=[CheckResult("zz", CheckStatus.POSSIBLY_AVAILABLE, http_status=404)],
            completed_lengths=[2],
        )
        report = await build_scanner(config, FakeChecker(taken), state=state).run()
        assert [result.username for result in report.candidates] == ["zz"]


class TestStatePersistence:
    async def test_state_is_checkpointed_during_the_scan(self, tmp_path: Path) -> None:
        config = scan_config(tmp_path, batch_size=3)
        await build_scanner(config, FakeChecker(taken)).run()

        payload = json.loads(config.state_file.read_text(encoding="utf-8"))
        assert payload["version"] == 1
        assert payload["checked"] == 9
        assert payload["completed_lengths"] == [2]
        assert payload["fingerprint"] == config.fingerprint()

    async def test_state_is_written_even_when_the_scan_raises(self, tmp_path: Path) -> None:
        config = scan_config(tmp_path)

        def explode(username: str) -> CheckResult:
            raise RuntimeError("transport exploded")

        scanner = build_scanner(config, FakeChecker(explode))
        report = await scanner.run()

        # Worker-level failures degrade to UNKNOWN rather than killing the scan.
        assert report.stats.errors == 9
        assert report.candidates == []
        assert StateStore(config.state_file).exists


class TestOutputIntegration:
    async def test_results_export_to_every_format(self, tmp_path: Path) -> None:
        config = scan_config(tmp_path, stop_on_first=False)
        report = await build_scanner(config, FakeChecker(only_available("bc"))).run()

        for fmt in OutputFormat:
            target = tmp_path / f"results.{fmt.value}"
            write_report(report, target, fmt)
            assert "bc" in target.read_text(encoding="utf-8")

        payload = json.loads((tmp_path / "results.json").read_text(encoding="utf-8"))
        assert payload["summary"]["possibly_available"] == 1
        assert payload["results"][0]["status"] == "possibly_available"
        assert "username,status" in render_csv(report.results)
