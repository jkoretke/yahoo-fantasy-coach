"""Tests for engine.email_render: the deterministic fallback email bodies
rendered straight from a run brief, with no Claude involvement.

Every test builds a real brief through engine.brief.build_brief against
engine.fixtures.load_fixture_league(), rather than inlining a duplicate
sample brief, so these tests exercise the real shapes those modules
produce.
"""
from __future__ import annotations

import pytest

from engine.brief import build_brief
from engine.common import EngineError
from engine.email_render import (
    INACTIVE_CHANGE_KEYS,
    format_changes,
    format_inactive_changes,
    format_lineup,
    format_matchup,
    format_trades,
    format_waivers,
    render_plain_email,
    subject_for,
)
from engine.fixtures import load_fixture_league
from engine.prose_gate import check_draft
from engine.trades import trade_ideas

EM_DASH = "\u2014"


def _assert_no_em_dash(*texts: str) -> None:
    for text in texts:
        assert EM_DASH not in text


@pytest.fixture()
def league():
    return load_fixture_league()


@pytest.fixture()
def priority_league():
    return load_fixture_league(waiver_type="priority")


@pytest.fixture()
def weekly_brief(league):
    # Owner team t1, week 3: the fixture week whose optimal lineup drops
    # p1002 (status O) and p1005 (bye 3), and whose W/R/T call between
    # p1012 and p1007 is a flagged toss up.
    return build_brief(league)


# ---------------------------------------------------------------------------
# INACTIVE_CHANGE_KEYS
# ---------------------------------------------------------------------------


def test_inactive_change_keys_is_the_documented_contract():
    assert INACTIVE_CHANGE_KEYS == (
        "player_id",
        "name",
        "slot",
        "status",
        "reason",
        "replacement_player_id",
        "replacement_name",
        "points_gained",
    )


# ---------------------------------------------------------------------------
# subject_for
# ---------------------------------------------------------------------------


def test_subject_for_weekly(weekly_brief):
    assert subject_for("weekly", weekly_brief) == "[Fantasy] Week 3 plan: Sample Squad One"


def test_subject_for_gameday(weekly_brief):
    assert subject_for("gameday", weekly_brief) == "[Fantasy] Game day lineup: week 3"


def test_subject_for_waiver(weekly_brief):
    assert subject_for("waiver", weekly_brief) == "[Fantasy] Waiver claims: week 3"


def test_subject_for_inactive_single_change(weekly_brief):
    change = {
        "player_id": "p1002",
        "name": "Brix Duskin",
        "slot": "RB",
        "status": "O",
        "reason": "Ruled out pregame.",
        "replacement_player_id": "p1010",
        "replacement_name": "Trace Winslow",
        "points_gained": 10.5,
    }
    subject = subject_for("inactive", weekly_brief, extra=[change])
    assert subject == "[Fantasy] Inactive alert: Brix Duskin"


def test_subject_for_inactive_two_changes_appends_and_n_more(weekly_brief):
    # The fixture produces exactly two inactive changes at week 3: p1002
    # (status O) and p1005 (bye 3). An unqualified single-name subject is
    # not implementable, hence "and 1 more".
    changes = [
        {
            "player_id": "p1002",
            "name": "Brix Duskin",
            "slot": "RB",
            "status": "O",
            "reason": "Ruled out pregame.",
            "replacement_player_id": "p1010",
            "replacement_name": "Trace Winslow",
            "points_gained": 10.5,
        },
        {
            "player_id": "p1005",
            "name": "Tanner Elderfield",
            "slot": "WR",
            "status": "BYE",
            "reason": "On bye in week 3.",
            "replacement_player_id": "p1011",
            "replacement_name": "Corbin Rourke",
            "points_gained": 8.9,
        },
    ]
    subject = subject_for("inactive", weekly_brief, extra=changes)
    assert subject == "[Fantasy] Inactive alert: Brix Duskin and 1 more"


def test_subject_for_inactive_with_no_changes_raises(weekly_brief):
    with pytest.raises(EngineError):
        subject_for("inactive", weekly_brief, extra=[])


def test_subject_for_unknown_routine_raises(weekly_brief):
    with pytest.raises(EngineError):
        subject_for("bogus", weekly_brief)


def test_subject_for_extra_ignored_for_non_inactive_routines(weekly_brief):
    # extra is documented as read only for the inactive routine.
    noise = [{"name": "Should Be Ignored"}]
    assert subject_for("weekly", weekly_brief, extra=noise) == subject_for("weekly", weekly_brief)


# ---------------------------------------------------------------------------
# render_plain_email: weekly
# ---------------------------------------------------------------------------


def test_weekly_render_with_trades_none(weekly_brief):
    subject, body = render_plain_email("weekly", weekly_brief, trades=None)

    assert subject == "[Fantasy] Week 3 plan: Sample Squad One"
    assert "no trade ideas this week" in body.lower()
    assert f"{weekly_brief['points_left_on_bench']:.2f}" in body
    assert "not built yet" in body.lower()
    _assert_no_em_dash(subject, body)


def test_weekly_render_points_left_on_bench_appears(weekly_brief):
    _, body = render_plain_email("weekly", weekly_brief, trades=None)
    assert f"{weekly_brief['points_left_on_bench']:.2f}" in body


def test_weekly_render_flagged_toss_up_names_both_options(weekly_brief):
    # engine.lineup.lineup_changes flags the W/R/T call between p1012
    # (Deacon Brightwater) and p1007 (Silas Stonebridge) as a toss up at
    # week 3 in the default fixture.
    toss_ups = [change for change in weekly_brief["lineup_changes"] if change.get("toss_up")]
    assert len(toss_ups) == 1
    first, second = toss_ups[0]["toss_up_options"]
    assert {first["name"], second["name"]} == {"Deacon Brightwater", "Silas Stonebridge"}

    _, body = render_plain_email("weekly", weekly_brief, trades=None)
    assert "toss up" in body.lower()
    assert "Deacon Brightwater" in body
    assert "Silas Stonebridge" in body


def test_weekly_render_with_real_trade_ideas(league):
    # t1 at weeks 3 and 4 has no genuine trade idea in the fixture (no
    # partner both surplus-matches and clears the safe-to-remove check),
    # but t2 at week 4 does, so build that team/week's own brief and
    # trades together rather than pairing mismatched data.
    brief = build_brief(league, team_id="t2", week=4)
    ideas = trade_ideas(league, "t2", 4)
    assert ideas["ideas"], "expected the fixture to produce at least one real trade idea"

    subject, body = render_plain_email("weekly", brief, trades=ideas)

    assert subject == "[Fantasy] Week 4 plan: Sample Squad Two"
    first_idea = ideas["ideas"][0]
    assert first_idea["partner_team_name"] in body
    assert first_idea["send"]["name"] in body
    assert first_idea["receive"]["name"] in body
    assert first_idea["note"] in body
    assert "no trade ideas this week" not in body.lower()
    _assert_no_em_dash(subject, body)


def test_weekly_render_includes_the_full_optimal_lineup(weekly_brief):
    _, body = render_plain_email("weekly", weekly_brief, trades=None)
    for assignment in weekly_brief["optimal_lineup"]["assignments"]:
        if assignment["player_id"] is not None:
            assert assignment["name"] in body


# ---------------------------------------------------------------------------
# render_plain_email: gameday
# ---------------------------------------------------------------------------


def test_gameday_render_is_self_contained_with_every_starter_named(weekly_brief):
    subject, body = render_plain_email("gameday", weekly_brief)

    assert subject == "[Fantasy] Game day lineup: week 3"
    for assignment in weekly_brief["optimal_lineup"]["assignments"]:
        if assignment["player_id"] is not None:
            assert assignment["name"] in body
    # This is a full lineup, not a diff: it names players the optimal
    # lineup promotes (p1010, p1011, p1012) that are NOT the manager's
    # currently rostered starters at this slot.
    assert "Trace Winslow" in body
    assert "Corbin Rourke" in body
    assert "Deacon Brightwater" in body
    _assert_no_em_dash(subject, body)


# ---------------------------------------------------------------------------
# render_plain_email: waiver (both branches)
# ---------------------------------------------------------------------------


def test_waiver_render_faab_branch(weekly_brief):
    subject, body = render_plain_email("waiver", weekly_brief)

    assert subject == "[Fantasy] Waiver claims: week 3"
    assert "$" in body  # faab_remaining and bids are dollar amounts
    targets = weekly_brief["waivers"]["targets"]
    assert {target["verdict"] for target in targets} == {"claim", "skip"}
    for target in targets:
        assert target["name"] in body
        assert f"verdict: {target['verdict']}" in body
    assert body.count("verdict:") == len(targets)
    _assert_no_em_dash(subject, body)


def test_waiver_render_priority_branch(priority_league):
    priority_brief = build_brief(priority_league)
    subject, body = render_plain_email("waiver", priority_brief)

    assert subject == "[Fantasy] Waiver claims: week 3"
    targets = priority_brief["waivers"]["targets"]
    assert {target["verdict"] for target in targets} == {"claim", "skip"}
    for target in targets:
        assert target["name"] in body
        assert f"verdict: {target['verdict']}" in body
    assert body.count("verdict:") == len(targets)
    # Priority specific facts: waiver position, required gain, and the
    # plain "not worth burning" statement on at least one skip.
    assert str(priority_brief["waivers"]["waiver_position"]) in body
    assert f"{priority_brief['waivers']['required_gain']:.2f}" in body
    assert "not worth burning waiver position" in body.lower()
    _assert_no_em_dash(subject, body)


def test_waiver_render_never_raises_keyerror_on_faab_fixture(weekly_brief):
    # Regression guard: reading required_gain unconditionally is a
    # KeyError on the default (faab) fixture.
    render_plain_email("waiver", weekly_brief)


def test_format_waivers_rejects_unknown_waiver_type():
    fake = {
        "team_id": "t1",
        "week": 3,
        "waiver_type": "bogus",
        "targets": [],
    }
    with pytest.raises(EngineError):
        format_waivers(fake)


# ---------------------------------------------------------------------------
# render_plain_email: inactive
# ---------------------------------------------------------------------------


def test_inactive_render_shows_the_exact_swap(weekly_brief):
    changes = [
        {
            "player_id": "p1002",
            "name": "Brix Duskin",
            "slot": "RB",
            "status": "O",
            "reason": "Ruled out pregame.",
            "replacement_player_id": "p1010",
            "replacement_name": "Trace Winslow",
            "points_gained": 10.5,
        },
        {
            "player_id": "p1005",
            "name": "Tanner Elderfield",
            "slot": "WR",
            "status": "BYE",
            "reason": "On bye in week 3.",
            "replacement_player_id": "p1011",
            "replacement_name": "Corbin Rourke",
            "points_gained": 8.9,
        },
    ]
    subject, body = render_plain_email("inactive", weekly_brief, inactive_changes=changes)

    assert subject == "[Fantasy] Inactive alert: Brix Duskin and 1 more"
    assert "Brix Duskin" in body
    assert "Trace Winslow" in body
    assert "Tanner Elderfield" in body
    assert "Corbin Rourke" in body
    _assert_no_em_dash(subject, body)


def test_inactive_render_with_no_replacement_states_so():
    change = {
        "player_id": "p1002",
        "name": "Brix Duskin",
        "slot": "RB",
        "status": "O",
        "reason": "Ruled out pregame.",
        "replacement_player_id": None,
        "replacement_name": None,
        "points_gained": 0.0,
    }
    body = format_inactive_changes([change])
    assert "no replacement available" in body.lower()


# ---------------------------------------------------------------------------
# render_plain_email: unknown routine
# ---------------------------------------------------------------------------


def test_render_plain_email_unknown_routine_raises(weekly_brief):
    with pytest.raises(EngineError):
        render_plain_email("bogus", weekly_brief)


# ---------------------------------------------------------------------------
# The individual format_ helpers, called directly
# ---------------------------------------------------------------------------


def test_format_lineup_reports_every_named_starter_and_the_total(weekly_brief):
    text = format_lineup(weekly_brief["optimal_lineup"])
    for assignment in weekly_brief["optimal_lineup"]["assignments"]:
        if assignment["player_id"] is not None:
            assert assignment["name"] in text
    assert f"{weekly_brief['optimal_lineup']['total_points']:.2f}" in text


def test_format_changes_empty_list_says_no_changes_needed():
    text = format_changes([])
    assert "no lineup changes needed" in text.lower()


def test_format_matchup_reports_margin_and_slot_edges(weekly_brief):
    text = format_matchup(weekly_brief["matchup"])
    matchup = weekly_brief["matchup"]
    assert matchup["team"]["team_name"] in text
    assert matchup["opponent"]["team_name"] in text
    assert f"{abs(matchup['margin']):.2f}" in text


def test_format_trades_none_is_one_line_notice():
    text = format_trades(None)
    assert "no trade ideas this week" in text.lower()


def test_format_trades_empty_ideas_is_also_one_line_notice():
    text = format_trades({"team_id": "t1", "week": 3, "surplus": [], "deficit": [], "ideas": []})
    assert "no trade ideas this week" in text.lower()


# ---------------------------------------------------------------------------
# No em dash, across every routine this module can render.
# ---------------------------------------------------------------------------


def test_no_em_dash_across_every_routine(weekly_brief, priority_league):
    priority_brief = build_brief(priority_league)
    inactive_changes = [
        {
            "player_id": "p1002",
            "name": "Brix Duskin",
            "slot": "RB",
            "status": "O",
            "reason": "Ruled out pregame.",
            "replacement_player_id": "p1010",
            "replacement_name": "Trace Winslow",
            "points_gained": 10.5,
        }
    ]

    for routine, brief, kwargs in [
        ("weekly", weekly_brief, {"trades": None}),
        ("gameday", weekly_brief, {}),
        ("waiver", weekly_brief, {}),
        ("waiver", priority_brief, {}),
        ("inactive", weekly_brief, {"inactive_changes": inactive_changes}),
    ]:
        subject, body = render_plain_email(routine, brief, **kwargs)
        _assert_no_em_dash(subject, body)


def test_render_plain_email_passes_its_own_prose_gate_for_every_routine(
    weekly_brief, priority_league, league
):
    # Regression guard for a QA finding: engine.prose_gate.check_draft
    # rejected render_plain_email's OWN weekly and waiver bodies (a
    # "START X over Y" comparative sentence reads as a verdict conflict
    # for Y, and "Drop <name>" reads as an unrecognized player name),
    # which would have made the Claude prose path fall back to plain on
    # nearly every real run. check_draft is meant to gate a Claude draft,
    # but a fully deterministic, brief-accurate rendering must never trip
    # it either; if it does, that draft would have been wrongly rejected
    # too. Every routine's plain body is exercised here, including a week
    # with a real (non-empty) trade idea, since that path names a second
    # team's player brief_player_names does not otherwise carry.
    priority_brief = build_brief(priority_league)
    inactive_changes = [
        {
            "player_id": "p1002",
            "name": "Brix Duskin",
            "slot": "RB",
            "status": "O",
            "reason": "Ruled out pregame.",
            "replacement_player_id": "p1010",
            "replacement_name": "Trace Winslow",
            "points_gained": 10.5,
        },
        {
            "player_id": "p1005",
            "name": "Tanner Elderfield",
            "slot": "WR",
            "status": "BYE",
            "reason": "On bye in week 3.",
            "replacement_player_id": "p1011",
            "replacement_name": "Corbin Rourke",
            "points_gained": 8.9,
        },
    ]

    trade_brief = build_brief(league, team_id="t2", week=4)
    trade_ideas_result = trade_ideas(league, "t2", 4)
    assert trade_ideas_result["ideas"], "this case only proves anything with real ideas in it"
    trade_brief_for_check = dict(trade_brief)
    trade_brief_for_check["trades"] = trade_ideas_result

    cases = [
        ("weekly", weekly_brief, {"trades": None}, weekly_brief),
        ("weekly", trade_brief, {"trades": trade_ideas_result}, trade_brief_for_check),
        ("gameday", weekly_brief, {}, weekly_brief),
        ("waiver", weekly_brief, {}, weekly_brief),
        ("waiver", priority_brief, {}, priority_brief),
        ("inactive", weekly_brief, {"inactive_changes": inactive_changes}, weekly_brief),
    ]

    for routine, brief, kwargs, brief_for_check in cases:
        subject, body = render_plain_email(routine, brief, **kwargs)
        result = check_draft(body, brief_for_check)
        assert result["ok"], (routine, result["violations"])
