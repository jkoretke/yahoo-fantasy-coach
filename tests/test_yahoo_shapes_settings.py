"""Tests for engine.yahoo_shapes: Yahoo league settings, scoring, roster
slot and waiver parsing.

Fixtures are loaded from fixtures/yahoo/ and fixtures/sample_league/ with
json.loads on the file text rather than engine.common.load_json, following
the same convention tests/test_sources_sleeper.py already uses, since
load_json requires a top level JSON object and this module's helper reads
files whose shape it should not need to assume in advance.

engine.yahoo_shapes must never import this repo's pinned Yahoo client
library and must never raise on bad input, so every public function gets
both a fixture-backed test and a garbage-input test proving its
documented empty return.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from engine.common import REPO_ROOT
from engine import yahoo_shapes


YAHOO_FIXTURES_DIR = REPO_ROOT / "fixtures" / "yahoo"
SAMPLE_LEAGUE_DIR = REPO_ROOT / "fixtures" / "sample_league"


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _league_settings_fixture() -> dict[str, Any]:
    return _load(YAHOO_FIXTURES_DIR / "league_settings.json")


def _league_metadata_fixture() -> dict[str, Any]:
    return _load(YAHOO_FIXTURES_DIR / "league_metadata.json")


# ---------------------------------------------------------------------------
# unwrap_yahoo_list
# ---------------------------------------------------------------------------


def test_unwrap_yahoo_list_list_of_wrappers():
    value = [{"stat": {"a": 1}}, {"stat": {"b": 2}}]
    assert yahoo_shapes.unwrap_yahoo_list(value, "stat") == [{"a": 1}, {"b": 2}]


def test_unwrap_yahoo_list_list_of_bare_dicts():
    value = [{"a": 1}, {"b": 2}]
    assert yahoo_shapes.unwrap_yahoo_list(value, "stat") == [{"a": 1}, {"b": 2}]


def test_unwrap_yahoo_list_single_wrapper_dict():
    value = {"stat": {"a": 1}}
    assert yahoo_shapes.unwrap_yahoo_list(value, "stat") == [{"a": 1}]


def test_unwrap_yahoo_list_single_bare_dict():
    value = {"a": 1}
    assert yahoo_shapes.unwrap_yahoo_list(value, "stat") == [{"a": 1}]


def test_unwrap_yahoo_list_none_returns_empty():
    assert yahoo_shapes.unwrap_yahoo_list(None, "stat") == []


def test_unwrap_yahoo_list_string_returns_empty():
    assert yahoo_shapes.unwrap_yahoo_list("garbage", "stat") == []


def test_unwrap_yahoo_list_int_returns_empty():
    assert yahoo_shapes.unwrap_yahoo_list(42, "stat") == []


def test_unwrap_yahoo_list_skips_non_dict_element():
    value = [{"stat": {"a": 1}}, "junk", 5, {"stat": {"b": 2}}]
    assert yahoo_shapes.unwrap_yahoo_list(value, "stat") == [{"a": 1}, {"b": 2}]


def test_unwrap_yahoo_list_skips_wrapper_with_non_dict_inner():
    value = [{"stat": "junk"}, {"stat": {"a": 1}}]
    assert yahoo_shapes.unwrap_yahoo_list(value, "stat") == [{"a": 1}]


# ---------------------------------------------------------------------------
# yahoo_stat_key
# ---------------------------------------------------------------------------


def test_yahoo_stat_key_passing_yards():
    assert yahoo_shapes.yahoo_stat_key("Passing Yards") == "passing_yards"


def test_yahoo_stat_key_reception_yards_aliases_to_receiving_yards():
    assert yahoo_shapes.yahoo_stat_key("Reception Yards") == "receiving_yards"


def test_yahoo_stat_key_sack_aliases_to_defense_sacks():
    assert yahoo_shapes.yahoo_stat_key("Sack") == "defense_sacks"


def test_yahoo_stat_key_interception_singular_aliases_to_defense_interceptions():
    assert yahoo_shapes.yahoo_stat_key("Interception") == "defense_interceptions"


def test_yahoo_stat_key_interceptions_plural_needs_no_alias():
    assert yahoo_shapes.yahoo_stat_key("Interceptions") == "interceptions"


def test_yahoo_stat_key_fumble_recovery_aliases_to_defense_fumble_recoveries():
    assert yahoo_shapes.yahoo_stat_key("Fumble Recovery") == "defense_fumble_recoveries"


def test_yahoo_stat_key_touchdown_aliases_to_defense_touchdowns():
    assert yahoo_shapes.yahoo_stat_key("Touchdown") == "defense_touchdowns"


def test_yahoo_stat_key_none_returns_empty_string():
    assert yahoo_shapes.yahoo_stat_key(None) == ""


def test_yahoo_stat_key_blank_returns_empty_string():
    assert yahoo_shapes.yahoo_stat_key("") == ""
    assert yahoo_shapes.yahoo_stat_key("   ") == ""


# ---------------------------------------------------------------------------
# parse_scoring_settings
# ---------------------------------------------------------------------------


def test_parse_scoring_settings_fixture_matches_expected_values():
    result = yahoo_shapes.parse_scoring_settings(_league_settings_fixture())

    expected = {
        "passing_yards": 0.04,
        "passing_touchdowns": 4.0,
        "interceptions": -2.0,
        "rushing_yards": 0.1,
        "rushing_touchdowns": 6.0,
        "receptions": 0.5,
        "receiving_yards": 0.1,
        "receiving_touchdowns": 6.0,
        "fumbles_lost": -2.0,
        "field_goals_made": 3.0,
        "extra_points_made": 1.0,
        "defense_sacks": 1.0,
        "defense_interceptions": 2.0,
        "defense_fumble_recoveries": 2.0,
        "defense_touchdowns": 6.0,
    }
    assert result["stats"] == expected
    assert result["unmapped"] == [{"stat_id": 78, "name": "Targets"}]


def test_parse_scoring_settings_stats_key_set_matches_sample_league_fixture():
    sample_league = _load(SAMPLE_LEAGUE_DIR / "league.json")
    expected_keys = set(sample_league["settings"]["scoring"]["stats"].keys())

    result = yahoo_shapes.parse_scoring_settings(_league_settings_fixture())

    assert set(result["stats"].keys()) == expected_keys


def test_parse_scoring_settings_no_modifier_is_skipped_not_unmapped():
    payload = {
        "stat_categories": {"stats": [{"stat": {"stat_id": 999, "name": "Mystery Stat"}}]},
        "stat_modifiers": {"stats": []},
    }
    result = yahoo_shapes.parse_scoring_settings(payload)
    assert result == {"stats": {}, "unmapped": []}


def test_parse_scoring_settings_unparseable_modifier_is_unmapped():
    payload = {
        "stat_categories": {"stats": [{"stat": {"stat_id": 4, "name": "Passing Yards"}}]},
        "stat_modifiers": {"stats": [{"stat": {"stat_id": 4, "value": "not-a-number"}}]},
    }
    result = yahoo_shapes.parse_scoring_settings(payload)
    assert result == {"stats": {}, "unmapped": [{"stat_id": 4, "name": "Passing Yards"}]}


def test_parse_scoring_settings_garbage_returns_empty():
    assert yahoo_shapes.parse_scoring_settings(None) == {"stats": {}, "unmapped": []}
    assert yahoo_shapes.parse_scoring_settings("garbage") == {"stats": {}, "unmapped": []}
    assert yahoo_shapes.parse_scoring_settings({}) == {"stats": {}, "unmapped": []}


def test_parse_scoring_settings_joins_across_mixed_stat_id_types():
    # A real Yahoo response may send stat_id as a string on one side (or
    # both) rather than the int the fixture happens to use everywhere.
    # The join must still succeed on str(stat_id), not silently fail.
    payload = {
        "stat_categories": {"stats": [{"stat": {"stat_id": "4", "name": "Passing Yards"}}]},
        "stat_modifiers": {"stats": [{"stat": {"stat_id": 4, "value": "0.04"}}]},
    }
    result = yahoo_shapes.parse_scoring_settings(payload)
    assert result == {"stats": {"passing_yards": 0.04}, "unmapped": []}


# ---------------------------------------------------------------------------
# parse_roster_slots
# ---------------------------------------------------------------------------


def test_parse_roster_slots_fixture_order_and_counts():
    slots = yahoo_shapes.parse_roster_slots(_league_settings_fixture())

    assert [s["slot"] for s in slots] == [
        "QB",
        "RB",
        "WR",
        "TE",
        "W/R/T",
        "K",
        "DEF",
        "BN",
        "IR",
    ]
    assert [s["count"] for s in slots] == [1, 2, 2, 1, 1, 1, 1, 6, 1]


def test_parse_roster_slots_bench_and_ir_not_starting_with_no_eligible_positions():
    slots = yahoo_shapes.parse_roster_slots(_league_settings_fixture())
    by_slot = {s["slot"]: s for s in slots}

    assert by_slot["BN"]["starting"] is False
    assert by_slot["BN"]["eligible_positions"] == []
    assert by_slot["IR"]["starting"] is False
    assert by_slot["IR"]["eligible_positions"] == []


def test_parse_roster_slots_flex_expands_to_real_positions():
    slots = yahoo_shapes.parse_roster_slots(_league_settings_fixture())
    by_slot = {s["slot"]: s for s in slots}

    flex = by_slot["W/R/T"]
    assert flex["eligible_positions"] == ["WR", "RB", "TE"]
    assert flex["starting"] is True


def test_parse_roster_slots_starting_slots_true():
    slots = yahoo_shapes.parse_roster_slots(_league_settings_fixture())
    by_slot = {s["slot"]: s for s in slots}

    for slot_name in ("QB", "RB", "WR", "TE", "K", "DEF"):
        assert by_slot[slot_name]["starting"] is True
        assert by_slot[slot_name]["eligible_positions"] == [slot_name]


def test_parse_roster_slots_garbage_returns_empty_list():
    assert yahoo_shapes.parse_roster_slots(None) == []
    assert yahoo_shapes.parse_roster_slots("garbage") == []
    assert yahoo_shapes.parse_roster_slots({}) == []


def test_parse_roster_slots_count_coercion():
    payload = {
        "roster_positions": [
            {"roster_position": {"count": "2", "position": "RB", "is_starting_position": 1}},
            {"roster_position": {"count": "junk", "position": "WR", "is_starting_position": 1}},
        ]
    }
    slots = yahoo_shapes.parse_roster_slots(payload)
    assert slots[0]["count"] == 2
    assert slots[1]["count"] == 0


def test_parse_roster_slots_starting_true_when_flags_absent():
    # A real Yahoo response may omit is_bench and is_starting_position
    # entirely on a starting slot; "starting" must still default to True.
    payload = {"roster_positions": [{"roster_position": {"count": 1, "position": "QB"}}]}
    slots = yahoo_shapes.parse_roster_slots(payload)
    assert slots[0]["starting"] is True


# ---------------------------------------------------------------------------
# parse_waiver_settings
# ---------------------------------------------------------------------------


def test_parse_waiver_settings_fixture_is_priority_type():
    result = yahoo_shapes.parse_waiver_settings(_league_settings_fixture())

    assert result["type"] == "priority"
    assert result["faab_budget"] is None
    assert result["faab_remaining"] == {}
    assert result["priority_order"] == []
    assert result["waiver_rule"] == "gametime"
    assert result["waiver_time"] == 2


def test_parse_waiver_settings_faab_variant():
    payload = dict(_league_settings_fixture())
    payload["uses_faab"] = "1"
    payload["faab_budget"] = 150

    result = yahoo_shapes.parse_waiver_settings(payload)

    assert result["type"] == "faab"
    assert result["faab_budget"] == 150
    assert result["faab_remaining"] == {}
    assert result["priority_order"] == []


def test_parse_waiver_settings_garbage_returns_documented_empty():
    expected = {
        "type": "priority",
        "faab_budget": None,
        "faab_remaining": {},
        "priority_order": [],
        "waiver_rule": "",
        "waiver_time": None,
    }
    assert yahoo_shapes.parse_waiver_settings(None) == expected
    assert yahoo_shapes.parse_waiver_settings("garbage") == expected
    assert yahoo_shapes.parse_waiver_settings(123) == expected


# ---------------------------------------------------------------------------
# parse_league_settings
# ---------------------------------------------------------------------------


def test_parse_league_settings_fixture_top_level_keys_and_brackets():
    result = yahoo_shapes.parse_league_settings(_league_settings_fixture())

    assert set(result.keys()) == {"scoring", "roster_slots", "waiver", "unmapped_stat_categories"}
    assert result["scoring"]["brackets"] == {}
    assert result["unmapped_stat_categories"] == [{"stat_id": 78, "name": "Targets"}]
    assert set(result["scoring"]["stats"].keys()) == {
        "passing_yards",
        "passing_touchdowns",
        "interceptions",
        "rushing_yards",
        "rushing_touchdowns",
        "receptions",
        "receiving_yards",
        "receiving_touchdowns",
        "fumbles_lost",
        "field_goals_made",
        "extra_points_made",
        "defense_sacks",
        "defense_interceptions",
        "defense_fumble_recoveries",
        "defense_touchdowns",
    }
    assert len(result["roster_slots"]) == 9
    assert result["waiver"]["type"] == "priority"


def test_parse_league_settings_garbage_returns_composed_empty_shape():
    expected = {
        "scoring": {"stats": {}, "brackets": {}},
        "roster_slots": [],
        "waiver": {
            "type": "priority",
            "faab_budget": None,
            "faab_remaining": {},
            "priority_order": [],
            "waiver_rule": "",
            "waiver_time": None,
        },
        "unmapped_stat_categories": [],
    }
    assert yahoo_shapes.parse_league_settings(None) == expected
    assert yahoo_shapes.parse_league_settings("garbage") == expected


# ---------------------------------------------------------------------------
# parse_league_metadata
# ---------------------------------------------------------------------------


def test_parse_league_metadata_fixture():
    result = yahoo_shapes.parse_league_metadata(_league_metadata_fixture())

    assert result["league_key"] == "461.l.524458"
    assert result["league_id"] == "524458"
    assert result["name"] == "Sample Yahoo League"
    assert result["season"] == 2025
    assert isinstance(result["season"], int)
    assert result["current_week"] == 3
    assert isinstance(result["current_week"], int)
    assert result["num_teams"] == 10
    assert isinstance(result["num_teams"], int)
    assert result["start_week"] == 1
    assert result["end_week"] == 17
    assert result["scoring_type"] == "head"
    assert result["url"] == "https://football.fantasysports.yahoo.com/f1/524458"


def test_parse_league_metadata_garbage_returns_documented_empty():
    expected = {
        "league_id": "",
        "league_key": "",
        "name": "",
        "season": None,
        "current_week": None,
        "num_teams": None,
        "start_week": None,
        "end_week": None,
        "scoring_type": "",
        "url": "",
    }
    assert yahoo_shapes.parse_league_metadata(None) == expected
    assert yahoo_shapes.parse_league_metadata("garbage") == expected
    assert yahoo_shapes.parse_league_metadata([]) == expected
    assert yahoo_shapes.parse_league_metadata({}) == expected
