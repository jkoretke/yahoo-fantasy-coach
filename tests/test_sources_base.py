"""Tests for engine.sources.base: cached stdlib HTTP, the result envelope,
and the name/team normalizers.

Every test that touches disk passes an explicit cache_root under tmp_path,
so the suite never writes into the repo's own runs/cache/ directory. Every
test that exercises the HTTP path patches urllib.request.urlopen directly
(the tests/conftest.py block_real_network fixture would otherwise raise on
any unpatched call), using a fake response object that implements
__enter__/__exit__/read, matching how urllib.request.urlopen is used as a
context manager in production code.
"""
from __future__ import annotations

import json
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from engine.common import EngineError
from engine.sources import base


# ---------------------------------------------------------------------------
# cache_path
# ---------------------------------------------------------------------------


def test_cache_path_honors_cache_root_override(tmp_path: Path) -> None:
    path = base.cache_path("sleeper_players", cache_root=tmp_path)
    assert path == tmp_path / "sleeper_players.json"


def test_cache_path_default_root_is_under_cache_root() -> None:
    path = base.cache_path("sleeper_players")
    assert path == base.CACHE_ROOT / "sleeper_players.json"


@pytest.mark.parametrize("bad_key", ["../evil", "a/b", "", ".", ".."])
def test_cache_path_rejects_traversal_like_keys(tmp_path: Path, bad_key: str) -> None:
    with pytest.raises(EngineError):
        base.cache_path(bad_key, cache_root=tmp_path)


# ---------------------------------------------------------------------------
# write_cache / read_cache
# ---------------------------------------------------------------------------


def test_write_cache_then_read_cache_round_trips_dict_payload(tmp_path: Path) -> None:
    payload = {"players": [{"id": "1", "name": "James Conner"}]}
    base.write_cache("key_a", "https://example.test/a", payload, cache_root=tmp_path)
    entry = base.read_cache("key_a", cache_root=tmp_path)
    assert entry is not None
    assert entry["payload"] == payload
    assert entry["url"] == "https://example.test/a"
    assert isinstance(entry["fetched_at"], str)


def test_write_cache_then_read_cache_round_trips_list_payload(tmp_path: Path) -> None:
    payload = [{"id": "1"}, {"id": "2"}]
    base.write_cache("key_b", "https://example.test/b", payload, cache_root=tmp_path)
    entry = base.read_cache("key_b", cache_root=tmp_path)
    assert entry is not None
    assert entry["payload"] == payload


def test_read_cache_missing_file_returns_none(tmp_path: Path) -> None:
    assert base.read_cache("nope", cache_root=tmp_path) is None


def test_read_cache_invalid_json_returns_none(tmp_path: Path) -> None:
    path = base.cache_path("broken", cache_root=tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not json{{{", encoding="utf-8")
    assert base.read_cache("broken", cache_root=tmp_path) is None


def test_read_cache_json_list_returns_none(tmp_path: Path) -> None:
    path = base.cache_path("a_list", cache_root=tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    assert base.read_cache("a_list", cache_root=tmp_path) is None


def test_read_cache_missing_payload_key_returns_none(tmp_path: Path) -> None:
    path = base.cache_path("no_payload", cache_root=tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"fetched_at": "2026-01-01T00:00:00Z"}), encoding="utf-8")
    assert base.read_cache("no_payload", cache_root=tmp_path) is None


def test_write_cache_swallows_unwritable_directory(tmp_path: Path) -> None:
    blocked = tmp_path / "blocked"
    blocked.write_text("i am a file, not a directory", encoding="utf-8")
    bad_root = blocked / "sub"
    # Should not raise even though bad_root can never be created (its parent
    # is a file, not a directory).
    base.write_cache("key", "https://example.test", {"a": 1}, cache_root=bad_root)
    assert base.read_cache("key", cache_root=bad_root) is None


# ---------------------------------------------------------------------------
# cache_age_seconds
# ---------------------------------------------------------------------------


def test_cache_age_seconds_is_roughly_zero_for_fresh_entry() -> None:
    entry = {"fetched_at": base_timestamp_now()}
    age = base.cache_age_seconds(entry)
    assert 0 <= age < 5


def test_cache_age_seconds_returns_inf_for_missing_fetched_at() -> None:
    assert base.cache_age_seconds({}) == float("inf")


def test_cache_age_seconds_returns_inf_for_garbage_fetched_at() -> None:
    assert base.cache_age_seconds({"fetched_at": "not-a-timestamp"}) == float("inf")


def test_cache_age_seconds_computes_delta_against_now() -> None:
    now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    entry = {"fetched_at": "2026-01-01T11:00:00Z"}
    age = base.cache_age_seconds(entry, now=now)
    assert age == pytest.approx(3600.0)


def base_timestamp_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# fetch_json
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


@patch("urllib.request.urlopen")
def test_fetch_json_returns_decoded_dict(mock_urlopen) -> None:
    mock_urlopen.return_value = _FakeResponse(json.dumps({"ok": True}).encode("utf-8"))
    result = base.fetch_json("https://example.test/dict")
    assert result == {"ok": True}


@patch("urllib.request.urlopen")
def test_fetch_json_returns_decoded_list(mock_urlopen) -> None:
    mock_urlopen.return_value = _FakeResponse(json.dumps([1, 2, 3]).encode("utf-8"))
    result = base.fetch_json("https://example.test/list")
    assert result == [1, 2, 3]


@patch("urllib.request.urlopen")
def test_fetch_json_sets_user_agent_header(mock_urlopen) -> None:
    mock_urlopen.return_value = _FakeResponse(b"{}")
    base.fetch_json("https://example.test/anything")
    request = mock_urlopen.call_args.args[0]
    # urllib.request.Request stores header keys through str.capitalize(),
    # so "User-Agent" is stored (and must be looked up) as "User-agent".
    assert request.get_header("User-agent") == "yahoo-fantasy-coach/1.0"


@patch("urllib.request.urlopen")
def test_fetch_json_caller_header_survives_default_casing(mock_urlopen) -> None:
    # A caller-supplied header must win over our default even when it is
    # spelled with different casing than the default we add ("User-Agent"),
    # since urllib.request.Request folds header names through
    # str.capitalize() when storing them.
    mock_urlopen.return_value = _FakeResponse(b"{}")
    base.fetch_json("https://example.test/anything", headers={"user-agent": "custom/9"})
    request = mock_urlopen.call_args.args[0]
    assert request.get_header("User-agent") == "custom/9"


@patch("urllib.request.urlopen")
def test_fetch_json_http_error_raises_source_unavailable(mock_urlopen) -> None:
    mock_urlopen.side_effect = urllib.error.HTTPError(
        "https://example.test", 503, "Service Unavailable", hdrs=None, fp=None
    )
    with pytest.raises(base.SourceUnavailable):
        base.fetch_json("https://example.test", service="sleeper")


@patch("urllib.request.urlopen")
def test_fetch_json_url_error_raises_source_unavailable(mock_urlopen) -> None:
    mock_urlopen.side_effect = urllib.error.URLError("no route to host")
    with pytest.raises(base.SourceUnavailable):
        base.fetch_json("https://example.test", service="sleeper")


@patch("urllib.request.urlopen")
def test_fetch_json_timeout_raises_source_unavailable(mock_urlopen) -> None:
    mock_urlopen.side_effect = TimeoutError("timed out")
    with pytest.raises(base.SourceUnavailable):
        base.fetch_json("https://example.test", service="sleeper")


@patch("urllib.request.urlopen")
def test_fetch_json_invalid_body_raises_source_unavailable(mock_urlopen) -> None:
    mock_urlopen.return_value = _FakeResponse(b"not json at all")
    with pytest.raises(base.SourceUnavailable):
        base.fetch_json("https://example.test", service="sleeper")


def test_source_unavailable_is_engine_error_subclass() -> None:
    assert issubclass(base.SourceUnavailable, EngineError)


# ---------------------------------------------------------------------------
# fetch_cached_json
# ---------------------------------------------------------------------------


def test_fetch_cached_json_fresh_entry_skips_network(tmp_path: Path) -> None:
    base.write_cache("weather", "https://example.test", {"temp": 72}, cache_root=tmp_path)

    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.side_effect = AssertionError("must not call urlopen for a fresh cache hit")
        payload, fetched_at, stale = base.fetch_cached_json(
            "https://example.test", "weather", cache_root=tmp_path
        )

    assert mock_urlopen.call_count == 0
    assert payload == {"temp": 72}
    assert stale is False


@patch("urllib.request.urlopen")
def test_fetch_cached_json_stale_entry_refetches_and_rewrites(mock_urlopen, tmp_path: Path) -> None:
    old = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat().replace("+00:00", "Z")
    base.write_cache(
        "weather", "https://example.test", {"temp": 50}, cache_root=tmp_path, fetched_at=old
    )
    mock_urlopen.return_value = _FakeResponse(json.dumps({"temp": 80}).encode("utf-8"))

    payload, fetched_at, stale = base.fetch_cached_json(
        "https://example.test", "weather", cache_root=tmp_path, max_age_seconds=3600
    )

    assert mock_urlopen.call_count == 1
    assert payload == {"temp": 80}
    assert stale is False

    entry = base.read_cache("weather", cache_root=tmp_path)
    assert entry is not None
    assert entry["payload"] == {"temp": 80}


@patch("urllib.request.urlopen")
def test_fetch_cached_json_stale_entry_failing_fetch_returns_stale(mock_urlopen, tmp_path: Path) -> None:
    old = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat().replace("+00:00", "Z")
    base.write_cache(
        "weather", "https://example.test", {"temp": 50}, cache_root=tmp_path, fetched_at=old
    )
    mock_urlopen.side_effect = urllib.error.URLError("down")

    payload, fetched_at, stale = base.fetch_cached_json(
        "https://example.test", "weather", cache_root=tmp_path, max_age_seconds=3600
    )

    assert payload == {"temp": 50}
    assert stale is True
    assert fetched_at == old


@patch("urllib.request.urlopen")
def test_fetch_cached_json_no_entry_failing_fetch_raises(mock_urlopen, tmp_path: Path) -> None:
    mock_urlopen.side_effect = urllib.error.URLError("down")
    with pytest.raises(base.SourceUnavailable):
        base.fetch_cached_json("https://example.test", "weather", cache_root=tmp_path)


@patch("urllib.request.urlopen")
def test_fetch_cached_json_force_refresh_calls_urlopen_even_when_fresh(mock_urlopen, tmp_path: Path) -> None:
    base.write_cache("weather", "https://example.test", {"temp": 72}, cache_root=tmp_path)
    mock_urlopen.return_value = _FakeResponse(json.dumps({"temp": 90}).encode("utf-8"))

    payload, fetched_at, stale = base.fetch_cached_json(
        "https://example.test", "weather", cache_root=tmp_path, force_refresh=True
    )

    assert mock_urlopen.call_count == 1
    assert payload == {"temp": 90}
    assert stale is False


@patch("urllib.request.urlopen")
def test_fetch_cached_json_force_refresh_failing_fetch_falls_back_to_stale(mock_urlopen, tmp_path: Path) -> None:
    # Fresh cache entry, but force_refresh is set and the forced fetch fails.
    # This proves fetch_cached_json reads the cache unconditionally in step
    # one: without that read, a forced refresh against a dead endpoint would
    # raise instead of degrading to the cached payload.
    base.write_cache("weather", "https://example.test", {"temp": 72}, cache_root=tmp_path)
    mock_urlopen.side_effect = urllib.error.URLError("down")

    payload, fetched_at, stale = base.fetch_cached_json(
        "https://example.test", "weather", cache_root=tmp_path, force_refresh=True
    )

    assert payload == {"temp": 72}
    assert stale is True


# ---------------------------------------------------------------------------
# result envelope
# ---------------------------------------------------------------------------


def test_source_result_has_exactly_source_result_keys() -> None:
    result = base.source_result("sleeper", data={"a": 1})
    assert tuple(result.keys()) == base.SOURCE_RESULT_KEYS
    assert json.dumps(result)


def test_disabled_result_shape() -> None:
    result = base.disabled_result("weather")
    assert tuple(result.keys()) == base.SOURCE_RESULT_KEYS
    assert result["available"] is False
    assert result["reason"] == base.DISABLED_REASON
    assert result["data"] is None
    assert json.dumps(result)


def test_unavailable_result_shape() -> None:
    result = base.unavailable_result("injuries", "espn GET ... failed (503)")
    assert tuple(result.keys()) == base.SOURCE_RESULT_KEYS
    assert result["available"] is False
    assert result["reason"] == "espn GET ... failed (503)"
    assert result["data"] is None
    assert json.dumps(result)


# ---------------------------------------------------------------------------
# normalize_name
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("James Conner", "james conner"),
        ("Amon-Ra St. Brown", "amon ra st brown"),
        ("Marvin Harrison Jr.", "marvin harrison"),
        ("  D.K.  Metcalf ", "d k metcalf"),
        (None, ""),
        ("", ""),
    ],
)
def test_normalize_name_worked_examples(raw: str | None, expected: str) -> None:
    assert base.normalize_name(raw) == expected


def test_normalize_name_never_drops_the_only_remaining_token() -> None:
    assert base.normalize_name("III") == "iii"


def test_normalize_name_blank_whitespace_returns_empty() -> None:
    assert base.normalize_name("   ") == ""


# ---------------------------------------------------------------------------
# normalize_team_abbreviation
# ---------------------------------------------------------------------------


def test_normalize_team_abbreviation_wsh_maps_to_was() -> None:
    assert base.normalize_team_abbreviation("WSH") == "WAS"


def test_normalize_team_abbreviation_lowercase_input() -> None:
    assert base.normalize_team_abbreviation("was") == "WAS"


def test_normalize_team_abbreviation_strips_whitespace() -> None:
    assert base.normalize_team_abbreviation(" jac ") == "JAX"


def test_normalize_team_abbreviation_none_returns_empty() -> None:
    assert base.normalize_team_abbreviation(None) == ""


def test_normalize_team_abbreviation_unknown_code_passes_through_uppercased() -> None:
    assert base.normalize_team_abbreviation("zzz") == "ZZZ"


_CANONICAL_TEAMS = {
    "ARI", "ATL", "BAL", "BUF", "CAR", "CHI", "CIN", "CLE", "DAL", "DEN",
    "DET", "GB", "HOU", "IND", "JAX", "KC", "LAC", "LAR", "LV", "MIA",
    "MIN", "NE", "NO", "NYG", "NYJ", "PHI", "PIT", "SEA", "SF", "TB",
    "TEN", "WAS",
}


def test_team_abbreviation_aliases_all_map_to_a_canonical_code() -> None:
    for alias, canonical in base.TEAM_ABBREVIATION_ALIASES.items():
        assert canonical in _CANONICAL_TEAMS, f"{alias} maps to non-canonical {canonical!r}"
        assert alias not in _CANONICAL_TEAMS, f"{alias} is itself canonical, should not be aliased"
