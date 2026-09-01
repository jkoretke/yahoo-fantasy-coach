"""Tests for engine.inactive_run: the 75-minutes-before-kickoff wrapper.

Every test drives main() directly with --fixtures --dry-run, so nothing
here reaches a real claude subprocess, a real network call, or a real
email send. engine.run_common.run_claude and engine.notify.send_email are
both monkeypatched to fail the test outright if called, since a --fixtures
--dry-run run resolves prose to "plain" and delivery to a printed preview
and must never reach either.

The fixture owner team's earliest starter kickoff (fixtures/phase4/
schedule.json) is 2026-09-14T13:00:00Z, so the default 75 minute window
opens at 2026-09-14T11:45Z and 2026-09-14T11:29Z sits just outside it.
"""
from __future__ import annotations

import pytest

import engine.notify as notify
import engine.run_common as run_common
from engine.inactive_run import main


def _must_not_run_claude(*args, **kwargs):
    raise AssertionError("engine.run_common.run_claude must never be called by this test")


def _must_not_send_email(*args, **kwargs):
    raise AssertionError("engine.notify.send_email must never be called by this test")


@pytest.fixture(autouse=True)
def _block_claude_and_email(monkeypatch):
    monkeypatch.setattr(run_common, "run_claude", _must_not_run_claude)
    monkeypatch.setattr(notify, "send_email", _must_not_send_email)


def test_outside_window_is_silent(tmp_path, capsys):
    exit_code = main(
        [
            "--fixtures",
            "--dry-run",
            "--runs-root",
            str(tmp_path),
            "--now",
            "2026-09-14T11:29Z",
        ]
    )

    assert exit_code == 0
    out = capsys.readouterr().out
    lines = [line for line in out.splitlines() if line.strip()]
    assert lines[-1] == "STATUS skipped not-yet"
    assert "[dry-run] would send:" not in out


def test_inside_window_emails_the_ruled_out_starter(tmp_path, capsys):
    exit_code = main(
        [
            "--fixtures",
            "--dry-run",
            "--runs-root",
            str(tmp_path),
            "--now",
            "2026-09-14T11:45Z",
        ]
    )

    assert exit_code == 0
    out = capsys.readouterr().out
    lines = [line for line in out.splitlines() if line.strip()]
    assert lines[-1] == "STATUS dry-run inactive"
    assert "[dry-run] would send:" in out
    assert "Brix Duskin" in out

    sent_files = list(tmp_path.rglob("inactive-*.sent"))
    assert len(sent_files) == 1

    brief_files = list(tmp_path.rglob("inactive-*.json"))
    assert len(brief_files) == 1


def test_failed_real_send_does_not_write_sent_and_the_alert_is_retried(
    tmp_path, monkeypatch, capsys
):
    # Regression guard for a QA finding: a real (non-dry-run) send that
    # engine.notify.send_email reports as failed must NOT write the
    # .sent suppression file, or a transient Brevo/SMTP failure would
    # permanently suppress the one alert this routine exists to deliver.
    # --fixtures with no --dry-run resolves prose to "plain" (see
    # engine.run_common.compose_email), so this still reaches
    # engine.run_common.deliver's real (non-dry-run) branch, which calls
    # engine.notify.send_email exactly once, without spawning claude or
    # touching the network, since send_email itself is monkeypatched.
    monkeypatch.setattr(notify, "send_email", lambda *args, **kwargs: False)

    first_exit = main(
        [
            "--fixtures",
            "--runs-root",
            str(tmp_path),
            "--now",
            "2026-09-14T11:45Z",
        ]
    )

    assert first_exit == 0
    out = capsys.readouterr().out
    lines = [line for line in out.splitlines() if line.strip()]
    assert lines[-1] == "STATUS failed inactive email-send-failed"

    sent_files = list(tmp_path.rglob("inactive-*.sent"))
    assert not sent_files, "a failed real send must not write the .sent file"

    # A second run, with send_email now succeeding, must still see (and
    # actually send) the same alert: nothing was lost by the failure above.
    seen_bodies: list[str] = []
    monkeypatch.setattr(
        notify,
        "send_email",
        lambda subject, body, **kwargs: seen_bodies.append(body) or True,
    )

    second_exit = main(
        [
            "--fixtures",
            "--runs-root",
            str(tmp_path),
            "--now",
            "2026-09-14T11:45Z",
        ]
    )

    assert second_exit == 0
    out = capsys.readouterr().out
    lines = [line for line in out.splitlines() if line.strip()]
    assert lines[-1] == "STATUS emailed inactive"
    assert len(seen_bodies) == 1
    assert "Brix Duskin" in seen_bodies[0]

    sent_files = list(tmp_path.rglob("inactive-*.sent"))
    assert len(sent_files) == 1, "a successful real send must write the .sent file"


def test_second_run_same_window_is_silent(tmp_path, capsys):
    first_exit = main(
        [
            "--fixtures",
            "--dry-run",
            "--runs-root",
            str(tmp_path),
            "--now",
            "2026-09-14T11:45Z",
        ]
    )
    assert first_exit == 0
    capsys.readouterr()  # discard first run's output

    second_exit = main(
        [
            "--fixtures",
            "--dry-run",
            "--runs-root",
            str(tmp_path),
            "--now",
            "2026-09-14T11:45Z",
        ]
    )

    assert second_exit == 0
    out = capsys.readouterr().out
    lines = [line for line in out.splitlines() if line.strip()]
    assert lines == ["STATUS skipped no-change"]
    assert "[dry-run] would send:" not in out
