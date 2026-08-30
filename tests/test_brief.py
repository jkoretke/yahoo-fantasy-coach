"""Tests for engine.brief: assembling the run brief JSON and the offline
demo entry point. Every test reads the real fixture through
engine.fixtures.load_fixture_league() rather than inlining a duplicate
sample league, and every write goes through the pytest tmp_path fixture so
the suite never writes into the repo's own runs/ directory.
"""
from __future__ import annotations

import json

import pytest

from engine import brief
from engine import lineup
from engine import matchup
from engine import waivers
from engine.common import EngineError, REPO_ROOT, load_json, round_points
from engine.fixtures import DEFAULT_FIXTURE_DIR, load_fixture_league


@pytest.fixture()
def league():
    return load_fixture_league()


TOP_LEVEL_KEYS = {
    "generated_at",
    "routine",
    "league",
    "week",
    "team",
    "optimal_lineup",
    "current_lineup",
    "lineup_changes",
    "points_left_on_bench",
    "matchup",
    "waivers",
}


# ---------------------------------------------------------------------------
# build_brief: defaults and shape
# ---------------------------------------------------------------------------


def test_build_brief_defaults_to_owner_team_and_current_week(league):
    result = brief.build_brief(league)

    assert result["team"]["team_id"] == "t1"
    assert result["week"] == 3
    assert result["league"]["league_id"] == league["league_id"]
    assert result["league"]["name"] == league["name"]
    assert result["league"]["season"] == league["season"]
    assert result["league"]["num_teams"] == league["num_teams"]
    assert result["league"]["waiver_type"] == league["settings"]["waiver"]["type"]


def test_build_brief_has_every_top_level_key_and_is_json_serializable(league):
    result = brief.build_brief(league)

    assert set(result.keys()) == TOP_LEVEL_KEYS
    serialized = json.dumps(result)
    assert isinstance(serialized, str)


def test_build_brief_team_metadata(league):
    result = brief.build_brief(league, team_id="t2", week=3)

    assert result["team"]["team_id"] == "t2"
    assert result["team"]["name"] == "Sample Squad Two"
    assert result["team"]["manager"] == "Manager Two"


# ---------------------------------------------------------------------------
# build_brief: numbers come from one scoring pass, not recomputed
# ---------------------------------------------------------------------------


def test_optimal_lineup_total_matches_direct_call(league):
    result = brief.build_brief(league, team_id="t1", week=3)

    expected = lineup.optimal_lineup(league, "t1", 3)
    assert result["optimal_lineup"]["total_points"] == pytest.approx(
        expected["total_points"], abs=1e-6
    )


def test_matchup_margin_matches_direct_call(league):
    result = brief.build_brief(league, team_id="t1", week=3)

    expected = matchup.matchup_projection(league, "t1", 3)
    assert result["matchup"]["margin"] == pytest.approx(expected["margin"], abs=1e-6)


def test_waiver_targets_match_direct_call(league):
    result = brief.build_brief(league, team_id="t1", week=3)

    expected = waivers.rank_waiver_targets(league, "t1", 3)["targets"]
    actual = result["waivers"]["targets"]

    assert len(actual) == len(expected)
    for actual_target, expected_target in zip(actual, expected):
        assert actual_target["player_id"] == expected_target["player_id"]
        assert actual_target["points_gained"] == pytest.approx(
            expected_target["points_gained"], abs=1e-6
        )


# ---------------------------------------------------------------------------
# points_left_on_bench
# ---------------------------------------------------------------------------


def test_points_left_on_bench_is_positive_for_suboptimal_t1_lineup(league):
    result = brief.build_brief(league, team_id="t1", week=3)

    assert result["points_left_on_bench"] > 0


def test_points_left_on_bench_equals_optimal_minus_current(league):
    result = brief.build_brief(league, team_id="t1", week=3)

    expected = round_points(
        result["optimal_lineup"]["total_points"] - result["current_lineup"]["total_points"]
    )
    assert result["points_left_on_bench"] == pytest.approx(expected, abs=1e-6)


# ---------------------------------------------------------------------------
# invalid routine
# ---------------------------------------------------------------------------


def test_invalid_routine_raises(league):
    with pytest.raises(EngineError):
        brief.build_brief(league, routine="not_a_real_routine")


# ---------------------------------------------------------------------------
# brief_path
# ---------------------------------------------------------------------------


def test_brief_path_shape(league, tmp_path):
    result = brief.build_brief(league, team_id="t1", week=3, routine="weekly")

    path = brief.brief_path(result, tmp_path)

    assert path.parent.name == "wk03"
    assert path.parent.parent.name == str(league["season"])
    assert path.parent.parent.parent == tmp_path
    assert path.name.startswith("weekly-")
    assert path.suffix == ".json"
    assert ":" not in path.name


# ---------------------------------------------------------------------------
# write_brief
# ---------------------------------------------------------------------------


def test_write_brief_round_trips_and_touches_only_tmp_path(league, tmp_path):
    result = brief.build_brief(league, team_id="t1", week=3)

    repo_runs = REPO_ROOT / "runs"
    existed_before = repo_runs.exists()
    contents_before = set(repo_runs.iterdir()) if existed_before else set()

    path = brief.write_brief(result, tmp_path)

    assert path.exists()
    assert tmp_path in path.parents

    loaded = load_json(path)
    assert loaded == result

    if existed_before:
        assert set(repo_runs.iterdir()) == contents_before
    else:
        assert not repo_runs.exists()


# ---------------------------------------------------------------------------
# main: default JSON-to-stdout path
# ---------------------------------------------------------------------------


def test_main_default_prints_parseable_json(capsys):
    exit_code = brief.main(["--fixtures", str(DEFAULT_FIXTURE_DIR)])

    assert exit_code == 0
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert parsed["team"]["team_id"] == "t1"
    assert parsed["week"] == 3


def test_main_invalid_routine_reports_one_stderr_line_no_traceback(capsys):
    exit_code = brief.main(
        ["--fixtures", str(DEFAULT_FIXTURE_DIR), "--routine", "not_a_real_routine"]
    )

    assert exit_code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    stderr_lines = captured.err.strip("\n").split("\n")
    assert len(stderr_lines) == 1
    assert "Traceback" not in captured.err


# ---------------------------------------------------------------------------
# main: --write path
# ---------------------------------------------------------------------------


def test_main_write_prints_only_the_path_under_runs_root(capsys, tmp_path):
    exit_code = brief.main(
        [
            "--fixtures",
            str(DEFAULT_FIXTURE_DIR),
            "--write",
            "--runs-root",
            str(tmp_path),
        ]
    )

    assert exit_code == 0
    out = capsys.readouterr().out
    lines = out.strip("\n").split("\n")
    assert len(lines) == 1

    from pathlib import Path

    written_path = Path(lines[0])
    assert tmp_path in written_path.parents
    assert written_path.exists()

    loaded = load_json(written_path)
    assert loaded["team"]["team_id"] == "t1"


# ---------------------------------------------------------------------------
# main: --waiver-type and --week
# ---------------------------------------------------------------------------


def test_main_waiver_type_priority(capsys):
    exit_code = brief.main(
        ["--fixtures", str(DEFAULT_FIXTURE_DIR), "--waiver-type", "priority"]
    )

    assert exit_code == 0
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert parsed["waivers"]["waiver_type"] == "priority"
    assert parsed["league"]["waiver_type"] == "priority"


def test_main_week_4_succeeds(capsys):
    exit_code = brief.main(["--fixtures", str(DEFAULT_FIXTURE_DIR), "--week", "4"])

    assert exit_code == 0
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert parsed["week"] == 4
