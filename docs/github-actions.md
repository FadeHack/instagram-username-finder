# GitHub Actions

Four workflows ship with the project.

| Workflow | Trigger | Purpose |
| --- | --- | --- |
| [`ci.yml`](../.github/workflows/ci.yml) | push to `main`, pull request | Lint, type check, test, build |
| [`docker.yml`](../.github/workflows/docker.yml) | push/PR touching image files | Build and verify the image |
| [`scheduled-scan.yml`](../.github/workflows/scheduled-scan.yml) | manual, daily cron (opt-in) | Bounded scanning |
| [`release.yml`](../.github/workflows/release.yml) | `v*.*.*` tags | Publish package and image |

## CI

Runs on every push to `main` and every pull request:

```text
Checkout → Setup Python → Install → Ruff → Ruff format → MyPy → Pytest → CLI smoke test
```

The matrix covers **Python 3.11, 3.12 and 3.13**. A second job builds the sdist
and wheel, installs the wheel into a clean virtualenv, runs
`instagram-finder --version` against it, and uploads the distributions.

Reproduce the whole thing locally:

```bash
make check
```

## Docker CI

Triggered only when something that affects the image changes (`Dockerfile`,
`requirements.txt`, `pyproject.toml`, `src/**`). It builds the image and then
verifies it actually works:

1. `docker run --rm image --version`
2. `docker run --rm image --help`
3. `docker run --rm image scan --help`
4. the container does **not** run as root
5. `/app/data` is writable when mounted

A second, advisory job runs [Trivy](https://github.com/aquasecurity/trivy)
against the image for `HIGH` and `CRITICAL` vulnerabilities and uploads SARIF to
the repository's Security tab. It is `continue-on-error`: a fresh CVE in the
Debian base image should not block an unrelated pull request.

Images are **never pushed** here. Publishing happens only in `release.yml`.

## Scheduled scans

### Enabling and disabling

Scheduled scanning is **off by default**. A fork that does nothing makes no
requests to Instagram, which is the correct default for a tool like this.

To enable it: **Settings → Secrets and variables → Actions → Variables**, add

```text
ENABLE_SCHEDULED_SCAN = true
```

To disable it again, delete the variable or set it to anything else. The cron
job still fires, but the gate step exits immediately without installing or
scanning.

`workflow_dispatch` ignores the gate, so manual runs always work.

### Manual runs

**Actions → Scheduled scan → Run workflow**, with inputs:

| Input | Default | Meaning |
| --- | --- | --- |
| `min_length` | `4` | Shortest length |
| `max_length` | `4` | Longest length |
| `charset` | `letters` | Character set |
| `max_checks` | `500` | Hard cap on checks for this run |
| `time_limit` | `900` | Hard cap in seconds for this run |
| `fresh` | `false` | Ignore saved state and start over |

### Bounded by construction

Every execution follows the same shape and then exits:

```text
Gate check
   ↓
Restore state (Actions cache)
   ↓
Run a bounded scan (--max-checks / --time-limit)
   ↓
Save state (Actions cache)
   ↓
Upload results + state (artifact)
   ↓
Exit
```

Four independent bounds keep a run finite: `--max-checks`, `--time-limit`, the
job's `timeout-minutes: 30`, and the circuit breaker. Nothing runs indefinitely.

Pacing is also more conservative than the local defaults — `concurrency 3`,
`delay 1.0` — because a scheduled scan has no deadline.

Runs are serialised through a `concurrency` group with
`cancel-in-progress: false`. Two scans sharing one state file would duplicate
work and double the request rate; and cancelling a scan mid-flight would discard
the checkpoint it was about to write.

Exit code `4` (circuit breaker tripped) is translated into a workflow
**warning**, not a failure: progress was saved and the next run resumes.

### State persistence

State persists through the **GitHub Actions cache**.

The cache key includes `github.run_id`, so every run writes a new entry, and
`restore-keys` falls back to the newest entry with the matching prefix:

```yaml
key: scan-state-${{ env.CHARSET }}-${{ env.MIN_LENGTH }}-${{ env.MAX_LENGTH }}-${{ github.run_id }}
restore-keys: |
  scan-state-${{ env.CHARSET }}-${{ env.MIN_LENGTH }}-${{ env.MAX_LENGTH }}-
```

(Cache entries are immutable, which is why a fresh key per run is necessary
rather than merely convenient.)

Why the cache, and not the alternatives:

| Mechanism | Verdict |
| --- | --- |
| **Actions cache** | ✅ **Chosen.** Built in, no credentials, no commits, survives between runs, trivially inspectable. |
| Artifacts | Retrievable across runs only via the API with extra token handling. Used here for *results*, which suits their one-run-one-download nature. |
| Repository commits | Rejected. A commit per run pollutes history and pushes to a protected branch — exactly the "unnecessary Git commits" this project avoids. |
| Release assets | Rejected. Abuses releases for mutable state and needs write permissions. |
| External storage (S3, …) | Rejected. Credentials, cost and setup for a small JSON file. |

The trade-off: **caches are evicted after 7 days without a hit, and a repository
has a 10 GB cache budget.** For a daily scan neither matters. If a cache is
evicted, the scan restarts from the beginning of the current length — the
results artifacts from previous runs are still available.

### Artifacts

Each run uploads `data/results.json` and `data/state.json` as
`scan-results-<run_id>`, kept for 30 days. The job summary shows checks
performed, candidates found, errors, stop reason, and the availability
disclaimer.

Artifacts are the durable record; the cache is only the resume mechanism.

## Releases

Push a version tag:

```bash
git tag v0.1.0
git push origin v0.1.0
```

Which runs:

```text
Test (Ruff, MyPy, pytest)
   ↓
Build sdist + wheel, verify the tag matches pyproject version
   ↓
Build and push multi-arch image to GHCR
   ↓
Create the GitHub release with the distributions attached
   ↓
Optionally publish to PyPI
```

The tag/version check fails loudly if `v0.2.0` is pushed while `pyproject.toml`
still says `0.1.0`, before anything is published.

Images land at `ghcr.io/<owner>/instagram-username-finder`, tagged
`{version}`, `{major}.{minor}` and `latest`, for `linux/amd64` and
`linux/arm64`.

### PyPI publishing (optional)

The `pypi` job is skipped unless the repository variable `PUBLISH_TO_PYPI` is
`true`. To enable it:

1. Configure [trusted publishing](https://docs.pypi.org/trusted-publishers/) on
   PyPI for this repository and workflow.
2. Create a repository environment named `pypi`.
3. Set `PUBLISH_TO_PYPI = true`.

Trusted publishing uses OIDC, so no PyPI token is ever stored in the repository.

## Permissions and secrets

Workflows request the minimum they need:

| Workflow | Permissions |
| --- | --- |
| `ci.yml` | `contents: read` |
| `docker.yml` | `contents: read`, `security-events: write` (SARIF upload) |
| `scheduled-scan.yml` | `contents: read` |
| `release.yml` | `contents: read`; `packages: write` for GHCR, `contents: write` for the release, `id-token: write` for PyPI |

No credentials are stored in the repository. GHCR uses the per-run
`GITHUB_TOKEN`; PyPI uses OIDC. Scanning needs no credentials at all, because the
tool never authenticates.

## Forking

A fresh fork does nothing on a schedule. If you enable scheduled scanning:

- Keep the pacing conservative. The defaults in the workflow already are.
- Keep runs bounded. `max_checks` and `time_limit` exist for this.
- Remember GitHub's Actions usage policies apply to your runs, and Instagram's
  terms apply to your scanning.
- Treat `POSSIBLY_AVAILABLE` output as leads to verify, never as results.
