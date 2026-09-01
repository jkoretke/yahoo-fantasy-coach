"""Tests for engine.weekly_run: the weekly wrapper's fixtures/dry-run path,
its STATUS-line contract, its EngineError-to-exit-1 mapping, and how it
resolves the week it runs for.

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


def test_build_brief_engine_error_exits_one_with_failed_status(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _raise(*args: object, **kwargs: object) -> None:
        raise EngineError("boom: no such team")

    monkeypatch.setattr(engine.brief, "build_brief", _raise)

    exit_code = weekly_run.main(["--fixtures", "--dry-run"])
    captured = capsys.readouterr()
    lines = [line for line in captured.out.splitlines() if line.strip()]

    assert exit_code == 1
    # The STATUS payload is a short fixed token, not the free-text message
    # (see engine.run_common.error_status_token): the message itself still
    # reaches stderr, on the line printed immediately before STATUS.
    assert lines[-1] == "STATUS failed weekly engine-error"
    assert "boom: no such team" in captured.err


# ---------------------------------------------------------------------------
# Which week the wrapper runs for
# ---------------------------------------------------------------------------


def test_fixtures_run_never_asks_espn_which_week_it_is(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The four README --fixtures commands promise zero network and zero
    # credentials. The week for a fixtures run comes from the fixture
    # league's own current_week, never from resolve_week.
    def _never(*args: object, **kwargs: object) -> None:
        raise AssertionError("a --fixtures run must never resolve the week over the network")

    monkeypatch.setattr(run_common.schedule_source, "fetch_current_week", _never)

    assert weekly_run.main(["--fixtures", "--dry-run", "--runs-root", str(tmp_path)]) == 0


def test_live_run_without_week_resolves_it_and_passes_it_downstream(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A live run with no --week no longer fails: it resolves the week and
    # hands that same number to build_live_league and build_brief. This is
    # the gap that kept the box's systemd timers disabled.
    from engine.fixtures import load_fixture_league

    monkeypatch.setattr(
        run_common.schedule_source,
        "fetch_current_week",
        lambda **kwargs: {
            "source": "schedule", "available": True, "stale": False, "reason": None,
            "fetched_at": "2026-09-23T12:00:00Z",
            "data": {"season": 2026, "week": 3, "season_type": 2, "source_url": "x"},
        },
    )

    seen: dict[str, object] = {}
    fixture_league = load_fixture_league()

    def _build_live(**kwargs: object) -> dict:
        seen["live_week"] = kwargs["week"]
        return fixture_league

    monkeypatch.setattr(weekly_run, "build_live_league", _build_live)

    # A live run does run the news pass, which is the one thing in this
    # wrapper that spawns claude outside the prose step. Patch the source,
    # not the runner, so this test proves the wiring without a subprocess.
    monkeypatch.setattr(
        weekly_run.news_source,
        "fetch_news",
        lambda *args, **kwargs: {
            "source": "news", "available": True, "stale": False, "reason": None,
            "fetched_at": "2026-09-23T12:00:00Z",
            "data": {"players": [], "items": [], "count": 0},
        },
    )

    real_build_brief = engine.brief.build_brief

    def _build_brief(league, team_id, week, routine):
        seen["brief_week"] = week
        return real_build_brief(league, team_id, week, routine)

    monkeypatch.setattr(engine.brief, "build_brief", _build_brief)

    exit_code = weekly_run.main(["--dry-run", "--prose", "plain", "--runs-root", str(tmp_path)])

    assert exit_code == 0
    assert seen["live_week"] == 3
    assert seen["brief_week"] == 3
    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert lines[-1] == "STATUS dry-run weekly"


def test_live_run_reports_failed_when_the_week_cannot_be_resolved(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        run_common.schedule_source,
        "fetch_current_week",
        lambda **kwargs: {
            "source": "schedule", "available": False, "stale": False,
            "reason": "espn unreachable", "fetched_at": None, "data": None,
        },
    )

    exit_code = weekly_run.main(["--dry-run", "--runs-root", str(tmp_path)])

    assert exit_code == 1
    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert lines[-1] == "STATUS failed weekly engine-error"
