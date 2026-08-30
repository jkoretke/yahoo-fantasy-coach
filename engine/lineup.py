"""Exact optimal legal lineup solve, plus start/sit deltas against a team's
currently selected lineup.

A "unit" is one individual starting slot, as expanded by
engine.fixtures.starting_slot_units (for example the roster slot RB with
count 2 becomes two units, {"slot": "RB", "unit": 0, ...} and
{"slot": "RB", "unit": 1, ...}). This module solves the assignment of
startable players onto those units that maximizes total projected points,
respecting each unit's eligible_positions, and compares that optimal
lineup against whatever lineup the manager actually has selected right
now.

Public names. engine.waivers imports is_startable, EXCLUDED_STATUSES and
DEFAULT_TOSS_UP_MARGIN_POINTS directly, so none of the three is renamed and
neither the status set nor the margin is ever re-inlined elsewhere:
    EXCLUDED_STATUSES, MAX_SLOT_UNITS, DEFAULT_TOSS_UP_MARGIN_POINTS,
    is_startable, optimal_lineup, current_lineup, lineup_changes.
"""
from __future__ import annotations

from typing import Any

from engine.common import EngineError, round_points
from engine.fixtures import (
    get_player,
    get_team,
    starting_slot_units,
    team_roster_player_ids,
)
from engine.scoring import projected_points_by_player

# Statuses that keep a player out of a starting lineup slot regardless of his
# projection. Shared with engine.waivers, which imports this set directly to
# pick drop candidates, so it lives here once and only here.
EXCLUDED_STATUSES = frozenset({"O", "IR", "SUSP"})

# The bitmask dynamic program below carries one bit per starting slot unit,
# so its state space is exponential in the unit count. A real league never
# comes close to this many starting units; it exists as a guard rail against
# a misconfigured fixture or settings file blowing up the solve.
MAX_SLOT_UNITS = 16

# The one shared toss up margin for the whole engine (docs/plan.md: "one
# shared default for start/sit, matchup, and waivers, not three separate
# numbers to keep straight"). A later phase reads the real value from
# config/league.yaml; this module constant is the Phase 1 default until
# that config layer exists. engine.waivers imports this name rather than
# declaring its own, so the band is never defined twice.
DEFAULT_TOSS_UP_MARGIN_POINTS = 2.0


def is_startable(player: dict[str, Any], week: int) -> bool:
    """Return whether player is eligible to occupy a starting slot for week.

    A player is not startable when his status is one of EXCLUDED_STATUSES
    ("O", "IR", "SUSP") or when his bye_week equals week. A player record
    missing "status" or "bye_week" raises EngineError naming the player_id,
    so a malformed record fails here instead of surfacing as a bare
    KeyError three modules downstream.
    """
    if "status" not in player or "bye_week" not in player:
        player_id = player.get("player_id", "<unknown>")
        raise EngineError(
            f"player record missing status or bye_week: {player_id}"
        )
    if player["status"] in EXCLUDED_STATUSES:
        return False
    if player["bye_week"] == week:
        return False
    return True


def _eligible_unit_indices(player: dict[str, Any], units: list[dict[str, Any]]) -> list[int]:
    positions = set(player["positions"])
    return [
        index
        for index, unit in enumerate(units)
        if positions & set(unit["eligible_positions"])
    ]


def _empty_assignment(unit: dict[str, Any]) -> dict[str, Any]:
    return {
        "slot": unit["slot"],
        "unit": unit["unit"],
        "player_id": None,
        "name": None,
        "positions": [],
        "points": 0.0,
        "startable": False,
    }


def optimal_lineup(
    league: dict[str, Any],
    team_id: str,
    week: int,
    points: dict[str, float] | None = None,
    extra_player_ids: list[str] | None = None,
    excluded_player_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Solve the maximum total projected points legal lineup for team_id/week.

    points defaults to engine.scoring.projected_points_by_player(league,
    week). extra_player_ids adds players (typically a free agent) to the
    candidate pool, for simulating a waiver claim; excluded_player_ids
    removes players (typically a bench player being dropped) from it. An
    excluded player never appears in starter_ids or bench_ids of the
    result, so a simulated drop does not linger on the bench of the post
    claim lineup. An extra player id that is not a known player_id raises
    EngineError.

    This is an EXACT solver: a bitmask dynamic program over the starting
    slot units, not a greedy pass, because a player eligible for more than
    one unit (a WR who also fills W/R/T, say) can make a naive
    position-by-position greedy miss the true optimum. Candidates are
    sorted once by (-points, player_id) so ties break on a stated rule
    rather than dict insertion order, then each is considered in turn:
    dp[mask] holds the best total achievable with the units named by mask
    already filled, and each candidate either leaves dp alone (skipped) or
    improves some dp[mask | unit_bit] from dp[mask] (placed into one empty
    eligible unit); only a strictly greater total replaces the previous
    best, so the reconstruction below is deterministic. A unit with no
    eligible startable candidate left simply stays unfilled.

    Since starting_slot_units(league) has at most MAX_SLOT_UNITS entries
    (or this raises EngineError below), the mask space is small and the
    whole solve is fast even though it is exact rather than greedy.
    """
    units = starting_slot_units(league)
    unit_count = len(units)
    if unit_count > MAX_SLOT_UNITS:
        raise EngineError(
            f"league has {unit_count} starting slot units, exceeding "
            f"MAX_SLOT_UNITS={MAX_SLOT_UNITS}; the bitmask solver is "
            "exponential in the unit count"
        )

    if points is None:
        points = projected_points_by_player(league, week)
    extra_player_ids = list(extra_player_ids or [])
    excluded_player_ids = list(excluded_player_ids or [])

    roster_ids = team_roster_player_ids(league, team_id)

    for player_id in extra_player_ids:
        get_player(league, player_id)

    excluded_set = set(excluded_player_ids)
    raw_pool = [
        player_id
        for player_id in list(roster_ids) + extra_player_ids
        if player_id not in excluded_set
    ]
    raw_pool = list(dict.fromkeys(raw_pool))

    candidates = [
        player_id
        for player_id in raw_pool
        if is_startable(get_player(league, player_id), week)
    ]
    candidates.sort(key=lambda player_id: (-points.get(player_id, 0.0), player_id))

    eligibility = [
        _eligible_unit_indices(get_player(league, player_id), units)
        for player_id in candidates
    ]

    size = 1 << unit_count
    negative_infinity = float("-inf")
    dp = [negative_infinity] * size
    dp[0] = 0.0
    # choice[i + 1][mask] is the unit index candidates[i] was placed into to
    # reach dp state mask at that step, or None if candidates[i] was skipped.
    choice: list[list[int | None]] = [[None] * size for _ in range(len(candidates) + 1)]

    for i, player_id in enumerate(candidates):
        value = points.get(player_id, 0.0)
        elig = eligibility[i]
        next_dp = list(dp)
        row = choice[i + 1]
        for mask in range(size):
            base = dp[mask]
            if base == negative_infinity:
                continue
            for unit_index in elig:
                bit = 1 << unit_index
                if mask & bit:
                    continue
                new_mask = mask | bit
                candidate_total = base + value
                if candidate_total > next_dp[new_mask]:
                    next_dp[new_mask] = candidate_total
                    row[new_mask] = unit_index
        dp = next_dp

    # Break ties toward the mask with more units filled: a zero point
    # startable candidate must still occupy his unit rather than be left
    # off in favor of an equally scored, but emptier, lineup.
    best_mask = max(range(size), key=lambda mask: (dp[mask], bin(mask).count("1")))

    assignment_by_unit: dict[int, str] = {}
    mask = best_mask
    for i in range(len(candidates), 0, -1):
        unit_index = choice[i][mask]
        if unit_index is not None:
            assignment_by_unit[unit_index] = candidates[i - 1]
            mask ^= 1 << unit_index

    assignments: list[dict[str, Any]] = []
    for index, unit in enumerate(units):
        player_id = assignment_by_unit.get(index)
        if player_id is None:
            assignments.append(_empty_assignment(unit))
            continue
        player = get_player(league, player_id)
        assignments.append(
            {
                "slot": unit["slot"],
                "unit": unit["unit"],
                "player_id": player_id,
                "name": player["name"],
                "positions": list(player["positions"]),
                "points": round_points(points.get(player_id, 0.0)),
                "startable": True,
            }
        )

    starter_ids = [a["player_id"] for a in assignments if a["player_id"] is not None]
    bench_ids = sorted(
        (player_id for player_id in raw_pool if player_id not in set(starter_ids)),
        key=lambda player_id: (-points.get(player_id, 0.0), player_id),
    )
    total_points = round_points(sum(a["points"] for a in assignments))

    return {
        "team_id": team_id,
        "week": week,
        "assignments": assignments,
        "starter_ids": starter_ids,
        "bench_ids": bench_ids,
        "total_points": total_points,
    }


def current_lineup(
    league: dict[str, Any],
    team_id: str,
    week: int,
    points: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Report the lineup the manager actually has selected right now.

    This builds the same {"assignments", "starter_ids", "bench_ids",
    "total_points"} shape as optimal_lineup, but from each roster entry's
    selected_slot rather than by solving anything. Players whose
    selected_slot is a non starting declared slot (BN, IR) go to
    bench_ids.

    This does not silently repair an illegal lineup: a selected_slot that
    names no declared slot raises EngineError naming the slot, and more
    roster entries carrying a slot name than that slot's declared count
    raises EngineError naming the slot. A player the manager left in a
    starting slot who is NOT startable is still reported in that slot,
    with startable false, but he counts 0.0 points toward the total,
    because a player who is out scores nothing and the cost of leaving him
    there is exactly that empty slot. That is reporting, not repairing,
    and it is what makes the optimal total exceed the current total by
    construction rather than by accident.
    """
    if points is None:
        points = projected_points_by_player(league, week)

    team = get_team(league, team_id)
    units = starting_slot_units(league)

    slot_counts = {slot["slot"]: slot["count"] for slot in league["settings"]["roster_slots"]}
    unit_indices_by_slot: dict[str, list[int]] = {}
    for index, unit in enumerate(units):
        unit_indices_by_slot.setdefault(unit["slot"], []).append(index)

    assignments: list[dict[str, Any] | None] = [None] * len(units)
    bench_ids: list[str] = []
    occurrence: dict[str, int] = {}

    for entry in team["roster"]:
        player_id = entry["player_id"]
        slot_name = entry["selected_slot"]
        if slot_name not in slot_counts:
            raise EngineError(f"unknown selected_slot: {slot_name!r}")
        seen = occurrence.get(slot_name, 0)
        if seen >= slot_counts[slot_name]:
            raise EngineError(
                f"too many roster entries in slot {slot_name!r}: "
                f"declared count is {slot_counts[slot_name]}"
            )
        occurrence[slot_name] = seen + 1

        if slot_name in unit_indices_by_slot:
            unit_index = unit_indices_by_slot[slot_name][seen]
            unit = units[unit_index]
            player = get_player(league, player_id)
            startable = is_startable(player, week)
            reported_points = round_points(points.get(player_id, 0.0)) if startable else 0.0
            assignments[unit_index] = {
                "slot": unit["slot"],
                "unit": unit["unit"],
                "player_id": player_id,
                "name": player["name"],
                "positions": list(player["positions"]),
                "points": reported_points,
                "startable": startable,
            }
        else:
            bench_ids.append(player_id)

    for index, unit in enumerate(units):
        if assignments[index] is None:
            assignments[index] = _empty_assignment(unit)

    filled_assignments = [a for a in assignments if a is not None]
    starter_ids = [a["player_id"] for a in filled_assignments if a["player_id"] is not None]
    bench_ids_sorted = sorted(
        bench_ids, key=lambda player_id: (-points.get(player_id, 0.0), player_id)
    )
    total_points = round_points(sum(a["points"] for a in filled_assignments))

    return {
        "team_id": team_id,
        "week": week,
        "assignments": filled_assignments,
        "starter_ids": starter_ids,
        "bench_ids": bench_ids_sorted,
        "total_points": total_points,
    }


def _pair_remaining_units(
    remaining_current: list[dict[str, Any]], remaining_optimal: list[dict[str, Any]]
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Pair leftover current units with leftover optimal units for the diff.

    Grouped by slot name first, since a same-slot-name pairing produces the
    more sensible label (a RB you are sitting paired with the RB replacing
    him, not a RB paired with a kicker). Within and across groups, both
    sides are walked in ascending points order before pairing, so a cheap
    current occupant is matched against a cheap optimal replacement rather
    than against the most expensive one; this is what keeps an individual
    pair's points_gained from going negative even though the overall
    optimal total always exceeds the current total.

    A slot name whose two sides do not have equal counts (a flex eligible
    player who is a starter in both current and optimal, but under two
    different slot names, shifts the balance by one on each side) leaves a
    leftover on each side; those leftovers are pooled across slot names and
    paired the same way, so every remaining unit is still accounted for.
    """
    current_by_slot: dict[str, list[dict[str, Any]]] = {}
    for unit in remaining_current:
        current_by_slot.setdefault(unit["slot"], []).append(unit)
    optimal_by_slot: dict[str, list[dict[str, Any]]] = {}
    for unit in remaining_optimal:
        optimal_by_slot.setdefault(unit["slot"], []).append(unit)

    def by_points_ascending(unit: dict[str, Any]) -> tuple[float, str]:
        return (unit["points"], unit["player_id"] or "")

    pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    leftover_current: list[dict[str, Any]] = []
    leftover_optimal: list[dict[str, Any]] = []

    for slot_name, current_units in current_by_slot.items():
        optimal_units = optimal_by_slot.pop(slot_name, [])
        current_units = sorted(current_units, key=by_points_ascending)
        optimal_units = sorted(optimal_units, key=by_points_ascending)
        matched = min(len(current_units), len(optimal_units))
        for index in range(matched):
            pairs.append((current_units[index], optimal_units[index]))
        leftover_current.extend(current_units[matched:])
        leftover_optimal.extend(optimal_units[matched:])

    for optimal_units in optimal_by_slot.values():
        leftover_optimal.extend(optimal_units)

    leftover_current.sort(key=by_points_ascending)
    leftover_optimal.sort(key=by_points_ascending)
    pairs.extend(zip(leftover_current, leftover_optimal))
    return pairs


def lineup_changes(
    current: dict[str, Any],
    optimal: dict[str, Any],
    toss_up_margin_points: float = DEFAULT_TOSS_UP_MARGIN_POINTS,
) -> list[dict[str, Any]]:
    """Return the start/sit verdicts turning current into optimal.

    A player who is a starter in BOTH current and optimal is never reported
    as a change, even when the two solves happened to place him in
    different units of the same slot (two RBs with identical eligible
    positions can land in either order out of the bitmask solver, since
    which literal unit index each gets is arbitrary relative to roster
    order). Diffing strictly by unit index instead of by this "is he
    starting either way" check would report a phantom sit for a player who
    is not actually being benched, alongside a phantom start for a player
    who is not actually a new addition, including a nonsensical negative
    points_gained on one of the pair. Pinning shared starters first, then
    pairing only the genuinely differing units (_pair_remaining_units
    above), is what keeps every reported points_gained the true cost of a
    real bench decision.

    One dict per genuine change, shaped {"slot", "unit", "start_player_id",
    "start_name", "sit_player_id", "sit_name", "points_gained"}.
    points_gained is round_points(the optimal assignment's counted points
    minus the current assignment's counted points), using the same counted
    "points" value each assignment already reports, so benching a non
    startable player (whose current assignment always counts 0.0) shows his
    full slot cost rather than his raw projection. No player_id ever
    appears as both a start_player_id and a sit_player_id in the returned
    list, no points_gained is ever negative, and the total of points_gained
    across every entry always equals optimal total_points minus current
    total_points, since a pinned shared starter contributes the same
    counted points on both sides and so nets to zero. Sorted by
    points_gained descending, then slot, then unit. Returns [] when current
    is already optimal, including when it differs from optimal only by
    which unit index a shared starter happens to occupy.

    toss_up_margin_points is docs/plan.md's shared toss up band (see
    DEFAULT_TOSS_UP_MARGIN_POINTS). An entry whose points_gained falls
    strictly inside that margin is a call close enough that a beat writer
    note should be allowed to break it: it is tagged "toss_up": true and
    gains "toss_up_margin" and "toss_up_options" (the two players the pick
    is between, start then sit, each {"player_id", "name"}). engine.waivers
    tags its own toss ups with the same three key names, so
    engine.prose_gate (a later phase) has one shape to check rather than
    two. An entry outside the band is left exactly as it always was: no
    toss_up key at all, and the verdict is final.
    """
    pinned_players = set(current["starter_ids"]) & set(optimal["starter_ids"])

    remaining_current = [
        unit for unit in current["assignments"] if unit["player_id"] not in pinned_players
    ]
    remaining_optimal = [
        unit for unit in optimal["assignments"] if unit["player_id"] not in pinned_players
    ]

    changes: list[dict[str, Any]] = []
    for current_unit, optimal_unit in _pair_remaining_units(remaining_current, remaining_optimal):
        if current_unit["player_id"] == optimal_unit["player_id"]:
            continue
        points_gained = round_points(optimal_unit["points"] - current_unit["points"])
        change: dict[str, Any] = {
            "slot": optimal_unit["slot"],
            "unit": optimal_unit["unit"],
            "start_player_id": optimal_unit["player_id"],
            "start_name": optimal_unit["name"],
            "sit_player_id": current_unit["player_id"],
            "sit_name": current_unit["name"],
            "points_gained": points_gained,
        }
        if 0 < points_gained < toss_up_margin_points:
            change["toss_up"] = True
            change["toss_up_margin"] = toss_up_margin_points
            change["toss_up_options"] = [
                {"player_id": optimal_unit["player_id"], "name": optimal_unit["name"]},
                {"player_id": current_unit["player_id"], "name": current_unit["name"]},
            ]
        changes.append(change)

    changes.sort(key=lambda change: (-change["points_gained"], change["slot"], change["unit"]))
    return changes
