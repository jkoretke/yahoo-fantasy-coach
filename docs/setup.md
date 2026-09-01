# Setup

This is the full setup reference for yahoo-fantasy-coach: installing it, configuring
a league, wiring up credentials, and the three ways to actually run it. README.md
stays short and links here; this file has the detail.

## Install

Clone the repo, then create a virtual environment and install dependencies into it:

```
git clone <this-repo-url>
cd yahoo-fantasy-coach
python3 -m venv .venv
.venv/bin/python3 -m pip install -r requirements.txt
```

Use `.venv/bin/python3` for every command in this repo, never a bare `python3`. A bare
`python3` on your machine may resolve to a different, unconfigured interpreter with none
of these dependencies installed.

Python 3.10 or newer is required (the `yfpy` dependency needs it); this project is
developed on Python 3.13.

Once installed, prove it works with the zero-credential smoke test:

```
.venv/bin/python3 -m engine.weekly_run --fixtures --dry-run
```

This works immediately, with no Yahoo access and no secrets of any kind, because
`--fixtures` points the engine at the checked-in sample league under `fixtures/`
instead of calling Yahoo.

## Configure your league

Copy the example config and edit it:

```
cp config/league.example.yaml config/league.yaml
```

`config/league.yaml` is gitignored on purpose. It is never committed, never pushed,
and never delivered by a `git pull`, so every place this project runs (your laptop,
a box, a GitHub Actions runner) needs its own copy created by hand.

`engine.config` merges your file over the built-in defaults, so a partial file is
legal: include only the keys you want to change. The keys that matter most:

- `league.league_id`: Yahoo's league key, of the form `<game_id>.l.<league_id>`
  (for example `449.l.123456`).
- `league.season`: the NFL season year this config points at.
- `league.game_id`: Yahoo's numeric game id for that season. Leave it `null` to let
  the engine resolve it from `league_id` or the season.
- `league.team_id`: your own team id within the league.
- `timezone`: the IANA timezone name used for every date and time this coach prints
  or schedules against.
- `waiver.day` and `waiver.time`: when your league's waivers actually process, used
  to time the waiver routine ahead of that deadline.
- `email.backend`: `brevo` (the Brevo transactional API) or `smtp` (curl over SMTP,
  for example Gmail). The shipped example defaults this to `smtp`.
- `email.to`, `email.from_email`, `email.from_name`: who the run summary is sent to
  and from.
- `email.curlrc`: path to the curl config file holding SMTP credentials, only read
  when `email.backend` is `smtp`.
- `sources.sleeper`, `sources.schedule`, `sources.injuries`, `sources.weather`:
  toggles for each optional data source. Turning one off falls back to Yahoo-only
  data for that signal instead of failing the run.
- `toss_up_margin_points`: the point margin below which two lineup or waiver options
  are treated as a toss-up rather than a confident recommendation.
- `claude.binary` and `claude.timeout_seconds`: the `claude` CLI binary to invoke for
  prose summaries, and how long to wait for it before giving up.

## Credentials

See README.md, "Yahoo API setup (do this first, one time)" for how to create a
Yahoo developer app and obtain your client id, client secret, and refresh token.
That is a one-time interactive step and is not repeated here; this section only
documents where those values live once you have them.

- `~/.config/yahoo-fantasy-coach/secrets.env`, directory mode 700, file mode 600.
  Holds `YAHOO_CLIENT_ID` and `YAHOO_CLIENT_SECRET`, plus `BREVO_API_KEY` if (and
  only if) `email.backend` is `brevo`. `deploy/secrets.env.example` is the template.
- `~/.config/yahoo-fantasy-coach/.env`, written and rewritten by `yfpy` itself on
  every token refresh, holding `YAHOO_CONSUMER_KEY`, `YAHOO_CONSUMER_SECRET`, and
  `YAHOO_REFRESH_TOKEN`. This is a full secret file, not a cache: treat it like
  `secrets.env`. The engine chmods it 600 (and its directory 700) after writing it.
- `~/.config/yahoo-fantasy-coach/curlrc`, mode 600, only needed for the `smtp` email
  backend (the exact path comes from `email.curlrc`). `engine.notify` checks that
  this file exists before trying to send, and prints "cannot send via smtp (no
  curlrc at ...)" when it does not, so the run finishes having sent no email. The
  `brevo` backend needs no curlrc at all.

One behavior worth stating plainly: `engine.common.load_secrets` returns nothing at
all when the secrets file is absent, full stop. Its environment-variable override
only replaces a key that is already present in that file; it never invents a new
key from an environment variable alone. Setting `YAHOO_CLIENT_ID` and friends as
plain environment variables is not enough by itself, the file has to exist first.
This is exactly why the Actions lane below writes `secrets.env` before every run
rather than relying on `env:` alone.

## Lane 1: ask Claude Code (no scheduling)

The simplest way to use this project is to not schedule anything: clone it, ask
Claude Code to run one of the four routines, and read the recommendation as chat
output. README.md walks through this as the primary path.

The four commands, for reference, all runnable with the checked-in fixtures and no
credentials:

```
.venv/bin/python3 -m engine.weekly_run --fixtures --dry-run
.venv/bin/python3 -m engine.gameday_run --fixtures --date 2026-09-14 --dry-run
.venv/bin/python3 -m engine.waiver_run --fixtures --dry-run
.venv/bin/python3 -m engine.inactive_run --fixtures --now 2026-09-14T11:45Z --dry-run
```

`--fixtures --dry-run` together never spawn a `claude` subprocess and never send an
email; they only print what the routine would do.

## Lane 2: the box (systemd)

This lane is for running unattended on your own always-on machine. It is documented
here for reference; none of these steps are run as part of building this repo, and
none of them are performed by an agent working in this checkout.

1. Copy `deploy/*.service` and `deploy/*.timer` to `/etc/systemd/system/` (or to
   `~/.config/systemd/user/` for a user-level install).
2. `systemctl daemon-reload`.
3. `systemctl enable --now` each of the four timers:
   `fantasy-weekly.timer`, `fantasy-waiver.timer`, `fantasy-gameday.timer`, and
   `fantasy-inactive.timer`.
4. `systemctl list-timers` to confirm all four are scheduled.
5. `journalctl -u <unit> -n 100` to read the last run of any one unit.

Each of the four routines is one service/timer pair: `fantasy-weekly.service` runs
the Wednesday week-plan email, `fantasy-waiver.service` runs ahead of the league's
waiver deadline, `fantasy-gameday.service` runs daily and is silent when there is no
game, and `fantasy-inactive.service` polls every 5 minutes so the runner itself can
decide whether a kickoff window is open. There is also a templated
`deploy/fantasy-failure-alert@.service`, which each of the four reaches through
`OnFailure=fantasy-failure-alert@%n.service`. That alert fires only when the
wrapper itself was killed (it hit `TimeoutStartSec`) or its `ExecStartPre` failed,
that is, when systemd itself could not even get the wrapper running. It does not
fire for anything the wrapper catches and reports on its own; see "Known gaps"
below for why that matters.

### Three things the box needs that git will never deliver

- `config/league.yaml`. It is gitignored, so `ExecStartPre=git pull --rebase`
  updates the code only. Create this file by hand on the box.
- `~/.config/yahoo-fantasy-coach/secrets.env`, created by hand as described above.
- `~/.config/yahoo-fantasy-coach/.env` (and `curlrc` too, if your email backend is
  `smtp`), also created by hand before the first run.

`ExecStartPre=git pull --rebase` also needs a git remote already configured in the
box's own checkout; without one, that step fails and the failure-alert unit is what
tells you.

The box also needs the `claude` CLI to resolve under the exact `PATH` the units set:
`Environment=PATH=/home/jeff/.local/bin:/usr/local/bin:/usr/bin:/bin`.
`engine.run_common.run_claude` spawns a bare `claude` and does not catch
`FileNotFoundError`, so a live run with prose enabled crashes the unit outright if
`claude` is not somewhere on that PATH. Verify it resolves there, or set
`claude.binary` in `config/league.yaml` to an absolute path instead. This is one of
the few failures that does trip the `OnFailure` alert, since a crash here kills the
wrapper before it can report its own outcome.

## Lane 3: GitHub Actions (scheduled, no box)

This lane runs the four routines on a schedule without any always-on machine of
your own, using GitHub Actions. The four workflow files and their crons:

- `.github/workflows/weekly.yml`: `0 3 * * 4` (Wednesday 20:00 Pacific).
- `.github/workflows/waiver.yml`: `0 3 * * 3` (Tuesday 20:00 Pacific, ahead of
  waiver processing).
- `.github/workflows/gameday.yml`: `0 15 * * *` (08:00 Pacific daily).
- `.github/workflows/inactive.yml`: `*/15 * * * 0,1,4` (every 15 minutes on Sunday,
  Monday, and Thursday).

Repository secrets to create: `YAHOO_CLIENT_ID`, `YAHOO_CLIENT_SECRET`,
`YAHOO_REFRESH_TOKEN`, and `LEAGUE_YAML` (the entire contents of your own
`config/league.yaml`) are required by all four workflows. `BREVO_API_KEY` is needed
only if `email.backend` is `brevo`, and `CURLRC` (the entire contents of your curl
config file) only if `email.backend` is `smtp`.

Each workflow writes `config/league.yaml` and the credential files fresh at the
start of every run, from these secrets, before invoking the engine. `CURLRC` is
written only when that secret is non-empty: an empty curlrc file would still pass
`engine.notify`'s existence check and then fail inside curl itself, which is worse
than not writing the file at all. All four workflows trigger on `schedule` and
`workflow_dispatch` only, never on any pull request event, so a fork opening a PR
against this repo can never see these secrets. Every scheduled or dispatched run
passes `--prose plain`, because no `claude` CLI exists on a GitHub-hosted runner.

Be honest with yourself about precision here: GitHub Actions cron runs in UTC and
can fire 15 minutes or more late. That slack is fine for the weekly and waiver
runs, but it makes the inactive workflow's 75 minute pre-kickoff check imprecise in
this lane. The systemd box lane above is the precise one; use Actions for
convenience, not for a tight kickoff-window check.

If you are not using this lane at all, disable these four workflows in the
repository's Actions settings so they do not fail on a schedule with no secrets
configured.

## Known gaps

- Every wrapper returns 0 on every failure path it catches, so a green Actions job
  and a clean systemd unit both look identical to a fully successful run. The
  `STATUS` line at the end of the log is the only signal that tells the two apart:
  `STATUS emailed <routine>` is the only real success. `STATUS failed <routine>
  engine-error` and `STATUS failed <routine> email-send-failed` are the two silent
  failures, and systemd's `OnFailure=` will not fire for either one, since the
  wrapper exited cleanly from systemd's point of view.
- There is no current-week resolver yet. A live (non-fixtures) run of any of the
  four wrappers currently requires an explicit `--week NN`, or it raises an engine
  error and reports `engine-error` as above. Until a resolver exists, use the
  `workflow_dispatch` `week` input in the Actions lane, or add `--week` to the
  `ExecStart` line on the box. Do not trust an unattended live schedule to guess
  the week correctly; today it cannot guess at all.
- Yahoo Fantasy Sports API access is still pending approval, so the one-time
  interactive OAuth browser sign-in cannot happen until that clears. Everything
  offline, that is every command that passes `--fixtures`, works today regardless
  of that approval.
- `runs/cache/` has no eviction policy and no size cap, so it grows without bound
  on a long-lived box deploy. Prune it by hand periodically for now.
