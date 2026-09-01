"""Inactive run wrapper: alert 75 minutes before kickoff when a starter has
just been ruled out or landed on bye.

This is one of the four run wrappers described in docs/plan.md's "Run
wrapper contract" section, following the pattern proven in moonsail-social/
engine/blog_run.py: assemble a run's data, compose exactly one email for it
(Claude prose when available and it passes the gate, a fully deterministic
rendering otherwise), send or preview that one email, then print a machine
readable STATUS line as the very last thing this process writes to stdout.
The wrapper's exit code follows that STATUS line and nothing else: 0 when the
run did its job (emailed, dry-run, or a legitimate skip), 1 when it printed a
"failed" outcome, including when an engine.common.EngineError is caught
anywhere in the flow. That is what makes a scheduler's OnFailure alert able to
see a failure this process handled itself, not only one that killed it.

On a live (non-fixtures) run, the schedule is fetched through
engine.sources.schedule.fetch_week_schedule, honoring config's
sources.schedule toggle (engine.config.source_enabled). When that source
is unavailable, whether from a genuine fetch failure or because it is
turned off, this routine cannot compute a kickoff window at all, so it
does not guess: it prints "STATUS failed inactive schedule-unavailable"
instead of "STATUS skipped no-games" and sends no email.

Flow: engine.timing.starter_nfl_teams names the NFL teams the team's
current starters play for; engine.timing.next_kickoff finds the earliest
kickoff among those teams at or after --now (an ISO instant, defaulting to
the current UTC time). No such kickoff at all: "STATUS skipped no-games".
A kickoff exists but --now is not yet within --window-minutes (default
engine.timing.DEFAULT_INACTIVE_WINDOW_MINUTES, 75) of it:
"STATUS skipped not-yet". Inside the window, engine.brief.build_brief's
brief is written to disk (via engine.brief.write_brief, the same as every
other run wrapper, on both dry and real runs, since the JSON is the
diagnosable record whether or not an email actually goes out this time)
before the change set is computed: every player the team currently has
parked in a starting slot who is no longer engine.lineup.is_startable for
the week (a status in engine.lineup.EXCLUDED_STATUSES, or a bye week
matching the run week) is one change; its replacement, when the numbers
already call for one, comes from the matching build_brief lineup_changes
entry.

REPEAT SUPPRESSION. Each change's key is f"{player_id}:{status}". The keys
already reported for this exact kickoff window are read from
engine.timing.sent_path(season, week, window_key(kickoff), runs_root) via
read_sent, and only the set difference (the genuinely new changes) is ever
emailed. An empty difference, whether because nothing changed at all or
because everything in it was already reported, is "STATUS skipped
no-change" with no email sent. Otherwise the union of the old keys and the
newly reported keys is written back with write_sent so the same change is
never emailed twice for the same window.

THE .sent FILE IS WRITTEN ON DRY RUNS TOO. --dry-run only suppresses the
actual email send (engine.run_common.deliver's own dry-run preview); the
suppression file is written exactly the same either way, since without
that a dry run could never be used to prove the second-run-is-silent
behavior. To force a clean re-test of the window from scratch, delete the
relevant file under the runs root first, for example:
    rm runs/2026/wk03/inactive-*.sent

THE .sent FILE IS NEVER WRITTEN AFTER A FAILED REAL SEND. A dry run always
writes it (see above), but a real (non-dry-run) send that
engine.run_common.deliver reports as failed leaves the file untouched, old
keys and all: writing the new keys anyway would permanently suppress the
one alert this routine exists to deliver, with no way to tell a legitimate
"nothing changed" skip apart from "the email never actually went out" on
the next run. A transient failure is therefore retried in full the next
time this routine runs, rather than silently lost.

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
from engine.fixtures import (
    get_player,
    get_team,
    load_fixture_league,
    owner_team_id,
    starting_slot_units,
)
from engine.lineup import EXCLUDED_STATUSES, is_startable
from engine.live_league import build_live_league
from engine.sources.schedule import fetch_week_schedule
from engine.timing import (
    DEFAULT_INACTIVE_WINDOW_MINUTES,
    inside_window,
    load_fixture_schedule,
    next_kickoff,
    read_sent,
    sent_path,
    starter_nfl_teams,
    window_key,
    write_sent,
)

ROUTINE = "inactive"


def _inactive_changes(
    league: dict[str, Any], team_id: str, week: int, brief: dict[str, Any]
) -> list[dict[str, Any]]:
    """Return the INACTIVE_CHANGE_KEYS shaped changes for team_id/week.

    One entry per roster player currently parked in a starting slot
    (selected_slot named in engine.fixtures.starting_slot_units) who is no
    longer engine.lineup.is_startable for week: either his own status is
    one of EXCLUDED_STATUSES (that literal status is used, e.g. "O"), or
    his bye_week equals week (reported as the synthetic status "BYE", since
    the player's real status field is often blank in that case). A
    replacement, when brief["lineup_changes"] already calls for one for
    this exact player (matched on sit_player_id), supplies
    replacement_player_id, replacement_name and points_gained; otherwise
    those three are "", "" and 0.0.
    """
    team = get_team(league, team_id)
    starting_slots = {unit["slot"] for unit in starting_slot_units(league)}
    replacement_by_sit_id = {
        change["sit_player_id"]: change for change in brief["lineup_changes"]
    }

    changes: list[dict[str, Any]] = []
    for entry in team["roster"]:
        slot_name = entry["selected_slot"]
        if slot_name not in starting_slots:
            continue

        player_id = entry["player_id"]
        player = get_player(league, player_id)
        if is_startable(player, week):
            continue

        if player["status"] in EXCLUDED_STATUSES:
            status = player["status"]
            reason = f"Ruled out: status {status}"
        else:
            status = "BYE"
            reason = f"On bye (week {week})"

        replacement = replacement_by_sit_id.get(player_id)
        if replacement is not None:
            replacement_player_id = replacement["start_player_id"]
            replacement_name = replacement["start_name"]
            points_gained = replacement["points_gained"]
        else:
            replacement_player_id = ""
            replacement_name = ""
            points_gained = 0.0

        changes.append(
            {
                "player_id": player_id,
                "name": player["name"],
                "slot": slot_name,
                "status": status,
                "reason": reason,
                "replacement_player_id": replacement_player_id,
                "replacement_name": replacement_name,
                "points_gained": points_gained,
            }
        )

    return changes


def _change_key(change: dict[str, Any]) -> str:
    return f"{change['player_id']}:{change['status']}"


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Inactive run: alert inside the kickoff window when a starter "
            "has just been ruled out or landed on bye, emailing only the "
            "changes not already reported for that window."
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
        help=(
            "Directory to write the run's brief and .sent suppression file "
            "under (default: runs/ at the repo root)."
        ),
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
            "run; ESPN's own current week for a live run, see "
            "engine.run_common.resolve_week)."
        ),
    )
    parser.add_argument(
        "--now",
        dest="now",
        default=None,
        help="ISO instant to treat as the current time (default: now, UTC).",
    )
    parser.add_argument(
        "--window-minutes",
        dest="window_minutes",
        type=int,
        default=DEFAULT_INACTIVE_WINDOW_MINUTES,
        help=(
            f"Minutes before kickoff the window opens "
            f"(default: {DEFAULT_INACTIVE_WINDOW_MINUTES})."
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
    """Run the inactive routine once. Returns 0, or 1 on a failed outcome.

    On success prints, in order: nothing at all on the no-games, not-yet or
    no-change skip paths; otherwise the composed email (deliver's own
    dry-run preview, or nothing at all on a real send); and finally a
    STATUS line ("STATUS skipped no-games", "STATUS skipped not-yet",
    "STATUS skipped no-change", "STATUS dry-run inactive", "STATUS emailed
    inactive", "STATUS failed inactive email-send-failed" when a real send
    reports failure without raising, or "STATUS failed inactive
    schedule-unavailable" when the live schedule source could not be read
    or is disabled in config, since this routine cannot compute a kickoff
    window without it).

    An engine.common.EngineError raised anywhere in the flow (a bad
    config, an unresolvable week on a live run, a bad routine label, and
    so on) is caught here, reported to stderr as one line, and reported as
    "STATUS failed inactive <token>" instead of propagating, and this
    process then exits 1. <token> is a short, fixed, kebab-case slug of
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
            week = args.week if args.week is not None else league["current_week"]
        else:
            week = run_common.resolve_week(args.week, config)
            league = build_live_league(
                league_id=config["league"]["league_id"],
                season=config["league"]["season"],
                week=week,
                game_id=config["league"]["game_id"],
                sources_enabled={
                    name: source_enabled(config, name) for name in SOURCE_NAMES
                },
            )

        team_id = args.team or config["league"]["team_id"] or owner_team_id(league)

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
                # routine cannot decide anything about kickoff timing
                # without a schedule, so there is no safe way to tell that
                # apart from a real no-games day other than saying so.
                return run_common.print_status("failed", ROUTINE, "schedule-unavailable")
            schedule_data = schedule_result["data"]

        now_value = args.now if args.now is not None else datetime.now(timezone.utc)

        teams = starter_nfl_teams(league, team_id, week)
        kickoff = next_kickoff(schedule_data, now_value, teams)

        if kickoff is None:
            return run_common.print_status("skipped", "no-games")

        if not inside_window(kickoff, now_value, args.window_minutes):
            return run_common.print_status("skipped", "not-yet")

        brief: dict[str, Any] = brief_module.build_brief(league, team_id, week, ROUTINE)
        brief = run_common.apply_toss_up_margin(brief, toss_up_margin(config))

        runs_root = Path(args.runs_root) if args.runs_root else None
        brief_module.write_brief(brief, runs_root)

        changes = _inactive_changes(league, team_id, week, brief)

        path = sent_path(league["season"], week, window_key(kickoff), runs_root)
        already_sent = read_sent(path)

        changes_to_send = [
            change for change in changes if _change_key(change) not in already_sent
        ]

        if not changes_to_send:
            return run_common.print_status("skipped", "no-change")

        subject, body, _source = run_common.compose_email(
            ROUTINE,
            brief,
            config,
            inactive_changes=changes_to_send,
            prose=args.prose,
            fixtures=args.fixtures,
        )

        sent = run_common.deliver(subject, body, config, dry_run=args.dry_run)

        # Only record these keys as reported when a dry run previewed them
        # (see the module docstring's "written on dry runs too") or a real
        # send actually succeeded. A failed real send must not be written
        # here, or the alert it failed to deliver would never be retried.
        if args.dry_run or sent:
            new_keys = {_change_key(change) for change in changes_to_send}
            write_sent(path, already_sent | new_keys)

        if args.dry_run:
            return run_common.print_status("dry-run", ROUTINE)
        elif sent:
            return run_common.print_status("emailed", ROUTINE)
        else:
            return run_common.print_status("failed", ROUTINE, "email-send-failed")

    except EngineError as error:
        print(f"engine.inactive_run: {error}", file=sys.stderr)
        return run_common.print_status("failed", ROUTINE, run_common.error_status_token(error))


if __name__ == "__main__":
    raise SystemExit(main())
