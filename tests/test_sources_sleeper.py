"""Tests for engine.sources.sleeper: projections, the player index, and
trending adds/drops.

Fixtures are loaded from fixtures/sources/ with json.loads rather than
engine.common.load_json, since sleeper_projections.json and
sleeper_trending_add.json are top level JSON lists and load_json requires
a top level object.

Two mocking conventions are used, matching which layer is under test:
  - FETCH PATH tests prove the url, the cache and the failure handling by
    patching urllib.request.urlopen with a fake response object.
  - PARSE PATH tests prove the shape mapping by patching
    engine.sources.sleeper.fetch_cached_json directly, bypassing the
    cache and HTTP layers entirely.

Every test that touches disk passes cache_root=tmp_path, so the suite
never writes into the repo's own runs/cache/ directory.
"""
from __future__ import annotations

import json
import urllib.error
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from engine.common import EngineError, REPO_ROOT
from engine.sources import sleeper


FIXTURES_DIR = REPO_ROOT / "fixtures" / "sources"


def _load_fixture(name: str) -> Any:
    return json.loads((FIXTURES_DIR / name).read_bytes())


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


def _fake_body(payload: Any) -> bytes:
    return json.dumps(payload).encode("utf-8")


# ---------------------------------------------------------------------------
# fetch_projections: parse path
# ---------------------------------------------------------------------------


@patch("engine.sources.sleeper.fetch_cached_json")
def test_fetch_projections_parses_primary_shape(mock_fetch, tmp_path: Path) -> None:
    payload = _load_fixture("sleeper_projections.json")
    mock_fetch.return_value = (payload, "2026-09-10T12:00:00Z", False)

    result = sleeper.fetch_projections(2025, 4, cache_root=tmp_path)

    assert result["available"] is True
    assert result["stale"] is False
    assert result["source"] == sleeper.SOURCE_NAME
    data = result["data"]
    assert data["season"] == 2025
    assert data["week"] == 4
    assert data["count"] == 4

    allen = data["projections"]["4984"]
    assert allen["name"] == "Josh Allen"
    assert allen["normalized_name"] == "josh allen"
    assert allen["position"] == "QB"
    assert allen["nfl_team"] == "BUF"
    assert allen["opponent"] == "NO"
    assert allen["injury_status"] == ""
    assert allen["projected_points"] == 25.24
    assert allen["stats"]["pass_yd"] == 247.03
    assert allen["stats"]["pass_td"] == 1.72

    adp_only = data["projections"]["11604"]
    assert adp_only["projected_points"] == 0.0
    assert "pts_ppr" not in adp_only["stats"]

    null_team_entry = data["projections"]["4995"]
    assert null_team_entry["nfl_team"] == ""
    assert null_team_entry["injury_status"] == "O"
    assert null_team_entry["opponent"] == "HOU"


@patch("engine.sources.sleeper.fetch_cached_json")
def test_fetch_projections_parses_legacy_dict_shape(mock_fetch, tmp_path: Path) -> None:
    legacy_payload = {
        "4984": {"adp_dd_ppr": 11.0, "pts_ppr": 25.24, "pass_yd": 247.03},
        "6462": {"adp_dd_ppr": 1000.0},
    }
    mock_fetch.return_value = (legacy_payload, "2026-09-10T12:00:00Z", False)

    result = sleeper.fetch_projections(2025, 4, cache_root=tmp_path, use_legacy_url=True)

    assert result["available"] is True
    data = result["data"]
    assert data["count"] == 2

    allen = data["projections"]["4984"]
    assert allen["projected_points"] == 25.24
    assert allen["name"] == ""
    assert allen["normalized_name"] == ""
    assert allen["position"] == ""
    assert allen["stats"]["pass_yd"] == 247.03

    adp_only = data["projections"]["6462"]
    assert adp_only["projected_points"] == 0.0


@patch("engine.sources.sleeper.fetch_cached_json")
def test_fetch_projections_enabled_false_skips_fetch(mock_fetch, tmp_path: Path) -> None:
    result = sleeper.fetch_projections(2025, 4, enabled=False, cache_root=tmp_path)

    assert result["available"] is False
    assert result["reason"] == "disabled"
    assert result["data"] is None
    mock_fetch.assert_not_called()


def test_fetch_projections_envelope_keys_and_json_serializable(tmp_path: Path) -> None:
    with patch("engine.sources.sleeper.fetch_cached_json") as mock_fetch:
        mock_fetch.return_value = (
            _load_fixture("sleeper_projections.json"),
            "2026-09-10T12:00:00Z",
            False,
        )
        result = sleeper.fetch_projections(2025, 4, cache_root=tmp_path)

    assert tuple(result.keys()) == ("source", "available", "stale", "reason", "fetched_at", "data")
    assert json.dumps(result)


# ---------------------------------------------------------------------------
# fetch_projections: fetch path (url, cache key, use_legacy_url)
# ---------------------------------------------------------------------------


@patch("urllib.request.urlopen")
def test_fetch_projections_uses_primary_url_and_cache_key(mock_urlopen, tmp_path: Path) -> None:
    mock_urlopen.return_value = _FakeResponse(_fake_body(_load_fixture("sleeper_projections.json")))

    sleeper.fetch_projections(2025, 4, cache_root=tmp_path)

    request = mock_urlopen.call_args.args[0]
    assert request.full_url == sleeper.PROJECTIONS_URL_TEMPLATE.format(season=2025, week=4)
    assert (tmp_path / "sleeper-projections-2025-wk04.json").exists()


@patch("urllib.request.urlopen")
def test_fetch_projections_use_legacy_url_hits_legacy_endpoint_and_cache_key(
    mock_urlopen, tmp_path: Path
) -> None:
    legacy_payload = {"4984": {"pts_ppr": 25.24}}
    mock_urlopen.return_value = _FakeResponse(_fake_body(legacy_payload))

    result = sleeper.fetch_projections(2025, 4, cache_root=tmp_path, use_legacy_url=True)

    request = mock_urlopen.call_args.args[0]
    assert request.full_url == sleeper.LEGACY_PROJECTIONS_URL_TEMPLATE.format(season=2025, week=4)
    assert (tmp_path / "sleeper-projections-legacy-2025-wk04.json").exists()
    assert result["data"]["source_url"] == sleeper.LEGACY_PROJECTIONS_URL_TEMPLATE.format(
        season=2025, week=4
    )


@patch("urllib.request.urlopen")
def test_fetch_projections_warm_cache_makes_no_additional_calls(mock_urlopen, tmp_path: Path) -> None:
    mock_urlopen.return_value = _FakeResponse(_fake_body(_load_fixture("sleeper_projections.json")))

    sleeper.fetch_projections(2025, 4, cache_root=tmp_path)
    assert mock_urlopen.call_count == 1

    sleeper.fetch_projections(2025, 4, cache_root=tmp_path)
    assert mock_urlopen.call_count == 1


# ---------------------------------------------------------------------------
# fetch_projections: degradation modes
# ---------------------------------------------------------------------------


@patch("urllib.request.urlopen")
def test_fetch_projections_http_error_degrades(mock_urlopen, tmp_path: Path) -> None:
    mock_urlopen.side_effect = urllib.error.HTTPError(
        "https://api.sleeper.app", 503, "Service Unavailable", hdrs=None, fp=None
    )

    result = sleeper.fetch_projections(2025, 4, cache_root=tmp_path)

    assert result["available"] is False
    assert result["reason"]
    assert result["data"] is None


@patch("urllib.request.urlopen")
def test_fetch_projections_url_error_degrades(mock_urlopen, tmp_path: Path) -> None:
    mock_urlopen.side_effect = urllib.error.URLError("no route to host")

    result = sleeper.fetch_projections(2025, 4, cache_root=tmp_path)

    assert result["available"] is False
    assert result["reason"]


@patch("urllib.request.urlopen")
def test_fetch_projections_timeout_degrades(mock_urlopen, tmp_path: Path) -> None:
    mock_urlopen.side_effect = TimeoutError("timed out")

    result = sleeper.fetch_projections(2025, 4, cache_root=tmp_path)

    assert result["available"] is False
    assert result["reason"]


@patch("urllib.request.urlopen")
def test_fetch_projections_invalid_json_degrades(mock_urlopen, tmp_path: Path) -> None:
    mock_urlopen.return_value = _FakeResponse(b"not json at all")

    result = sleeper.fetch_projections(2025, 4, cache_root=tmp_path)

    assert result["available"] is False
    assert result["reason"]


@patch("urllib.request.urlopen")
def test_fetch_projections_wrong_shape_degrades(mock_urlopen, tmp_path: Path) -> None:
    mock_urlopen.return_value = _FakeResponse(_fake_body("nope"))

    result = sleeper.fetch_projections(2025, 4, cache_root=tmp_path)

    assert result["available"] is False
    assert result["reason"]
    assert result["data"] is None


# ---------------------------------------------------------------------------
# fetch_player_index
# ---------------------------------------------------------------------------


@patch("engine.sources.sleeper.fetch_cached_json")
def test_fetch_player_index_parses_fixture(mock_fetch, tmp_path: Path) -> None:
    payload = _load_fixture("sleeper_players.json")
    mock_fetch.return_value = (payload, "2026-09-10T12:00:00Z", False)

    result = sleeper.fetch_player_index(cache_root=tmp_path)

    assert result["available"] is True
    data = result["data"]
    assert data["count"] == 4

    allen = data["players"]["4984"]
    assert isinstance(allen["positions"], list)
    assert allen["positions"] == ["QB"]
    assert allen["active"] is True

    harrison = data["players"]["11604"]
    assert harrison["normalized_name"] == "marvin harrison"
    assert isinstance(harrison["positions"], list)

    st_brown = data["players"]["7547"]
    assert st_brown["normalized_name"] == "amon ra st brown"
    assert st_brown["nfl_team"] == ""
    assert st_brown["active"] is False


@patch("engine.sources.sleeper.fetch_cached_json")
def test_fetch_player_index_skips_non_dict_values(mock_fetch, tmp_path: Path) -> None:
    payload = {"1": {"first_name": "A", "last_name": "B", "active": True}, "2": "garbage"}
    mock_fetch.return_value = (payload, "2026-09-10T12:00:00Z", False)

    result = sleeper.fetch_player_index(cache_root=tmp_path)

    assert result["data"]["count"] == 1
    assert "2" not in result["data"]["players"]


def test_fetch_player_index_enabled_false(tmp_path: Path) -> None:
    with patch("urllib.request.urlopen") as mock_urlopen:
        result = sleeper.fetch_player_index(enabled=False, cache_root=tmp_path)
        assert mock_urlopen.call_count == 0

    assert result["available"] is False
    assert result["reason"] == "disabled"


@patch("urllib.request.urlopen")
def test_fetch_player_index_wrong_shape_degrades(mock_urlopen, tmp_path: Path) -> None:
    mock_urlopen.return_value = _FakeResponse(_fake_body(["not", "a", "dict"]))

    result = sleeper.fetch_player_index(cache_root=tmp_path)

    assert result["available"] is False
    assert result["reason"]


@patch("urllib.request.urlopen")
def test_fetch_player_index_http_error_degrades(mock_urlopen, tmp_path: Path) -> None:
    mock_urlopen.side_effect = urllib.error.HTTPError(
        "https://api.sleeper.app", 500, "Internal Server Error", hdrs=None, fp=None
    )

    result = sleeper.fetch_player_index(cache_root=tmp_path)

    assert result["available"] is False
    assert result["reason"]


@patch("urllib.request.urlopen")
def test_fetch_player_index_warm_cache_makes_no_additional_calls(mock_urlopen, tmp_path: Path) -> None:
    mock_urlopen.return_value = _FakeResponse(_fake_body(_load_fixture("sleeper_players.json")))

    sleeper.fetch_player_index(cache_root=tmp_path)
    sleeper.fetch_player_index(cache_root=tmp_path)

    assert mock_urlopen.call_count == 1


# ---------------------------------------------------------------------------
# fetch_trending
# ---------------------------------------------------------------------------


@patch("engine.sources.sleeper.fetch_cached_json")
def test_fetch_trending_add_parses_fixture_preserving_order(mock_fetch, tmp_path: Path) -> None:
    payload = _load_fixture("sleeper_trending_add.json")
    mock_fetch.return_value = (payload, "2026-09-10T12:00:00Z", False)

    result = sleeper.fetch_trending("add", cache_root=tmp_path)

    assert result["available"] is True
    data = result["data"]
    assert data["kind"] == "add"
    assert data["count"] == 4
    assert [p["player_id"] for p in data["players"]] == ["11581", "13264", "4986", "7547"]
    assert data["players"][0]["count"] == 289449


def test_fetch_trending_bogus_kind_raises_engine_error(tmp_path: Path) -> None:
    with pytest.raises(EngineError):
        sleeper.fetch_trending("bogus", cache_root=tmp_path)


def test_fetch_trending_bogus_kind_raises_even_when_disabled(tmp_path: Path) -> None:
    with pytest.raises(EngineError):
        sleeper.fetch_trending("bogus", enabled=False, cache_root=tmp_path)


def test_fetch_trending_enabled_false(tmp_path: Path) -> None:
    with patch("urllib.request.urlopen") as mock_urlopen:
        result = sleeper.fetch_trending("add", enabled=False, cache_root=tmp_path)
        assert mock_urlopen.call_count == 0

    assert result["available"] is False
    assert result["reason"] == "disabled"


@patch("urllib.request.urlopen")
def test_fetch_trending_uses_url_template_and_cache_key(mock_urlopen, tmp_path: Path) -> None:
    mock_urlopen.return_value = _FakeResponse(_fake_body(_load_fixture("sleeper_trending_add.json")))

    sleeper.fetch_trending("drop", limit=10, lookback_hours=48, cache_root=tmp_path)

    request = mock_urlopen.call_args.args[0]
    expected_url = sleeper.TRENDING_URL_TEMPLATE.format(kind="drop", lookback_hours=48, limit=10)
    assert request.full_url == expected_url
    assert (tmp_path / "sleeper-trending-drop-48h-10.json").exists()


@patch("urllib.request.urlopen")
def test_fetch_trending_http_error_degrades(mock_urlopen, tmp_path: Path) -> None:
    mock_urlopen.side_effect = urllib.error.HTTPError(
        "https://api.sleeper.app", 503, "Service Unavailable", hdrs=None, fp=None
    )

    result = sleeper.fetch_trending("add", cache_root=tmp_path)

    assert result["available"] is False
    assert result["reason"]


@patch("urllib.request.urlopen")
def test_fetch_trending_url_error_degrades(mock_urlopen, tmp_path: Path) -> None:
    mock_urlopen.side_effect = urllib.error.URLError("no route to host")

    result = sleeper.fetch_trending("add", cache_root=tmp_path)

    assert result["available"] is False
    assert result["reason"]


@patch("urllib.request.urlopen")
def test_fetch_trending_timeout_degrades(mock_urlopen, tmp_path: Path) -> None:
    mock_urlopen.side_effect = TimeoutError("timed out")

    result = sleeper.fetch_trending("add", cache_root=tmp_path)

    assert result["available"] is False
    assert result["reason"]


@patch("urllib.request.urlopen")
def test_fetch_trending_invalid_json_degrades(mock_urlopen, tmp_path: Path) -> None:
    mock_urlopen.return_value = _FakeResponse(b"not json at all")

    result = sleeper.fetch_trending("add", cache_root=tmp_path)

    assert result["available"] is False
    assert result["reason"]


@patch("urllib.request.urlopen")
def test_fetch_trending_wrong_shape_degrades(mock_urlopen, tmp_path: Path) -> None:
    mock_urlopen.return_value = _FakeResponse(_fake_body({"error": "not found"}))

    result = sleeper.fetch_trending("add", cache_root=tmp_path)

    assert result["available"] is False
    assert result["reason"]
    assert result["data"] is None


@patch("urllib.request.urlopen")
def test_fetch_trending_warm_cache_makes_no_additional_calls(mock_urlopen, tmp_path: Path) -> None:
    mock_urlopen.return_value = _FakeResponse(_fake_body(_load_fixture("sleeper_trending_add.json")))

    sleeper.fetch_trending("add", cache_root=tmp_path)
    sleeper.fetch_trending("add", cache_root=tmp_path)

    assert mock_urlopen.call_count == 1


def test_fetch_trending_envelope_keys_and_json_serializable(tmp_path: Path) -> None:
    with patch("engine.sources.sleeper.fetch_cached_json") as mock_fetch:
        mock_fetch.return_value = (
            _load_fixture("sleeper_trending_add.json"),
            "2026-09-10T12:00:00Z",
            False,
        )
        result = sleeper.fetch_trending("add", cache_root=tmp_path)

    assert tuple(result.keys()) == ("source", "available", "stale", "reason", "fetched_at", "data")
    assert json.dumps(result)


# ---------------------------------------------------------------------------
# player_id_by_normalized_name
# ---------------------------------------------------------------------------


def test_player_id_by_normalized_name_from_player_index_data() -> None:
    index_data = {
        "players": {
            "4984": {"player_id": "4984", "normalized_name": "josh allen"},
            "11604": {"player_id": "11604", "normalized_name": "marvin harrison"},
        },
        "count": 2,
    }

    mapping = sleeper.player_id_by_normalized_name(index_data)

    assert mapping == {"josh allen": "4984", "marvin harrison": "11604"}


def test_player_id_by_normalized_name_from_projections_data() -> None:
    projections_data = {
        "projections": {
            "4984": {"player_id": "4984", "normalized_name": "josh allen"},
        },
        "season": 2025,
    }

    mapping = sleeper.player_id_by_normalized_name(projections_data)

    assert mapping == {"josh allen": "4984"}


def test_player_id_by_normalized_name_first_wins_on_collision() -> None:
    index_data = {
        "players": {
            "1": {"player_id": "1", "normalized_name": "same name"},
            "2": {"player_id": "2", "normalized_name": "same name"},
        }
    }

    mapping = sleeper.player_id_by_normalized_name(index_data)

    assert mapping == {"same name": "1"}


def test_player_id_by_normalized_name_skips_blank_normalized_name() -> None:
    index_data = {"players": {"1": {"player_id": "1", "normalized_name": ""}}}

    assert sleeper.player_id_by_normalized_name(index_data) == {}


@pytest.mark.parametrize(
    "garbage",
    [None, "nope", 42, [], {}, {"players": "nope"}, {"unrelated": {}}],
)
def test_player_id_by_normalized_name_returns_empty_for_garbage(garbage: Any) -> None:
    assert sleeper.player_id_by_normalized_name(garbage) == {}
