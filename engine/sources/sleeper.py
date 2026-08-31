"""Sleeper source: weekly projections, the full player index, and trending
adds/drops, read from Sleeper's free, no-auth API.

Sleeper publishes three endpoints this module wraps, each documented here
with its confirmed real response shape (probed live during planning; these
are undocumented endpoints and either one might change or be retired
without notice):

Weekly projections (fetch_projections)
    PROJECTIONS_URL_TEMPLATE
    ("https://api.sleeper.app/projections/nfl/{season}/{week}?season_type=regular&order_by=pts_ppr")
    returns a JSON LIST of entry objects, one per player, shaped like:
        {"status", "date", "last_modified", "stats": {...}, "category": "proj",
         "week": int, "season": "2025" (a STRING), "season_type": "regular",
         "sport": "nfl",
         "player": {"fantasy_positions": [...], "first_name", "last_name",
                     "position", "team", "team_abbr", "injury_status",
                     "injury_body_part", "injury_notes", "injury_start_date",
                     "metadata", "news_updated", "team_changed_at",
                     "years_exp"},
         "team", "player_id", "opponent", "game_id", "company"}
    stats is a raw projected stat line, for example {"pass_yd": 247.03,
    "pass_td": 1.72, "pts_ppr": 25.24, ...}. Many entries are inactive
    players whose stats hold nothing but {"adp_dd_ppr": 1000.0} and no
    pts_ppr at all; this module's parser tolerates that rather than
    crashing, and reports projected_points 0.0 for such an entry.
    LEGACY_PROJECTIONS_URL_TEMPLATE
    ("https://api.sleeper.app/v1/projections/nfl/regular/{season}/{week}")
    is a second, older endpoint returning the same underlying projections
    as a JSON DICT keyed by player_id, whose values are the bare stats
    dicts with no player block at all. fetch_projections accepts EITHER
    top level shape from EITHER url (the shape is detected from the
    payload's own type, not from which url was requested), so a silent
    endpoint swap on Sleeper's side still parses instead of breaking.

Player index (fetch_player_index)
    PLAYERS_URL ("https://api.sleeper.app/v1/players/nfl") returns a JSON
    DICT keyed by Sleeper player_id, each value holding at least
    first_name, last_name, full_name, position, fantasy_positions (a list
    or null), team (an abbreviation string or null), injury_status, active
    and search_full_name. The real response is roughly 5 MB, which is why
    PLAYERS_MAX_AGE_SECONDS is a full day rather than an hour, and why
    nothing in this module's test suite fetches it for real.

Trending adds and drops (fetch_trending)
    TRENDING_URL_TEMPLATE
    ("https://api.sleeper.app/v1/players/nfl/trending/{kind}?lookback_hours={lookback_hours}&limit={limit}")
    with kind "add" or "drop" returns a JSON LIST of
    {"count": int, "player_id": str} objects, in the order Sleeper ranked
    them; that order is preserved in this module's output.

Every public fetch_ function takes enabled: bool = True as a plain keyword
parameter. There is no config/league.yaml in this repo yet; wiring this
parameter to one is Phase 3 work, so this module never reads such a file.
No public function ever raises for a network failure or a malformed
response: each one catches engine.sources.base.SourceUnavailable and any
JSON payload of the wrong shape, returning
engine.sources.base.unavailable_result(...) so one dead Sleeper endpoint
degrades this source, not the whole run. The only exception a public
function may raise is engine.common.EngineError, and only for a
programmer error such as an unrecognized trending kind.

SCOPE LIMIT: this module deliberately stops at exposing normalized player
names and a name -> Sleeper player_id index (player_id_by_normalized_name).
It does NOT reconcile Sleeper's player ids with this repo's own player_id
values in fixtures/sample_league/players.json, and it does NOT feed
engine/scoring.py. That identity join needs Yahoo's canonical ids and is
Phase 3 work; attempting it here would mean guessing ahead of data this
module does not have.

Public names: SOURCE_NAME, PLAYERS_URL, PROJECTIONS_URL_TEMPLATE,
LEGACY_PROJECTIONS_URL_TEMPLATE, TRENDING_URL_TEMPLATE, TRENDING_KINDS,
PROJECTIONS_MAX_AGE_SECONDS, PLAYERS_MAX_AGE_SECONDS,
TRENDING_MAX_AGE_SECONDS, fetch_projections, fetch_player_index,
fetch_trending, player_id_by_normalized_name.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from engine.common import EngineError, round_points
from engine.sources.base import (
    SourceUnavailable, fetch_cached_json, source_result, disabled_result,
    unavailable_result, normalize_name, normalize_team_abbreviation,
)

SOURCE_NAME = "sleeper"

PLAYERS_URL = "https://api.sleeper.app/v1/players/nfl"
PROJECTIONS_URL_TEMPLATE = (
    "https://api.sleeper.app/projections/nfl/{season}/{week}"
    "?season_type=regular&order_by=pts_ppr"
)
LEGACY_PROJECTIONS_URL_TEMPLATE = (
    "https://api.sleeper.app/v1/projections/nfl/regular/{season}/{week}"
)
TRENDING_URL_TEMPLATE = (
    "https://api.sleeper.app/v1/players/nfl/trending/{kind}"
    "?lookback_hours={lookback_hours}&limit={limit}"
)

TRENDING_KINDS = ("add", "drop")

PROJECTIONS_MAX_AGE_SECONDS = 3600
PLAYERS_MAX_AGE_SECONDS = 86400
TRENDING_MAX_AGE_SECONDS = 3600


class _ShapeError(Exception):
    """A JSON payload decoded fine but was not the shape this module reads.

    This is a private, module internal signal only: every public function
    catches it right next to SourceUnavailable and converts it into the
    same unavailable_result(...) envelope, so a caller never sees this
    type and never needs to know it exists.
    """


def _clean_stats(stats: Any) -> dict[str, float]:
    """Return the int/float entries of stats, dropping everything else.

    stats is expected to be a dict but is not assumed to be one; a
    non-dict input returns {}. Booleans are excluded even though bool is
    technically an int subclass, since a raw stat line never legitimately
    holds one.
    """
    if not isinstance(stats, dict):
        return {}
    cleaned: dict[str, float] = {}
    for key, value in stats.items():
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            cleaned[key] = value
    return cleaned


def _projected_points(stats: Any) -> float:
    """Return round_points(stats["pts_ppr"]) when that is an int or float, else 0.0.

    This is the only place round_points is called in this module, and it
    is guarded so round_points (which raises TypeError on None) is never
    handed a null or non-numeric reading.
    """
    if not isinstance(stats, dict):
        return 0.0
    value = stats.get("pts_ppr")
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        return round_points(value)
    return 0.0


def _entry_name(player: dict[str, Any]) -> str:
    """Return "first_name last_name" joined and stripped, falling back to full_name."""
    first = player.get("first_name")
    last = player.get("last_name")
    parts = [part.strip() for part in (first, last) if isinstance(part, str) and part.strip()]
    if parts:
        return " ".join(parts)
    full_name = player.get("full_name")
    if isinstance(full_name, str) and full_name.strip():
        return full_name.strip()
    return ""


def _string_or_blank(value: Any) -> str:
    """Return value if it is a string, else "" (never None)."""
    return value if isinstance(value, str) else ""


def _parse_projections(payload: Any, *, season: int, week: int, source_url: str) -> dict[str, Any]:
    """Parse either confirmed top level shape of a projections payload.

    A JSON LIST is read as the primary shape (one entry object per
    player). A JSON DICT is read as the legacy shape (player_id -> bare
    stats dict), and every value produced from it has "" for name,
    normalized_name, position, nfl_team, opponent and injury_status,
    since that shape carries no player block at all. Anything else (a
    string, a number, or null) raises _ShapeError. A non-empty list or
    dict payload that yields zero entries (for example a list of
    non-dicts, a list of dicts none of which carry a usable player_id, or
    a dict error body like {"error": "not found"} whose only value is a
    non-dict) also raises _ShapeError, since that is a wrong shaped
    payload, not a legitimate empty result; a genuinely empty list ([])
    or dict ({}) still parses to an empty, available result, since that
    is a legitimate "no projections this week" answer.
    """
    projections: dict[str, Any] = {}

    if isinstance(payload, list):
        for entry in payload:
            if not isinstance(entry, dict):
                continue
            raw_player_id = entry.get("player_id")
            if not raw_player_id:
                continue
            player_id = str(raw_player_id)

            stats = entry.get("stats") if isinstance(entry.get("stats"), dict) else {}
            player = entry.get("player") if isinstance(entry.get("player"), dict) else {}

            name = _entry_name(player)
            raw_team = entry.get("team") or player.get("team")
            projections[player_id] = {
                "player_id": player_id,
                "name": name,
                "normalized_name": normalize_name(name),
                "position": _string_or_blank(player.get("position")),
                "nfl_team": normalize_team_abbreviation(raw_team),
                "opponent": normalize_team_abbreviation(entry.get("opponent")),
                "injury_status": _string_or_blank(player.get("injury_status")),
                "projected_points": _projected_points(stats),
                "stats": _clean_stats(stats),
            }
        if payload and not projections:
            raise _ShapeError(
                "sleeper projections payload is a non-empty list but no entry carried "
                "a usable player_id"
            )
    elif isinstance(payload, dict):
        for raw_player_id, stats in payload.items():
            if not isinstance(stats, dict):
                continue
            if not raw_player_id:
                continue
            player_id = str(raw_player_id)
            projections[player_id] = {
                "player_id": player_id,
                "name": "",
                "normalized_name": "",
                "position": "",
                "nfl_team": "",
                "opponent": "",
                "injury_status": "",
                "projected_points": _projected_points(stats),
                "stats": _clean_stats(stats),
            }
        if payload and not projections:
            raise _ShapeError(
                "sleeper projections payload is a non-empty dict but no entry matched "
                "the legacy player_id -> stats shape"
            )
    else:
        raise _ShapeError("sleeper projections payload has an unrecognized top level shape")

    return {
        "season": season,
        "week": week,
        "source_url": source_url,
        "projections": projections,
        "count": len(projections),
    }


def fetch_projections(
    season: int,
    week: int,
    *,
    enabled: bool = True,
    cache_root: Path | None = None,
    max_age_seconds: int = PROJECTIONS_MAX_AGE_SECONDS,
    force_refresh: bool = False,
    use_legacy_url: bool = False,
) -> dict[str, Any]:
    """Return the source_result envelope for one week's Sleeper projections.

    season and week are always explicit; this function never derives
    either from the clock. use_legacy_url switches from
    PROJECTIONS_URL_TEMPLATE to LEGACY_PROJECTIONS_URL_TEMPLATE, and the
    two urls use different cache keys ("sleeper-projections-{season}-wk{week:02d}"
    versus "sleeper-projections-legacy-{season}-wk{week:02d}") so warming
    one never masks the other. The parser accepts either top level payload
    shape regardless of which url was requested, so a silent endpoint
    swap on Sleeper's side still parses.

    enabled=False returns disabled_result(SOURCE_NAME) immediately, with
    no network or disk access at all. A network failure
    (SourceUnavailable) or a payload of the wrong shape both return
    unavailable_result(SOURCE_NAME, reason); this function never raises
    for either. On success the envelope's data is:
        {"season", "week", "source_url",
         "projections": {player_id: {"player_id", "name", "normalized_name",
             "position", "nfl_team", "opponent", "injury_status",
             "projected_points", "stats"}},
         "count"}
    """
    if not enabled:
        return disabled_result(SOURCE_NAME)

    if use_legacy_url:
        url = LEGACY_PROJECTIONS_URL_TEMPLATE.format(season=season, week=week)
        cache_key = f"sleeper-projections-legacy-{season}-wk{week:02d}"
    else:
        url = PROJECTIONS_URL_TEMPLATE.format(season=season, week=week)
        cache_key = f"sleeper-projections-{season}-wk{week:02d}"

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

    try:
        data = _parse_projections(payload, season=season, week=week, source_url=url)
    except _ShapeError as exc:
        return unavailable_result(SOURCE_NAME, str(exc))

    return source_result(SOURCE_NAME, data=data, stale=stale, fetched_at=fetched_at)


def _parse_player_index(payload: Any) -> dict[str, Any]:
    """Parse the Sleeper player index dict into this module's documented shape.

    Raises _ShapeError when payload is not a JSON object. A value under a
    given player_id that is itself not a dict is skipped rather than
    failing the whole call.
    """
    if not isinstance(payload, dict):
        raise _ShapeError("sleeper player index payload is not a JSON object")

    players: dict[str, Any] = {}
    for raw_player_id, value in payload.items():
        if not isinstance(value, dict):
            continue
        player_id = str(raw_player_id)
        name = _entry_name(value)
        position = _string_or_blank(value.get("position"))
        positions = value.get("fantasy_positions")
        if not isinstance(positions, list):
            positions = [position] if position else []
        players[player_id] = {
            "player_id": player_id,
            "name": name,
            "normalized_name": normalize_name(name),
            "position": position,
            "positions": positions,
            "nfl_team": normalize_team_abbreviation(value.get("team")),
            "injury_status": _string_or_blank(value.get("injury_status")),
            "active": bool(value.get("active")),
        }

    return {"players": players, "count": len(players)}


def fetch_player_index(
    *,
    enabled: bool = True,
    cache_root: Path | None = None,
    max_age_seconds: int = PLAYERS_MAX_AGE_SECONDS,
    force_refresh: bool = False,
) -> dict[str, Any]:
    """Return the source_result envelope for Sleeper's full NFL player index.

    Fetches PLAYERS_URL under cache key "sleeper-players". enabled=False
    returns disabled_result(SOURCE_NAME) immediately with no network or
    disk access. A network failure or a payload of the wrong shape (for
    example a JSON list instead of an object) both return
    unavailable_result(SOURCE_NAME, reason); this function never raises
    for either. On success the envelope's data is:
        {"players": {player_id: {"player_id", "name", "normalized_name",
            "position", "positions" (always a list), "nfl_team",
            "injury_status", "active"}},
         "count"}
    """
    if not enabled:
        return disabled_result(SOURCE_NAME)

    cache_key = "sleeper-players"

    try:
        payload, fetched_at, stale = fetch_cached_json(
            PLAYERS_URL,
            cache_key,
            max_age_seconds=max_age_seconds,
            cache_root=cache_root,
            service=SOURCE_NAME,
            force_refresh=force_refresh,
        )
    except SourceUnavailable as exc:
        return unavailable_result(SOURCE_NAME, str(exc))

    try:
        data = _parse_player_index(payload)
    except _ShapeError as exc:
        return unavailable_result(SOURCE_NAME, str(exc))

    return source_result(SOURCE_NAME, data=data, stale=stale, fetched_at=fetched_at)


def _parse_trending(payload: Any, *, kind: str, limit: int, lookback_hours: int) -> dict[str, Any]:
    """Parse the Sleeper trending payload into this module's documented shape.

    Raises _ShapeError when payload is not a JSON list. An entry that is
    not a dict, or is missing a usable player_id or an int count, is
    skipped rather than failing the whole call. Order is preserved. A
    non-empty list that yields zero usable players (for example
    [{"bogus": 1}, {"x": 2}], where no entry has both a player_id and an
    int count) also raises _ShapeError, since that is a wrong shaped
    payload, not a legitimate empty result; a genuinely empty list ([])
    still parses to an empty, available result, since that is a
    legitimate "nothing trending right now" answer.
    """
    if not isinstance(payload, list):
        raise _ShapeError("sleeper trending payload is not a JSON list")

    players: list[dict[str, Any]] = []
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        raw_player_id = entry.get("player_id")
        count = entry.get("count")
        if not raw_player_id:
            continue
        if not isinstance(count, int) or isinstance(count, bool):
            continue
        players.append({"player_id": str(raw_player_id), "count": count})

    if payload and not players:
        raise _ShapeError("sleeper trending payload is a non-empty list but no entry was usable")

    return {
        "kind": kind,
        "limit": limit,
        "lookback_hours": lookback_hours,
        "players": players,
        "count": len(players),
    }


def fetch_trending(
    kind: str = "add",
    *,
    limit: int = 25,
    lookback_hours: int = 24,
    enabled: bool = True,
    cache_root: Path | None = None,
    max_age_seconds: int = TRENDING_MAX_AGE_SECONDS,
    force_refresh: bool = False,
) -> dict[str, Any]:
    """Return the source_result envelope for Sleeper's trending add/drop list.

    kind must be one of TRENDING_KINDS ("add", "drop"); an unrecognized
    kind raises EngineError, a programmer error rather than an outage, and
    that check happens BEFORE the enabled check so a bad kind is caught
    even when the source is switched off.

    enabled=False returns disabled_result(SOURCE_NAME) with no network or
    disk access. A network failure or a payload of the wrong shape both
    return unavailable_result(SOURCE_NAME, reason); this function never
    raises for either. On success the envelope's data is:
        {"kind", "limit", "lookback_hours",
         "players": [{"player_id", "count"}, ...] in the order Sleeper
             returned them,
         "count"}
    """
    if kind not in TRENDING_KINDS:
        raise EngineError(f"unknown sleeper trending kind: {kind!r}")

    if not enabled:
        return disabled_result(SOURCE_NAME)

    url = TRENDING_URL_TEMPLATE.format(kind=kind, lookback_hours=lookback_hours, limit=limit)
    cache_key = f"sleeper-trending-{kind}-{lookback_hours}h-{limit}"

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

    try:
        data = _parse_trending(payload, kind=kind, limit=limit, lookback_hours=lookback_hours)
    except _ShapeError as exc:
        return unavailable_result(SOURCE_NAME, str(exc))

    return source_result(SOURCE_NAME, data=data, stale=stale, fetched_at=fetched_at)


def player_id_by_normalized_name(index_data: dict[str, Any]) -> dict[str, str]:
    """Return {normalized_name: player_id} from a fetch_player_index or fetch_projections data dict.

    index_data is looked up for a "players" key first (fetch_player_index's
    shape), then a "projections" key (fetch_projections's shape), so
    either data dict works. Entries whose normalized_name is empty are
    skipped. When two players normalize to the same name, the FIRST one
    encountered (in index_data's own iteration order) wins; the collision
    is simply not overwritten by whichever entry comes second. This is a
    pure function: no network, no disk, and anything unusable (not a
    dict, no usable inner mapping) returns {}.
    """
    if not isinstance(index_data, dict):
        return {}

    entries = index_data.get("players")
    if not isinstance(entries, dict):
        entries = index_data.get("projections")
    if not isinstance(entries, dict):
        return {}

    result: dict[str, str] = {}
    for raw_player_id, entry in entries.items():
        if not isinstance(entry, dict):
            continue
        normalized_name = entry.get("normalized_name")
        if not isinstance(normalized_name, str) or not normalized_name:
            continue
        player_id = entry.get("player_id")
        player_id = str(player_id) if player_id else str(raw_player_id)
        if normalized_name not in result:
            result[normalized_name] = player_id

    return result
