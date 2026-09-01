# yahoo-fantasy-coach

Reads your Yahoo Fantasy Football league (read only, the API grants no write access),
computes the week's optimal lineup, start/sit calls, matchup projection, waiver targets,
and trade ideas, then hands you the decisions. You tap the moves into the Yahoo app
yourself. Python computes every number and every verdict; Claude only writes the prose,
checked against the JSON Python produced.

## Run it with Claude Code (start here)

This is the lead way to use this project, and it needs no development background:

1. Clone the repo.
2. One time, install dependencies into a virtual environment:
   ```
   python3 -m venv .venv
   .venv/bin/python3 -m pip install -r requirements.txt
   ```
3. Open the folder in Claude Code and just ask it plainly, for example "do my fantasy
   analysis" or "what should I do about waivers this week".
4. Claude Code runs the routine for you and the recommendation comes back as chat
   output. There is no cron to configure, no GitHub Actions secret, no email setup,
   and no always-on machine.

Under the hood, Claude Code runs one of these four commands. Each one works right now,
with zero credentials, against the checked-in sample league:

```
.venv/bin/python3 -m engine.weekly_run --fixtures --dry-run
.venv/bin/python3 -m engine.gameday_run --fixtures --date 2026-09-14 --dry-run
.venv/bin/python3 -m engine.waiver_run --fixtures --dry-run
.venv/bin/python3 -m engine.inactive_run --fixtures --now 2026-09-14T11:45Z --dry-run
```

| Routine | What it does |
|---|---|
| `weekly_run` | the full week plan: lineup, start/sit, matchup projection, waivers, trades |
| `gameday_run` | a self-contained current lineup check for any day you have a game |
| `waiver_run` | ranked waiver claims ahead of your league's waiver deadline |
| `inactive_run` | a scratch caught 75 minutes before kickoff |

The only setup you cannot skip for your own real league is the one-time Yahoo sign-in
in the next section, and Claude Code can walk you through it two clicks at a time. Be
honest with yourself about timing: Yahoo's Fantasy Sports API access is reviewed by a
person and takes time to clear. Until yours does, the `--fixtures` demo above is what
runs.

See `docs/setup.md` for the full install and configuration reference.

## Yahoo API setup (do this first, one time)

Yahoo's Fantasy Sports API requires two separate steps: creating a developer app, then applying
for Fantasy Sports data access on top of it. As of 2026-08-30, Yahoo no longer offers a "Fantasy
Sports" checkbox during app creation. This is a real, two-step process now, not a formality.

### 1. Create a Yahoo developer app

1. Go to [developer.yahoo.com/apps/create](https://developer.yahoo.com/apps/create/) and sign in
   with the Yahoo account tied to your fantasy league.
2. Fill out the form:
   - **Application Name**: anything you want, but it cannot contain the word "yahoo" (the form
     rejects it).
   - **Redirect URI(s)**: `https://localhost:8080`
   - **OAuth Client Type**: Confidential Client (this app needs a client secret, unlike a mobile
     or single-page app).
   - **API Permissions**: leave both checkboxes (OpenID Connect Permissions, TW Auction)
     unchecked. Neither applies here, and Fantasy Sports is not listed. That is expected, not a
     bug: Fantasy Sports access comes from the separate application in step 2 below.
3. Click **Create App**. You'll land on a page showing a **Client ID** and a **Client Secret**.
   You only need those two values. The **App ID** shown on the same page is a dashboard
   identifier and is not used anywhere in this project.
4. Save the two values somewhere only you can read:
   ```
   mkdir -p ~/.config/yahoo-fantasy-coach
   chmod 700 ~/.config/yahoo-fantasy-coach
   touch ~/.config/yahoo-fantasy-coach/secrets.env
   chmod 600 ~/.config/yahoo-fantasy-coach/secrets.env
   ```
   Then open `~/.config/yahoo-fantasy-coach/secrets.env` in a text editor and add:
   ```
   YAHOO_CLIENT_ID=<your Client ID>
   YAHOO_CLIENT_SECRET=<your Client Secret>
   ```
   Never commit this file or paste these values anywhere public.

### 2. Apply for Yahoo Fantasy Sports API access

Having a developer app is not enough on its own: every API call returns a 401 until Yahoo
approves your app for Fantasy Sports data specifically. This is reviewed by a person at Yahoo,
so there is no published turnaround time.

1. Go to [sports.yahoo.com/developer/access](https://sports.yahoo.com/developer/access/).
2. Fill out the application. A few fields worth knowing before you start:
   - **Business Title**: if you're doing this as an individual rather than through a company,
     something like "Independent Developer" is fine.
   - **Business Name & Address**: your own name and address is fine for an individual,
     personal-use application; you do not need to represent a company.
   - **Consumer-Facing Product or App Name**: a plain, human name for the project (for example
     "Yahoo Fantasy Coach"), not the repo's technical name.
   - **Website URL or App Store Details**: this field requires an actual, real URL. If you have
     nothing else live yet, your GitHub profile URL (`https://github.com/<your-username>`) is a
     real, honest answer.
   - **Client ID**: paste the Client ID from step 1. The form explicitly allows leaving this
     blank if you have not created a developer app yet, but if you already have, fill it in so
     the approval attaches to the right app.
   - **Describe Your Intended Use Case**: be specific and honest that this is read-only,
     personal or single-league use, computed by your own code, with any actual roster moves
     made by hand in the Yahoo app (the API grants no write access). Applications that are vague
     get closed without a response, per Yahoo's own note on the form.
   - **Expected Users**: pick the smallest option if this is just for your own league.
3. Submit. You'll see an on-page confirmation that the application was received. There is no
   further action on your end until Yahoo responds.

Nothing that touches Yahoo (`engine/yahoo_client.py` and anything built on it) can be used for
real until this application is approved. See `docs/setup.md` for what runs in the meantime.

## Optional: get it emailed on a schedule

Both of these are an upgrade for people who want the recommendation pushed to them
automatically instead of asking each week. Neither is required to use this project.

### A box you control (systemd)

`deploy/` holds a systemd service and timer for each of the four routines, plus a
templated `fantasy-failure-alert@.service` that catches the case where a wrapper
never even started. See `docs/setup.md` for the install steps.

### GitHub Actions (no box needed)

`.github/workflows/` holds the four scheduled workflows (`weekly.yml`, `waiver.yml`,
`gameday.yml`, `inactive.yml`), each on its own cron. Secrets go in the repository's
own secrets, never in the repo itself. See `docs/setup.md`.

Be honest about precision here: GitHub Actions cron can fire 15 minutes or more late.
That is fine for the weekly and waiver runs, but it makes the 75 minute pre-kickoff
check in the inactive workflow imprecise in this lane.

A run that failed exits non-zero in both lanes, so a red job or a failed systemd
unit is a real failure. A legitimate skip (no games today, not yet inside the
kickoff window) exits 0. The `STATUS` line at the end of the log says which of the
two it was. See docs/setup.md's Known gaps section.

## Configuration

Copy `config/league.example.yaml` to `config/league.yaml` and edit it for your own
league. `config/league.yaml` is gitignored and is never committed. Real secrets live
only in `~/.config/yahoo-fantasy-coach/secrets.env`, never in the repo. See
`docs/setup.md` for every key.

## Development

```
python3 -m venv .venv
.venv/bin/python3 -m pip install -r requirements-dev.txt
.venv/bin/python3 -m pytest -q
.venv/bin/python3 -m ruff check .
```

Tests run fully offline, with no credentials, because Yahoo Fantasy Sports API access
is granted per person and is approval gated: `fixtures/` is the entire development
path for this project. See `CONTRIBUTING.md` for the full guide.

## Contributing, security and license

See `CONTRIBUTING.md` for how to propose a change, `CODE_OF_CONDUCT.md` for how we
treat each other, and `SECURITY.md` to report a security issue. Every pull request
needs the owner's review, enforced by `CODEOWNERS`. Licensed under the MIT license,
see `LICENSE`.

## Attribution

Fantasy data provided by Yahoo Fantasy

Additional data comes from Sleeper, ESPN's public endpoints, and Open-Meteo.

The weekly routine also runs a news pass: Claude searches the web for late breaking
news about the players your brief already names, and reports only what it read.
Python still computes every number and every verdict; the news is context, not a
decision. It is the one data source that costs anything to run, so only the weekly
routine asks it, once per run, and never on a `--fixtures` run. Turn it off with
`sources.news: false` in `config/league.yaml`.
