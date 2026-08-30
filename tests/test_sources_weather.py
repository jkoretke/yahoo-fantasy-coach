"""Tests for engine.sources.weather: Open-Meteo kickoff-hour conditions at
outdoor and retractable-roof NFL stadiums.

FETCH PATH tests (proving the URL, the cache and the network failure
handling) patch urllib.request.urlopen directly with a fake response
object implementing __enter__/__exit__/read, the same convention
tests/test_sources_base.py uses. PARSE PATH tests (proving the JSON shape
mapping) patch engine.sources.weather.fetch_cached_json instead, so they
never touch urllib at all.

Every test that hits disk passes cache_root=tmp_path, so the suite never
writes into the repo's own runs/cache/ directory. The recorded fixture is
loaded with json.loads, not engine.common.load_json, matching the
convention documented in fixtures/sources/README.md.
"""
from __future__ import annotations

import json
import urllib.error
from pathlib import Path
from unittest.mock import patch

import pytest

from engine.common import EngineError, REPO_ROOT
from engine.sources import weather

FIXTURE_PATH = REPO_ROOT / "fixtures" / "sources" / "open_meteo_forecast.json"

CANONICAL_TEAMS = {
    "ARI", "ATL", "BAL", "BUF", "CAR", "CHI", "CIN", "CLE", "DAL", "DEN",
    "DET", "GB", "HOU", "IND", "JAX", "KC", "LAC", "LAR", "LV", "MIA",
    "MIN", "NE", "NO", "NYG", "NYJ", "PHI", "PIT", "SEA", "SF", "TB",
    "TEN", "WAS",
}


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
# STADIUMS table sanity
# ---------------------------------------------------------------------------


def test_stadiums_table_has_exactly_32_canonical_teams() -> None:
    assert len(weather.STADIUMS) == 32
    assert set(weather.STADIUMS.keys()) == CANONICAL_TEAMS


def test_stadiums_entries_are_well_formed() -> None:
    for team, entry in weather.STADIUMS.items():
        assert set(entry.keys()) == {"name", "latitude", "longitude", "roof"}
        assert entry["roof"] in weather.ROOF_TYPES
        assert 25.0 <= entry["latitude"] <= 48.5, f"{team} latitude out of range"
        assert -123.0 <= entry["longitude"] <= -70.0, f"{team} longitude out of range"


# ---------------------------------------------------------------------------
# stadium_for_team / is_outdoor
# ---------------------------------------------------------------------------


def test_stadium_for_team_returns_a_copy_not_a_reference() -> None:
    result = weather.stadium_for_team("NE")
    assert result is not None
    result["name"] = "mutated"
    assert weather.STADIUMS["NE"]["name"] == "Gillette Stadium"


def test_stadium_for_team_unknown_team_returns_none() -> None:
    assert weather.stadium_for_team("ZZ") is None


def test_stadium_for_team_normalizes_alias() -> None:
    result = weather.stadium_for_team("WSH")
    assert result is not None
    assert result["team"] == "WAS"
    assert result["name"] == "Northwest Stadium"


def test_is_outdoor_true_for_outdoor_and_retractable() -> None:
    assert weather.is_outdoor("NE") is True
    assert weather.is_outdoor("DAL") is True


def test_is_outdoor_false_for_dome_and_unknown() -> None:
    assert weather.is_outdoor("MIN") is False
    assert weather.is_outdoor("ZZ") is False


def test_forecast_url_template_matches_the_confirmed_live_api_shape() -> None:
    assert weather.FORECAST_URL_TEMPLATE == (
        "https://api.open-meteo.com/v1/forecast"
        "?latitude={latitude}&longitude={longitude}"
        "&hourly=temperature_2m,precipitation,precipitation_probability,"
        "wind_speed_10m,wind_gusts_10m"
        "&temperature_unit=fahrenheit&wind_speed_unit=mph&precipitation_unit=inch"
        "&timezone=UTC&start_date={start_date}&end_date={end_date}"
    )


# ---------------------------------------------------------------------------
# fetch_kickoff_weather: dome roof, zero network
# ---------------------------------------------------------------------------


@patch("urllib.request.urlopen")
def test_dome_team_is_available_indoor_and_makes_no_network_call(mock_urlopen, tmp_path: Path) -> None:
    result = weather.fetch_kickoff_weather("MIN", "2026-09-13T17:00:00Z", cache_root=tmp_path)

    assert result["available"] is True
    assert result["data"]["indoor"] is True
    assert result["data"]["roof"] == "dome"
    assert result["data"]["conditions"] is None
    assert result["data"]["flags"] == []
    assert mock_urlopen.call_count == 0
    assert list(tmp_path.iterdir()) == []


# ---------------------------------------------------------------------------
# fetch_kickoff_weather: real fetch path (outdoor and retractable)
# ---------------------------------------------------------------------------


@patch("urllib.request.urlopen")
def test_outdoor_team_fetches_and_parses_the_kickoff_hour(mock_urlopen, tmp_path: Path) -> None:
    mock_urlopen.return_value = _FakeResponse(FIXTURE_PATH.read_bytes())

    result = weather.fetch_kickoff_weather("NE", "2026-09-13T17:00:00Z", cache_root=tmp_path)

    assert mock_urlopen.call_count == 1
    request = mock_urlopen.call_args.args[0]
    assert "latitude=42.0909" in request.full_url
    assert "longitude=-71.2643" in request.full_url
    assert "start_date=2026-09-13" in request.full_url
    assert "end_date=2026-09-13" in request.full_url

    assert result["available"] is True
    assert result["stale"] is False
    data = result["data"]
    assert data["roof"] == "outdoor"
    assert data["indoor"] is False
    assert data["kickoff_utc"] == "2026-09-13T17:00:00Z"
    conditions = data["conditions"]
    assert conditions["forecast_hour_utc"] == "2026-09-13T17:00:00Z"
    assert conditions["temperature_f"] == 64.0
    assert conditions["wind_mph"] == 24.5
    assert conditions["wind_gust_mph"] == 34.0
    assert conditions["precipitation_in"] == 0.08
    assert conditions["precipitation_probability"] == 65
    assert sorted(data["flags"]) == [
        "high gusts",
        "high wind",
        "precipitation",
        "precipitation likely",
    ]

    assert set(result.keys()) == {"source", "available", "stale", "reason", "fetched_at", "data"}
    assert set(data.keys()) == {
        "team", "stadium", "roof", "kickoff_utc", "indoor", "source_url", "conditions", "flags",
    }
    json.dumps(result)


@patch("urllib.request.urlopen")
def test_second_call_against_warm_cache_makes_no_additional_network_call(
    mock_urlopen, tmp_path: Path
) -> None:
    mock_urlopen.return_value = _FakeResponse(FIXTURE_PATH.read_bytes())

    weather.fetch_kickoff_weather("NE", "2026-09-13T17:00:00Z", cache_root=tmp_path)
    assert mock_urlopen.call_count == 1

    result = weather.fetch_kickoff_weather("NE", "2026-09-13T17:00:00Z", cache_root=tmp_path)
    assert mock_urlopen.call_count == 1
    assert result["available"] is True
    assert result["stale"] is False


@patch("urllib.request.urlopen")
def test_retractable_team_fetches_and_reports_retractable_roof(mock_urlopen, tmp_path: Path) -> None:
    mock_urlopen.return_value = _FakeResponse(FIXTURE_PATH.read_bytes())

    result = weather.fetch_kickoff_weather("DAL", "2026-09-13T17:00:00Z", cache_root=tmp_path)

    assert mock_urlopen.call_count == 1
    assert result["available"] is True
    assert result["data"]["roof"] == "retractable"
    assert result["data"]["indoor"] is False
    assert result["data"]["conditions"] is not None


@patch("urllib.request.urlopen")
def test_unknown_team_is_unavailable_and_makes_no_network_call(mock_urlopen, tmp_path: Path) -> None:
    result = weather.fetch_kickoff_weather("ZZ", "2026-09-13T17:00:00Z", cache_root=tmp_path)

    assert result["available"] is False
    assert result["reason"]
    assert mock_urlopen.call_count == 0


# ---------------------------------------------------------------------------
# PARSE PATH: shape mapping, nulls, nearest hour, out of window
# ---------------------------------------------------------------------------


@patch("engine.sources.weather.fetch_cached_json")
def test_null_reading_yields_none_never_zero_and_never_raises(mock_fetch, tmp_path: Path) -> None:
    payload = load_fixture()
    mock_fetch.return_value = (payload, "2026-09-10T12:00:00Z", False)

    result = weather.fetch_kickoff_weather("NE", "2026-09-13T18:00:00Z", cache_root=tmp_path)

    assert result["available"] is True
    conditions = result["data"]["conditions"]
    assert conditions["temperature_f"] is None
    assert result["data"]["flags"] == []


@patch("engine.sources.weather.fetch_cached_json")
def test_short_hourly_array_yields_none_for_that_reading(mock_fetch, tmp_path: Path) -> None:
    payload = load_fixture()
    # Kickoff (17:00) is index 3; shorten this array so index 3 is out of range.
    payload["hourly"]["wind_gusts_10m"] = [14.0, 16.0]
    mock_fetch.return_value = (payload, "2026-09-10T12:00:00Z", False)

    result = weather.fetch_kickoff_weather("NE", "2026-09-13T17:00:00Z", cache_root=tmp_path)

    assert result["available"] is True
    assert result["data"]["conditions"]["wind_gust_mph"] is None
    assert "high gusts" not in result["data"]["flags"]


@patch("engine.sources.weather.fetch_cached_json")
def test_kickoff_between_hours_picks_the_nearest_one(mock_fetch, tmp_path: Path) -> None:
    payload = load_fixture()
    mock_fetch.return_value = (payload, "2026-09-10T12:00:00Z", False)

    # 17:40 is 40 minutes from 17:00 and 20 minutes from 18:00: nearest is 18:00.
    result = weather.fetch_kickoff_weather("NE", "2026-09-13T17:40:00Z", cache_root=tmp_path)

    assert result["available"] is True
    assert result["data"]["conditions"]["forecast_hour_utc"] == "2026-09-13T18:00:00Z"


@patch("engine.sources.weather.fetch_cached_json")
def test_kickoff_far_outside_forecast_window_is_unavailable(mock_fetch, tmp_path: Path) -> None:
    payload = load_fixture()
    mock_fetch.return_value = (payload, "2026-09-10T12:00:00Z", False)

    result = weather.fetch_kickoff_weather("NE", "2026-09-20T17:00:00Z", cache_root=tmp_path)

    assert result["available"] is False
    assert result["reason"] == "no forecast hour near kickoff"


@patch("engine.sources.weather.fetch_cached_json")
def test_empty_hourly_time_is_unavailable(mock_fetch, tmp_path: Path) -> None:
    payload = load_fixture()
    payload["hourly"]["time"] = []
    mock_fetch.return_value = (payload, "2026-09-10T12:00:00Z", False)

    result = weather.fetch_kickoff_weather("NE", "2026-09-13T17:00:00Z", cache_root=tmp_path)

    assert result["available"] is False
    assert result["reason"] == "no forecast hour near kickoff"


@patch("engine.sources.weather.fetch_cached_json")
def test_payload_missing_hourly_key_is_unavailable(mock_fetch, tmp_path: Path) -> None:
    mock_fetch.return_value = ({"latitude": 42.0, "longitude": -71.0}, "2026-09-10T12:00:00Z", False)

    result = weather.fetch_kickoff_weather("NE", "2026-09-13T17:00:00Z", cache_root=tmp_path)

    assert result["available"] is False
    assert result["reason"]


@patch("engine.sources.weather.fetch_cached_json")
def test_payload_that_is_a_json_list_is_unavailable(mock_fetch, tmp_path: Path) -> None:
    mock_fetch.return_value = ([1, 2, 3], "2026-09-10T12:00:00Z", False)

    result = weather.fetch_kickoff_weather("NE", "2026-09-13T17:00:00Z", cache_root=tmp_path)

    assert result["available"] is False
    assert result["reason"]


# ---------------------------------------------------------------------------
# Degradation modes: HTTPError, URLError/TimeoutError, invalid JSON body
# ---------------------------------------------------------------------------


@patch("urllib.request.urlopen")
def test_http_error_degrades_to_unavailable(mock_urlopen, tmp_path: Path) -> None:
    mock_urlopen.side_effect = urllib.error.HTTPError(
        "https://example.test", 503, "Service Unavailable", hdrs=None, fp=None
    )

    result = weather.fetch_kickoff_weather("NE", "2026-09-13T17:00:00Z", cache_root=tmp_path)

    assert result["available"] is False
    assert result["reason"]


@patch("urllib.request.urlopen")
def test_url_error_degrades_to_unavailable(mock_urlopen, tmp_path: Path) -> None:
    mock_urlopen.side_effect = urllib.error.URLError("no route to host")

    result = weather.fetch_kickoff_weather("NE", "2026-09-13T17:00:00Z", cache_root=tmp_path)

    assert result["available"] is False
    assert result["reason"]


@patch("urllib.request.urlopen")
def test_timeout_error_degrades_to_unavailable(mock_urlopen, tmp_path: Path) -> None:
    mock_urlopen.side_effect = TimeoutError("timed out")

    result = weather.fetch_kickoff_weather("NE", "2026-09-13T17:00:00Z", cache_root=tmp_path)

    assert result["available"] is False
    assert result["reason"]


@patch("urllib.request.urlopen")
def test_invalid_json_body_degrades_to_unavailable(mock_urlopen, tmp_path: Path) -> None:
    mock_urlopen.return_value = _FakeResponse(b"not json at all")

    result = weather.fetch_kickoff_weather("NE", "2026-09-13T17:00:00Z", cache_root=tmp_path)

    assert result["available"] is False
    assert result["reason"]


# ---------------------------------------------------------------------------
# enabled=False, programmer errors, envelope shape
# ---------------------------------------------------------------------------


@patch("urllib.request.urlopen")
def test_disabled_returns_disabled_reason_with_no_network(mock_urlopen, tmp_path: Path) -> None:
    result = weather.fetch_kickoff_weather(
        "NE", "2026-09-13T17:00:00Z", enabled=False, cache_root=tmp_path
    )

    assert result["available"] is False
    assert result["reason"] == "disabled"
    assert mock_urlopen.call_count == 0
    assert list(tmp_path.iterdir()) == []


def test_naive_kickoff_utc_raises_engine_error(tmp_path: Path) -> None:
    with pytest.raises(EngineError):
        weather.fetch_kickoff_weather("NE", "2026-09-13T17:00:00", cache_root=tmp_path)


def test_unparseable_kickoff_utc_raises_engine_error(tmp_path: Path) -> None:
    with pytest.raises(EngineError):
        weather.fetch_kickoff_weather("NE", "not-a-timestamp", cache_root=tmp_path)


def test_non_string_kickoff_utc_raises_engine_error(tmp_path: Path) -> None:
    with pytest.raises(EngineError):
        weather.fetch_kickoff_weather("NE", 12345, cache_root=tmp_path)  # type: ignore[arg-type]


def test_naive_kickoff_utc_raises_even_when_disabled(tmp_path: Path) -> None:
    with pytest.raises(EngineError):
        weather.fetch_kickoff_weather(
            "NE", "2026-09-13T17:00:00", enabled=False, cache_root=tmp_path
        )


def test_envelope_has_exactly_the_expected_keys_and_is_json_serializable(tmp_path: Path) -> None:
    result = weather.fetch_kickoff_weather("MIN", "2026-09-13T17:00:00Z", cache_root=tmp_path)

    assert set(result.keys()) == {"source", "available", "stale", "reason", "fetched_at", "data"}
    assert result["source"] == weather.SOURCE_NAME
    json.dumps(result)


# ---------------------------------------------------------------------------
# weather_flags
# ---------------------------------------------------------------------------


def _conditions(**overrides):
    base = {
        "temperature_f": 70.0,
        "wind_mph": 5.0,
        "wind_gust_mph": 8.0,
        "precipitation_in": 0.0,
        "precipitation_probability": 5,
    }
    base.update(overrides)
    return base


def test_weather_flags_none_returns_empty_list() -> None:
    assert weather.weather_flags(None) == []


def test_weather_flags_calm_hour_returns_empty_list() -> None:
    assert weather.weather_flags(_conditions()) == []


def test_weather_flags_high_wind_threshold() -> None:
    assert weather.weather_flags(_conditions(wind_mph=weather.HIGH_WIND_MPH)) == ["high wind"]
    assert weather.weather_flags(_conditions(wind_mph=weather.HIGH_WIND_MPH - 0.1)) == []


def test_weather_flags_high_gusts_threshold() -> None:
    assert weather.weather_flags(_conditions(wind_gust_mph=weather.HIGH_GUST_MPH)) == ["high gusts"]
    assert weather.weather_flags(_conditions(wind_gust_mph=weather.HIGH_GUST_MPH - 0.1)) == []


def test_weather_flags_precipitation_threshold() -> None:
    assert weather.weather_flags(
        _conditions(precipitation_in=weather.PRECIPITATION_INCHES)
    ) == ["precipitation"]
    assert weather.weather_flags(
        _conditions(precipitation_in=weather.PRECIPITATION_INCHES - 0.01)
    ) == []


def test_weather_flags_precipitation_probability_threshold() -> None:
    assert weather.weather_flags(
        _conditions(precipitation_probability=weather.PRECIPITATION_PROBABILITY_PERCENT)
    ) == ["precipitation likely"]
    assert weather.weather_flags(
        _conditions(precipitation_probability=weather.PRECIPITATION_PROBABILITY_PERCENT - 1)
    ) == []


def test_weather_flags_extreme_cold_threshold() -> None:
    assert weather.weather_flags(_conditions(temperature_f=weather.COLD_TEMPERATURE_F)) == ["extreme cold"]
    assert weather.weather_flags(_conditions(temperature_f=weather.COLD_TEMPERATURE_F + 0.1)) == []


def test_weather_flags_multiple_thresholds_sorted() -> None:
    flags = weather.weather_flags(
        _conditions(
            wind_mph=25.0,
            wind_gust_mph=35.0,
            precipitation_in=0.1,
            precipitation_probability=90,
            temperature_f=10.0,
        )
    )
    assert flags == sorted(flags)
    assert set(flags) == {
        "high wind",
        "high gusts",
        "precipitation",
        "precipitation likely",
        "extreme cold",
    }


def test_weather_flags_none_reading_never_trips_its_flag() -> None:
    conditions = _conditions(wind_mph=None, wind_gust_mph=None, precipitation_in=None,
                              precipitation_probability=None, temperature_f=None)
    assert weather.weather_flags(conditions) == []
