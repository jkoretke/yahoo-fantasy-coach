"""Tests for engine.sources.schedule: ESPN scoreboard kickoff times and venues.

FETCH PATH tests (proving the URL, the cache and the failure handling) patch
urllib.request.urlopen directly, with a fake response object matching how
engine.sources.base.fetch_json uses it (a context manager whose read()
returns bytes). PARSE PATH tests (proving the shape mapping) patch
engine.sources.schedule.fetch_cached_json directly, since that name lives in
this module's own namespace (see the mandatory import form in
engine/sources/schedule.py). Every test that touches disk passes an explicit
cache_root=tmp_path, so the suite never writes into the repo's own
runs/cache/ directory.
"""
from __future__ import annotations

import json
import urllib.error
from pathlib import Path
from unittest.mock import patch

import pytest

from engine.common import REPO_ROOT
from engine.sources import schedule


FIXTURE_PATH = REPO_ROOT / "fixtures" / "sources" / "espn_scoreboard.json"


def load_fixture() -> dict:
    return json.loads(FIXTURE_PATH.read_bytes())


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


# ---------------------------------------------------------------------------
# fetch_week_schedule: parse path, against the recorded fixture
# ---------------------------------------------------------------------------


@patch("engine.sources.schedule.fetch_cached_json")
def test_fetch_week_schedule_parses_fixture_into_documented_shape(mock_fetch, tmp_path: Path) -> None:
    mock_fetch.return_value = (load_fixture(), "2026-09-10T12:00:00Z", False)

    result = schedule.fetch_week_schedule(2025, 4, cache_root=tmp_path)

    assert result["source"] == schedule.SOURCE_NAME
    assert result["available"] is True
    assert result["stale"] is False
    assert result["fetched_at"] == "2026-09-10T12:00:00Z"

    data = result["data"]
    assert data["season"] == 2025
    assert data["week"] == 4
    assert data["season_type"] == schedule.REGULAR_SEASON_TYPE
    assert data["count"] == 4
    assert len(data["games"]) == 4

    game = data["games"][0]
    assert set(game.keys()) == {
        "game_id", "kickoff_utc", "name", "short_name",
        "home_team", "away_team", "venue", "city", "state", "country",
        "indoor", "neutral_site", "status_state", "status_detail", "completed",
    }


@patch("engine.sources.schedule.fetch_cached_json")
def test_fetch_week_schedule_sorts_games_by_kickoff_then_game_id(mock_fetch, tmp_path: Path) -> None:
    fixture = load_fixture()
    raw_ids_in_file_order = [event["id"] for event in fixture["events"]]
    # The fixture is deliberately scrambled; prove that first, so a passing
    # sort assertion below cannot be an accident of already-sorted input.
    assert raw_ids_in_file_order == ["401772940", "401772942", "401772938", "401772935"]

    mock_fetch.return_value = (fixture, "2026-09-10T12:00:00Z", False)
    result = schedule.fetch_week_schedule(2025, 4, cache_root=tmp_path)

    game_ids = [game["game_id"] for game in result["data"]["games"]]
    assert game_ids == ["401772938", "401772935", "401772940", "401772942"]


@patch("engine.sources.schedule.fetch_cached_json")
def test_kickoff_utc_is_normalized_to_seconds_precision_with_z(mock_fetch, tmp_path: Path) -> None:
    mock_fetch.return_value = (load_fixture(), "2026-09-10T12:00:00Z", False)
    result = schedule.fetch_week_schedule(2025, 4, cache_root=tmp_path)

    game = next(g for g in result["data"]["games"] if g["game_id"] == "401772938")
    assert game["kickoff_utc"] == "2025-09-26T00:15:00Z"


@patch("engine.sources.schedule.fetch_cached_json")
def test_wsh_abbreviation_is_normalized_to_was(mock_fetch, tmp_path: Path) -> None:
    mock_fetch.return_value = (load_fixture(), "2026-09-10T12:00:00Z", False)
    result = schedule.fetch_week_schedule(2025, 4, cache_root=tmp_path)

    game = next(g for g in result["data"]["games"] if g["game_id"] == "401772942")
    assert game["away_team"] == "WAS"
    assert game["home_team"] == "GB"


@patch("engine.sources.schedule.fetch_cached_json")
def test_indoor_flag_is_true_and_false_per_game(mock_fetch, tmp_path: Path) -> None:
    mock_fetch.return_value = (load_fixture(), "2026-09-10T12:00:00Z", False)
    result = schedule.fetch_week_schedule(2025, 4, cache_root=tmp_path)
    games_by_id = {g["game_id"]: g for g in result["data"]["games"]}

    assert games_by_id["401772938"]["indoor"] is True  # State Farm Stadium
    assert games_by_id["401772942"]["indoor"] is False  # Lambeau Field


@patch("engine.sources.schedule.fetch_cached_json")
def test_neutral_site_event_with_no_state_key_defaults_state_to_empty(mock_fetch, tmp_path: Path) -> None:
    mock_fetch.return_value = (load_fixture(), "2026-09-10T12:00:00Z", False)
    result = schedule.fetch_week_schedule(2025, 4, cache_root=tmp_path)

    game = next(g for g in result["data"]["games"] if g["game_id"] == "401772935")
    assert game["neutral_site"] is True
    assert game["state"] == ""
    assert game["country"] == "England"
    assert game["city"] == "London"


# ---------------------------------------------------------------------------
# skipping malformed events, without failing the whole call
# ---------------------------------------------------------------------------


def _wrap(events: list) -> dict:
    return {"events": events, "season": {"year": 2025, "type": 2}, "week": {"number": 4}}


@patch("engine.sources.schedule.fetch_cached_json")
def test_event_with_missing_date_is_skipped(mock_fetch, tmp_path: Path) -> None:
    fixture = load_fixture()
    broken_event = dict(fixture["events"][0])
    broken_event.pop("date")
    mock_fetch.return_value = (_wrap([broken_event] + fixture["events"][1:]), "2026-09-10T12:00:00Z", False)

    result = schedule.fetch_week_schedule(2025, 4, cache_root=tmp_path)

    assert result["available"] is True
    assert result["data"]["count"] == 3


@patch("engine.sources.schedule.fetch_cached_json")
def test_event_with_unparseable_date_is_skipped(mock_fetch, tmp_path: Path) -> None:
    fixture = load_fixture()
    broken_event = dict(fixture["events"][0])
    broken_event["date"] = "not-a-date"
    mock_fetch.return_value = (_wrap([broken_event] + fixture["events"][1:]), "2026-09-10T12:00:00Z", False)

    result = schedule.fetch_week_schedule(2025, 4, cache_root=tmp_path)

    assert result["available"] is True
    assert result["data"]["count"] == 3


@patch("engine.sources.schedule.fetch_cached_json")
def test_event_with_only_one_competitor_is_skipped(mock_fetch, tmp_path: Path) -> None:
    fixture = load_fixture()
    broken_event = json.loads(json.dumps(fixture["events"][0]))
    broken_event["competitions"][0]["competitors"] = broken_event["competitions"][0]["competitors"][:1]
    mock_fetch.return_value = (_wrap([broken_event] + fixture["events"][1:]), "2026-09-10T12:00:00Z", False)

    result = schedule.fetch_week_schedule(2025, 4, cache_root=tmp_path)

    assert result["available"] is True
    assert result["data"]["count"] == 3


@patch("engine.sources.schedule.fetch_cached_json")
def test_event_with_no_competitions_is_skipped(mock_fetch, tmp_path: Path) -> None:
    fixture = load_fixture()
    broken_event = dict(fixture["events"][0])
    broken_event["competitions"] = []
    mock_fetch.return_value = (_wrap([broken_event] + fixture["events"][1:]), "2026-09-10T12:00:00Z", False)

    result = schedule.fetch_week_schedule(2025, 4, cache_root=tmp_path)

    assert result["available"] is True
    assert result["data"]["count"] == 3


# ---------------------------------------------------------------------------
# degradation modes: never raise, always available=False with a reason
# ---------------------------------------------------------------------------


@patch("urllib.request.urlopen")
def test_http_error_degrades_to_unavailable(mock_urlopen, tmp_path: Path) -> None:
    mock_urlopen.side_effect = urllib.error.HTTPError(
        "https://example.test", 503, "Service Unavailable", hdrs=None, fp=None
    )
    result = schedule.fetch_week_schedule(2025, 4, cache_root=tmp_path)

    assert result["available"] is False
    assert result["reason"]
    assert result["data"] is None


@patch("urllib.request.urlopen")
def test_url_error_degrades_to_unavailable(mock_urlopen, tmp_path: Path) -> None:
    mock_urlopen.side_effect = urllib.error.URLError("no route to host")
    result = schedule.fetch_week_schedule(2025, 4, cache_root=tmp_path)

    assert result["available"] is False
    assert result["reason"]


@patch("urllib.request.urlopen")
def test_timeout_error_degrades_to_unavailable(mock_urlopen, tmp_path: Path) -> None:
    mock_urlopen.side_effect = TimeoutError("timed out")
    result = schedule.fetch_week_schedule(2025, 4, cache_root=tmp_path)

    assert result["available"] is False
    assert result["reason"]


@patch("urllib.request.urlopen")
def test_invalid_json_body_degrades_to_unavailable(mock_urlopen, tmp_path: Path) -> None:
    mock_urlopen.return_value = _FakeResponse(b"not json at all")
    result = schedule.fetch_week_schedule(2025, 4, cache_root=tmp_path)

    assert result["available"] is False
    assert result["reason"]


@patch("engine.sources.schedule.fetch_cached_json")
def test_wrong_shape_json_list_degrades_to_unavailable(mock_fetch, tmp_path: Path) -> None:
    mock_fetch.return_value = ([1, 2, 3], "2026-09-10T12:00:00Z", False)
    result = schedule.fetch_week_schedule(2025, 4, cache_root=tmp_path)

    assert result["available"] is False
    assert result["reason"]
    assert result["data"] is None


@patch("engine.sources.schedule.fetch_cached_json")
def test_wrong_shape_no_events_key_degrades_to_unavailable(mock_fetch, tmp_path: Path) -> None:
    mock_fetch.return_value = ({"leagues": []}, "2026-09-10T12:00:00Z", False)
    result = schedule.fetch_week_schedule(2025, 4, cache_root=tmp_path)

    assert result["available"] is False
    assert result["reason"]
    assert result["data"] is None


# ---------------------------------------------------------------------------
# enabled=False: zero network, zero disk
# ---------------------------------------------------------------------------


@patch("urllib.request.urlopen")
def test_disabled_makes_zero_network_and_zero_disk_calls(mock_urlopen, tmp_path: Path) -> None:
    result = schedule.fetch_week_schedule(2025, 4, enabled=False, cache_root=tmp_path)

    assert result["available"] is False
    assert result["reason"] == "disabled"
    assert mock_urlopen.call_count == 0
    assert list(tmp_path.iterdir()) == []


# ---------------------------------------------------------------------------
# envelope shape
# ---------------------------------------------------------------------------


@patch("engine.sources.schedule.fetch_cached_json")
def test_envelope_has_exactly_the_expected_keys_and_is_json_serializable(mock_fetch, tmp_path: Path) -> None:
    mock_fetch.return_value = (load_fixture(), "2026-09-10T12:00:00Z", False)
    result = schedule.fetch_week_schedule(2025, 4, cache_root=tmp_path)

    assert set(result.keys()) == {"source", "available", "stale", "reason", "fetched_at", "data"}
    assert json.dumps(result)


# ---------------------------------------------------------------------------
# rerun-is-free: a warm cache makes zero additional urlopen calls
# ---------------------------------------------------------------------------


@patch("urllib.request.urlopen")
def test_second_call_against_warm_cache_makes_no_additional_urlopen_calls(mock_urlopen, tmp_path: Path) -> None:
    mock_urlopen.return_value = _FakeResponse(json.dumps(load_fixture()).encode("utf-8"))

    first = schedule.fetch_week_schedule(2025, 4, cache_root=tmp_path)
    assert mock_urlopen.call_count == 1
    assert first["available"] is True
    assert first["data"]["count"] == 4

    second = schedule.fetch_week_schedule(2025, 4, cache_root=tmp_path)
    assert mock_urlopen.call_count == 1
    assert second["available"] is True
    assert second["data"]["count"] == 4


# ---------------------------------------------------------------------------
# the URL actually built (fetch path, proving the template substitution)
# ---------------------------------------------------------------------------


@patch("urllib.request.urlopen")
def test_fetch_uses_the_documented_url_template(mock_urlopen, tmp_path: Path) -> None:
    mock_urlopen.return_value = _FakeResponse(json.dumps(load_fixture()).encode("utf-8"))
    schedule.fetch_week_schedule(2025, 4, cache_root=tmp_path)

    request = mock_urlopen.call_args.args[0]
    assert request.full_url == (
        "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
        "?dates=2025&seasontype=2&week=4"
    )


@patch("urllib.request.urlopen")
def test_fetch_uses_postseason_type_in_the_url_when_given(mock_urlopen, tmp_path: Path) -> None:
    mock_urlopen.return_value = _FakeResponse(json.dumps(load_fixture()).encode("utf-8"))
    schedule.fetch_week_schedule(2025, 2, season_type=schedule.POSTSEASON_TYPE, cache_root=tmp_path)

    request = mock_urlopen.call_args.args[0]
    assert "seasontype=3" in request.full_url


@patch("urllib.request.urlopen")
def test_cache_key_is_zero_padded_by_week(mock_urlopen, tmp_path: Path) -> None:
    mock_urlopen.return_value = _FakeResponse(json.dumps(load_fixture()).encode("utf-8"))
    schedule.fetch_week_schedule(2025, 4, cache_root=tmp_path)

    assert (tmp_path / "espn-scoreboard-2025-st2-wk04.json").exists()


# ---------------------------------------------------------------------------
# kickoff_by_team / teams_playing / earliest_kickoff / game_for_team
# ---------------------------------------------------------------------------


@pytest.fixture()
def parsed_data(tmp_path: Path) -> dict:
    with patch("engine.sources.schedule.fetch_cached_json") as mock_fetch:
        mock_fetch.return_value = (load_fixture(), "2026-09-10T12:00:00Z", False)
        result = schedule.fetch_week_schedule(2025, 4, cache_root=tmp_path)
    return result["data"]


def test_kickoff_by_team_maps_every_playing_team(parsed_data: dict) -> None:
    mapping = schedule.kickoff_by_team(parsed_data)
    assert mapping["ARI"] == "2025-09-26T00:15:00Z"
    assert mapping["SEA"] == "2025-09-26T00:15:00Z"
    assert mapping["WAS"] == "2025-09-29T00:20:00Z"
    assert "WSH" not in mapping


def test_kickoff_by_team_bye_team_is_absent(parsed_data: dict) -> None:
    mapping = schedule.kickoff_by_team(parsed_data)
    assert "KC" not in mapping


def test_kickoff_by_team_garbage_input_returns_empty_dict() -> None:
    assert schedule.kickoff_by_team({}) == {}
    assert schedule.kickoff_by_team({"games": "nope"}) == {}
    assert schedule.kickoff_by_team(None) == {}
    assert schedule.kickoff_by_team(42) == {}


def test_teams_playing_returns_sorted_list(parsed_data: dict) -> None:
    teams = schedule.teams_playing(parsed_data)
    assert teams == sorted(teams)
    assert "ARI" in teams
    assert "WAS" in teams
    assert "WSH" not in teams


def test_teams_playing_garbage_input_returns_empty_list() -> None:
    assert schedule.teams_playing({}) == []
    assert schedule.teams_playing(None) == []


def test_earliest_kickoff_across_all_games(parsed_data: dict) -> None:
    assert schedule.earliest_kickoff(parsed_data) == "2025-09-26T00:15:00Z"


def test_earliest_kickoff_with_team_filter(parsed_data: dict) -> None:
    assert schedule.earliest_kickoff(parsed_data, ["NE", "PIT"]) == "2025-09-28T17:00:00Z"


def test_earliest_kickoff_normalizes_teams_in_the_filter(parsed_data: dict) -> None:
    assert schedule.earliest_kickoff(parsed_data, ["wsh"]) == "2025-09-29T00:20:00Z"


def test_earliest_kickoff_bye_team_returns_none(parsed_data: dict) -> None:
    assert schedule.earliest_kickoff(parsed_data, ["KC"]) is None


def test_earliest_kickoff_garbage_input_returns_none() -> None:
    assert schedule.earliest_kickoff({}) is None
    assert schedule.earliest_kickoff(None) is None
    assert schedule.earliest_kickoff({"games": []}, ["ARI"]) is None


def test_game_for_team_returns_the_matching_game(parsed_data: dict) -> None:
    game = schedule.game_for_team(parsed_data, "SEA")
    assert game is not None
    assert game["game_id"] == "401772938"
    assert game["away_team"] == "SEA"


def test_game_for_team_normalizes_input(parsed_data: dict) -> None:
    game = schedule.game_for_team(parsed_data, "wsh")
    assert game is not None
    assert game["game_id"] == "401772942"


def test_game_for_team_bye_returns_none(parsed_data: dict) -> None:
    assert schedule.game_for_team(parsed_data, "KC") is None


def test_game_for_team_garbage_input_returns_none() -> None:
    assert schedule.game_for_team({}, "SEA") is None
    assert schedule.game_for_team(None, "SEA") is None
    assert schedule.game_for_team({"games": []}, "") is None
