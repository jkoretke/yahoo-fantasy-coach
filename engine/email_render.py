"""Deterministic plain-text email rendering, straight from a run brief.

This module renders a subject line and a plain-text email body from a run
brief JSON (engine.brief.build_brief's return shape) with no Claude
involvement at all: no HTML, no markdown tables, wrapped for a phone
screen. It serves two callers: it is the body used in --fixtures mode (a
later phase's run wrappers), and it is the fallback engine.prose_gate's
caller falls back to whenever a Claude draft is rejected, since the plan
this repo follows says the email always goes out even when the draft is
not usable.

INACTIVE_CHANGE_KEYS is the contract this module shares with
engine.inactive_run (a later phase): the shape of one inactive change
dict this module's format_inactive_changes and render_plain_email("inactive",
...) consume. It is defined here, not there, because this module is what
gives the shape meaning; engine.inactive_run is expected to produce dicts
carrying exactly these keys.

Every point number rendered here comes from a value the brief (or the
caller supplied trades / inactive_changes data) already ran through
engine.common.round_points; this module only formats that number for
display (two decimal places), it never rounds a fresh one.

Public names: INACTIVE_CHANGE_KEYS, format_lineup, format_changes,
format_matchup, format_waivers, format_trades, format_inactive_changes,
subject_for, render_plain_email.
"""
from __future__ import annotations

import textwrap
from typing import Any

from engine.common import EngineError

# The contract engine.inactive_run (a later phase) produces and this
# module's format_inactive_changes / render_plain_email("inactive", ...)
# consume. One dict per player whose game-day status changed: which
# player, what happened to him, and the swap made in response.
INACTIVE_CHANGE_KEYS = (
    "player_id",
    "name",
    "slot",
    "status",
    "reason",
    "replacement_player_id",
    "replacement_name",
    "points_gained",
)

_WIDTH = 78


def _fmt_pts(value: float) -> str:
    """Format an already round_points-rounded value for display."""
    return f"{value:.2f}"


def _wrap_line(line: str) -> str:
    """Wrap one line to about _WIDTH columns, preserving its own leading
    indent on continuation lines so a sub-bullet stays a sub-bullet."""
    if len(line) <= _WIDTH:
        return line
    leading = len(line) - len(line.lstrip(" "))
    indent = " " * leading
    content = line[leading:]
    return textwrap.fill(
        content,
        width=max(_WIDTH - leading, 20),
        initial_indent=indent,
        subsequent_indent=indent + "  ",
    )


def _section(title: str, lines: list[str]) -> str:
    """Build one titled, phone readable plain-text block."""
    body = lines if lines else ["(none)"]
    wrapped = [_wrap_line(line) for line in body]
    return "\n".join([title, "-" * len(title), *wrapped])


def format_lineup(lineup: dict[str, Any]) -> str:
    """Render a full lineup (optimal_lineup or current_lineup shaped)
    as a self contained, readable-alone block: one line per starting
    slot unit, an unfilled unit shown as empty, then the total.

    This is what makes engine.email_render's gameday routine self
    contained rather than a diff: every started player's name and points
    line up here regardless of what the manager currently has set.
    """
    lines: list[str] = []
    for assignment in lineup["assignments"]:
        slot = assignment["slot"]
        name = assignment["name"]
        if assignment["player_id"] is None:
            lines.append(f"{slot}: (empty)")
            continue
        note = "" if assignment["startable"] else " (not startable)"
        lines.append(f"{slot}: {name} - {_fmt_pts(assignment['points'])} pts{note}")
    lines.append(f"Total: {_fmt_pts(lineup['total_points'])} pts")
    return _section("Recommended lineup", lines)


def format_changes(changes: list[dict[str, Any]]) -> str:
    """Render the start/sit calls turning a current lineup into the
    optimal one, one line per change plus its point margin.

    A flagged toss up (engine.lineup.lineup_changes's toss_up band) gets
    an explicit extra line naming both options the pick is between.
    """
    if not changes:
        return _section("Start/sit calls", ["No lineup changes needed this week."])

    lines: list[str] = []
    for change in changes:
        lines.append(
            f"{change['slot']}: START {change['start_name']} over "
            f"{change['sit_name']} (+{_fmt_pts(change['points_gained'])} pts)"
        )
        if change.get("toss_up"):
            first, second = change["toss_up_options"]
            # Ends on a lowercase word on purpose (see engine.prose_gate's
            # own module docstring): a capitalized player name immediately
            # followed by the next section's capitalized opening word
            # would otherwise merge into one unrecognized run.
            lines.append(
                f"  Toss up, within {_fmt_pts(change['toss_up_margin'])} pts: "
                f"could go either way between {first['name']} and {second['name']} this week."
            )
    return _section("Start/sit calls", lines)


def format_matchup(matchup: dict[str, Any]) -> str:
    """Render this week's matchup projection: the two projected totals,
    who is favored by how much, and the per slot edge breakdown."""
    team = matchup["team"]
    opponent = matchup["opponent"]

    if matchup["favorite_team_id"] == team["team_id"]:
        favored = f"{team['team_name']} favored"
    else:
        favored = f"{opponent['team_name']} favored"

    lines = [
        f"Week {matchup['week']}: {team['team_name']} vs {opponent['team_name']}",
        f"Projected: {_fmt_pts(team['total_points'])} - {_fmt_pts(opponent['total_points'])} "
        f"({favored} by {_fmt_pts(abs(matchup['margin']))} pts)",
        "",
        "Slot by slot edge:",
    ]
    for edge in matchup["slot_edges"]:
        sign = "+" if edge["edge"] >= 0 else ""
        lines.append(
            f"  {edge['slot']}: {edge['team_name']} ({_fmt_pts(edge['team_points'])}) vs "
            f"{edge['opponent_name']} ({_fmt_pts(edge['opponent_points'])}), "
            f"edge {sign}{_fmt_pts(edge['edge'])}"
        )
    return _section("Matchup projection", lines)


def format_waivers(waivers: dict[str, Any]) -> str:
    """Render the ranked waiver claims, branching on waivers["waiver_type"].

    faab shows each target's dollar bid off faab_remaining. priority
    shows the team's waiver_position and required_gain, and states
    plainly when a target's gain is not worth burning that position on.
    Both branches show every target's drop candidate and verdict; a
    priority target flagged as a toss up (its gain sits within the
    shared margin of required_gain) gets an extra line naming both
    options the call is between.
    """
    waiver_type = waivers["waiver_type"]
    lines: list[str] = []

    if waiver_type == "faab":
        lines.append(f"FAAB remaining: ${waivers['faab_remaining']}")
        for target in waivers["targets"]:
            lines.append(
                f"{target['name']} ({'/'.join(target['positions'])}, "
                f"{target['nfl_team']}) - proj {_fmt_pts(target['projected_points'])} pts, "
                f"{target['percent_owned']}% owned"
            )
            lines.append(
                f"  Drop {target['drop_player_name']} for net "
                f"{_fmt_pts(target['points_gained'])} pts, bid ${target['bid']}, "
                f"verdict: {target['verdict']}"
            )
    elif waiver_type == "priority":
        lines.append(
            f"Waiver position: {waivers['waiver_position']}, required gain to "
            f"claim: {_fmt_pts(waivers['required_gain'])} pts"
        )
        for target in waivers["targets"]:
            lines.append(
                f"{target['name']} ({'/'.join(target['positions'])}, "
                f"{target['nfl_team']}) - proj {_fmt_pts(target['projected_points'])} pts, "
                f"{target['percent_owned']}% owned"
            )
            lines.append(
                f"  Drop {target['drop_player_name']} for net "
                f"{_fmt_pts(target['points_gained'])} pts, needs "
                f"{_fmt_pts(target['required_gain'])} pts, verdict: {target['verdict']}"
            )
            if target["verdict"] == "skip":
                lines.append(
                    f"  Not worth burning waiver position "
                    f"{target['priority_position']} on this gain."
                )
            if target.get("toss_up"):
                first, second = target["toss_up_options"]
                lines.append(
                    f"  Toss up, within {_fmt_pts(target['toss_up_margin'])} pts of the "
                    f"required gain: could go either way between {first} and {second}."
                )
    else:
        raise EngineError(
            f"invalid waiver type: {waiver_type!r} (must be one of ('faab', 'priority'))"
        )

    return _section(f"Waiver claims ({waiver_type})", lines)


def format_trades(trades: dict[str, Any] | None) -> str:
    """Render up to engine.trades.trade_ideas's ideas, or a plain one
    line notice when trades is None or carries no ideas.

    The section itself always appears, even with nothing to report,
    since a caller (engine.email_render's weekly routine) must not
    silently drop the trade ideas section just because there is nothing
    to say this week.
    """
    if not trades or not trades.get("ideas"):
        return _section("Trade ideas", ["No trade ideas this week."])

    lines: list[str] = []
    for idea in trades["ideas"]:
        send = idea["send"]
        receive = idea["receive"]
        lines.append(
            f"Send {send['name']} ({send['position']}) to "
            f"{idea['partner_team_name']} for {receive['name']} "
            f"({receive['position']}), net {_fmt_pts(idea['points_gained'])} pts"
        )
        lines.append(f"  {idea['note']}")
    return _section("Trade ideas", lines)


def format_inactive_changes(changes: list[dict[str, Any]]) -> str:
    """Render one screen of inactive alerts: the player, what happened,
    and the exact swap made, from a list of INACTIVE_CHANGE_KEYS shaped
    dicts."""
    if not changes:
        return _section("Inactive alert", ["No inactive alerts."])

    lines: list[str] = []
    for change in changes:
        # name/slot and reason share one line on purpose: change["status"]
        # (a bare "O" or "BYE") ending a line on its own, immediately
        # followed by the next line's capitalized opening word, is exactly
        # the cross-line merge engine.prose_gate's own module docstring
        # warns about, and reason already restates status in context
        # ("Ruled out: status O", "On bye (week 3)"), so nothing is lost.
        lines.append(f"{change['name']} ({change['slot']}): {change['reason']}")
        if change.get("replacement_player_id"):
            lines.append(
                f"  Swap in {change['replacement_name']} "
                f"(+{_fmt_pts(change['points_gained'])} pts)"
            )
        else:
            lines.append("  No replacement available; leave the slot as is.")
    return _section("Inactive alert", lines)


def subject_for(routine: str, brief: dict[str, Any], *, extra: list[dict[str, Any]] | None = None) -> str:
    """Return the exact subject line for one routine's email.

    extra is read only for the "inactive" routine, where it is the list
    of INACTIVE_CHANGE_KEYS shaped changes: the subject names the first
    change's player, with " and {n} more" appended when there is more
    than one. It is ignored for every other routine. An unknown routine
    raises EngineError.
    """
    if routine == "weekly":
        return f"[Fantasy] Week {brief['week']} plan: {brief['team']['name']}"
    if routine == "gameday":
        return f"[Fantasy] Game day lineup: week {brief['week']}"
    if routine == "waiver":
        return f"[Fantasy] Waiver claims: week {brief['week']}"
    if routine == "inactive":
        changes = extra or []
        if not changes:
            raise EngineError(
                "subject_for(\"inactive\", ...) needs at least one change in extra"
            )
        subject = f"[Fantasy] Inactive alert: {changes[0]['name']}"
        if len(changes) > 1:
            subject += f" and {len(changes) - 1} more"
        return subject
    raise EngineError(f"unknown routine: {routine!r}")


def render_plain_email(
    routine: str,
    brief: dict[str, Any],
    *,
    trades: dict[str, Any] | None = None,
    inactive_changes: list[dict[str, Any]] | None = None,
    config: dict[str, Any] | None = None,
) -> tuple[str, str]:
    """Render a fully deterministic (subject, body) pair for one routine.

    routine must be one of "weekly", "gameday", "waiver", "inactive"; any
    other value raises EngineError. config is accepted so this function's
    signature matches what a later phase's run wrappers need to call, but
    it is not read this phase: this function never imports engine.config
    and never branches on config's contents.

    weekly renders the full optimal lineup, every start/sit call and its
    point margin, points_left_on_bench, the matchup projection, the trade
    ideas section built from trades (a one line notice when trades is
    None, never an omitted section), and a closing line stating plainly
    that the news pass is not built yet.

    gameday renders the full recommended lineup from
    brief["optimal_lineup"]["assignments"], self contained rather than a
    diff against a prior lineup, so it reads correctly alone on a phone.

    waiver renders the ranked claims from brief["waivers"], branching on
    waiver_type exactly as format_waivers does.

    inactive renders one screen from inactive_changes: the player, what
    happened, and the exact swap made.
    """
    if routine == "weekly":
        subject = subject_for("weekly", brief)
        closing = _wrap_line(
            "News check: the injury and late breaking news pass is not built "
            "yet. Confirm player status yourself before kickoff."
        )
        body = "\n\n".join(
            [
                f"Week {brief['week']} plan for {brief['team']['name']}",
                format_lineup(brief["optimal_lineup"]),
                format_changes(brief["lineup_changes"]),
                f"Points left on bench: {_fmt_pts(brief['points_left_on_bench'])} pts",
                format_matchup(brief["matchup"]),
                format_trades(trades),
                closing,
            ]
        )
        return subject, body

    if routine == "gameday":
        subject = subject_for("gameday", brief)
        body = "\n\n".join(
            [
                f"Game day lineup for {brief['team']['name']}, week {brief['week']}",
                format_lineup(brief["optimal_lineup"]),
            ]
        )
        return subject, body

    if routine == "waiver":
        subject = subject_for("waiver", brief)
        body = "\n\n".join(
            [
                f"Waiver claims for {brief['team']['name']}, week {brief['week']}",
                format_waivers(brief["waivers"]),
            ]
        )
        return subject, body

    if routine == "inactive":
        changes = inactive_changes or []
        subject = subject_for("inactive", brief, extra=changes)
        body = format_inactive_changes(changes)
        return subject, body

    raise EngineError(f"unknown routine: {routine!r}")
