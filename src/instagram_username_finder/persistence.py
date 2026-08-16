"""Atomic, resumable scan state.

State is written to a temporary file in the destination directory and then
moved into place with :func:`os.replace`, which is atomic on POSIX and Windows.
A crash (or Ctrl+C) mid-write therefore leaves the previous state intact rather
than a truncated file.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path

from .models import STATE_VERSION, ScanState

logger = logging.getLogger(__name__)


class CorruptStateError(RuntimeError):
    """Raised when a state file exists but cannot be used."""


class StateStore:
    """Loads and saves :class:`ScanState` for a single scan."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    @property
    def exists(self) -> bool:
        return self.path.is_file()

    # ------------------------------------------------------------------
    def load(self) -> ScanState:
        """Read state from disk.

        Raises :class:`CorruptStateError` when the file is unreadable, is not
        valid JSON, or was written by an incompatible version.
        """
        if not self.exists:
            raise CorruptStateError(f"no state file at {self.path}")
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CorruptStateError(f"cannot read state file {self.path}: {exc}") from exc

        if not isinstance(payload, dict):
            raise CorruptStateError(f"state file {self.path} is not a JSON object")

        version = payload.get("version")
        if version != STATE_VERSION:
            raise CorruptStateError(
                f"state file {self.path} has version {version!r}, "
                f"expected {STATE_VERSION}; start again with --fresh"
            )
        try:
            return ScanState.from_dict(payload)
        except (KeyError, TypeError, ValueError) as exc:
            raise CorruptStateError(f"state file {self.path} is malformed: {exc}") from exc

    def load_or_none(self) -> ScanState | None:
        """Best-effort load; logs and returns ``None`` instead of raising."""
        try:
            return self.load()
        except CorruptStateError as exc:
            logger.warning("%s", exc)
            return None

    # ------------------------------------------------------------------
    def save(self, state: ScanState) -> None:
        """Atomically persist ``state``."""
        state.touch()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(state.to_dict(), indent=2, sort_keys=False)

        descriptor, name = tempfile.mkstemp(
            dir=self.path.parent, prefix=f".{self.path.name}.", suffix=".tmp"
        )
        temp_path = Path(name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self.path)
        except BaseException:
            temp_path.unlink(missing_ok=True)
            raise

    def clear(self) -> None:
        """Remove any existing state file."""
        self.path.unlink(missing_ok=True)

    def quarantine(self) -> Path | None:
        """Move an unusable state file aside so a fresh scan can proceed."""
        if not self.exists:
            return None
        target = self.path.with_suffix(self.path.suffix + ".corrupt")
        os.replace(self.path, target)
        logger.warning("moved unusable state file to %s", target)
        return target
