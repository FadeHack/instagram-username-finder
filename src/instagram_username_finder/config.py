"""Configuration loading, layering and validation.

Precedence, highest first::

    CLI flags  ->  environment variables  ->  TOML file  ->  built-in defaults

Every layer produces a plain ``dict`` of overrides; the layers are merged and
then validated once, so an invalid value is reported the same way regardless of
where it came from.
"""

from __future__ import annotations

import os
import string
import tomllib
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Final

from .models import Charset, OutputFormat

ENV_PREFIX: Final = "USERNAME_FINDER_"

LETTERS: Final = string.ascii_lowercase
DIGITS: Final = string.digits
#: Instagram permits letters, digits, underscore and period.
INSTAGRAM_EXTRA: Final = "_."

CHARSET_TABLE: Final[dict[Charset, str]] = {
    Charset.LETTERS: LETTERS,
    Charset.DIGITS: DIGITS,
    Charset.LETTERS_DIGITS: LETTERS + DIGITS,
    Charset.INSTAGRAM: LETTERS + DIGITS + INSTAGRAM_EXTRA,
}

MAX_SUPPORTED_LENGTH: Final = 30
DEFAULT_USER_AGENT: Final = (
    "instagram-username-finder/{version} "
    "(+https://github.com/FadeHack/instagram-username-finder)"
)


class ConfigError(ValueError):
    """Raised when configuration values are missing or inconsistent."""


@dataclass(slots=True)
class Config:
    """Fully resolved, validated runtime configuration."""

    # --- search space -----------------------------------------------------
    min_length: int = 3
    max_length: int = 4
    charset: Charset = Charset.LETTERS
    characters: str | None = None

    # --- concurrency and pacing -------------------------------------------
    concurrency: int = 5
    batch_size: int = 100
    delay: float = 0.5
    timeout: float = 10.0

    # --- retry and backoff ------------------------------------------------
    max_retries: int = 3
    retry_base_delay: float = 1.0
    retry_max_delay: float = 60.0
    circuit_breaker_threshold: int = 5
    rate_limit_cooldown: float = 60.0

    # --- outputs ----------------------------------------------------------
    output: Path | None = None
    format: OutputFormat = OutputFormat.JSON
    state_file: Path = Path("data/state.json")

    # --- behaviour --------------------------------------------------------
    resume: bool = False
    fresh: bool = False
    stop_on_first: bool = True
    max_checks: int | None = None
    time_limit: float | None = None

    # --- transport --------------------------------------------------------
    base_url: str = "https://www.instagram.com"
    user_agent: str | None = None

    # --- logging ----------------------------------------------------------
    verbose: bool = False
    quiet: bool = False
    no_progress: bool = False

    #: Resolved alphabet, sorted and de-duplicated. Set during validation.
    alphabet: str = field(default="", init=False)

    def __post_init__(self) -> None:
        self.validate()

    # ------------------------------------------------------------------
    # validation
    # ------------------------------------------------------------------
    def validate(self) -> None:
        if self.min_length < 1:
            raise ConfigError("min_length must be >= 1")
        if self.max_length < self.min_length:
            raise ConfigError("max_length must be >= min_length")
        if self.max_length > MAX_SUPPORTED_LENGTH:
            raise ConfigError(f"max_length must be <= {MAX_SUPPORTED_LENGTH}")
        if self.concurrency < 1:
            raise ConfigError("concurrency must be >= 1")
        if self.batch_size < 1:
            raise ConfigError("batch_size must be >= 1")
        if self.delay < 0:
            raise ConfigError("delay must be >= 0")
        if self.timeout <= 0:
            raise ConfigError("timeout must be > 0")
        if self.max_retries < 0:
            raise ConfigError("max_retries must be >= 0")
        if self.retry_base_delay <= 0:
            raise ConfigError("retry_base_delay must be > 0")
        if self.retry_max_delay < self.retry_base_delay:
            raise ConfigError("retry_max_delay must be >= retry_base_delay")
        if self.circuit_breaker_threshold < 1:
            raise ConfigError("circuit_breaker_threshold must be >= 1")
        if self.rate_limit_cooldown < 0:
            raise ConfigError("rate_limit_cooldown must be >= 0")
        if self.max_checks is not None and self.max_checks < 1:
            raise ConfigError("max_checks must be >= 1")
        if self.time_limit is not None and self.time_limit <= 0:
            raise ConfigError("time_limit must be > 0")
        if self.resume and self.fresh:
            raise ConfigError("--resume and --fresh are mutually exclusive")
        if self.verbose and self.quiet:
            raise ConfigError("--verbose and --quiet are mutually exclusive")

        self.alphabet = self._resolve_alphabet()

    def _resolve_alphabet(self) -> str:
        if self.charset is Charset.CUSTOM or self.characters:
            if not self.characters:
                raise ConfigError(
                    "charset 'custom' requires --characters (e.g. --characters abc123)"
                )
            raw = self.characters
        else:
            raw = CHARSET_TABLE[self.charset]

        alphabet = "".join(sorted(set(raw)))
        if not alphabet:
            raise ConfigError("character set resolved to an empty alphabet")

        allowed = set(LETTERS + DIGITS + INSTAGRAM_EXTRA)
        invalid = sorted(set(alphabet) - allowed)
        if invalid:
            raise ConfigError(
                "characters contain values Instagram usernames cannot use: "
                + ", ".join(repr(character) for character in invalid)
            )
        return alphabet

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def fingerprint(self) -> str:
        """Identity of the search space.

        A saved state may only be resumed by a scan with the same fingerprint,
        because indices are meaningless against a different alphabet.
        """
        return f"{self.alphabet}|{self.min_length}-{self.max_length}"

    def resolved_user_agent(self, version: str) -> str:
        if self.user_agent:
            return self.user_agent
        return DEFAULT_USER_AGENT.format(version=version)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["charset"] = self.charset.value
        payload["format"] = self.format.value
        payload["state_file"] = str(self.state_file)
        payload["output"] = str(self.output) if self.output else None
        return payload


# ----------------------------------------------------------------------
# layer loaders
# ----------------------------------------------------------------------
_FIELD_NAMES: Final = frozenset(f.name for f in fields(Config) if f.name != "alphabet")

_COERCERS: Final[dict[str, Any]] = {
    "min_length": int,
    "max_length": int,
    "concurrency": int,
    "batch_size": int,
    "max_retries": int,
    "circuit_breaker_threshold": int,
    "max_checks": int,
    "delay": float,
    "timeout": float,
    "retry_base_delay": float,
    "retry_max_delay": float,
    "rate_limit_cooldown": float,
    "time_limit": float,
    "charset": Charset,
    "format": OutputFormat,
    "state_file": Path,
    "output": Path,
    "characters": str,
    "base_url": str,
    "user_agent": str,
}

_BOOL_TRUE: Final = frozenset({"1", "true", "yes", "on"})
_BOOL_FALSE: Final = frozenset({"0", "false", "no", "off"})


def load_config_file(path: Path) -> dict[str, Any]:
    """Read a TOML config file into an overrides dict."""
    if not path.exists():
        raise ConfigError(f"config file not found: {path}")
    try:
        with path.open("rb") as handle:
            raw = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {path}: {exc}") from exc

    # Allow either a flat file or a [scan] table.
    table = raw.get("scan", raw)
    if not isinstance(table, dict):
        raise ConfigError(f"{path}: expected a table of settings")
    return _clean(table, source=str(path))


def load_env(environ: dict[str, str] | None = None) -> dict[str, Any]:
    """Read ``USERNAME_FINDER_*`` variables into an overrides dict."""
    source = os.environ if environ is None else environ
    overrides: dict[str, Any] = {}
    for key, value in source.items():
        if not key.startswith(ENV_PREFIX):
            continue
        name = key[len(ENV_PREFIX) :].lower()
        if name in _FIELD_NAMES:
            overrides[name] = value
    return _clean(overrides, source="environment")


def build_config(
    *,
    cli: dict[str, Any] | None = None,
    env: dict[str, str] | None = None,
    config_file: Path | None = None,
) -> Config:
    """Merge every layer in precedence order and validate the result."""
    merged: dict[str, Any] = {}
    if config_file is not None:
        merged.update(load_config_file(config_file))
    merged.update(load_env(env))
    if cli:
        merged.update(_clean(cli, source="command line"))

    unknown = sorted(set(merged) - _FIELD_NAMES)
    if unknown:
        raise ConfigError("unknown settings: " + ", ".join(unknown))

    return Config(**merged)


def _clean(raw: dict[str, Any], *, source: str) -> dict[str, Any]:
    """Drop ``None`` values and coerce the rest to their declared types."""
    cleaned: dict[str, Any] = {}
    for key, value in raw.items():
        if value is None:
            continue
        if key not in _FIELD_NAMES:
            cleaned[key] = value  # reported later as "unknown setting"
            continue
        try:
            cleaned[key] = _coerce(key, value)
        except (ValueError, KeyError) as exc:
            raise ConfigError(f"{source}: invalid value for {key!r}: {value!r}") from exc
    return cleaned


def _coerce(key: str, value: Any) -> Any:
    coercer = _COERCERS.get(key)
    if coercer is None:  # boolean flags
        return _coerce_bool(value)
    if isinstance(value, coercer) and not isinstance(value, bool):
        return value
    if coercer is Charset:
        return Charset(str(value).lower())
    if coercer is OutputFormat:
        return OutputFormat(str(value).lower())
    return coercer(value)


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in _BOOL_TRUE:
        return True
    if text in _BOOL_FALSE:
        return False
    raise ValueError(f"cannot interpret {value!r} as a boolean")
