"""Command line interface.

``instagram-finder scan`` is the only command that does work; everything else
is help, version and configuration plumbing. This module owns argument
parsing, logging setup, signal handling and process exit codes - and nothing
else.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from . import __version__
from .checker import InstagramChecker, build_session
from .config import Config, ConfigError, build_config
from .generator import UsernameGenerator
from .models import Charset, OutputFormat, ScanReport, ScanState, StopReason
from .output import DISCLAIMER, render, write_report
from .persistence import CorruptStateError, StateStore
from .progress import build_reporter, format_duration
from .rate_limiter import RateLimiter
from .retry import RetryPolicy
from .scanner import Scanner

logger = logging.getLogger("instagram_username_finder")

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_RATE_LIMITED = 4
EXIT_INTERRUPTED = 130

STDOUT_TARGET = Path("-")

EPILOG = """\
Results reported as POSSIBLY_AVAILABLE are not guaranteed to be registrable.
Instagram may reserve, restrict or hold usernames even when no public profile
exists. Always verify a candidate directly with Instagram.
"""


# ----------------------------------------------------------------------
# argument parsing
# ----------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="instagram-finder",
        description=("A responsible, open-source Instagram username availability scanner."),
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--version", action="version", version=f"instagram-finder {__version__}"
    )
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")

    scan = subparsers.add_parser(
        "scan",
        help="scan for usernames without a publicly accessible profile",
        description="Scan candidate usernames, shortest length first.",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_scan_arguments(scan)
    return parser


def _add_scan_arguments(parser: argparse.ArgumentParser) -> None:
    space = parser.add_argument_group("search space")
    space.add_argument("--min-length", type=int, help="shortest username length")
    space.add_argument("--max-length", type=int, help="longest username length")
    space.add_argument(
        "--charset",
        choices=[choice.value for choice in Charset],
        help="named character set to search",
    )
    space.add_argument(
        "--characters",
        help="explicit character set, e.g. --characters abc123 (implies custom)",
    )

    network = parser.add_argument_group("networking")
    network.add_argument("--concurrency", type=int, help="in-flight requests (default 5)")
    network.add_argument("--batch-size", type=int, help="candidates per checkpoint")
    network.add_argument("--delay", type=float, help="minimum seconds between requests")
    network.add_argument("--timeout", type=float, help="per-request timeout in seconds")
    network.add_argument("--max-retries", type=int, help="retries for transient failures")
    network.add_argument("--base-url", help="override the profile base URL")
    network.add_argument("--user-agent", help="override the User-Agent header")

    limits = parser.add_argument_group("limits")
    limits.add_argument("--max-checks", type=int, help="stop after this many checks")
    limits.add_argument("--time-limit", type=float, help="stop after this many seconds")

    outputs = parser.add_argument_group("output")
    outputs.add_argument(
        "--output", type=Path, help="write results to this file ('-' for stdout)"
    )
    outputs.add_argument(
        "--format",
        choices=[choice.value for choice in OutputFormat],
        help="result format (default json)",
    )
    outputs.add_argument("--state-file", type=Path, help="resume state file location")
    outputs.add_argument("--config", type=Path, help="TOML configuration file")

    behaviour = parser.add_argument_group("behaviour")
    resume_group = behaviour.add_mutually_exclusive_group()
    resume_group.add_argument(
        "--resume",
        action="store_true",
        default=None,
        help="require an existing state file and continue from it",
    )
    resume_group.add_argument(
        "--fresh",
        action="store_true",
        default=None,
        help="discard any existing state and start over",
    )

    stop_group = behaviour.add_mutually_exclusive_group()
    stop_group.add_argument(
        "--stop-on-first",
        dest="stop_on_first",
        action="store_true",
        default=None,
        help="stop at the first candidate (default)",
    )
    stop_group.add_argument(
        "--collect-all",
        dest="stop_on_first",
        action="store_false",
        default=None,
        help="keep scanning the whole space and collect every candidate",
    )

    logging_group = parser.add_argument_group("logging")
    verbosity = logging_group.add_mutually_exclusive_group()
    verbosity.add_argument(
        "--verbose", action="store_true", default=None, help="enable debug logging"
    )
    verbosity.add_argument(
        "--quiet", action="store_true", default=None, help="suppress progress output"
    )
    logging_group.add_argument(
        "--no-progress",
        action="store_true",
        default=None,
        help="disable the live progress display",
    )


def _cli_overrides(args: argparse.Namespace) -> dict[str, Any]:
    """Extract only the options the user actually supplied."""
    fields = (
        "min_length",
        "max_length",
        "charset",
        "characters",
        "concurrency",
        "batch_size",
        "delay",
        "timeout",
        "max_retries",
        "base_url",
        "user_agent",
        "max_checks",
        "time_limit",
        "output",
        "format",
        "state_file",
        "resume",
        "fresh",
        "stop_on_first",
        "verbose",
        "quiet",
        "no_progress",
    )
    overrides = {
        name: getattr(args, name)
        for name in fields
        if getattr(args, name, None) is not None
    }
    if overrides.get("characters") and "charset" not in overrides:
        overrides["charset"] = Charset.CUSTOM.value
    return overrides


# ----------------------------------------------------------------------
# logging
# ----------------------------------------------------------------------
def configure_logging(config: Config) -> None:
    if config.quiet:
        level = logging.ERROR
    elif config.verbose:
        level = logging.DEBUG
    else:
        level = logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
        force=True,
    )
    # aiohttp is chatty at debug level and adds nothing useful here.
    logging.getLogger("aiohttp").setLevel(max(level, logging.WARNING))


# ----------------------------------------------------------------------
# state resolution
# ----------------------------------------------------------------------
def resolve_state(config: Config, store: StateStore) -> ScanState:
    """Decide whether to resume, restart, or refuse."""
    fingerprint = config.fingerprint()

    if config.fresh:
        if store.exists:
            logger.info("--fresh: discarding existing state at %s", store.path)
            store.clear()
        return ScanState(fingerprint=fingerprint)

    if not store.exists:
        if config.resume:
            raise ConfigError(
                f"--resume was requested but no state file exists at {store.path}"
            )
        return ScanState(fingerprint=fingerprint)

    try:
        state = store.load()
    except CorruptStateError as exc:
        if config.resume:
            raise ConfigError(f"{exc}") from exc
        logger.warning("%s", exc)
        store.quarantine()
        return ScanState(fingerprint=fingerprint)

    if state.fingerprint and state.fingerprint != fingerprint:
        raise ConfigError(
            "the existing state file was written for a different search space "
            f"({state.fingerprint!r} != {fingerprint!r}); "
            "use --fresh or choose another --state-file"
        )

    logger.info(
        "resuming from %s: length %d, index %d, %d already checked",
        store.path,
        state.search_length,
        state.current_index,
        state.checked,
    )
    state.fingerprint = fingerprint
    return state


# ----------------------------------------------------------------------
# scan execution
# ----------------------------------------------------------------------
async def run_scan(config: Config) -> ScanReport:
    """Wire the components together and run one scan."""
    store = StateStore(config.state_file)
    state = resolve_state(config, store)

    generator = UsernameGenerator(config.alphabet)
    limiter = RateLimiter(
        concurrency=config.concurrency,
        delay=config.delay,
        cooldown=config.rate_limit_cooldown,
        circuit_threshold=config.circuit_breaker_threshold,
    )
    retry_policy = RetryPolicy(
        max_retries=config.max_retries,
        base_delay=config.retry_base_delay,
        max_delay=config.retry_max_delay,
    )
    reporter = build_reporter(config)

    session = build_session(
        user_agent=config.resolved_user_agent(__version__),
        concurrency=config.concurrency,
        timeout=config.timeout,
    )
    try:
        checker = InstagramChecker(
            session=session,
            rate_limiter=limiter,
            retry_policy=retry_policy,
            base_url=config.base_url,
            timeout=config.timeout,
        )
        scanner = Scanner(
            config=config,
            checker=checker,
            generator=generator,
            rate_limiter=limiter,
            state_store=store,
            reporter=reporter,
            state=state,
        )
        with _signal_handlers(scanner):
            return await scanner.run()
    finally:
        await session.close()


@contextlib.contextmanager
def _signal_handlers(scanner: Scanner) -> Any:
    """Translate SIGINT/SIGTERM into a graceful scanner stop."""
    loop = asyncio.get_running_loop()
    installed: list[signal.Signals] = []

    def _handle(signum: signal.Signals) -> None:
        logger.warning(
            "received %s; finishing in-flight requests and saving state", signum.name
        )
        scanner.request_stop(StopReason.INTERRUPTED)

    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, _handle, signum)
        except (NotImplementedError, RuntimeError, ValueError):
            # Windows, or a non-main thread: fall back to default handling.
            continue
        installed.append(signum)
    try:
        yield
    finally:
        for signum in installed:
            with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
                loop.remove_signal_handler(signum)


# ----------------------------------------------------------------------
# reporting
# ----------------------------------------------------------------------
def emit_results(report: ScanReport, config: Config) -> None:
    """Write machine-readable output, then a human summary."""
    stdout_is_data = config.output == STDOUT_TARGET

    if stdout_is_data:
        sys.stdout.write(render(report, config.format))
    elif config.output is not None:
        path = config.output
        write_report(report, path, config.format)

    if config.quiet or stdout_is_data:
        return

    stats = report.stats
    print()
    print(f"Scan finished: {report.stop_reason}")
    print(f"  Checked:            {stats.checked:,}")
    print(f"  Taken:              {stats.taken:,}")
    print(f"  Possibly available: {stats.candidates:,}")
    print(f"  Errors:             {stats.errors:,}")
    print(f"  Elapsed:            {format_duration(stats.elapsed_seconds)}")

    candidates = report.candidates
    if candidates:
        print()
        print("POSSIBLY_AVAILABLE candidates:")
        for result in candidates[:25]:
            print(f"  {result.username}  (HTTP {result.http_status})")
        if len(candidates) > 25:
            print(f"  ... and {len(candidates) - 25:,} more")
        print()
        print(DISCLAIMER)


def _exit_code(report: ScanReport) -> int:
    if report.stop_reason is StopReason.RATE_LIMITED:
        return EXIT_RATE_LIMITED
    if report.stop_reason is StopReason.INTERRUPTED:
        return EXIT_INTERRUPTED
    return EXIT_OK


# ----------------------------------------------------------------------
# entry point
# ----------------------------------------------------------------------
def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return EXIT_OK

    try:
        config = build_config(
            cli=_cli_overrides(args), config_file=getattr(args, "config", None)
        )
    except ConfigError as exc:
        parser.error(str(exc))  # raises SystemExit(2)

    configure_logging(config)

    try:
        report = asyncio.run(run_scan(config))
    except ConfigError as exc:
        logger.error("%s", exc)
        return EXIT_USAGE
    except KeyboardInterrupt:  # pragma: no cover - depends on signal timing
        logger.warning("interrupted")
        return EXIT_INTERRUPTED
    except Exception as exc:
        logger.error("scan failed: %s", exc, exc_info=config.verbose)
        return EXIT_ERROR

    emit_results(report, config)
    return _exit_code(report)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
