"""Scan orchestration.

The scanner is the only component that knows the *shape* of a run: which
lengths to search, in what order, when to checkpoint, and when to stop. It
delegates everything else - generating candidates, making requests, pacing,
persisting, reporting - to collaborators passed in by the CLI.

Work is executed in bounded batches::

    generator -> batch (batch_size) -> bounded queue -> N workers -> results
                                                                 -> checkpoint

Only ``concurrency`` tasks exist at any moment, and only ``batch_size``
candidates are ever held in memory, regardless of how large the search space is.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Iterator
from itertools import islice

from .checker import UsernameChecker
from .config import Config
from .generator import UsernameGenerator
from .models import (
    Candidate,
    CheckResult,
    CheckStatus,
    ScanReport,
    ScanState,
    ScanStats,
    StopReason,
)
from .persistence import StateStore
from .progress import ProgressReporter
from .rate_limiter import RateLimiter

logger = logging.getLogger(__name__)

#: Inconclusive results are kept for the report, but never without bound.
MAX_RETAINED_ERRORS = 500


class Scanner:
    """Runs a resumable, bounded scan over the configured search space."""

    def __init__(
        self,
        *,
        config: Config,
        checker: UsernameChecker,
        generator: UsernameGenerator,
        rate_limiter: RateLimiter,
        state_store: StateStore,
        reporter: ProgressReporter,
        state: ScanState | None = None,
    ) -> None:
        self._config = config
        self._checker = checker
        self._generator = generator
        self._limiter = rate_limiter
        self._store = state_store
        self._reporter = reporter

        self._state = state or ScanState(fingerprint=config.fingerprint())
        self._stats = ScanStats(
            checked=self._state.checked,
            taken=self._state.taken,
            errors=self._state.errors,
            candidates=len(self._state.found),
        )
        self._results: list[CheckResult] = list(self._state.found)
        self._errors_retained = 0
        self._stop_event = asyncio.Event()
        self._stop_reason: StopReason | None = None
        self._deadline: float | None = (
            time.monotonic() + config.time_limit if config.time_limit else None
        )

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------
    @property
    def state(self) -> ScanState:
        return self._state

    @property
    def stats(self) -> ScanStats:
        return self._stats

    def request_stop(self, reason: StopReason = StopReason.INTERRUPTED) -> None:
        """Ask the scan to wind down at the next safe point."""
        if self._stop_reason is None:
            self._stop_reason = reason
            logger.info("stop requested: %s", reason)
        self._stop_event.set()

    async def run(self) -> ScanReport:
        """Execute the scan and return a report. Always persists state."""
        self._reporter.start(self._config)
        try:
            await self._run_lengths()
        finally:
            self._checkpoint()
            self._reporter.close()

        report = ScanReport(
            stop_reason=self._stop_reason or StopReason.COMPLETED,
            stats=self._stats,
            results=self._results,
            state=self._state,
        )
        self._reporter.finish(report)
        return report

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------
    async def _run_lengths(self) -> None:
        config = self._config
        for length in self._generator.iter_lengths(config.min_length, config.max_length):
            if length in self._state.completed_lengths:
                logger.debug("length %d already completed; skipping", length)
                continue
            if self._stop_event.is_set():
                return

            start_index = (
                self._state.current_index if self._state.search_length == length else 0
            )
            self._state.search_length = length
            self._state.current_index = start_index
            self._stats.current_length = length
            self._stats.length_total = self._generator.space_size(length)
            self._stats.current_index = start_index

            logger.info(
                "scanning length %d from index %d of %d",
                length,
                start_index,
                self._stats.length_total,
            )
            exhausted = await self._run_length(length, start_index)
            if not exhausted:
                return

            self._state.completed_lengths.append(length)
            self._state.current_index = 0
            self._checkpoint()

            if self._config.stop_on_first and self._stats.candidates > 0:
                self.request_stop(StopReason.FOUND)
                return

    async def _run_length(self, length: int, start_index: int) -> bool:
        """Scan one length. Returns True when the length was fully covered."""
        candidates = self._generator.generate(length, start_index)
        total = self._generator.space_size(length)

        for batch in _batched(candidates, self._config.batch_size):
            await self._run_batch(batch)

            # A batch is the checkpoint unit: every candidate in it has been
            # resolved, so the next index is safe to restart from.
            next_index = batch[-1].index + 1
            self._state.current_index = next_index
            self._stats.current_index = next_index
            self._sync_state()
            self._checkpoint()

            if self._should_stop():
                return False

        self._stats.current_index = total
        return True

    async def _run_batch(self, batch: list[Candidate]) -> None:
        queue: asyncio.Queue[Candidate | None] = asyncio.Queue(
            maxsize=self._config.concurrency * 2
        )
        workers = [
            asyncio.create_task(self._worker(queue), name=f"worker-{index}")
            for index in range(self._config.concurrency)
        ]
        try:
            for candidate in batch:
                if self._stop_event.is_set():
                    break
                await queue.put(candidate)
        finally:
            for _ in workers:
                await queue.put(None)
            await asyncio.gather(*workers)

    async def _worker(self, queue: asyncio.Queue[Candidate | None]) -> None:
        while True:
            candidate = await queue.get()
            if candidate is None:
                return
            # Stop conditions are evaluated per candidate, not merely at batch
            # boundaries. A paused limiter can stretch a single batch across
            # hours, so a batch-boundary check would let an open circuit
            # breaker, a time limit or a check budget go unenforced for the
            # whole of it.
            if self._stop_event.is_set() or self._should_stop():
                continue  # drain remaining items without issuing requests

            self._stats.current_username = candidate.username
            try:
                result = await self._checker.check(candidate.username)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("unexpected error checking %s: %s", candidate.username, exc)
                result = CheckResult(
                    username=candidate.username,
                    status=CheckStatus.UNKNOWN,
                    error=str(exc),
                )
            self._record(result)

    def _record(self, result: CheckResult) -> None:
        self._stats.record(result)
        self._stats.rate_limited_now = self._limiter.paused

        if result.is_candidate:
            self._results.append(result)
            self._state.found.append(result)
            logger.info(
                "POSSIBLY_AVAILABLE: %s (HTTP %s) - verify with Instagram before use",
                result.username,
                result.http_status,
            )
        elif not result.is_conclusive and self._errors_retained < MAX_RETAINED_ERRORS:
            self._errors_retained += 1
            self._results.append(result)

        self._reporter.update(self._stats)

    def _should_stop(self) -> bool:
        if self._stop_event.is_set():
            return True
        if self._limiter.circuit_open:
            logger.warning(
                "circuit breaker open after %d consecutive rate limits; "
                "stopping and saving progress",
                self._limiter.consecutive_rate_limits,
            )
            self.request_stop(StopReason.RATE_LIMITED)
            return True
        if self._config.stop_on_first and self._stats.candidates > 0:
            self.request_stop(StopReason.FOUND)
            return True
        if self._config.max_checks and self._stats.checked >= self._config.max_checks:
            self.request_stop(StopReason.MAX_CHECKS)
            return True
        if self._deadline is not None and time.monotonic() >= self._deadline:
            self.request_stop(StopReason.TIME_LIMIT)
            return True
        return False

    def _sync_state(self) -> None:
        self._state.checked = self._stats.checked
        self._state.taken = self._stats.taken
        self._state.errors = self._stats.errors
        self._state.fingerprint = self._config.fingerprint()

    def _checkpoint(self) -> None:
        self._sync_state()
        try:
            self._store.save(self._state)
        except OSError as exc:
            logger.error("could not save state to %s: %s", self._store.path, exc)


def _batched(items: Iterator[Candidate], size: int) -> Iterator[list[Candidate]]:
    """Yield lists of at most ``size`` candidates, pulling lazily."""
    while True:
        batch = list(islice(items, size))
        if not batch:
            return
        yield batch
