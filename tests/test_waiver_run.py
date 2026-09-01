"""Tests for engine.waiver_run: the waiver wrapper's fixtures/dry-run path,
its STATUS-line contract, its EngineError-to-exit-0 mapping, and the
priority waiver-type render branch.

Every test here drives engine.waiver_run.main directly with --fixtures
--dry-run, so no test ever touches a live Yahoo/Sleeper/ESPN source.
tests/conftest.py's autouse block_real_network fixture already fails any
unpatched urllib.request.urlopen call; on top of that, every test below
also monkeypatches engine.run_common.run_claude and
engine.notify.send_email to raise if either is ever actually invoked, so
a future change to compose_email's prose default or deliver's dry-run
short circuit would fail these tests loudly instead of quietly spawning a
real subprocess or sending a real email.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import engine.brief
import engine.run_common as run_common
import engine.waiver_run as waiver_run
from engine.common import EngineError


def _refuse_run_claude(*args: object, **kwargs: object) -> tuple[int, str, str]:
    raise AssertionError("run_claude must never be invoked by a --fixtures run")


def _refuse_send_email(*args: object, **kwargs: object) -> bool:
    raise AssertionError("send_email must never be invoked by a --dry-run")


@pytest.fixture(autouse=True)
def _block_claude_and_email(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(run_common, "run_claude", _refuse_run_claude)
    monkeypatch.setattr(run_common.notify, "send_email", _refuse_send_email)


def test_fixtures_dry_run_exits_zero_and_ends_with_status_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = waiver_run.main(
        ["--fixtures", "--dry-run", "--runs-root", str(tmp_path)]
    )
    captured = capsys.readouterr()
    lines = [line for line in captured.out.splitlines() if line.strip()]

    assert exit_code == 0
    assert lines[-1] == "STATUS dry-run waiver"


def test_fixtures_dry_run_prints_email_body_and_writes_brief_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = waiver_run.main(
        ["--fixtures", "--dry-run", "--runs-root", str(tmp_path)]
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "[dry-run] would send:" in captured.out
    assert "Subject:" in captured.out

    written = list(tmp_path.rglob("waiver-*.json"))
    assert len(written) == 1
    on_disk = json.loads(written[0].read_text(encoding="utf-8"))
    assert on_disk["routine"] == "waiver"

    # "Dax Voss" is a known fixture player who is actually in the optimal
    # lineup: the waiver run wrapper builds a full brief the same as every
    # other routine, so the written artifact carries the real lineup even
    # though the waiver-routine email body itself only prints free agent
    # targets, never the lineup.
    lineup_names = [
        a["name"] for a in on_disk["optimal_lineup"]["assignments"] if a["player_id"]
    ]
    assert "Dax Voss" in lineup_names

    # The email body itself is checked against a real waiver target pulled
    # straight from that same brief, so this stays correct even if the
    # fixture's ranked targets change.
    first_target_name = on_disk["waivers"]["targets"][0]["name"]
    assert first_target_name in captured.out


def test_build_brief_engine_error_exits_one_with_failed_status(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _raise(*args: object, **kwargs: object) -> None:
        raise EngineError("boom: no such team")

    monkeypatch.setattr(engine.brief, "build_brief", _raise)

    exit_code = waiver_run.main(["--fixtures", "--dry-run"])
    captured = capsys.readouterr()
    lines = [line for line in captured.out.splitlines() if line.strip()]

    assert exit_code == 1
    # The STATUS payload is a short fixed token, not the free-text message
    # (see engine.run_common.error_status_token): the message itself still
    # reaches stderr, on the line printed immediately before STATUS.
    assert lines[-1] == "STATUS failed waiver engine-error"
    assert "boom: no such team" in captured.err


def test_fixtures_dry_run_with_priority_waiver_type_exercises_priority_branch(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = waiver_run.main(
        [
            "--fixtures",
            "--waiver-type",
            "priority",
            "--dry-run",
            "--runs-root",
            str(tmp_path),
        ]
    )
    captured = capsys.readouterr()
    lines = [line for line in captured.out.splitlines() if line.strip()]

    assert exit_code == 0
    assert lines[-1] == "STATUS dry-run waiver"
    assert "Waiver claims (priority)" in captured.out

    written = list(tmp_path.rglob("waiver-*.json"))
    assert len(written) == 1
    on_disk = json.loads(written[0].read_text(encoding="utf-8"))
    assert on_disk["league"]["waiver_type"] == "priority"
