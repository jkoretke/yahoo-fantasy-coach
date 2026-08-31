"""Tests for engine.live_league: the real-data equivalent of
engine.fixtures.load_fixture_league.

Yahoo's Fantasy Sports API access application for this project was still
pending review when this module was written, so no test here may make a
real network call and none does: tests/conftest.py's autouse
block_real_network fixture already fails any unpatched urllib.request.
urlopen, and every engine.yahoo_client.fetch_ function, both engine.
sources.sleeper.fetch_ functions and engine.sources.injuries.
fetch_injuries are monkeypatched directly (via _patch_all_fetches below)
to return canned source_result envelopes before engine.live_league's own
functions are ever called, so those functions never reach a network layer
at all during a test.

Every canned envelope's "data" is built from the REAL fixture files
already checked in at fixtures/yahoo/ and fixtures/sources/, run through
the REAL parsers those fixtures already have (engine.yahoo_shapes.parse_*
for the Yahoo ones; engine.sources.sleeper.fetch_player_index/
fetch_projections and engine.sources.injuries.fetch_injuries themselves,
called with only their own fetch_cached_json patched out, matching
tests/test_identity.py's established convention), never a hand written
fake data dict, so a test here cannot drift from what those real parsers
actually produce. Nothing is added to fixtures/yahoo/ or fixtures/sources/;
one test (test_injury_status_overrides_yahoo_status_even_when_active)
builds one small ESPN injury record in Python, not from a file, since it
targets a specific edge case (an ESPN "active" record, whose own status
code is "", must still overwrite a non-blank Yahoo status) that the
checked-in espn_injuries.json fixture does not happen to exercise for the
one Yahoo player this test needs it for.

Fixture week alignment: fixtures/yahoo/league_metadata.json,
team_roster_week.json and league_matchups_week.json are all week 3;
fixtures/sources/sleeper_projections.json's entries carry their own
internal "week": 4, but engine.sources.sleeper.fetch_projections stamps
its OWN "week" onto the returned data from its own week argument, not
from any entry's internal field (verified against
engine/sources/sleeper.py directly), so every test below calls
fetch_projections with week=3 to match the rest of the fixture set, and
that internal "week": 4 is simply never read.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from engine import identity as identity_module
from engine import live_league
from engine import yahoo_client
from engine import yahoo_shapes
from engine.brief import build_brief
from engine.common import REPO_ROOT
from engine.scoring import projected_points_by_player
from engine.sources import base as sources_base
from engine.sources import injuries as injuries_source
from engine.sources import sleeper as sleeper_source
from engine.sources.base import normalize_name

YAHOO_FIXTURES_DIR = REPO_ROOT / "fixtures" / "yahoo"
SOURCES_FIXTURES_DIR = REPO_ROOT / "fixtures" / "sources"

ELEVEN_KEYS_IN_ORDER = (
    "league_id",
    "name",
    "season",
    "current_week",
    "num_teams",
    "settings",
    "players",
    "teams",
    "matchups",
    "projections",
    "free_agents",
)

DEFAULT_LEAGUE_ID = "524458"
DEFAULT_SEASON = 2025
DEFAULT_WEEK = 3
DEFAULT_TEAM_IDS = ["1", "2", "3", "4"]
FETCHED_AT = "2026-09-10T12:00:00Z"


def _load_yahoo(name: str) -> Any:
    return json.loads((YAHOO_FIXTURES_DIR / name).read_text(encoding="utf-8"))


def _load_sources(name: str) -> Any:
    return json.loads((SOURCES_FIXTURES_DIR / name).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Canned envelope builders. Each one runs the real parser over the real
# fixture file and wraps the result in engine.sources.base.source_result,
# the same envelope shape every real fetch_ function returns.
# ---------------------------------------------------------------------------


def _metadata_envelope() -> dict[str, Any]:
    data = yahoo_shapes.parse_league_metadata(_load_yahoo("league_metadata.json"))
    return sources_base.source_result("yahoo", data=data, fetched_at=FETCHED_AT)


def _settings_envelope() -> dict[str, Any]:
    data = yahoo_shapes.parse_league_settings(_load_yahoo("league_settings.json"))
    return sources_base.source_result("yahoo", data=data, fetched_at=FETCHED_AT)


def _matchups_envelope(week: int = DEFAULT_WEEK) -> dict[str, Any]:
    data = yahoo_shapes.parse_matchups(_load_yahoo("league_matchups_week.json"), week=week)
    return sources_base.source_result("yahoo", data=data, fetched_at=FETCHED_AT)


def _rosters_envelope(team_ids: list[str], week: int = DEFAULT_WEEK) -> dict[str, Any]:
    """Return a canned fetch_rosters envelope for team_ids.

    Only fixtures/yahoo/team_roster_week.json's single recorded roster
    (team "1") carries real players; every other team_id gets the same
    empty roster engine.yahoo_shapes.parse_roster returns for garbage
    input, which is a legitimate shape (an empty roster), not a failure.
    """
    roster_payload = _load_yahoo("team_roster_week.json")
    rosters = []
    for team_id in team_ids:
        if team_id == "1":
            rosters.append(yahoo_shapes.parse_roster(roster_payload, team_id=team_id))
        else:
            rosters.append(yahoo_shapes.parse_roster(None, team_id=team_id))
    data = {"week": week, "rosters": rosters, "failed_team_ids": []}
    return sources_base.source_result("yahoo", data=data, fetched_at=FETCHED_AT)


def _free_agents_envelope() -> dict[str, Any]:
    data = yahoo_shapes.parse_free_agents(_load_yahoo("free_agents.json"))
    return sources_base.source_result("yahoo", data=data, fetched_at=FETCHED_AT)


def _player_list_envelope() -> dict[str, Any]:
    data = yahoo_shapes.parse_player_list(_load_yahoo("league_players.json"))
    return sources_base.source_result("yahoo", data=data, fetched_at=FETCHED_AT)


def _sleeper_index_envelope() -> dict[str, Any]:
    payload = _load_sources("sleeper_players.json")
    with patch("engine.sources.sleeper.fetch_cached_json") as mock_fetch:
        mock_fetch.return_value = (payload, FETCHED_AT, False)
        return sleeper_source.fetch_player_index()


def _sleeper_projections_envelope(season: int = DEFAULT_SEASON, week: int = DEFAULT_WEEK) -> dict[str, Any]:
    payload = _load_sources("sleeper_projections.json")
    with patch("engine.sources.sleeper.fetch_cached_json") as mock_fetch:
        mock_fetch.return_value = (payload, FETCHED_AT, False)
        return sleeper_source.fetch_projections(season, week)


def _injuries_envelope() -> dict[str, Any]:
    payload = _load_sources("espn_injuries.json")
    with patch("engine.sources.injuries.fetch_cached_json") as mock_fetch:
        mock_fetch.return_value = (payload, FETCHED_AT, False)
        return injuries_source.fetch_injuries()


def _patch_all_fetches(
    monkeypatch: pytest.MonkeyPatch,
    *,
    team_ids: list[str] | None = None,
    settings_env: dict[str, Any] | None = None,
    matchups_env: dict[str, Any] | None = None,
    sleeper_projections_env: dict[str, Any] | None = None,
    injuries_env: dict[str, Any] | None = None,
) -> None:
    """Monkeypatch every fetch_ function assemble_live_league calls.

    Each is replaced with a plain lambda that ignores whatever arguments
    it is called with and returns one already-built canned envelope, so
    engine.live_league never reaches engine.sources.base.fetch_json or
    fetch_cached_json (and therefore never urllib.request.urlopen) during
    the actual assemble_live_league/build_live_league call this test then
    makes. A caller may override any one envelope to exercise a
    degraded-source path.
    """
    resolved_team_ids = team_ids if team_ids is not None else DEFAULT_TEAM_IDS

    metadata_env = _metadata_envelope()
    resolved_settings_env = settings_env if settings_env is not None else _settings_envelope()
    resolved_matchups_env = matchups_env if matchups_env is not None else _matchups_envelope()
    rosters_env = _rosters_envelope(resolved_team_ids)
    free_agents_env = _free_agents_envelope()
    player_list_env = _player_list_envelope()
    sleeper_index_env = _sleeper_index_envelope()
    resolved_sleeper_projections_env = (
        sleeper_projections_env if sleeper_projections_env is not None
        else _sleeper_projections_envelope()
    )
    resolved_injuries_env = injuries_env if injuries_env is not None else _injuries_envelope()

    monkeypatch.setattr(yahoo_client, "fetch_league_metadata", lambda **kw: metadata_env)
    monkeypatch.setattr(yahoo_client, "fetch_league_settings", lambda **kw: resolved_settings_env)
    monkeypatch.setattr(yahoo_client, "fetch_matchups", lambda **kw: resolved_matchups_env)
    monkeypatch.setattr(yahoo_client, "fetch_rosters", lambda **kw: rosters_env)
    monkeypatch.setattr(yahoo_client, "fetch_free_agents", lambda **kw: free_agents_env)
    monkeypatch.setattr(yahoo_client, "fetch_player_list", lambda **kw: player_list_env)
    monkeypatch.setattr(sleeper_source, "fetch_player_index", lambda **kw: sleeper_index_env)
    monkeypatch.setattr(
        sleeper_source, "fetch_projections", lambda *a, **kw: resolved_sleeper_projections_env
    )
    monkeypatch.setattr(injuries_source, "fetch_injuries", lambda **kw: resolved_injuries_env)


def _assemble(
    monkeypatch: pytest.MonkeyPatch, *, patch_overrides: dict[str, Any] | None = None, **call_kwargs: Any
) -> dict[str, Any]:
    _patch_all_fetches(monkeypatch, **(patch_overrides or {}))
    call_kwargs.setdefault("league_id", DEFAULT_LEAGUE_ID)
    call_kwargs.setdefault("season", DEFAULT_SEASON)
    call_kwargs.setdefault("week", DEFAULT_WEEK)
    return live_league.assemble_live_league(**call_kwargs)


def _build(
    monkeypatch: pytest.MonkeyPatch, *, patch_overrides: dict[str, Any] | None = None, **call_kwargs: Any
) -> dict[str, Any]:
    _patch_all_fetches(monkeypatch, **(patch_overrides or {}))
    call_kwargs.setdefault("league_id", DEFAULT_LEAGUE_ID)
    call_kwargs.setdefault("season", DEFAULT_SEASON)
    call_kwargs.setdefault("week", DEFAULT_WEEK)
    return live_league.build_live_league(**call_kwargs)


# ---------------------------------------------------------------------------
# SLEEPER_STAT_KEY_MAP / SLEEPER_IGNORED_STAT_KEYS
# ---------------------------------------------------------------------------


def test_stat_key_map_and_ignored_keys_do_not_overlap() -> None:
    assert set(live_league.SLEEPER_STAT_KEY_MAP) & live_league.SLEEPER_IGNORED_STAT_KEYS == set()


def test_stat_key_map_and_ignored_keys_cover_every_key_in_the_real_fixture() -> None:
    """Every Sleeper stat key the checked-in fixture actually contains is
    accounted for, either translated or deliberately ignored, so a real
    run against this same shape reports zero unmapped stat keys."""
    payload = _load_sources("sleeper_projections.json")
    seen_keys: set[str] = set()
    for entry in payload:
        seen_keys.update(entry.get("stats", {}).keys())

    known = set(live_league.SLEEPER_STAT_KEY_MAP) | live_league.SLEEPER_IGNORED_STAT_KEYS
    assert seen_keys, "fixture is expected to carry at least one stat key"
    assert seen_keys <= known


# ---------------------------------------------------------------------------
# sleeper_projections_for_league
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_projections,bad_identity",
    [
        (None, None),
        ("not a dict", {}),
        ({}, None),
        ({"projections": "not a dict"}, {"players": {}}),
        ({"projections": {}}, {"players": "not a dict"}),
        ({"projections": {}}, "not a dict"),
    ],
)
def test_sleeper_projections_for_league_never_raises_on_garbage(
    bad_projections: Any, bad_identity: Any
) -> None:
    result = live_league.sleeper_projections_for_league(bad_projections, bad_identity, week=3)
    assert result == {
        "projections": [],
        "matched": 0,
        "unmatched_yahoo_ids": [],
        "unmapped_stat_keys": [],
    }


def test_sleeper_projections_for_league_rekeys_onto_yahoo_ids_and_reports_unmapped_keys() -> None:
    """The one test that proves the id re-key actually happened.

    The Yahoo player id (9001) and the Sleeper player id (SLEEP-XYZ) are
    deliberately different strings, so a bug that forgot the re-key and
    just reused the Sleeper id would fail this test's player_id
    assertion, unlike a test built only from this repo's own fixtures,
    where the Yahoo and Sleeper ids for the same player happen to be
    spelled identically and so could not catch that bug.
    """
    identity_data = {
        "players": {
            "9001": {
                "yahoo_player_id": "9001",
                "yahoo_player_key": "",
                "name": "Fixture Player",
                "normalized_name": "fixture player",
                "nfl_team": "SEA",
                "positions": ["WR"],
                "sleeper_player_id": "SLEEP-XYZ",
                "injury": None,
            }
        }
    }
    sleeper_projections_data = {
        "season": 2025,
        "week": 3,
        "source_url": "https://example.invalid/projections",
        "projections": {
            "SLEEP-XYZ": {
                "player_id": "SLEEP-XYZ",
                "stats": {
                    "pass_yd": 300.0,
                    "rush_td": 2.0,
                    "totally_unknown_stat": 5.0,
                },
            }
        },
        "count": 1,
    }

    result = live_league.sleeper_projections_for_league(
        sleeper_projections_data, identity_data, week=3
    )

    assert result["matched"] == 1
    assert result["unmatched_yahoo_ids"] == []
    assert result["unmapped_stat_keys"] == ["totally_unknown_stat"]
    assert not set(result["unmapped_stat_keys"]) & live_league.SLEEPER_IGNORED_STAT_KEYS
    assert result["projections"] == [
        {
            "week": 3,
            "player_id": "9001",
            "stats": {"passing_yards": 300.0, "rushing_touchdowns": 2.0},
        }
    ]


def test_sleeper_projections_for_league_against_the_real_fixtures() -> None:
    """End to end against the real Phase 2/3 fixtures: Yahoo player list,
    real Sleeper player index, real ESPN injuries, and the real identity
    join, feeding the real Sleeper projections fixture."""
    yahoo_players = yahoo_shapes.parse_player_list(_load_yahoo("league_players.json"))["players"]
    sleeper_index_data = _sleeper_index_envelope()["data"]
    injuries_data = _injuries_envelope()["data"]
    identity_data = identity_module.identity_result(
        yahoo_players, sleeper_index_data=sleeper_index_data, injuries_data=injuries_data
    )["data"]
    sleeper_projections_data = _sleeper_projections_envelope()["data"]

    result = live_league.sleeper_projections_for_league(
        sleeper_projections_data, identity_data, week=DEFAULT_WEEK
    )

    # Amon-Ra St. Brown (7547) has a Sleeper player id via the identity
    # join (fixtures/sources/sleeper_players.json carries him) but no
    # entry in fixtures/sources/sleeper_projections.json this week, so he
    # is the one expected miss.
    assert result["matched"] == 3
    assert result["unmatched_yahoo_ids"] == ["7547"]
    assert result["unmapped_stat_keys"] == []

    by_player_id = {row["player_id"]: row for row in result["projections"]}
    assert set(by_player_id) == {"4984", "4986", "11604"}

    allen = by_player_id["4984"]
    assert allen["week"] == DEFAULT_WEEK
    assert allen["stats"] == {
        "passing_yards": pytest.approx(247.03),
        "passing_touchdowns": pytest.approx(1.72),
        "interceptions": pytest.approx(0.76),
        "rushing_yards": pytest.approx(35.53),
        "rushing_touchdowns": pytest.approx(0.62),
        "fumbles_lost": pytest.approx(0.26),
    }


# ---------------------------------------------------------------------------
# assemble_live_league / build_live_league: shape
# ---------------------------------------------------------------------------


def test_build_live_league_returns_exactly_eleven_keys_in_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    league = _build(monkeypatch, waiver_settings={"priority_order": DEFAULT_TEAM_IDS})
    assert tuple(league.keys()) == ELEVEN_KEYS_IN_ORDER


def test_build_live_league_returns_exactly_assemble_live_leagues_league(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_all_fetches(monkeypatch)
    kwargs = {"league_id": DEFAULT_LEAGUE_ID, "season": DEFAULT_SEASON, "week": DEFAULT_WEEK}
    assembled = live_league.assemble_live_league(**kwargs)
    built = live_league.build_live_league(**kwargs)
    assert built == assembled["league"]
    assert tuple(built.keys()) == ELEVEN_KEYS_IN_ORDER


def test_assemble_live_league_sources_dict_carries_every_fetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _assemble(monkeypatch)
    for name in (
        "league_metadata",
        "league_settings",
        "matchups",
        "rosters",
        "free_agents",
        "player_list",
        "sleeper_player_index",
        "sleeper_projections",
        "injuries",
        "identity",
    ):
        assert name in result["sources"]
        assert result["sources"][name]["available"] is True


# ---------------------------------------------------------------------------
# assemble_live_league / build_live_league: engine.brief.build_brief contract
# ---------------------------------------------------------------------------


def test_build_live_league_feeds_build_brief_with_priority_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    league = _build(monkeypatch, waiver_settings={"priority_order": DEFAULT_TEAM_IDS})

    brief = build_brief(league, team_id="1", week=DEFAULT_WEEK)

    assert brief["team"]["team_id"] == "1"
    assert brief["week"] == DEFAULT_WEEK
    assert brief["league"]["waiver_type"] == "priority"
    assert "optimal_lineup" in brief
    assert "waivers" in brief


def test_build_live_league_feeds_build_brief_with_faab_remaining(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    league = _build(
        monkeypatch,
        waiver_settings={"type": "faab", "faab_remaining": {"1": 50}},
    )

    assert league["settings"]["waiver"]["type"] == "faab"

    brief = build_brief(league, team_id="1", week=DEFAULT_WEEK)

    assert brief["league"]["waiver_type"] == "faab"
    assert brief["waivers"]["team_id"] == "1"


def test_projected_points_by_player_is_nonzero_for_a_translated_yahoo_player(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    league = _build(monkeypatch)
    points = projected_points_by_player(league, DEFAULT_WEEK)
    # Josh Allen, yahoo player_id 4984: five of his Sleeper stat keys
    # (pass_yd, pass_td, pass_int, rush_yd, rush_td) translate onto
    # scored keys under fixtures/yahoo/league_settings.json's scoring
    # rules; an untranslated stat line would score him exactly 0.0.
    assert points["4984"] > 20.0


# ---------------------------------------------------------------------------
# assemble_live_league: team/owner synthesis
# ---------------------------------------------------------------------------


def test_owner_team_is_flagged_from_yahoo_matchup_data(monkeypatch: pytest.MonkeyPatch) -> None:
    league = _build(monkeypatch)
    owners = [team["team_id"] for team in league["teams"] if team["is_owner_team"]]
    assert owners == ["1"]


def test_team_names_and_managers_are_synthesized_placeholders_with_a_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _assemble(monkeypatch)
    teams = result["league"]["teams"]
    assert teams
    for team in teams:
        assert team["name"] == f"Team {team['team_id']}"
        assert team["manager"] == ""
    assert any(
        "team" in warning.lower() and ("name" in warning.lower() or "manager" in warning.lower())
        for warning in result["warnings"]
    )


def test_blank_owner_team_id_flags_no_team_and_warns(monkeypatch: pytest.MonkeyPatch) -> None:
    stripped_payload = copy.deepcopy(_load_yahoo("league_matchups_week.json"))
    for matchup in stripped_payload:
        for team_wrapper in matchup["teams"]:
            team_wrapper["team"].pop("is_owned_by_current_login", None)
    data = yahoo_shapes.parse_matchups(stripped_payload, week=DEFAULT_WEEK)
    assert data["owner_team_id"] == ""
    blank_owner_env = sources_base.source_result("yahoo", data=data, fetched_at=FETCHED_AT)

    result = _assemble(monkeypatch, patch_overrides={"matchups_env": blank_owner_env})

    assert result["league"]["teams"]
    assert all(not team["is_owner_team"] for team in result["league"]["teams"])
    assert any("owner_team_id" in warning for warning in result["warnings"])


# ---------------------------------------------------------------------------
# assemble_live_league: player merge (dedup, status, positions, bye_week)
# ---------------------------------------------------------------------------


def test_players_are_deduped_by_id_and_always_carry_status_positions_bye_week(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    league = _build(monkeypatch)
    players = league["players"]

    player_ids = [player["player_id"] for player in players]
    assert len(player_ids) == len(set(player_ids))
    # Trey McBride (9509) appears in both league_players.json (the
    # general player pool) and free_agents.json; only one record for him
    # should survive the merge.
    assert player_ids.count("9509") == 1

    for player in players:
        assert set(player.keys()) == {
            "player_id",
            "name",
            "positions",
            "nfl_team",
            "status",
            "bye_week",
        }
        assert isinstance(player["positions"], list)
        assert isinstance(player["bye_week"], int)
        assert isinstance(player["status"], str)

    # James Conner (4986) is IR on Yahoo's own record; nothing about the
    # merge should ever leave a rostered player's status blank/absent.
    conner = next(player for player in players if player["player_id"] == "4986")
    assert conner["status"] == "IR"


def test_injury_status_overrides_yahoo_status_even_when_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ESPN record reporting a player active carries status code "" (see
    engine.sources.injuries.STATUS_CODES). That "" must still overwrite a
    non-blank Yahoo status, and must never be mistaken for "no injury
    record was found" just because "" is falsy.

    Amon-Ra St. Brown (yahoo player_id 7547) carries Yahoo status "Q" in
    fixtures/yahoo/league_players.json. fixtures/sources/espn_injuries.json
    does not happen to carry an active record for him, so this one test
    builds a small, otherwise-real-shaped ESPN injury record directly
    (not read from a fixture file) to exercise exactly this case.
    """
    name = "Amon-Ra St. Brown"
    injuries_data = {
        "source_url": "https://example.invalid/injuries",
        "season": None,
        "reported_at": None,
        "players": [
            {
                "name": name,
                "normalized_name": normalize_name(name),
                "nfl_team": "DET",
                "position": "WR",
                "status": "",
                "status_raw": "Active",
                "injury_type": "",
                "return_date": "",
                "fantasy_status": "",
                "comment": "",
                "updated": FETCHED_AT,
            }
        ],
        "count": 1,
    }
    injuries_env = sources_base.source_result("injuries", data=injuries_data, fetched_at=FETCHED_AT)

    result = _assemble(monkeypatch, patch_overrides={"injuries_env": injuries_env})

    player = next(p for p in result["league"]["players"] if p["player_id"] == "7547")
    assert player["status"] == ""


# ---------------------------------------------------------------------------
# assemble_live_league: degraded sources warn instead of raising
# ---------------------------------------------------------------------------


def test_unavailable_yahoo_source_warns_and_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    bad_settings_env = sources_base.unavailable_result("yahoo", "league settings boom")

    result = _assemble(monkeypatch, patch_overrides={"settings_env": bad_settings_env})

    assert any("league settings" in warning and "boom" in warning for warning in result["warnings"])
    assert tuple(result["league"].keys()) == ELEVEN_KEYS_IN_ORDER
    assert result["league"]["settings"]["scoring"]["stats"] == {}
    assert result["league"]["settings"]["roster_slots"] == []


def test_unavailable_sleeper_source_warns_and_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    bad_projections_env = sources_base.unavailable_result("sleeper", "projections boom")

    result = _assemble(monkeypatch, patch_overrides={"sleeper_projections_env": bad_projections_env})

    assert any("projections boom" in warning for warning in result["warnings"])
    assert result["league"]["projections"] == []
    # The rest of the assembly still succeeds: eleven keys, no exception.
    assert tuple(result["league"].keys()) == ELEVEN_KEYS_IN_ORDER


def test_unavailable_injuries_source_warns_and_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    bad_injuries_env = sources_base.unavailable_result("injuries", "espn boom")

    result = _assemble(monkeypatch, patch_overrides={"injuries_env": bad_injuries_env})

    assert any("espn boom" in warning for warning in result["warnings"])
    # Every player still carries a status, falling back to Yahoo's own.
    conner = next(p for p in result["league"]["players"] if p["player_id"] == "4986")
    assert conner["status"] == "IR"
