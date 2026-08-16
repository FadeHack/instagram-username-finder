# Configuration

## Precedence

Settings are resolved from four layers. Higher layers win:

```text
CLI flag
   ↓
Environment variable (USERNAME_FINDER_*)
   ↓
TOML config file (--config)
   ↓
Built-in default
```

Layers merge per setting, not wholesale: a config file can set `delay` while an
environment variable sets `concurrency` and a flag sets `--min-length`.

Validation happens once, after merging, so an invalid value produces the same
error message wherever it came from.

## Every setting

| Setting | CLI flag | Env var (`USERNAME_FINDER_…`) | Default | Notes |
| --- | --- | --- | --- | --- |
| `min_length` | `--min-length` | `MIN_LENGTH` | `3` | ≥ 1 |
| `max_length` | `--max-length` | `MAX_LENGTH` | `4` | ≥ `min_length`, ≤ 30 |
| `charset` | `--charset` | `CHARSET` | `letters` | `letters`, `digits`, `letters_digits`, `instagram`, `custom` |
| `characters` | `--characters` | `CHARACTERS` | – | Explicit alphabet; implies `custom` |
| `concurrency` | `--concurrency` | `CONCURRENCY` | `5` | In-flight requests |
| `batch_size` | `--batch-size` | `BATCH_SIZE` | `100` | Candidates per checkpoint |
| `delay` | `--delay` | `DELAY` | `0.5` | Minimum seconds between request starts |
| `timeout` | `--timeout` | `TIMEOUT` | `10` | Per-request timeout |
| `max_retries` | `--max-retries` | `MAX_RETRIES` | `3` | Timeouts, connection errors, 5xx |
| `retry_base_delay` | – | `RETRY_BASE_DELAY` | `1.0` | First backoff step |
| `retry_max_delay` | – | `RETRY_MAX_DELAY` | `60.0` | Backoff ceiling |
| `circuit_breaker_threshold` | – | `CIRCUIT_BREAKER_THRESHOLD` | `5` | Consecutive 403/429 before stopping |
| `rate_limit_cooldown` | – | `RATE_LIMIT_COOLDOWN` | `60.0` | Pause when `Retry-After` is absent |
| `max_checks` | `--max-checks` | `MAX_CHECKS` | – | Stop after N checks |
| `time_limit` | `--time-limit` | `TIME_LIMIT` | – | Stop after N seconds |
| `output` | `--output` | `OUTPUT` | – | File path, or `-` for stdout |
| `format` | `--format` | `FORMAT` | `json` | `txt`, `json`, `csv` |
| `state_file` | `--state-file` | `STATE_FILE` | `data/state.json` | Resume state location |
| `resume` | `--resume` | `RESUME` | `false` | Require existing state |
| `fresh` | `--fresh` | `FRESH` | `false` | Discard existing state |
| `stop_on_first` | `--stop-on-first` / `--collect-all` | `STOP_ON_FIRST` | `true` | Stop at first candidate |
| `base_url` | `--base-url` | `BASE_URL` | `https://www.instagram.com` | Mostly for testing |
| `user_agent` | `--user-agent` | `USER_AGENT` | tool identifier | Please keep it honest |
| `verbose` | `--verbose` | `VERBOSE` | `false` | Debug logging |
| `quiet` | `--quiet` | `QUIET` | `false` | Errors only |
| `no_progress` | `--no-progress` | `NO_PROGRESS` | `false` | Disable the dashboard |

Settings without a CLI flag are available through the config file and the
environment. They exist for tuning, and the defaults are sensible.

## Config file

```bash
instagram-finder scan --config config.toml
```

Keys may sit at the top level or inside a `[scan]` table:

```toml
min_length = 3
max_length = 4
charset = "letters"

concurrency = 5
batch_size = 100

delay = 0.5
timeout = 10
max_retries = 3

output = "data/results.json"
state_file = "data/state.json"

stop_on_first = true
```

```toml
[scan]
concurrency = 3
delay = 1.0
```

A missing file, invalid TOML, or an unrecognised key is an error rather than a
silent no-op. See [`examples/config.example.toml`](../examples/config.example.toml).

## Environment variables

Every setting maps to `USERNAME_FINDER_` + its upper-cased name:

```bash
export USERNAME_FINDER_CONCURRENCY=3
export USERNAME_FINDER_DELAY=1.0
export USERNAME_FINDER_STATE_FILE=/data/state.json
export USERNAME_FINDER_STOP_ON_FIRST=false

instagram-finder scan --min-length 4 --max-length 4
```

Booleans accept `1/true/yes/on` and `0/false/no/off`, case-insensitively.
Unrecognised `USERNAME_FINDER_*` variables are ignored; a recognised one with an
unparseable value is an error that names the variable.

This is the mechanism Docker and Compose use — see [docker.md](docker.md).

## Character sets

| Charset | Alphabet | Size | Space at length 3 |
| --- | --- | --- | --- |
| `letters` | `a-z` | 26 | 17,576 |
| `digits` | `0-9` | 10 | 1,000 |
| `letters_digits` | `a-z0-9` | 36 | 46,656 |
| `instagram` | `a-z0-9_.` | 38 | 54,872 |
| `custom` | `--characters` | varies | varies |

Notes:

- The alphabet is sorted and de-duplicated, so `--characters cba` and
  `--characters abc` produce identical, deterministic scans.
- Only characters Instagram permits (`a-z`, `0-9`, `_`, `.`) are accepted;
  anything else is rejected at validation time.
- Uppercase is not used: Instagram usernames are case-insensitive.
- With `instagram` or any alphabet containing `.`, structurally invalid
  candidates (leading, trailing or doubled periods) are skipped automatically.

## Sizing a scan

The space grows exponentially, so check the arithmetic before starting:

| Length | `letters` | `letters_digits` |
| --- | --- | --- |
| 3 | 17,576 | 46,656 |
| 4 | 456,976 | 1,679,616 |
| 5 | 11,881,376 | 60,466,176 |

At the default `--delay 0.5`, throughput is about two checks per second — roughly
2.4 hours for all four-letter usernames, and about 69 days for five. For anything
large, use `--max-checks` or `--time-limit` and resume across sessions.

## Tuning guidance

The defaults are deliberately conservative. Raising them mostly earns a rate
limit, which stops the scan sooner than the slower settings would have.

**If you are being rate limited** (exit code 4, `rate_limited` results):

```bash
instagram-finder scan --concurrency 2 --delay 2.0 --timeout 15 --resume
```

Also consider a longer cooldown:

```bash
export USERNAME_FINDER_RATE_LIMIT_COOLDOWN=300
```

**If your connection is flaky**, raise the retry budget rather than the
concurrency:

```bash
instagram-finder scan --max-retries 5 --timeout 20
```

**`batch_size`** trades checkpoint frequency against overhead. Smaller batches
mean less repeated work after an interruption (at most one batch) and more state
writes. The default of 100 is a reasonable middle; 20–50 suits an unstable
connection.

## Precedence in practice

```toml
# config.toml
concurrency = 1
delay = 2.5
min_length = 3
```

```bash
export USERNAME_FINDER_CONCURRENCY=7

instagram-finder scan --config config.toml --min-length 5 --max-length 6
```

Result:

| Setting | Value | Source |
| --- | --- | --- |
| `concurrency` | 7 | environment beats file |
| `delay` | 2.5 | file beats default |
| `min_length` | 5 | CLI beats file |
| `max_length` | 6 | CLI |
| `charset` | `letters` | default |
