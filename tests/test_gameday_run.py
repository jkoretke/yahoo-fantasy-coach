"""Tests for engine.gameday_run: the every-morning "does today matter"
wrapper.

Every test drives main() directly with --fixtures --dry-run, so nothing
here reaches a real claude subprocess, a real network call, or a real
email send. engine.run_common.run_claude and engine.notify.send_email are
both monkeypatched to fail the test outright if called, since a --fixtures
--dry-run run resolves prose to "plain" and delivery to a printed preview
and must never reach either.
"""
from __future__ import annotations

import pytest

import engine.notify as notify
import engine.run_common as run_common
from engine.brief import build_brief
from engine.fixtures import load_fixture_league
from engine.gameday_run import main


def _must_not_run_claude(*args, **kwargs):
    raise AssertionError("engine.run_common.run_claude must never be called by this test")


def _must_not_send_email(*args, **kwargs):
    raise AssertionError("engine.notify.send_email must never be called by this test")


@pytest.fixture(autouse=True)
def _block_claude_and_email(monkeypatch):
    monkeypatch.setattr(run_common, "run_claude", _must_not_run_claude)
    monkeypatch.setattr(notify, "send_email", _must_not_send_email)


def test_no_games_on_date_skips_silently_and_exits_zero(tmp_path, capsys):
    exit_code = main(
        [
            "--fixtures",
            "--dry-run",
            "--runs-root",
            str(tmp_path),
            "--date",
            "2026-09-13",
        ]
    )

    assert exit_code == 0
    out = capsys.readouterr().out
    lines = [line for line in out.splitlines() if line.strip()]
    assert lines[-1] == "STATUS skipped no-games"
    assert "[dry-run] would send:" not in out
    assert "Subject:" not in out


def test_game_on_date_prints_full_self_contained_lineup(tmp_path, capsys):
    exit_code = main(
        [
            "--fixtures",
            "--dry-run",
            "--runs-root",
            str(tmp_path),
            "--date",
            "2026-09-14",
        ]
    )

    assert exit_code == 0
    out = capsys.readouterr().out
    lines = [line for line in out.splitlines() if line.strip()]
    assert lines[-1] == "STATUS dry-run gameday"
    assert "[dry-run] would send:" in out

    league = load_fixture_league()
    brief = build_brief(league)
    for assignment in brief["optimal_lineup"]["assignments"]:
        if assignment["name"] is not None:
            assert assignment["name"] in out
