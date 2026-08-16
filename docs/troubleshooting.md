# Troubleshooting

## Exit codes

| Code | Meaning | What to do |
| --- | --- | --- |
| `0` | Completed, or stopped cleanly at a limit | Nothing |
| `1` | Unexpected error | Re-run with `--verbose`; open an issue with the traceback |
| `2` | Invalid usage or configuration | Read the message; it names the setting |
| `4` | Circuit breaker tripped after persistent rate limiting | Wait, then resume with a longer `--delay` |
| `130` | Interrupted (`SIGINT`/`SIGTERM`) | Progress saved; resume when ready |

## Rate limiting

### "My scan stops with exit code 4"

Working as designed. Instagram returned `403`/`429` repeatedly, so the tool
paused, saved state and stopped rather than pushing through.

```bash
# Wait a while (30+ minutes is realistic), then resume more gently
instagram-finder scan --concurrency 2 --delay 2.0 --resume
```

Longer cooldowns and a lower threshold make the tool back off sooner:

```bash
export USERNAME_FINDER_RATE_LIMIT_COOLDOWN=300
export USERNAME_FINDER_CIRCUIT_BREAKER_THRESHOLD=3
```

There is no setting that avoids rate limits, and the project will not add one.
Proxy/IP rotation, session rotation and fingerprint spoofing are explicitly out
of scope.

### "Everything comes back rate_limited immediately"

Your IP is likely in a cool-off period from earlier activity. Options: wait
(hours, sometimes longer), scan from a different network you legitimately
control, or reduce the scan's ambition. Repeatedly retrying makes it worse.

### "Results are all unknown with HTTP 200"

Instagram is serving a login wall. That response describes your session, not the
username, so it is classified `unknown` — never as available. Wait for the
restriction to lift and retry later. If it persists across networks, the page
markup may have changed; please open a
[false availability report](../.github/ISSUE_TEMPLATE/false_availability.yml).

## Resume and state

### "My scan starts from zero every time"

Check that the state file is where you think it is:

```bash
cat data/state.json | head -20
```

Common causes:

- `--state-file` differs between runs;
- in Docker, the state path is outside the mounted volume (see
  [docker.md](docker.md#volumes-and-state-persistence));
- `--fresh` is still in your command;
- the search space changed (see below);
- the file could not be written — re-run with `--verbose` and look for
  `could not save state`.

### "the existing state file was written for a different search space"

Indices are meaningless against a different alphabet, so resuming would silently
skip or repeat candidates. Either restore the original parameters, or:

```bash
instagram-finder scan --min-length 3 --max-length 5 --fresh
# or keep both scans alive with separate state files
instagram-finder scan --min-length 3 --max-length 5 --state-file data/wide.json
```

### "state file ... is malformed" / corrupt state

Without `--resume`, the tool moves the bad file to `state.json.corrupt` and
starts over. With `--resume` it refuses, so nothing is lost silently.

Recover found candidates from the corrupt file if you can — it is plain JSON —
then start fresh.

### "--resume was requested but no state file exists"

There is nothing to resume: the path is wrong, or the first run never
checkpointed. Drop `--resume` to start a new scan.

## Interruption

### "Does Ctrl+C lose my progress?"

No. `SIGINT` and `SIGTERM` stop new work, let in-flight requests finish, persist
state and flush results. You lose at most the current batch (`--batch-size`,
default 100). Smaller batches lose less and write state more often.

If a second `Ctrl+C` is needed, the process is likely stuck in a long timeout;
the state from the last completed batch is still on disk.

## Configuration

### "unknown settings: ..."

A key in your config file or a `USERNAME_FINDER_*` variable is not recognised.
Check the table in [configuration.md](configuration.md#every-setting) — the
message lists the offending names.

### "environment: invalid value for 'concurrency'"

The value could not be coerced (for example `USERNAME_FINDER_CONCURRENCY=many`).
The message names both the layer and the setting.

### "characters contain values Instagram usernames cannot use"

Only `a-z`, `0-9`, `_` and `.` are permitted. Uppercase is not used because
Instagram usernames are case-insensitive.

### "My flags seem to be ignored"

Check precedence: CLI beats environment beats config file beats defaults. An
exported `USERNAME_FINDER_*` variable cannot override a flag, but it does
override your config file:

```bash
env | grep USERNAME_FINDER
```

## Performance

### "The scan is very slow"

By design. At the default `--delay 0.5` throughput is roughly two checks per
second. All four-letter usernames take about 2.4 hours; five-letter usernames
take months.

For large spaces, work in bounded sessions:

```bash
instagram-finder scan --min-length 5 --max-length 5 --max-checks 20000
# later
instagram-finder scan --min-length 5 --max-length 5 --resume
```

Raising `--concurrency` and lowering `--delay` will get you rate limited, which
is slower than the conservative settings, not faster.

### "How large is my search space?"

`alphabet_size ** length`:

| Length | `letters` (26) | `letters_digits` (36) |
| --- | --- | --- |
| 3 | 17,576 | 46,656 |
| 4 | 456,976 | 1,679,616 |
| 5 | 11,881,376 | 60,466,176 |

### "Memory keeps growing"

It should not. Candidates are generated lazily, only `batch_size` are in memory
at once, `taken` results are counted rather than stored, and inconclusive
results are capped. If you observe unbounded growth, that is a bug — please
report it with your exact command.

## Output

### "My JSON file is full of terminal formatting"

It should never be. Progress goes to stderr, data to the file or to stdout with
`--output -`. If you are capturing both, separate them:

```bash
instagram-finder scan --output - --format json 2>/dev/null | jq '.summary'
```

### "No results file was written"

`--output` is required to write one. Without it, results are only summarised on
the terminal (and candidates are still recorded in the state file).

### "Where are the taken usernames in my output?"

Only candidates and a bounded sample of inconclusive results are exported. A
four-letter letters scan would otherwise write nearly half a million `taken`
rows. The counts are in `summary`.

## Docker

See [docker.md](docker.md#troubleshooting) for permissions, missing dashboards
and volume issues.

## Accuracy

### "A POSSIBLY_AVAILABLE username could not be registered"

Expected, at least sometimes. `POSSIBLY_AVAILABLE` means no publicly accessible
profile was observed — not that the name is registrable. Instagram reserves,
restricts and holds usernames, and deleted or suspended accounts can look
identical from the outside.

If the mismatch looks systematic, please file a
[false availability report](../.github/ISSUE_TEMPLATE/false_availability.yml).
Those reports are how the classifier improves.

### "A username I know exists was reported as available"

That is a genuine classification bug, and worth reporting with the username,
version, HTTP status and timestamp. It usually means Instagram changed its page
markup and the profile markers need updating.

## Development

### "pytest fails with an event loop or fixture error"

Async tests need no decorator — `asyncio_mode = "auto"` is set in
`pyproject.toml`. Install with dev extras:

```bash
pip install -e ".[dev]"
```

### "mypy fails on code I did not touch"

Confirm you are on Python 3.11+ and that dependencies are current:

```bash
pip install -e ".[dev]" --upgrade
mypy src
```

### "ruff format --check fails in CI but not locally"

Run the formatter and commit the result:

```bash
make format
```

## Still stuck?

Open a [bug report](../.github/ISSUE_TEMPLATE/bug_report.yml) with your OS,
Python version, project version, the exact command, and `--verbose` output.
**Redact anything sensitive** — though note this tool never uses credentials, so
there should be none to redact.
