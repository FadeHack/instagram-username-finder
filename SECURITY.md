# Security Policy

## Supported versions

| Version | Supported |
| --- | --- |
| 0.1.x | ✅ Actively maintained |
| < 0.1 | ❌ Not supported |

While the project is pre-1.0, security fixes land on the latest minor release.
Please upgrade before reporting: the issue may already be fixed.

## Reporting a vulnerability

**Please do not open a public issue for a security vulnerability.**

Report it privately through GitHub's private vulnerability reporting:

<https://github.com/FadeHack/instagram-username-finder/security/advisories/new>

Please include:

- a description of the vulnerability and its impact,
- the affected version (`instagram-finder --version`),
- steps to reproduce, ideally minimal,
- any suggested remediation.

### What to expect

| Stage | Target |
| --- | --- |
| Acknowledgement | within 3 business days |
| Initial assessment | within 7 business days |
| Fix or mitigation plan | within 30 days for confirmed issues |

We will keep you updated as the report progresses, credit you in the advisory
and changelog unless you prefer otherwise, and publish an advisory once a fix is
available.

## Responsible disclosure

Please give us a reasonable opportunity to fix an issue before disclosing it
publicly — 90 days is the usual window, shorter if a fix ships sooner. Do not
exploit a vulnerability beyond what is needed to demonstrate it, and do not
access, modify or exfiltrate data that is not yours.

We will not pursue legal action against researchers who follow this policy in
good faith. There is no paid bug bounty for this project.

## Never include secrets in a report

This tool needs **no credentials of any kind**. It reads publicly accessible
pages and never authenticates. So please never send us — in an issue, a pull
request, a log excerpt, a test fixture or an advisory:

- passwords
- cookies or `Set-Cookie` headers
- session IDs (`sessionid`, `csrftoken`, …)
- access tokens, API keys or bearer tokens
- `Authorization` headers
- personal data about anyone

If you accidentally include a secret, rotate it immediately and tell us so we
can help scrub what we can.

## Scope

**In scope**

- Vulnerabilities in this codebase (for example: arbitrary file write via a
  crafted state file or output path, code execution via a malicious config
  file, unsafe deserialisation, dependency vulnerabilities we can act on).
- Vulnerabilities in the published container image or release workflow.

**Out of scope**

- Vulnerabilities in Instagram or Meta's services. Report those to
  [Meta's bug bounty programme](https://www.facebook.com/whitehat).
- The tool being rate limited, blocked or receiving `403`/`429`. That is
  expected behaviour and is handled by design.
- Requests to add functionality that circumvents platform restrictions. These
  are out of scope by policy, not oversight — see the README's responsible-usage
  section.
- Findings that require an attacker to already control the machine running the
  tool.

## Security design notes

- The tool sends no credentials and stores none.
- No secrets are baked into the container image; it runs as a non-root user.
- Release publishing uses short-lived GitHub-minted tokens (`GITHUB_TOKEN`) and
  PyPI trusted publishing (OIDC). No long-lived credentials live in the repo.
- Logs never contain credentials, because none exist. `--verbose` logs URLs,
  statuses and timings only.
- State files are plain JSON containing usernames, counters and timestamps.
