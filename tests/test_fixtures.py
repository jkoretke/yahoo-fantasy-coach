"""Tests for engine.fixtures: one test per public function, plus the
structural integrity checks that are the real guard on the frozen fixture
schema. Every test reads fixtures/sample_league/ through load_fixture_league
rather than inlining duplicate sample data.
"""
from __future__ import annotations

import pytest

from engine.common import EngineError
from engine import fixtures


STARTABLE_BAD_STATUS = {"O", "IR", "SUSP"}


def _startable_candidates(league, team_id):
    """(player_id, positions) pairs for team_id's week 3 startable players."""
    result = []
    for pid in fixtures.team_roster_player_ids(league, team_id):
        player = fixtures.get_player(league, pid)
        if player["status"] in STARTABLE_BAD_STATUS:
            continue
        if player["bye_week"] == 3:
            continue
        result.append((pid, player["positions"]))
    return result


def _can_fill_all_units(candidates, units):
    """Small recursive legality check: can these candidates fill every unit."""

    def eligible(unit, positions):
        return any(p in unit["eligible_positions"] for p in positions)

    def rec(idx, used):
        if idx == len(units):
            return True
        unit = units[idx]
        for pid, positions in candidates:
            if pid in used:
                continue
            if eligible(unit, positions):
                if rec(idx + 1, used | {pid}):
                    return True
        return False

    return rec(0, frozenset())


# ---------------------------------------------------------------------------
# load_fixture_league
# ---------------------------------------------------------------------------


def test_load_fixture_league_has_exactly_eleven_top_level_keys():
    league = fixtures.load_fixture_league()
    expected = {
        "league_id", "name", "season", "current_week", "num_teams",
        "settings", "players", "teams", "matchups", "projections",
        "free_agents",
    }
    assert set(league.keys()) == expected


def test_load_fixture_league_scalars_and_lists():
    league = fixtures.load_fixture_league()
    assert league["league_id"] == "sample.l.100001"
    assert league["current_week"] == 3
    assert league["num_teams"] == 4
    assert isinstance(league["players"], list) and len(league["players"]) > 0
    assert isinstance(league["teams"], list) and len(league["teams"]) == 4
    assert isinstance(league["matchups"], list) and len(league["matchups"]) > 0
    assert isinstance(league["projections"], list) and len(league["projections"]) > 0
    assert isinstance(league["free_agents"], list) and len(league["free_agents"]) == 10


def test_load_fixture_league_default_dir_is_sample_league():
    assert fixtures.DEFAULT_FIXTURE_DIR == fixtures.FIXTURES_ROOT / "sample_league"
    league = fixtures.load_fixture_league(fixtures.DEFAULT_FIXTURE_DIR)
    assert league["league_id"] == "sample.l.100001"


def test_load_fixture_league_missing_file_raises(tmp_path):
    (tmp_path / "league.json").write_text("{}", encoding="utf-8")
    with pytest.raises(EngineError):
        fixtures.load_fixture_league(tmp_path)


def test_load_fixture_league_waiver_type_priority_flips_type():
    league = fixtures.load_fixture_league(waiver_type="priority")
    assert league["settings"]["waiver"]["type"] == "priority"


def test_load_fixture_league_waiver_type_faab_keeps_type():
    league = fixtures.load_fixture_league(waiver_type="faab")
    assert league["settings"]["waiver"]["type"] == "faab"


def test_load_fixture_league_waiver_type_default_is_faab():
    league = fixtures.load_fixture_league()
    assert league["settings"]["waiver"]["type"] == "faab"


def test_load_fixture_league_invalid_waiver_type_raises():
    with pytest.raises(EngineError):
        fixtures.load_fixture_league(waiver_type="nonsense")


def test_load_fixture_league_waiver_override_does_not_mutate_default_dir_files():
    # Loading with an override, then loading fresh again, must not leak state.
    fixtures.load_fixture_league(waiver_type="priority")
    league = fixtures.load_fixture_league()
    assert league["settings"]["waiver"]["type"] == "faab"


# ---------------------------------------------------------------------------
# get_player / get_team
# ---------------------------------------------------------------------------


def test_get_player_returns_record():
    league = fixtures.load_fixture_league()
    player = fixtures.get_player(league, "p1001")
    assert player["player_id"] == "p1001"
    assert isinstance(player["positions"], list)


def test_get_player_missing_raises_naming_id():
    league = fixtures.load_fixture_league()
    with pytest.raises(EngineError, match="pXXXX"):
        fixtures.get_player(league, "pXXXX")


def test_get_team_returns_record():
    league = fixtures.load_fixture_league()
    team = fixtures.get_team(league, "t1")
    assert team["team_id"] == "t1"
    assert team["is_owner_team"] is True


def test_get_team_missing_raises_naming_id():
    league = fixtures.load_fixture_league()
    with pytest.raises(EngineError, match="tXXXX"):
        fixtures.get_team(league, "tXXXX")


# ---------------------------------------------------------------------------
# owner_team_id
# ---------------------------------------------------------------------------


def test_owner_team_id_is_t1():
    league = fixtures.load_fixture_league()
    assert fixtures.owner_team_id(league) == "t1"


def test_owner_team_id_raises_when_zero_owners():
    league = fixtures.load_fixture_league()
    for team in league["teams"]:
        team["is_owner_team"] = False
    with pytest.raises(EngineError):
        fixtures.owner_team_id(league)


def test_owner_team_id_raises_when_two_owners():
    league = fixtures.load_fixture_league()
    league["teams"][1]["is_owner_team"] = True
    with pytest.raises(EngineError):
        fixtures.owner_team_id(league)


# ---------------------------------------------------------------------------
# team_roster_player_ids
# ---------------------------------------------------------------------------


def test_team_roster_player_ids_t1_has_thirteen():
    league = fixtures.load_fixture_league()
    ids = fixtures.team_roster_player_ids(league, "t1")
    assert len(ids) == 13
    assert len(set(ids)) == 13


def test_team_roster_player_ids_other_teams_have_twelve():
    league = fixtures.load_fixture_league()
    for team_id in ("t2", "t3", "t4"):
        ids = fixtures.team_roster_player_ids(league, team_id)
        assert len(ids) == 12
        assert len(set(ids)) == 12


# ---------------------------------------------------------------------------
# free_agent_ids
# ---------------------------------------------------------------------------


def test_free_agent_ids_returns_ten():
    league = fixtures.load_fixture_league()
    ids = fixtures.free_agent_ids(league)
    assert len(ids) == 10
    assert all(pid.startswith("f") for pid in ids)


def test_free_agent_ids_include_the_seam_players():
    league = fixtures.load_fixture_league()
    ids = fixtures.free_agent_ids(league)
    assert "f2001" in ids
    assert "f2002" in ids


# ---------------------------------------------------------------------------
# projections_for_week
# ---------------------------------------------------------------------------


def test_projections_for_week_three_covers_every_player():
    league = fixtures.load_fixture_league()
    week3 = fixtures.projections_for_week(league, 3)
    all_ids = {p["player_id"] for p in league["players"]}
    assert set(week3.keys()) == all_ids


def test_projections_for_week_four_covers_every_player():
    league = fixtures.load_fixture_league()
    week4 = fixtures.projections_for_week(league, 4)
    all_ids = {p["player_id"] for p in league["players"]}
    assert set(week4.keys()) == all_ids


def test_projections_for_week_two_covers_owner_team_only():
    league = fixtures.load_fixture_league()
    week2 = fixtures.projections_for_week(league, 2)
    assert len(week2) > 0
    owner_ids = set(fixtures.team_roster_player_ids(league, fixtures.owner_team_id(league)))
    assert set(week2.keys()) == owner_ids


def test_projections_for_week_with_no_data_raises():
    league = fixtures.load_fixture_league()
    with pytest.raises(EngineError, match="9"):
        fixtures.projections_for_week(league, 9)


# ---------------------------------------------------------------------------
# starting_slot_units
# ---------------------------------------------------------------------------


def test_starting_slot_units_returns_nine_units():
    league = fixtures.load_fixture_league()
    units = fixtures.starting_slot_units(league)
    assert len(units) == 9


def test_starting_slot_units_excludes_bench_and_ir():
    league = fixtures.load_fixture_league()
    units = fixtures.starting_slot_units(league)
    slot_names = {unit["slot"] for unit in units}
    assert "BN" not in slot_names
    assert "IR" not in slot_names


def test_starting_slot_units_has_expected_slot_counts():
    league = fixtures.load_fixture_league()
    units = fixtures.starting_slot_units(league)
    counts = {}
    for unit in units:
        counts[unit["slot"]] = counts.get(unit["slot"], 0) + 1
    assert counts == {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "W/R/T": 1, "K": 1, "DEF": 1}


# ---------------------------------------------------------------------------
# Structural integrity of the fixture data itself
# ---------------------------------------------------------------------------


def test_every_referenced_player_id_exists_in_players_json():
    league = fixtures.load_fixture_league()
    all_ids = {p["player_id"] for p in league["players"]}
    for team in league["teams"]:
        for entry in team["roster"]:
            assert entry["player_id"] in all_ids
    for fa in league["free_agents"]:
        assert fa["player_id"] in all_ids
    for proj in league["projections"]:
        assert proj["player_id"] in all_ids


def test_no_player_id_appears_on_two_rosters():
    league = fixtures.load_fixture_league()
    seen = set()
    for team in league["teams"]:
        for entry in team["roster"]:
            pid = entry["player_id"]
            assert pid not in seen, f"{pid} rostered on two teams"
            seen.add(pid)


def test_no_rostered_player_is_also_a_free_agent():
    league = fixtures.load_fixture_league()
    rostered = set()
    for team in league["teams"]:
        rostered.update(entry["player_id"] for entry in team["roster"])
    fa_ids = set(fixtures.free_agent_ids(league))
    assert rostered.isdisjoint(fa_ids)


def test_every_player_has_week_three_and_week_four_projection():
    league = fixtures.load_fixture_league()
    week3 = fixtures.projections_for_week(league, 3)
    week4 = fixtures.projections_for_week(league, 4)
    for player in league["players"]:
        pid = player["player_id"]
        assert pid in week3, f"{pid} missing week 3 projection"
        assert pid in week4, f"{pid} missing week 4 projection"


def test_every_team_roster_fills_declared_slot_counts():
    league = fixtures.load_fixture_league()
    declared = {slot["slot"]: slot["count"] for slot in league["settings"]["roster_slots"]}
    for team in league["teams"]:
        slot_counts = {}
        for entry in team["roster"]:
            slot_counts[entry["selected_slot"]] = slot_counts.get(entry["selected_slot"], 0) + 1
        for slot_name, count in slot_counts.items():
            assert count <= declared[slot_name], (
                f"{team['team_id']} has {count} in {slot_name}, "
                f"declared max is {declared[slot_name]}"
            )
        for slot_name, declared_count in declared.items():
            if slot_name == "IR":
                expected = 1 if team["team_id"] == "t1" else 0
            else:
                expected = declared_count
            assert slot_counts.get(slot_name, 0) == expected, (
                f"{team['team_id']} has {slot_counts.get(slot_name, 0)} in "
                f"{slot_name}, expected exactly {expected}"
            )


def test_every_team_can_fill_all_nine_starting_units_week_three():
    league = fixtures.load_fixture_league()
    units = fixtures.starting_slot_units(league)
    for team in league["teams"]:
        team_id = team["team_id"]
        candidates = _startable_candidates(league, team_id)
        assert _can_fill_all_units(candidates, units), (
            f"{team_id} cannot fill all nine starting units with its "
            f"week 3 startable players"
        )


def test_all_ten_free_agents_are_startable_week_three():
    league = fixtures.load_fixture_league()
    for fa in league["free_agents"]:
        player = fixtures.get_player(league, fa["player_id"])
        assert player["status"] == ""
        assert player["bye_week"] != 3


def test_exactly_one_owner_team_and_it_is_t1():
    league = fixtures.load_fixture_league()
    owners = [t["team_id"] for t in league["teams"] if t["is_owner_team"]]
    assert owners == ["t1"]
    assert fixtures.owner_team_id(league) == "t1"


def test_t1_ir_slot_player_has_ir_status():
    league = fixtures.load_fixture_league()
    team = fixtures.get_team(league, "t1")
    ir_entries = [e for e in team["roster"] if e["selected_slot"] == "IR"]
    assert len(ir_entries) == 1
    player = fixtures.get_player(league, ir_entries[0]["player_id"])
    assert player["status"] == "IR"


def test_t1_has_a_bye_week_three_starter_and_an_out_starter():
    league = fixtures.load_fixture_league()
    team = fixtures.get_team(league, "t1")
    starting_slots = {u["slot"] for u in fixtures.starting_slot_units(league)}
    bye3_starters = []
    out_starters = []
    for entry in team["roster"]:
        if entry["selected_slot"] not in starting_slots:
            continue
        player = fixtures.get_player(league, entry["player_id"])
        if player["bye_week"] == 3:
            bye3_starters.append(player)
        if player["status"] == "O":
            out_starters.append(player)
    assert len(bye3_starters) == 1
    assert len(out_starters) == 1


def test_stat_keys_used_in_projections_are_known_or_deliberately_unknown():
    league = fixtures.load_fixture_league()
    known = set(league["settings"]["scoring"]["stats"].keys())
    known.add("defense_points_allowed")
    unknown_keys_seen = set()
    for proj in league["projections"]:
        for key in proj["stats"]:
            if key not in known:
                unknown_keys_seen.add(key)
    assert unknown_keys_seen, "expected at least one deliberate unknown stat key"
    assert unknown_keys_seen == {"targets"}


def test_week_three_projected_points_have_no_ties_league_wide():
    league = fixtures.load_fixture_league()
    stats = league["settings"]["scoring"]["stats"]
    brackets = league["settings"]["scoring"]["brackets"]["defense_points_allowed"]

    def bracket_points(value):
        for low, high, pts in brackets:
            if low <= value <= high:
                return pts
        raise EngineError(f"no bracket matches {value}")

    def project(player_stats):
        total = 0.0
        for key, value in player_stats.items():
            if key == "defense_points_allowed":
                total += bracket_points(value)
            elif key in stats:
                total += stats[key] * value
        return round(total, 2)

    week3 = fixtures.projections_for_week(league, 3)
    totals = [project(s) for s in week3.values()]
    assert len(totals) == len(set(totals)), "duplicate week 3 point totals found"


def test_defense_points_allowed_matches_a_bracket_in_every_week():
    league = fixtures.load_fixture_league()
    brackets = league["settings"]["scoring"]["brackets"]["defense_points_allowed"]

    def matches_some_bracket(value):
        return any(low <= value <= high for low, high, _pts in brackets)

    checked = 0
    for proj in league["projections"]:
        value = proj["stats"].get("defense_points_allowed")
        if value is None:
            continue
        checked += 1
        assert matches_some_bracket(value), (
            f"week {proj['week']} player {proj['player_id']} has "
            f"defense_points_allowed={value} matching no bracket"
        )
    assert checked > 0, "expected at least one defense_points_allowed value to check"
