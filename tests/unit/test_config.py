"""Configuration layering, coercion and validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from instagram_username_finder.config import Config, ConfigError, build_config
from instagram_username_finder.models import Charset, OutputFormat


class TestDefaults:
    def test_defaults_are_conservative(self) -> None:
        config = Config()
        assert config.concurrency == 5
        assert config.delay == 0.5
        assert config.stop_on_first is True
        assert config.alphabet == "abcdefghijklmnopqrstuvwxyz"


class TestValidation:
    @pytest.mark.parametrize(
        "kwargs",
        [
            {"min_length": 0},
            {"min_length": 5, "max_length": 3},
            {"max_length": 99},
            {"concurrency": 0},
            {"batch_size": 0},
            {"delay": -0.1},
            {"timeout": 0},
            {"max_retries": -1},
            {"retry_base_delay": 0},
            {"retry_max_delay": 0.1},
            {"circuit_breaker_threshold": 0},
            {"max_checks": 0},
            {"time_limit": 0},
            {"resume": True, "fresh": True},
            {"verbose": True, "quiet": True},
        ],
    )
    def test_rejects_invalid_values(self, kwargs: dict[str, object]) -> None:
        with pytest.raises(ConfigError):
            Config(**kwargs)  # type: ignore[arg-type]

    def test_boundaries_are_accepted(self) -> None:
        config = Config(min_length=1, max_length=1, concurrency=1, delay=0.0)
        assert config.min_length == config.max_length == 1

    def test_custom_charset_requires_characters(self) -> None:
        with pytest.raises(ConfigError, match="requires --characters"):
            Config(charset=Charset.CUSTOM)

    def test_characters_outside_the_instagram_alphabet_are_rejected(self) -> None:
        with pytest.raises(ConfigError, match="Instagram usernames cannot use"):
            Config(characters="ab!")


class TestAlphabet:
    @pytest.mark.parametrize(
        ("charset", "size"),
        [
            (Charset.LETTERS, 26),
            (Charset.DIGITS, 10),
            (Charset.LETTERS_DIGITS, 36),
            (Charset.INSTAGRAM, 38),
        ],
    )
    def test_named_charsets_resolve(self, charset: Charset, size: int) -> None:
        assert len(Config(charset=charset).alphabet) == size

    def test_characters_are_deduplicated_and_sorted(self) -> None:
        assert Config(characters="cbaa").alphabet == "abc"

    def test_characters_imply_a_custom_alphabet(self) -> None:
        assert Config(charset=Charset.LETTERS, characters="xyz").alphabet == "xyz"

    def test_fingerprint_tracks_the_search_space(self) -> None:
        first = Config(characters="ab", min_length=1, max_length=2).fingerprint()
        assert first != Config(characters="abc", min_length=1, max_length=2).fingerprint()
        assert first != Config(characters="ab", min_length=1, max_length=3).fingerprint()


class TestLayering:
    def test_defaults_apply_when_nothing_is_supplied(self) -> None:
        assert build_config(env={}).concurrency == 5

    def test_environment_overrides_defaults(self) -> None:
        config = build_config(env={"USERNAME_FINDER_CONCURRENCY": "9"})
        assert config.concurrency == 9

    def test_cli_overrides_the_environment(self) -> None:
        config = build_config(
            cli={"concurrency": 2}, env={"USERNAME_FINDER_CONCURRENCY": "9"}
        )
        assert config.concurrency == 2

    def test_file_is_overridden_by_environment_and_cli(self, tmp_path: Path) -> None:
        path = tmp_path / "config.toml"
        path.write_text("concurrency = 1\ndelay = 2.5\nmin_length = 3\n", encoding="utf-8")
        config = build_config(
            cli={"min_length": 5, "max_length": 6},
            env={"USERNAME_FINDER_CONCURRENCY": "7"},
            config_file=path,
        )
        assert config.concurrency == 7  # env beats file
        assert config.delay == 2.5  # file beats default
        assert config.min_length == 5  # cli beats file

    def test_scan_table_is_supported(self, tmp_path: Path) -> None:
        path = tmp_path / "config.toml"
        path.write_text("[scan]\nconcurrency = 3\n", encoding="utf-8")
        assert build_config(env={}, config_file=path).concurrency == 3

    def test_unrelated_environment_variables_are_ignored(self) -> None:
        assert build_config(env={"PATH": "/usr/bin"}).concurrency == 5


class TestCoercion:
    def test_strings_are_coerced_to_typed_values(self) -> None:
        config = build_config(
            env={
                "USERNAME_FINDER_DELAY": "1.5",
                "USERNAME_FINDER_CHARSET": "digits",
                "USERNAME_FINDER_FORMAT": "csv",
                "USERNAME_FINDER_STATE_FILE": "/tmp/state.json",
                "USERNAME_FINDER_STOP_ON_FIRST": "false",
            }
        )
        assert config.delay == 1.5
        assert config.charset is Charset.DIGITS
        assert config.format is OutputFormat.CSV
        assert config.state_file == Path("/tmp/state.json")
        assert config.stop_on_first is False

    @pytest.mark.parametrize("value", ["1", "true", "YES", "on"])
    def test_truthy_booleans(self, value: str) -> None:
        assert build_config(env={"USERNAME_FINDER_VERBOSE": value}).verbose is True

    @pytest.mark.parametrize("value", ["0", "false", "No", "off"])
    def test_falsy_booleans(self, value: str) -> None:
        assert build_config(env={"USERNAME_FINDER_VERBOSE": value}).verbose is False

    def test_unparseable_values_are_reported_with_their_source(self) -> None:
        with pytest.raises(ConfigError, match="environment"):
            build_config(env={"USERNAME_FINDER_CONCURRENCY": "many"})

    def test_unknown_settings_are_rejected(self) -> None:
        with pytest.raises(ConfigError, match="unknown settings"):
            build_config(cli={"nonsense": 1}, env={})


class TestFiles:
    def test_missing_config_file_is_an_error(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="not found"):
            build_config(env={}, config_file=tmp_path / "absent.toml")

    def test_invalid_toml_is_an_error(self, tmp_path: Path) -> None:
        path = tmp_path / "config.toml"
        path.write_text("concurrency = = 3", encoding="utf-8")
        with pytest.raises(ConfigError, match="invalid TOML"):
            build_config(env={}, config_file=path)

    def test_the_shipped_example_config_is_valid(self) -> None:
        example = Path(__file__).resolve().parents[2] / "examples" / "config.example.toml"
        config = build_config(env={}, config_file=example)
        assert config.min_length >= 1
