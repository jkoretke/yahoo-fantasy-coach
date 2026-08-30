"""Tests for engine.common: one test per public function."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from engine import common


def test_load_yaml_reads_mapping(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("name: coach\ncount: 3\n", encoding="utf-8")
    data = common.load_yaml(path)
    assert data == {"name": "coach", "count": 3}


def test_load_yaml_rejects_scalar(tmp_path: Path) -> None:
    path = tmp_path / "scalar.yaml"
    path.write_text("just a string\n", encoding="utf-8")
    with pytest.raises(common.EngineError):
        common.load_yaml(path)


def test_load_yaml_missing_file_raises(tmp_path: Path) -> None:
    path = tmp_path / "missing.yaml"
    with pytest.raises(common.EngineError):
        common.load_yaml(path)


def test_load_json_reads_object(tmp_path: Path) -> None:
    path = tmp_path / "data.json"
    path.write_text(json.dumps({"a": 1, "b": "two"}), encoding="utf-8")
    data = common.load_json(path)
    assert data == {"a": 1, "b": "two"}


def test_load_json_rejects_array(tmp_path: Path) -> None:
    path = tmp_path / "list.json"
    path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    with pytest.raises(common.EngineError):
        common.load_json(path)


def test_load_json_missing_file_raises(tmp_path: Path) -> None:
    path = tmp_path / "missing.json"
    with pytest.raises(common.EngineError):
        common.load_json(path)


def test_atomic_write_creates_parents_and_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "deeper" / "out.txt"
    assert not path.parent.exists()
    common.atomic_write(path, "hello world\n")
    assert path.parent.exists()
    assert path.read_text(encoding="utf-8") == "hello world\n"


def test_write_json_creates_parents_and_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "deeper" / "out.json"
    value = {"players": ["a", "b"], "week": 1}
    common.write_json(path, value)
    assert path.parent.exists()
    text = path.read_text(encoding="utf-8")
    assert text.endswith("\n")
    assert json.loads(text) == value


def test_load_secrets_parses_file(tmp_path: Path) -> None:
    path = tmp_path / "secrets.env"
    path.write_text(
        "# a comment\n"
        "\n"
        "PLAIN_KEY=plainvalue\n"
        "QUOTED_KEY=\"quoted value\"\n",
        encoding="utf-8",
    )
    values = common.load_secrets(path)
    assert values == {"PLAIN_KEY": "plainvalue", "QUOTED_KEY": "quoted value"}


def test_load_secrets_found_via_env_var(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "secrets.env"
    path.write_text("PLAIN_KEY=plainvalue\n", encoding="utf-8")
    monkeypatch.setenv(common.SECRETS_ENV_VAR, str(path))
    values = common.load_secrets()
    assert values == {"PLAIN_KEY": "plainvalue"}


def test_load_secrets_env_var_overrides_file_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "secrets.env"
    path.write_text("PLAIN_KEY=plainvalue\n", encoding="utf-8")
    monkeypatch.setenv("PLAIN_KEY", "envvalue")
    values = common.load_secrets(path)
    assert values == {"PLAIN_KEY": "envvalue"}


def test_load_secrets_missing_file_returns_empty(tmp_path: Path) -> None:
    path = tmp_path / "does-not-exist.env"
    assert common.load_secrets(path) == {}


def test_require_secret_returns_stripped_value() -> None:
    values = {"KEY": "  value  "}
    assert common.require_secret(values, "KEY") == "value"


def test_require_secret_raises_when_missing_or_blank() -> None:
    with pytest.raises(common.EngineError):
        common.require_secret({}, "MISSING")
    with pytest.raises(common.EngineError):
        common.require_secret({"BLANK": "   "}, "BLANK")


def test_timestamp_ends_with_z() -> None:
    result = common.timestamp()
    assert result.endswith("Z")
    aware = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    assert common.timestamp(aware).endswith("Z")


def test_timestamp_naive_raises() -> None:
    naive = datetime(2026, 1, 1, 12, 0, 0)
    with pytest.raises(common.EngineError):
        common.timestamp(naive)


def test_round_points() -> None:
    assert common.round_points(0.04 * 312) == 12.48
    assert common.round_points(1) == 1.0
