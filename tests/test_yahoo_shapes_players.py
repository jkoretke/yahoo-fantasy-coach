"""Tests for engine.yahoo_shapes: Yahoo player, roster, matchup and
free-agent parsing.

Fixtures are loaded from fixtures/yahoo/ with json.loads on the file text
rather than engine.common.load_json, following the same convention
tests/test_yahoo_shapes_settings.py already uses, since load_json requires
a top level JSON object and league_players.json, free_agents.json and
league_matchups_week.json are all top level JSON lists.

engine.yahoo_shapes must never import this repo's pinned Yahoo client
library and must never raise on bad input, so every public function added
by this chunk gets both a fixture-backed test and a garbage-input test
proving its documented empty return.

Every expected normalized_name value is computed by calling
engine.sources.base.normalize_name directly, never hardcoded, so the
assertion cannot drift from the real function.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from engine.common import REPO_ROOT
from engine.sources.base import normalize_name
from engine import yahoo_shapes


YAHOO_FIXTURES_DIR = REPO_ROOT / "fixtures" / "yahoo"


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _players_fixture() -> list[dict[str, Any]]:
    return _load(YAHOO_FIXTURES_DIR / "league_players.json")


def _free_agents_fixture() -> list[dict[str, Any]]:
    return _load(YAHOO_FIXTURES_DIR / "free_agents.json")


def _roster_fixture() -> dict[str, Any]:
    return _load(YAHOO_FIXTURES_DIR / "team_roster_week.json")


def _matchups_fixture() -> list[dict[str, Any]]:
    return _load(YAHOO_FIXTURES_DIR / "league_matchups_week.json")


def _player_by_id(records: list[dict[str, Any]], player_id: str) -> dict[str, Any]:
    for record in records:
        if record["player_id"] == player_id:
            return record
    raise AssertionError(f"no parsed player with player_id {player_id!r}")


# ---------------------------------------------------------------------------
# parse_player
# ---------------------------------------------------------------------------


def test_parse_player_key_set_matches_yahoo_player_keys_for_all_fixture_records():
    for payload in _players_fixture():
        record = yahoo_shapes.parse_player(payload)
        assert record is not None
        assert set(record.keys()) == set(yahoo_shapes.YAHOO_PLAYER_KEYS)


def test_parse_player_marvin_harrison_jr_drops_generational_suffix():
    records = [yahoo_shapes.parse_player(p) for p in _players_fixture()]
    record = _player_by_id(records, "11604")
    assert record["name"] == "Marvin Harrison Jr."
    assert record["normalized_name"] == normalize_name("Marvin Harrison Jr.")
    assert "jr" not in record["normalized_name"].split()


def test_parse_player_amon_ra_st_brown_dict_form_positions_and_folded_name():
    records = [yahoo_shapes.parse_player(p) for p in _players_fixture()]
    record = _player_by_id(records, "7547")
    # Record 4 carries eligible_positions as the raw wrapped-dict form
    # {"position": "WR"}; see fixtures/yahoo/README.md.
    assert record["positions"] == ["WR"]
    assert record["normalized_name"] == normalize_name("Amon-Ra St. Brown")
    assert "-" not in record["normalized_name"]
    assert "." not in record["normalized_name"]


def test_parse_player_trey_mcbride_bare_string_positions():
    records = [yahoo_shapes.parse_player(p) for p in _players_fixture()]
    record = _player_by_id(records, "9509")
    # Record 5 carries eligible_positions as the raw bare string "TE".
    assert record["positions"] == ["TE"]
    assert record["normalized_name"] == normalize_name("Trey McBride")


def test_parse_player_james_conner_status_and_team():
    records = [yahoo_shapes.parse_player(p) for p in _players_fixture()]
    record = _player_by_id(records, "4986")
    assert record["status"] == "IR"
    assert record["status_full"] == "Injured Reserve"
    # Yahoo sends "Ari"; normalize_team_abbreviation upper-cases it.
    assert record["nfl_team"] == "ARI"


def test_parse_player_kansas_city_defense_positions_and_team():
    records = [yahoo_shapes.parse_player(p) for p in _players_fixture()]
    record = _player_by_id(records, "100024")
    assert record["positions"] == ["DEF"]
    assert record["nfl_team"] == "KC"
    assert record["name"] == "Kansas City"


def test_parse_player_bye_week_and_percent_owned_types():
    records = [yahoo_shapes.parse_player(p) for p in _players_fixture()]
    josh_allen = _player_by_id(records, "4984")
    assert josh_allen["bye_week"] == 7
    assert isinstance(josh_allen["bye_week"], int)
    # league_players.json records carry no percent_owned block at all.
    assert josh_allen["percent_owned"] is None


def test_parse_player_derives_id_from_player_key_when_player_id_missing():
    payload = {
        "player_key": "461.p.55555",
        "name": {"first": "No", "last": "IdField", "full": "No IdField"},
    }
    record = yahoo_shapes.parse_player(payload)
    assert record is not None
    assert record["player_id"] == "55555"


def test_parse_player_falls_back_to_first_last_when_full_blank():
    payload = {
        "player_id": "999",
        "name": {"first": "Jane", "last": "Doe", "full": ""},
    }
    record = yahoo_shapes.parse_player(payload)
    assert record is not None
    assert record["name"] == "Jane Doe"


def test_parse_player_returns_none_when_no_id_and_no_name():
    assert yahoo_shapes.parse_player({"status": "Q", "editorial_team_abbr": "SF"}) is None


def test_parse_player_garbage_input_returns_none():
    assert yahoo_shapes.parse_player(None) is None
    assert yahoo_shapes.parse_player("garbage") is None
    assert yahoo_shapes.parse_player(42) is None
    assert yahoo_shapes.parse_player([]) is None


# ---------------------------------------------------------------------------
# parse_player_list
# ---------------------------------------------------------------------------


def test_parse_player_list_fixture_count_and_order():
    result = yahoo_shapes.parse_player_list(_players_fixture())
    assert result["count"] == 6
    assert len(result["players"]) == 6
    assert [p["player_id"] for p in result["players"]] == [
        "4984",
        "4986",
        "11604",
        "7547",
        "9509",
        "100024",
    ]


def test_parse_player_list_accepts_wrapped_form():
    wrapped = [{"player": p} for p in _players_fixture()]
    wrapped_result = yahoo_shapes.parse_player_list(wrapped)
    bare_result = yahoo_shapes.parse_player_list(_players_fixture())
    assert wrapped_result == bare_result


def test_parse_player_list_accepts_dict_with_players_key():
    result = yahoo_shapes.parse_player_list({"players": _players_fixture()})
    assert result["count"] == 6


def test_parse_player_list_garbage_input_returns_empty():
    assert yahoo_shapes.parse_player_list("garbage") == {"players": [], "count": 0}
    assert yahoo_shapes.parse_player_list(None) == {"players": [], "count": 0}
    assert yahoo_shapes.parse_player_list(42) == {"players": [], "count": 0}


# ---------------------------------------------------------------------------
# parse_roster
# ---------------------------------------------------------------------------


def test_parse_roster_fixture():
    result = yahoo_shapes.parse_roster(_roster_fixture(), team_id="1")
    assert result["team_id"] == "1"
    assert result["week"] == 3
    assert result["coverage_type"] == "week"
    assert len(result["roster"]) == 4
    assert len(result["players"]) == 4

    by_id = {entry["player_id"]: entry for entry in result["roster"]}
    assert by_id["4986"]["selected_slot"] == "BN"
    assert by_id["11604"]["selected_slot"] == "W/R/T"


def test_parse_roster_entries_have_documented_shape():
    result = yahoo_shapes.parse_roster(_roster_fixture(), team_id="1")
    for entry in result["roster"]:
        assert set(entry.keys()) == {"player_id", "player_key", "selected_slot"}


def test_parse_roster_unwrapped_players_form_matches_wrapped():
    wrapped = _roster_fixture()
    unwrapped = copy.deepcopy(wrapped)
    unwrapped["players"] = [entry["player"] for entry in wrapped["players"]]

    wrapped_result = yahoo_shapes.parse_roster(wrapped, team_id="1")
    unwrapped_result = yahoo_shapes.parse_roster(unwrapped, team_id="1")
    assert wrapped_result == unwrapped_result


def test_parse_roster_garbage_input_returns_empty_lists():
    result = yahoo_shapes.parse_roster("garbage", team_id="1")
    assert result == {
        "team_id": "1",
        "week": None,
        "coverage_type": "",
        "roster": [],
        "players": [],
    }
    assert yahoo_shapes.parse_roster(None, team_id="7") == {
        "team_id": "7",
        "week": None,
        "coverage_type": "",
        "roster": [],
        "players": [],
    }


# ---------------------------------------------------------------------------
# parse_matchups
# ---------------------------------------------------------------------------


def test_parse_matchups_fixture():
    result = yahoo_shapes.parse_matchups(_matchups_fixture(), week=3)
    assert result["week"] == 3
    assert len(result["matchups"]) == 2
    team_id_pairs = [m["team_ids"] for m in result["matchups"]]
    assert team_id_pairs == [["1", "2"], ["3", "4"]]
    assert result["owner_team_id"] == "1"

    matchup_ids = [m["matchup_id"] for m in result["matchups"]]
    assert len(set(matchup_ids)) == 2


def test_parse_matchups_matchup_id_deterministic_across_calls():
    payload = _matchups_fixture()
    first = yahoo_shapes.parse_matchups(payload, week=3)
    second = yahoo_shapes.parse_matchups(payload, week=3)
    assert first == second
    first_ids = [m["matchup_id"] for m in first["matchups"]]
    second_ids = [m["matchup_id"] for m in second["matchups"]]
    assert first_ids == second_ids


def test_parse_matchups_skips_matchup_without_exactly_two_teams():
    payload = [
        {
            "week": "3",
            "status": "midevent",
            "teams": [{"team": {"team_id": "1", "team_key": "461.l.1.t.1"}}],
        }
    ]
    result = yahoo_shapes.parse_matchups(payload, week=3)
    assert result["matchups"] == []


def test_parse_matchups_unwrapped_teams_form_matches_wrapped():
    wrapped = _matchups_fixture()
    unwrapped = copy.deepcopy(wrapped)
    for entry in unwrapped:
        entry["teams"] = [t["team"] for t in entry["teams"]]

    wrapped_result = yahoo_shapes.parse_matchups(wrapped, week=3)
    unwrapped_result = yahoo_shapes.parse_matchups(unwrapped, week=3)
    assert wrapped_result == unwrapped_result


def test_parse_matchups_garbage_input_returns_empty():
    assert yahoo_shapes.parse_matchups("garbage", week=3) == {
        "week": 3,
        "matchups": [],
        "owner_team_id": "",
    }
    assert yahoo_shapes.parse_matchups(None, week=5) == {
        "week": 5,
        "matchups": [],
        "owner_team_id": "",
    }


# ---------------------------------------------------------------------------
# parse_free_agents
# ---------------------------------------------------------------------------


def test_parse_free_agents_fixture():
    result = yahoo_shapes.parse_free_agents(_free_agents_fixture())
    assert result["count"] == 2
    assert len(result["free_agents"]) == 2
    assert [fa["percent_owned"] for fa in result["free_agents"]] == [41.0, 4.0]
    for fa in result["free_agents"]:
        assert isinstance(fa["percent_owned"], float)
    assert set(result["free_agents"][0].keys()) == {"player_id", "percent_owned"}


def test_parse_free_agents_wrapped_form_matches_unwrapped_fixture():
    unwrapped = _free_agents_fixture()
    wrapped = [{"player": p} for p in unwrapped]

    unwrapped_result = yahoo_shapes.parse_free_agents(unwrapped)
    wrapped_result = yahoo_shapes.parse_free_agents(wrapped)
    assert unwrapped_result == wrapped_result


def test_parse_free_agents_defaults_percent_owned_to_zero_when_absent():
    payload = [
        {
            "player_id": "5001",
            "player_key": "461.p.5001",
            "name": {"first": "No", "last": "Ownership", "full": "No Ownership"},
        }
    ]
    result = yahoo_shapes.parse_free_agents(payload)
    assert result["free_agents"] == [{"player_id": "5001", "percent_owned": 0.0}]


def test_parse_free_agents_garbage_input_returns_empty():
    assert yahoo_shapes.parse_free_agents("garbage") == {
        "free_agents": [],
        "players": [],
        "count": 0,
    }
    assert yahoo_shapes.parse_free_agents(None) == {
        "free_agents": [],
        "players": [],
        "count": 0,
    }
