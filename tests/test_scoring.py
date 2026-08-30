"""Tests for engine.scoring: stat lines plus league rules to projected
points. Every test reads the real fixture through
engine.fixtures.load_fixture_league() rather than inlining a duplicate
sample league. Where a test needs an expected number, it is hand computed
from the fixture's own values, with the arithmetic shown in a comment
directly above the assertion that checks it.
"""
from __future__ import annotations

import pytest

from engine.common import EngineError
from engine import fixtures
from engine import scoring


@pytest.fixture()
def league():
    return fixtures.load_fixture_league()


# ---------------------------------------------------------------------------
# score_stat_line: per unit stats
# ---------------------------------------------------------------------------


def test_score_stat_line_skill_player_matches_hand_computation(league):
    # p1004 (Reeve Wexford, WR, owner team) week 3 stat line:
    #   receptions: 5.0, receiving_yards: 73.0
    # scoring.stats: receptions 0.5, receiving_yards 0.1
    # 5.0 * 0.5 + 73.0 * 0.1 = 2.5 + 7.3 = 9.8
    week_stats = fixtures.projections_for_week(league, 3)
    stats = week_stats["p1004"]
    assert stats == {"receptions": 5.0, "receiving_yards": 73.0}

    total = scoring.score_stat_line(stats, league["settings"]["scoring"])
    assert total == pytest.approx(9.8, abs=1e-6)


def test_score_stat_line_handles_negative_coefficient(league):
    # p1001 (Dax Voss, QB, owner team) week 3 stat line:
    #   passing_yards: 275.0, passing_touchdowns: 2.0, interceptions: 1.0,
    #   rushing_yards: 55.0
    # scoring.stats: passing_yards 0.04, passing_touchdowns 4.0,
    #   interceptions -2.0, rushing_yards 0.1
    # 275.0*0.04 + 2.0*4.0 + 1.0*(-2.0) + 55.0*0.1
    #   = 11.0 + 8.0 - 2.0 + 5.5 = 22.5
    week_stats = fixtures.projections_for_week(league, 3)
    stats = week_stats["p1001"]
    assert stats == {
        "passing_yards": 275.0,
        "passing_touchdowns": 2.0,
        "interceptions": 1.0,
        "rushing_yards": 55.0,
    }

    total = scoring.score_stat_line(stats, league["settings"]["scoring"])
    assert total == pytest.approx(22.5, abs=1e-6)


def test_score_stat_line_ignores_unknown_stat_key(league):
    # f2001 (free agent WR) week 3 stat line already carries "targets",
    # a key with no entry in settings.scoring.stats:
    #   receptions: 3.0, receiving_yards: 40.0, targets: 5.0
    # Only the known keys count:
    # 3.0 * 0.5 + 40.0 * 0.1 = 1.5 + 4.0 = 5.5
    week_stats = fixtures.projections_for_week(league, 3)
    stats = week_stats["f2001"]
    assert stats == {"receptions": 3.0, "receiving_yards": 40.0, "targets": 5.0}

    scoring_rules = league["settings"]["scoring"]
    total = scoring.score_stat_line(stats, scoring_rules)
    assert total == pytest.approx(5.5, abs=1e-6)

    # Adding a second, deliberately huge unknown key must not move the total.
    stats_with_junk = dict(stats)
    stats_with_junk["definitely_unknown_stat"] = 999999.0
    total_with_junk = scoring.score_stat_line(stats_with_junk, scoring_rules)
    assert total_with_junk == pytest.approx(total, abs=1e-6)


# ---------------------------------------------------------------------------
# score_stat_line: brackets
# ---------------------------------------------------------------------------


def test_score_stat_line_bracket_path_and_bracket_delta(league):
    # p1009 (Wes Thornwell, DEF, owner team) week 3 stat line:
    #   defense_sacks: 2.0, defense_interceptions: 1.0,
    #   defense_fumble_recoveries: 0.5, defense_points_allowed: 15.0
    # Per unit part: 2.0*1.0 + 1.0*2.0 + 0.5*2.0 = 2.0 + 2.0 + 1.0 = 5.0
    # Bracket for defense_points_allowed 15.0 is [14, 20, 1.0] (inclusive),
    # so + 1.0. Total = 5.0 + 1.0 = 6.0
    week_stats = fixtures.projections_for_week(league, 3)
    stats = week_stats["p1009"]
    assert stats == {
        "defense_sacks": 2.0,
        "defense_interceptions": 1.0,
        "defense_fumble_recoveries": 0.5,
        "defense_points_allowed": 15.0,
    }

    scoring_rules = league["settings"]["scoring"]
    total_mid = scoring.score_stat_line(stats, scoring_rules)
    assert total_mid == pytest.approx(6.0, abs=1e-6)

    # Move defense_points_allowed from 15.0 into the [1, 6, 7.0] bracket by
    # setting it to 3.0. The per unit part (5.0) is unchanged; only the
    # bracket contribution changes, from 1.0 to 7.0, a delta of 6.0.
    # New total = 5.0 + 7.0 = 12.0
    stats_low_points_allowed = dict(stats)
    stats_low_points_allowed["defense_points_allowed"] = 3.0
    total_low = scoring.score_stat_line(stats_low_points_allowed, scoring_rules)
    assert total_low == pytest.approx(12.0, abs=1e-6)
    assert (total_low - total_mid) == pytest.approx(7.0 - 1.0, abs=1e-6)


def test_score_stat_line_unmatched_bracket_value_raises(league):
    week_stats = fixtures.projections_for_week(league, 3)
    stats = dict(week_stats["p1009"])
    # -1 falls below every bracket's low bound (the lowest bracket starts
    # at 0), so it must match nothing and raise rather than clamp.
    stats["defense_points_allowed"] = -1

    with pytest.raises(EngineError):
        scoring.score_stat_line(stats, league["settings"]["scoring"])


def test_score_stat_line_malformed_bracket_entry_raises(league):
    # The fixture's own first defense_points_allowed bracket is
    # [0, 0, 10.0]. Truncate a copy of it to two elements so it is no
    # longer a valid [low, high, points] triple.
    real_brackets = league["settings"]["scoring"]["brackets"]["defense_points_allowed"]
    broken_entry = list(real_brackets[0])[:2]
    scoring_rules = {
        "stats": {},
        "brackets": {"defense_points_allowed": [broken_entry]},
    }
    with pytest.raises(EngineError):
        scoring.score_stat_line({"defense_points_allowed": 0}, scoring_rules)


# ---------------------------------------------------------------------------
# project_player_points
# ---------------------------------------------------------------------------


def test_project_player_points_matches_score_stat_line(league):
    week_stats = fixtures.projections_for_week(league, 3)
    stats = week_stats["p1004"]
    expected = scoring.score_stat_line(stats, league["settings"]["scoring"])

    assert scoring.project_player_points(league, "p1004", 3) == pytest.approx(
        expected, abs=1e-6
    )


def test_project_player_points_absent_player_in_populated_week_is_zero(league):
    # Week 2 is deliberately projected for the owner team (t1) only. p200
    # is on t2, a non owner team, so week 2 is populated but has no entry
    # for him.
    week_stats = fixtures.projections_for_week(league, 2)
    assert "p200" not in week_stats

    assert scoring.project_player_points(league, "p200", 2) == 0.0


def test_project_player_points_week_with_no_projections_raises(league):
    # Week 1 has no projections at all in this fixture (only weeks 2, 3
    # and 4 are populated), so this must propagate EngineError rather than
    # returning 0.0 like the merely-absent-player case above.
    with pytest.raises(EngineError):
        fixtures.projections_for_week(league, 1)

    with pytest.raises(EngineError):
        scoring.project_player_points(league, "p200", 1)


# ---------------------------------------------------------------------------
# projected_points_by_player
# ---------------------------------------------------------------------------


def test_projected_points_by_player_covers_every_player_and_is_rounded(league):
    result = scoring.projected_points_by_player(league, 3)

    all_player_ids = {player["player_id"] for player in league["players"]}
    assert set(result.keys()) == all_player_ids

    free_agent_ids = set(fixtures.free_agent_ids(league))
    assert free_agent_ids
    assert free_agent_ids.issubset(result.keys())

    for player_id, points in result.items():
        assert isinstance(points, float)
        assert points == pytest.approx(round(points, 2), abs=1e-9)
