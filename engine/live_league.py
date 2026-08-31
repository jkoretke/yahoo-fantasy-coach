"""Assemble a real-data league dict from Yahoo, Sleeper and ESPN.

engine.fixtures.load_fixture_league builds the eleven-key league dict every
Phase 1 module (engine.scoring, engine.lineup, engine.matchup,
engine.waivers, engine.brief) consumes, but only from the frozen sample
fixture. Nothing before this module built the real-data equivalent: Phase
3's engine.yahoo_client fetches Yahoo's settings, metadata, rosters,
matchups, free agents and player list; Phase 2's engine.sources.sleeper
fetches weekly projections keyed by Sleeper's own player id; engine.identity
joins a Yahoo player id to a Sleeper player id and an ESPN injury record.
This module is the seam that merges all of that into one league dict
matching load_fixture_league's exact shape.

THE STAT-KEY RE-KEY. Sleeper's projected stat lines use Sleeper's own stat
names (pass_yd, rush_td, rec, and so on); engine.scoring multiplies
stats[k] by settings.scoring.stats[k] using this repo's canonical names
(passing_yards, rushing_touchdowns, receptions, and so on) and silently
ignores any key it does not recognize. An untranslated Sleeper stat line
therefore scores every player 0.0 with no error anywhere. sleeper_
projections_for_league is what performs that translation, on top of the
player-id re-key from Sleeper's id space onto Yahoo's, before a projection
ever reaches engine.scoring. It deliberately never falls back to Sleeper's
own precomputed pts_ppr / projected_points: doing so would bypass this
league's own scoring rules and break engine.brief's one shared scoring
pass contract (every number in a brief coming from a single scoring pass
over the same stat lines).

Yahoo does not carry team names or managers anywhere this phase reads
(engine.yahoo_shapes.parse_matchups keeps only team_id and team_key per
team; engine.yahoo_shapes.parse_roster carries no team name at all), so
every team's name and manager are synthesized placeholders here, and
assemble_live_league always says so in its returned warnings.

Every external read degrades rather than raises: an unavailable Yahoo,
Sleeper or ESPN source appends a warning and the assembly continues with
that source's honest empty shape. The only exception this module may
raise is engine.common.EngineError, and this module never raises one
itself; whether one propagates out of it depends only on whether the
mocked or real fetch functions it calls raise one (a genuine
configuration problem, per each of those modules' own documented
contract).

This module makes no assumption about how its Yahoo, Sleeper, ESPN or
identity calls are wired up in a given run: every fetch happens through
the engine.yahoo_client, engine.sources.sleeper, engine.sources.injuries
and engine.identity modules, imported here as whole modules (not
individual names) specifically so a caller (a test, or a later run
wrapper) can monkeypatch engine.yahoo_client.fetch_league_metadata (and
so on) in place and have this module's own calls pick up the patched
version, the same seam Phase 3's own tests already rely on.

Public names: SLEEPER_STAT_KEY_MAP, SLEEPER_IGNORED_STAT_KEYS,
sleeper_projections_for_league, assemble_live_league, build_live_league.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from engine import identity as identity_module
from engine import yahoo_client
from engine import yahoo_shapes
from engine.sources import injuries as injuries_source
from engine.sources import sleeper as sleeper_source

# Sleeper's own stat key, mapped to the repo's canonical scoring key
# (engine/scoring.py reads league["settings"]["scoring"]["stats"] under
# these names, and yahoo_shapes._VALID_SCORING_KEYS lists the fifteen
# per-unit keys plus defense_points_allowed as the one bracket-scored
# key). pts_allow maps to defense_points_allowed, which is never one of
# the fifteen per-unit stats keys; it only scores when a caller supplies
# a settings.scoring.brackets.defense_points_allowed table (assemble_
# live_league's scoring_brackets keyword exists for exactly that), and
# contributes nothing otherwise, the same silent-ignore behavior
# engine.scoring.score_stat_line already documents for any stat key
# absent from both the stats map and the brackets map.
SLEEPER_STAT_KEY_MAP: dict[str, str] = {
    "pass_yd": "passing_yards",
    "pass_td": "passing_touchdowns",
    "pass_int": "interceptions",
    "rush_yd": "rushing_yards",
    "rush_td": "rushing_touchdowns",
    "rec": "receptions",
    "rec_yd": "receiving_yards",
    "rec_td": "receiving_touchdowns",
    "fum_lost": "fumbles_lost",
    "fgm": "field_goals_made",
    "xpm": "extra_points_made",
    "sack": "defense_sacks",
    "int": "defense_interceptions",
    "fr": "defense_fumble_recoveries",
    "def_td": "defense_touchdowns",
    "pts_allow": "defense_points_allowed",
}

# Sleeper stat keys that are real, observed in fixtures/sources/
# sleeper_projections.json, and deliberately not scored (efficiency and
# volume stats no league scoring rule references, and Sleeper's own
# precomputed point totals, which this module never uses; see the module
# docstring). Listed here, rather than left to fall through as
# "unmapped", so a real Sleeper projection's own extra columns never
# pollute sleeper_projections_for_league's unmapped_stat_keys report.
SLEEPER_IGNORED_STAT_KEYS: frozenset[str] = frozenset(
    {
        "adp_dd_ppr",
        "cmp_pct",
        "fum",
        "gp",
        "pass_2pt",
        "pass_att",
        "pass_cmp",
        "pass_fd",
        "pass_inc",
        "pass_sack",
        "pts_half_ppr",
        "pts_ppr",
        "pts_std",
        "rec_fd",
        "rush_att",
        "rush_fd",
    }
)


def _empty_sleeper_projections_result() -> dict[str, Any]:
    return {
        "projections": [],
        "matched": 0,
        "unmatched_yahoo_ids": [],
        "unmapped_stat_keys": [],
    }


def sleeper_projections_for_league(
    sleeper_projections_data: Any, identity_data: Any, *, week: int
) -> dict[str, Any]:
    """Re-key one week of Sleeper projections onto Yahoo player ids.

    sleeper_projections_data is the "data" dict from INSIDE engine.
    sources.sleeper.fetch_projections's envelope: {"season", "week",
    "source_url", "projections": {<sleeper_player_id>: {..., "stats"}},
    "count"}. "projections" is a DICT keyed by Sleeper's own player id,
    not a list. identity_data is the "data" dict from inside engine.
    identity.identity_result's envelope: {"players": {<yahoo_player_id>:
    {..., "sleeper_player_id", "injury"}, ...}, ...}.

    A reverse index sleeper_player_id -> yahoo_player_id is built once
    from identity_data["players"] (the first Yahoo player encountered, in
    that dict's own iteration order, wins any collision where two Yahoo
    players somehow share a sleeper_player_id). Every Yahoo player that
    reverse index names is then looked up in sleeper_projections_data's
    own "projections" dict: a hit becomes one output projection row and
    counts toward "matched"; a miss (the identity join found a Sleeper
    id for this Yahoo player, but Sleeper published no projection entry
    for it this week) adds that yahoo_player_id to "unmatched_yahoo_ids".
    A Sleeper projections entry whose id maps to no Yahoo player at all
    (nothing in this league's roster or player pool matched it) is simply
    not part of this league's projections and is skipped without being
    counted either way.

    Each matched player's raw stats dict is translated key by key: a key
    in SLEEPER_STAT_KEY_MAP becomes its canonical key; a key in
    SLEEPER_IGNORED_STAT_KEYS is dropped on purpose; any other key is
    recorded, deduped and sorted, into "unmapped_stat_keys", mirroring
    the unmapped_stat_categories pattern engine.yahoo_shapes.
    parse_scoring_settings already uses, rather than silently dropping a
    stat key this module has never seen before. A stat value that will
    not parse as a float is dropped from that one player's stats rather
    than failing the whole row.

    Returns {"projections": [{"week": int(week), "player_id":
    <yahoo_player_id>, "stats": {<canonical key>: float, ...}}, ...],
    "matched": int, "unmatched_yahoo_ids": [str, ...] sorted,
    "unmapped_stat_keys": [str, ...] sorted}. Never raises: garbage input
    at any level (not a dict, no usable "projections"/"players" mapping)
    returns {"projections": [], "matched": 0, "unmatched_yahoo_ids": [],
    "unmapped_stat_keys": []}.
    """
    if not isinstance(sleeper_projections_data, dict) or not isinstance(identity_data, dict):
        return _empty_sleeper_projections_result()

    raw_projections = sleeper_projections_data.get("projections")
    if not isinstance(raw_projections, dict):
        return _empty_sleeper_projections_result()

    identity_players = identity_data.get("players")
    if not isinstance(identity_players, dict):
        return _empty_sleeper_projections_result()

    reverse_index: dict[str, str] = {}
    for yahoo_player_id, record in identity_players.items():
        if not isinstance(record, dict):
            continue
        sleeper_player_id = record.get("sleeper_player_id")
        if not isinstance(sleeper_player_id, str) or not sleeper_player_id:
            continue
        if sleeper_player_id not in reverse_index:
            reverse_index[sleeper_player_id] = str(yahoo_player_id)

    week_int = int(week)
    projections: list[dict[str, Any]] = []
    matched_yahoo_ids: set[str] = set()
    unmapped_stat_keys: set[str] = set()

    for sleeper_player_id, entry in raw_projections.items():
        if not isinstance(entry, dict):
            continue
        yahoo_player_id = reverse_index.get(str(sleeper_player_id))
        if yahoo_player_id is None:
            continue

        raw_stats = entry.get("stats")
        stats: dict[str, float] = {}
        if isinstance(raw_stats, dict):
            for sleeper_key, raw_value in raw_stats.items():
                if sleeper_key in SLEEPER_STAT_KEY_MAP:
                    try:
                        stats[SLEEPER_STAT_KEY_MAP[sleeper_key]] = float(raw_value)
                    except (TypeError, ValueError):
                        continue
                elif sleeper_key in SLEEPER_IGNORED_STAT_KEYS:
                    continue
                else:
                    unmapped_stat_keys.add(sleeper_key)

        projections.append(
            {"week": week_int, "player_id": yahoo_player_id, "stats": stats}
        )
        matched_yahoo_ids.add(yahoo_player_id)

    unmatched_yahoo_ids = sorted(set(reverse_index.values()) - matched_yahoo_ids)

    return {
        "projections": projections,
        "matched": len(matched_yahoo_ids),
        "unmatched_yahoo_ids": unmatched_yahoo_ids,
        "unmapped_stat_keys": sorted(unmapped_stat_keys),
    }


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Return a new dict: override merged over base, recursing into shared dict keys.

    A key whose value is a dict in both base and override is merged
    recursively; any other key (including a list, which is never merged
    element-wise) is simply replaced by override's value. base is not
    mutated.
    """
    result = dict(base)
    for key, value in override.items():
        base_value = result.get(key)
        if isinstance(value, dict) and isinstance(base_value, dict):
            result[key] = _deep_merge(base_value, value)
        else:
            result[key] = value
    return result


def _dedup_player_records(*groups: Any) -> list[dict[str, Any]]:
    """Merge several Yahoo player-record lists into one, deduped by player_id.

    The first record seen for a given player_id wins; later duplicates
    (for example the same player appearing in both the general player
    list and a roster, or both a roster and the free agent list) are
    dropped rather than overwriting the first. Order across groups is the
    order groups are given in, and within a group the order the records
    already have.
    """
    combined: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for group in groups:
        if not isinstance(group, list):
            continue
        for record in group:
            if not isinstance(record, dict):
                continue
            raw_id = record.get("player_id")
            if not raw_id:
                continue
            player_id = str(raw_id)
            if player_id in seen_ids:
                continue
            seen_ids.add(player_id)
            combined.append(record)
    return combined


def _coerce_bye_week(value: Any) -> int:
    """Coerce a parsed player's bye_week to a plain int, 0 when unknown."""
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    return 0


def _build_players(
    combined_records: list[dict[str, Any]], identity_data: dict[str, Any]
) -> list[dict[str, Any]]:
    """Turn deduped Yahoo player records into the fixture's six-key player shape.

    Each record's "status" is replaced by engine.identity.
    injury_for_yahoo_id's own status code field when an ESPN injury
    record was joined for that player (including when that status code
    is "", meaning ESPN reports the player active: that is still more
    current than a possibly stale Yahoo status and must not be treated as
    falsy-and-skip). A player with no injury match keeps whatever status
    string Yahoo itself reported, defaulting to "" rather than ever being
    left absent.
    """
    players: list[dict[str, Any]] = []
    for record in combined_records:
        player_id = str(record.get("player_id", ""))
        name = record.get("name") if isinstance(record.get("name"), str) else ""
        positions = record.get("positions") if isinstance(record.get("positions"), list) else []
        nfl_team = record.get("nfl_team") if isinstance(record.get("nfl_team"), str) else ""

        yahoo_status = record.get("status") if isinstance(record.get("status"), str) else ""
        injury = identity_module.injury_for_yahoo_id(identity_data, player_id)
        if injury is not None:
            injury_status = injury.get("status")
            status = injury_status if isinstance(injury_status, str) else ""
        else:
            status = yahoo_status

        players.append(
            {
                "player_id": player_id,
                "name": name,
                "positions": list(positions),
                "nfl_team": nfl_team,
                "status": status,
                "bye_week": _coerce_bye_week(record.get("bye_week")),
            }
        )
    return players


def _matchup_team_ids(matchups_data: dict[str, Any]) -> list[str]:
    """Return every team_id appearing in matchups_data's matchups, deduped in order."""
    ordered: list[str] = []
    seen: set[str] = set()
    for matchup in matchups_data.get("matchups", []):
        if not isinstance(matchup, dict):
            continue
        for team_id in matchup.get("team_ids", []):
            if not team_id or team_id in seen:
                continue
            seen.add(team_id)
            ordered.append(team_id)
    return ordered


def _build_teams(
    rosters: list[dict[str, Any]], owner_team_id: str, warnings: list[str]
) -> list[dict[str, Any]]:
    """Turn fetch_rosters's per-team roster payloads into the fixture's team shape.

    Yahoo does not hand this phase a team name or manager anywhere it
    reads from (see the module docstring), so both are synthesized
    placeholders and warnings records that once, only when there is at
    least one team to build.
    """
    if rosters:
        warnings.append(
            "Yahoo roster and matchup data carry no team name or manager for "
            "this phase to read; every team's name and manager below are "
            "synthesized placeholders (\"Team <team_id>\" and \"\")."
        )

    teams: list[dict[str, Any]] = []
    for roster in rosters:
        if not isinstance(roster, dict):
            continue
        team_id = str(roster.get("team_id", ""))
        roster_entries = roster.get("roster", [])
        team_roster = [
            {
                "player_id": str(entry.get("player_id", "")),
                "selected_slot": entry.get("selected_slot", "")
                if isinstance(entry.get("selected_slot"), str)
                else "",
            }
            for entry in roster_entries
            if isinstance(entry, dict)
        ]
        teams.append(
            {
                "team_id": team_id,
                "name": f"Team {team_id}",
                "manager": "",
                "is_owner_team": bool(owner_team_id) and team_id == owner_team_id,
                "roster": team_roster,
            }
        )
    return teams


def assemble_live_league(
    *,
    league_id: str,
    season: int,
    week: int,
    game_id: int | None = None,
    team_ids: list[str] | None = None,
    sources_enabled: dict[str, bool] | None = None,
    waiver_settings: dict[str, Any] | None = None,
    scoring_brackets: dict[str, Any] | None = None,
    player_count_limit: int = 1000,
    free_agent_limit: int = 50,
    query: Any | None = None,
    secrets_path: Path | None = None,
    token_dir: Path | None = None,
    browser_callback: bool = True,
    cache_root: Path | None = None,
) -> dict[str, Any]:
    """Assemble one real-data league dict, the non-fixture equivalent of
    engine.fixtures.load_fixture_league.

    Reads, in order: Yahoo league metadata and settings, this week's
    Yahoo matchups, this week's Yahoo rosters (for team_ids, or the union
    of every team_id this week's matchups name when team_ids is not
    given), Yahoo free agents and Yahoo's player pool; then Sleeper's
    player index and this week's Sleeper projections; then ESPN's
    injuries; then engine.identity.identity_result joining the Yahoo
    player pool to both; then sleeper_projections_for_league re-keying
    Sleeper's projections onto Yahoo player ids.

    sources_enabled, when given, maps a Phase 2 source's name ("sleeper",
    "injuries") to a bool passed through as that source's own fetch
    function's enabled keyword; a name it does not mention defaults to
    enabled. It does not gate any Yahoo fetch: a live league cannot be
    assembled at all without Yahoo. cache_root is passed to the Phase 2
    fetches only; engine.yahoo_client's fetch functions take no
    cache_root of their own. query, secrets_path, token_dir and
    browser_callback are passed through to every engine.yahoo_client
    fetch call unchanged, so a caller can reuse one already-authenticated
    query object across the whole assembly instead of repeating Yahoo's
    OAuth2 handshake per call.

    waiver_settings, when given, is deep merged over the parsed
    settings["waiver"] mapping (see _deep_merge): a "priority_order" list
    or a "faab_remaining" dict entry supplied here is what makes
    engine.waivers.rank_waiver_targets usable against this league, since
    Yahoo's own league settings response carries neither (see
    engine.yahoo_shapes.parse_waiver_settings's own docstring). scoring_
    brackets, when given, is shallow-merged into settings["scoring"]
    ["brackets"] (engine.yahoo_shapes.parse_league_settings always
    returns an empty brackets mapping, since Yahoo expresses defense
    points-allowed scoring differently from this repo's [low, high,
    points] triples).

    Every one of the eleven Yahoo/Sleeper/ESPN/identity reads this
    function makes degrades independently: an unavailable or disabled
    source appends one line to the returned "warnings" list and this
    function keeps going with that source's honest empty shape, via the
    same engine.yahoo_shapes.parse_*(None) / empty-envelope defaults each
    source's own parser already documents for garbage input. Team names
    and managers are always synthesized placeholders (Yahoo never
    supplies them to this phase; see the module docstring), and that,
    too, is always recorded as a warning. A blank owner_team_id from
    Yahoo's matchup data (no team flagged as the current login's own)
    leaves every team's is_owner_team False and is also recorded as a
    warning, rather than raising: engine.fixtures.owner_team_id would
    raise on that, but deciding whether to call it is a caller's concern,
    not this function's.

    Returns {"league": <dict with exactly the eleven keys engine.
    fixtures.load_fixture_league documents, in that order>, "sources":
    {<name>: <source_result envelope>, ...}, "warnings": [str, ...]}.
    This function itself never raises anything but engine.common.
    EngineError, and only when a fetch it calls raises one (a
    configuration problem such as a missing credential, per engine.
    yahoo_client's own documented contract: that is never turned into a
    degraded warning, since it means this run cannot proceed at all, not
    that one source is missing).
    """
    warnings: list[str] = []
    sources: dict[str, Any] = {}

    yahoo_kwargs: dict[str, Any] = {
        "query": query,
        "league_id": league_id,
        "season": season,
        "game_id": game_id,
        "secrets_path": secrets_path,
        "token_dir": token_dir,
        "browser_callback": browser_callback,
    }

    metadata_env = yahoo_client.fetch_league_metadata(**yahoo_kwargs)
    sources["league_metadata"] = metadata_env
    if metadata_env["available"]:
        metadata = metadata_env["data"]
    else:
        warnings.append(f"Yahoo league metadata unavailable: {metadata_env['reason']}")
        metadata = yahoo_shapes.parse_league_metadata(None)

    settings_env = yahoo_client.fetch_league_settings(**yahoo_kwargs)
    sources["league_settings"] = settings_env
    if settings_env["available"]:
        settings_data = settings_env["data"]
    else:
        warnings.append(f"Yahoo league settings unavailable: {settings_env['reason']}")
        settings_data = yahoo_shapes.parse_league_settings(None)

    matchups_env = yahoo_client.fetch_matchups(week=week, **yahoo_kwargs)
    sources["matchups"] = matchups_env
    if matchups_env["available"]:
        matchups_data = matchups_env["data"]
    else:
        warnings.append(f"Yahoo matchups unavailable: {matchups_env['reason']}")
        matchups_data = yahoo_shapes.parse_matchups(None, week=week)

    resolved_team_ids = team_ids if team_ids is not None else _matchup_team_ids(matchups_data)

    rosters_env = yahoo_client.fetch_rosters(
        week=week, team_ids=resolved_team_ids, **yahoo_kwargs
    )
    sources["rosters"] = rosters_env
    if rosters_env["available"]:
        rosters_list = rosters_env["data"].get("rosters", [])
        failed_team_ids = rosters_env["data"].get("failed_team_ids", [])
        if failed_team_ids:
            warnings.append(
                f"Yahoo roster fetch failed for team_ids: {failed_team_ids}"
            )
    else:
        warnings.append(f"Yahoo rosters unavailable: {rosters_env['reason']}")
        rosters_list = []

    free_agents_env = yahoo_client.fetch_free_agents(limit=free_agent_limit, **yahoo_kwargs)
    sources["free_agents"] = free_agents_env
    if free_agents_env["available"]:
        free_agents_data = free_agents_env["data"]
    else:
        warnings.append(f"Yahoo free agents unavailable: {free_agents_env['reason']}")
        free_agents_data = yahoo_shapes.parse_free_agents(None)

    player_list_env = yahoo_client.fetch_player_list(
        player_count_limit=player_count_limit, **yahoo_kwargs
    )
    sources["player_list"] = player_list_env
    if player_list_env["available"]:
        player_list_data = player_list_env["data"]
    else:
        warnings.append(f"Yahoo player list unavailable: {player_list_env['reason']}")
        player_list_data = yahoo_shapes.parse_player_list(None)

    sources_enabled = sources_enabled or {}
    sleeper_enabled = sources_enabled.get("sleeper", True)
    injuries_enabled = sources_enabled.get("injuries", True)

    sleeper_index_env = sleeper_source.fetch_player_index(
        enabled=sleeper_enabled, cache_root=cache_root
    )
    sources["sleeper_player_index"] = sleeper_index_env
    if sleeper_index_env["available"]:
        sleeper_index_data = sleeper_index_env["data"]
    else:
        warnings.append(f"Sleeper player index unavailable: {sleeper_index_env['reason']}")
        sleeper_index_data = {"players": {}, "count": 0}

    sleeper_projections_env = sleeper_source.fetch_projections(
        season, week, enabled=sleeper_enabled, cache_root=cache_root
    )
    sources["sleeper_projections"] = sleeper_projections_env
    if sleeper_projections_env["available"]:
        sleeper_projections_data = sleeper_projections_env["data"]
    else:
        warnings.append(
            f"Sleeper projections unavailable: {sleeper_projections_env['reason']}"
        )
        sleeper_projections_data = {
            "season": season,
            "week": week,
            "source_url": "",
            "projections": {},
            "count": 0,
        }

    injuries_env = injuries_source.fetch_injuries(enabled=injuries_enabled, cache_root=cache_root)
    sources["injuries"] = injuries_env
    if injuries_env["available"]:
        injuries_data = injuries_env["data"]
    else:
        warnings.append(f"ESPN injuries unavailable: {injuries_env['reason']}")
        injuries_data = {
            "source_url": "",
            "season": None,
            "reported_at": None,
            "players": [],
            "count": 0,
        }

    combined_records = _dedup_player_records(
        player_list_data.get("players", []),
        *[roster.get("players", []) for roster in rosters_list if isinstance(roster, dict)],
        free_agents_data.get("players", []),
    )

    identity_env = identity_module.identity_result(
        combined_records,
        sleeper_index_data=sleeper_index_data,
        injuries_data=injuries_data,
    )
    sources["identity"] = identity_env
    identity_data = identity_env["data"]

    players = _build_players(combined_records, identity_data)

    owner_team_id = matchups_data.get("owner_team_id") or ""
    if not owner_team_id:
        warnings.append(
            "Yahoo matchup data had no owner_team_id (nothing was flagged as "
            "the current login's own team); no team below has is_owner_team true."
        )
    teams = _build_teams(rosters_list, owner_team_id, warnings)

    projections_result = sleeper_projections_for_league(
        sleeper_projections_data, identity_data, week=week
    )
    if projections_result["unmapped_stat_keys"]:
        warnings.append(
            "Sleeper projections carried stat keys with no scoring mapping: "
            f"{projections_result['unmapped_stat_keys']}"
        )

    scoring_stats = dict(settings_data.get("scoring", {}).get("stats", {}))
    scoring_brackets_out = dict(settings_data.get("scoring", {}).get("brackets", {}))
    if scoring_brackets:
        scoring_brackets_out.update(scoring_brackets)

    waiver = dict(settings_data.get("waiver", {}))
    if waiver_settings:
        waiver = _deep_merge(waiver, waiver_settings)

    settings = {
        "scoring": {"stats": scoring_stats, "brackets": scoring_brackets_out},
        "roster_slots": list(settings_data.get("roster_slots", [])),
        "waiver": waiver,
        "unmapped_stat_categories": list(settings_data.get("unmapped_stat_categories", [])),
    }

    out_league_id = metadata.get("league_id") or str(league_id)
    out_season = metadata.get("season") if metadata.get("season") is not None else season
    out_current_week = (
        metadata.get("current_week") if metadata.get("current_week") is not None else week
    )

    league: dict[str, Any] = {
        "league_id": out_league_id,
        "name": metadata.get("name", ""),
        "season": out_season,
        "current_week": out_current_week,
        "num_teams": metadata.get("num_teams"),
        "settings": settings,
        "players": players,
        "teams": teams,
        "matchups": matchups_data.get("matchups", []),
        "projections": projections_result["projections"],
        "free_agents": free_agents_data.get("free_agents", []),
    }

    return {"league": league, "sources": sources, "warnings": warnings}


def build_live_league(**kwargs: Any) -> dict[str, Any]:
    """Return assemble_live_league(**kwargs)["league"] only.

    This is the direct, real-data equivalent of engine.fixtures.
    load_fixture_league: its return is exactly the eleven keys that
    function documents, in that order, ready to pass straight into
    engine.brief.build_brief. See assemble_live_league for every keyword
    argument this accepts and what each one does.
    """
    return assemble_live_league(**kwargs)["league"]
