"""ESPN's free, no-auth NFL injuries feed, mapped onto the repo's frozen
player status vocabulary.

ESPN's site.api.espn.com surface is entirely undocumented (there is no
published schema, no versioning promise, and no rate limit contract), so
the response shape below is recorded from a live capture taken during
Phase 2 planning, not from any spec. fixtures/sources/espn_injuries.json
holds a trimmed, faithful sample of that capture for the test suite.

Endpoint: INJURIES_URL, a single GET with no auth and no query parameters.

Response shape (top level object):
    injuries: a list of TEAM GROUPS, one per NFL team (32 in the real
        response). Each group is
            {"id": "22", "displayName": "Arizona Cardinals",
             "injuries": [item, ...]}
        Note the group itself carries no "abbreviation" key; the team's
        abbreviation only appears on each item, at athlete.team.abbreviation.
    season: whatever ESPN attaches this run (observed as an object); this
        module does not interpret it and passes it through unchanged.
    status: a top level string ESPN reports for the request itself.
    timestamp: a minute precision UTC string for when ESPN generated the
        response, in the same "...Z" shape as each item's own date (see
        below).

Each item inside a group's "injuries" list:
    {"id", "longComment", "shortComment", "status", "date", "athlete",
     "details", "source", "type"}
    status: a display string. Values observed in one live off-season
        capture, with counts: "Active" 470, "Questionable" 217,
        "Injured Reserve" 87, "Out" 21, "Suspension" 5. "Doubtful" did not
        appear in that off-season snapshot but is an expected in-season
        value and is mapped below regardless.
    date: "2026-08-30T21:50Z", UTC, minute precision, no seconds, with a
        literal trailing "Z". datetime.fromisoformat cannot parse a bare
        "Z" on every supported Python, so the trailing "Z" is replaced with
        "+00:00" before parsing, and the result is re-emitted through
        engine.common.timestamp() so every date this module produces has
        the same seconds-included "...Z" shape as the rest of the repo
        (for example "2026-08-30T21:50:00Z").
    athlete: {"displayName", "firstName", "lastName", "shortName",
        "status", "headshot", "links", "notes",
        "position": {"id", "name", "displayName", "abbreviation", "leaf"},
        "team": {"id", "uid", "slug", "name", "abbreviation", "displayName",
                 "logos"}}
    details: {"fantasyStatus": {"description", "abbreviation"}, "type",
        "location", "detail", "side", "returnDate"}
    type: {"id", "name", "description", "abbreviation"} -- ESPN's own
        coded injury status, independent of the free text "status" field.
    Every one of athlete, details, type and their sub-objects may be
    missing or explicitly null on a given record; every read below defaults
    rather than assumes the key is present.

Status mapping (status_code, backed by STATUS_CODES): ESPN's free text
status is lowercased and stripped and looked up against the repo's frozen
player status vocabulary ("" active, "Q" questionable, "D" doubtful, "O"
out, "IR" injured reserve, "SUSP" suspended -- see engine/fixtures.py,
which owns and freezes that vocabulary; this module only consumes it).

    ESPN status (lowercased)              -> repo status
    ------------------------------------------------------
    active                                -> ""
    questionable                          -> "Q"
    day-to-day                            -> "Q"
    doubtful                              -> "D"
    out                                   -> "O"
    injured reserve                       -> "IR"
    ir                                    -> "IR"
    physically unable to perform          -> "IR"
    pup                                   -> "IR"
    non football injury                   -> "IR"
    suspension                            -> "SUSP"
    suspended                             -> "SUSP"
    (anything else, with a recognized      -> that type abbreviation
     ESPN type.abbreviation)
    (anything else)                       -> "O" (conservative default;
                                              see status_code)

SCOPE LIMIT: this module matches players by normalized display name only
(engine.sources.base.normalize_name). It does NOT reconcile ESPN's names
with this repo's own player_id values in fixtures/sample_league/players.json,
and it does not write into any lineup or scoring path. That identity join
needs Yahoo's canonical ids and is Phase 3 work; do not attempt it here.

Public names: SOURCE_NAME, INJURIES_URL, INJURIES_MAX_AGE_SECONDS,
STATUS_CODES, STATUS_VOCABULARY, status_code, fetch_injuries,
injuries_by_team, status_for_player.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from engine.common import timestamp
from engine.sources.base import (
    SourceUnavailable,
    fetch_cached_json,
    source_result,
    disabled_result,
    unavailable_result,
    normalize_name,
    normalize_team_abbreviation,
)

SOURCE_NAME = "injuries"

# Undocumented but confirmed live and reachable (site.api.espn.com carries
# no published spec or versioning promise). A future ESPN breakage is a
# one line fix here.
INJURIES_URL = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/injuries"

# Fifteen minutes: a game morning needs an injury report fresher than, say,
# the week's schedule, since a last minute inactive can flip a start/sit
# call right up to kickoff.
INJURIES_MAX_AGE_SECONDS = 900

_CACHE_KEY = "espn-injuries"

# The only six values status_code may ever return, matching the frozen
# vocabulary engine/fixtures.py documents ("" active, "Q", "D", "O", "IR",
# "SUSP"). This module consumes that vocabulary; it does not redefine it.
STATUS_VOCABULARY: tuple[str, ...] = ("", "Q", "D", "O", "IR", "SUSP")

# ESPN's free text injury status, lowercased and stripped, mapped onto
# STATUS_VOCABULARY. See the module docstring's mapping table for the
# rationale of each row.
STATUS_CODES: dict[str, str] = {
    "active": "",
    "questionable": "Q",
    "doubtful": "D",
    "out": "O",
    "injured reserve": "IR",
    "ir": "IR",
    "physically unable to perform": "IR",
    "pup": "IR",
    "non football injury": "IR",
    "suspension": "SUSP",
    "suspended": "SUSP",
    "day-to-day": "Q",
}


def status_code(espn_status: str | None, espn_type_abbreviation: str | None = None) -> str:
    """Map one ESPN injury status string onto the repo's frozen vocabulary.

    Pure function: no network, no disk. espn_status is lowercased and
    stripped and looked up in STATUS_CODES. None or a blank espn_status
    returns "" (active) directly, without ever consulting
    espn_type_abbreviation.

    On a miss in STATUS_CODES, espn_type_abbreviation (ESPN's own coded
    "type.abbreviation" field, independent of the free text status) is
    uppercased and used instead if it already happens to be one of
    STATUS_VOCABULARY's six values. A blank or missing
    espn_type_abbreviation never matches here: the truthiness check runs
    before the STATUS_VOCABULARY membership check specifically so a blank
    type abbreviation cannot accidentally satisfy STATUS_VOCABULARY's own
    "" (active) member.

    Failing both lookups, this returns "O": an injury designation this
    module does not recognize is treated as the conservative case (assume
    the player does not play) rather than silently as active, since being
    wrong in the "sit a player who could have played" direction is a far
    smaller error for a fantasy lineup than the reverse.

    The return value is always one of STATUS_VOCABULARY.
    """
    if not espn_status:
        return ""
    normalized = str(espn_status).strip().lower()
    if not normalized:
        return ""

    mapped = STATUS_CODES.get(normalized)
    if mapped is not None:
        return mapped

    if espn_type_abbreviation:
        candidate = str(espn_type_abbreviation).strip().upper()
        if candidate in STATUS_VOCABULARY:
            return candidate

    return "O"


def _as_dict(value: Any) -> dict[str, Any]:
    """Return value if it is a dict, else {}.

    Used everywhere a sub-object (athlete, team, position, details, type,
    fantasyStatus) may be missing or explicitly null in a real ESPN record,
    so every read below can chain .get() calls without a None or wrong-type
    crash.
    """
    return value if isinstance(value, dict) else {}


def _parse_espn_timestamp(value: Any) -> str:
    """Return the normalized UTC timestamp for an ESPN date string.

    ESPN's date and top level timestamp strings are minute precision UTC
    with a literal trailing "Z" ("2026-08-30T21:50Z"), a shape
    datetime.fromisoformat cannot parse directly on every supported
    Python; the trailing "Z" is replaced with "+00:00" first. Anything
    that is not a non-blank string, or that still fails to parse, returns
    "" rather than raising, so one malformed date degrades that one field
    instead of failing the whole fetch. A successful parse is re-emitted
    through engine.common.timestamp(), so "2026-08-30T21:50Z" becomes
    "2026-08-30T21:50:00Z".
    """
    if not isinstance(value, str) or not value.strip():
        return ""
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return ""
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return timestamp(parsed)


def _extract_player(item: dict[str, Any]) -> dict[str, Any] | None:
    """Return the documented player dict for one ESPN injuries item, or None.

    Returns None when the item carries no usable player name (neither a
    non-blank athlete.displayName nor a non-blank "firstName lastName"
    join), so the caller can skip that one record instead of failing the
    whole fetch.
    """
    athlete = _as_dict(item.get("athlete"))

    name = athlete.get("displayName")
    if not isinstance(name, str) or not name.strip():
        first = athlete.get("firstName") or ""
        last = athlete.get("lastName") or ""
        joined = f"{first} {last}".strip()
        name = joined or None
    if not name:
        return None
    name = name.strip()

    team = _as_dict(athlete.get("team"))
    position = _as_dict(athlete.get("position"))
    details = _as_dict(item.get("details"))
    item_type = _as_dict(item.get("type"))
    fantasy_status = _as_dict(details.get("fantasyStatus"))

    status_raw = item.get("status")
    status_raw = status_raw if isinstance(status_raw, str) else ""

    short_comment = item.get("shortComment")
    long_comment = item.get("longComment")
    comment = short_comment if isinstance(short_comment, str) and short_comment.strip() else None
    if comment is None:
        comment = long_comment if isinstance(long_comment, str) and long_comment.strip() else ""

    return {
        "name": name,
        "normalized_name": normalize_name(name),
        "nfl_team": normalize_team_abbreviation(team.get("abbreviation")),
        "position": position.get("abbreviation") or "",
        "status": status_code(status_raw or None, item_type.get("abbreviation")),
        "status_raw": status_raw,
        "injury_type": details.get("type") or "",
        "return_date": details.get("returnDate") or "",
        "fantasy_status": fantasy_status.get("abbreviation") or "",
        "comment": comment,
        "updated": _parse_espn_timestamp(item.get("date")),
    }


def fetch_injuries(
    *,
    enabled: bool = True,
    cache_root: Path | None = None,
    max_age_seconds: int = INJURIES_MAX_AGE_SECONDS,
    force_refresh: bool = False,
) -> dict[str, Any]:
    """Fetch and flatten ESPN's NFL injuries report into one player list.

    enabled=False returns disabled_result(SOURCE_NAME) immediately, with
    zero network and zero disk access; Phase 3 will wire this parameter up
    to config/league.yaml, which does not exist yet.

    On success, the envelope's "data" is:
        {"source_url": INJURIES_URL, "season": <payload["season"] as is,
         or None>, "reported_at": <normalized payload "timestamp", or None
         if missing or unparseable>,
         "players": [ {"name", "normalized_name", "nfl_team", "position",
                        "status", "status_raw", "injury_type",
                        "return_date", "fantasy_status", "comment",
                        "updated"}, ... ],
         "count": len(players)}
    Every team group in the response is flattened into that one players
    list, sorted by (nfl_team, normalized_name) so the output is
    deterministic run to run. A group that is not a dict, or whose
    "injuries" key is not a list, is skipped; an item that is not a dict is
    skipped; an item with no usable player name is skipped (see
    _extract_player). None of those are treated as a fetch failure: a few
    unparseable records degrade to a shorter list, not to unavailable.

    A dead endpoint, a cache miss on a failed fetch, a non-JSON body, or a
    well formed JSON payload of the wrong shape (top level not a dict, or
    no "injuries" list) all return unavailable_result(SOURCE_NAME, reason)
    instead of raising, so one bad response degrades this source without
    ending the run. The only exception this function may raise is
    EngineError, and only for a programmer error (for example an invalid
    cache_root value bubbling out of fetch_cached_json), never for a
    network or data problem.
    """
    if not enabled:
        return disabled_result(SOURCE_NAME)

    try:
        payload, fetched_at, stale = fetch_cached_json(
            INJURIES_URL,
            _CACHE_KEY,
            max_age_seconds=max_age_seconds,
            cache_root=cache_root,
            service=SOURCE_NAME,
            force_refresh=force_refresh,
        )
    except SourceUnavailable as exc:
        return unavailable_result(SOURCE_NAME, str(exc))

    if not isinstance(payload, dict):
        return unavailable_result(SOURCE_NAME, "espn injuries payload was not a JSON object")

    groups = payload.get("injuries")
    if not isinstance(groups, list):
        return unavailable_result(SOURCE_NAME, "espn injuries payload had no 'injuries' list")

    players: list[dict[str, Any]] = []
    for group in groups:
        if not isinstance(group, dict):
            continue
        items = group.get("injuries")
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            player = _extract_player(item)
            if player is not None:
                players.append(player)

    players.sort(key=lambda player: (player["nfl_team"], player["normalized_name"]))

    data = {
        "source_url": INJURIES_URL,
        "season": payload.get("season"),
        "reported_at": _parse_espn_timestamp(payload.get("timestamp")) or None,
        "players": players,
        "count": len(players),
    }
    return source_result(SOURCE_NAME, data=data, stale=stale, fetched_at=fetched_at)


def injuries_by_team(injuries_data: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Group fetch_injuries's "data" dict's players by nfl_team.

    Pure function: no network, no disk. injuries_data is the "data" dict
    from inside fetch_injuries's envelope (not the whole envelope).
    Within each team's list, players keep the order fetch_injuries already
    sorted them into. Anything unusable (not a dict, no "players" list,
    a non-dict player) is skipped; garbage input returns {}.
    """
    if not isinstance(injuries_data, dict):
        return {}
    players = injuries_data.get("players")
    if not isinstance(players, list):
        return {}

    grouped: dict[str, list[dict[str, Any]]] = {}
    for player in players:
        if not isinstance(player, dict):
            continue
        team = player.get("nfl_team")
        if not isinstance(team, str):
            continue
        grouped.setdefault(team, []).append(player)
    return grouped


def status_for_player(
    injuries_data: dict[str, Any], name: str, nfl_team: str | None = None
) -> dict[str, Any] | None:
    """Look up one player's injury record by normalized display name.

    Pure function: no network, no disk. injuries_data is the "data" dict
    from inside fetch_injuries's envelope. name is matched via
    normalize_name(name) against each player's "normalized_name". A blank
    or unmatchable name returns None.

    nfl_team, when given as a non-blank value, is normalized with
    normalize_team_abbreviation and also required to match; a blank
    nfl_team (None or "") is treated as "not given" and is not used to
    filter, so a caller need not already know a player's team. A
    non-blank nfl_team that does not match any same-named player returns
    None even if a same-named player exists on a different team.

    fetch_injuries's players list is already sorted by (nfl_team,
    normalized_name), and this function returns the first match it finds
    in that order, so a genuine name collision (two players sharing a
    normalized name) deterministically returns the one on the
    alphabetically first team.

    Garbage input (injuries_data not a dict, or with no "players" list)
    returns None rather than raising.
    """
    if not isinstance(injuries_data, dict):
        return None
    players = injuries_data.get("players")
    if not isinstance(players, list):
        return None

    target_name = normalize_name(name)
    if not target_name:
        return None
    target_team = normalize_team_abbreviation(nfl_team) if nfl_team else None

    for player in players:
        if not isinstance(player, dict):
            continue
        if player.get("normalized_name") != target_name:
            continue
        if target_team and player.get("nfl_team") != target_team:
            continue
        return player
    return None
