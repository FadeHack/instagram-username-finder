# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- Declare the license as a PEP 639 SPDX expression (`license = "MIT"` with
  `license-files`) instead of embedding the full licence text in the metadata,
  so package indexes show a plain "MIT" rather than the whole file. The
  now-redundant `License :: OSI Approved :: MIT License` classifier is dropped,
  and the build backend requires `hatchling>=1.27` for PEP 639 support.

### Fixed

- Pin `aquasecurity/trivy-action` to `v0.36.0`. The tags carry a `v` prefix, so
  the previous reference did not resolve and the vulnerability scan job failed
  during action setup.

## [0.1.0] - 2026-08-17

Initial public release.

### Added

- **CLI** — `instagram-finder scan` with `--help` and `--version`, layered
  configuration (CLI → environment → TOML → defaults), and distinct exit codes
  for clean stops, rate-limit stops and interruption.
- **Lazy username generator** — deterministic, index-addressable odometer over a
  sorted alphabet, with `letters`, `digits`, `letters_digits`, `instagram` and
  custom character sets. Structurally invalid usernames are skipped.
- **Shortest-first search** with `--stop-on-first` (default) and `--collect-all`.
- **Bounded async scanning** — batches feed a bounded queue consumed by a fixed
  worker pool over a single pooled `aiohttp` session.
- **Conservative classification** — `taken`, `possibly_available`,
  `rate_limited`, `timeout`, `network_error`, `unknown`. Ambiguous responses and
  login walls resolve to `unknown` and are never reported as available.
- **Responsible networking** — configurable delay and concurrency, per-request
  timeouts, exponential backoff with jitter, `Retry-After` support, a separate
  and smaller retry budget for `403`/`429`, cooldown pauses, and a circuit
  breaker that stops the scan after persistent throttling.
- **Resumable scans** — atomic, versioned state checkpoints after every batch,
  with `--resume`, `--fresh`, corrupt-state quarantine, and a search-space
  fingerprint that refuses to resume against a different alphabet or range.
- **Graceful shutdown** — `SIGINT`/`SIGTERM` stop new work, let in-flight
  requests finish, persist state and flush results.
- **Output** — live Rich dashboard on stderr, plus `txt`, `json` and `csv`
  export to a file or stdout, with the availability disclaimer embedded.
- **Run limits** — `--max-checks` and `--time-limit` for bounded runs.
- **Docker** — multi-stage build, non-root runtime user, no build tools or
  secrets in the runtime image, plus an optional Compose file.
- **GitHub Actions** — CI (Ruff, MyPy, pytest on 3.11–3.13, package build),
  Docker build verification with a Trivy scan, opt-in bounded scheduled scans
  with Actions-cache state persistence, and a tag-triggered release workflow
  publishing to GHCR.
- **Documentation** — README, architecture, configuration, Docker, GitHub
  Actions and troubleshooting guides; contributing, security and code-of-conduct
  policies; issue and pull request templates.
- **Tests** — 236 tests covering generation, classification, retry, rate
  limiting, persistence, output, CLI and end-to-end scans. None contact
  Instagram.

[Unreleased]: https://github.com/FadeHack/instagram-username-finder/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/FadeHack/instagram-username-finder/releases/tag/v0.1.0
