"""Open-Meteo kickoff-hour weather for outdoor and retractable-roof stadiums.

Open-Meteo (https://open-meteo.com) is a free, no-auth weather API. This
module reads its hourly forecast endpoint for the stadium hosting an NFL
game's home team and reports the conditions at the hour closest to kickoff:
temperature, wind speed, wind gusts, precipitation and precipitation
probability, plus a small set of deterministic flags a human brief can show
next to a game.

REQUEST SHAPE. One GET request per (team, date) pair:

    FORECAST_URL_TEMPLATE.format(
        latitude=..., longitude=..., start_date="2026-09-13", end_date="2026-09-13",
    )

asking for hourly temperature_2m, precipitation, precipitation_probability,
wind_speed_10m and wind_gusts_10m, in Fahrenheit / inches / mph, pinned to
timezone=UTC so the returned hourly.time strings are UTC even though they
carry no "Z" or offset suffix. A confirmed sample response looks like:

    {"latitude": 42.09629, "longitude": -71.24448, "generationtime_ms": 0.08,
     "utc_offset_seconds": 0, "timezone": "GMT", "timezone_abbreviation": "GMT",
     "elevation": 79.0,
     "hourly_units": {"time": "iso8601", "temperature_2m": "F",
                       "precipitation": "inch", "precipitation_probability": "%",
                       "wind_speed_10m": "mp/h", "wind_gusts_10m": "mp/h"},
     "hourly": {"time": ["2026-09-13T16:00", "2026-09-13T17:00", ...],
                "temperature_2m": [71.2, 70.5, ...],
                "precipitation": [0.0, 0.02, ...],
                "precipitation_probability": [10, 15, ...],
                "wind_speed_10m": [8.1, 9.4, ...],
                "wind_gusts_10m": [14.0, 16.2, ...]}}

Open-Meteo's forecast horizon is roughly 16 days ahead. A kickoff further
out than that comes back with no hourly.time entry near it, which this
module treats as a degraded fetch (unavailable), not a crash.

ROOF POLICY. Every stadium in STADIUMS carries one of three roof values,
never a boolean:

  "outdoor"     the field is exposed to weather every game; fetched and
                flagged normally.
  "retractable" the roof can close, but whether it is open or closed on a
                given Sunday is not something this API (or any free, no-auth
                one) can tell us. This module still fetches and flags a
                retractable-roof stadium exactly like an outdoor one, and
                reports roof="retractable" on the result, so a reader can
                see the actual outdoor conditions with the caveat attached
                rather than the game being silently, and possibly wrongly,
                classified as indoor.
  "dome"        a fixed roof with no outdoor exposure at all. A dome game
                makes no network call and no disk access: the result is
                reported directly as weather neutral (indoor=True,
                conditions=None, flags=[]).

THRESHOLDS. weather_flags() turns one hour's conditions into a small set of
plain, deterministic strings, no model judgement involved:

  "high wind"            wind_mph >= HIGH_WIND_MPH (20.0)
  "high gusts"           wind_gust_mph >= HIGH_GUST_MPH (30.0)
  "precipitation"        precipitation_in >= PRECIPITATION_INCHES (0.05)
  "precipitation likely" precipitation_probability >= PRECIPITATION_PROBABILITY_PERCENT (50)
  "extreme cold"         temperature_f <= COLD_TEMPERATURE_F (25.0)

A missing (None) reading never trips its flag.

NULL HANDLING. Any hourly array may be shorter than the chosen index, or
carry a JSON null at it. Either case reads as None, never 0.0, so a missing
reading is never mistaken for a calm one. round_points is only ever called
on a raw value that is not None; precipitation_probability is kept as a
plain int (or None), never routed through round_points, since round_points
always returns a float and raises on None.

STADIUMS is this module's own static team-to-stadium table (name,
latitude, longitude, roof), kept independent of engine.sources.schedule on
purpose: NFL stadiums are effectively fixed, so duplicating this small
table here means this module can be built, tested and degraded without any
dependency on the schedule source.

Public names: SOURCE_NAME, FORECAST_URL_TEMPLATE, WEATHER_MAX_AGE_SECONDS,
ROOF_TYPES, STADIUMS, HIGH_WIND_MPH, HIGH_GUST_MPH, PRECIPITATION_INCHES,
PRECIPITATION_PROBABILITY_PERCENT, COLD_TEMPERATURE_F, stadium_for_team,
is_outdoor, fetch_kickoff_weather, weather_flags.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from engine.common import EngineError, round_points, timestamp
from engine.sources.base import (
    SourceUnavailable,
    fetch_cached_json,
    source_result,
    disabled_result,
    unavailable_result,
    normalize_team_abbreviation,
)

SOURCE_NAME = "weather"

FORECAST_URL_TEMPLATE = (
    "https://api.open-meteo.com/v1/forecast"
    "?latitude={latitude}&longitude={longitude}"
    "&hourly=temperature_2m,precipitation,precipitation_probability,"
    "wind_speed_10m,wind_gusts_10m"
    "&temperature_unit=fahrenheit&wind_speed_unit=mph&precipitation_unit=inch"
    "&timezone=UTC&start_date={start_date}&end_date={end_date}"
)

WEATHER_MAX_AGE_SECONDS = 1800

ROOF_TYPES = ("outdoor", "dome", "retractable")

HIGH_WIND_MPH = 20.0
HIGH_GUST_MPH = 30.0
PRECIPITATION_INCHES = 0.05
PRECIPITATION_PROBABILITY_PERCENT = 50
COLD_TEMPERATURE_F = 25.0

# A kickoff whose closest forecast hour is farther away than this is treated
# as outside the useful forecast window (either past Open-Meteo's ~16 day
# horizon, or a gap in the recorded hourly data) rather than picking a
# misleading distant hour.
_MAX_HOUR_DISTANCE = timedelta(minutes=90)

# Static team to stadium table. NFL stadiums are effectively fixed, so this
# module owns its own copy rather than depending on engine.sources.schedule.
STADIUMS: dict[str, dict[str, Any]] = {
    "ARI": {"name": "State Farm Stadium", "latitude": 33.5276, "longitude": -112.2626, "roof": "retractable"},
    "ATL": {"name": "Mercedes-Benz Stadium", "latitude": 33.7554, "longitude": -84.4008, "roof": "retractable"},
    "BAL": {"name": "M&T Bank Stadium", "latitude": 39.2780, "longitude": -76.6227, "roof": "outdoor"},
    "BUF": {"name": "Highmark Stadium", "latitude": 42.7738, "longitude": -78.7870, "roof": "outdoor"},
    "CAR": {"name": "Bank of America Stadium", "latitude": 35.2258, "longitude": -80.8528, "roof": "outdoor"},
    "CHI": {"name": "Soldier Field", "latitude": 41.8623, "longitude": -87.6167, "roof": "outdoor"},
    "CIN": {"name": "Paycor Stadium", "latitude": 39.0955, "longitude": -84.5161, "roof": "outdoor"},
    "CLE": {"name": "Huntington Bank Field", "latitude": 41.5061, "longitude": -81.6995, "roof": "outdoor"},
    "DAL": {"name": "AT&T Stadium", "latitude": 32.7473, "longitude": -97.0945, "roof": "retractable"},
    "DEN": {"name": "Empower Field at Mile High", "latitude": 39.7439, "longitude": -105.0201, "roof": "outdoor"},
    "DET": {"name": "Ford Field", "latitude": 42.3400, "longitude": -83.0456, "roof": "dome"},
    "GB": {"name": "Lambeau Field", "latitude": 44.5013, "longitude": -88.0622, "roof": "outdoor"},
    "HOU": {"name": "NRG Stadium", "latitude": 29.6847, "longitude": -95.4107, "roof": "retractable"},
    "IND": {"name": "Lucas Oil Stadium", "latitude": 39.7601, "longitude": -86.1639, "roof": "retractable"},
    "JAX": {"name": "EverBank Stadium", "latitude": 30.3239, "longitude": -81.6373, "roof": "outdoor"},
    "KC": {"name": "GEHA Field at Arrowhead Stadium", "latitude": 39.0489, "longitude": -94.4839, "roof": "outdoor"},
    "LAC": {"name": "SoFi Stadium", "latitude": 33.9535, "longitude": -118.3392, "roof": "dome"},
    "LAR": {"name": "SoFi Stadium", "latitude": 33.9535, "longitude": -118.3392, "roof": "dome"},
    "LV": {"name": "Allegiant Stadium", "latitude": 36.0909, "longitude": -115.1833, "roof": "dome"},
    "MIA": {"name": "Hard Rock Stadium", "latitude": 25.9580, "longitude": -80.2389, "roof": "outdoor"},
    "MIN": {"name": "U.S. Bank Stadium", "latitude": 44.9738, "longitude": -93.2578, "roof": "dome"},
    "NE": {"name": "Gillette Stadium", "latitude": 42.0909, "longitude": -71.2643, "roof": "outdoor"},
    "NO": {"name": "Caesars Superdome", "latitude": 29.9511, "longitude": -90.0812, "roof": "dome"},
    "NYG": {"name": "MetLife Stadium", "latitude": 40.8135, "longitude": -74.0745, "roof": "outdoor"},
    "NYJ": {"name": "MetLife Stadium", "latitude": 40.8135, "longitude": -74.0745, "roof": "outdoor"},
    "PHI": {"name": "Lincoln Financial Field", "latitude": 39.9008, "longitude": -75.1675, "roof": "outdoor"},
    "PIT": {"name": "Acrisure Stadium", "latitude": 40.4468, "longitude": -80.0158, "roof": "outdoor"},
    "SEA": {"name": "Lumen Field", "latitude": 47.5952, "longitude": -122.3316, "roof": "outdoor"},
    "SF": {"name": "Levi's Stadium", "latitude": 37.4033, "longitude": -121.9694, "roof": "outdoor"},
    "TB": {"name": "Raymond James Stadium", "latitude": 27.9759, "longitude": -82.5033, "roof": "outdoor"},
    "TEN": {"name": "Nissan Stadium", "latitude": 36.1665, "longitude": -86.7713, "roof": "outdoor"},
    "WAS": {"name": "Northwest Stadium", "latitude": 38.9077, "longitude": -76.8645, "roof": "outdoor"},
}


def stadium_for_team(team: str) -> dict[str, Any] | None:
    """Return a copy of the stadium record for team, or None if unknown.

    team is normalized through normalize_team_abbreviation first, so "WSH"
    resolves the same entry as "WAS". The returned dict is always a fresh
    copy of the STADIUMS entry with a "team" key added (the normalized
    abbreviation), so a caller mutating the result can never corrupt the
    module level STADIUMS table.
    """
    code = normalize_team_abbreviation(team)
    entry = STADIUMS.get(code)
    if entry is None:
        return None
    result = dict(entry)
    result["team"] = code
    return result


def is_outdoor(team: str) -> bool:
    """Return True when team's stadium is exposed to weather.

    True for roof "outdoor" or "retractable" (a retractable roof may be
    open on any given Sunday, and this API cannot tell us either way), and
    False for "dome" and for an unrecognized team.
    """
    stadium = stadium_for_team(team)
    if stadium is None:
        return False
    return stadium["roof"] in ("outdoor", "retractable")


def _parse_kickoff(kickoff_utc: str) -> datetime:
    """Parse kickoff_utc into an aware UTC datetime, or raise EngineError.

    A trailing "Z" is accepted by replacing it with "+00:00" first. A
    non-string, an unparseable string, or a naive result (no "Z" and no UTC
    offset in the source string) is a programmer error, not a degraded
    source, so this raises EngineError rather than returning None.
    """
    if not isinstance(kickoff_utc, str):
        raise EngineError(f"kickoff_utc must be a string, got {type(kickoff_utc).__name__}")
    text = kickoff_utc[:-1] + "+00:00" if kickoff_utc.endswith("Z") else kickoff_utc
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise EngineError(f"kickoff_utc is not a valid ISO 8601 timestamp: {kickoff_utc!r}") from exc
    if parsed.tzinfo is None:
        raise EngineError(f"kickoff_utc must be timezone aware: {kickoff_utc!r}")
    return parsed.astimezone(timezone.utc)


def _parse_hourly_time(value: str) -> datetime | None:
    """Parse one hourly.time entry (no timezone suffix) as UTC, or None."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _reading_at(values: Any, index: int) -> Any:
    """Return values[index] if present and not JSON null, else None."""
    if not isinstance(values, list):
        return None
    if index < 0 or index >= len(values):
        return None
    value = values[index]
    if value is None:
        return None
    return value


def _rounded_or_none(raw: Any) -> float | None:
    """Return round_points(raw) when raw is a usable number, else None.

    round_points always calls float(raw), which raises TypeError on None,
    so raw must be checked first: a missing or non-numeric reading must
    stay None rather than crash the whole fetch.
    """
    if raw is None or isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    return round_points(raw)


def _int_or_none(raw: Any) -> int | None:
    """Return int(raw) when raw is a usable number, else None.

    Deliberately not routed through round_points: round_points returns a
    float (wrong declared type for a percentage) and raises on None.
    """
    if raw is None or isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    return int(raw)


def fetch_kickoff_weather(
    team: str,
    kickoff_utc: str,
    *,
    enabled: bool = True,
    cache_root: Path | None = None,
    max_age_seconds: int = WEATHER_MAX_AGE_SECONDS,
    force_refresh: bool = False,
) -> dict[str, Any]:
    """Return the source envelope for team's home stadium at kickoff_utc.

    team is the HOME team's abbreviation; kickoff_utc is an ISO 8601 UTC
    timestamp such as "2026-09-13T17:00:00Z". kickoff_utc is parsed before
    anything else, so a malformed or naive timestamp always raises
    EngineError, even when enabled is False: that is a programmer error in
    the caller, not a degraded source, and must never be swallowed into a
    disabled/unavailable result.

    Order of operations after that parse:

      1. enabled is False: return disabled_result(SOURCE_NAME). No network,
         no disk access.
      2. team does not resolve to a known stadium (stadium_for_team returns
         None): return unavailable_result(SOURCE_NAME, ...). No network.
      3. The stadium's roof is "dome": return an available result with
         indoor=True, conditions=None, flags=[]. No network, no disk
         access, since a dome's conditions are always weather neutral.
      4. Otherwise ("outdoor" or "retractable"): fetch the day's hourly
         forecast for the stadium's coordinates through fetch_cached_json,
         cached under a key derived from the team and the kickoff date so a
         second call against a warm cache costs no network call. Pick the
         hourly index whose parsed time is closest to kickoff_utc. If
         hourly.time is empty or the closest hour is more than 90 minutes
         from kickoff (Open-Meteo's forecast horizon is roughly 16 days;
         a kickoff further out than that has no matching hour), return
         unavailable_result(SOURCE_NAME, ...) instead of guessing.

    A malformed JSON payload (not a dict, no "hourly" key, hourly["time"]
    not a list, or the hourly arrays too short to have any usable time
    entry) and a SourceUnavailable from fetch_cached_json both degrade to
    unavailable_result(SOURCE_NAME, ...); this function's only raise is
    EngineError, and only for the kickoff_utc parsing described above.
    """
    kickoff = _parse_kickoff(kickoff_utc)
    kickoff_normalized = timestamp(kickoff)

    if not enabled:
        return disabled_result(SOURCE_NAME)

    stadium = stadium_for_team(team)
    if stadium is None:
        return unavailable_result(SOURCE_NAME, f"no stadium on file for team {team}")

    if stadium["roof"] == "dome":
        return source_result(
            SOURCE_NAME,
            data={
                "team": stadium["team"],
                "stadium": stadium["name"],
                "roof": "dome",
                "kickoff_utc": kickoff_normalized,
                "indoor": True,
                "conditions": None,
                "flags": [],
            },
            fetched_at=timestamp(),
        )

    date_str = kickoff.strftime("%Y-%m-%d")
    cache_key = f"open-meteo-{stadium['team']}-{kickoff.strftime('%Y%m%d')}"
    url = FORECAST_URL_TEMPLATE.format(
        latitude=stadium["latitude"],
        longitude=stadium["longitude"],
        start_date=date_str,
        end_date=date_str,
    )

    try:
        payload, fetched_at, stale = fetch_cached_json(
            url,
            cache_key,
            max_age_seconds=max_age_seconds,
            cache_root=cache_root,
            service=SOURCE_NAME,
            force_refresh=force_refresh,
        )
    except SourceUnavailable as exc:
        return unavailable_result(SOURCE_NAME, str(exc))

    if not isinstance(payload, dict):
        return unavailable_result(SOURCE_NAME, "open-meteo response was not a JSON object")

    hourly = payload.get("hourly")
    if not isinstance(hourly, dict):
        return unavailable_result(SOURCE_NAME, "open-meteo response had no hourly block")

    times = hourly.get("time")
    if not isinstance(times, list):
        return unavailable_result(SOURCE_NAME, "open-meteo response had no forecast hours")
    if not times:
        return unavailable_result(SOURCE_NAME, "no forecast hour near kickoff")

    best_index: int | None = None
    best_distance: timedelta | None = None
    for index, raw_time in enumerate(times):
        parsed = _parse_hourly_time(raw_time)
        if parsed is None:
            continue
        distance = abs(parsed - kickoff)
        if best_distance is None or distance < best_distance:
            best_distance = distance
            best_index = index

    if best_index is None or best_distance is None or best_distance > _MAX_HOUR_DISTANCE:
        return unavailable_result(SOURCE_NAME, "no forecast hour near kickoff")

    forecast_hour = _parse_hourly_time(times[best_index])
    conditions = {
        "forecast_hour_utc": timestamp(forecast_hour),
        "temperature_f": _rounded_or_none(_reading_at(hourly.get("temperature_2m"), best_index)),
        "wind_mph": _rounded_or_none(_reading_at(hourly.get("wind_speed_10m"), best_index)),
        "wind_gust_mph": _rounded_or_none(_reading_at(hourly.get("wind_gusts_10m"), best_index)),
        "precipitation_in": _rounded_or_none(_reading_at(hourly.get("precipitation"), best_index)),
        "precipitation_probability": _int_or_none(
            _reading_at(hourly.get("precipitation_probability"), best_index)
        ),
    }

    return source_result(
        SOURCE_NAME,
        stale=stale,
        data={
            "team": stadium["team"],
            "stadium": stadium["name"],
            "roof": stadium["roof"],
            "kickoff_utc": kickoff_normalized,
            "indoor": False,
            "source_url": url,
            "conditions": conditions,
            "flags": weather_flags(conditions),
        },
        fetched_at=fetched_at,
    )


def weather_flags(conditions: dict[str, Any] | None) -> list[str]:
    """Return a sorted list of plain flag strings for one hour's conditions.

    conditions is the {"temperature_f", "wind_mph", "wind_gust_mph",
    "precipitation_in", "precipitation_probability"} mapping produced by
    fetch_kickoff_weather, or None. A missing (None) reading never trips
    its flag. This is pure and deterministic: the same conditions always
    produce the same flags, with no judgement beyond the fixed thresholds
    HIGH_WIND_MPH, HIGH_GUST_MPH, PRECIPITATION_INCHES,
    PRECIPITATION_PROBABILITY_PERCENT and COLD_TEMPERATURE_F.
    """
    if not isinstance(conditions, dict):
        return []

    flags: list[str] = []

    wind_mph = conditions.get("wind_mph")
    if isinstance(wind_mph, (int, float)) and wind_mph >= HIGH_WIND_MPH:
        flags.append("high wind")

    wind_gust_mph = conditions.get("wind_gust_mph")
    if isinstance(wind_gust_mph, (int, float)) and wind_gust_mph >= HIGH_GUST_MPH:
        flags.append("high gusts")

    precipitation_in = conditions.get("precipitation_in")
    if isinstance(precipitation_in, (int, float)) and precipitation_in >= PRECIPITATION_INCHES:
        flags.append("precipitation")

    precipitation_probability = conditions.get("precipitation_probability")
    if (
        isinstance(precipitation_probability, (int, float))
        and precipitation_probability >= PRECIPITATION_PROBABILITY_PERCENT
    ):
        flags.append("precipitation likely")

    temperature_f = conditions.get("temperature_f")
    if isinstance(temperature_f, (int, float)) and temperature_f <= COLD_TEMPERATURE_F:
        flags.append("extreme cold")

    return sorted(flags)
