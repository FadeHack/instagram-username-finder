"""Shared fixtures.

No test in this suite touches the network. Transport behaviour is simulated
either with ``aioresponses`` or with in-memory fake checkers.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from instagram_username_finder.config import Config
from instagram_username_finder.models import Charset, CheckResult, CheckStatus


@pytest.fixture
def config(tmp_path: Path) -> Config:
    """A fast, deterministic configuration pointed at a temp directory."""
    return Config(
        min_length=2,
        max_length=2,
        characters="ab",
        charset=Charset.CUSTOM,
        concurrency=2,
        batch_size=2,
        delay=0.0,
        timeout=1.0,
        max_retries=0,
        state_file=tmp_path / "state.json",
        output=tmp_path / "results.json",
        no_progress=True,
    )


@pytest.fixture
def taken_result() -> CheckResult:
    return CheckResult(
        username="abc", status=CheckStatus.TAKEN, http_status=200, latency_ms=210.0
    )


@pytest.fixture
def candidate_result() -> CheckResult:
    return CheckResult(
        username="qzx",
        status=CheckStatus.POSSIBLY_AVAILABLE,
        http_status=404,
        latency_ms=184.0,
    )


@pytest.fixture
def anyio_backend() -> str:  # pragma: no cover - compatibility shim
    return "asyncio"


@pytest.fixture(autouse=True)
def _isolate_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Keep stray relative-path writes inside the test's temp directory."""
    monkeypatch.chdir(tmp_path)
    yield
