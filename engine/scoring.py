"""Turn raw per player stat projections into fantasy points under a league's
scoring rules.

This module reads the settings.scoring mapping documented in
engine.fixtures (a "stats" mapping of stat_key to points_per_unit, plus an
optional "brackets" mapping of stat_key to a list of inclusive
[low, high, points] triples) and turns one stat line, or a whole week of
projections, into fantasy points. It does not fetch or invent stat data
itself; that is engine.fixtures' job.
"""
from __future__ import annotations

from typing import Any

from engine.common import EngineError, round_points
from engine.fixtures import projections_for_week


def _bracket_points(stat_key: str, value: float, bracket_list: Any) -> float:
    """Return the points for value under stat_key's list of brackets.

    Each entry in bracket_list must be a three element [low, high, points]
    sequence, both bounds inclusive; anything else raises EngineError. A
    value that matches no bracket also raises EngineError naming the stat
    key and the value, rather than being clamped to the nearest bracket.
    """
    for entry in bracket_list:
        if not isinstance(entry, (list, tuple)) or len(entry) != 3:
            raise EngineError(
                f"malformed scoring bracket for stat {stat_key!r}: {entry!r} "
                "(expected a three element [low, high, points] sequence)"
            )
        low, high, points = entry
        if low <= value <= high:
            return float(points)
    raise EngineError(
        f"value {value!r} for stat {stat_key!r} matched no scoring bracket"
    )


def score_stat_line(stats: dict[str, float], scoring_rules: dict[str, Any]) -> float:
    """Score one raw stat line against a league's settings.scoring mapping.

    scoring_rules is the dict at league["settings"]["scoring"]. Its "stats"
    key maps a stat name to points per unit; its optional "brackets" key
    maps a stat name (for example defense_points_allowed) to a list of
    inclusive [low, high, points] triples.

    The total is the sum, over every stat key present in BOTH stats and
    scoring_rules["stats"], of value times points_per_unit, plus, for every
    key in scoring_rules.get("brackets", {}) that is also present in
    stats, the points of the bracket whose low <= value <= high.

    A stat key present in the stat line but named in neither the per unit
    stats rules nor the brackets is ignored silently, not an error. Real
    stat feeds send extra columns this league's scoring does not use (a
    raw target count, say), and that is expected rather than a problem.

    A bracketed value that matches no bracket raises EngineError naming the
    stat key and the value; it is never clamped to the nearest bracket. A
    malformed bracket entry, anything that is not a three element
    sequence, also raises EngineError.

    Returns the total rounded through engine.common.round_points.
    """
    per_unit_rules = scoring_rules.get("stats", {})
    total = 0.0
    for stat_key, value in stats.items():
        if stat_key in per_unit_rules:
            total += float(value) * float(per_unit_rules[stat_key])

    brackets = scoring_rules.get("brackets", {})
    for stat_key, bracket_list in brackets.items():
        if stat_key not in stats:
            continue
        total += _bracket_points(stat_key, float(stats[stat_key]), bracket_list)

    return round_points(total)


def project_player_points(league: dict[str, Any], player_id: str, week: int) -> float:
    """Return player_id's projected fantasy points for week.

    The player's raw stat line is looked up through
    engine.fixtures.projections_for_week and scored against
    league["settings"]["scoring"] with score_stat_line.

    This has two different failure behaviours for two different situations,
    and they are meant to differ:
      - A player who simply has no entry in an otherwise populated week
        (a real roster can hold a player a stat feed has no line for)
        returns 0.0 rather than raising.
      - A week with NO projections at all is a different, harder failure:
        projections_for_week raises EngineError for that case, naming the
        week, and that error is left to propagate out of this function
        unchanged, since it means the data itself is missing rather than
        just this one player having no line.
    """
    week_stats = projections_for_week(league, week)
    stats = week_stats.get(player_id)
    if stats is None:
        return 0.0
    return score_stat_line(stats, league["settings"]["scoring"])


def projected_points_by_player(league: dict[str, Any], week: int) -> dict[str, float]:
    """Return {player_id: points} for every player in league["players"].

    This is the single projected-points map every downstream module (lineup,
    matchup, waivers, brief) is expected to build from and pass around. It
    covers free agents as well as rostered players, since it is built from
    league["players"] directly rather than from any one team's roster.

    Nothing is cached here: scoring one week is cheap, and a caller that
    needs the map more than once should just call this again or hold onto
    the returned dict itself.
    """
    return {
        player["player_id"]: project_player_points(league, player["player_id"], week)
        for player in league["players"]
    }
