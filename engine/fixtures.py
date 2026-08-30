"""Loader for the frozen sample league fixture used by every Phase 1 module.

This module owns the fixture schema. Five later modules (scoring, lineup,
matchup, waivers, brief) read exactly the files and keys documented here and
must never invent their own sample data. The schema is frozen: a key is never
renamed, no top level file is added, and no nesting level changes.

Fixture directory layout, under fixtures/sample_league/ (six JSON files, each
a single top level JSON object since engine.common.load_json requires that):

league.json
    A single object with the league's scalar fields and its settings:
        league_id, name, season, current_week, num_teams (scalars)
        settings.scoring.stats: {stat_name: points_per_unit, ...}
            Multiplied against a player's raw projected stat line to get
            fantasy points for every stat except defense_points_allowed.
        settings.scoring.brackets.defense_points_allowed: a list of
            [low, high, points] triples. Both bounds are INCLUSIVE. A points
            allowed value that matches no bracket is a scoring error
            (engine.common.EngineError), not a silent zero. The fixture's
            last bracket high is 999 so every realistic points-allowed value
            matches something.
        settings.roster_slots: a list of
            {slot, count, eligible_positions, starting} objects describing
            the league's roster. "starting": true slots are the ones a
            lineup is built from; "starting": false slots (bench, IR) are
            not. See starting_slot_units() below.
        settings.waiver: {type, faab_budget, faab_remaining, priority_order}
            type is "faab" or "priority". A real league has exactly one
            waiver type; load_fixture_league's waiver_type override exists
            only so tests and the demo can exercise both branches against
            this one fixture, not because a league actually has both.

players.json
    {"players": [player, ...]}. Every player, rostered, free agent, or
    otherwise, appears exactly once here. A player object has:
        player_id, name, positions (always a list, even for one position),
        nfl_team, status, bye_week.
    status is one of "" (active), "Q", "D", "O", "IR", "SUSP". Whether a
    given status keeps a player out of the starting lineup is a lineup.py
    concern, not a fixture concern; this module makes no such judgement.
    A player's bye_week is a plain int and is never used to zero out his
    projected stats here. Real feeds zero out a bye player's projection;
    this fixture deliberately does not, so that the bye exclusion is proved
    by lineup.py's own logic and not by the sample data happening to be zero.

teams.json
    {"teams": [team, ...]}. Exactly one team has is_owner_team true. A team
    has team_id, name, manager, is_owner_team, and roster, where roster is a
    list of {player_id, selected_slot}. selected_slot is whatever slot name
    the manager currently has that player parked in (a starting slot name,
    "BN", or "IR").

matchups.json
    {"matchups": [matchup, ...]}. Each matchup has matchup_id, week,
    team_ids (a two element list). Multiple weeks may be present.

projections.json
    {"projections": [projection, ...]}. Each projection has week,
    player_id, stats, where stats is a raw stat line such as
    {"rushing_yards": 78.0, "receptions": 3.0, ...}. A stats mapping may
    contain keys with no entry in settings.scoring.stats (for example
    "targets" in this fixture); scoring.py is expected to ignore unknown
    keys rather than error on them. Not every player has an entry in every
    week: this fixture's week 2 only covers the owner team, on purpose, so
    that scoring.py can be proven to score an absent player as 0.0 for a
    week that does otherwise have data, rather than crashing. A week with
    NO projections at all (never populated) is a different, harder failure
    and projections_for_week raises EngineError for it.

free_agents.json
    {"free_agents": [{"player_id", "percent_owned"}, ...]}. A free agent's
    full player record (name, positions, status, bye_week) lives in
    players.json like any other player; this file only marks that he is
    not on a roster and records his ownership percentage.

engine.fixtures publishes these names for other modules to import:
    FIXTURES_ROOT, DEFAULT_FIXTURE_DIR, load_fixture_league, get_player,
    get_team, owner_team_id, team_roster_player_ids, free_agent_ids,
    projections_for_week, starting_slot_units.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from engine.common import REPO_ROOT, EngineError, load_json

FIXTURES_ROOT = REPO_ROOT / "fixtures"
DEFAULT_FIXTURE_DIR = FIXTURES_ROOT / "sample_league"

_VALID_WAIVER_TYPES = ("faab", "priority")

_LEAGUE_FILE = "league.json"
_PLAYERS_FILE = "players.json"
_TEAMS_FILE = "teams.json"
_MATCHUPS_FILE = "matchups.json"
_PROJECTIONS_FILE = "projections.json"
_FREE_AGENTS_FILE = "free_agents.json"


def _load_named(fixture_dir: Path, filename: str) -> dict[str, Any]:
    path = fixture_dir / filename
    if not path.exists():
        raise EngineError(f"fixture file missing: {path}")
    return load_json(path)


def load_fixture_league(
    fixture_dir: Path | None = None, *, waiver_type: str | None = None
) -> dict[str, Any]:
    """Load the six fixture files and merge them into one league dict.

    The returned dict has exactly eleven top level keys: league_id, name,
    season, current_week, num_teams, settings, players, teams, matchups,
    projections, free_agents. The first six come straight from league.json;
    the rest are the lists unwrapped from their per-file wrapper objects.

    waiver_type, when given, must be "faab" or "priority" and overwrites
    settings.waiver.type in the returned dict; anything else raises
    EngineError. This is a test and demo affordance only, so both waiver
    branches can be exercised against this one fixture: a real league has
    exactly one waiver type and never needs this override.
    """
    if fixture_dir is None:
        fixture_dir = DEFAULT_FIXTURE_DIR

    league = _load_named(fixture_dir, _LEAGUE_FILE)
    players = _load_named(fixture_dir, _PLAYERS_FILE)
    teams = _load_named(fixture_dir, _TEAMS_FILE)
    matchups = _load_named(fixture_dir, _MATCHUPS_FILE)
    projections = _load_named(fixture_dir, _PROJECTIONS_FILE)
    free_agents = _load_named(fixture_dir, _FREE_AGENTS_FILE)

    settings = dict(league["settings"])
    if waiver_type is not None:
        if waiver_type not in _VALID_WAIVER_TYPES:
            raise EngineError(
                f"invalid waiver_type override: {waiver_type!r} "
                f"(must be one of {_VALID_WAIVER_TYPES})"
            )
        waiver = dict(settings["waiver"])
        waiver["type"] = waiver_type
        settings["waiver"] = waiver

    return {
        "league_id": league["league_id"],
        "name": league["name"],
        "season": league["season"],
        "current_week": league["current_week"],
        "num_teams": league["num_teams"],
        "settings": settings,
        "players": players["players"],
        "teams": teams["teams"],
        "matchups": matchups["matchups"],
        "projections": projections["projections"],
        "free_agents": free_agents["free_agents"],
    }


def get_player(league: dict[str, Any], player_id: str) -> dict[str, Any]:
    """Return the player record for player_id, or raise EngineError naming it."""
    for player in league["players"]:
        if player["player_id"] == player_id:
            return player
    raise EngineError(f"unknown player_id: {player_id}")


def get_team(league: dict[str, Any], team_id: str) -> dict[str, Any]:
    """Return the team record for team_id, or raise EngineError naming it."""
    for team in league["teams"]:
        if team["team_id"] == team_id:
            return team
    raise EngineError(f"unknown team_id: {team_id}")


def owner_team_id(league: dict[str, Any]) -> str:
    """Return the team_id of the single team with is_owner_team true.

    Raises EngineError if zero teams or more than one team is flagged as the
    owner team. A silent None here would otherwise surface three modules
    downstream as an unrelated failure, so it is caught at the source.
    """
    owners = [team["team_id"] for team in league["teams"] if team.get("is_owner_team")]
    if len(owners) != 1:
        raise EngineError(
            f"expected exactly one owner team, found {len(owners)}: {owners}"
        )
    return owners[0]


def team_roster_player_ids(league: dict[str, Any], team_id: str) -> list[str]:
    """Return the player_ids on team_id's roster, in roster order."""
    team = get_team(league, team_id)
    return [entry["player_id"] for entry in team["roster"]]


def free_agent_ids(league: dict[str, Any]) -> list[str]:
    """Return the player_ids of every free agent, in free_agents.json order."""
    return [entry["player_id"] for entry in league["free_agents"]]


def projections_for_week(league: dict[str, Any], week: int) -> dict[str, dict[str, float]]:
    """Return {player_id: stats} for the given week only.

    A player merely absent from a populated week is not an error here: he
    simply has no entry in the returned mapping, and it is scoring.py's job
    to turn that absence into 0.0 projected points. Only a week with NO
    projections at all (this fixture never populates every week for every
    player, on purpose, see the module docstring) raises EngineError, naming
    the week, since that is a real data gap rather than an expected partial
    week.
    """
    result: dict[str, dict[str, float]] = {}
    for projection in league["projections"]:
        if projection["week"] == week:
            result[projection["player_id"]] = projection["stats"]
    if not result:
        raise EngineError(f"no projections found for week {week}")
    return result


def starting_slot_units(league: dict[str, Any]) -> list[dict[str, Any]]:
    """Expand settings.roster_slots into one unit per individual starting slot.

    Slots with "starting": false (bench, IR) are skipped entirely. Each unit
    is {"slot": <slot name>, "unit": <index within that slot name, from 0>,
    "eligible_positions": [...]}, in the declared roster_slots order. For
    the fixture shipped here this returns nine units (QB, RB x2, WR x2, TE,
    W/R/T, K, DEF).
    """
    units: list[dict[str, Any]] = []
    for slot in league["settings"]["roster_slots"]:
        if not slot.get("starting"):
            continue
        for index in range(slot["count"]):
            units.append(
                {
                    "slot": slot["slot"],
                    "unit": index,
                    "eligible_positions": list(slot["eligible_positions"]),
                }
            )
    return units
