"""Tests for engine.weekly_run: the weekly wrapper's fixtures/dry-run path,
its STATUS-line contract, and its EngineError-to-exit-0 mapping.

Every test here drives engine.weekly_run.main directly with --fixtures
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
import engine.weekly_run as weekly_run
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
    exit_code = weekly_run.main(
        ["--fixtures", "--dry-run", "--runs-root", str(tmp_path)]
    )
    captured = capsys.readouterr()
    lines = [line for line in captured.out.splitlines() if line.strip()]

    assert exit_code == 0
    assert lines[-1] == "STATUS dry-run weekly"


def test_fixtures_dry_run_prints_json_then_email_body(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = weekly_run.main(
        ["--fixtures", "--dry-run", "--runs-root", str(tmp_path)]
    )
    captured = capsys.readouterr()

    assert exit_code == 0

    decoder = json.JSONDecoder()
    stripped = captured.out.lstrip()
    assert stripped.startswith("{"), "brief JSON must be the first thing printed"
    brief, end = decoder.raw_decode(stripped)
    assert brief["routine"] == "weekly"

    rest = stripped[end:]
    assert "[dry-run] would send:" in rest
    assert "Subject:" in rest
    # A known fixture player who is actually in the optimal lineup, so this
    # also checks the email body carries real lineup content, not just a
    # subject line.
    assert "Dax Voss" in rest


def test_fixtures_dry_run_writes_brief_json_under_runs_root(tmp_path: Path) -> None:
    exit_code = weekly_run.main(
        ["--fixtures", "--dry-run", "--runs-root", str(tmp_path)]
    )
    assert exit_code == 0

    written = list(tmp_path.rglob("weekly-*.json"))
    assert len(written) == 1
    on_disk = json.loads(written[0].read_text(encoding="utf-8"))
    assert on_disk["routine"] == "weekly"


def test_build_brief_engine_error_still_exits_zero_with_failed_status(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _raise(*args: object, **kwargs: object) -> None:
        raise EngineError("boom: no such team")

    monkeypatch.setattr(engine.brief, "build_brief", _raise)

    exit_code = weekly_run.main(["--fixtures", "--dry-run"])
    captured = capsys.readouterr()
    lines = [line for line in captured.out.splitlines() if line.strip()]

    assert exit_code == 0
    assert lines[-1] == "STATUS failed weekly boom: no such team"
    assert "boom: no such team" in captured.err
