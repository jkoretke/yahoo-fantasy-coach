"""Tests for engine.config."""
from __future__ import annotations

from pathlib import Path

import pytest

from engine import config
from engine.common import EngineError


def test_example_file_loads_and_validates() -> None:
    result = config.load_league_config(config.LEAGUE_EXAMPLE_CONFIG_PATH)
    assert result["league"]["league_id"] == "nfl.l.123456"
    assert result["email"]["backend"] == "smtp"
    assert result["sources"]["sleeper"] is True


@pytest.mark.skipif(
    not config.LEAGUE_CONFIG_PATH.exists(),
    reason="config/league.yaml is gitignored and untracked; absent on a fresh checkout",
)
def test_shipped_league_yaml_loads_and_validates() -> None:
    result = config.load_league_config(config.LEAGUE_CONFIG_PATH)
    assert result["email"]["backend"] in config.EMAIL_BACKENDS
    assert isinstance(result["league"]["league_id"], str)


def test_partial_file_merges_over_defaults(tmp_path: Path) -> None:
    path = tmp_path / "partial.yaml"
    path.write_text("waiver:\n  day: monday\n", encoding="utf-8")

    result = config.load_league_config(path)

    assert result["waiver"]["day"] == "monday"
    assert result["waiver"]["time"] == config.DEFAULT_LEAGUE_CONFIG["waiver"]["time"]
    assert result["timezone"] == config.DEFAULT_LEAGUE_CONFIG["timezone"]
    assert result["claude"] == config.DEFAULT_LEAGUE_CONFIG["claude"]


def test_missing_explicit_path_raises(tmp_path: Path) -> None:
    with pytest.raises(EngineError):
        config.load_league_config(tmp_path / "no-such-file.yaml")


def test_no_args_falls_back_through_example_to_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing_league = tmp_path / "league.yaml"
    missing_example = tmp_path / "league.example.yaml"
    monkeypatch.setattr(config, "LEAGUE_CONFIG_PATH", missing_league)
    monkeypatch.setattr(config, "LEAGUE_EXAMPLE_CONFIG_PATH", missing_example)

    result = config.load_league_config()

    assert result == config.DEFAULT_LEAGUE_CONFIG


def test_no_args_uses_example_when_league_yaml_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing_league = tmp_path / "league.yaml"
    example_path = tmp_path / "league.example.yaml"
    example_path.write_text("waiver:\n  day: sunday\n", encoding="utf-8")
    monkeypatch.setattr(config, "LEAGUE_CONFIG_PATH", missing_league)
    monkeypatch.setattr(config, "LEAGUE_EXAMPLE_CONFIG_PATH", example_path)

    result = config.load_league_config()

    assert result["waiver"]["day"] == "sunday"


def test_no_args_prefers_league_yaml_over_example(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    league_path = tmp_path / "league.yaml"
    example_path = tmp_path / "league.example.yaml"
    league_path.write_text("waiver:\n  day: friday\n", encoding="utf-8")
    example_path.write_text("waiver:\n  day: sunday\n", encoding="utf-8")
    monkeypatch.setattr(config, "LEAGUE_CONFIG_PATH", league_path)
    monkeypatch.setattr(config, "LEAGUE_EXAMPLE_CONFIG_PATH", example_path)

    result = config.load_league_config()

    assert result["waiver"]["day"] == "friday"


def test_bad_email_backend_raises_naming_key(tmp_path: Path) -> None:
    path = tmp_path / "bad-backend.yaml"
    path.write_text("email:\n  backend: carrier-pigeon\n", encoding="utf-8")

    with pytest.raises(EngineError) as excinfo:
        config.load_league_config(path)
    assert "email.backend" in str(excinfo.value)


def test_negative_toss_up_margin_raises_naming_key(tmp_path: Path) -> None:
    path = tmp_path / "bad-margin.yaml"
    path.write_text("toss_up_margin_points: -1\n", encoding="utf-8")

    with pytest.raises(EngineError) as excinfo:
        config.load_league_config(path)
    assert "toss_up_margin_points" in str(excinfo.value)


def test_non_bool_source_raises_naming_key(tmp_path: Path) -> None:
    path = tmp_path / "bad-source.yaml"
    path.write_text("sources:\n  sleeper: yes-please\n", encoding="utf-8")

    with pytest.raises(EngineError) as excinfo:
        config.load_league_config(path)
    assert "sources.sleeper" in str(excinfo.value)


def test_unknown_source_key_raises_naming_key(tmp_path: Path) -> None:
    path = tmp_path / "unknown-source.yaml"
    path.write_text("sources:\n  espn: true\n", encoding="utf-8")

    with pytest.raises(EngineError) as excinfo:
        config.load_league_config(path)
    assert "sources.espn" in str(excinfo.value)


def test_source_enabled_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "sources.yaml"
    path.write_text("sources:\n  weather: false\n", encoding="utf-8")
    result = config.load_league_config(path)

    assert config.source_enabled(result, "weather") is False
    assert config.source_enabled(result, "sleeper") is True


def test_source_enabled_rejects_unknown_name() -> None:
    result = config.load_league_config(config.LEAGUE_EXAMPLE_CONFIG_PATH)
    with pytest.raises(EngineError):
        config.source_enabled(result, "espn")


def test_toss_up_margin_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "margin.yaml"
    path.write_text("toss_up_margin_points: 3.5\n", encoding="utf-8")
    result = config.load_league_config(path)

    assert config.toss_up_margin(result) == 3.5


def test_email_config_has_exactly_the_documented_keys() -> None:
    result = config.load_league_config(config.LEAGUE_EXAMPLE_CONFIG_PATH)
    email = config.email_config(result)

    assert set(email.keys()) == {"backend", "to", "from_email", "from_name", "curlrc"}


def test_claude_config_has_exactly_the_documented_keys() -> None:
    result = config.load_league_config(config.LEAGUE_EXAMPLE_CONFIG_PATH)
    claude = config.claude_config(result)

    assert set(claude.keys()) == {"binary", "timeout_seconds"}


@pytest.mark.parametrize(
    "path_attr",
    [
        pytest.param(
            "LEAGUE_CONFIG_PATH",
            marks=pytest.mark.skipif(
                not config.LEAGUE_CONFIG_PATH.exists(),
                reason="config/league.yaml is gitignored and untracked; absent on a fresh checkout",
            ),
        ),
        "LEAGUE_EXAMPLE_CONFIG_PATH",
    ],
)
def test_explicit_path_to_shipped_files_has_every_documented_key(
    path_attr: str,
) -> None:
    path = getattr(config, path_attr)
    result = config.load_league_config(path)

    assert set(result["league"].keys()) == {
        "league_id",
        "season",
        "game_id",
        "team_id",
    }
    assert set(result["waiver"].keys()) == {"day", "time"}
    assert set(result["email"].keys()) == {
        "backend",
        "to",
        "from_email",
        "from_name",
        "curlrc",
    }
    assert set(result["sources"].keys()) == set(config.SOURCE_NAMES)
    assert set(result["claude"].keys()) == {"binary", "timeout_seconds"}
    assert "timezone" in result
    assert "toss_up_margin_points" in result
