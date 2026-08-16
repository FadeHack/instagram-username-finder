"""CLI parsing, help/version, exit codes and state resolution."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from instagram_username_finder import __version__
from instagram_username_finder.cli import (
    EXIT_OK,
    EXIT_RATE_LIMITED,
    _cli_overrides,
    build_parser,
    emit_results,
    main,
    resolve_state,
)
from instagram_username_finder.config import Config, ConfigError
from instagram_username_finder.models import (
    CheckResult,
    CheckStatus,
    OutputFormat,
    ScanReport,
    ScanState,
    ScanStats,
    StopReason,
)
from instagram_username_finder.persistence import StateStore


def make_report(stop_reason: StopReason = StopReason.FOUND) -> ScanReport:
    return ScanReport(
        stop_reason=stop_reason,
        stats=ScanStats(checked=2, taken=1, candidates=1),
        results=[
            CheckResult("qzx", CheckStatus.POSSIBLY_AVAILABLE, http_status=404),
        ],
        state=ScanState(),
    )


class TestTopLevel:
    def test_help_exits_cleanly(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit) as exc:
            build_parser().parse_args(["--help"])
        assert exc.value.code == 0
        assert "instagram-finder" in capsys.readouterr().out

    def test_version_prints_the_package_version(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(SystemExit) as exc:
            build_parser().parse_args(["--version"])
        assert exc.value.code == 0
        assert __version__ in capsys.readouterr().out

    def test_scan_help_exits_cleanly(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit) as exc:
            build_parser().parse_args(["scan", "--help"])
        assert exc.value.code == 0
        output = capsys.readouterr().out
        assert "--min-length" in output
        assert "--resume" in output

    def test_no_command_prints_help(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert main([]) == EXIT_OK
        assert "usage:" in capsys.readouterr().out

    def test_help_warns_that_results_are_not_guaranteed(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(SystemExit):
            build_parser().parse_args(["--help"])
        assert "not guaranteed" in capsys.readouterr().out


class TestParsing:
    def test_only_supplied_options_become_overrides(self) -> None:
        args = build_parser().parse_args(["scan", "--min-length", "3"])
        assert _cli_overrides(args) == {"min_length": 3}

    def test_characters_implies_the_custom_charset(self) -> None:
        args = build_parser().parse_args(["scan", "--characters", "abc"])
        assert _cli_overrides(args) == {"characters": "abc", "charset": "custom"}

    def test_collect_all_disables_stop_on_first(self) -> None:
        args = build_parser().parse_args(["scan", "--collect-all"])
        assert _cli_overrides(args) == {"stop_on_first": False}

    def test_stop_on_first_is_explicit_when_requested(self) -> None:
        args = build_parser().parse_args(["scan", "--stop-on-first"])
        assert _cli_overrides(args) == {"stop_on_first": True}

    def test_resume_and_fresh_are_mutually_exclusive(self) -> None:
        with pytest.raises(SystemExit):
            build_parser().parse_args(["scan", "--resume", "--fresh"])

    def test_verbose_and_quiet_are_mutually_exclusive(self) -> None:
        with pytest.raises(SystemExit):
            build_parser().parse_args(["scan", "--verbose", "--quiet"])

    def test_invalid_charset_is_rejected(self) -> None:
        with pytest.raises(SystemExit):
            build_parser().parse_args(["scan", "--charset", "emoji"])

    def test_full_option_set_parses(self) -> None:
        args = build_parser().parse_args(
            [
                "scan",
                "--min-length",
                "3",
                "--max-length",
                "4",
                "--charset",
                "letters",
                "--concurrency",
                "5",
                "--batch-size",
                "50",
                "--delay",
                "0.5",
                "--timeout",
                "10",
                "--max-retries",
                "3",
                "--output",
                "out.json",
                "--format",
                "json",
                "--state-file",
                "state.json",
                "--resume",
                "--collect-all",
                "--verbose",
            ]
        )
        overrides = _cli_overrides(args)
        assert overrides["max_length"] == 4
        assert overrides["state_file"] == Path("state.json")
        assert overrides["stop_on_first"] is False
        assert overrides["resume"] is True

    def test_invalid_values_are_rejected_before_scanning(self) -> None:
        with pytest.raises(SystemExit) as exc:
            main(["scan", "--min-length", "5", "--max-length", "2"])
        assert exc.value.code == 2


class TestResolveState:
    def test_returns_empty_state_when_none_exists(self, tmp_path: Path) -> None:
        config = Config(state_file=tmp_path / "state.json")
        state = resolve_state(config, StateStore(config.state_file))
        assert state.current_index == 0
        assert state.fingerprint == config.fingerprint()

    def test_resume_without_state_is_an_error(self, tmp_path: Path) -> None:
        config = Config(state_file=tmp_path / "state.json", resume=True)
        with pytest.raises(ConfigError, match="no state file"):
            resolve_state(config, StateStore(config.state_file))

    def test_fresh_discards_existing_state(self, tmp_path: Path) -> None:
        config = Config(state_file=tmp_path / "state.json", fresh=True)
        store = StateStore(config.state_file)
        store.save(ScanState(current_index=500, fingerprint=config.fingerprint()))
        assert resolve_state(config, store).current_index == 0
        assert not store.exists

    def test_compatible_state_is_resumed(self, tmp_path: Path) -> None:
        config = Config(state_file=tmp_path / "state.json")
        store = StateStore(config.state_file)
        store.save(
            ScanState(search_length=3, current_index=42, fingerprint=config.fingerprint())
        )
        assert resolve_state(config, store).current_index == 42

    def test_state_from_a_different_search_space_is_refused(self, tmp_path: Path) -> None:
        config = Config(state_file=tmp_path / "state.json")
        store = StateStore(config.state_file)
        store.save(ScanState(fingerprint="somethingelse|9-9"))
        with pytest.raises(ConfigError, match="different search space"):
            resolve_state(config, store)

    def test_corrupt_state_is_quarantined_and_restarted(self, tmp_path: Path) -> None:
        config = Config(state_file=tmp_path / "state.json")
        config.state_file.write_text("not json", encoding="utf-8")
        store = StateStore(config.state_file)
        assert resolve_state(config, store).current_index == 0
        assert (tmp_path / "state.json.corrupt").is_file()

    def test_corrupt_state_with_resume_is_an_error(self, tmp_path: Path) -> None:
        config = Config(state_file=tmp_path / "state.json", resume=True)
        config.state_file.write_text("not json", encoding="utf-8")
        with pytest.raises(ConfigError):
            resolve_state(config, StateStore(config.state_file))


class TestEmitResults:
    def test_writes_the_requested_file(self, tmp_path: Path) -> None:
        target = tmp_path / "results.json"
        config = Config(output=target, format=OutputFormat.JSON, quiet=True)
        emit_results(make_report(), config)
        assert json.loads(target.read_text(encoding="utf-8"))["results"]

    def test_dash_streams_data_to_stdout(self, capsys: pytest.CaptureFixture[str]) -> None:
        config = Config(output=Path("-"), format=OutputFormat.CSV)
        emit_results(make_report(), config)
        captured = capsys.readouterr().out
        assert captured.startswith("username,status,http_status,latency_ms,error")
        assert "Scan finished" not in captured  # no pretty output mixed into data

    def test_human_summary_carries_the_disclaimer(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        emit_results(make_report(), Config())
        output = capsys.readouterr().out
        assert "Possibly available: 1" in output
        assert "not a guarantee" in output

    def test_quiet_prints_nothing(self, capsys: pytest.CaptureFixture[str]) -> None:
        emit_results(make_report(), Config(quiet=True))
        assert capsys.readouterr().out == ""


class TestExitCodes:
    def test_rate_limited_scans_signal_a_distinct_exit_code(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        report = make_report(StopReason.RATE_LIMITED)

        async def fake_run(config: Config) -> ScanReport:
            return report

        monkeypatch.setattr("instagram_username_finder.cli.run_scan", fake_run)
        code = main(["scan", "--quiet", "--state-file", str(tmp_path / "s.json")])
        assert code == EXIT_RATE_LIMITED

    def test_completed_scans_exit_zero(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        async def fake_run(config: Config) -> ScanReport:
            return make_report(StopReason.COMPLETED)

        monkeypatch.setattr("instagram_username_finder.cli.run_scan", fake_run)
        assert main(["scan", "--quiet", "--state-file", str(tmp_path / "s.json")]) == 0
