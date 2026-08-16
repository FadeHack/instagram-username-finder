"""Terminal progress reporting.

Progress is rendered to **stderr** so that ``--output -`` can stream JSON or CSV
to stdout without interleaving. Rich is used when it is installed and the
stream is a TTY; otherwise a plain, log-friendly reporter takes over.
"""

from __future__ import annotations

import sys
from types import TracebackType
from typing import Protocol, TextIO

from .config import Config
from .models import ScanReport, ScanStats, StopReason

try:  # pragma: no cover - exercised implicitly by the reporter factory
    from rich.console import Console
    from rich.live import Live
    from rich.table import Table

    RICH_AVAILABLE = True
except ImportError:  # pragma: no cover - optional dependency fallback
    RICH_AVAILABLE = False

TITLE = "Instagram Username Finder"
RULE = "─" * 44


class ProgressReporter(Protocol):
    """Receives scan lifecycle events for display."""

    def start(self, config: Config) -> None: ...

    def update(self, stats: ScanStats) -> None: ...

    def finish(self, report: ScanReport) -> None: ...

    def close(self) -> None: ...


class NullProgressReporter:
    """Displays nothing. Used by ``--quiet`` and by non-interactive runs."""

    def start(self, config: Config) -> None:
        return None

    def update(self, stats: ScanStats) -> None:
        return None

    def finish(self, report: ScanReport) -> None:
        return None

    def close(self) -> None:
        return None

    def __enter__(self) -> ProgressReporter:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


class PlainProgressReporter(NullProgressReporter):
    """Line-oriented progress, suitable for CI logs and piped output."""

    def __init__(self, stream: TextIO | None = None, every: int = 250) -> None:
        self._stream = stream if stream is not None else sys.stderr
        self._every = max(1, every)
        self._last = 0

    def start(self, config: Config) -> None:
        self._write(
            f"{TITLE}: scanning {config.min_length}-{config.max_length} characters "
            f"over charset '{config.charset}' "
            f"(concurrency={config.concurrency}, delay={config.delay}s)"
        )

    def update(self, stats: ScanStats) -> None:
        if stats.checked - self._last < self._every:
            return
        self._last = stats.checked
        self._write(
            f"length={stats.current_length} "
            f"checked={stats.checked:,} "
            f"progress={stats.completion:.1%} "
            f"taken={stats.taken:,} "
            f"candidates={stats.candidates:,} "
            f"errors={stats.errors:,}"
        )

    def finish(self, report: ScanReport) -> None:
        stats = report.stats
        self._write(
            f"finished ({report.stop_reason}): checked={stats.checked:,} "
            f"candidates={stats.candidates:,} errors={stats.errors:,} "
            f"elapsed={format_duration(stats.elapsed_seconds)}"
        )

    def _write(self, message: str) -> None:
        print(message, file=self._stream, flush=True)


class RichProgressReporter(NullProgressReporter):
    """Live, redrawing dashboard."""

    def __init__(self, refresh_per_second: float = 6.0) -> None:
        self._console = Console(stderr=True)
        self._live: Live | None = None
        self._refresh = refresh_per_second
        self._config: Config | None = None

    def start(self, config: Config) -> None:
        self._config = config
        self._live = Live(
            self._render(ScanStats()),
            console=self._console,
            refresh_per_second=self._refresh,
            transient=False,
        )
        self._live.start()

    def update(self, stats: ScanStats) -> None:
        if self._live is not None:
            self._live.update(self._render(stats))

    def finish(self, report: ScanReport) -> None:
        self.update(report.stats)
        self.close()
        self._console.print(f"[bold]Stopped:[/bold] {_reason_text(report.stop_reason)}")

    def close(self) -> None:
        if self._live is not None:
            self._live.stop()
            self._live = None

    def _render(self, stats: ScanStats) -> Table:
        config = self._config
        table = Table.grid(padding=(0, 2))
        table.add_column(style="dim", justify="left", min_width=13)
        table.add_column(justify="left")

        table.add_row("[bold]" + TITLE + "[/bold]", "")
        table.add_row(RULE, "")
        if config is not None:
            table.add_row(
                "Search:", f"{config.min_length} → {config.max_length} characters"
            )
            table.add_row("Charset:", str(config.charset))
        table.add_row("Current:", stats.current_username or "-")
        table.add_row("Progress:", f"{stats.current_index:,} / {stats.length_total:,}")
        table.add_row("Completion:", f"{stats.completion:.1%}")
        table.add_row("", "")
        table.add_row("Taken:", f"{stats.taken:,}")
        table.add_row("Candidates:", f"[green]{stats.candidates:,}[/green]")
        table.add_row("Errors:", f"{stats.errors:,}")
        table.add_row(
            "Rate limited:",
            "[red]Yes[/red]" if stats.rate_limited_now else "No",
        )
        table.add_row("", "")
        table.add_row("Elapsed:", format_duration(stats.elapsed_seconds))
        table.add_row(RULE, "")
        return table


def build_reporter(config: Config, stream: TextIO | None = None) -> ProgressReporter:
    """Pick the best reporter for the current configuration and terminal."""
    if config.quiet or config.no_progress:
        return NullProgressReporter()
    target = stream if stream is not None else sys.stderr
    if RICH_AVAILABLE and target.isatty() and not config.verbose:
        return RichProgressReporter()
    return PlainProgressReporter(target)


def format_duration(seconds: float) -> str:
    """Format a duration as ``HH:MM:SS``."""
    total = int(max(0.0, seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def _reason_text(reason: StopReason) -> str:
    return {
        StopReason.COMPLETED: "search space exhausted",
        StopReason.FOUND: "candidate found (--stop-on-first)",
        StopReason.INTERRUPTED: "interrupted; progress saved",
        StopReason.RATE_LIMITED: "rate limited; progress saved, resume later",
        StopReason.MAX_CHECKS: "check budget reached; progress saved",
        StopReason.TIME_LIMIT: "time limit reached; progress saved",
    }[reason]
