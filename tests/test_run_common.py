"""Tests for engine.run_common: the STATUS line contract, the claude subprocess
boundary, the prose gate fallback, and delivery.

Every test that could reach a real `claude` subprocess either monkeypatches
engine.run_common.run_claude directly, or drives compose_email down its plain
path (prose="plain" or fixtures=True), which never calls run_claude at all.
No test in this file ever invokes a real claude process, curl process, or
network call.
"""
from __future__ import annotations

import copy

import pytest

import engine.run_common as run_common
from engine.brief import ROUTINES, build_brief
from engine.common import EngineError
from engine.config import load_league_config
from engine.fixtures import load_fixture_league
from engine.run_common import (
    PROSE_MODES,
    apply_toss_up_margin,
    compose_email,
    deliver,
    error_status_token,
    find_status_line,
    load_prompt,
    print_status,
    status_line,
    strip_status_line,
)
from engine.trades import trade_ideas

_PROMPT_FILES = {
    "weekly": "prompts/weekly.md",
    "gameday": "prompts/gameday.md",
    "waiver": "prompts/waiver.md",
    "inactive": "prompts/inactive.md",
}


def _config():
    return load_league_config()


def _weekly_brief():
    return build_brief(load_fixture_league())


def _priority_waiver_brief():
    return build_brief(load_fixture_league(waiver_type="priority"))


# ---------------------------------------------------------------------------
# STATUS line contract
# ---------------------------------------------------------------------------


def test_status_line_builds_expected_string():
    assert status_line("skipped", "no-games") == "STATUS skipped no-games"
    assert status_line("ok") == "STATUS ok"


def test_find_status_line_returns_none_when_absent():
    assert find_status_line("just some regular output\nnothing machine readable here") is None


def test_find_status_line_picks_the_last_one():
    text = "some prose\nSTATUS first one\nmore prose\nSTATUS second one\n"
    assert find_status_line(text) == "second one"


def test_strip_status_line_removes_every_status_line():
    text = "line one\nSTATUS first\nline two\nSTATUS second\n"
    stripped = strip_status_line(text)
    assert "STATUS" not in stripped
    assert "line one" in stripped
    assert "line two" in stripped


def test_strip_status_line_trims_trailing_whitespace():
    text = "body text\nSTATUS ok\n\n"
    assert strip_status_line(text) == "body text"


def test_print_status_prints_expected_line(capsys):
    print_status("skipped", "no-games")
    captured = capsys.readouterr()
    assert captured.out.strip() == "STATUS skipped no-games"


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("routine", ROUTINES)
def test_load_prompt_returns_nonempty_text_for_every_routine(routine):
    text = load_prompt(routine)
    assert isinstance(text, str)
    assert text.strip() != ""
    assert "STATUS" in text
    assert "toss_up" in text


def test_prompt_path_rejects_unknown_routine():
    with pytest.raises(EngineError):
        run_common.prompt_path("not-a-routine")


# ---------------------------------------------------------------------------
# run_claude boundary: never invoked for real, only ever monkeypatched
# ---------------------------------------------------------------------------


def _failing_runner(*args, **kwargs):
    raise AssertionError("run_claude must never be invoked on this path")


def test_compose_email_prose_plain_never_calls_run_claude(monkeypatch):
    monkeypatch.setattr(run_common, "run_claude", _failing_runner)
    brief = _weekly_brief()

    subject, body, source = compose_email("weekly", brief, _config(), prose="plain")

    assert source == "plain"
    assert subject
    assert body


def test_compose_email_fixtures_true_never_calls_run_claude(monkeypatch):
    monkeypatch.setattr(run_common, "run_claude", _failing_runner)
    brief = _weekly_brief()

    # prose left at its default ("auto"): fixtures=True is what forces plain,
    # not an explicit prose value, per compose_email's own contract.
    subject, body, source = compose_email("weekly", brief, _config(), fixtures=True)

    assert source == "plain"
    assert subject
    assert body


def test_compose_email_explicit_claude_prose_ignores_fixtures_flag(monkeypatch):
    # fixtures=True must not silently override an explicit prose="claude":
    # only "auto" is sensitive to the fixtures flag.
    calls = []

    def _runner(prompt, **kwargs):
        calls.append(prompt)
        return 0, "Everything looks fine this week, nothing changes here.\nSTATUS ok", ""

    subject, body, source = compose_email(
        "weekly", _weekly_brief(), _config(), prose="claude", fixtures=True, runner=_runner
    )

    assert calls, "an explicit prose='claude' must still draft, even with fixtures=True"
    assert source == "claude"


# ---------------------------------------------------------------------------
# draft_body directly: the prompt it builds, not just compose_email's use of it
# ---------------------------------------------------------------------------


def test_draft_body_prompt_carries_prompt_text_brief_json_and_extra_context():
    seen = {}

    def _runner(prompt, **kwargs):
        seen["prompt"] = prompt
        return 0, "Some drafted prose right here.\nSTATUS ok", ""

    body, reason = run_common.draft_body(
        "weekly",
        _weekly_brief(),
        _config(),
        extra_context="NEWS: a beat writer note.",
        runner=_runner,
    )

    assert reason == "ok"
    assert body is not None
    assert "STATUS" not in body
    assert "```json" in seen["prompt"]
    assert '"lineup_changes"' in seen["prompt"]
    assert "NEWS: a beat writer note." in seen["prompt"]
    # The routine's own prompt file text opens the assembled prompt.
    assert seen["prompt"].startswith(load_prompt("weekly"))


def test_draft_body_returns_none_and_a_reason_on_nonzero_exit():
    def _runner(prompt, **kwargs):
        return 1, "", "claude blew up"

    body, reason = run_common.draft_body("weekly", _weekly_brief(), _config(), runner=_runner)

    assert body is None
    assert "1" in reason


def test_draft_body_returns_none_when_stdout_is_only_a_status_line():
    def _runner(prompt, **kwargs):
        return 0, "STATUS ok\n", ""

    body, reason = run_common.draft_body("weekly", _weekly_brief(), _config(), runner=_runner)

    assert body is None
    assert reason


# ---------------------------------------------------------------------------
# compose_email: claude path, acceptance and rejection
# ---------------------------------------------------------------------------


def test_compose_email_clean_draft_is_used_with_status_line_stripped():
    brief = _weekly_brief()

    def _clean_runner(prompt, **kwargs):
        stdout = (
            "This week looks stable and no major moves are needed right now.\n"
            "STATUS ok\n"
        )
        return 0, stdout, ""

    subject, body, source = compose_email("weekly", brief, _config(), runner=_clean_runner)

    assert source == "claude"
    assert "STATUS" not in body
    assert body.strip() != ""
    assert subject


def test_compose_email_falls_back_to_plain_on_unknown_player(capsys):
    brief = _weekly_brief()

    def _bad_runner(prompt, **kwargs):
        stdout = "Start Bogus McFakename this week.\nSTATUS ok\n"
        return 0, stdout, ""

    subject, body, source = compose_email("weekly", brief, _config(), runner=_bad_runner)

    assert source == "plain"
    assert body.strip() != ""
    assert subject
    # The rejection was logged for anyone reading the run's stderr.
    captured = capsys.readouterr()
    assert captured.err.strip() != ""


def test_compose_email_falls_back_to_plain_when_draft_body_returns_none(capsys):
    brief = _weekly_brief()

    def _crashing_runner(prompt, **kwargs):
        return 1, "", "boom"

    subject, body, source = compose_email("weekly", brief, _config(), runner=_crashing_runner)

    assert source == "plain"
    assert body.strip() != ""
    captured = capsys.readouterr()
    assert captured.err.strip() != ""


def test_compose_email_invalid_prose_mode_raises():
    with pytest.raises(EngineError):
        compose_email("weekly", _weekly_brief(), _config(), prose="not-a-mode")
    assert "not-a-mode" not in PROSE_MODES


# ---------------------------------------------------------------------------
# compose_email: trades and inactive_changes actually reach the claude path
# ---------------------------------------------------------------------------


def test_compose_email_forwards_trade_ideas_to_the_claude_prompt():
    # Regression guard: compose_email used to build the claude prompt from
    # brief alone, so weekly's trade ideas never reached the model even
    # though the plain fallback renders them. t2/week4 is the fixture's
    # own case with a real (non-empty) idea, per tests/test_email_render.py.
    league = load_fixture_league()
    brief = build_brief(league, team_id="t2", week=4)
    ideas = trade_ideas(league, "t2", 4)
    assert ideas["ideas"], "this test only proves anything with a real idea in it"
    first_idea = ideas["ideas"][0]

    seen = {}

    def _runner(prompt, **kwargs):
        seen["prompt"] = prompt
        return 0, "This week looks stable, nothing else to add here.\nSTATUS ok", ""

    compose_email("weekly", brief, _config(), trades=ideas, runner=_runner)

    assert "TRADE IDEAS" in seen["prompt"]
    assert first_idea["send"]["name"] in seen["prompt"]
    assert first_idea["receive"]["name"] in seen["prompt"]


def test_compose_email_claude_draft_naming_a_trade_partner_player_is_accepted():
    # A trade partner's player is not named anywhere else in brief, so
    # without prose_gate.brief_player_names also reading brief["trades"],
    # a draft that follows the prompt's own instruction to mention a
    # trade idea would always be rejected as an unknown player.
    league = load_fixture_league()
    brief = build_brief(league, team_id="t2", week=4)
    ideas = trade_ideas(league, "t2", 4)
    first_idea = ideas["ideas"][0]
    send_name = first_idea["send"]["name"]
    receive_name = first_idea["receive"]["name"]

    def _runner(prompt, **kwargs):
        stdout = (
            f"Consider sending {send_name} to {first_idea['partner_team_name']} "
            f"for {receive_name} this week.\nSTATUS ok\n"
        )
        return 0, stdout, ""

    subject, body, source = compose_email(
        "weekly", brief, _config(), trades=ideas, runner=_runner
    )

    assert source == "claude"
    assert send_name in body
    assert receive_name in body


def test_compose_email_forwards_inactive_changes_to_the_claude_prompt():
    brief = _weekly_brief()
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
        }
    ]

    seen = {}

    def _runner(prompt, **kwargs):
        seen["prompt"] = prompt
        return 0, "Brix Duskin is out and Trace Winslow steps in this week.\nSTATUS ok", ""

    compose_email("inactive", brief, _config(), inactive_changes=changes, runner=_runner)

    assert "INACTIVE CHANGES" in seen["prompt"]
    assert "Brix Duskin" in seen["prompt"]
    assert "Trace Winslow" in seen["prompt"]


def test_compose_email_with_neither_trades_nor_inactive_changes_omits_extra_context():
    seen = {}

    def _runner(prompt, **kwargs):
        seen["prompt"] = prompt
        return 0, "Everything looks fine this week, nothing changes here.\nSTATUS ok", ""

    compose_email("weekly", _weekly_brief(), _config(), runner=_runner)

    assert "TRADE IDEAS" not in seen["prompt"]
    assert "INACTIVE CHANGES" not in seen["prompt"]


# ---------------------------------------------------------------------------
# error_status_token: a fixed machine-readable slug, not free-text
# ---------------------------------------------------------------------------


def test_error_status_token_slugs_the_error_class_name():
    assert error_status_token(EngineError("unknown team_id: nope")) == "engine-error"


def test_error_status_token_never_carries_the_message_text():
    token = error_status_token(EngineError("multi word message: with a colon"))
    assert " " not in token
    assert ":" not in token


# ---------------------------------------------------------------------------
# deliver: dry-run prints and touches nothing; real send calls send_email once
# ---------------------------------------------------------------------------


def test_deliver_dry_run_prints_and_never_sends_or_loads_secrets(monkeypatch, capsys):
    def _send_email_must_not_run(*args, **kwargs):
        raise AssertionError("send_email must not be called on the dry-run path")

    def _load_secrets_must_not_run(*args, **kwargs):
        raise AssertionError("load_secrets must not be called on the dry-run path")

    monkeypatch.setattr(run_common.notify, "send_email", _send_email_must_not_run)
    monkeypatch.setattr(run_common.notify, "load_secrets", _load_secrets_must_not_run)

    result = deliver("Test subject", "Test body", _config(), dry_run=True)

    captured = capsys.readouterr()
    assert "Test subject" in captured.out
    assert "Test body" in captured.out
    assert result is True


def test_deliver_real_run_calls_send_email_exactly_once(monkeypatch):
    calls = []

    def _fake_send_email(subject, body, *, config=None, email=None, secrets=None):
        calls.append((subject, body))
        return True

    monkeypatch.setattr(run_common.notify, "send_email", _fake_send_email)

    result = deliver("Real subject", "Real body", _config(), dry_run=False, secrets={})

    assert len(calls) == 1
    assert calls[0] == ("Real subject", "Real body")
    assert result is True


def test_deliver_real_run_returns_false_when_send_email_fails(monkeypatch):
    monkeypatch.setattr(run_common.notify, "send_email", lambda *a, **k: False)

    result = deliver("Subject", "Body", _config(), dry_run=False)

    assert result is False


# ---------------------------------------------------------------------------
# apply_toss_up_margin: the mandatory drift guard, for both waiver branches
# ---------------------------------------------------------------------------


def test_apply_toss_up_margin_is_a_noop_on_default_faab_brief():
    brief = _weekly_brief()
    before = copy.deepcopy(brief)

    result = apply_toss_up_margin(brief, 2.0)

    assert result == before
    assert brief == before


def test_apply_toss_up_margin_is_a_noop_on_default_priority_brief():
    brief = _priority_waiver_brief()
    before = copy.deepcopy(brief)

    result = apply_toss_up_margin(brief, 2.0)

    assert result == before
    assert brief == before


def test_apply_toss_up_margin_faab_branch_does_not_raise_keyerror():
    brief = _weekly_brief()
    assert brief["waivers"]["waiver_type"] == "faab"
    assert "required_gain" not in brief["waivers"]

    # Would raise KeyError if apply_toss_up_margin ever read required_gain
    # on the faab branch.
    apply_toss_up_margin(brief, 5.0)


def test_apply_toss_up_margin_narrower_margin_strips_existing_toss_up_keys():
    brief = _priority_waiver_brief()
    # At the module default (2.0) the fixture already flags three targets
    # (closest is 1.8 points from a 3.5 required gain); a much narrower
    # margin must actively remove all three tags, not just leave them be.
    assert any(t.get("toss_up") for t in brief["waivers"]["targets"])

    apply_toss_up_margin(brief, 0.01)

    for target in brief["waivers"]["targets"]:
        assert "toss_up" not in target
        assert "toss_up_margin" not in target
        assert "toss_up_options" not in target


def test_apply_toss_up_margin_changes_banding_at_a_different_margin():
    brief = _priority_waiver_brief()

    apply_toss_up_margin(brief, 100.0)

    # A margin this wide bands every target against required_gain.
    toss_ups = [t for t in brief["waivers"]["targets"] if t.get("toss_up")]
    assert len(toss_ups) == len(brief["waivers"]["targets"])
    for target in toss_ups:
        assert target["toss_up_options"] == ["claim", "skip"]
        assert target["toss_up_margin"] == 100.0


# ---------------------------------------------------------------------------
# No em dashes anywhere in the files this chunk owns
# ---------------------------------------------------------------------------


_EM_DASH = "\u2014"  # written as an escape, not a literal, so this file itself
# never contains the character the repo's own em dash grep scans for.


def test_no_em_dash_in_run_common_source():
    text = (run_common.REPO_ROOT / "engine" / "run_common.py").read_text(encoding="utf-8")
    assert _EM_DASH not in text


@pytest.mark.parametrize("relative_path", sorted(_PROMPT_FILES.values()))
def test_no_em_dash_in_prompt_files(relative_path):
    text = (run_common.REPO_ROOT / relative_path).read_text(encoding="utf-8")
    assert _EM_DASH not in text


# ---------------------------------------------------------------------------
# exit_code: which STATUS outcomes mean the run failed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("outcome", ["emailed", "dry-run", "skipped"])
def test_exit_code_treats_every_non_failed_outcome_as_success(outcome):
    assert run_common.exit_code(outcome) == 0


def test_exit_code_failed_is_one():
    assert run_common.exit_code(run_common.FAILED_OUTCOME) == 1
    assert run_common.FAILED_OUTCOME == "failed"


def test_skipped_is_a_success_so_a_quiet_sunday_raises_no_alert():
    # inactive_run prints "STATUS skipped not-yet" on nearly every one of
    # its five-minute fires, and gameday_run prints "STATUS skipped
    # no-games" on every day none of the owner's players play. A non-zero
    # exit on either would mean an OnFailure alert every five minutes.
    assert run_common.exit_code("skipped") == 0


def test_print_status_returns_the_matching_exit_code(capsys):
    assert run_common.print_status("emailed", "weekly") == 0
    assert run_common.print_status("failed", "weekly", "engine-error") == 1
    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert lines == ["STATUS emailed weekly", "STATUS failed weekly engine-error"]


# ---------------------------------------------------------------------------
# resolve_week: which NFL week a live run is for
# ---------------------------------------------------------------------------


def _week_config(season=2026):
    config = load_league_config()
    config["league"]["season"] = season
    return config


def _current_week_envelope(*, season=2026, week=3, season_type=2, available=True):
    if not available:
        return {
            "source": "schedule", "available": False, "stale": False,
            "reason": "espn unreachable", "fetched_at": None, "data": None,
        }
    return {
        "source": "schedule", "available": True, "stale": False, "reason": None,
        "fetched_at": "2026-09-23T12:00:00Z",
        "data": {
            "season": season, "week": week, "season_type": season_type,
            "source_url": "https://example.invalid/scoreboard",
        },
    }


def test_resolve_week_explicit_wins_and_never_calls_the_network(monkeypatch):
    def _never(*args, **kwargs):
        raise AssertionError("fetch_current_week must not be called for an explicit week")

    monkeypatch.setattr(run_common.schedule_source, "fetch_current_week", _never)
    assert run_common.resolve_week(7, _week_config()) == 7


def test_resolve_week_reads_espns_current_week(monkeypatch):
    monkeypatch.setattr(
        run_common.schedule_source,
        "fetch_current_week",
        lambda **kwargs: _current_week_envelope(week=3),
    )
    assert run_common.resolve_week(None, _week_config()) == 3


def test_resolve_week_preseason_resolves_to_week_one(monkeypatch):
    monkeypatch.setattr(
        run_common.schedule_source,
        "fetch_current_week",
        lambda **kwargs: _current_week_envelope(week=4, season_type=1),
    )
    # Preseason week 4 is not fantasy week 4. Week 1 is the next real week.
    assert run_common.resolve_week(None, _week_config()) == 1


def test_resolve_week_postseason_raises_rather_than_taking_week_one(monkeypatch):
    # ESPN restarts week numbering at 1 for the wild card round, so taking
    # that number would silently run January against the regular season's
    # week 1. This is the failure this branch exists to prevent.
    monkeypatch.setattr(
        run_common.schedule_source,
        "fetch_current_week",
        lambda **kwargs: _current_week_envelope(week=1, season_type=3),
    )
    with pytest.raises(EngineError) as excinfo:
        run_common.resolve_week(None, _week_config())
    assert "postseason" in str(excinfo.value)


def test_resolve_week_season_mismatch_raises(monkeypatch):
    monkeypatch.setattr(
        run_common.schedule_source,
        "fetch_current_week",
        lambda **kwargs: _current_week_envelope(season=2027, week=1),
    )
    with pytest.raises(EngineError) as excinfo:
        run_common.resolve_week(None, _week_config(season=2026))
    assert "2027" in str(excinfo.value)


def test_resolve_week_unavailable_source_raises(monkeypatch):
    monkeypatch.setattr(
        run_common.schedule_source,
        "fetch_current_week",
        lambda **kwargs: _current_week_envelope(available=False),
    )
    with pytest.raises(EngineError) as excinfo:
        run_common.resolve_week(None, _week_config())
    assert "--week" in str(excinfo.value)


@pytest.mark.parametrize("season_type", [0, 4, "2"])
def test_resolve_week_unknown_season_type_raises(monkeypatch, season_type):
    monkeypatch.setattr(
        run_common.schedule_source,
        "fetch_current_week",
        lambda **kwargs: _current_week_envelope(season_type=season_type),
    )
    with pytest.raises(EngineError):
        run_common.resolve_week(None, _week_config())


def test_resolve_week_zero_week_raises(monkeypatch):
    monkeypatch.setattr(
        run_common.schedule_source,
        "fetch_current_week",
        lambda **kwargs: _current_week_envelope(week=0),
    )
    with pytest.raises(EngineError):
        run_common.resolve_week(None, _week_config())
