# Contributing

## You cannot test against a real Yahoo league

Yahoo Fantasy Sports API access is granted per person, and Yahoo's approval process
takes weeks. Because of that, `fixtures/` is the entire development path for this
project. Every routine (weekly, gameday, waiver, inactive) runs fully offline against
the frozen sample league in `fixtures/`, and every change you make must be
demonstrable that way, with `--fixtures`.

A change that can only be verified against a live Yahoo league is much harder to
accept. If you genuinely cannot demonstrate a change offline, say so up front, in the
pull request description, and explain why.

## Setup

```
git clone <this repo>
cd yahoo-fantasy-coach
python3 -m venv .venv
.venv/bin/python3 -m pip install -r requirements-dev.txt
```

Always invoke the interpreter as `.venv/bin/python3`. Never use a bare `python3` or
`python`, they may resolve to a different, unconfigured interpreter.

## Running the four routines offline

```
.venv/bin/python3 -m engine.weekly_run --fixtures --dry-run
.venv/bin/python3 -m engine.gameday_run --fixtures --date 2026-09-14 --dry-run
.venv/bin/python3 -m engine.waiver_run --fixtures --dry-run
.venv/bin/python3 -m engine.inactive_run --fixtures --now 2026-09-14T11:45Z --dry-run
```

All four read from the checked in sample league and never touch the network.

## Tests

Every behavior change needs a test. Tests live in `tests/` and must not require
network access, credentials, or a real subprocess.

Before opening a pull request, run:

```
.venv/bin/python3 -m pytest -q
.venv/bin/python3 -m ruff check .
```

## Fixtures are frozen

`fixtures/sample_league/`, `fixtures/sources/` and `fixtures/yahoo/` are frozen. Add
new sample data in a new file or a new directory rather than renaming or
restructuring an existing one, so that other people's tests and fixtures keep
working against the same shapes.

## Keep decisions in Python

Python computes every number and every verdict. Claude only writes prose from the
JSON that Python already produced, and `engine/prose_gate.py` rejects a draft that
names a player absent from that JSON or that contradicts a START or BENCH verdict. A
contribution must not move a decision out of Python and into a prompt.

## Secrets

Never commit `config/league.yaml`, a `secrets.env`, a token, a real league id, or any
personal email address. Secrets belong only in `~/.config/yahoo-fantasy-coach/secrets.env`
on your own machine, never in the repo.

## Commit style

One logical change per commit. Write an imperative, one line subject. Stage files by
exact path, never with `git add -A` or `git add .`.

## Pull requests

CI (the `ci` workflow) must pass, and the owner is a required reviewer on every file
through CODEOWNERS. Your pull request description must include this checklist,
checked off honestly:

- [ ] `.venv/bin/python3 -m pytest -q` passes locally
- [ ] `.venv/bin/python3 -m ruff check .` reports no new violations
- [ ] Fixtures under `fixtures/` were updated if the change needs new sample data, and no existing fixture directory was renamed or restructured
- [ ] No secrets, tokens, API keys, real league ids or personal email addresses are in this diff
- [ ] `config/league.yaml` is not part of this diff (it is gitignored on purpose)

## Conduct and security

By contributing you agree to follow `CODE_OF_CONDUCT.md`. If you find a security
issue, follow the process in `SECURITY.md` rather than opening a public issue.
