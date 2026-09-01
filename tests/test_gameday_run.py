"""Tests for engine.gameday_run: the every-morning "does today matter"
wrapper.

Every test drives main() directly with --fixtures --dry-run, so nothing
here reaches a real claude subprocess, a real network call, or a real
email send. engine.run_common.run_claude and engine.notify.send_email are
both monkeypatched to fail the test outright if called, since a --fixtures
--dry-run run resolves prose to "plain" and delivery to a printed preview
and must never reach either.
"""
from __future__ import annotations

import pytest

import engine.gameday_run as gameday_run
import engine.notify as notify
import engine.run_common as run_common
from engine.brief import build_brief
from engine.fixtures import load_fixture_league
from engine.gameday_run import main
from engine.sources.base import disabled_result, unavailable_result
from engine.timing import load_fixture_schedule


def _must_not_run_claude(*args, **kwargs):
    raise AssertionError("engine.run_common.run_claude must never be called by this test")


def _must_not_send_email(*args, **kwargs):
    raise AssertionError("engine.notify.send_email must never be called by this test")


@pytest.fixture(autouse=True)
def _block_claude_and_email(monkeypatch):
    monkeypatch.setattr(run_common, "run_claude", _must_not_run_claude)
    monkeypatch.setattr(notify, "send_email", _must_not_send_email)


def test_no_games_on_date_skips_silently_and_exits_zero(tmp_path, capsys):
    exit_code = main(
        [
            "--fixtures",
            "--dry-run",
            "--runs-root",
            str(tmp_path),
            "--date",
            "2026-09-13",
        ]
    )

    assert exit_code == 0
    out = capsys.readouterr().out
    lines = [line for line in out.splitlines() if line.strip()]
    assert lines[-1] == "STATUS skipped no-games"
    assert "[dry-run] would send:" not in out
    assert "Subject:" not in out


def test_game_on_date_prints_full_self_contained_lineup(tmp_path, capsys):
    exit_code = main(
        [
            "--fixtures",
            "--dry-run",
            "--runs-root",
            str(tmp_path),
            "--date",
            "2026-09-14",
        ]
    )

    assert exit_code == 0
    out = capsys.readouterr().out
    lines = [line for line in out.splitlines() if line.strip()]
    assert lines[-1] == "STATUS dry-run gameday"
    assert "[dry-run] would send:" in out

    league = load_fixture_league()
    brief = build_brief(league)
    for assignment in brief["optimal_lineup"]["assignments"]:
        if assignment["name"] is not None:
            assert assignment["name"] in out


def test_live_schedule_fetch_failure_reports_failed_not_no_games(tmp_path, monkeypatch, capsys):
    # A genuinely unavailable schedule must never read as a real no-games
    # day: that would silently skip the email and look like a legitimate
    # outcome. build_live_league is monkeypatched to the fixture league
    # (this is a wrapper-level test of the schedule check, not of live
    # league assembly, which Phase 4's ground truth keeps out of scope
    # here), so the only thing under test is the schedule envelope check.
    fixture_league = load_fixture_league()
    monkeypatch.setattr(gameday_run, "build_live_league", lambda **kwargs: fixture_league)
    monkeypatch.setattr(
        gameday_run,
        "fetch_week_schedule",
        lambda *args, **kwargs: unavailable_result("schedule", "espn unreachable"),
    )

    exit_code = main(["--week", "3", "--dry-run", "--runs-root", str(tmp_path)])

    assert exit_code == 1
    out = capsys.readouterr().out
    lines = [line for line in out.splitlines() if line.strip()]
    assert lines[-1] == "STATUS failed gameday schedule-unavailable"
    assert "[dry-run] would send:" not in out


def test_live_schedule_disabled_in_config_also_reports_failed(tmp_path, monkeypatch, capsys):
    # sources.schedule: false must actually reach fetch_week_schedule's
    # own enabled= kwarg, and a disabled schedule is handled the same as
    # a genuinely unavailable one: this routine cannot decide anything
    # about kickoff timing without it either way.
    fixture_league = load_fixture_league()
    monkeypatch.setattr(gameday_run, "build_live_league", lambda **kwargs: fixture_league)

    seen = {}

    def _fetch(season, week, *, enabled=True, **kwargs):
        seen["enabled"] = enabled
        return disabled_result("schedule") if not enabled else unavailable_result("schedule", "x")

    monkeypatch.setattr(gameday_run, "fetch_week_schedule", _fetch)

    config_path = tmp_path / "league.yaml"
    config_path.write_text("sources:\n  schedule: false\n", encoding="utf-8")

    exit_code = main(
        ["--week", "3", "--dry-run", "--runs-root", str(tmp_path), "--config", str(config_path)]
    )

    assert exit_code == 1
    assert seen["enabled"] is False
    out = capsys.readouterr().out
    lines = [line for line in out.splitlines() if line.strip()]
    assert lines[-1] == "STATUS failed gameday schedule-unavailable"


def test_config_team_id_rescues_a_live_run_without_calling_owner_team_id(
    tmp_path, monkeypatch, capsys
):
    # engine.fixtures.owner_team_id raises when a live league's matchup
    # data does not cleanly flag exactly one owner team; config's own
    # league.team_id is the documented fallback for exactly that case, and
    # must be tried BEFORE owner_team_id, not just as a result matching
    # what owner_team_id would have returned anyway.
    fixture_league = load_fixture_league()
    monkeypatch.setattr(gameday_run, "build_live_league", lambda **kwargs: fixture_league)
    monkeypatch.setattr(gameday_run, "owner_team_id", _must_not_be_called)
    monkeypatch.setattr(
        gameday_run,
        "fetch_week_schedule",
        lambda *args, **kwargs: {"available": True, "data": load_fixture_schedule()},
    )

    config_path = tmp_path / "league.yaml"
    config_path.write_text('league:\n  team_id: "t1"\n', encoding="utf-8")

    exit_code = main(
        [
            "--week",
            "3",
            "--date",
            "2026-09-14",
            "--dry-run",
            "--prose",
            "plain",
            "--runs-root",
            str(tmp_path),
            "--config",
            str(config_path),
        ]
    )

    assert exit_code == 0
    out = capsys.readouterr().out
    lines = [line for line in out.splitlines() if line.strip()]
    assert lines[-1] == "STATUS dry-run gameday"


def _must_not_be_called(*args, **kwargs):
    raise AssertionError("owner_team_id must not be called when config.league.team_id is set")
