"""Tests for engine.matchup: the weekly matchup projection against the
scheduled opponent. Every test reads the real fixture through
engine.fixtures.load_fixture_league() rather than inlining a duplicate
sample league.
"""
from __future__ import annotations

import json

import pytest

from engine.common import EngineError
from engine.common import round_points
from engine import fixtures
from engine import lineup
from engine import matchup


@pytest.fixture()
def league():
    return fixtures.load_fixture_league()


# ---------------------------------------------------------------------------
# week_matchups
# ---------------------------------------------------------------------------


def test_week_matchups_week3_covers_all_four_teams_once(league):
    result = matchup.week_matchups(league, 3)

    assert len(result) == 2
    team_ids = []
    for m in result:
        team_ids.extend(m["team_ids"])

    assert sorted(team_ids) == ["t1", "t2", "t3", "t4"]
    assert len(set(team_ids)) == 4


def test_week_matchups_unscheduled_week_raises(league):
    with pytest.raises(EngineError):
        matchup.week_matchups(league, 99)


# ---------------------------------------------------------------------------
# find_opponent
# ---------------------------------------------------------------------------


def test_find_opponent_symmetric_for_every_week3_pairing(league):
    for m in matchup.week_matchups(league, 3):
        team_a, team_b = m["team_ids"]
        assert matchup.find_opponent(league, team_a, 3) == team_b
        assert matchup.find_opponent(league, team_b, 3) == team_a


def test_find_opponent_differs_by_week_for_t1(league):
    week2_opponent = matchup.find_opponent(league, "t1", 2)
    week3_opponent = matchup.find_opponent(league, "t1", 3)
    week4_opponent = matchup.find_opponent(league, "t1", 4)

    assert week2_opponent == "t3"
    assert week3_opponent == "t2"
    assert week4_opponent == "t4"

    assert week2_opponent != week3_opponent
    assert week4_opponent != week3_opponent


def test_find_opponent_unknown_team_raises(league):
    with pytest.raises(EngineError):
        matchup.find_opponent(league, "not_a_real_team", 3)


# ---------------------------------------------------------------------------
# matchup_projection
# ---------------------------------------------------------------------------


def test_matchup_projection_totals_match_optimal_lineup(league):
    result = matchup.matchup_projection(league, "t1", 3)

    expected_team = lineup.optimal_lineup(league, "t1", 3)
    opponent_id = matchup.find_opponent(league, "t1", 3)
    expected_opponent = lineup.optimal_lineup(league, opponent_id, 3)

    assert result["team"]["total_points"] == expected_team["total_points"]
    assert result["opponent"]["total_points"] == expected_opponent["total_points"]

    expected_margin = round_points(
        expected_team["total_points"] - expected_opponent["total_points"]
    )
    assert result["margin"] == pytest.approx(expected_margin, abs=1e-6)

    if expected_margin > 0:
        assert result["favorite_team_id"] == "t1"
    elif expected_margin < 0:
        assert result["favorite_team_id"] == opponent_id
    else:
        assert result["favorite_team_id"] == min("t1", opponent_id)


def test_matchup_projection_team_names_and_matchup_id(league):
    result = matchup.matchup_projection(league, "t1", 3)

    opponent_id = matchup.find_opponent(league, "t1", 3)
    assert result["team"]["team_name"] == fixtures.get_team(league, "t1")["name"]
    assert result["opponent"]["team_name"] == fixtures.get_team(league, opponent_id)["name"]
    assert result["week"] == 3

    week3_matchup = next(
        m for m in matchup.week_matchups(league, 3) if "t1" in m["team_ids"]
    )
    assert result["matchup_id"] == week3_matchup["matchup_id"]


def test_matchup_projection_shares_one_points_map(league):
    from engine.scoring import projected_points_by_player

    shared_points = projected_points_by_player(league, 3)
    result = matchup.matchup_projection(league, "t1", 3, points=shared_points)

    expected_team = lineup.optimal_lineup(league, "t1", 3, points=shared_points)
    assert result["team"]["total_points"] == expected_team["total_points"]


# ---------------------------------------------------------------------------
# slot_edges
# ---------------------------------------------------------------------------


def test_slot_edges_one_per_unit_sorted_and_sums_to_margin(league):
    result = matchup.matchup_projection(league, "t1", 3)

    units = fixtures.starting_slot_units(league)
    assert len(result["slot_edges"]) == len(units) == 9

    edges = [edge["edge"] for edge in result["slot_edges"]]
    assert edges == sorted(edges, reverse=True)

    assert sum(edges) == pytest.approx(result["margin"], abs=1e-6)


def test_slot_edges_player_name_is_team_side_player_not_fantasy_team(league):
    result = matchup.matchup_projection(league, "t1", 3)

    fantasy_team_name = result["team"]["team_name"]
    for edge in result["slot_edges"]:
        if edge["team_player_id"] is not None:
            assert edge["team_name"] != fantasy_team_name


# ---------------------------------------------------------------------------
# JSON serializability
# ---------------------------------------------------------------------------


def test_matchup_projection_is_json_serializable(league):
    result = matchup.matchup_projection(league, "t1", 3)
    serialized = json.dumps(result)
    assert isinstance(serialized, str)
