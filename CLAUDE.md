# CLAUDE.md

Guidance for Claude Code working in this repo. Durable rules only. For what is currently
true, read `docs/plan.md` (local, not tracked). For install and config, read `docs/setup.md`.

## What this is

Reads a Yahoo Fantasy Football league, computes the week's lineup, start/sit calls, matchup
projection, waiver targets and trade ideas, and hands the owner the decisions. He taps every
move into the Yahoo app himself.

## Python decides, Claude only writes

Python computes every number and every verdict: scoring, the optimal legal lineup, the matchup
projection, waiver ranking, trades. Claude writes prose from the JSON Python already produced,
nothing more. `engine/prose_gate.py` rejects any draft that disagrees with that JSON.

One narrow exception. When two options are within `toss_up_margin_points` (default 2.0), Python
flags a toss-up instead of a verdict, and only there may Claude's news reading break the tie.
Outside that band the number always wins.

## Never write to Yahoo

The Yahoo Fantasy Sports API grants no write access. Every routine ends in output the owner
acts on by hand. Do not add code that sets a lineup or submits a claim through the API.

## Files that must never be tracked

`config/league.yaml` and `docs/plan.md` both hold the owner's real email. Both were committed
once and needed a full `git filter-branch` history rewrite to remove. They are gitignored. Do
not `git add -f` either one, and do not paste their contents into a tracked file.

Real credentials live only in `~/.config/yahoo-fantasy-coach/secrets.env`, never in the repo.

## This repo is public

It lives at `github.com/jkoretke/yahoo-fantasy-coach`, the owner's personal account, never the
`moonsail-software` org. Assume anything you commit is readable by anyone.

`master` is protected: a PR with a code-owner review is required, force-push and branch
deletion are blocked, and there is no bypass even for the owner. Commit locally and let him
decide the PR. Never push.

## Exit codes follow the STATUS line

A wrapper exits 1 on a `STATUS failed ...` outcome and 0 on everything else. A legitimate
skip (`skipped no-games`, `skipped not-yet`) is a success on purpose: `gameday_run` and
`inactive_run` print those constantly, so failing there would alert every five minutes.

Keep it that way. The exit code and the printed STATUS line must never disagree, which is
why `run_common.print_status` returns the code the wrapper then returns.

## The news pass costs money, the other sources do not

`engine/sources/news.py` spawns a `claude` subprocess. Sleeper, ESPN and Open-Meteo are free
public APIs. So the news pass is asked by `weekly_run` only, once per run, and never on a
`--fixtures` run. Do not call it from another routine without saying why.

Claude reports what it read there. It never produces a number or a verdict.

## Development

```
.venv/bin/python3 -m pytest -q
.venv/bin/python3 -m ruff check .
```

Python 3.10 or newer is required, because `yfpy` needs it. On an older Python, `yfpy` stays
pinned in `requirements.txt` but never actually installs, and the failure is silent.

Never let a test depend on whether `yfpy` is really present. Force the ImportError with
`monkeypatch.setitem(sys.modules, "yfpy", None)`.

A fresh checkout reports 2 skipped. Those two tests read the owner's gitignored
`config/league.yaml`, so they skip when it is absent. That is not breakage. No exact test
count is written here on purpose, because it goes stale the moment anyone adds a test.

Tests run fully offline with no credentials. `fixtures/` is the whole development path,
because Yahoo API access is granted per person and is approval gated.
