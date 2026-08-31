"""Tests for engine.trades: position surplus and deficit across every
roster in the league, and the trade ideas built from them.

Every test reads the real fixture through engine.fixtures.load_fixture_league()
rather than inlining a duplicate sample league, except the two tests that
need to force the "would drop below starting_slots" skip: those build a
small synthetic league dict directly, matching the same schema
engine.fixtures documents, and pass points explicitly so no projections
file is needed.
"""
from __future__ import annotations

import json

import pytest

from engine.common import EngineError, round_points
from engine import fixtures
from engine import trades


@pytest.fixture()
def league():
    return fixtures.load_fixture_league()


def _assert_all_points_rounded(value):
    """Recursively assert every float in value already equals its own
    round_points, so nothing sneaks a raw unrounded points value out."""
    if isinstance(value, float):
        assert value == round_points(value)
    elif isinstance(value, dict):
        for item in value.values():
            _assert_all_points_rounded(item)
    elif isinstance(value, list):
        for item in value:
            _assert_all_points_rounded(item)


# ---------------------------------------------------------------------------
# position_demand
# ---------------------------------------------------------------------------


def test_position_demand_counts_the_nine_starting_units(league):
    demand = trades.position_demand(league)

    assert demand["single"] == {
        "QB": 1,
        "RB": 2,
        "WR": 2,
        "TE": 1,
        "K": 1,
        "DEF": 1,
    }
    # 1 + 2 + 2 + 1 + 1 + 1 single units, plus the one W/R/T flex unit = 9.
    assert sum(demand["single"].values()) + len(demand["flex"]) == 9


def test_position_demand_records_wrt_slot_under_flex(league):
    demand = trades.position_demand(league)

    assert demand["flex"] == [
        {"slot": "W/R/T", "eligible_positions": ["WR", "RB", "TE"]}
    ]
    # A flex slot's own count never adds to any single position's demand:
    # WR/RB/TE each count only their own single starting slots (2, 2, 1).
    assert demand["single"]["WR"] == 2
    assert demand["single"]["RB"] == 2
    assert demand["single"]["TE"] == 1


def test_position_demand_is_json_serializable(league):
    json.dumps(trades.position_demand(league))


# ---------------------------------------------------------------------------
# team_position_inventory
# ---------------------------------------------------------------------------


def test_team_position_inventory_excludes_out_and_bye_players_from_startable(league):
    inventory = trades.team_position_inventory(league, "t1", 3)
    by_position = {entry["position"]: entry for entry in inventory}

    rb = by_position["RB"]
    rb_ids = {p["player_id"] for p in rb["players"]}
    assert "p1002" in rb_ids  # status "O": rostered, but not startable
    assert rb["startable"] < rb["rostered"]

    wr = by_position["WR"]
    wr_ids = {p["player_id"] for p in wr["players"]}
    assert "p1005" in wr_ids  # bye_week 3: rostered, but not startable
    assert wr["startable"] < wr["rostered"]

    # Directly confirm neither excluded player is counted in a startable total
    # by rebuilding the startable count from is_startable at week 3.
    from engine.lineup import is_startable

    p1002 = fixtures.get_player(league, "p1002")
    p1005 = fixtures.get_player(league, "p1005")
    assert is_startable(p1002, 3) is False
    assert is_startable(p1005, 3) is False


def test_team_position_inventory_covers_positions_from_demand_and_roster(league):
    inventory = trades.team_position_inventory(league, "t1", 3)
    positions = [entry["position"] for entry in inventory]

    assert positions == sorted(positions)
    demand_positions = set(trades.position_demand(league)["single"].keys())
    assert demand_positions.issubset(set(positions))


def test_team_position_inventory_multi_position_player_counts_under_each(league):
    # p1010 is RB/WR; p1011 is WR/TE. Both must appear in every one of their
    # own eligible position groups, not just their "primary" one.
    inventory = trades.team_position_inventory(league, "t1", 3)
    by_position = {entry["position"]: entry for entry in inventory}

    rb_ids = {p["player_id"] for p in by_position["RB"]["players"]}
    wr_ids = {p["player_id"] for p in by_position["WR"]["players"]}
    te_ids = {p["player_id"] for p in by_position["TE"]["players"]}

    assert "p1010" in rb_ids and "p1010" in wr_ids
    assert "p1011" in wr_ids and "p1011" in te_ids


def test_team_position_inventory_players_sorted_by_points_desc_then_id(league):
    inventory = trades.team_position_inventory(league, "t1", 3)
    for entry in inventory:
        keys = [(-p["points"], p["player_id"]) for p in entry["players"]]
        assert keys == sorted(keys)


def test_team_position_inventory_surplus_is_startable_minus_starting_slots(league):
    inventory = trades.team_position_inventory(league, "t1", 3)
    for entry in inventory:
        assert entry["surplus"] == entry["startable"] - entry["starting_slots"]


def test_team_position_inventory_best_surplus_points_zero_when_no_surplus(league):
    inventory = trades.team_position_inventory(league, "t1", 3)
    for entry in inventory:
        if entry["surplus"] <= 0:
            assert entry["best_surplus_points"] == 0.0


def test_team_position_inventory_unknown_team_raises(league):
    with pytest.raises(EngineError):
        trades.team_position_inventory(league, "not_a_team", 3)


def test_team_position_inventory_is_json_serializable(league):
    json.dumps(trades.team_position_inventory(league, "t1", 3))


# ---------------------------------------------------------------------------
# league_position_table
# ---------------------------------------------------------------------------


def test_league_position_table_covers_all_four_teams(league):
    table = trades.league_position_table(league, 3)
    assert set(table.keys()) == {"t1", "t2", "t3", "t4"}
    for team_id, inventory in table.items():
        assert inventory == trades.team_position_inventory(league, team_id, 3)


def test_league_position_table_shares_one_points_map(league):
    from engine.scoring import projected_points_by_player

    shared_points = projected_points_by_player(league, 3)
    table = trades.league_position_table(league, 3, points=shared_points)
    expected = trades.team_position_inventory(league, "t2", 3, points=shared_points)
    assert table["t2"] == expected


def test_league_position_table_is_json_serializable(league):
    json.dumps(trades.league_position_table(league, 3))


# ---------------------------------------------------------------------------
# trade_ideas: structure, against the fixture at week 3 (acceptance case)
# ---------------------------------------------------------------------------


def test_trade_ideas_week3_t1_has_the_required_keys(league):
    result = trades.trade_ideas(league, "t1", 3)

    assert set(result.keys()) == {"team_id", "week", "surplus", "deficit", "ideas"}
    assert result["team_id"] == "t1"
    assert result["week"] == 3
    assert isinstance(result["surplus"], list)
    assert isinstance(result["deficit"], list)
    assert isinstance(result["ideas"], list)


def test_trade_ideas_surplus_and_deficit_match_team_position_inventory(league):
    result = trades.trade_ideas(league, "t1", 3)
    inventory = trades.team_position_inventory(league, "t1", 3)

    assert result["surplus"] == [e for e in inventory if e["surplus"] > 0]
    assert result["deficit"] == [e for e in inventory if e["surplus"] < 0]


def test_trade_ideas_never_names_owner_as_partner_week3(league):
    result = trades.trade_ideas(league, "t1", 3)
    for idea in result["ideas"]:
        assert idea["partner_team_id"] != "t1"


def test_trade_ideas_week3_is_json_serializable(league):
    result = trades.trade_ideas(league, "t1", 3)
    json.dumps(result)


def test_trade_ideas_unknown_team_raises(league):
    with pytest.raises(EngineError):
        trades.trade_ideas(league, "not_a_team", 3)


# ---------------------------------------------------------------------------
# trade_ideas: deeper behaviour, at week 2, where the fixture actually
# produces non empty ideas for t1 (t1's TE inventory sits at exactly its
# starting_slots that week, unlike week 3, since p1011's own bye is week 2).
# ---------------------------------------------------------------------------


def test_trade_ideas_week2_t1_is_deterministic_across_two_calls(league):
    first = trades.trade_ideas(league, "t1", 2)
    second = trades.trade_ideas(league, "t1", 2)
    assert first == second
    assert json.dumps(first) == json.dumps(second)


def test_trade_ideas_week2_t1_produces_ideas(league):
    result = trades.trade_ideas(league, "t1", 2)
    assert len(result["ideas"]) > 0


def test_trade_ideas_week2_never_names_owner_as_partner(league):
    for team_id in ("t1", "t2", "t3", "t4"):
        result = trades.trade_ideas(league, team_id, 2)
        for idea in result["ideas"]:
            assert idea["partner_team_id"] != team_id


def test_trade_ideas_week2_ordered_by_points_gained_descending(league):
    result = trades.trade_ideas(league, "t1", 2)
    gains = [idea["points_gained"] for idea in result["ideas"]]
    assert gains == sorted(gains, reverse=True)


def test_trade_ideas_week2_sort_key_full_tiebreak(league):
    result = trades.trade_ideas(league, "t1", 2, limit=100)
    keys = [
        (-idea["points_gained"], idea["partner_team_id"], idea["send"]["player_id"])
        for idea in result["ideas"]
    ]
    assert keys == sorted(keys)


def test_trade_ideas_week2_respects_limit(league):
    unlimited = trades.trade_ideas(league, "t1", 2, limit=100)
    limited = trades.trade_ideas(league, "t1", 2, limit=1)
    assert len(limited["ideas"]) == 1
    assert limited["ideas"][0] == unlimited["ideas"][0]


def test_trade_ideas_week2_idea_shape_and_note_names_both_positions(league):
    result = trades.trade_ideas(league, "t1", 2)
    assert result["ideas"], "expected at least one idea to check shape against"

    for idea in result["ideas"]:
        assert set(idea.keys()) == {
            "partner_team_id",
            "partner_team_name",
            "send",
            "receive",
            "send_position",
            "receive_position",
            "points_gained",
            "note",
        }
        assert set(idea["send"].keys()) == {"player_id", "name", "position", "points"}
        assert set(idea["receive"].keys()) == {"player_id", "name", "position", "points"}
        assert idea["send"]["position"] == idea["send_position"]
        assert idea["receive"]["position"] == idea["receive_position"]
        assert idea["send_position"] in idea["note"]
        assert idea["receive_position"] in idea["note"]
        assert idea["partner_team_name"] == fixtures.get_team(league, idea["partner_team_id"])["name"]
        assert idea["points_gained"] == round_points(
            idea["receive"]["points"] - idea["send"]["points"]
        )


def test_trade_ideas_week2_never_sends_and_receives_the_same_player(league):
    result = trades.trade_ideas(league, "t1", 2)
    for idea in result["ideas"]:
        assert idea["send"]["player_id"] != idea["receive"]["player_id"]


def test_trade_ideas_week2_send_side_is_a_genuine_owner_surplus_position(league):
    result = trades.trade_ideas(league, "t1", 2)
    surplus_positions = {e["position"] for e in result["surplus"]}
    for idea in result["ideas"]:
        assert idea["send_position"] in surplus_positions


def test_trade_ideas_week2_receive_side_is_not_already_an_owner_surplus(league):
    result = trades.trade_ideas(league, "t1", 2)
    owner_by_position = {e["position"]: e for e in trades.team_position_inventory(league, "t1", 2)}
    for idea in result["ideas"]:
        owner_entry = owner_by_position.get(idea["receive_position"])
        owner_surplus = owner_entry["surplus"] if owner_entry else 0
        assert owner_surplus <= 0


def test_trade_ideas_week2_all_points_are_rounded(league):
    result = trades.trade_ideas(league, "t1", 2)
    _assert_all_points_rounded(result)


def test_trade_ideas_week2_is_json_serializable(league):
    result = trades.trade_ideas(league, "t1", 2)
    json.dumps(result)


# ---------------------------------------------------------------------------
# trade_ideas: never drops either side below its starting_slots. A small
# synthetic league (not the frozen fixture) isolates this one behaviour, with
# points passed explicitly so no projections file is needed.
# ---------------------------------------------------------------------------


def _synthetic_league(swing_player_is_te_eligible: bool) -> dict:
    swing_positions = ["WR", "TE"] if swing_player_is_te_eligible else ["WR"]
    return {
        "settings": {
            "roster_slots": [
                {"slot": "QB", "count": 1, "eligible_positions": ["QB"], "starting": True},
                {"slot": "WR", "count": 1, "eligible_positions": ["WR"], "starting": True},
                {"slot": "TE", "count": 1, "eligible_positions": ["TE"], "starting": True},
                {"slot": "BN", "count": 2, "eligible_positions": [], "starting": False},
            ],
        },
        "players": [
            {"player_id": "w1", "name": "Swing Player", "positions": swing_positions, "status": "", "bye_week": 99},
            {"player_id": "w2", "name": "Top WR", "positions": ["WR"], "status": "", "bye_week": 99},
            {"player_id": "q1", "name": "Only QB", "positions": ["QB"], "status": "", "bye_week": 99},
            {"player_id": "b_te1", "name": "B Starter TE", "positions": ["TE"], "status": "", "bye_week": 99},
            {"player_id": "b_te2", "name": "B Backup TE", "positions": ["TE"], "status": "", "bye_week": 99},
        ],
        "teams": [
            {
                "team_id": "A",
                "name": "Team A",
                "roster": [{"player_id": "q1"}, {"player_id": "w1"}, {"player_id": "w2"}],
            },
            {
                "team_id": "B",
                "name": "Team B",
                "roster": [{"player_id": "b_te1"}, {"player_id": "b_te2"}],
            },
        ],
    }


_SYNTHETIC_POINTS = {"w1": 6.0, "w2": 10.0, "q1": 12.0, "b_te1": 8.0, "b_te2": 4.0}


def test_trade_ideas_skips_a_pair_that_would_drop_a_position_below_starting_slots():
    # w1 is Team A's only TE-eligible player (his own TE slot has no backup),
    # and he is also the best WR surplus candidate. Trading him for a WR-for-TE
    # idea would leave Team A's TE slot with zero startable players.
    league = _synthetic_league(swing_player_is_te_eligible=True)

    inventory = trades.team_position_inventory(league, "A", 1, points=_SYNTHETIC_POINTS)
    by_position = {e["position"]: e for e in inventory}
    assert by_position["TE"]["surplus"] == 0
    assert by_position["WR"]["surplus"] == 1
    assert by_position["WR"]["best_surplus_points"] == 6.0  # w1 is the surplus candidate

    result = trades.trade_ideas(league, "A", 1, points=_SYNTHETIC_POINTS)
    assert result["ideas"] == []


def test_trade_ideas_control_case_without_the_cross_position_conflict():
    # Same league, except the swing player is WR only, so trading him away
    # never touches Team A's TE slot: the idea now goes through.
    league = _synthetic_league(swing_player_is_te_eligible=False)

    result = trades.trade_ideas(league, "A", 1, points=_SYNTHETIC_POINTS)
    assert len(result["ideas"]) == 1
    idea = result["ideas"][0]
    assert idea["send"]["player_id"] == "w1"
    assert idea["receive"]["player_id"] == "b_te2"
    assert idea["partner_team_id"] == "B"
    assert idea["points_gained"] == round_points(4.0 - 6.0)


def test_trade_ideas_synthetic_leagues_are_json_serializable():
    for swing_te in (True, False):
        league = _synthetic_league(swing_player_is_te_eligible=swing_te)
        result = trades.trade_ideas(league, "A", 1, points=_SYNTHETIC_POINTS)
        json.dumps(result)
