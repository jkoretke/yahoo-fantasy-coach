"""Tests for engine.identity: the Yahoo id to Sleeper id and ESPN injury join.

The Yahoo player input is built by loading fixtures/yahoo/league_players.json
(a plain JSON list) and running each record through
engine.yahoo_shapes.parse_player, exactly as a real caller would.

sleeper_index_data and injuries_data are built by running the REAL Phase 2
parsers (engine.sources.sleeper.fetch_player_index and
engine.sources.injuries.fetch_injuries) over the REAL Phase 2 fixtures,
never a hand-written fake dict, so these tests prove the real Phase 2
output shapes actually join. Following the repo's own established mocking
pattern (see tests/test_sources_injuries.py's _patched_fetch), each
module's own fetch_cached_json name is patched, since sleeper.py and
injuries.py both do "from engine.sources.base import fetch_cached_json"
and therefore hold their own bound reference; patching
engine.sources.base.fetch_cached_json would not affect either module.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from engine.common import REPO_ROOT
from engine import identity
from engine.sources import injuries, sleeper
from engine.sources.base import SOURCE_RESULT_KEYS, normalize_name
from engine.yahoo_shapes import parse_player

YAHOO_PLAYERS_FIXTURE = REPO_ROOT / "fixtures" / "yahoo" / "league_players.json"
SLEEPER_FIXTURE = REPO_ROOT / "fixtures" / "sources" / "sleeper_players.json"
INJURIES_FIXTURE = REPO_ROOT / "fixtures" / "sources" / "espn_injuries.json"

EMPTY_IDENTITY_MAP = {
    "players": {},
    "count": 0,
    "matched_sleeper": 0,
    "matched_injuries": 0,
    "unmatched_sleeper": [],
    "unmatched_injuries": [],
    "duplicate_normalized_names": [],
}


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def yahoo_players() -> list:
    raw = json.loads(YAHOO_PLAYERS_FIXTURE.read_text())
    return [parse_player(entry) for entry in raw]


@pytest.fixture
def sleeper_index_data(tmp_path: Path) -> dict:
    payload = json.loads(SLEEPER_FIXTURE.read_text())
    with patch(
        "engine.sources.sleeper.fetch_cached_json",
        return_value=(payload, "2026-09-10T12:00:00Z", False),
    ):
        return sleeper.fetch_player_index(cache_root=tmp_path)["data"]


@pytest.fixture
def injuries_data(tmp_path: Path) -> dict:
    payload = json.loads(INJURIES_FIXTURE.read_text())
    with patch(
        "engine.sources.injuries.fetch_cached_json",
        return_value=(payload, "2026-09-10T12:00:00Z", False),
    ):
        return injuries.fetch_injuries(cache_root=tmp_path)["data"]


@pytest.fixture
def identity_map(yahoo_players, sleeper_index_data, injuries_data) -> dict:
    return identity.build_identity_map(
        yahoo_players,
        sleeper_index_data=sleeper_index_data,
        injuries_data=injuries_data,
    )


# ---------------------------------------------------------------------------
# the six documented fixture outcomes
# ---------------------------------------------------------------------------


def test_josh_allen_matches_sleeper_and_has_no_injury(identity_map, sleeper_index_data) -> None:
    sleeper_lookup = sleeper.player_id_by_normalized_name(sleeper_index_data)
    expected_sleeper_id = sleeper_lookup[normalize_name("Josh Allen")]

    record = identity_map["players"]["4984"]
    assert record["sleeper_player_id"] == expected_sleeper_id
    assert record["nfl_team"] == "BUF"
    assert record["injury"] is None
    assert "4984" not in identity_map["unmatched_sleeper"]
    assert "4984" in identity_map["unmatched_injuries"]


def test_james_conner_matches_sleeper_and_gets_ir_injury(
    identity_map, sleeper_index_data, injuries_data
) -> None:
    sleeper_lookup = sleeper.player_id_by_normalized_name(sleeper_index_data)
    expected_sleeper_id = sleeper_lookup[normalize_name("James Conner")]
    expected_injury = injuries.status_for_player(injuries_data, "James Conner", "ARI")

    record = identity_map["players"]["4986"]
    assert record["sleeper_player_id"] == expected_sleeper_id
    assert record["injury"] == expected_injury
    assert expected_injury is not None
    assert record["injury"]["status"] == "IR"
    assert "4986" not in identity_map["unmatched_sleeper"]
    assert "4986" not in identity_map["unmatched_injuries"]


def test_marvin_harrison_jr_matches_via_generational_suffix_stripping(
    identity_map, sleeper_index_data
) -> None:
    # "Marvin Harrison Jr." only joins to Sleeper's "Marvin Harrison" entry
    # because normalize_name drops the trailing "jr" token from both sides.
    assert normalize_name("Marvin Harrison Jr.") == normalize_name("Marvin Harrison")

    sleeper_lookup = sleeper.player_id_by_normalized_name(sleeper_index_data)
    expected_sleeper_id = sleeper_lookup[normalize_name("Marvin Harrison Jr.")]

    record = identity_map["players"]["11604"]
    assert record["sleeper_player_id"] == expected_sleeper_id
    assert record["injury"] is None


def test_amon_ra_st_brown_matches_via_punctuation_stripping(
    identity_map, sleeper_index_data
) -> None:
    sleeper_lookup = sleeper.player_id_by_normalized_name(sleeper_index_data)
    expected_sleeper_id = sleeper_lookup[normalize_name("Amon-Ra St. Brown")]

    # The Sleeper fixture record for this player carries no team at all;
    # the join still succeeds because player_id_by_normalized_name is keyed
    # purely by normalized name.
    assert sleeper_index_data["players"][expected_sleeper_id]["nfl_team"] == ""

    record = identity_map["players"]["7547"]
    assert record["sleeper_player_id"] == expected_sleeper_id
    assert record["nfl_team"] == "DET"


def test_trey_mcbride_unmatched_sleeper_but_matched_injury(
    identity_map, injuries_data
) -> None:
    expected_injury = injuries.status_for_player(injuries_data, "Trey McBride", "ARI")

    record = identity_map["players"]["9509"]
    assert record["sleeper_player_id"] is None
    assert record["injury"] == expected_injury
    assert expected_injury is not None
    assert "9509" in identity_map["unmatched_sleeper"]
    assert "9509" not in identity_map["unmatched_injuries"]


def test_kansas_city_defense_matches_neither_feed(identity_map) -> None:
    record = identity_map["players"]["100024"]
    assert record["sleeper_player_id"] is None
    assert record["injury"] is None
    assert "100024" in identity_map["unmatched_sleeper"]
    assert "100024" in identity_map["unmatched_injuries"]


# ---------------------------------------------------------------------------
# counts, key sets, determinism
# ---------------------------------------------------------------------------


def test_counts_add_up_to_total(identity_map) -> None:
    assert identity_map["count"] == 6
    assert identity_map["matched_sleeper"] + len(identity_map["unmatched_sleeper"]) == identity_map["count"]
    assert identity_map["matched_injuries"] + len(identity_map["unmatched_injuries"]) == identity_map["count"]


def test_every_record_has_exactly_identity_record_keys(identity_map) -> None:
    for record in identity_map["players"].values():
        assert set(record.keys()) == set(identity.IDENTITY_RECORD_KEYS)


def test_unmatched_lists_are_sorted(identity_map) -> None:
    assert identity_map["unmatched_sleeper"] == sorted(identity_map["unmatched_sleeper"])
    assert identity_map["unmatched_injuries"] == sorted(identity_map["unmatched_injuries"])
    assert identity_map["duplicate_normalized_names"] == sorted(identity_map["duplicate_normalized_names"])


# ---------------------------------------------------------------------------
# match_injury_team
# ---------------------------------------------------------------------------


def test_match_injury_team_toggle(injuries_data) -> None:
    # None of the six fixture players can demonstrate match_injury_team:
    # James Conner and Trey McBride both already match ESPN on ARI whether
    # or not the team is passed through. A synthetic Yahoo-shaped record is
    # used instead: the ESPN fixture carries a Terry Wilson on WSH, and
    # this hand-built record claims DAL, so the two nfl_team values
    # disagree only when match_injury_team actually gates the lookup.
    synthetic_record = {
        "player_id": "999001",
        "player_key": "461.p.999001",
        "name": "Terry Wilson",
        "normalized_name": normalize_name("Terry Wilson"),
        "positions": ["WR"],
        "primary_position": "WR",
        "nfl_team": "DAL",
        "status": "",
        "status_full": "",
        "bye_week": None,
        "percent_owned": None,
        "selected_slot": "",
    }

    team_enforced = identity.build_identity_map(
        [synthetic_record], injuries_data=injuries_data, match_injury_team=True
    )
    team_ignored = identity.build_identity_map(
        [synthetic_record], injuries_data=injuries_data, match_injury_team=False
    )

    assert team_enforced["players"]["999001"]["injury"] is None

    expected_injury = injuries.status_for_player(injuries_data, "Terry Wilson", None)
    assert expected_injury is not None
    assert expected_injury["nfl_team"] == "WAS"  # WSH normalizes to WAS
    assert team_ignored["players"]["999001"]["injury"] == expected_injury


# ---------------------------------------------------------------------------
# duplicate_normalized_names
# ---------------------------------------------------------------------------


def test_duplicate_normalized_names_populated_and_both_records_kept() -> None:
    record_a = {
        "player_id": "d1",
        "name": "Mike Williams",
        "normalized_name": normalize_name("Mike Williams"),
        "nfl_team": "NYJ",
        "positions": ["WR"],
    }
    record_b = {
        "player_id": "d2",
        "name": "Mike Williams",
        "normalized_name": normalize_name("Mike Williams"),
        "nfl_team": "LAC",
        "positions": ["WR"],
    }

    result = identity.build_identity_map([record_a, record_b])

    assert result["duplicate_normalized_names"] == [normalize_name("Mike Williams")]
    assert set(result["players"].keys()) == {"d1", "d2"}
    assert result["players"]["d1"]["nfl_team"] == "NYJ"
    assert result["players"]["d2"]["nfl_team"] == "LAC"


def test_no_duplicate_when_normalized_names_differ() -> None:
    record_a = {"player_id": "e1", "name": "Mike Williams", "nfl_team": "NYJ", "positions": []}
    record_b = {"player_id": "e2", "name": "Mike Evans", "nfl_team": "TB", "positions": []}
    result = identity.build_identity_map([record_a, record_b])
    assert result["duplicate_normalized_names"] == []


# ---------------------------------------------------------------------------
# hand-built records: tolerating missing keys, recomputing derived fields
# ---------------------------------------------------------------------------


def test_hand_built_record_without_normalized_name_still_joins(sleeper_index_data) -> None:
    # engine.yahoo_shapes.parse_player always supplies normalized_name
    # already computed, and an already-canonical nfl_team, so only a
    # hand-built record can prove the join recomputes both itself: this
    # record gives a raw, un-normalized "Buf" and omits normalized_name
    # entirely.
    bare_record = {"player_id": "4984", "name": "Josh Allen", "nfl_team": "Buf"}

    result = identity.build_identity_map([bare_record], sleeper_index_data=sleeper_index_data)
    record = result["players"]["4984"]

    sleeper_lookup = sleeper.player_id_by_normalized_name(sleeper_index_data)
    assert record["normalized_name"] == normalize_name("Josh Allen")
    assert record["sleeper_player_id"] == sleeper_lookup[normalize_name("Josh Allen")]
    assert record["nfl_team"] == "BUF"  # raw "Buf" normalized by the join itself


def test_record_with_only_a_player_id_still_builds(sleeper_index_data, injuries_data) -> None:
    # The join must tolerate a record missing every key except a usable
    # player_id, degrading every other field to its documented default
    # rather than raising or skipping the record.
    result = identity.build_identity_map(
        [{"player_id": "42"}], sleeper_index_data=sleeper_index_data, injuries_data=injuries_data
    )
    record = result["players"]["42"]

    assert set(record.keys()) == set(identity.IDENTITY_RECORD_KEYS)
    assert record["name"] == ""
    assert record["normalized_name"] == ""
    assert record["yahoo_player_key"] == ""
    assert record["positions"] == []
    assert record["nfl_team"] == ""
    assert record["sleeper_player_id"] is None
    assert record["injury"] is None
    assert result["count"] == 1


# ---------------------------------------------------------------------------
# sleeper_id_for_yahoo_id / injury_for_yahoo_id
# ---------------------------------------------------------------------------


def test_lookup_helpers_return_real_values(identity_map) -> None:
    assert identity.sleeper_id_for_yahoo_id(identity_map, "4984") == (
        identity_map["players"]["4984"]["sleeper_player_id"]
    )
    assert identity.injury_for_yahoo_id(identity_map, "4986") == (
        identity_map["players"]["4986"]["injury"]
    )


def test_lookup_helpers_return_none_for_unknown_id(identity_map) -> None:
    assert identity.sleeper_id_for_yahoo_id(identity_map, "not-a-real-id") is None
    assert identity.injury_for_yahoo_id(identity_map, "not-a-real-id") is None


@pytest.mark.parametrize(
    "garbage_identity_data",
    [None, "garbage", 42, [], {"players": "not a dict"}, {"players": {"4984": "not a dict"}}, {}],
)
def test_lookup_helpers_return_none_for_garbage(garbage_identity_data) -> None:
    assert identity.sleeper_id_for_yahoo_id(garbage_identity_data, "4984") is None
    assert identity.injury_for_yahoo_id(garbage_identity_data, "4984") is None


def test_lookup_helpers_tolerate_unhashable_yahoo_player_id(identity_map) -> None:
    assert identity.sleeper_id_for_yahoo_id(identity_map, ["not", "hashable"]) is None
    assert identity.injury_for_yahoo_id(identity_map, ["not", "hashable"]) is None


# ---------------------------------------------------------------------------
# identity_result envelope
# ---------------------------------------------------------------------------


def test_identity_result_envelope_matches_source_result_keys(
    yahoo_players, sleeper_index_data, injuries_data
) -> None:
    result = identity.identity_result(
        yahoo_players, sleeper_index_data=sleeper_index_data, injuries_data=injuries_data
    )

    assert set(result.keys()) == set(SOURCE_RESULT_KEYS)
    assert result["source"] == identity.SOURCE_NAME
    assert result["available"] is True
    assert result["stale"] is False
    assert result["reason"] is None
    assert isinstance(result["fetched_at"], str) and result["fetched_at"]
    assert result["data"]["count"] == 6
    assert json.dumps(result)


def test_identity_result_uses_given_fetched_at(yahoo_players) -> None:
    result = identity.identity_result(yahoo_players, fetched_at="2026-01-01T00:00:00Z")
    assert result["fetched_at"] == "2026-01-01T00:00:00Z"


# ---------------------------------------------------------------------------
# garbage input to build_identity_map
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_input",
    [
        None,
        "garbage",
        42,
        3.14,
        [],
        {},
        {"players": "not a list"},
        {"data": None},
        {"data": {"players": "not a list"}},
        [{"no_player_id_here": "x"}, "not a dict either", None],
        [{"player_id": None}, {"player_id": ""}, {"player_id": "   "}],
    ],
)
def test_build_identity_map_garbage_input_returns_documented_empty_result(bad_input) -> None:
    assert identity.build_identity_map(bad_input) == EMPTY_IDENTITY_MAP


def test_build_identity_map_never_raises_on_garbage_side_inputs(yahoo_players) -> None:
    # sleeper_index_data / injuries_data being garbage must degrade quietly
    # rather than raise, since both engine.sources.sleeper.player_id_by_normalized_name
    # and engine.sources.injuries.status_for_player already document this.
    result = identity.build_identity_map(
        yahoo_players, sleeper_index_data="garbage", injuries_data=12345
    )
    assert result["count"] == 6
    assert result["matched_sleeper"] == 0
    assert result["matched_injuries"] == 0


# ---------------------------------------------------------------------------
# the three accepted yahoo_players shapes
# ---------------------------------------------------------------------------


def test_three_accepted_input_shapes_produce_identical_results(
    yahoo_players, sleeper_index_data, injuries_data
) -> None:
    plain_list_shape = yahoo_players
    players_dict_shape = {"players": yahoo_players, "count": len(yahoo_players)}
    envelope_shape = {
        "source": "yahoo",
        "available": True,
        "stale": False,
        "reason": None,
        "fetched_at": "2026-09-10T12:00:00Z",
        "data": players_dict_shape,
    }

    kwargs = dict(sleeper_index_data=sleeper_index_data, injuries_data=injuries_data)
    result_list = identity.build_identity_map(plain_list_shape, **kwargs)
    result_dict = identity.build_identity_map(players_dict_shape, **kwargs)
    result_envelope = identity.build_identity_map(envelope_shape, **kwargs)

    assert result_list == result_dict == result_envelope
    assert result_list["count"] == 6


# ---------------------------------------------------------------------------
# no second normalization scheme
# ---------------------------------------------------------------------------


def test_module_defines_no_second_name_normalization_scheme() -> None:
    # The join must key off engine.sources.base.normalize_name alone. A
    # second, home-grown normalization scheme here (its own unicodedata
    # folding, or its own "def normalize..." function) would let this
    # join silently drift from what Sleeper's and ESPN's own modules
    # already consider a match.
    source_text = Path(identity.__file__).read_text()
    assert "unicodedata" not in source_text
    assert "def normalize" not in source_text
