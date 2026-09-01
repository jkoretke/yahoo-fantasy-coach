"""Waiver run wrapper: build the week's ranked waiver claims, email them.

This is one of the four run wrappers described in docs/plan.md's "Run
wrapper contract" section, following the pattern proven in moonsail-social/
engine/blog_run.py: assemble a run's data, compose exactly one email for
it (Claude prose when available and it passes the gate, a fully
deterministic rendering otherwise), send or preview that one email, then
print a machine readable STATUS line as the very last thing this process
writes to stdout. The wrapper's exit code follows that STATUS line and
nothing else: 0 when the run did its job (emailed, dry-run, or a legitimate
skip), 1 when it printed a "failed" outcome, including when an
engine.common.EngineError is caught anywhere in the flow. That is what makes
a scheduler's OnFailure alert able to see a failure this process handled
itself, not only one that killed it.

Unlike engine.weekly_run, this routine carries no trade ideas section:
waiver day is about this week's free agent pool, not roster-to-roster
trades.

team_id and week are resolved once, explicitly, right after the league
dict is built, and that same team_id and week are passed to
engine.brief.build_brief, exactly mirroring engine.weekly_run's own
resolution so both wrappers behave identically on the same inputs.

engine.brief.build_brief is reached through its module, not imported by
name, so a test can monkeypatch engine.brief.build_brief in place and
have this wrapper's own call pick up the patched version, the same seam
engine.live_league already relies on for its own Yahoo/Sleeper/ESPN calls.

Public names: main.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from engine import brief as brief_module
from engine import run_common
from engine.common import EngineError
from engine.config import SOURCE_NAMES, load_league_config, source_enabled, toss_up_margin
from engine.fixtures import load_fixture_league, owner_team_id
from engine.live_league import build_live_league

ROUTINE = "waiver"


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Waiver run: build this week's ranked waiver claims for the "
            "league's own waiver type (faab or priority), then email or "
            "preview the result."
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
            "run; ESPN's own current week for a live run, see "
            "engine.run_common.resolve_week)."
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
    """Run the waiver routine once. Returns 0, or 1 on a failed outcome.

    On success prints, in order: the composed email (deliver's own
    dry-run preview, or nothing at all on a real send), and finally a
    STATUS line ("STATUS dry-run waiver", "STATUS emailed waiver", or
    "STATUS failed waiver email-send-failed" when a real send reports
    failure without raising).

    An engine.common.EngineError raised anywhere in the flow (a bad
    config, an unresolvable week on a live run, a bad routine label, and
    so on) is caught here, reported to stderr as one line, and reported
    as "STATUS failed waiver <token>" instead of propagating, and this
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
            league = load_fixture_league(fixture_dir, waiver_type=args.waiver_type)
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
            return run_common.print_status("dry-run", ROUTINE)
        elif sent:
            return run_common.print_status("emailed", ROUTINE)
        else:
            return run_common.print_status("failed", ROUTINE, "email-send-failed")

    except EngineError as error:
        print(f"engine.waiver_run: {error}", file=sys.stderr)
        return run_common.print_status("failed", ROUTINE, run_common.error_status_token(error))


if __name__ == "__main__":
    raise SystemExit(main())
