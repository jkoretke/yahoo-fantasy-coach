"""Tests for engine.yahoo_client's six read-only fetch functions.

This module never imports yfpy, and it never makes a real Yahoo call:
Yahoo's Fantasy Sports API access application for this project was still
pending review when this module was written, so every yfpy interaction
below is against FakeQuery, a plain class defined in this file, never a
real or live query object. tests/conftest.py's block_real_network and
block_real_yahoo_token_dir fixtures apply to every test here automatically
and would fail any test that slipped through to a real network call or a
real credential file.

Fixtures are loaded from fixtures/yahoo/ with json.loads on the file text
rather than engine.common.load_json, the same convention
tests/test_yahoo_shapes_players.py already uses, since load_json requires
a top level JSON object and league_players.json, free_agents.json and
league_matchups_week.json are all top level JSON lists.

Every expected "data" value in a success-path test is computed by calling
the matching engine.yahoo_shapes parse function directly on the same
fixture, never hand-copied, so the assertion cannot drift from what that
parser actually returns.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import engine.yahoo_client as yc
from engine.common import EngineError, REPO_ROOT
from engine.sources.base import SOURCE_RESULT_KEYS, disabled_result
from engine.yahoo_client import YahooUnavailable
from engine import yahoo_shapes


YAHOO_FIXTURES_DIR = REPO_ROOT / "fixtures" / "yahoo"


def _load(name: str) -> Any:
    return json.loads((YAHOO_FIXTURES_DIR / name).read_text(encoding="utf-8"))


def _settings_fixture() -> dict[str, Any]:
    return _load("league_settings.json")


def _metadata_fixture() -> dict[str, Any]:
    return _load("league_metadata.json")


def _players_fixture() -> list[dict[str, Any]]:
    return _load("league_players.json")


def _free_agents_fixture() -> list[dict[str, Any]]:
    return _load("free_agents.json")


def _roster_fixture() -> dict[str, Any]:
    return _load("team_roster_week.json")


def _matchups_fixture() -> list[dict[str, Any]]:
    return _load("league_matchups_week.json")


def _make_free_agent(player_id: str) -> dict[str, Any]:
    """A minimal synthetic Yahoo player dict, for the paging tests below.

    Real fixture data only has two free agent records, which is not
    enough to exercise a 25-per-page paging loop, so these tests generate
    as many distinct records as a scenario needs instead.
    """
    return {
        "player_key": f"461.p.{player_id}",
        "player_id": player_id,
        "name": {"first": "Test", "last": player_id, "full": f"Test Player {player_id}"},
        "editorial_team_abbr": "FA",
        "display_position": "WR",
        "primary_position": "WR",
        "eligible_positions": ["WR"],
        "percent_owned": {"value": 1},
    }


def _assert_envelope_shape(result: dict[str, Any]) -> None:
    assert set(result.keys()) == set(SOURCE_RESULT_KEYS)
    assert result["source"] == "yahoo"
    json.dumps(result)  # every source result must be plain, json.dumps-able JSON


def _maybe_raise(value: Any) -> Any:
    if isinstance(value, BaseException):
        raise value
    return value


class FakeQuery:
    """A stand-in for a resolved yfpy query object.

    A plain class, not unittest.mock.Mock, so the call log (self.calls)
    is easy to read and assert on directly. Every method logs its own
    call before doing anything else, so a call is recorded even when the
    configured behavior is to raise.

    Each of the six data attributes below can be set to either a plain
    JSON value (returned as is) or an exception instance (raised via
    _maybe_raise), so a single test can configure "this method fails
    this way" just by assigning an exception instead of a value.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.league_settings: Any = None
        self.league_metadata: Any = None
        self.rosters: dict[str, Any] = {}
        self.matchups: Any = None
        self.league_key: Any = "461.l.524458"
        self.player_pages: list[Any] = []
        self.player_list: Any = None

    def get_league_settings(self) -> Any:
        self.calls.append(("get_league_settings", (), {}))
        return _maybe_raise(self.league_settings)

    def get_league_metadata(self) -> Any:
        self.calls.append(("get_league_metadata", (), {}))
        return _maybe_raise(self.league_metadata)

    def get_team_roster_by_week(self, team_id: str, chosen_week: Any) -> Any:
        self.calls.append(("get_team_roster_by_week", (team_id, chosen_week), {}))
        return _maybe_raise(self.rosters.get(team_id))

    def get_league_matchups_by_week(self, week: int) -> Any:
        self.calls.append(("get_league_matchups_by_week", (week,), {}))
        return _maybe_raise(self.matchups)

    def get_league_key(self, season: Any = None) -> Any:
        self.calls.append(("get_league_key", (season,), {}))
        return _maybe_raise(self.league_key)

    def query(self, url: str, data_key_list: list[str]) -> Any:
        page_calls_so_far = sum(1 for call in self.calls if call[0] == "query")
        self.calls.append(("query", (url, data_key_list), {}))
        return _maybe_raise(self.player_pages[page_calls_so_far])

    def get_league_players(self, player_count_limit: int | None = None) -> Any:
        self.calls.append(
            ("get_league_players", (), {"player_count_limit": player_count_limit})
        )
        return _maybe_raise(self.player_list)


def _raise_build_query(**kwargs: Any) -> Any:
    raise AssertionError("build_query must not be called when a query object was passed")


# --- success path: envelope shape, "yahoo" source, data matches the parser ---


def test_fetch_league_settings_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(yc, "build_query", _raise_build_query)
    fq = FakeQuery()
    fq.league_settings = _settings_fixture()

    result = yc.fetch_league_settings(query=fq, league_id="524458")

    _assert_envelope_shape(result)
    assert result["available"] is True
    assert result["stale"] is False
    assert result["reason"] is None
    assert result["fetched_at"] is not None
    assert result["data"] == yahoo_shapes.parse_league_settings(_settings_fixture())
    assert fq.calls == [("get_league_settings", (), {})]


def test_fetch_league_metadata_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(yc, "build_query", _raise_build_query)
    fq = FakeQuery()
    fq.league_metadata = _metadata_fixture()

    result = yc.fetch_league_metadata(query=fq, league_id="524458")

    _assert_envelope_shape(result)
    assert result["available"] is True
    assert result["stale"] is False
    assert result["fetched_at"] is not None
    assert result["data"] == yahoo_shapes.parse_league_metadata(_metadata_fixture())
    assert fq.calls == [("get_league_metadata", (), {})]


def test_fetch_matchups_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(yc, "build_query", _raise_build_query)
    fq = FakeQuery()
    fq.matchups = _matchups_fixture()

    result = yc.fetch_matchups(query=fq, league_id="524458", week=3)

    _assert_envelope_shape(result)
    assert result["available"] is True
    assert result["stale"] is False
    assert result["fetched_at"] is not None
    assert result["data"] == yahoo_shapes.parse_matchups(_matchups_fixture(), week=3)
    assert fq.calls == [("get_league_matchups_by_week", (3,), {})]


def test_fetch_player_list_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(yc, "build_query", _raise_build_query)
    fq = FakeQuery()
    fq.player_list = _players_fixture()

    result = yc.fetch_player_list(query=fq, league_id="524458", player_count_limit=10)

    _assert_envelope_shape(result)
    assert result["available"] is True
    assert result["stale"] is False
    assert result["fetched_at"] is not None
    assert result["data"] == yahoo_shapes.parse_player_list(_players_fixture())
    assert fq.calls == [("get_league_players", (), {"player_count_limit": 10})]


def test_fetch_rosters_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(yc, "build_query", _raise_build_query)
    fq = FakeQuery()
    fq.rosters["1"] = _roster_fixture()

    result = yc.fetch_rosters(query=fq, league_id="524458", week=3, team_ids=["1"])

    _assert_envelope_shape(result)
    assert result["available"] is True
    assert result["stale"] is False
    assert result["fetched_at"] is not None
    expected_roster = yahoo_shapes.parse_roster(_roster_fixture(), team_id="1")
    assert result["data"] == {
        "week": 3,
        "rosters": [expected_roster],
        "failed_team_ids": [],
    }
    assert ("get_team_roster_by_week", ("1", 3), {}) in fq.calls


def test_fetch_free_agents_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(yc, "build_query", _raise_build_query)
    fq = FakeQuery()
    fq.player_pages = [_free_agents_fixture()]

    result = yc.fetch_free_agents(query=fq, league_id="524458", limit=50)

    _assert_envelope_shape(result)
    assert result["available"] is True
    assert result["stale"] is False
    assert result["fetched_at"] is not None
    assert result["data"] == yahoo_shapes.parse_free_agents(_free_agents_fixture())
    query_calls = [call for call in fq.calls if call[0] == "query"]
    assert len(query_calls) == 1
    assert ";status=FA;start=0;count=25" in query_calls[0][1][0]


# --- enabled=False: disabled_result, zero calls, no credential read ---


@pytest.mark.parametrize(
    "invoke",
    [
        lambda fq, secrets_path: yc.fetch_league_settings(
            enabled=False, query=fq, league_id="524458", secrets_path=secrets_path
        ),
        lambda fq, secrets_path: yc.fetch_league_metadata(
            enabled=False, query=fq, league_id="524458", secrets_path=secrets_path
        ),
        lambda fq, secrets_path: yc.fetch_matchups(
            enabled=False, query=fq, league_id="524458", week=3, secrets_path=secrets_path
        ),
        lambda fq, secrets_path: yc.fetch_player_list(
            enabled=False, query=fq, league_id="524458", secrets_path=secrets_path
        ),
        lambda fq, secrets_path: yc.fetch_free_agents(
            enabled=False, query=fq, league_id="524458", secrets_path=secrets_path
        ),
        lambda fq, secrets_path: yc.fetch_rosters(
            enabled=False,
            query=fq,
            league_id="524458",
            week=3,
            team_ids=["1"],
            secrets_path=secrets_path,
        ),
    ],
    ids=[
        "league_settings",
        "league_metadata",
        "matchups",
        "player_list",
        "free_agents",
        "rosters",
    ],
)
def test_enabled_false_returns_disabled_result_with_zero_calls(
    invoke: Any, tmp_path: Path
) -> None:
    # secrets_path points at a file that was never written, so any code
    # path that actually tried to read credentials would raise EngineError
    # (see engine.yahoo_client.yahoo_credentials / require_secret). Getting
    # disabled_result back with no error proves enabled=False short
    # circuits before any credential read happens.
    secrets_path = tmp_path / "does-not-exist.env"
    fq = FakeQuery()

    result = invoke(fq, secrets_path)

    assert result == disabled_result("yahoo")
    assert fq.calls == []


# --- unavailable_result on Exception / SystemExit / YahooUnavailable ---
# (fetch_rosters is deliberately not included here: a per-team failure
# there does not make the whole call unavailable, see the dedicated
# fetch_rosters tests below instead.)


def _settings_setup(fq: FakeQuery, exc: BaseException) -> Any:
    fq.league_settings = exc
    return lambda: yc.fetch_league_settings(query=fq, league_id="524458")


def _metadata_setup(fq: FakeQuery, exc: BaseException) -> Any:
    fq.league_metadata = exc
    return lambda: yc.fetch_league_metadata(query=fq, league_id="524458")


def _matchups_setup(fq: FakeQuery, exc: BaseException) -> Any:
    fq.matchups = exc
    return lambda: yc.fetch_matchups(query=fq, league_id="524458", week=3)


def _player_list_setup(fq: FakeQuery, exc: BaseException) -> Any:
    fq.player_list = exc
    return lambda: yc.fetch_player_list(query=fq, league_id="524458")


def _free_agents_first_page_setup(fq: FakeQuery, exc: BaseException) -> Any:
    fq.player_pages = [exc]
    return lambda: yc.fetch_free_agents(query=fq, league_id="524458", limit=10)


DEGRADE_SETUPS = [
    ("league_settings", _settings_setup),
    ("league_metadata", _metadata_setup),
    ("matchups", _matchups_setup),
    ("player_list", _player_list_setup),
    ("free_agents_first_page", _free_agents_first_page_setup),
]

DEGRADE_EXCEPTIONS = [
    pytest.param(ValueError("boom"), id="exception"),
    pytest.param(SystemExit(1), id="system_exit"),
    pytest.param(YahooUnavailable("yahoo outage"), id="yahoo_unavailable"),
]


@pytest.mark.parametrize("name, setup", DEGRADE_SETUPS, ids=[case[0] for case in DEGRADE_SETUPS])
@pytest.mark.parametrize("exc", DEGRADE_EXCEPTIONS)
def test_fetch_functions_degrade_to_unavailable_result(
    name: str, setup: Any, exc: BaseException
) -> None:
    fq = FakeQuery()
    invoke = setup(fq, exc)

    result = invoke()

    _assert_envelope_shape(result)
    assert result["available"] is False
    assert result["data"] is None
    assert isinstance(result["reason"], str) and result["reason"]


# --- the ordering test: missing credential raises, YahooUnavailable returns ---


def test_missing_credential_raises_but_yahoo_unavailable_returns_envelope(
    tmp_path: Path,
) -> None:
    # YahooUnavailable subclasses SourceUnavailable, which subclasses
    # EngineError: YahooUnavailable IS an EngineError and IS an Exception.
    # Only the except clause order inside engine.yahoo_client._run_yahoo_call
    # (YahooUnavailable caught and degraded first, EngineError re-raised
    # second) is what makes these two cases behave differently below; a
    # plain isinstance(exc, EngineError) check could not tell them apart.

    # No secrets file exists at this path at all, and query is not
    # passed, so fetch_league_settings must resolve a query through
    # build_query, which raises EngineError for a missing credential.
    secrets_path = tmp_path / "no-such-secrets.env"
    with pytest.raises(EngineError) as excinfo:
        yc.fetch_league_settings(
            league_id="524458", secrets_path=secrets_path, token_dir=tmp_path / "token-dir"
        )
    assert type(excinfo.value) is EngineError
    assert type(excinfo.value) is not YahooUnavailable

    # Here the query object itself is supplied (no credential read at
    # all), and its get_league_settings raises YahooUnavailable: this
    # must come back as an unavailable envelope, not propagate.
    fq = FakeQuery()
    fq.league_settings = YahooUnavailable("yahoo says no")
    result = yc.fetch_league_settings(query=fq, league_id="524458")
    assert result["available"] is False
    assert result["reason"] == "yahoo says no"
    assert result["data"] is None


# --- fetch_rosters: per-team failure handling ---


def test_fetch_rosters_skips_a_failing_team_and_records_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(yc, "build_query", _raise_build_query)
    fq = FakeQuery()
    fq.rosters["1"] = _roster_fixture()
    fq.rosters["2"] = ValueError("team 2 is broken")

    result = yc.fetch_rosters(query=fq, league_id="524458", week=3, team_ids=["1", "2"])

    assert result["available"] is True
    assert result["data"]["failed_team_ids"] == ["2"]
    assert len(result["data"]["rosters"]) == 1
    assert result["data"]["rosters"][0]["team_id"] == "1"


@pytest.mark.parametrize("exc", DEGRADE_EXCEPTIONS)
def test_fetch_rosters_records_any_exception_type_as_a_failed_team(exc: BaseException) -> None:
    # SystemExit must never escape here either, even though a per-team
    # failure does not make the whole call unavailable: it is caught the
    # same way and simply recorded, proving SystemExit cannot crash this
    # loop.
    fq = FakeQuery()
    fq.rosters["1"] = exc

    result = yc.fetch_rosters(query=fq, league_id="524458", week=3, team_ids=["1"])

    assert result["available"] is True
    assert result["data"]["rosters"] == []
    assert result["data"]["failed_team_ids"] == ["1"]


def test_fetch_rosters_is_unavailable_when_query_resolution_itself_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Every roster test above passes query=fq, so _resolved_query returns
    # immediately and never touches build_query: the resolve-side half of
    # fetch_rosters's own _run_yahoo_call(_resolve) wrapping (as opposed
    # to the per-team half covered above) is only exercised by leaving
    # query unset here, so build_query is what actually gets called and
    # can fail.
    def _fail_build_query(**kwargs: Any) -> Any:
        raise YahooUnavailable("outage during query resolution")

    monkeypatch.setattr(yc, "build_query", _fail_build_query)

    result = yc.fetch_rosters(league_id="524458", week=3, team_ids=["1"])

    assert result["available"] is False
    assert result["data"] is None
    assert result["reason"] == "outage during query resolution"


def test_fetch_rosters_with_no_team_ids_is_unavailable() -> None:
    fq = FakeQuery()

    result = yc.fetch_rosters(query=fq, league_id="524458", week=3, team_ids=None)

    assert result["available"] is False
    assert result["data"] is None
    assert result["reason"] == "team_ids required"
    assert fq.calls == []


# --- fetch_free_agents: paging behavior ---


def test_fetch_free_agents_stops_on_short_page() -> None:
    full_page = [_make_free_agent(f"full-{i}") for i in range(25)]
    short_page = [_make_free_agent(f"short-{i}") for i in range(3)]
    fq = FakeQuery()
    fq.player_pages = [full_page, short_page]

    result = yc.fetch_free_agents(query=fq, league_id="524458", limit=50)

    query_calls = [call for call in fq.calls if call[0] == "query"]
    assert len(query_calls) == 2
    assert result["data"]["count"] == 28


def test_fetch_free_agents_honors_limit_trim() -> None:
    pages = [[_make_free_agent(f"p{page}-{i}") for i in range(25)] for page in range(5)]
    fq = FakeQuery()
    fq.player_pages = pages

    result = yc.fetch_free_agents(query=fq, league_id="524458", limit=30)

    query_calls = [call for call in fq.calls if call[0] == "query"]
    assert len(query_calls) == 2  # 25 + 25 >= 30, so paging stops after 2 pages
    assert result["data"]["count"] == 30


def test_fetch_free_agents_hard_caps_at_max_pages() -> None:
    pages = [[_make_free_agent(f"p{page}-{i}") for i in range(25)] for page in range(45)]
    fq = FakeQuery()
    fq.player_pages = pages

    result = yc.fetch_free_agents(query=fq, league_id="524458", limit=1_000_000)

    query_calls = [call for call in fq.calls if call[0] == "query"]
    assert len(query_calls) == 40  # the hard cap, even though limit was never reached
    assert result["data"]["count"] == 1000


def test_fetch_free_agents_normalizes_a_bare_dict_page() -> None:
    fq = FakeQuery()
    fq.player_pages = [_make_free_agent("solo")]  # a bare dict, not a one item list

    result = yc.fetch_free_agents(query=fq, league_id="524458", limit=50)

    query_calls = [call for call in fq.calls if call[0] == "query"]
    assert len(query_calls) == 1  # a page under 25 entries ends paging
    assert result["data"]["count"] == 1
    assert result["data"]["free_agents"][0]["player_id"] == "solo"


# --- _jsonify: the pass-through seam other chunks call directly ---


def test_jsonify_passes_through_a_plain_dict_unchanged() -> None:
    value = {"a": 1}
    assert yc._jsonify(value) is value  # the same object, not just an equal one


def test_jsonify_passes_through_a_list_of_plain_dicts_unchanged() -> None:
    value = [{"a": 1}, {"b": 2}]
    assert yc._jsonify(value) is value


def test_jsonify_passes_through_an_empty_list_unchanged() -> None:
    value: list[Any] = []
    assert yc._jsonify(value) is value


def test_jsonify_raises_engine_error_naming_python_310_for_a_real_model_value() -> None:
    # A bare object is neither a dict nor a list of dicts, so this falls
    # through to _jsonify's own lazy yfpy import (never written here),
    # which fails on this repo's Python 3.9 virtualenv exactly the way
    # _query_class's own ImportError branch does (see
    # tests/test_yahoo_client_auth.py's matching test for that function).
    with pytest.raises(EngineError, match="3.10"):
        yc._jsonify(object())


# --- a passed-in query is reused as is; build_query is never called ---


def test_passed_in_query_is_reused_and_build_query_is_never_called(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(yc, "build_query", _raise_build_query)

    fq = FakeQuery()
    fq.league_settings = _settings_fixture()
    fq.league_metadata = _metadata_fixture()
    fq.matchups = _matchups_fixture()
    fq.player_list = _players_fixture()
    fq.player_pages = [_free_agents_fixture()]
    fq.rosters["1"] = _roster_fixture()

    assert yc.fetch_league_settings(query=fq, league_id="524458")["available"] is True
    assert yc.fetch_league_metadata(query=fq, league_id="524458")["available"] is True
    assert yc.fetch_matchups(query=fq, league_id="524458", week=3)["available"] is True
    assert yc.fetch_player_list(query=fq, league_id="524458")["available"] is True
    assert yc.fetch_free_agents(query=fq, league_id="524458")["available"] is True
    assert (
        yc.fetch_rosters(query=fq, league_id="524458", week=3, team_ids=["1"])["available"]
        is True
    )
