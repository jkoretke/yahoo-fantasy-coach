"""Tests for engine.prose_gate: validating a Claude drafted email body
against its own run brief JSON. Every test reads a real brief through
engine.brief.build_brief(engine.fixtures.load_fixture_league()) rather
than hand building one, since the fixture league already ships the one
flagged lineup toss up (p1012 Deacon Brightwater / p1007 Silas
Stonebridge in the W/R/T slot) this gate's carve-out exists for.
"""
from __future__ import annotations

import copy
from pathlib import Path

from engine.brief import build_brief
from engine.fixtures import load_fixture_league
from engine.prose_gate import check_draft, format_violations

_FIXTURE_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "phase4"


def _read(name: str) -> str:
    return (_FIXTURE_DIR / name).read_text()


def _weekly_brief():
    return build_brief(load_fixture_league())


def _priority_waiver_brief():
    return build_brief(load_fixture_league(waiver_type="priority"))


def test_pass_draft_is_ok():
    brief = _weekly_brief()
    draft = _read("draft_weekly_pass.md")

    result = check_draft(draft, brief)

    assert result["ok"] is True
    assert result["violations"] == []
    # The draft names both the owner's team and the opponent by name; a
    # brief-recognized player was still found and reported.
    assert "p1001" in result["named_players"]


def test_fail_draft_has_unknown_player_and_verdict_conflict():
    brief = _weekly_brief()
    draft = _read("draft_weekly_fail.md")

    result = check_draft(draft, brief)

    assert result["ok"] is False
    kinds = {violation["kind"] for violation in result["violations"]}
    assert "unknown-player" in kinds
    assert "verdict-conflict" in kinds


def test_flagged_lineup_toss_up_passes_either_side():
    brief = _weekly_brief()

    # The fixture's own optimal pick: start Deacon Brightwater, sit Silas
    # Stonebridge.
    draft_optimal_side = "Start Deacon Brightwater in the flex over Silas Stonebridge this week."
    result_optimal = check_draft(draft_optimal_side, brief)
    assert result_optimal["ok"] is True

    # The opposite pick is just as valid, since the pair is flagged toss_up.
    draft_other_side = "Bench Deacon Brightwater in the flex, and start Silas Stonebridge instead."
    result_other = check_draft(draft_other_side, brief)
    assert result_other["ok"] is True


def test_non_toss_up_starter_benched_fails():
    brief = _weekly_brief()

    # Dax Voss is a plain optimal starter, not part of any toss up.
    draft = "Bench Dax Voss at quarterback this week."

    result = check_draft(draft, brief)

    assert result["ok"] is False
    assert any(v["kind"] == "verdict-conflict" for v in result["violations"])


def test_flagged_waiver_toss_up_passes_either_word():
    brief = _priority_waiver_brief()
    targets_by_id = {t["player_id"]: t for t in brief["waivers"]["targets"]}

    # f2002 (Beckett Pemberly) is a flagged toss up whose own computed
    # verdict is "claim"; the draft may still say skip.
    assert targets_by_id["f2002"]["toss_up"] is True
    draft_skip_side = "Skip Beckett Pemberly this week, the priority cost is not worth it."
    result_skip = check_draft(draft_skip_side, brief)
    assert result_skip["ok"] is True

    # f2010 (Bodie Millbrook) is a flagged toss up whose own computed
    # verdict is "skip"; the draft may still say claim.
    assert targets_by_id["f2010"]["toss_up"] is True
    draft_claim_side = "Claim Bodie Millbrook off waivers this week."
    result_claim = check_draft(draft_claim_side, brief)
    assert result_claim["ok"] is True


def test_non_toss_up_waiver_target_wrong_side_fails():
    brief = _priority_waiver_brief()
    targets_by_id = {t["player_id"]: t for t in brief["waivers"]["targets"]}

    # f2001 (Rowan Ironside) is not a flagged toss up, and its own verdict
    # is "skip".
    assert "toss_up" not in targets_by_id["f2001"]
    assert targets_by_id["f2001"]["verdict"] == "skip"

    draft = "Claim Rowan Ironside off waivers this week."
    result = check_draft(draft, brief)

    assert result["ok"] is False
    assert any(v["kind"] == "unknown-waiver-verdict" for v in result["violations"])


def test_empty_draft_passes_with_no_violations():
    brief = _weekly_brief()

    result = check_draft("", brief)

    assert result["ok"] is True
    assert result["violations"] == []
    assert result["named_players"] == []


def test_check_draft_never_raises_on_garbage_draft():
    brief = _weekly_brief()

    garbage = bytes(range(0, 256)).decode("latin-1")

    result = check_draft(garbage, brief)

    assert isinstance(result, dict)
    assert "ok" in result
    assert "violations" in result
    assert "named_players" in result


def test_check_draft_never_raises_on_brief_with_empty_lineup_changes():
    brief = copy.deepcopy(_weekly_brief())
    brief["lineup_changes"] = []

    # The toss up pair is only known through lineup_changes, so with that
    # section emptied out, benching an optimal starter is no longer
    # carved out and should be flagged as a plain verdict conflict.
    draft = "Bench Dax Voss at quarterback this week."

    result = check_draft(draft, brief)

    assert result["ok"] is False
    assert any(v["kind"] == "verdict-conflict" for v in result["violations"])


def test_format_violations_is_one_line_per_violation():
    brief = _weekly_brief()
    draft = _read("draft_weekly_fail.md")

    result = check_draft(draft, brief)
    text = format_violations(result)

    lines = text.splitlines()
    assert len(lines) == len(result["violations"])
    assert all(line for line in lines)
