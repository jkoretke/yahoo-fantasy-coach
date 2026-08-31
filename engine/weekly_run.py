"""Weekly run wrapper: build the week's brief, compose its email, send it.

This is one of the four run wrappers described in docs/plan.md's "Run
wrapper contract" section, following the pattern proven in moonsail-social/
engine/blog_run.py: assemble a run's data, compose exactly one email for
it (Claude prose when available and it passes the gate, a fully
deterministic rendering otherwise), send or preview that one email, then
print a machine readable STATUS line as the very last thing this process
writes to stdout. The wrapper always exits 0, including when an
engine.common.EngineError is raised anywhere in the flow, so a scheduler's
OnFailure alert only fires if this process itself is killed.

The weekly routine is the only one of the four that also computes
engine.trades.trade_ideas and folds it into the email, since a week-level
plan is the natural place to also flag a positional surplus worth trading
away.

team_id and week are resolved once, explicitly, right after the league
dict is built, and that same team_id and week are passed to every
downstream call (engine.brief.build_brief and engine.trades.trade_ideas
alike). Neither is left to build_brief's own defaults, because trade_ideas
needs both and must see the same values build_brief used.

engine.brief.build_brief and engine.trades.trade_ideas are reached through
their modules, not imported by name, so a test can monkeypatch
engine.brief.build_brief (or engine.trades.trade_ideas) in place and have
this wrapper's own call pick up the patched version, the same seam
engine.live_league already relies on for its own Yahoo/Sleeper/ESPN calls.

Public names: main.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from engine import brief as brief_module
from engine import run_common
from engine import trades as trades_module
from engine.common import EngineError
from engine.config import SOURCE_NAMES, load_league_config, source_enabled, toss_up_margin
from engine.fixtures import load_fixture_league, owner_team_id
from engine.live_league import build_live_league

ROUTINE = "weekly"


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Weekly run: build one week's optimal lineup, start/sit calls, "
            "matchup projection, waiver targets and trade ideas, then email "
            "or preview the result."
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
        "--waiver-type",
        dest="waiver_type",
        default=None,
        help=(
            "Override the fixture's waiver type (faab or priority). Only "
            "read when --fixtures is given."
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
    """Run the weekly routine once. Always returns 0.

    On success prints, in order: the brief JSON (only with --dry-run), the
    composed email (deliver's own dry-run preview, or nothing at all on a
    real send), and finally a STATUS line ("STATUS dry-run weekly",
    "STATUS emailed weekly", or "STATUS failed weekly email-send-failed"
    when a real send reports failure without raising).

    An engine.common.EngineError raised anywhere in the flow (a bad
    config, an unresolvable week on a live run, a bad routine label, and
    so on) is caught here, reported to stderr as one line, and reported
    as "STATUS failed weekly <reason>" instead of propagating, so this
    process always exits 0.
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
            league = load_fixture_league(fixture_dir, waiver_type=args.waiver_type)
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

        team_id = args.team or owner_team_id(league)
        week = args.week if args.week is not None else league["current_week"]

        brief: dict[str, Any] = brief_module.build_brief(league, team_id, week, ROUTINE)
        brief = run_common.apply_toss_up_margin(brief, toss_up_margin(config))

        runs_root = Path(args.runs_root) if args.runs_root else None
        brief_module.write_brief(brief, runs_root)

        trade_ideas_result = trades_module.trade_ideas(league, team_id, week)

        if args.dry_run:
            print(json.dumps(brief, indent=2))

        subject, body, _source = run_common.compose_email(
            ROUTINE,
            brief,
            config,
            trades=trade_ideas_result,
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
        print(f"engine.weekly_run: {error}", file=sys.stderr)
        run_common.print_status("failed", ROUTINE, str(error))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
