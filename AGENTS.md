# AGENTS.md

Guidance for AI coding agents working in this repository. Humans should read
[CONTRIBUTING.md](CONTRIBUTING.md) — this file covers the same ground more
tersely, plus the constraints that are easy to violate without noticing.

## What this project is

A CLI that finds Instagram usernames with no publicly accessible profile. It
reports `POSSIBLY_AVAILABLE`, which means exactly one thing: *no public profile
was observed*. It never means the username can be registered.

## Commands

```bash
pip install -e ".[dev]"   # setup
make check                # lint + format check + typecheck + tests (what CI runs)
make test                 # pytest
make lint                 # ruff check .
make format               # ruff format + safe autofixes
make typecheck            # mypy src (strict)
make docker-build         # build the image
```

Always run `make check` before finishing. CI runs the same gates on Python
3.11, 3.12 and 3.13.

## Hard constraints

These are not preferences. A change that violates one is wrong regardless of
how well it works.

**1. Never add anything that circumvents platform restrictions.**
No proxy or IP rotation, CAPTCHA or auth bypass, cookie/session/account
rotation, browser fingerprint spoofing, or automated account creation. If a
task asks you to make scanning faster or avoid rate limits, the acceptable
answers are: adjust pacing settings, or back off sooner. Nothing else. Say so
rather than implementing a workaround.

**2. Never let uncertainty become an availability claim.**
Unrecognised markup, login walls, timeouts, connection errors and unexpected
statuses all resolve to `UNKNOWN` / `TIMEOUT` / `NETWORK_ERROR` — never
`POSSIBLY_AVAILABLE`. Classification requires *positive evidence*. This is not
theoretical: an earlier version defaulted unrecognised HTTP 200 pages to
`POSSIBLY_AVAILABLE`, and because Instagram serves 200 for profiles that do
not exist, every unrecognised page would have become a false candidate.

**3. Never describe results as guaranteed.**
Not in code, docs, CLI output, commit messages or comments. `POSSIBLY_AVAILABLE`
is the strongest claim available.

**4. No test may touch the real Instagram.**
Use the local aiohttp server in `tests/unit/test_checker.py`, the fake checkers
in `tests/integration/test_scan.py`, or the captured fixtures in
`tests/fixtures/`. Tests must pass offline.

**5. Keep the scan bounded.**
Candidate generation is lazy, concurrency is capped, batches bound memory, and
stop conditions are checked before *every* candidate — not per batch. Do not
materialise the search space, create unbounded tasks, or accumulate unbounded
results. See rule 2 in "Known traps" below.

## Architecture

```text
CLI → Config → Scanner → {Generator, RateLimiter, Checker → Retry, Persistence, Output}
```

One responsibility per module. The scanner does not format terminal output, the
checker does not persist, the generator makes no requests, `retry.py` never
sleeps. Preserve these boundaries — they are what makes the code testable
without patching timers or sockets. Full detail in
[docs/architecture.md](docs/architecture.md).

Anything crossing a module boundary is a dataclass or enum from `models.py`,
not a dictionary.

## Known traps

1. **Instagram returns HTTP 200 for non-existent profiles**, not 404. The real
   signal is Open Graph metadata: a real profile carries `og:title`,
   `og:description` and `al:ios:url`; a missing one serves a generic shell with
   a bare `<title>Instagram</title>` and none of them.
2. **Stop conditions must be checked per candidate.** A throttled scan pauses
   for an escalating cooldown before each request, so one batch can span hours.
   Checking only at batch boundaries leaves the circuit breaker, `--time-limit`
   and `--max-checks` unenforced for all of it.
3. **Not every string in the page is a marker.** `profilePage_` and
   `PolarisProfile` appear in the JavaScript bundle of *both* real and missing
   profiles. Verify a proposed marker against both fixtures before trusting it.
4. **Resume state is index-addressed.** Indices are meaningless against a
   different alphabet, which is why `Config.fingerprint()` gates resuming. Do
   not loosen that check.

## Style

- Python 3.11+, fully typed; `mypy src` is strict and must pass with no new
  `# type: ignore`.
- Line length 92, enforced by Ruff.
- Comments explain *why*. The code already says what.
- Match the surrounding style rather than introducing a new one.

## Before you finish

- `make check` passes.
- New behaviour has tests; a bug fix has a test that fails without the fix.
- `CHANGELOG.md` updated under **Unreleased**.
- Docs updated if flags or behaviour changed.
- No credentials, cookies, tokens, scan results or state files committed.
  This tool never authenticates, so it should never handle a secret at all.
