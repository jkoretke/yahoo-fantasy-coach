"""Tests for engine.lineup: the exact optimal legal lineup solve and
start/sit deltas. Every test reads the real fixture through
engine.fixtures.load_fixture_league() rather than inlining a duplicate
sample league. Where a test needs an expected number, it is hand computed
from the fixture's own values, with the arithmetic shown in a comment
directly above the assertion that checks it.
"""
from __future__ import annotations

import copy

import pytest

from engine.common import EngineError
from engine import fixtures
from engine import lineup


@pytest.fixture()
def league():
    return fixtures.load_fixture_league()


def _brute_force_max_total(league, team_id, week):
    """Independently compute the maximum legal starting total for team_id.

    Walks the starting slot units in order and, at each unit, recursively
    tries every not yet used startable player eligible for that unit, plus
    the option of leaving the unit empty. This is a per slot recursion,
    never itertools.permutations over the whole candidate pool: for this
    fixture's roster sizes that permutation count would be tens of
    millions, while the per slot recursion only branches where more than
    one candidate is actually eligible for a unit (QB, TE, K and DEF each
    have at most two startable candidates on any one fixture team), so it
    finishes in a fraction of a second.
    """
    from engine.scoring import projected_points_by_player

    points = projected_points_by_player(league, week)
    roster_ids = fixtures.team_roster_player_ids(league, team_id)
    startable_ids = [
        player_id
        for player_id in roster_ids
        if lineup.is_startable(fixtures.get_player(league, player_id), week)
    ]
    units = fixtures.starting_slot_units(league)

    best = [0.0]

    def recurse(index, used, total):
        if index == len(units):
            if total > best[0]:
                best[0] = total
            return
        eligible_positions = set(units[index]["eligible_positions"])
        # Option: leave this unit empty.
        recurse(index + 1, used, total)
        # Option: place each not yet used, eligible startable candidate here.
        for player_id in startable_ids:
            if player_id in used:
                continue
            player = fixtures.get_player(league, player_id)
            if set(player["positions"]) & eligible_positions:
                used.add(player_id)
                recurse(index + 1, used, total + points.get(player_id, 0.0))
                used.discard(player_id)

    recurse(0, set(), 0.0)
    return round(best[0], 2)


# ---------------------------------------------------------------------------
# Known answer: t1's optimal week 3 lineup
# ---------------------------------------------------------------------------


def test_optimal_lineup_t1_week3_known_answer(league):
    result = lineup.optimal_lineup(league, "t1", 3)

    # t1's startable week 3 players and points (status "O" p1002 and bye
    # week 3 p1005 excluded; IR p1013 excluded):
    #   p1001 QB          22.5
    #   p1003 RB          11.2
    #   p1004 WR           9.8
    #   p1006 TE           9.2
    #   p1007 WR           3.9
    #   p1008 K            9.0
    #   p1009 DEF          6.0
    #   p1010 RB/WR       10.5
    #   p1011 WR/TE        8.9
    #   p1012 RB           4.3
    # QB, K and DEF each have exactly one startable candidate, so they are
    # forced: p1001 (22.5), p1008 (9.0), p1009 (6.0) = 37.5.
    # The remaining six slots (RB, RB, WR, WR, TE, W/R/T) are filled from
    # {p1003, p1010, p1004, p1006, p1011, p1012} at 11.2 + 10.5 + 9.8 + 9.2
    # + 8.9 + 4.3 = 53.9, leaving only p1007 (3.9) on the bench among the
    # startable players.
    # Total: 37.5 + 53.9 = 91.4
    expected_total = 91.4
    expected_starters = {
        "p1001", "p1003", "p1004", "p1006", "p1008", "p1009", "p1010", "p1011", "p1012",
    }

    assert set(result["starter_ids"]) == expected_starters
    assert result["total_points"] == pytest.approx(expected_total, abs=1e-6)


# ---------------------------------------------------------------------------
# Brute force cross check: the strongest test in this chunk
# ---------------------------------------------------------------------------


def test_optimal_lineup_matches_brute_force_for_every_team(league):
    for team_id in ["t1", "t2", "t3", "t4"]:
        expected = _brute_force_max_total(league, team_id, 3)
        result = lineup.optimal_lineup(league, team_id, 3)
        assert result["total_points"] == pytest.approx(expected, abs=1e-6), team_id


# ---------------------------------------------------------------------------
# The waiver seam: authoritative gain bands for the fixture free agents
# ---------------------------------------------------------------------------


def test_optimal_lineup_free_agent_gain_bands(league):
    baseline = lineup.optimal_lineup(league, "t1", 3)["total_points"]

    def gain(free_agent_id):
        with_fa = lineup.optimal_lineup(
            league, "t1", 3, extra_player_ids=[free_agent_id]
        )["total_points"]
        return with_fa - baseline

    all_free_agents = fixtures.free_agent_ids(league)
    assert "f2001" in all_free_agents
    assert "f2002" in all_free_agents

    gain_f2001 = gain("f2001")
    gain_f2002 = gain("f2002")
    assert 0.5 < gain_f2001 < 2.5
    assert gain_f2002 >= 5.0

    at_or_below_zero = 0
    for free_agent_id in all_free_agents:
        if free_agent_id in ("f2001", "f2002"):
            continue
        this_gain = gain(free_agent_id)
        assert this_gain < 3.0, (free_agent_id, this_gain)
        if this_gain <= 0.0:
            at_or_below_zero += 1

    assert at_or_below_zero >= 3


# ---------------------------------------------------------------------------
# Exclusion rules: status and bye week
# ---------------------------------------------------------------------------


def test_status_out_player_never_starts(league):
    result = lineup.optimal_lineup(league, "t1", 3)
    # p1002 (Brix Duskin) carries status "O" on t1's roster.
    assert "p1002" not in result["starter_ids"]


def test_bye_week_player_never_starts_but_is_startable_other_weeks(league):
    player = fixtures.get_player(league, "p1005")
    assert player["bye_week"] == 3
    # The fixture deliberately gives the bye player a normal nonzero week 3
    # projection, so exclusion must come from the bye check, not a zero.
    from engine.scoring import projected_points_by_player

    week3_points = projected_points_by_player(league, 3)
    assert week3_points["p1005"] > 0

    assert lineup.is_startable(player, 3) is False
    assert lineup.is_startable(player, 4) is True

    result = lineup.optimal_lineup(league, "t1", 3)
    assert "p1005" not in result["starter_ids"]


def test_ir_slot_player_is_benched_never_started(league):
    result = lineup.optimal_lineup(league, "t1", 3)
    # p1013 (Micah Ridgeway) sits in t1's IR slot with status "IR".
    assert "p1013" in result["bench_ids"]
    assert "p1013" not in result["starter_ids"]


def test_is_startable_raises_on_missing_status_or_bye_week():
    with pytest.raises(EngineError):
        lineup.is_startable({"player_id": "x", "bye_week": 3}, 3)
    with pytest.raises(EngineError):
        lineup.is_startable({"player_id": "x", "status": ""}, 3)


# ---------------------------------------------------------------------------
# current_lineup: reporting, not repairing
# ---------------------------------------------------------------------------


def test_current_lineup_reports_out_player_with_zero_points(league):
    result = lineup.current_lineup(league, "t1", 3)
    out_assignment = next(a for a in result["assignments"] if a["player_id"] == "p1002")
    assert out_assignment["startable"] is False
    assert out_assignment["points"] == 0.0


def test_optimal_beats_current_and_lineup_changes_nonempty(league):
    optimal = lineup.optimal_lineup(league, "t1", 3)
    current = lineup.current_lineup(league, "t1", 3)

    assert optimal["total_points"] > current["total_points"]

    changes = lineup.lineup_changes(current, optimal)
    assert changes
    assert changes[0]["points_gained"] > 0

    # At least one swap must be a real start/sit call: a sit player who was
    # actually startable at week 3, proving the fixture is not exercising
    # only the two injury/bye benchings.
    startable_sits = [
        change
        for change in changes
        if change["sit_player_id"] is not None
        and lineup.is_startable(fixtures.get_player(league, change["sit_player_id"]), 3)
    ]
    assert startable_sits, changes


def test_lineup_changes_empty_when_already_optimal(league):
    optimal = lineup.optimal_lineup(league, "t1", 3)
    assert lineup.lineup_changes(optimal, optimal) == []


def test_current_lineup_unknown_selected_slot_raises(league):
    broken = copy.deepcopy(league)
    for team in broken["teams"]:
        if team["team_id"] == "t1":
            team["roster"][0]["selected_slot"] = "NOT_A_SLOT"

    with pytest.raises(EngineError):
        lineup.current_lineup(broken, "t1", 3)


def test_current_lineup_overfilled_slot_raises(league):
    broken = copy.deepcopy(league)
    for team in broken["teams"]:
        if team["team_id"] == "t1":
            # p1002 (already RB) gets relabeled into the single count QB
            # slot alongside the real QB, p1001.
            team["roster"][1]["selected_slot"] = "QB"

    with pytest.raises(EngineError):
        lineup.current_lineup(broken, "t1", 3)


# ---------------------------------------------------------------------------
# extra_player_ids and excluded_player_ids together
# ---------------------------------------------------------------------------


def test_extra_and_excluded_player_ids_combine(league):
    baseline = lineup.optimal_lineup(league, "t1", 3)
    # p1013 is the lowest scoring bench player in the baseline optimal
    # lineup's bench_ids (already sorted points desc, so it is last).
    worst_bench_id = baseline["bench_ids"][-1]
    assert worst_bench_id == "p1013"

    result = lineup.optimal_lineup(
        league,
        "t1",
        3,
        extra_player_ids=["f2001"],
        excluded_player_ids=[worst_bench_id],
    )

    # p1013 is on IR and was never a startable candidate, so excluding him
    # costs nothing; the only change from baseline is f2001 entering and
    # winning a slot, a gain of 1.2 (see the free agent gain band test):
    # 91.4 + 1.2 = 92.6
    assert result["total_points"] == pytest.approx(92.6, abs=1e-6)
    assert worst_bench_id not in result["starter_ids"]
    assert worst_bench_id not in result["bench_ids"]


def test_unknown_extra_player_id_raises(league):
    with pytest.raises(EngineError):
        lineup.optimal_lineup(league, "t1", 3, extra_player_ids=["not_a_real_player"])


# ---------------------------------------------------------------------------
# Unfillable slot: does not crash
# ---------------------------------------------------------------------------


def test_unfillable_slot_reports_none_without_crashing(league):
    stripped = copy.deepcopy(league)
    for team in stripped["teams"]:
        if team["team_id"] == "t1":
            team["roster"] = [
                entry for entry in team["roster"] if entry["player_id"] != "p1008"
            ]

    result = lineup.optimal_lineup(stripped, "t1", 3)
    k_assignment = next(a for a in result["assignments"] if a["slot"] == "K")
    assert k_assignment["player_id"] is None
    assert k_assignment["points"] == 0.0
    assert k_assignment["startable"] is False

    other_assignments = [a for a in result["assignments"] if a["slot"] != "K"]
    assert all(a["player_id"] is not None for a in other_assignments)


# ---------------------------------------------------------------------------
# MAX_SLOT_UNITS guard rail
# ---------------------------------------------------------------------------


def test_too_many_starting_slot_units_raises(league):
    inflated = copy.deepcopy(league)
    for slot in inflated["settings"]["roster_slots"]:
        if slot["slot"] == "RB" and slot["starting"]:
            slot["count"] = 20

    assert len(fixtures.starting_slot_units(inflated)) > lineup.MAX_SLOT_UNITS

    with pytest.raises(EngineError):
        lineup.optimal_lineup(inflated, "t1", 3)


# ---------------------------------------------------------------------------
# lineup_changes: no phantom sits, no negative gains, sums reconcile
# ---------------------------------------------------------------------------


def test_lineup_changes_never_lists_a_player_as_both_start_and_sit(league):
    # A player who starts under both current and optimal must never be
    # reported as a change at all, regardless of which literal unit index
    # each solve happened to place him in (two same slot units are
    # interchangeable, see the module docstring on lineup_changes).
    for team_id in ["t1", "t2", "t3", "t4"]:
        for week in [3, 4]:
            optimal = lineup.optimal_lineup(league, team_id, week)
            current = lineup.current_lineup(league, team_id, week)
            changes = lineup.lineup_changes(current, optimal)

            starts = {c["start_player_id"] for c in changes}
            sits = {c["sit_player_id"] for c in changes if c["sit_player_id"] is not None}
            assert not (starts & sits), (team_id, week, changes)


def test_lineup_changes_never_negative_and_sums_to_the_total_gap(league):
    for team_id in ["t1", "t2", "t3", "t4"]:
        for week in [3, 4]:
            optimal = lineup.optimal_lineup(league, team_id, week)
            current = lineup.current_lineup(league, team_id, week)
            changes = lineup.lineup_changes(current, optimal)

            for change in changes:
                assert change["points_gained"] >= 0, (team_id, week, change)

            total_gained = round(sum(c["points_gained"] for c in changes), 2)
            expected = round(optimal["total_points"] - current["total_points"], 2)
            assert total_gained == pytest.approx(expected, abs=1e-6), (team_id, week)


def test_lineup_changes_t1_week3_reproduces_the_known_phantom_sit_bug(league):
    # This is the exact case QA reported: a manager's two RBs (p1002 at
    # unit 0, p1003 at unit 1) diffed strictly by unit index against the
    # optimal solve's own arbitrary unit order used to say start p1003 and
    # sit p1003 in the same list, with a negative points_gained alongside
    # it. p1003 starts in both current and optimal, so it must not appear
    # in the result at all.
    optimal = lineup.optimal_lineup(league, "t1", 3)
    current = lineup.current_lineup(league, "t1", 3)
    changes = lineup.lineup_changes(current, optimal)

    for change in changes:
        assert change["start_player_id"] != "p1003"
        assert change["sit_player_id"] != "p1003"


def test_lineup_changes_empty_when_current_is_optimal_with_units_swapped(league):
    # Simulate a manager whose lineup is already point optimal, but whose
    # two RB starters happen to sit in the opposite unit order from
    # whichever order the bitmask solver picked. lineup_changes must not
    # recommend two pointless swaps in that case; it must return [].
    optimal = lineup.optimal_lineup(league, "t1", 3)
    swapped = copy.deepcopy(optimal)

    rb_indices = [
        index for index, unit in enumerate(swapped["assignments"]) if unit["slot"] == "RB"
    ]
    assert len(rb_indices) == 2
    first, second = rb_indices
    for key in ("player_id", "name", "positions", "points", "startable"):
        swapped["assignments"][first][key], swapped["assignments"][second][key] = (
            swapped["assignments"][second][key],
            swapped["assignments"][first][key],
        )

    assert lineup.lineup_changes(swapped, optimal) == []


# ---------------------------------------------------------------------------
# lineup_changes: the toss up band
# ---------------------------------------------------------------------------


def test_lineup_changes_flags_a_toss_up_inside_the_band_but_not_outside(league):
    optimal = lineup.optimal_lineup(league, "t1", 3)
    current = lineup.current_lineup(league, "t1", 3)
    changes = lineup.lineup_changes(current, optimal)

    inside = [c for c in changes if 0 < c["points_gained"] < lineup.DEFAULT_TOSS_UP_MARGIN_POINTS]
    outside = [c for c in changes if c["points_gained"] >= lineup.DEFAULT_TOSS_UP_MARGIN_POINTS]
    assert inside, changes
    assert outside, changes

    for change in inside:
        assert change["toss_up"] is True
        assert change["toss_up_margin"] == lineup.DEFAULT_TOSS_UP_MARGIN_POINTS
        option_ids = {option["player_id"] for option in change["toss_up_options"]}
        assert option_ids == {change["start_player_id"], change["sit_player_id"]}

    for change in outside:
        assert "toss_up" not in change
        assert "toss_up_options" not in change


def test_lineup_changes_toss_up_margin_is_configurable(league):
    optimal = lineup.optimal_lineup(league, "t1", 3)
    current = lineup.current_lineup(league, "t1", 3)

    # A margin of 0 flags nothing, since every real change has a strictly
    # positive points_gained and the band check is strict on both ends.
    changes = lineup.lineup_changes(current, optimal, toss_up_margin_points=0.0)
    assert all("toss_up" not in c for c in changes)

    # A very wide margin flags every change that is not already zero.
    changes = lineup.lineup_changes(current, optimal, toss_up_margin_points=100.0)
    assert all(c.get("toss_up") is True for c in changes)


# ---------------------------------------------------------------------------
# is_startable: SUSP excludes, Q does not
# ---------------------------------------------------------------------------


def test_is_startable_false_for_susp_true_for_questionable(league):
    # p204 (t2 bench) carries status "SUSP" and p207 (t2 bench) carries
    # status "Q" in the fixture, so both branches of EXCLUDED_STATUSES'
    # "flagged but not necessarily excluded" behavior are exercised: SUSP
    # benches him regardless of projection, Q does not bench anyone.
    susp_player = fixtures.get_player(league, "p204")
    assert susp_player["status"] == "SUSP"
    assert lineup.is_startable(susp_player, 3) is False

    questionable_player = fixtures.get_player(league, "p207")
    assert questionable_player["status"] == "Q"
    assert lineup.is_startable(questionable_player, 3) is True
