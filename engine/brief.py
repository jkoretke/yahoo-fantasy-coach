"""Assemble one run's brief JSON, and the offline demo entry point.

A brief is the single artifact a routine run produces: one league's optimal
lineup, current lineup, the start/sit deltas between them, the week's
matchup projection, and the ranked waiver targets, all scored from one
shared projected points map (engine.scoring.projected_points_by_player)
computed exactly once and passed into every other module. That is what
makes every number in one brief consistent with every other number in it,
rather than each section quietly rebuilding its own scoring pass.

ROUTINES is a label carried on the artifact only in this phase. The four
run wrappers that actually schedule a "weekly" run versus a "gameday" run
and so on are later phase work and are not started here.

The shipped fixture (fixtures/sample_league/) only has full projections
for weeks 3 and 4, so those are the two weeks this module's demo path can
actually build a brief for. --week exists here for when a later phase
points this same code at a real league with a full season of projections;
against the sample fixture, pass 3 (the default) or 4.

Public names: ROUTINES, build_brief, brief_path, write_brief, main.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from engine.common import EngineError, REPO_ROOT, round_points, timestamp, write_json
from engine.fixtures import DEFAULT_FIXTURE_DIR, get_team, load_fixture_league, owner_team_id
from engine.lineup import current_lineup, lineup_changes, optimal_lineup
from engine.matchup import matchup_projection
from engine.scoring import projected_points_by_player
from engine.waivers import rank_waiver_targets

# Labels a run wrapper (a later phase) will use to select which brief to
# build. Only "weekly" is exercised by this phase's demo command; the other
# three are reserved names so downstream code can rely on them existing.
ROUTINES = ("weekly", "gameday", "waiver", "inactive")


def build_brief(
    league: dict[str, Any],
    team_id: str | None = None,
    week: int | None = None,
    routine: str = "weekly",
) -> dict[str, Any]:
    """Assemble one run's brief for team_id/week from league.

    team_id defaults to the fixture's owner team
    (engine.fixtures.owner_team_id); week defaults to
    league["current_week"]. routine must be one of ROUTINES, or this
    raises EngineError; it is recorded on the artifact as a label only,
    Phase 1 does not branch its behavior on it.

    projected_points_by_player(league, week) is computed exactly once and
    passed into optimal_lineup, current_lineup and matchup_projection, so
    every point total in the returned brief comes from that one scoring
    pass.

    Returns a plain JSON serializable dict: no set, tuple, Path or
    datetime appears anywhere in it, since this is written straight to
    disk as the run artifact.
    """
    if routine not in ROUTINES:
        raise EngineError(
            f"invalid routine: {routine!r} (must be one of {ROUTINES})"
        )

    if team_id is None:
        team_id = owner_team_id(league)
    if week is None:
        week = league["current_week"]

    points = projected_points_by_player(league, week)

    optimal = optimal_lineup(league, team_id, week, points=points)
    current = current_lineup(league, team_id, week, points=points)
    changes = lineup_changes(current, optimal)
    matchup = matchup_projection(league, team_id, week, points=points)
    waivers = rank_waiver_targets(league, team_id, week)

    points_left_on_bench = round_points(optimal["total_points"] - current["total_points"])

    team = get_team(league, team_id)

    return {
        "generated_at": timestamp(),
        "routine": routine,
        "league": {
            "league_id": league["league_id"],
            "name": league["name"],
            "season": league["season"],
            "num_teams": league["num_teams"],
            "waiver_type": league["settings"]["waiver"]["type"],
        },
        "week": week,
        "team": {
            "team_id": team["team_id"],
            "name": team["name"],
            "manager": team["manager"],
        },
        "optimal_lineup": optimal,
        "current_lineup": current,
        "lineup_changes": changes,
        "points_left_on_bench": points_left_on_bench,
        "matchup": matchup,
        "waivers": waivers,
    }


def brief_path(brief: dict[str, Any], runs_root: Path | None = None) -> Path:
    """Return the path a brief should be written to under runs_root.

    runs_root defaults to engine.common.REPO_ROOT / "runs". The path is
    runs_root/<season>/wk<week zero padded to 2 digits>/<routine>-<safe
    timestamp>.json, where the safe timestamp is brief["generated_at"]
    with every ":" replaced by "-", since a colon is not a legal character
    in a Windows file name and every other platform accepts the
    replacement just as well.
    """
    if runs_root is None:
        runs_root = REPO_ROOT / "runs"

    season = brief["league"]["season"]
    week = brief["week"]
    routine = brief["routine"]
    safe_timestamp = brief["generated_at"].replace(":", "-")

    return runs_root / str(season) / ("wk%02d" % week) / f"{routine}-{safe_timestamp}.json"


def write_brief(brief: dict[str, Any], runs_root: Path | None = None) -> Path:
    """Write brief as JSON under brief_path(brief, runs_root) and return that path.

    Parent directories are created as part of engine.common.write_json's
    atomic write; callers never need to mkdir first.
    """
    path = brief_path(brief, runs_root)
    write_json(path, brief)
    return path


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Offline demo: build one run brief from the sample fixture league, "
            "with no credentials and no network access."
        )
    )
    parser.add_argument(
        "--fixtures",
        default=None,
        help="Fixture directory to load (default: the shipped sample league).",
    )
    parser.add_argument(
        "--team",
        dest="team_id",
        default=None,
        help="Team id to build the brief for (default: the owner team).",
    )
    parser.add_argument(
        "--week",
        type=int,
        default=None,
        help=(
            "Week number to build the brief for (default: the league's current week). "
            "The shipped fixture supports weeks 3 and 4; other values need a real league, "
            "which is a later phase."
        ),
    )
    parser.add_argument(
        "--routine",
        default="weekly",
        help=(
            "Routine label to record on the brief (default: weekly). Any value is "
            "accepted here; an unrecognized one is reported as a single error line "
            "and exit code 1, not an argument parsing failure."
        ),
    )
    parser.add_argument(
        "--waiver-type",
        dest="waiver_type",
        default=None,
        choices=("faab", "priority"),
        help="Override the fixture's waiver type, to demo either branch.",
    )
    parser.add_argument(
        "--runs-root",
        dest="runs_root",
        default=None,
        help="Directory to write the brief under with --write (default: runs/ at the repo root).",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="Write the brief to disk under --runs-root and print only its path, instead of printing the brief itself.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Offline demo entry point: build (and optionally write) one run brief.

    Prints json.dumps(brief, indent=2) to stdout by default, or, with
    --write, writes the brief under --runs-root and prints only the
    resulting path. Sends no email, spawns no subprocess, and makes no
    network call; an EngineError is caught here and reported to stderr as
    one line, with exit code 1 and no traceback.
    """
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    try:
        fixture_dir = Path(args.fixtures) if args.fixtures is not None else DEFAULT_FIXTURE_DIR
        league = load_fixture_league(fixture_dir, waiver_type=args.waiver_type)

        brief = build_brief(
            league,
            team_id=args.team_id,
            week=args.week,
            routine=args.routine,
        )

        if args.write:
            runs_root = Path(args.runs_root) if args.runs_root is not None else None
            path = write_brief(brief, runs_root)
            print(path)
        else:
            print(json.dumps(brief, indent=2))
    except EngineError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
