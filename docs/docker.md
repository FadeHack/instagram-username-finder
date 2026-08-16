# Docker

## Build

```bash
docker build -t instagram-username-finder .
```

The build is multi-stage:

1. **builder** — creates a virtualenv, installs pinned runtime dependencies from
   `requirements.txt`, then installs the package itself.
2. **runtime** — copies only `/opt/venv` into a fresh `python:3.13-slim` image.

No compilers, no build tools, no source tree and no secrets reach the runtime
image. It runs as the unprivileged user `finder` (uid 1001).

Verify a build:

```bash
docker run --rm instagram-username-finder --version
docker run --rm instagram-username-finder --help
docker run --rm --entrypoint id instagram-username-finder   # uid=1001(finder)
```

## Run

`ENTRYPOINT` is `instagram-finder`, so arguments go straight through:

```bash
docker run --rm \
  instagram-username-finder \
  scan \
  --min-length 3 \
  --max-length 3 \
  --charset letters
```

With no arguments the image prints `--help`.

Terminal output is unbuffered (`PYTHONUNBUFFERED=1`), so logs stream live.
For the Rich dashboard, allocate a TTY:

```bash
docker run --rm -it -v "$PWD/data:/app/data" instagram-username-finder scan
```

## Volumes and state persistence

Everything worth keeping lives in `/app/data`, which is declared as a volume.
Without a mount, results and resume state vanish with the container:

```bash
mkdir -p data

docker run --rm \
  -v "$PWD/data:/app/data" \
  instagram-username-finder \
  scan --min-length 4 --max-length 4 --charset letters \
       --max-checks 5000 \
       --state-file /app/data/state.json \
       --output /app/data/results.json
```

Interrupt with `Ctrl+C` (or `docker stop`, which sends `SIGTERM`) and the scanner
finishes in-flight requests, saves state and exits. Resume with the same command
plus `--resume`:

```bash
docker run --rm \
  -v "$PWD/data:/app/data" \
  instagram-username-finder \
  scan --min-length 4 --max-length 4 --charset letters \
       --state-file /app/data/state.json \
       --output /app/data/results.json \
       --resume
```

`docker stop` allows 10 seconds by default; give a long-running scan more room
with `--stop-timeout 30`.

### Permissions

The container writes as uid 1001. If the host directory is owned by someone else
and not group-writable, the scan cannot save state. Fix the ownership:

```bash
sudo chown -R 1001:1001 data
```

…or run as your own user:

```bash
docker run --rm -u "$(id -u):$(id -g)" -v "$PWD/data:/app/data" \
  instagram-username-finder scan --help
```

## Environment variables

Every setting is available as `USERNAME_FINDER_*`, which is usually cleaner than
a long argument list:

```bash
docker run --rm \
  -e USERNAME_FINDER_CONCURRENCY=3 \
  -e USERNAME_FINDER_DELAY=1.0 \
  -e USERNAME_FINDER_MIN_LENGTH=4 \
  -e USERNAME_FINDER_MAX_LENGTH=4 \
  -e USERNAME_FINDER_OUTPUT=/app/data/results.json \
  -v "$PWD/data:/app/data" \
  instagram-username-finder scan
```

The image sets `USERNAME_FINDER_STATE_FILE=/app/data/state.json` by default, so
state lands in the volume whether or not you pass `--state-file`.

CLI flags still win over environment variables — see
[configuration.md](configuration.md).

## Configuration file

Mount a TOML file read-only:

```bash
docker run --rm \
  -v "$PWD/config.toml:/app/config.toml:ro" \
  -v "$PWD/data:/app/data" \
  instagram-username-finder \
  scan --config /app/config.toml
```

## Docker Compose

Compose is a convenience for development. It is **not** required for normal use.

```bash
docker compose run --rm scanner            # run the configured scan
docker compose run --rm scanner --help     # override the command
docker compose build                       # rebuild after code changes
```

The bundled [`docker-compose.yml`](../docker-compose.yml) mounts `./data`, sets
conservative pacing through environment variables, and defines a bounded example
scan. Edit it freely — it is a starting point, not a contract.

## GitHub Container Registry

Images are published to GHCR **only on version tags** (`v0.1.0`, `v1.0.0`, …),
never on pull requests or ordinary pushes to `main`.

```bash
docker pull ghcr.io/FadeHack/instagram-username-finder:latest
docker pull ghcr.io/FadeHack/instagram-username-finder:0.1.0
docker pull ghcr.io/FadeHack/instagram-username-finder:0.1

docker run --rm ghcr.io/FadeHack/instagram-username-finder:latest --version
```

Tags follow the pushed version: `{version}`, `{major}.{minor}`, and `latest`.
Images are built for `linux/amd64` and `linux/arm64`.

Publishing uses the per-run `GITHUB_TOKEN` with `packages: write`. No registry
credentials are stored in the repository.

## Image details

| Property | Value |
| --- | --- |
| Base image | `python:3.13-slim-bookworm` |
| Runtime user | `finder` (uid 1001, gid 1001) |
| Working directory | `/app` |
| Volume | `/app/data` |
| Entrypoint | `instagram-finder` |
| Default command | `--help` |
| Size | ~250 MB uncompressed |

`.dockerignore` keeps the build context small and, importantly, keeps `data/`
out of the image — scan results and state must never be baked into a layer.

## Troubleshooting

**"Permission denied" writing state** — see [Permissions](#permissions) above.

**Scan restarts from zero every run** — the state file is not in a mounted
volume. Confirm `--state-file` (or `USERNAME_FINDER_STATE_FILE`) points inside
`/app/data`, and that `/app/data` is mounted.

**No live dashboard** — Rich needs a TTY. Add `-it`, or accept the plain
line-oriented progress that non-interactive runs produce by design.

**Exit code 4** — the circuit breaker stopped the scan after persistent rate
limiting. Progress was saved; wait, then resume with a longer `--delay`.

**Build is slow after a code change** — dependencies are installed before the
source is copied, so only the final layers rebuild. If everything rebuilds,
check that `requirements.txt` really did not change.

More in [troubleshooting.md](troubleshooting.md).
