"""Position surplus and deficit across every roster, and simple trade ideas
built from them.

This reads no source engine.fixtures does not already provide and makes no
network call: roster composition comes from engine.fixtures.get_team,
team_roster_player_ids and starting_slot_units, and player value comes from
engine.scoring.projected_points_by_player. A position's "starting_slots" is
how many single position starting units the league declares for it
(engine.fixtures.starting_slot_units); a unit whose slot accepts more than
one position (a flex slot such as W/R/T) is never counted toward any single
position's starting_slots, so a flex slot stays visible in position_demand
without inventing fractional demand for any one position.

"surplus" at a position is how many more startable rostered players a team
holds at that position than it has starting slots for; "deficit" is the
same idea when that difference is negative. trade_ideas pairs one team's
surplus position against another team's surplus position, offering only
when the receiving side would actually plug a hole (its own surplus at
that position is zero or negative), and never proposes moving a player out
of a slot the sending or receiving side still needs to fill.

Public names: position_demand, team_position_inventory, league_position_table,
trade_ideas.
"""
from __future__ import annotations

from typing import Any

from engine.common import EngineError, round_points
from engine.fixtures import get_player, get_team, starting_slot_units, team_roster_player_ids
from engine.lineup import is_startable
from engine.scoring import projected_points_by_player


def position_demand(league: dict[str, Any]) -> dict[str, Any]:
    """Return the league's starting demand, split into single and flex.

    Walks engine.fixtures.starting_slot_units(league) once. A unit with
    exactly one eligible position adds 1 to that position's count under
    "single". A unit with more than one eligible position (a flex slot
    such as W/R/T) is recorded under "flex" instead, as
    {"slot", "eligible_positions"}, and adds nothing to any single
    position's count, so the flex slot stays visible without inventing
    fractional demand for the positions it accepts.

    Returns {"single": {position: count}, "flex": [flex unit, ...]}, both
    in starting_slot_units' own order.
    """
    single: dict[str, int] = {}
    flex: list[dict[str, Any]] = []
    for unit in starting_slot_units(league):
        eligible_positions = list(unit["eligible_positions"])
        if len(eligible_positions) == 1:
            position = eligible_positions[0]
            single[position] = single.get(position, 0) + 1
        else:
            flex.append({"slot": unit["slot"], "eligible_positions": eligible_positions})
    return {"single": single, "flex": flex}


def _players_with_position(players: list[dict[str, Any]], position: str) -> list[dict[str, Any]]:
    return [player for player in players if position in player["positions"]]


def _startable_sorted(
    players: list[dict[str, Any]], week: int, points: dict[str, float]
) -> list[dict[str, Any]]:
    startable = [player for player in players if is_startable(player, week)]
    startable.sort(key=lambda player: (-points.get(player["player_id"], 0.0), player["player_id"]))
    return startable


def _surplus_pool(
    players: list[dict[str, Any]], week: int, points: dict[str, float], starting_slots: int
) -> list[dict[str, Any]]:
    """Return the startable players at a position beyond its starting_slots.

    players is already filtered to one position. The pool is the tail of
    the startable-sorted list past index starting_slots, so pool[0] (when
    present) is the single best player a team could trade away from this
    position without touching a player it needs to fill a starting slot.
    """
    return _startable_sorted(players, week, points)[starting_slots:]


def _position_entry(
    roster_players: list[dict[str, Any]],
    position: str,
    starting_slots: int,
    week: int,
    points: dict[str, float],
) -> dict[str, Any]:
    position_players = _players_with_position(roster_players, position)
    rostered = len(position_players)
    startable_players = [player for player in position_players if is_startable(player, week)]
    startable = len(startable_players)
    surplus = startable - starting_slots

    pool = _surplus_pool(position_players, week, points, starting_slots)
    best_surplus_points = round_points(points.get(pool[0]["player_id"], 0.0)) if pool else 0.0

    players_list = sorted(
        (
            {
                "player_id": player["player_id"],
                "name": player["name"],
                "points": round_points(points.get(player["player_id"], 0.0)),
            }
            for player in position_players
        ),
        key=lambda entry: (-entry["points"], entry["player_id"]),
    )

    return {
        "position": position,
        "starting_slots": starting_slots,
        "rostered": rostered,
        "startable": startable,
        "surplus": surplus,
        "best_surplus_points": best_surplus_points,
        "players": players_list,
    }


def team_position_inventory(
    league: dict[str, Any],
    team_id: str,
    week: int,
    points: dict[str, float] | None = None,
) -> list[dict[str, Any]]:
    """Return team_id's roster, broken out one entry per position.

    One entry per position appearing either in
    position_demand(league)["single"] or on team_id's own roster (a
    position a team happens to roster but the league gives no single slot
    for, if any, still gets an entry so it is never silently dropped),
    sorted by position name. See the module docstring for what "surplus"
    and "startable" mean. A player eligible for more than one position
    (for example a WR/TE) counts under each of them.

    "best_surplus_points" is NOT the highest scoring rostered player at
    the position: the top starting_slots startable players, by points,
    are treated as locked starters and are never the surplus candidate.
    It is the highest points among the startable players left over once
    those locked starters are set aside, so it is the value of the one
    player this position could actually trade away without an immediate
    lineup downgrade; 0.0 when no startable player is left over.
    """
    if points is None:
        points = projected_points_by_player(league, week)

    demand = position_demand(league)
    roster_ids = team_roster_player_ids(league, team_id)
    roster_players = [get_player(league, player_id) for player_id in roster_ids]

    positions = set(demand["single"].keys())
    for player in roster_players:
        positions.update(player["positions"])

    inventory = []
    for position in sorted(positions):
        starting_slots = demand["single"].get(position, 0)
        inventory.append(_position_entry(roster_players, position, starting_slots, week, points))
    return inventory


def league_position_table(
    league: dict[str, Any], week: int, points: dict[str, float] | None = None
) -> dict[str, list[dict[str, Any]]]:
    """Return team_position_inventory for every team in league["teams"].

    projected_points_by_player is computed once here (when points is not
    already given) and passed into every team's inventory call, so no two
    teams in the same table are ever scored from two independently rebuilt
    point maps.
    """
    if points is None:
        points = projected_points_by_player(league, week)
    return {
        team["team_id"]: team_position_inventory(league, team["team_id"], week, points=points)
        for team in league["teams"]
    }


def _safe_to_remove(
    inventory_by_position: dict[str, dict[str, Any]],
    player: dict[str, Any],
    week: int,
) -> bool:
    """Return whether trading player away would still leave every position
    he is eligible for at or above its starting_slots.

    A player pulled from a surplus pool is always startable, but he may
    also be eligible for a second position where his own team is not
    running a surplus (a WR/TE who is the team's only real starting TE
    depth, say). Losing him there would drop that position below its
    starting_slots, so the trade is skipped rather than proposed.
    """
    if not is_startable(player, week):
        return True
    for position in player["positions"]:
        entry = inventory_by_position.get(position)
        if entry is None:
            continue
        if entry["startable"] - 1 < entry["starting_slots"]:
            return False
    return True


def trade_ideas(
    league: dict[str, Any],
    team_id: str,
    week: int,
    points: dict[str, float] | None = None,
    limit: int = 5,
) -> dict[str, Any]:
    """Propose up to limit trades that move team_id's surplus toward its need.

    Every other team in the league is a candidate partner. An idea pairs
    one position where team_id has surplus > 0 with one position where the
    partner has surplus > 0 and team_id itself has surplus <= 0, offering
    team_id's best surplus player at the send position for the partner's
    best surplus player at the receive position (see _surplus_pool). A
    pair is skipped whenever completing it would drop either side's
    startable count below its starting_slots at any position either
    traded player is eligible for (_safe_to_remove), and team_id is never
    proposed as its own partner.

    Returns {"team_id", "week", "surplus", "deficit", "ideas"}: surplus
    and deficit are team_id's own team_position_inventory entries with
    surplus > 0 and surplus < 0 respectively; ideas is sorted by
    (-points_gained, partner_team_id, the sent player's player_id) and
    truncated to limit. Every point value is round_points-rounded, and the
    whole result is JSON serializable.
    """
    if points is None:
        points = projected_points_by_player(league, week)

    get_team(league, team_id)  # raises EngineError naming an unknown team_id

    table = league_position_table(league, week, points=points)
    owner_inventory = table[team_id]
    owner_by_position = {entry["position"]: entry for entry in owner_inventory}
    surplus_entries = [entry for entry in owner_inventory if entry["surplus"] > 0]
    deficit_entries = [entry for entry in owner_inventory if entry["surplus"] < 0]

    owner_roster_players = [
        get_player(league, player_id) for player_id in team_roster_player_ids(league, team_id)
    ]
    owner_send_pool: dict[str, list[dict[str, Any]]] = {}
    for entry in surplus_entries:
        position = entry["position"]
        position_players = _players_with_position(owner_roster_players, position)
        owner_send_pool[position] = _surplus_pool(
            position_players, week, points, entry["starting_slots"]
        )

    ideas: list[dict[str, Any]] = []

    for partner in league["teams"]:
        partner_id = partner["team_id"]
        if partner_id == team_id:
            continue

        partner_inventory = table[partner_id]
        partner_by_position = {entry["position"]: entry for entry in partner_inventory}
        partner_surplus_entries = [entry for entry in partner_inventory if entry["surplus"] > 0]
        if not partner_surplus_entries:
            continue

        partner_roster_players = [
            get_player(league, player_id) for player_id in team_roster_player_ids(league, partner_id)
        ]

        for receive_entry in partner_surplus_entries:
            receive_position = receive_entry["position"]
            owner_entry_here = owner_by_position.get(receive_position)
            owner_surplus_here = owner_entry_here["surplus"] if owner_entry_here else 0
            if owner_surplus_here > 0:
                continue

            receive_pool = _surplus_pool(
                _players_with_position(partner_roster_players, receive_position),
                week,
                points,
                receive_entry["starting_slots"],
            )
            if not receive_pool:
                continue
            receive_player = receive_pool[0]

            for send_entry in surplus_entries:
                send_position = send_entry["position"]
                send_pool = owner_send_pool.get(send_position) or []
                if not send_pool:
                    continue
                send_player = send_pool[0]

                if not _safe_to_remove(owner_by_position, send_player, week):
                    continue
                if not _safe_to_remove(partner_by_position, receive_player, week):
                    continue

                send_points = points.get(send_player["player_id"], 0.0)
                receive_points = points.get(receive_player["player_id"], 0.0)
                points_gained = round_points(receive_points - send_points)

                ideas.append(
                    {
                        "partner_team_id": partner_id,
                        "partner_team_name": partner["name"],
                        "send": {
                            "player_id": send_player["player_id"],
                            "name": send_player["name"],
                            "position": send_position,
                            "points": round_points(send_points),
                        },
                        "receive": {
                            "player_id": receive_player["player_id"],
                            "name": receive_player["name"],
                            "position": receive_position,
                            "points": round_points(receive_points),
                        },
                        "send_position": send_position,
                        "receive_position": receive_position,
                        "points_gained": points_gained,
                        "note": (
                            f"Send surplus {send_position} depth to {partner['name']} "
                            f"for help at {receive_position} this week."
                        ),
                    }
                )

    ideas.sort(
        key=lambda idea: (
            -idea["points_gained"],
            idea["partner_team_id"],
            idea["send"]["player_id"],
        )
    )
    ideas = ideas[:limit]

    return {
        "team_id": team_id,
        "week": week,
        "surplus": surplus_entries,
        "deficit": deficit_entries,
        "ideas": ideas,
    }
