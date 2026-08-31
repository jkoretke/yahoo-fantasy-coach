"""Tests for engine.timing: kickoff windows and repeat suppression.

Every test that reads schedule data uses engine.timing.load_fixture_schedule
against the real fixtures/phase4/schedule.json, and every test that reads
league data uses engine.fixtures.load_fixture_league against the real
fixtures/sample_league fixture. No test writes to either fixture directory;
tests that touch disk (read_sent/write_sent) always use tmp_path.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from engine.common import EngineError
from engine.fixtures import load_fixture_league
from engine import timing


# ---------------------------------------------------------------------------
# parse_iso_utc
# ---------------------------------------------------------------------------


def test_parse_iso_utc_accepts_all_four_string_forms() -> None:
    expected = datetime(2026, 9, 14, 11, 45, tzinfo=timezone.utc)

    assert timing.parse_iso_utc("2026-09-14T11:45Z") == expected
    assert timing.parse_iso_utc("2026-09-14T11:45:00Z") == expected
    assert timing.parse_iso_utc("2026-09-14T11:45:00+00:00") == expected

    bare_date = timing.parse_iso_utc("2026-09-14")
    assert bare_date == datetime(2026, 9, 14, 0, 0, tzinfo=timezone.utc)


def test_parse_iso_utc_passes_through_an_aware_datetime() -> None:
    already_aware = datetime(2026, 9, 14, 11, 45, tzinfo=timezone.utc)
    assert timing.parse_iso_utc(already_aware) == already_aware


def test_parse_iso_utc_raises_engine_error_on_garbage() -> None:
    with pytest.raises(EngineError):
        timing.parse_iso_utc("not a timestamp")

    with pytest.raises(EngineError):
        timing.parse_iso_utc("")


# ---------------------------------------------------------------------------
# games_on_date / earliest_kickoff_on_date
# ---------------------------------------------------------------------------


def test_games_on_date_2026_09_13_is_empty() -> None:
    schedule = timing.load_fixture_schedule()
    assert timing.games_on_date(schedule, "2026-09-13") == []


def test_games_on_date_2026_09_14_is_five_games() -> None:
    schedule = timing.load_fixture_schedule()
    games = timing.games_on_date(schedule, "2026-09-14")
    assert len(games) == 5


def test_fixture_schedule_day_distribution_and_count() -> None:
    schedule = timing.load_fixture_schedule()
    assert schedule["count"] == 6
    assert len(schedule["games"]) == 6
    assert len(timing.games_on_date(schedule, "2026-09-15")) == 1


def test_earliest_kickoff_on_date_for_t1_starters() -> None:
    schedule = timing.load_fixture_schedule()
    league = load_fixture_league()
    starter_teams = timing.starter_nfl_teams(league, "t1", 3)

    earliest = timing.earliest_kickoff_on_date(schedule, "2026-09-14", starter_teams)

    assert earliest == "2026-09-14T13:00:00Z"


# ---------------------------------------------------------------------------
# inside_window
# ---------------------------------------------------------------------------


def test_inside_window_boundary_is_inclusive_at_exactly_75_minutes() -> None:
    kickoff = "2026-09-14T13:00:00Z"
    assert timing.inside_window(kickoff, "2026-09-14T11:45:00Z") is True


def test_inside_window_is_false_outside_the_window() -> None:
    kickoff = "2026-09-14T13:00:00Z"
    assert timing.inside_window(kickoff, "2026-09-14T11:29:00Z") is False


# ---------------------------------------------------------------------------
# window_key
# ---------------------------------------------------------------------------


def test_window_key_is_stable_across_calls() -> None:
    kickoff = "2026-09-14T13:00:00Z"
    first = timing.window_key(kickoff)
    second = timing.window_key(kickoff)
    assert first == second == "20260914T1300Z"


# ---------------------------------------------------------------------------
# read_sent / write_sent
# ---------------------------------------------------------------------------


def test_read_sent_on_missing_path_returns_empty_set(tmp_path: Path) -> None:
    missing = tmp_path / "does" / "not" / "exist.sent"
    assert timing.read_sent(missing) == set()


def test_write_sent_then_read_sent_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "2026" / "wk03" / "inactive-20260914T1300Z.sent"
    keys = {"p1002", "p1005", "p1001"}

    timing.write_sent(path, keys)
    result = timing.read_sent(path)

    assert result == keys


# ---------------------------------------------------------------------------
# starter_nfl_teams
# ---------------------------------------------------------------------------


def test_starter_nfl_teams_includes_out_and_bye_players_teams() -> None:
    league = load_fixture_league()
    teams = timing.starter_nfl_teams(league, "t1", 3)

    # p1002 (RB, out) plays for QRN; p1005 (WR, on bye week 3) plays for OKS.
    assert "QRN" in teams
    assert "OKS" in teams


def test_starter_nfl_teams_excludes_bench_only_teams() -> None:
    league = load_fixture_league()
    teams = timing.starter_nfl_teams(league, "t1", 3)

    # p1010 (FLX), p1011 (GLE), p1012 (MTN) are bench slots, not starters.
    assert "FLX" not in teams
    assert "GLE" not in teams
    assert "MTN" not in teams
