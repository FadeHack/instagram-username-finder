"""Typed domain models shared across the application.

Everything that crosses a module boundary is represented here as a dataclass or
enum. No module should pass around bare dictionaries.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any

STATE_VERSION = 1


class CheckStatus(str, Enum):
    """Outcome of a single username check.

    ``POSSIBLY_AVAILABLE`` deliberately avoids claiming availability: it only
    means that no publicly accessible profile was observed for the username.
    """

    TAKEN = "taken"
    POSSIBLY_AVAILABLE = "possibly_available"
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    NETWORK_ERROR = "network_error"
    UNKNOWN = "unknown"

    def __str__(self) -> str:
        return self.value


#: Statuses that represent a completed, trustworthy observation.
CONCLUSIVE_STATUSES = frozenset({CheckStatus.TAKEN, CheckStatus.POSSIBLY_AVAILABLE})

#: Statuses that mean "we do not know", and must never imply availability.
INCONCLUSIVE_STATUSES = frozenset(
    {
        CheckStatus.RATE_LIMITED,
        CheckStatus.TIMEOUT,
        CheckStatus.NETWORK_ERROR,
        CheckStatus.UNKNOWN,
    }
)


class Charset(str, Enum):
    """Named character sets available to the username generator."""

    LETTERS = "letters"
    DIGITS = "digits"
    LETTERS_DIGITS = "letters_digits"
    INSTAGRAM = "instagram"
    CUSTOM = "custom"

    def __str__(self) -> str:
        return self.value


class OutputFormat(str, Enum):
    """Supported machine-readable result formats."""

    TXT = "txt"
    JSON = "json"
    CSV = "csv"

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class Candidate:
    """A username produced by the generator.

    ``index`` is the position of the username within the raw combinatorial space
    for ``length``. It is the unit of resume: a scan that has checkpointed index
    ``N`` restarts at ``N`` without re-checking earlier candidates.
    """

    username: str
    length: int
    index: int


@dataclass(frozen=True, slots=True)
class CheckResult:
    """The result of checking a single username."""

    username: str
    status: CheckStatus
    http_status: int | None = None
    latency_ms: float | None = None
    error: str | None = None
    attempts: int = 1
    checked_at: str = field(default_factory=lambda: _utc_now())

    @property
    def is_candidate(self) -> bool:
        """True when the username looks unclaimed on a public profile check."""
        return self.status is CheckStatus.POSSIBLY_AVAILABLE

    @property
    def is_conclusive(self) -> bool:
        return self.status in CONCLUSIVE_STATUSES

    def to_dict(self) -> dict[str, Any]:
        return {
            "username": self.username,
            "status": self.status.value,
            "http_status": self.http_status,
            "latency_ms": (
                round(self.latency_ms, 1) if self.latency_ms is not None else None
            ),
            "error": self.error,
            "attempts": self.attempts,
            "checked_at": self.checked_at,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> CheckResult:
        return cls(
            username=str(payload["username"]),
            status=CheckStatus(payload["status"]),
            http_status=payload.get("http_status"),
            latency_ms=payload.get("latency_ms"),
            error=payload.get("error"),
            attempts=int(payload.get("attempts", 1)),
            checked_at=str(payload.get("checked_at", _utc_now())),
        )


@dataclass
class ScanStats:
    """Mutable counters describing scan progress.

    Owned by the scanner; consumed (never mutated) by progress reporters.
    """

    checked: int = 0
    taken: int = 0
    candidates: int = 0
    errors: int = 0
    rate_limited_events: int = 0
    current_length: int = 0
    current_username: str = ""
    current_index: int = 0
    length_total: int = 0
    started_at: float = field(default_factory=time.monotonic)
    rate_limited_now: bool = False

    @property
    def elapsed_seconds(self) -> float:
        return time.monotonic() - self.started_at

    @property
    def completion(self) -> float:
        """Fraction of the current length's space that has been dispatched."""
        if self.length_total <= 0:
            return 0.0
        return min(1.0, self.current_index / self.length_total)

    def record(self, result: CheckResult) -> None:
        self.checked += 1
        if result.status is CheckStatus.TAKEN:
            self.taken += 1
        elif result.status is CheckStatus.POSSIBLY_AVAILABLE:
            self.candidates += 1
        else:
            self.errors += 1
            if result.status is CheckStatus.RATE_LIMITED:
                self.rate_limited_events += 1


class StopReason(str, Enum):
    """Why a scan stopped."""

    COMPLETED = "completed"
    FOUND = "found"
    INTERRUPTED = "interrupted"
    RATE_LIMITED = "rate_limited"
    MAX_CHECKS = "max_checks"
    TIME_LIMIT = "time_limit"

    def __str__(self) -> str:
        return self.value


@dataclass
class ScanState:
    """Resumable scan checkpoint, serialised to the state file.

    ``current_index`` is the first index of ``search_length`` that has *not*
    been confirmed checked. Restarting from it never skips a username.
    """

    version: int = STATE_VERSION
    search_length: int = 0
    current_index: int = 0
    checked: int = 0
    taken: int = 0
    errors: int = 0
    found: list[CheckResult] = field(default_factory=list)
    completed_lengths: list[int] = field(default_factory=list)
    fingerprint: str = ""
    updated_at: str = field(default_factory=lambda: _utc_now())

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "search_length": self.search_length,
            "current_index": self.current_index,
            "checked": self.checked,
            "taken": self.taken,
            "errors": self.errors,
            "found": [result.to_dict() for result in self.found],
            "completed_lengths": sorted(self.completed_lengths),
            "fingerprint": self.fingerprint,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ScanState:
        return cls(
            version=int(payload.get("version", STATE_VERSION)),
            search_length=int(payload.get("search_length", 0)),
            current_index=int(payload.get("current_index", 0)),
            checked=int(payload.get("checked", 0)),
            taken=int(payload.get("taken", 0)),
            errors=int(payload.get("errors", 0)),
            found=[CheckResult.from_dict(item) for item in payload.get("found", [])],
            completed_lengths=[int(n) for n in payload.get("completed_lengths", [])],
            fingerprint=str(payload.get("fingerprint", "")),
            updated_at=str(payload.get("updated_at", _utc_now())),
        )

    def touch(self) -> None:
        self.updated_at = _utc_now()


@dataclass
class ScanReport:
    """Final summary handed back to the CLI when a scan finishes."""

    stop_reason: StopReason
    stats: ScanStats
    results: list[CheckResult]
    state: ScanState

    @property
    def candidates(self) -> list[CheckResult]:
        return [result for result in self.results if result.is_candidate]


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
