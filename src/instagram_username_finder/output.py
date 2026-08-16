"""Machine-readable result export.

Nothing in this module prints to the terminal, and nothing in the terminal UI
writes files. Keeping the two apart is what makes ``--format json`` safe to
pipe into another program.
"""

from __future__ import annotations

import csv
import io
import json
import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from .models import CheckResult, OutputFormat, ScanReport

logger = logging.getLogger(__name__)

CSV_COLUMNS: Final = ["username", "status", "http_status", "latency_ms", "error"]

DISCLAIMER: Final = (
    "POSSIBLY_AVAILABLE means no publicly accessible profile was observed. "
    "It is not a guarantee that the username can be registered. Instagram may "
    "reserve, restrict or hold usernames. Verify directly with Instagram."
)


def render(report: ScanReport, fmt: OutputFormat) -> str:
    """Render a finished scan into the requested format."""
    if fmt is OutputFormat.JSON:
        return render_json(report)
    if fmt is OutputFormat.CSV:
        return render_csv(report.results)
    return render_txt(report.results)


def render_json(report: ScanReport) -> str:
    payload: dict[str, Any] = {
        "tool": "instagram-username-finder",
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "disclaimer": DISCLAIMER,
        "summary": summarize(report),
        "results": [result.to_dict() for result in report.results],
    }
    return json.dumps(payload, indent=2) + "\n"


def render_csv(results: Sequence[CheckResult]) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=CSV_COLUMNS, lineterminator="\n")
    writer.writeheader()
    for result in results:
        row = result.to_dict()
        writer.writerow({column: _csv_value(row[column]) for column in CSV_COLUMNS})
    return buffer.getvalue()


def render_txt(results: Sequence[CheckResult]) -> str:
    lines = [
        "# instagram-username-finder results",
        f"# {DISCLAIMER}",
        "#",
    ]
    candidates = [result for result in results if result.is_candidate]
    if candidates:
        lines.extend(result.username for result in candidates)
    else:
        lines.append("# no candidates found")
    return "\n".join(lines) + "\n"


def summarize(report: ScanReport) -> dict[str, Any]:
    stats = report.stats
    return {
        "stop_reason": report.stop_reason.value,
        "checked": stats.checked,
        "taken": stats.taken,
        "possibly_available": stats.candidates,
        "errors": stats.errors,
        "rate_limited_events": stats.rate_limited_events,
        "elapsed_seconds": round(stats.elapsed_seconds, 2),
    }


def write_report(report: ScanReport, path: Path, fmt: OutputFormat) -> Path:
    """Write a rendered report to ``path``, creating parent directories."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(report, fmt), encoding="utf-8")
    logger.info("wrote %s results to %s", fmt.value, path)
    return path


def infer_format(path: Path, fallback: OutputFormat) -> OutputFormat:
    """Guess an output format from a file extension."""
    suffix = path.suffix.lower().lstrip(".")
    try:
        return OutputFormat(suffix)
    except ValueError:
        return fallback


def _csv_value(value: Any) -> str:
    return "" if value is None else str(value)
