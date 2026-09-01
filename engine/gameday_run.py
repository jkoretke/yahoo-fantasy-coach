"""Gameday run wrapper: email the self-contained lineup on any day one of
the team's starters actually plays.

This is one of the four run wrappers described in docs/plan.md's "Run
wrapper contract" section, following the pattern proven in moonsail-social/
engine/blog_run.py: assemble a run's data, compose exactly one email for it
(Claude prose when available and it passes the gate, a fully deterministic
rendering otherwise), send or preview that one email, then print a machine
readable STATUS line as the very last thing this process writes to stdout.
The wrapper always exits 0, including when an engine.common.EngineError is
raised anywhere in the flow, so a scheduler's OnFailure alert only fires if
this process itself is killed.

The runner, not a fixed weekday list, decides whether today matters:
engine.timing.starter_nfl_teams names the NFL teams the team's current
starters play for, and engine.timing.games_on_date checks whether any of
those teams kick off on --date (a UTC calendar date, defaulting to today's
UTC date). No game on that date means no email at all: "STATUS skipped
no-games" is printed and this process returns 0 having built no brief and
sent nothing.

On a live (non-fixtures) run, the schedule is fetched through
engine.sources.schedule.fetch_week_schedule, honoring config's
sources.schedule toggle (engine.config.source_enabled). When that source
is unavailable, whether from a genuine fetch failure or because it is
turned off, this routine cannot tell a real no-games day apart from "the
schedule could not be read", so it does not guess: it prints "STATUS
failed gameday schedule-unavailable" instead of "STATUS skipped no-games"
and sends no email.

The gameday email is deliberately NOT a diff against a prior lineup: it
carries the full recommended lineup from engine.brief.build_brief's
optimal_lineup, exactly as engine.email_render.render_plain_email("gameday",
...) renders it, so it reads correctly alone on a phone with lineups about
to lock at kickoff.

team_id and week are resolved once, explicitly, right after the league
dict is built, and that same team_id and week are passed to every
downstream call, matching engine.weekly_run's own resolution order.

engine.brief.build_brief is reached through its module, not imported by
name, so a test can monkeypatch engine.brief.build_brief in place and have
this wrapper's own call pick it up, the same seam engine.live_league
already relies on for its own Yahoo/Sleeper/ESPN calls.

Public names: main.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from engine import brief as brief_module
from engine import run_common
from engine.common import EngineError
from engine.config import SOURCE_NAMES, load_league_config, source_enabled, toss_up_margin
from engine.fixtures import load_fixture_league, owner_team_id
from engine.live_league import build_live_league
from engine.sources.schedule import fetch_week_schedule
from engine.timing import games_on_date, load_fixture_schedule, starter_nfl_teams

ROUTINE = "gameday"


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Gameday run: email the full recommended lineup on any day one "
            "of the team's current starters actually plays, or skip "
            "silently when none do."
        )
    )
    parser.add_argument(
        "--fixtures",
        action="store_true",
        help="Load the shipped sample fixture league instead of a live one.",
    )
    parser.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        help="Print what would be sent instead of sending a real email.",
    )
    parser.add_argument(
        "--fixture-dir",
        dest="fixture_dir",
        default=None,
        help=(
            "Fixture directory to load (default: the shipped sample league). "
            "Only read when --fixtures is given."
        ),
    )
    parser.add_argument(
        "--config",
        dest="config",
        default=None,
        help=(
            "League config yaml path (default: config/league.yaml, falling "
            "back to config/league.example.yaml)."
        ),
    )
    parser.add_argument(
        "--runs-root",
        dest="runs_root",
        default=None,
        help="Directory to write the run's brief under (default: runs/ at the repo root).",
    )
    parser.add_argument(
        "--team",
        dest="team",
        default=None,
        help="Team id to build the brief for (default: the owner team).",
    )
    parser.add_argument(
        "--week",
        dest="week",
        type=int,
        default=None,
        help=(
            "Week number (default: the league's current week for a fixtures "
            "run; required for a live run)."
        ),
    )
    parser.add_argument(
        "--date",
        dest="date",
        default=None,
        help=(
            "UTC calendar date to check for games, as YYYY-MM-DD "
            "(default: today's UTC date)."
        ),
    )
    parser.add_argument(
        "--schedule",
        dest="schedule",
        default=None,
        help=(
            "Path to a schedule JSON file, overriding the default source "
            "(engine.timing.FIXTURE_SCHEDULE_PATH under --fixtures, a live "
            "fetch of the current week's schedule otherwise)."
        ),
    )
    parser.add_argument(
        "--prose",
        dest="prose",
        choices=run_common.PROSE_MODES,
        default="auto",
        help="Email prose mode: auto, claude or plain (default: auto).",
    )
    parser.add_argument(
        "--claude-bin",
        dest="claude_bin",
        default=None,
        help="Override the claude binary path read from config.",
    )
    parser.add_argument(
        "--timeout",
        dest="timeout",
        type=int,
        default=None,
        help="Override claude's timeout in seconds read from config.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the gameday routine once. Always returns 0.

    On success prints, in order: nothing at all on the no-games skip path;
    otherwise the composed email (deliver's own dry-run preview, or nothing
    at all on a real send); and finally a STATUS line ("STATUS skipped
    no-games", "STATUS dry-run gameday", "STATUS emailed gameday",
    "STATUS failed gameday email-send-failed" when a real send reports
    failure without raising, or "STATUS failed gameday schedule-unavailable"
    when the live schedule source could not be read or is disabled in
    config, since this routine cannot decide whether today matters without
    it).

    An engine.common.EngineError raised anywhere in the flow (a bad
    config, an unresolvable week on a live run, a bad routine label, and
    so on) is caught here, reported to stderr as one line, and reported as
    "STATUS failed gameday <token>" instead of propagating, so this
    process always exits 0. <token> is a short, fixed, kebab-case slug of
    the error's class name (engine.run_common.error_status_token, e.g.
    "engine-error"), never the free-text message itself: the full message
    is still on the stderr line printed immediately before it.
    """
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    try:
        config = load_league_config(Path(args.config) if args.config else None)
        if args.claude_bin is not None:
            config["claude"]["binary"] = args.claude_bin
        if args.timeout is not None:
            config["claude"]["timeout_seconds"] = args.timeout

        if args.fixtures:
            fixture_dir = Path(args.fixture_dir) if args.fixture_dir else None
            league = load_fixture_league(fixture_dir)
        else:
            if args.week is None:
                raise EngineError(
                    "--week is required for a live (non-fixtures) run: the "
                    "current week cannot be known before the live league is "
                    "fetched, and fetching the live league itself needs a "
                    "week to fetch."
                )
            league = build_live_league(
                league_id=config["league"]["league_id"],
                season=config["league"]["season"],
                week=args.week,
                game_id=config["league"]["game_id"],
                sources_enabled={
                    name: source_enabled(config, name) for name in SOURCE_NAMES
                },
            )

        team_id = args.team or config["league"]["team_id"] or owner_team_id(league)
        week = args.week if args.week is not None else league["current_week"]

        if args.schedule is not None:
            schedule_data = load_fixture_schedule(Path(args.schedule))
        elif args.fixtures:
            schedule_data = load_fixture_schedule()
        else:
            schedule_result = fetch_week_schedule(
                league["season"], week, enabled=source_enabled(config, "schedule")
            )
            if not schedule_result["available"]:
                # A genuine fetch failure and a deliberate sources.schedule:
                # false both land here with the same distinct status: this
                # routine cannot decide whether today matters without a
                # schedule, so there is no safe way to tell that apart from
                # a real no-games day other than saying so.
                run_common.print_status("failed", ROUTINE, "schedule-unavailable")
                return 0
            schedule_data = schedule_result["data"]

        target_date = args.date if args.date is not None else datetime.now(timezone.utc).date()

        teams = starter_nfl_teams(league, team_id, week)
        games = games_on_date(schedule_data, target_date, teams)

        if not games:
            run_common.print_status("skipped", "no-games")
            return 0

        brief: dict[str, Any] = brief_module.build_brief(league, team_id, week, ROUTINE)
        brief = run_common.apply_toss_up_margin(brief, toss_up_margin(config))

        runs_root = Path(args.runs_root) if args.runs_root else None
        brief_module.write_brief(brief, runs_root)

        subject, body, _source = run_common.compose_email(
            ROUTINE,
            brief,
            config,
            prose=args.prose,
            fixtures=args.fixtures,
        )

        sent = run_common.deliver(subject, body, config, dry_run=args.dry_run)

        if args.dry_run:
            run_common.print_status("dry-run", ROUTINE)
        elif sent:
            run_common.print_status("emailed", ROUTINE)
        else:
            run_common.print_status("failed", ROUTINE, "email-send-failed")

    except EngineError as error:
        print(f"engine.gameday_run: {error}", file=sys.stderr)
        run_common.print_status("failed", ROUTINE, run_common.error_status_token(error))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
