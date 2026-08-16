# Contributing

Thanks for considering a contribution. This project aims to be small, readable
and dependable, and to stay well-behaved toward a service it does not own.

## Scope

Before opening a pull request, note what this project will not accept:

- proxy or IP rotation intended to bypass rate limits
- CAPTCHA or authentication bypass
- cookie, session or account rotation to evade restrictions
- browser fingerprint spoofing
- automated account creation
- anything else intended to circumvent platform restrictions

Also non-negotiable: results are never described as guaranteed available.
`POSSIBLY_AVAILABLE` is the strongest claim the tool makes.

## Workflow

```text
Fork
 ↓
Clone
 ↓
Branch
 ↓
Install dev dependencies
 ↓
Run tests
 ↓
Make changes
 ↓
Add tests
 ↓
Run lint / typecheck
 ↓
Submit PR
```

```bash
# 1. Fork on GitHub, then clone your fork
git clone https://github.com/<you>/instagram-username-finder.git
cd instagram-username-finder

# 2. Branch
git checkout -b fix/retry-after-parsing

# 3. Install with dev extras
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
# or: make install

# 4. Confirm a clean baseline
make check

# 5. Make your change, with tests

# 6. Re-run every gate before pushing
make check
```

Then open a pull request against `main` and fill in the template.

## Quality gates

CI runs exactly what `make check` runs, on Python 3.11, 3.12 and 3.13:

```bash
ruff check .            # lint
ruff format --check .   # formatting
mypy src                # strict type checking
pytest                  # tests
```

`make format` applies formatting and safe autofixes.

## Coding standards

- **Python 3.11+, fully typed.** `mypy src` runs in strict mode and must pass
  with no new `# type: ignore`. If you need one, explain why in a comment.
- **One responsibility per module.** The scanner orchestrates; the checker does
  HTTP; the generator produces candidates; persistence writes state; output
  formats results. Do not blur these — for example, the scanner must not print,
  and the checker must not save state.
- **Typed models, not dictionaries.** Anything crossing a module boundary is a
  dataclass or enum from `models.py`.
- **Keep dependencies minimal.** Runtime dependencies are `aiohttp` and `rich`.
  Adding a third needs a clear justification in the PR.
- **Laziness and bounds are load-bearing.** Never materialise the candidate
  space, never create an unbounded number of tasks, never accumulate unbounded
  results in memory.
- **Pure logic where possible.** Retry decisions, classification and generation
  are pure functions so they can be tested without patching timers or sockets.
- **Line length 92**, enforced by Ruff. Follow the surrounding style.
- **Comments explain why**, not what. The code already says what.

## Testing standards

- Every behavioural change needs a test. Bug fixes need a test that fails
  without the fix.
- **No test may contact Instagram**, or any real external host. Use the local
  aiohttp server pattern in `tests/unit/test_checker.py`, or a fake checker as
  in `tests/integration/test_scan.py`.
- Time-dependent behaviour uses the injected clock/sleeper (see
  `tests/unit/test_rate_limiter.py`) rather than real sleeping.
- Async tests need no decorator: `asyncio_mode = "auto"` is configured.
- Unit tests live in `tests/unit/`, end-to-end scans in `tests/integration/`.

## Commits and pull requests

- Write commit messages in the imperative mood: `Fix Retry-After date parsing`.
- Keep pull requests focused. Unrelated changes are much harder to review.
- Update `README.md` / `docs/` when behaviour or flags change.
- Add an entry to `CHANGELOG.md` under **Unreleased**.
- Never commit credentials, cookies, tokens, scan results or state files.
  `data/` is gitignored for exactly this reason.

## Reporting classification errors

If a username reported `POSSIBLY_AVAILABLE` turns out to be unregistrable, open
a [false availability report](.github/ISSUE_TEMPLATE/false_availability.yml).
These reports are the main way the classifier improves. Some divergence is
expected — Instagram reserves and holds names — but systematic errors are bugs.

## Releases

Maintainers only:

1. Update `CHANGELOG.md`, moving **Unreleased** entries under the new version.
2. Bump `version` in `pyproject.toml` and `__version__` in
   `src/instagram_username_finder/__init__.py`.
3. Tag `vX.Y.Z` and push. `release.yml` verifies that the tag matches the
   package version, runs the gates, builds, publishes the image to GHCR, and
   creates the GitHub release.

The project follows [semantic versioning](https://semver.org/).

## Code of conduct

Participation is governed by [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).
