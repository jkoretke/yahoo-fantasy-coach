"""Tests for engine.waivers: FAAB bids and rolling priority claim or skip.

Every test reads the real fixture through
engine.fixtures.load_fixture_league() rather than inlining a duplicate
sample league. Where a branch case needs a broken league (an unknown
waiver type, a team missing from faab_remaining), the fixture is deep
copied and mutated in memory; nothing under fixtures/ is ever edited.
"""
from __future__ import annotations

import copy
import json

import pytest

from engine.common import EngineError
from engine import fixtures
from engine import waivers


@pytest.fixture()
def league():
    return fixtures.load_fixture_league()


# ---------------------------------------------------------------------------
# waiver_position and required_priority_gain
# ---------------------------------------------------------------------------


def test_waiver_position_t1_is_first(league):
    assert waivers.waiver_position(league, "t1") == 1


def test_waiver_position_unknown_team_raises(league):
    with pytest.raises(EngineError):
        waivers.waiver_position(league, "not_a_team")


def test_required_priority_gain_scales_with_position(league):
    # t1 sits at position 1 of 4: 2.0 * (1 + 3/4) = 3.5.
    assert waivers.required_priority_gain(league, "t1") == pytest.approx(3.5, abs=1e-6)

    # t4 sits at position 4 of 4: 2.0 * (1 + 0/4) = 2.0, the base gain.
    assert waivers.required_priority_gain(league, "t4") == pytest.approx(2.0, abs=1e-6)


# ---------------------------------------------------------------------------
# drop_candidates
# ---------------------------------------------------------------------------


def test_drop_candidates_excludes_ir_and_current_optimal_starters(league):
    from engine.lineup import optimal_lineup

    optimal = optimal_lineup(league, "t1", 3)
    starter_ids = set(optimal["starter_ids"])

    candidates = waivers.drop_candidates(league, "t1", 3)

    assert candidates, "t1 must have at least one legal drop candidate"
    # p1013 sits in t1's IR slot and must never be offered as a drop.
    assert "p1013" not in candidates
    for player_id in candidates:
        assert player_id not in starter_ids

    from engine.scoring import projected_points_by_player

    points = projected_points_by_player(league, 3)
    ordered = [points.get(player_id, 0.0) for player_id in candidates]
    assert ordered == sorted(ordered)


# ---------------------------------------------------------------------------
# evaluate_claim: the fixture gain bands, shared with test_lineup.py
# ---------------------------------------------------------------------------


def test_evaluate_claim_small_upgrade_f2001(league):
    candidates = waivers.drop_candidates(league, "t1", 3)
    result = waivers.evaluate_claim(league, "t1", 3, "f2001")

    assert 0.5 < result["points_gained"] < 2.5
    assert result["drop_player_id"] is not None
    assert result["drop_player_id"] != "p1013"
    assert result["drop_player_id"] in fixtures.team_roster_player_ids(league, "t1")

    # Dropping any player already outside the baseline optimal lineup
    # produces the identical resulting total, so the tie must break toward
    # the first candidate in drop_candidates' own ascending order, the
    # least valuable player to keep, rather than an arbitrary one.
    assert result["drop_player_id"] == candidates[0]


def test_evaluate_claim_big_upgrade_f2002(league):
    result = waivers.evaluate_claim(league, "t1", 3, "f2002")
    assert result["points_gained"] >= 5.0


def test_evaluate_claim_unknown_free_agent_raises(league):
    with pytest.raises(EngineError):
        waivers.evaluate_claim(league, "t1", 3, "not_a_real_player")


# ---------------------------------------------------------------------------
# faab_bid edge cases
# ---------------------------------------------------------------------------


def test_faab_bid_edge_cases():
    assert waivers.faab_bid(0.0, 80) == 0
    assert waivers.faab_bid(-1.0, 80) == 0
    assert waivers.faab_bid(6.0, 0) == 0
    assert waivers.faab_bid(6.0, 2) <= 2


# ---------------------------------------------------------------------------
# FAAB never declines a real upgrade
# ---------------------------------------------------------------------------


def test_faab_never_declines_a_real_upgrade():
    league_faab = fixtures.load_fixture_league(waiver_type="faab")
    result = waivers.rank_waiver_targets(league_faab, "t1", 3)

    assert result["waiver_type"] == "faab"
    faab_remaining = result["faab_remaining"]

    for target in result["targets"]:
        if target["points_gained"] > 0:
            assert target["verdict"] == "claim", target
        else:
            assert target["verdict"] == "skip", target
            assert target["bid"] == 0, target
        assert target["bid"] <= faab_remaining, target

    # The fixture guarantees both kinds of target exist (see
    # tests/test_lineup.py's free agent gain band test); assert both
    # branches of the loop above actually ran, so this cannot pass
    # vacuously if that guarantee ever drifts.
    assert any(target["points_gained"] > 0 for target in result["targets"])
    assert any(target["points_gained"] <= 0 for target in result["targets"])


def test_faab_zero_budget_bids_nothing_but_still_claims():
    broken = copy.deepcopy(fixtures.load_fixture_league(waiver_type="faab"))
    broken["settings"]["waiver"]["faab_remaining"]["t1"] = 0

    result = waivers.rank_waiver_targets(broken, "t1", 3)

    assert result["faab_remaining"] == 0
    for target in result["targets"]:
        assert target["bid"] == 0, target
        if target["points_gained"] > 0:
            assert target["verdict"] == "claim", target


# ---------------------------------------------------------------------------
# Priority correctly skips a real upgrade
# ---------------------------------------------------------------------------


def test_priority_skips_a_real_upgrade_but_claims_a_big_one():
    league_priority = fixtures.load_fixture_league(waiver_type="priority")
    result = waivers.rank_waiver_targets(league_priority, "t1", 3)

    assert result["waiver_type"] == "priority"
    bar = waivers.required_priority_gain(league_priority, "t1")

    targets_by_id = {target["player_id"]: target for target in result["targets"]}

    f2001_target = targets_by_id["f2001"]
    assert 0 < f2001_target["points_gained"] < bar
    assert f2001_target["verdict"] == "skip"

    f2002_target = targets_by_id["f2002"]
    assert f2002_target["points_gained"] >= bar
    assert f2002_target["verdict"] == "claim"

    positive_skips = [
        target
        for target in result["targets"]
        if target["points_gained"] > 0 and target["verdict"] == "skip"
    ]
    assert positive_skips, "at least one positive gain target must still read as skip"


# ---------------------------------------------------------------------------
# The two branches carry different keys
# ---------------------------------------------------------------------------


def test_faab_and_priority_branches_carry_different_keys():
    league_faab = fixtures.load_fixture_league(waiver_type="faab")
    league_priority = fixtures.load_fixture_league(waiver_type="priority")

    faab_result = waivers.rank_waiver_targets(league_faab, "t1", 3)
    priority_result = waivers.rank_waiver_targets(league_priority, "t1", 3)

    assert "faab_remaining" in faab_result
    assert "waiver_position" not in faab_result
    assert "required_gain" not in faab_result

    assert "waiver_position" in priority_result
    assert "required_gain" in priority_result
    assert "faab_remaining" not in priority_result

    for target in faab_result["targets"]:
        assert "bid" in target

    for target in priority_result["targets"]:
        assert "bid" not in target
        assert "required_gain" in target
        assert "priority_position" in target


# ---------------------------------------------------------------------------
# Sorting
# ---------------------------------------------------------------------------


def test_targets_sorted_by_points_gained_descending():
    result = waivers.rank_waiver_targets(fixtures.load_fixture_league(waiver_type="faab"), "t1", 3)
    gains = [target["points_gained"] for target in result["targets"]]
    assert gains == sorted(gains, reverse=True)


# ---------------------------------------------------------------------------
# Error branches, built from a deep copied fixture, never from fixtures/
# ---------------------------------------------------------------------------


def test_unknown_waiver_type_raises(league):
    broken = copy.deepcopy(league)
    broken["settings"]["waiver"]["type"] = "snake_draft"

    with pytest.raises(EngineError):
        waivers.rank_waiver_targets(broken, "t1", 3)


def test_faab_team_missing_from_faab_remaining_raises():
    broken = copy.deepcopy(fixtures.load_fixture_league(waiver_type="faab"))
    del broken["settings"]["waiver"]["faab_remaining"]["t1"]

    with pytest.raises(EngineError):
        waivers.rank_waiver_targets(broken, "t1", 3)


# ---------------------------------------------------------------------------
# JSON serializability
# ---------------------------------------------------------------------------


def test_rank_waiver_targets_is_json_serializable():
    league_faab = fixtures.load_fixture_league(waiver_type="faab")
    league_priority = fixtures.load_fixture_league(waiver_type="priority")

    faab_result = waivers.rank_waiver_targets(league_faab, "t1", 3)
    priority_result = waivers.rank_waiver_targets(league_priority, "t1", 3)

    json.dumps(faab_result)
    json.dumps(priority_result)
