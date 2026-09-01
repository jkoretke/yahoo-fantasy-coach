# Security Policy

## Supported versions

This project has no tagged releases. Only the latest commit on the default
branch (`master`) is supported. If you find a problem on an older commit,
please confirm it still reproduces on the current `master` before reporting.

## Reporting a vulnerability

Please report security issues privately: go to the repository's Security
tab and click "Report a vulnerability".

If that option is not available to you, open a normal GitHub issue instead,
but describe only the class of problem. Do not include a working exploit or
step-by-step extraction path in a public issue.

## What is in scope

This project reads the Yahoo Fantasy Sports API in a read-only way. It has
no write path back to Yahoo. Credentials are held on the user's own machine,
in `~/.config/yahoo-fantasy-coach/secrets.env`, and are never stored in this
repository.

The highest-value reports are:

- Secrets leaking through CI logs, a workflow file, or committed config.
- Any code path that would let this project write to Yahoo instead of only
  reading from it.
- Any other way a run of this project could expose credentials or personal
  fantasy league data to someone who should not see them.

## Please do not

- Do not open a public issue containing a real Yahoo client id, client
  secret, refresh token, or Brevo API key.
- Do not paste a real run's JSON output if it contains personal data.

Redact or describe these instead, and use the private reporting path above
if the report itself requires including sensitive detail.
