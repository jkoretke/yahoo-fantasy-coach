"""Read ESPN's free, no-auth public scoreboard endpoint for one NFL week.

This module answers one question per NFL week: for every game, who plays
whom, when (kickoff, in UTC), and at what venue. That is the ground truth a
later gameday routine needs to decide whether "today" matters for a given
team, and what weather.py needs before it can decide whether an outdoor
stadium's forecast is even relevant. Building that gameday routine, and
wiring the outdoor-stadium decision into weather.py, are both later work;
this module only produces the schedule data.

ENDPOINT (undocumented, no API key, confirmed live during planning against
season 2025, seasontype 2, week 4, and re-confirmed on plan review; ESPN
publishes no contract for site.api.espn.com, so this shape is recorded from
observation, not from a spec):

SCOREBOARD_URL_TEMPLATE.format(season=..., season_type=..., week=...) ->
    "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
    "?dates={season}&seasontype={season_type}&week={week}"

Top level response keys: events, leagues, provider, season, week. events is
a list (16 games for a normal regular season week). Each entry:

    id: "401772938" (a string, not an int)
    date: "2025-09-26T00:15Z"
        UTC, minute precision, no seconds, a literal trailing "Z". Python's
        datetime.fromisoformat cannot parse a bare trailing "Z" on every
        supported version, so this module replaces it with "+00:00" before
        parsing, then re-renders through engine.common.timestamp() so every
        kickoff_utc this module emits has seconds and a "Z"
        ("2025-09-26T00:15:00Z").
    name / shortName: "Seattle Seahawks at Arizona Cardinals" / "SEA @ ARI"
    season: {"year": 2025, "type": 2, "slug": "regular-season"}
    week: {"number": 4}
    status.type: {"id", "name", "state", "completed", "description",
        "detail", "shortDetail"}. state is one of "pre", "in", "post".

    competitions[0] (the module only ever reads index 0):
        venue: {"id", "fullName", "address": {"city", "state", "country"},
            "indoor"}. "indoor" is a real boolean on this payload.
            "state" and "country" may be absent for an international game.
        competitors: a two element list, each
            {"homeAway": "home" | "away", "team": {"abbreviation": ...,
            "displayName": ...}}. ESPN sends "WSH" for Washington; every
            abbreviation this module emits is passed through
            engine.sources.base.normalize_team_abbreviation.
        neutralSite: boolean.

An event whose date cannot be parsed, or that has no competitions or fewer
than two competitors, is skipped rather than failing the whole fetch: one
malformed event in a 16 game response must not blank out the other 15.

Cache and degradation: fetch_week_schedule reads and writes through
engine.sources.base.fetch_cached_json, so a fresh on-disk cache entry never
touches the network, a stale entry is served when a refetch fails, and a
totally dead endpoint (no cache at all) or a well formed but wrong shaped
JSON body both degrade to unavailable_result(...) rather than raising out of
this module. The only exception fetch_week_schedule may raise is
engine.common.EngineError, and only for a genuine programmer error (an
invalid cache key), never for anything ESPN itself can send back.

Current week: the same endpoint called with no dates/seasontype/week query
string at all answers a different question, "which week is it right now",
in its own top level "season" and "week" keys. fetch_current_week reads
exactly that and nothing else; see its docstring for why it is the one
function in this module that refuses a stale cache entry.

Public names: SOURCE_NAME, SCOREBOARD_URL_TEMPLATE, CURRENT_SCOREBOARD_URL,
PRESEASON_TYPE, REGULAR_SEASON_TYPE, POSTSEASON_TYPE,
SCHEDULE_MAX_AGE_SECONDS, CURRENT_WEEK_MAX_AGE_SECONDS, fetch_week_schedule,
fetch_current_week, kickoff_by_team, teams_playing, earliest_kickoff,
game_for_team.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from engine.common import timestamp
from engine.sources.base import (
    SourceUnavailable, fetch_cached_json, source_result, disabled_result,
    unavailable_result, normalize_team_abbreviation,
)

SOURCE_NAME = "schedule"
SCOREBOARD_URL_TEMPLATE = (
    "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
    "?dates={season}&seasontype={season_type}&week={week}"
)
CURRENT_SCOREBOARD_URL = (
    "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
)
PRESEASON_TYPE = 1
REGULAR_SEASON_TYPE = 2
POSTSEASON_TYPE = 3
SCHEDULE_MAX_AGE_SECONDS = 21600
# Deliberately far shorter than SCHEDULE_MAX_AGE_SECONDS. A week's game list
# barely moves once published, but the answer to "which week is it" changes
# the moment ESPN rolls over, and every downstream fetch is keyed on it.
CURRENT_WEEK_MAX_AGE_SECONDS = 3600


def _parse_kickoff(raw: Any) -> str | None:
    """Return the normalized "YYYY-MM-DDTHH:MM:SSZ" kickoff time for raw.

    raw is expected to be ESPN's own date string, e.g. "2025-09-26T00:15Z".
    A trailing "Z" is swapped for "+00:00" before datetime.fromisoformat, a
    naive result (no offset at all in the source string) is treated as UTC,
    and the result is re-rendered through engine.common.timestamp() so
    every value this module emits has second precision and a trailing "Z".
    Returns None for anything missing, blank, or unparseable; this never
    raises, since a single bad date must degrade to "skip this event", not
    fail the whole fetch.
    """
    if not isinstance(raw, str) or not raw:
        return None
    text = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return timestamp(parsed)


def _parse_event(event: Any) -> dict[str, Any] | None:
    """Return one games[] entry for a single raw ESPN event, or None to skip it.

    Returns None (skip, never raise) when event is not a dict, its date is
    missing or unparseable, or competitions[0].competitors has fewer than
    two entries. Every other field defaults to "" or False when the
    expected key or nesting is absent, so a partially malformed event still
    contributes whatever it can rather than being dropped outright.
    """
    if not isinstance(event, dict):
        return None

    kickoff_utc = _parse_kickoff(event.get("date"))
    if kickoff_utc is None:
        return None

    raw_id = event.get("id")
    game_id = str(raw_id) if raw_id is not None else ""

    competitions = event.get("competitions")
    if not isinstance(competitions, list) or not competitions:
        return None
    competition = competitions[0]
    if not isinstance(competition, dict):
        return None

    competitors = competition.get("competitors")
    if not isinstance(competitors, list) or len(competitors) < 2:
        return None

    home_team = ""
    away_team = ""
    for competitor in competitors:
        if not isinstance(competitor, dict):
            continue
        team = competitor.get("team")
        abbreviation = team.get("abbreviation") if isinstance(team, dict) else None
        normalized = normalize_team_abbreviation(abbreviation)
        home_away = competitor.get("homeAway")
        if home_away == "home":
            home_team = normalized
        elif home_away == "away":
            away_team = normalized

    venue = competition.get("venue")
    venue_name = ""
    city = ""
    state = ""
    country = ""
    indoor = False
    if isinstance(venue, dict):
        venue_name = venue.get("fullName") or ""
        address = venue.get("address")
        if isinstance(address, dict):
            city = address.get("city") or ""
            state = address.get("state") or ""
            country = address.get("country") or ""
        indoor = bool(venue.get("indoor", False))

    neutral_site = bool(competition.get("neutralSite", False))

    status = event.get("status")
    status_type = status.get("type") if isinstance(status, dict) else None
    status_state = ""
    status_detail = ""
    completed = False
    if isinstance(status_type, dict):
        status_state = status_type.get("state") or ""
        status_detail = status_type.get("detail") or ""
        completed = bool(status_type.get("completed", False))

    return {
        "game_id": game_id,
        "kickoff_utc": kickoff_utc,
        "name": event.get("name") or "",
        "short_name": event.get("shortName") or "",
        "home_team": home_team,
        "away_team": away_team,
        "venue": venue_name,
        "city": city,
        "state": state,
        "country": country,
        "indoor": indoor,
        "neutral_site": neutral_site,
        "status_state": status_state,
        "status_detail": status_detail,
        "completed": completed,
    }


def fetch_week_schedule(
    season: int,
    week: int,
    *,
    season_type: int = REGULAR_SEASON_TYPE,
    enabled: bool = True,
    cache_root: Path | None = None,
    max_age_seconds: int = SCHEDULE_MAX_AGE_SECONDS,
    force_refresh: bool = False,
) -> dict[str, Any]:
    """Return the result envelope for the season/week/season_type scoreboard.

    season, week and season_type are always taken as given; this function
    never guesses them from the clock or from a fixture. enabled=False
    returns disabled_result(SOURCE_NAME) immediately, before the cache key
    or URL are even built, so it makes zero network and zero disk calls.

    On success, envelope["data"] is:
        {"season": season, "week": week, "season_type": season_type,
         "source_url": <url used>, "games": [...], "count": len(games)}
    where each games[] entry is documented on _parse_event above. games is
    sorted by (kickoff_utc, game_id) so the output order never depends on
    the order ESPN happened to return events in.

    Any SourceUnavailable raised while fetching or reading the cache (a
    fetch failure with no cache to fall back on) is caught here and turned
    into unavailable_result(SOURCE_NAME, reason). A response that decodes
    as JSON but is not a dict, or is a dict with no "events" list, is
    likewise turned into unavailable_result(...) rather than raised. This
    function never raises anything except engine.common.EngineError, and
    only for a genuine caller error such as a malformed cache key.
    """
    if not enabled:
        return disabled_result(SOURCE_NAME)

    url = SCOREBOARD_URL_TEMPLATE.format(season=season, season_type=season_type, week=week)
    cache_key = f"espn-scoreboard-{season}-st{season_type}-wk{week:02d}"

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
        return unavailable_result(
            SOURCE_NAME, "espn scoreboard response was not a JSON object"
        )
    raw_events = payload.get("events")
    if not isinstance(raw_events, list):
        return unavailable_result(
            SOURCE_NAME, "espn scoreboard response has no events list"
        )

    games: list[dict[str, Any]] = []
    for raw_event in raw_events:
        parsed = _parse_event(raw_event)
        if parsed is not None:
            games.append(parsed)
    games.sort(key=lambda game: (game["kickoff_utc"], game["game_id"]))

    data = {
        "season": season,
        "week": week,
        "season_type": season_type,
        "source_url": url,
        "games": games,
        "count": len(games),
    }
    return source_result(SOURCE_NAME, data=data, stale=stale, fetched_at=fetched_at)


def fetch_current_week(
    *,
    enabled: bool = True,
    cache_root: Path | None = None,
    max_age_seconds: int = CURRENT_WEEK_MAX_AGE_SECONDS,
    force_refresh: bool = False,
) -> dict[str, Any]:
    """Return the result envelope for "which NFL week is it right now".

    Reads CURRENT_SCOREBOARD_URL, the same ESPN endpoint
    fetch_week_schedule uses but with no query string, whose top level
    "season" ({"type": 2, "year": 2026}) and "week" ({"number": 1}) keys
    describe the current week rather than a requested one. Confirmed live
    against that endpoint on 2026-08-31.

    On success, envelope["data"] is:
        {"season": <year>, "week": <number>, "season_type": <type>,
         "source_url": CURRENT_SCOREBOARD_URL}
    All three are ints. The games list is deliberately not parsed here:
    this function answers only which week it is, and the caller then asks
    fetch_week_schedule for that week's games through its own cache key.

    THIS IS THE ONE FUNCTION IN THIS MODULE THAT REFUSES A STALE CACHE
    ENTRY, inverting the house rule that stale data beats no data. Every
    other source degrades one section of a brief; this one names the week
    every other fetch is then keyed on, so a stale hit does not degrade a
    run, it silently runs the entire week against the wrong week's data. A
    stale entry is reported as unavailable_result(...) instead, and the
    caller (engine.run_common.resolve_week) turns that into a hard
    EngineError.

    Like fetch_week_schedule: enabled=False returns disabled_result
    immediately with zero network and zero disk calls, and every other
    failure (a dead endpoint, a non-object body, a missing or unparseable
    season/week block) degrades to unavailable_result rather than raising.
    """
    if not enabled:
        return disabled_result(SOURCE_NAME)

    try:
        payload, fetched_at, stale = fetch_cached_json(
            CURRENT_SCOREBOARD_URL,
            "espn-scoreboard-current",
            max_age_seconds=max_age_seconds,
            cache_root=cache_root,
            service=SOURCE_NAME,
            force_refresh=force_refresh,
        )
    except SourceUnavailable as exc:
        return unavailable_result(SOURCE_NAME, str(exc))

    if stale:
        return unavailable_result(
            SOURCE_NAME,
            "espn current-week response is stale and the week number must be current",
        )

    if not isinstance(payload, dict):
        return unavailable_result(
            SOURCE_NAME, "espn current-week response was not a JSON object"
        )

    season_block = payload.get("season")
    week_block = payload.get("week")
    if not isinstance(season_block, dict) or not isinstance(week_block, dict):
        return unavailable_result(
            SOURCE_NAME, "espn current-week response has no season/week block"
        )

    season = _as_int(season_block.get("year"))
    season_type = _as_int(season_block.get("type"))
    week = _as_int(week_block.get("number"))
    if season is None or season_type is None or week is None:
        return unavailable_result(
            SOURCE_NAME, "espn current-week response has an unreadable season/week block"
        )

    data = {
        "season": season,
        "week": week,
        "season_type": season_type,
        "source_url": CURRENT_SCOREBOARD_URL,
    }
    return source_result(SOURCE_NAME, data=data, stale=False, fetched_at=fetched_at)


def _as_int(value: Any) -> int | None:
    """Return value as an int, or None when it is not usable as one.

    ESPN sends these as real JSON numbers, but a bool is an int in Python
    and a string year would still parse, so both are rejected explicitly
    rather than quietly becoming a week number.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def kickoff_by_team(schedule_data: dict[str, Any]) -> dict[str, str]:
    """Return {team_abbreviation: kickoff_utc} for every team with a game.

    schedule_data is the "data" dict returned inside fetch_week_schedule's
    envelope (not the whole envelope). A team on a bye is simply absent
    from the result, since it never appears as a home or away team in any
    game. This is a pure function: no network, no disk. Anything unusable
    (schedule_data is not a dict, or has no "games" list) returns {}.
    """
    if not isinstance(schedule_data, dict):
        return {}
    games = schedule_data.get("games")
    if not isinstance(games, list):
        return {}

    mapping: dict[str, str] = {}
    for game in games:
        if not isinstance(game, dict):
            continue
        kickoff = game.get("kickoff_utc")
        if not isinstance(kickoff, str) or not kickoff:
            continue
        home_team = game.get("home_team")
        away_team = game.get("away_team")
        if isinstance(home_team, str) and home_team:
            mapping[home_team] = kickoff
        if isinstance(away_team, str) and away_team:
            mapping[away_team] = kickoff
    return mapping


def teams_playing(schedule_data: dict[str, Any]) -> list[str]:
    """Return the sorted list of every team abbreviation with a game this week.

    Pure function; delegates to kickoff_by_team, so unusable input returns
    an empty list.
    """
    return sorted(kickoff_by_team(schedule_data).keys())


def earliest_kickoff(
    schedule_data: dict[str, Any], teams: list[str] | None = None
) -> str | None:
    """Return the earliest kickoff_utc among teams, or across all games.

    Each entry in teams is passed through normalize_team_abbreviation
    before the lookup, so a caller may pass a raw feed's spelling (e.g.
    "wsh") and still match this module's own "WAS" keyed games. When teams
    is None, the earliest kickoff across every game this week is returned.
    Returns None when schedule_data is unusable, this week has no games at
    all, or none of the given teams play this week (for example, every
    named team is on a bye). This is a pure function: no network, no disk.
    This is the raw lookup a later gameday routine will build its
    "does today matter" decision on; that routine is not built here.
    """
    mapping = kickoff_by_team(schedule_data)
    if not mapping:
        return None

    if teams is None:
        return min(mapping.values())

    kickoffs = [
        mapping[normalized]
        for normalized in (normalize_team_abbreviation(team) for team in teams)
        if normalized in mapping
    ]
    if not kickoffs:
        return None
    return min(kickoffs)


def game_for_team(schedule_data: dict[str, Any], team: str) -> dict[str, Any] | None:
    """Return the games[] entry team plays in this week, or None.

    team is passed through normalize_team_abbreviation before matching
    against each game's home_team/away_team. Pure function: no network, no
    disk. Returns None for unusable schedule_data, a blank/unrecognized
    team, or a team that is on a bye this week.
    """
    if not isinstance(schedule_data, dict):
        return None
    games = schedule_data.get("games")
    if not isinstance(games, list):
        return None

    normalized = normalize_team_abbreviation(team)
    if not normalized:
        return None

    for game in games:
        if not isinstance(game, dict):
            continue
        if game.get("home_team") == normalized or game.get("away_team") == normalized:
            return game
    return None
