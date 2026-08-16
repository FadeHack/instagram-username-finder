"""Rendering of TXT, JSON and CSV output."""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path

import pytest

from instagram_username_finder.models import (
    CheckResult,
    CheckStatus,
    OutputFormat,
    ScanReport,
    ScanState,
    ScanStats,
    StopReason,
)
from instagram_username_finder.output import (
    DISCLAIMER,
    infer_format,
    render,
    render_csv,
    render_json,
    render_txt,
    write_report,
)

RESULTS = [
    CheckResult("qzx", CheckStatus.POSSIBLY_AVAILABLE, http_status=404, latency_ms=184.4),
    CheckResult("abc", CheckStatus.TAKEN, http_status=200, latency_ms=210.0),
    CheckResult("err", CheckStatus.TIMEOUT, error="request timed out"),
]


@pytest.fixture
def report() -> ScanReport:
    stats = ScanStats(checked=3, taken=1, candidates=1, errors=1)
    return ScanReport(
        stop_reason=StopReason.FOUND,
        stats=stats,
        results=RESULTS,
        state=ScanState(),
    )


class TestJson:
    def test_is_valid_json_with_a_results_array(self, report: ScanReport) -> None:
        payload = json.loads(render_json(report))
        assert payload["tool"] == "instagram-username-finder"
        assert [item["username"] for item in payload["results"]] == ["qzx", "abc", "err"]

    def test_result_shape_matches_the_documented_schema(self, report: ScanReport) -> None:
        first = json.loads(render_json(report))["results"][0]
        assert first["status"] == "possibly_available"
        assert first["http_status"] == 404
        assert first["latency_ms"] == pytest.approx(184.4)

    def test_carries_the_availability_disclaimer(self, report: ScanReport) -> None:
        assert "not a guarantee" in json.loads(render_json(report))["disclaimer"]

    def test_includes_a_summary(self, report: ScanReport) -> None:
        summary = json.loads(render_json(report))["summary"]
        assert summary["checked"] == 3
        assert summary["possibly_available"] == 1
        assert summary["stop_reason"] == "found"

    def test_never_claims_guaranteed_availability(self, report: ScanReport) -> None:
        rendered = render_json(report).lower()
        for phrase in ("guaranteed available", "100% available", "instagram approved"):
            assert phrase not in rendered


class TestCsv:
    def test_has_the_documented_header(self) -> None:
        header = render_csv(RESULTS).splitlines()[0]
        assert header == "username,status,http_status,latency_ms,error"

    def test_rows_parse_back(self) -> None:
        rows = list(csv.DictReader(io.StringIO(render_csv(RESULTS))))
        assert rows[0]["username"] == "qzx"
        assert rows[0]["status"] == "possibly_available"
        assert rows[1]["status"] == "taken"

    def test_missing_values_render_as_empty_strings(self) -> None:
        rows = list(csv.DictReader(io.StringIO(render_csv(RESULTS))))
        assert rows[0]["error"] == ""
        assert rows[2]["http_status"] == ""

    def test_empty_results_still_emit_a_header(self) -> None:
        assert render_csv([]).strip() == "username,status,http_status,latency_ms,error"


class TestTxt:
    def test_lists_only_candidates(self) -> None:
        lines = [
            line for line in render_txt(RESULTS).splitlines() if not line.startswith("#")
        ]
        assert lines == ["qzx"]

    def test_includes_the_disclaimer_as_a_comment(self) -> None:
        assert f"# {DISCLAIMER}" in render_txt(RESULTS)

    def test_reports_when_nothing_was_found(self) -> None:
        assert "no candidates found" in render_txt([])


class TestDispatch:
    @pytest.mark.parametrize(
        ("fmt", "marker"),
        [
            (OutputFormat.JSON, '"results"'),
            (OutputFormat.CSV, "username,status"),
            (OutputFormat.TXT, "# instagram-username-finder"),
        ],
    )
    def test_render_dispatches_by_format(
        self, report: ScanReport, fmt: OutputFormat, marker: str
    ) -> None:
        assert marker in render(report, fmt)

    def test_write_report_creates_parent_directories(
        self, report: ScanReport, tmp_path: Path
    ) -> None:
        target = tmp_path / "deep" / "nested" / "results.json"
        write_report(report, target, OutputFormat.JSON)
        assert json.loads(target.read_text(encoding="utf-8"))["results"]

    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("results.csv", OutputFormat.CSV),
            ("results.txt", OutputFormat.TXT),
            ("results.json", OutputFormat.JSON),
            ("results.dat", OutputFormat.JSON),
        ],
    )
    def test_infer_format_from_extension(self, name: str, expected: OutputFormat) -> None:
        assert infer_format(Path(name), OutputFormat.JSON) is expected
