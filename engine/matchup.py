"""Weekly matchup projection: a team's optimal lineup total against the
optimal lineup total of its scheduled opponent for the same week.

Both sides of a matchup are scored from one shared projected points map
(engine.scoring.projected_points_by_player), computed once and passed to
both engine.lineup.optimal_lineup calls, so a matchup is never scored with
two independently rebuilt point maps that could drift apart.

Public names:
    week_matchups, find_opponent, matchup_projection.
"""
from __future__ import annotations

from typing import Any

from engine.common import EngineError, round_points
from engine.fixtures import get_team, starting_slot_units
from engine.lineup import optimal_lineup
from engine.scoring import projected_points_by_player


def week_matchups(league: dict[str, Any], week: int) -> list[dict[str, Any]]:
    """Return every matchup scheduled for week, in fixture order.

    A week with no scheduled matchups at all raises EngineError naming the
    week, since an empty schedule is a data problem, not a normal state.
    """
    matchups = [m for m in league["matchups"] if m["week"] == week]
    if not matchups:
        raise EngineError(f"no matchups found for week {week}")
    return matchups


def find_opponent(league: dict[str, Any], team_id: str, week: int) -> str:
    """Return the team_id of team_id's opponent for week.

    Raises EngineError when team_id is not scheduled that week, and
    EngineError when the matchup that does contain team_id does not have
    exactly two team_ids.
    """
    for matchup in week_matchups(league, week):
        team_ids = matchup["team_ids"]
        if team_id not in team_ids:
            continue
        if len(team_ids) != 2:
            raise EngineError(
                f"matchup {matchup['matchup_id']} does not have exactly "
                f"two team_ids: {team_ids}"
            )
        other = [tid for tid in team_ids if tid != team_id]
        return other[0]
    raise EngineError(f"team {team_id!r} is not scheduled for week {week}")


def _slot_edges(
    team_lineup: dict[str, Any], opponent_lineup: dict[str, Any], units: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    edges: list[dict[str, Any]] = []
    for unit, team_assignment, opponent_assignment in zip(
        units, team_lineup["assignments"], opponent_lineup["assignments"]
    ):
        team_points = team_assignment["points"]
        opponent_points = opponent_assignment["points"]
        edges.append(
            {
                "slot": unit["slot"],
                "unit": unit["unit"],
                "team_player_id": team_assignment["player_id"],
                "team_name": team_assignment["name"],
                "team_points": team_points,
                "opponent_player_id": opponent_assignment["player_id"],
                "opponent_name": opponent_assignment["name"],
                "opponent_points": opponent_points,
                "edge": round_points(team_points - opponent_points),
            }
        )

    edges.sort(key=lambda edge: (-edge["edge"], edge["slot"], edge["unit"]))
    return edges


def matchup_projection(
    league: dict[str, Any],
    team_id: str,
    week: int,
    points: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Project team_id's optimal lineup total against its week opponent's.

    Both optimal_lineup calls share one projected points map (points when
    given, otherwise computed once here) so both sides are scored
    identically. Returns a JSON serializable dict:
        {"week", "matchup_id", "team", "opponent", "margin",
         "favorite_team_id", "slot_edges"}.
    margin is round_points(team total minus opponent total); positive
    means team_id is favored. favorite_team_id is team_id when margin is
    positive, the opponent's team_id when margin is negative, and the
    alphabetically lower of the two team ids when margin is exactly zero,
    so the result is deterministic. slot_edges pairs the two sides'
    assignment at each starting slot unit, sorted by edge descending then
    slot then unit; the raw assignment order inside "team" and "opponent"
    is left untouched.
    """
    opponent_id = find_opponent(league, team_id, week)
    matchup_id = next(
        m["matchup_id"]
        for m in week_matchups(league, week)
        if team_id in m["team_ids"]
    )

    if points is None:
        points = projected_points_by_player(league, week)

    team_lineup = optimal_lineup(league, team_id, week, points=points)
    opponent_lineup = optimal_lineup(league, opponent_id, week, points=points)

    team_lineup = dict(team_lineup)
    team_lineup["team_name"] = get_team(league, team_id)["name"]
    opponent_lineup = dict(opponent_lineup)
    opponent_lineup["team_name"] = get_team(league, opponent_id)["name"]

    margin = round_points(team_lineup["total_points"] - opponent_lineup["total_points"])
    if margin > 0:
        favorite_team_id = team_id
    elif margin < 0:
        favorite_team_id = opponent_id
    else:
        favorite_team_id = min(team_id, opponent_id)

    units = starting_slot_units(league)
    slot_edges = _slot_edges(team_lineup, opponent_lineup, units)

    return {
        "week": week,
        "matchup_id": matchup_id,
        "team": team_lineup,
        "opponent": opponent_lineup,
        "margin": margin,
        "favorite_team_id": favorite_team_id,
        "slot_edges": slot_edges,
    }
