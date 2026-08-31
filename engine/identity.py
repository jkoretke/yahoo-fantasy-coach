"""The player identity join: Yahoo player id to Sleeper player id and ESPN
injury record.

Phase 2 deliberately left one gap open: engine.sources.sleeper's weekly
projections are keyed by Sleeper's own player ids, engine.sources.injuries's
ESPN injury records are keyed by athlete display names, and
engine/scoring.py consumes projections keyed by Yahoo's player_id. Nothing
in the repo maps between the three. This module is that map.

Every match runs through engine.sources.base.normalize_name, the same
deterministic join key engine.sources.sleeper and engine.sources.injuries
already use internally, so all three feeds agree on one key rather than
this module inventing a second, competing normalization scheme. Team
matching (when used to disambiguate two players who share a normalized
name) runs through engine.sources.base.normalize_team_abbreviation, again
the same function the rest of the repo already uses.

This module makes no network call and touches no disk: build_identity_map
is a pure function over three already-fetched, already-parsed data
structures (a list of Yahoo player records, a Sleeper player-index or
projections data dict, and an ESPN injuries data dict), so it is fully
testable offline, with no live Yahoo, Sleeper or ESPN access required.

A Yahoo team-defense entry (for example the "Kansas City" DEF record) will
never match a Sleeper player id, since Sleeper's player index has no
defense entries at all. A non-empty unmatched_sleeper list is therefore
expected and normal, not a bug to chase down; the same is true for a
handful of unmatched_injuries entries for any player ESPN's feed simply
has nothing to report for.

Public names: SOURCE_NAME, IDENTITY_RECORD_KEYS, build_identity_map,
identity_result, sleeper_id_for_yahoo_id, injury_for_yahoo_id.
"""
from __future__ import annotations

from collections import Counter
from typing import Any

from engine.common import timestamp
from engine.sources.base import normalize_name, normalize_team_abbreviation, source_result
from engine.sources.injuries import status_for_player
from engine.sources.sleeper import player_id_by_normalized_name

SOURCE_NAME = "identity"

# The exact key set, in order, every build_identity_map player record
# carries.
IDENTITY_RECORD_KEYS: tuple[str, ...] = (
    "yahoo_player_id",
    "yahoo_player_key",
    "name",
    "normalized_name",
    "nfl_team",
    "positions",
    "sleeper_player_id",
    "injury",
)


def _empty_identity_map() -> dict[str, Any]:
    """Return the documented empty build_identity_map result."""
    return {
        "players": {},
        "count": 0,
        "matched_sleeper": 0,
        "matched_injuries": 0,
        "unmatched_sleeper": [],
        "unmatched_injuries": [],
        "duplicate_normalized_names": [],
    }


def _extract_player_records(yahoo_players: Any) -> list[Any]:
    """Return the raw list of Yahoo player records from any accepted shape.

    Accepts a plain list, a dict with a "players" key holding that list
    (engine.yahoo_client.fetch_player_list's inner data dict shape), or a
    full source_result envelope with a "data" key holding either of those
    (engine.yahoo_client.fetch_player_list's own return shape). Anything
    else, at any stage, returns []. This function does not import
    engine.yahoo_client; it only recognizes the shapes structurally, so it
    has no dependency on that module ever existing or being importable.
    """
    value: Any = yahoo_players
    if isinstance(value, dict):
        if "data" in value:
            value = value["data"]
        if isinstance(value, dict) and "players" in value:
            value = value["players"]
    if isinstance(value, list):
        return value
    return []


def _as_str_or_blank(value: Any) -> str:
    """Return value if it is a string, else "" (never None, never raises)."""
    return value if isinstance(value, str) else ""


def build_identity_map(
    yahoo_players: Any,
    *,
    sleeper_index_data: dict[str, Any] | None = None,
    injuries_data: dict[str, Any] | None = None,
    match_injury_team: bool = True,
) -> dict[str, Any]:
    """Join Yahoo player records to a Sleeper player id and an ESPN injury.

    yahoo_players accepts a plain list of Yahoo player records (the shape
    engine.yahoo_shapes.parse_player_list's "players" list already is), a
    dict with a "players" key holding that list, or a full source_result
    envelope with a "data" key holding either of those. Any other shape
    returns the documented empty result below.

    Only records carrying a non-blank player_id are joined; a record
    missing every other key still joins as best it can, since name and
    normalized_name, nfl_team, positions and player_key all default to a
    blank value rather than failing the whole record.

    For each such record, this builds one output record with exactly
    IDENTITY_RECORD_KEYS:
        yahoo_player_id: str(record["player_id"]).
        yahoo_player_key: record["player_key"] when it is a string, else "".
        name: record["name"] when it is a string, else "".
        normalized_name: record["normalized_name"] when that is already a
            non-blank string, else normalize_name(name) is computed fresh,
            so a hand-built record with no normalized_name field still
            joins correctly.
        nfl_team: normalize_team_abbreviation(record["nfl_team"]).
        positions: record["positions"] when it is a list, else [].
        sleeper_player_id: looked up from
            engine.sources.sleeper.player_id_by_normalized_name(sleeper_index_data),
            called ONCE for the whole batch rather than once per player, by
            normalized_name. None when sleeper_index_data has no such
            entry (including when sleeper_index_data itself is None or
            unusable).
        injury: looked up from
            engine.sources.injuries.status_for_player(injuries_data, name,
            nfl_team), where nfl_team is passed through only when
            match_injury_team is True and this record's own nfl_team is
            non-blank (passing the team is the default, since it is what
            disambiguates two players who share a normalized name). None
            when injuries_data has no such entry (including when
            injuries_data itself is None or unusable).

    Returns:
        {"players": {yahoo_player_id: <record with exactly
             IDENTITY_RECORD_KEYS>, ...},
         "count": int,
         "matched_sleeper": int, "matched_injuries": int,
         "unmatched_sleeper": [yahoo_player_id, ...] sorted,
         "unmatched_injuries": [yahoo_player_id, ...] sorted,
         "duplicate_normalized_names": [normalized_name, ...] sorted}

    matched_sleeper + len(unmatched_sleeper) always equals count, and the
    same identity holds for matched_injuries / unmatched_injuries: every
    processed record lands in exactly one side of each pair. Every record
    still appears in "players" regardless of which lists it lands in,
    keyed by its own distinct yahoo_player_id, so a downstream caller can
    always find a player's record even when nothing joined for it.

    duplicate_normalized_names lists any normalized_name shared by two or
    more of the processed Yahoo records, so a same-name collision is
    visible rather than silently resolved one way or another; both
    colliding records still appear in "players" under their own ids.

    Garbage input (yahoo_players not one of the three accepted shapes, or
    a list none of whose entries carry a usable player_id) returns
    {"players": {}, "count": 0, "matched_sleeper": 0, "matched_injuries": 0,
     "unmatched_sleeper": [], "unmatched_injuries": [],
     "duplicate_normalized_names": []}. This function never raises.
    """
    records = _extract_player_records(yahoo_players)
    if not records:
        return _empty_identity_map()

    sleeper_index = player_id_by_normalized_name(sleeper_index_data)

    players: dict[str, Any] = {}
    count = 0
    matched_sleeper = 0
    matched_injuries = 0
    unmatched_sleeper: list[str] = []
    unmatched_injuries: list[str] = []
    name_counts: "Counter[str]" = Counter()

    for record in records:
        if not isinstance(record, dict):
            continue

        raw_player_id = record.get("player_id")
        if raw_player_id is None:
            continue
        yahoo_player_id = str(raw_player_id).strip()
        if not yahoo_player_id:
            continue

        yahoo_player_key = _as_str_or_blank(record.get("player_key"))
        name = _as_str_or_blank(record.get("name"))

        raw_normalized_name = record.get("normalized_name")
        if isinstance(raw_normalized_name, str) and raw_normalized_name:
            normalized_name = raw_normalized_name
        else:
            normalized_name = normalize_name(name)

        raw_nfl_team = record.get("nfl_team")
        nfl_team = normalize_team_abbreviation(raw_nfl_team if isinstance(raw_nfl_team, str) else None)

        raw_positions = record.get("positions")
        positions = raw_positions if isinstance(raw_positions, list) else []

        sleeper_player_id = sleeper_index.get(normalized_name) if normalized_name else None

        team_for_injury = nfl_team if (match_injury_team and nfl_team) else None
        injury = status_for_player(injuries_data, name, team_for_injury)

        players[yahoo_player_id] = {
            "yahoo_player_id": yahoo_player_id,
            "yahoo_player_key": yahoo_player_key,
            "name": name,
            "normalized_name": normalized_name,
            "nfl_team": nfl_team,
            "positions": positions,
            "sleeper_player_id": sleeper_player_id,
            "injury": injury,
        }

        count += 1
        if sleeper_player_id is not None:
            matched_sleeper += 1
        else:
            unmatched_sleeper.append(yahoo_player_id)

        if injury is not None:
            matched_injuries += 1
        else:
            unmatched_injuries.append(yahoo_player_id)

        if normalized_name:
            name_counts[normalized_name] += 1

    duplicate_normalized_names = sorted(name for name, seen in name_counts.items() if seen > 1)

    return {
        "players": players,
        "count": count,
        "matched_sleeper": matched_sleeper,
        "matched_injuries": matched_injuries,
        "unmatched_sleeper": sorted(unmatched_sleeper),
        "unmatched_injuries": sorted(unmatched_injuries),
        "duplicate_normalized_names": duplicate_normalized_names,
    }


def identity_result(
    yahoo_players: Any,
    *,
    sleeper_index_data: dict[str, Any] | None = None,
    injuries_data: dict[str, Any] | None = None,
    match_injury_team: bool = True,
    fetched_at: str | None = None,
) -> dict[str, Any]:
    """Wrap build_identity_map's result in the repo's common source_result envelope.

    This lets the identity join report through the same
    {"source", "available", "stale", "reason", "fetched_at", "data"} shape
    every Phase 2 source module already returns, even though this module
    is not itself a network source. available is always True and stale is
    always False (source_result's own defaults): build_identity_map
    performs no I/O of any kind, so there is no network or cache state
    that could make this result unavailable or stale the way a real fetch
    can be. fetched_at defaults to engine.common.timestamp() when not
    given.
    """
    data = build_identity_map(
        yahoo_players,
        sleeper_index_data=sleeper_index_data,
        injuries_data=injuries_data,
        match_injury_team=match_injury_team,
    )
    return source_result(SOURCE_NAME, data=data, fetched_at=fetched_at or timestamp())


def sleeper_id_for_yahoo_id(identity_data: dict[str, Any], yahoo_player_id: str) -> str | None:
    """Return the Sleeper player id for one Yahoo player id, or None.

    identity_data is build_identity_map's returned data dict (not the
    identity_result envelope). Returns None on any miss: an unknown
    yahoo_player_id, a Yahoo player with no Sleeper match, or garbage
    identity_data. Never raises.
    """
    if not isinstance(identity_data, dict):
        return None
    players = identity_data.get("players")
    if not isinstance(players, dict):
        return None
    try:
        record = players.get(yahoo_player_id)
    except TypeError:
        return None
    if not isinstance(record, dict):
        return None
    value = record.get("sleeper_player_id")
    return value if isinstance(value, str) else None


def injury_for_yahoo_id(identity_data: dict[str, Any], yahoo_player_id: str) -> dict[str, Any] | None:
    """Return the ESPN injury record for one Yahoo player id, or None.

    identity_data is build_identity_map's returned data dict (not the
    identity_result envelope). Returns None on any miss: an unknown
    yahoo_player_id, a Yahoo player with no ESPN injury match, or garbage
    identity_data. Never raises.
    """
    if not isinstance(identity_data, dict):
        return None
    players = identity_data.get("players")
    if not isinstance(players, dict):
        return None
    try:
        record = players.get(yahoo_player_id)
    except TypeError:
        return None
    if not isinstance(record, dict):
        return None
    value = record.get("injury")
    return value if isinstance(value, dict) else None
