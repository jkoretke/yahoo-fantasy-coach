"""Tests for engine.sources.injuries: ESPN's NFL injuries feed, mapped onto
the repo's frozen player status vocabulary.

FETCH PATH tests (proving the URL, the cache and the failure handling)
patch urllib.request.urlopen directly, with a fake response object
implementing __enter__/__exit__/read, matching tests/test_sources_base.py
and the tests/conftest.py block_real_network fixture that guards this
whole suite against a real network call.

PARSE PATH tests (proving the shape mapping) patch
"engine.sources.injuries.fetch_cached_json" directly, since that name is
imported into this module's own namespace (not looked up through
engine.sources.base at call time), and never touch disk.

Every test that does touch disk passes cache_root=tmp_path, so the suite
never writes into the repo's own runs/cache/ directory.
"""
from __future__ import annotations

import json
import urllib.error
from pathlib import Path
from unittest.mock import patch

import pytest

from engine.common import REPO_ROOT
from engine.sources import injuries

FIXTURE_PATH = REPO_ROOT / "fixtures" / "sources" / "espn_injuries.json"


def _load_fixture() -> dict:
    return json.loads(FIXTURE_PATH.read_bytes())


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


# ---------------------------------------------------------------------------
# status_code
# ---------------------------------------------------------------------------


def test_status_code_covers_every_status_codes_entry() -> None:
    for raw, expected in injuries.STATUS_CODES.items():
        assert expected in injuries.STATUS_VOCABULARY
        assert injuries.status_code(raw) == expected


def test_status_code_is_case_and_whitespace_insensitive() -> None:
    assert injuries.status_code("  QUESTIONABLE  ") == "Q"
    assert injuries.status_code("Injured Reserve") == "IR"
    assert injuries.status_code("SUSPENDED") == "SUSP"


def test_status_code_none_returns_active() -> None:
    assert injuries.status_code(None) == ""


def test_status_code_blank_returns_active() -> None:
    assert injuries.status_code("") == ""
    assert injuries.status_code("   ") == ""


def test_status_code_none_ignores_type_abbreviation() -> None:
    # A blank/None espn_status must return "" directly, without ever
    # consulting espn_type_abbreviation, even if that fallback would
    # otherwise produce a different vocabulary member.
    assert injuries.status_code(None, "IR") == ""
    assert injuries.status_code("", "IR") == ""


def test_status_code_unknown_status_falls_back_to_recognized_type_abbreviation() -> None:
    assert injuries.status_code("Reserve/Something New", "IR") == "IR"
    assert injuries.status_code("Reserve/Something New", "q") == "Q"


def test_status_code_fully_unknown_returns_conservative_out() -> None:
    assert injuries.status_code("Reserve/Commissioner Exempt", "CEL") == "O"
    assert injuries.status_code("Totally Unrecognized Designation") == "O"


def test_status_code_blank_type_abbreviation_does_not_match_active_member() -> None:
    # STATUS_VOCABULARY contains "" (active) as a real member; a blank or
    # missing type abbreviation must not accidentally satisfy that
    # membership check and must fall through to the conservative "O".
    assert injuries.status_code("Nonsense Status", "") == "O"
    assert injuries.status_code("Nonsense Status", None) == "O"


def test_status_code_always_returns_a_vocabulary_member() -> None:
    for raw in list(injuries.STATUS_CODES) + ["something else entirely", ""]:
        assert injuries.status_code(raw) in injuries.STATUS_VOCABULARY


# ---------------------------------------------------------------------------
# fetch_injuries: enabled=False
# ---------------------------------------------------------------------------


@patch("urllib.request.urlopen")
def test_fetch_injuries_disabled_makes_zero_network_calls(mock_urlopen, tmp_path: Path) -> None:
    result = injuries.fetch_injuries(enabled=False, cache_root=tmp_path)
    assert result["available"] is False
    assert result["reason"] == "disabled"
    assert result["data"] is None
    assert mock_urlopen.call_count == 0
    assert not tmp_path.exists() or not any(tmp_path.iterdir())


def test_fetch_injuries_disabled_envelope_has_exact_keys(tmp_path: Path) -> None:
    result = injuries.fetch_injuries(enabled=False, cache_root=tmp_path)
    assert set(result.keys()) == {"source", "available", "stale", "reason", "fetched_at", "data"}
    assert json.dumps(result)


# ---------------------------------------------------------------------------
# fetch_injuries: PARSE PATH (patching fetch_cached_json)
# ---------------------------------------------------------------------------


def _patched_fetch(payload):
    return patch(
        "engine.sources.injuries.fetch_cached_json",
        return_value=(payload, "2026-09-10T12:00:00Z", False),
    )


def test_fetch_injuries_parses_fixture_into_documented_shape(tmp_path: Path) -> None:
    payload = _load_fixture()
    with _patched_fetch(payload):
        result = injuries.fetch_injuries(cache_root=tmp_path)

    assert result["available"] is True
    assert result["stale"] is False
    assert result["reason"] is None
    assert result["fetched_at"] == "2026-09-10T12:00:00Z"

    data = result["data"]
    assert data["source_url"] == injuries.INJURIES_URL
    assert data["season"] == {"name": "Regular Season", "type": 2, "year": 2026}
    assert data["reported_at"] == "2026-08-30T22:15:00Z"
    assert data["count"] == 6
    assert len(data["players"]) == 6


def test_fetch_injuries_flattens_and_sorts_all_groups(tmp_path: Path) -> None:
    payload = _load_fixture()
    with _patched_fetch(payload):
        result = injuries.fetch_injuries(cache_root=tmp_path)

    order = [(p["nfl_team"], p["normalized_name"]) for p in result["data"]["players"]]
    assert order == [
        ("", "cole bennett"),
        ("ARI", "james conner"),
        ("ARI", "trey mcbride"),
        ("KC", "deion marsh"),
        ("KC", "marcus ridley"),
        ("WAS", "terry wilson"),
    ]


def test_fetch_injuries_wsh_normalizes_to_was(tmp_path: Path) -> None:
    payload = _load_fixture()
    with _patched_fetch(payload):
        result = injuries.fetch_injuries(cache_root=tmp_path)

    wilson = next(p for p in result["data"]["players"] if p["name"] == "Terry Wilson")
    assert wilson["nfl_team"] == "WAS"
    assert wilson["status"] == "Q"
    assert wilson["status_raw"] == "Questionable"


def test_fetch_injuries_missing_team_and_details_default_to_blank(tmp_path: Path) -> None:
    payload = _load_fixture()
    with _patched_fetch(payload):
        result = injuries.fetch_injuries(cache_root=tmp_path)

    bennett = next(p for p in result["data"]["players"] if p["name"] == "Cole Bennett")
    assert bennett["nfl_team"] == ""
    assert bennett["injury_type"] == ""
    assert bennett["return_date"] == ""
    assert bennett["fantasy_status"] == ""
    assert bennett["status"] == "O"
    # No shortComment key at all on this fixture item; must fall back to
    # longComment rather than crash or return "".
    assert bennett["comment"].startswith("Not yet cleared")


def test_fetch_injuries_ir_item_has_full_details(tmp_path: Path) -> None:
    payload = _load_fixture()
    with _patched_fetch(payload):
        result = injuries.fetch_injuries(cache_root=tmp_path)

    conner = next(p for p in result["data"]["players"] if p["name"] == "James Conner")
    assert conner["status"] == "IR"
    assert conner["status_raw"] == "Injured Reserve"
    assert conner["injury_type"] == "Foot"
    assert conner["return_date"] == "2026-10-11"
    assert conner["fantasy_status"] == "IR-R"
    assert conner["nfl_team"] == "ARI"
    assert conner["position"] == "RB"


def test_fetch_injuries_date_is_normalized_to_seconds_precision(tmp_path: Path) -> None:
    payload = _load_fixture()
    with _patched_fetch(payload):
        result = injuries.fetch_injuries(cache_root=tmp_path)

    conner = next(p for p in result["data"]["players"] if p["name"] == "James Conner")
    assert conner["updated"] == "2026-08-30T21:50:00Z"


def test_fetch_injuries_comment_prefers_short_over_long(tmp_path: Path) -> None:
    payload = _load_fixture()
    with _patched_fetch(payload):
        result = injuries.fetch_injuries(cache_root=tmp_path)

    conner = next(p for p in result["data"]["players"] if p["name"] == "James Conner")
    assert conner["comment"] == "Conner (foot) was placed on injured reserve Sunday."
    assert "significant time" not in conner["comment"]


def test_fetch_injuries_unrecognized_status_falls_back_to_out(tmp_path: Path) -> None:
    payload = _load_fixture()
    with _patched_fetch(payload):
        result = injuries.fetch_injuries(cache_root=tmp_path)

    marsh = next(p for p in result["data"]["players"] if p["name"] == "Deion Marsh")
    assert marsh["status_raw"] == "Reserve/Commissioner Exempt"
    assert marsh["status"] == "O"


def test_fetch_injuries_skips_item_with_no_usable_name(tmp_path: Path) -> None:
    payload = {
        "injuries": [
            {
                "id": "1",
                "displayName": "Some Team",
                "injuries": [
                    {
                        "id": "1",
                        "status": "Questionable",
                        "date": "2026-08-30T21:50Z",
                        "athlete": {"displayName": "", "firstName": "", "lastName": ""},
                    },
                    {
                        "id": "2",
                        "status": "Active",
                        "date": "2026-08-30T21:50Z",
                        "athlete": {"displayName": "Real Player"},
                    },
                ],
            }
        ],
        "season": None,
        "status": "success",
        "timestamp": "2026-08-30T22:00Z",
    }
    with _patched_fetch(payload):
        result = injuries.fetch_injuries(cache_root=tmp_path)

    assert result["data"]["count"] == 1
    assert result["data"]["players"][0]["name"] == "Real Player"
    assert result["data"]["season"] is None


def test_fetch_injuries_skips_non_dict_groups_and_items(tmp_path: Path) -> None:
    payload = {
        "injuries": [
            "not a group",
            {"id": "1", "displayName": "Team", "injuries": "not a list"},
            {
                "id": "2",
                "displayName": "Team Two",
                "injuries": [
                    "not an item",
                    {"id": "1", "status": "Active", "athlete": {"displayName": "Fine Player"}},
                ],
            },
        ],
        "season": {"year": 2026},
        "status": "success",
        "timestamp": "2026-08-30T22:00Z",
    }
    with _patched_fetch(payload):
        result = injuries.fetch_injuries(cache_root=tmp_path)

    assert result["available"] is True
    assert result["data"]["count"] == 1
    assert result["data"]["players"][0]["name"] == "Fine Player"


# ---------------------------------------------------------------------------
# fetch_injuries: degradation modes
# ---------------------------------------------------------------------------


@patch("urllib.request.urlopen")
def test_fetch_injuries_http_error_returns_unavailable(mock_urlopen, tmp_path: Path) -> None:
    mock_urlopen.side_effect = urllib.error.HTTPError(
        injuries.INJURIES_URL, 503, "Service Unavailable", hdrs=None, fp=None
    )
    result = injuries.fetch_injuries(cache_root=tmp_path)
    assert result["available"] is False
    assert result["stale"] is False
    assert result["reason"]
    assert result["data"] is None


@patch("urllib.request.urlopen")
def test_fetch_injuries_url_error_returns_unavailable(mock_urlopen, tmp_path: Path) -> None:
    mock_urlopen.side_effect = urllib.error.URLError("no route to host")
    result = injuries.fetch_injuries(cache_root=tmp_path)
    assert result["available"] is False
    assert result["reason"]


@patch("urllib.request.urlopen")
def test_fetch_injuries_timeout_returns_unavailable(mock_urlopen, tmp_path: Path) -> None:
    mock_urlopen.side_effect = TimeoutError("timed out")
    result = injuries.fetch_injuries(cache_root=tmp_path)
    assert result["available"] is False
    assert result["reason"]


@patch("urllib.request.urlopen")
def test_fetch_injuries_invalid_json_body_returns_unavailable(mock_urlopen, tmp_path: Path) -> None:
    mock_urlopen.return_value = _FakeResponse(b"not json at all")
    result = injuries.fetch_injuries(cache_root=tmp_path)
    assert result["available"] is False
    assert result["reason"]


@patch("urllib.request.urlopen")
def test_fetch_injuries_wrong_shape_json_list_returns_unavailable(mock_urlopen, tmp_path: Path) -> None:
    mock_urlopen.return_value = _FakeResponse(json.dumps([1, 2, 3]).encode("utf-8"))
    result = injuries.fetch_injuries(cache_root=tmp_path)
    assert result["available"] is False
    assert result["reason"]
    assert result["data"] is None


@patch("urllib.request.urlopen")
def test_fetch_injuries_wrong_shape_no_injuries_key_returns_unavailable(mock_urlopen, tmp_path: Path) -> None:
    mock_urlopen.return_value = _FakeResponse(json.dumps({"season": {"year": 2026}}).encode("utf-8"))
    result = injuries.fetch_injuries(cache_root=tmp_path)
    assert result["available"] is False
    assert result["reason"]
    assert result["data"] is None


def test_degradation_modes_never_raise(tmp_path: Path) -> None:
    # Belt and suspenders: none of the four failure modes above may ever
    # propagate an exception out of fetch_injuries.
    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.side_effect = urllib.error.URLError("down")
        try:
            injuries.fetch_injuries(cache_root=tmp_path)
        except Exception as exc:  # pragma: no cover - failure path
            pytest.fail(f"fetch_injuries raised {exc!r} instead of degrading")


# ---------------------------------------------------------------------------
# fetch_injuries: envelope shape and caching behavior
# ---------------------------------------------------------------------------


@patch("urllib.request.urlopen")
def test_fetch_injuries_success_envelope_has_exact_keys(mock_urlopen, tmp_path: Path) -> None:
    payload = _load_fixture()
    mock_urlopen.return_value = _FakeResponse(json.dumps(payload).encode("utf-8"))
    result = injuries.fetch_injuries(cache_root=tmp_path)
    assert set(result.keys()) == {"source", "available", "stale", "reason", "fetched_at", "data"}
    assert json.dumps(result)


@patch("urllib.request.urlopen")
def test_fetch_injuries_requests_the_documented_url(mock_urlopen, tmp_path: Path) -> None:
    payload = _load_fixture()
    mock_urlopen.return_value = _FakeResponse(json.dumps(payload).encode("utf-8"))
    injuries.fetch_injuries(cache_root=tmp_path)
    request = mock_urlopen.call_args.args[0]
    assert request.full_url == injuries.INJURIES_URL


@patch("urllib.request.urlopen")
def test_fetch_injuries_second_call_against_warm_cache_makes_no_new_calls(
    mock_urlopen, tmp_path: Path
) -> None:
    payload = _load_fixture()
    mock_urlopen.return_value = _FakeResponse(json.dumps(payload).encode("utf-8"))

    first = injuries.fetch_injuries(cache_root=tmp_path)
    assert mock_urlopen.call_count == 1
    assert first["available"] is True

    second = injuries.fetch_injuries(cache_root=tmp_path)
    assert mock_urlopen.call_count == 1
    assert second["data"]["count"] == first["data"]["count"]


# ---------------------------------------------------------------------------
# injuries_by_team
# ---------------------------------------------------------------------------


def _fetch_parsed_data(tmp_path: Path) -> dict:
    payload = _load_fixture()
    with _patched_fetch(payload):
        result = injuries.fetch_injuries(cache_root=tmp_path)
    return result["data"]


def test_injuries_by_team_groups_players_preserving_order(tmp_path: Path) -> None:
    data = _fetch_parsed_data(tmp_path)
    grouped = injuries.injuries_by_team(data)

    assert set(grouped.keys()) == {"", "ARI", "KC", "WAS"}
    assert [p["normalized_name"] for p in grouped["ARI"]] == ["james conner", "trey mcbride"]
    assert [p["normalized_name"] for p in grouped["KC"]] == ["deion marsh", "marcus ridley"]
    assert [p["normalized_name"] for p in grouped["WAS"]] == ["terry wilson"]


def test_injuries_by_team_garbage_input_returns_empty_dict() -> None:
    assert injuries.injuries_by_team({}) == {}
    assert injuries.injuries_by_team({"players": "not a list"}) == {}
    assert injuries.injuries_by_team(None) == {}
    assert injuries.injuries_by_team("nonsense") == {}


# ---------------------------------------------------------------------------
# status_for_player
# ---------------------------------------------------------------------------


def test_status_for_player_finds_by_normalized_name(tmp_path: Path) -> None:
    data = _fetch_parsed_data(tmp_path)
    player = injuries.status_for_player(data, "james conner")
    assert player is not None
    assert player["name"] == "James Conner"
    assert player["status"] == "IR"


def test_status_for_player_matches_with_accents_and_punctuation_via_normalize_name(
    tmp_path: Path,
) -> None:
    data = _fetch_parsed_data(tmp_path)
    player = injuries.status_for_player(data, "  JAMES   CONNER  ")
    assert player is not None
    assert player["name"] == "James Conner"


def test_status_for_player_team_filter_matches(tmp_path: Path) -> None:
    data = _fetch_parsed_data(tmp_path)
    player = injuries.status_for_player(data, "Terry Wilson", nfl_team="WSH")
    assert player is not None
    assert player["name"] == "Terry Wilson"
    assert player["nfl_team"] == "WAS"


def test_status_for_player_team_filter_mismatch_returns_none(tmp_path: Path) -> None:
    data = _fetch_parsed_data(tmp_path)
    player = injuries.status_for_player(data, "Terry Wilson", nfl_team="ARI")
    assert player is None


def test_status_for_player_unknown_name_returns_none(tmp_path: Path) -> None:
    data = _fetch_parsed_data(tmp_path)
    assert injuries.status_for_player(data, "Nobody Here") is None


def test_status_for_player_blank_name_returns_none(tmp_path: Path) -> None:
    data = _fetch_parsed_data(tmp_path)
    assert injuries.status_for_player(data, "") is None


def test_status_for_player_garbage_input_returns_none() -> None:
    assert injuries.status_for_player({}, "James Conner") is None
    assert injuries.status_for_player({"players": "nope"}, "James Conner") is None
    assert injuries.status_for_player(None, "James Conner") is None
