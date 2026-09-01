"""Waiver claim ranking for both FAAB and rolling priority leagues.

A league runs its waivers one of two ways, read from
league["settings"]["waiver"]["type"]:

    "faab"     Each team holds a season long dollar budget
               (settings.waiver.faab_remaining) and bids blind dollars on a
               claim. A real upgrade is always worth a bid, even a small
               one, so FAAB never declines a claim, it only bids low.

    "priority" Teams hold a rolling priority order
               (settings.waiver.priority_order), and winning a claim sends
               the winning team to the back of that order. Priority is
               scarce, so spending the number one slot on a marginal
               upgrade is a real cost, and this module can and does
               recommend skipping a positive gain when it is not worth
               that cost. See rank_waiver_targets below for exactly when.

Both branches share the same claim simulation: evaluate_claim works out,
for one free agent, which current roster player would actually be dropped
and how many points the team's optimal lineup gains from the swap. Only
the verdict and bid/priority bookkeeping layered on top of that simulation
differ between the two branches.

Public names other chunks import directly:
    PRIORITY_BASE_GAIN, FAAB_BID_TIERS, waiver_position, drop_candidates,
    evaluate_claim, faab_bid, required_priority_gain, rank_waiver_targets.
"""
from __future__ import annotations

from typing import Any

from engine.common import EngineError, round_points
from engine.fixtures import free_agent_ids, get_player, get_team
# EXCLUDED_STATUSES is imported (not used directly below) so the status set
# that keeps a player out of a starting lineup is never re-declared here;
# is_startable is used in drop_candidates' fallback path.
# DEFAULT_TOSS_UP_MARGIN_POINTS is imported rather than redeclared here, so
# the whole engine shares docs/plan.md's one toss up margin instead of
# carrying a second, easy to drift, copy of the same number.
from engine.lineup import (  # noqa: F401 - EXCLUDED_STATUSES kept per this module's own comment above
    DEFAULT_TOSS_UP_MARGIN_POINTS,
    EXCLUDED_STATUSES,
    is_startable,
    optimal_lineup,
)
from engine.scoring import projected_points_by_player

# The bar a single week projected points gain must clear to be worth
# spending waiver priority on, before the position based multiplier is
# applied by required_priority_gain. Pinned; do not retune to make a test
# pass, since five other chunks share this fixture and these thresholds.
PRIORITY_BASE_GAIN = 2.0

# FAAB bid sizing tiers, walked in order. Each entry is
# (points_gained_threshold, percent_of_remaining_budget). The first tier
# whose threshold is at or below the claim's points_gained applies. Pinned
# for the same reason as PRIORITY_BASE_GAIN above.
FAAB_BID_TIERS: tuple[tuple[float, float], ...] = (
    (5.0, 0.25),
    (3.0, 0.12),
    (1.5, 0.06),
    (0.0, 0.01),
)

_VALID_WAIVER_TYPES = ("faab", "priority")


def waiver_position(league: dict[str, Any], team_id: str) -> int:
    """Return team_id's 1 based rolling priority position, 1 is best.

    Raises EngineError naming the team if it is absent from
    settings.waiver.priority_order, since a team with no priority position
    cannot be evaluated for waivers.
    """
    priority_order = league["settings"]["waiver"]["priority_order"]
    for index, ordered_team_id in enumerate(priority_order):
        if ordered_team_id == team_id:
            return index + 1
    raise EngineError(f"team_id not found in waiver priority_order: {team_id}")


def drop_candidates(
    league: dict[str, Any],
    team_id: str,
    week: int,
    points: dict[str, float] | None = None,
) -> list[str]:
    """Return the roster players team_id may legally drop to make a claim.

    A drop candidate is any roster player who is NOT in team_id's current
    optimal starting lineup for week, and whose selected_slot is not "IR".
    A player parked in the IR slot is never offered up as a drop, no
    matter how little he projects to score. The result is sorted by
    projected points ascending then player_id, so the least valuable
    player to keep is first.

    If every roster player is either a current optimal starter or sits in
    the IR slot, there is nothing to legally drop; the fallback is the
    single lowest projected player among the current optimal starters, so
    a claim always has some drop to evaluate against.
    """
    if points is None:
        points = projected_points_by_player(league, week)

    team = get_team(league, team_id)
    optimal = optimal_lineup(league, team_id, week, points=points)
    starter_ids = set(optimal["starter_ids"])

    candidates = [
        entry["player_id"]
        for entry in team["roster"]
        if entry["player_id"] not in starter_ids and entry["selected_slot"] != "IR"
    ]
    candidates.sort(key=lambda player_id: (points.get(player_id, 0.0), player_id))

    if candidates:
        return candidates

    startable_starters = [
        player_id
        for player_id in optimal["starter_ids"]
        if is_startable(get_player(league, player_id), week)
    ]
    fallback_pool = startable_starters or list(optimal["starter_ids"])
    fallback_pool.sort(key=lambda player_id: (points.get(player_id, 0.0), player_id))
    return fallback_pool[:1]


def evaluate_claim(
    league: dict[str, Any],
    team_id: str,
    week: int,
    free_agent_id: str,
    points: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Simulate team_id claiming free_agent_id for week and return the result.

    Computes team_id's baseline optimal lineup total, then, for every
    legal drop candidate in turn (drop_candidates' own sorted order,
    ascending projected points then player_id), computes the optimal
    lineup with free_agent_id added and that candidate removed. The drop
    that produces the highest resulting total is kept; the first candidate
    to reach that maximum wins the tie, since dropping any player already
    outside the baseline optimal lineup produces the identical new total,
    and the least valuable such player is the one drop_candidates lists
    first.

    Raises EngineError, via engine.fixtures.get_player, if free_agent_id
    is not a known player_id.
    """
    free_agent = get_player(league, free_agent_id)

    if points is None:
        points = projected_points_by_player(league, week)

    baseline_points = optimal_lineup(league, team_id, week, points=points)["total_points"]
    candidates = drop_candidates(league, team_id, week, points=points)

    best_total: float | None = None
    best_drop_id: str | None = None
    for candidate_id in candidates:
        result = optimal_lineup(
            league,
            team_id,
            week,
            points=points,
            extra_player_ids=[free_agent_id],
            excluded_player_ids=[candidate_id],
        )
        total = result["total_points"]
        if best_total is None or total > best_total:
            best_total = total
            best_drop_id = candidate_id

    drop_player = get_player(league, best_drop_id)
    percent_owned = 0
    for entry in league["free_agents"]:
        if entry["player_id"] == free_agent_id:
            percent_owned = entry["percent_owned"]
            break

    projected_total = round_points(best_total)
    baseline_total = round_points(baseline_points)

    return {
        "player_id": free_agent_id,
        "name": free_agent["name"],
        "positions": list(free_agent["positions"]),
        "nfl_team": free_agent["nfl_team"],
        "percent_owned": percent_owned,
        "projected_points": round_points(points.get(free_agent_id, 0.0)),
        "drop_player_id": best_drop_id,
        "drop_player_name": drop_player["name"],
        "baseline_points": baseline_total,
        "projected_total": projected_total,
        "points_gained": round_points(projected_total - baseline_total),
    }


def faab_bid(points_gained: float, faab_remaining: int) -> int:
    """Return the dollar bid for a claim worth points_gained, capped at
    faab_remaining.

    There is nothing to bid on, or nothing to bid with, when points_gained
    is at or below zero or faab_remaining is at or below zero, so that
    returns 0. Otherwise the first FAAB_BID_TIERS entry whose threshold is
    at or below points_gained sets the percent of the remaining budget to
    bid; the bid is never less than 1 dollar and never more than
    faab_remaining itself.
    """
    if points_gained <= 0 or faab_remaining <= 0:
        return 0

    percent = FAAB_BID_TIERS[-1][1]
    for threshold, tier_percent in FAAB_BID_TIERS:
        if points_gained >= threshold:
            percent = tier_percent
            break

    bid = max(1, int(round(faab_remaining * percent)))
    return min(bid, faab_remaining)


def required_priority_gain(league: dict[str, Any], team_id: str) -> float:
    """Return the points gain team_id must clear to be worth spending its
    rolling waiver priority.

    Holding the number one priority position is scarce, so the bar rises
    the better team_id's current position is:
        PRIORITY_BASE_GAIN * (1 + (num_teams - waiver_position) / num_teams)
    A team at position 1 of 4 faces 2.0 * (1 + 3/4) = 3.5; a team at
    position 4 of 4 faces 2.0 * (1 + 0/4) = 2.0, the base gain itself.
    """
    num_teams = league["num_teams"]
    position = waiver_position(league, team_id)
    return PRIORITY_BASE_GAIN * (1 + (num_teams - position) / num_teams)


def rank_waiver_targets(league: dict[str, Any], team_id: str, week: int) -> dict[str, Any]:
    """Rank every free agent as a waiver target for team_id in week.

    Reads the branch from league["settings"]["waiver"]["type"]; anything
    other than "faab" or "priority" raises EngineError. Every free agent
    is evaluated once with evaluate_claim, against one shared
    projected_points_by_player map, then sorted by points_gained
    descending, then player_id.

    The two branches are genuinely different, not one shape relabelled:

    FAAB never declines a real upgrade. Every target with points_gained
    greater than 0 gets verdict "claim"; a weak upgrade is expressed as a
    low bid (faab_bid), never as a refusal. Only a target at or below zero
    gain gets verdict "skip", with bid 0. The result also carries
    "faab_remaining" for team_id, read from settings.waiver.faab_remaining;
    a team absent from that mapping raises EngineError rather than
    silently defaulting to a zero budget.

    Priority CAN correctly skip a real upgrade. verdict is "claim" only
    when points_gained is at least required_priority_gain(league,
    team_id); otherwise it is "skip", even though points_gained is
    strictly positive. At position 1 of 4 the bar is 3.5 points, so a
    genuine one to two point upgrade is not worth burning the number one
    claim on. This is a single week projected points heuristic: it has no
    notion of rest of season value, upcoming schedule, or how many future
    claims the team might want, so a small but durable upgrade can read as
    skip here. That is a known limitation of this phase, not a bug; a
    later phase can weigh those factors without this function's callers
    needing to change. The result also carries "waiver_position" and
    "required_gain" for team_id, and no target here carries a "bid" key.

    Priority also flags docs/plan.md's toss up band: when a target's
    points_gained sits strictly within DEFAULT_TOSS_UP_MARGIN_POINTS of
    required_gain either way, the claim/skip line is close enough that a
    beat writer note should be allowed to break it. That target keeps
    whichever verdict the threshold above computed (Python still makes
    every call) but additionally carries "toss_up": true, "toss_up_margin"
    and "toss_up_options": ["claim", "skip"]. A target outside that band
    carries none of those three keys and its verdict is final. FAAB never
    carries them either, since FAAB never declines a real upgrade in the
    first place, so there is no close claim/skip line to flag.
    """
    waiver_type = league["settings"]["waiver"]["type"]
    if waiver_type not in _VALID_WAIVER_TYPES:
        raise EngineError(
            f"invalid waiver type: {waiver_type!r} (must be one of {_VALID_WAIVER_TYPES})"
        )

    points = projected_points_by_player(league, week)
    claims = [
        evaluate_claim(league, team_id, week, free_agent_id, points=points)
        for free_agent_id in free_agent_ids(league)
    ]

    result: dict[str, Any] = {
        "team_id": team_id,
        "week": week,
        "waiver_type": waiver_type,
    }

    if waiver_type == "faab":
        faab_remaining_by_team = league["settings"]["waiver"]["faab_remaining"]
        if team_id not in faab_remaining_by_team:
            raise EngineError(f"team_id not found in waiver faab_remaining: {team_id}")
        team_faab = faab_remaining_by_team[team_id]

        targets = []
        for claim in claims:
            target = dict(claim)
            target["bid"] = faab_bid(claim["points_gained"], team_faab)
            target["verdict"] = "claim" if claim["points_gained"] > 0 else "skip"
            targets.append(target)

        result["faab_remaining"] = team_faab
    else:
        position = waiver_position(league, team_id)
        required_gain = required_priority_gain(league, team_id)

        targets = []
        for claim in claims:
            target = dict(claim)
            target["required_gain"] = required_gain
            target["priority_position"] = position
            target["verdict"] = "claim" if claim["points_gained"] >= required_gain else "skip"
            if abs(claim["points_gained"] - required_gain) < DEFAULT_TOSS_UP_MARGIN_POINTS:
                target["toss_up"] = True
                target["toss_up_margin"] = DEFAULT_TOSS_UP_MARGIN_POINTS
                target["toss_up_options"] = ["claim", "skip"]
            targets.append(target)

        result["waiver_position"] = position
        result["required_gain"] = required_gain

    targets.sort(key=lambda target: (-target["points_gained"], target["player_id"]))
    result["targets"] = targets
    return result
