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
    find_status_line,
    load_prompt,
    print_status,
    status_line,
    strip_status_line,
)

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
