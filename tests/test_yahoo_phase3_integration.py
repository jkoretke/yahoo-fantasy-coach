"""End to end Phase 3 integration test: the seam between engine.yahoo_client
and engine.identity, and two whole-package guards.

No earlier chunk could prove this seam on its own.
tests/test_yahoo_client_fetch.py proves engine.yahoo_client's fetch
functions parse Yahoo fixtures correctly, in isolation. Whatever tests
engine/identity.py already has prove build_identity_map joins a plain
Yahoo player list to Sleeper and ESPN data correctly, also in isolation.
Neither proves that fetch_player_list's actual output shape is something
identity_result can actually consume. This module drives real data
through both in one pass and asserts the joined result, which is exactly
where a shape mismatch between the two modules would first show up.

Yahoo's Fantasy Sports API access application for this project was still
pending review when this module was written, and yfpy is not installed in
this repo's own Python 3.9 virtualenv (yfpy 17.0.0 requires Python 3.10 or
newer). So every yfpy interaction below is against FakeYfpyQuery, a plain
class defined in this file, never a real or live query object, and this
module never imports yfpy itself. tests/conftest.py's block_real_network
and block_real_yahoo_token_dir fixtures apply to every test here
automatically and would fail any test that slipped through to a real
network call or a real credential file.

The Sleeper and ESPN halves of the join instead run the REAL Phase 2
parsers (engine.sources.sleeper.fetch_player_index,
engine.sources.injuries.fetch_injuries) over the REAL Phase 2 fixtures
(fixtures/sources/sleeper_players.json, fixtures/sources/espn_injuries.json),
with only their fetch_cached_json call patched out, matching
tests/test_sources_injuries.py's own _patched_fetch pattern. Each module
imports fetch_cached_json into its own namespace with
`from engine.sources.base import fetch_cached_json`, so the patch target
must be "engine.sources.sleeper.fetch_cached_json" and
"engine.sources.injuries.fetch_cached_json"; patching
engine.sources.base.fetch_cached_json instead would not affect either
module's already-bound reference and would silently do nothing.

Fixtures are loaded from fixtures/yahoo/ with json.loads on the file text
rather than engine.common.load_json, the same convention
tests/test_yahoo_client_fetch.py already uses, since load_json requires a
top level JSON object and league_players.json and
league_matchups_week.json are both top level JSON lists.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

import engine.identity as identity
import engine.yahoo_client as yc
import engine.yahoo_shapes as yahoo_shapes
from engine.common import REPO_ROOT
from engine.sources import base as sources_base
from engine.sources import injuries as sources_injuries
from engine.sources import sleeper as sources_sleeper

YAHOO_FIXTURES_DIR = REPO_ROOT / "fixtures" / "yahoo"
SAMPLE_LEAGUE_DIR = REPO_ROOT / "fixtures" / "sample_league"
SOURCES_FIXTURES_DIR = REPO_ROOT / "fixtures" / "sources"


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _players_fixture() -> list[dict[str, Any]]:
    return _load(YAHOO_FIXTURES_DIR / "league_players.json")


def _settings_fixture() -> dict[str, Any]:
    return _load(YAHOO_FIXTURES_DIR / "league_settings.json")


def _metadata_fixture() -> dict[str, Any]:
    return _load(YAHOO_FIXTURES_DIR / "league_metadata.json")


def _matchups_fixture() -> list[dict[str, Any]]:
    return _load(YAHOO_FIXTURES_DIR / "league_matchups_week.json")


def _roster_fixture() -> dict[str, Any]:
    return _load(YAHOO_FIXTURES_DIR / "team_roster_week.json")


def _sample_league_fixture() -> dict[str, Any]:
    return _load(SAMPLE_LEAGUE_DIR / "league.json")


def _sleeper_players_payload() -> dict[str, Any]:
    return _load(SOURCES_FIXTURES_DIR / "sleeper_players.json")


def _espn_injuries_payload() -> dict[str, Any]:
    return _load(SOURCES_FIXTURES_DIR / "espn_injuries.json")


def _assert_envelope_shape(result: dict[str, Any]) -> None:
    assert set(result.keys()) == set(sources_base.SOURCE_RESULT_KEYS)
    json.dumps(result)  # every source result must be plain, json.dumps-able JSON


class FakeYfpyQuery:
    """A minimal stand-in for a resolved yfpy query object.

    A plain class, not unittest.mock.Mock, so its call log is easy to
    read. Every method returns fixture JSON loaded fresh from
    fixtures/yahoo/ (never a yfpy model object, never anything this file
    constructed by hand), so this class proves nothing about yfpy itself,
    only that engine.yahoo_client's fetch functions correctly carry a
    query object's return values through _jsonify and the matching
    engine.yahoo_shapes parser.

    get_league_key is implemented even though no test below calls
    fetch_free_agents, because fetch_free_agents calls it before paging,
    and on a real (non fake) query object that call would itself reach
    Yahoo's network; a fully faithful fake needs it regardless of which
    fetch functions a given test exercises.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []

    def get_league_players(self, player_count_limit: int | None = None) -> Any:
        self.calls.append("get_league_players")
        return _players_fixture()

    def get_league_settings(self) -> Any:
        self.calls.append("get_league_settings")
        return _settings_fixture()

    def get_league_metadata(self) -> Any:
        self.calls.append("get_league_metadata")
        return _metadata_fixture()

    def get_league_matchups_by_week(self, week: int) -> Any:
        self.calls.append("get_league_matchups_by_week")
        return _matchups_fixture()

    def get_team_roster_by_week(self, team_id: str, week: Any) -> Any:
        self.calls.append("get_team_roster_by_week")
        return _roster_fixture()

    def get_league_key(self, season: Any = None) -> Any:
        self.calls.append("get_league_key")
        return "461.l.524458"


def _raise_build_query(**kwargs: Any) -> Any:
    raise AssertionError(
        "build_query must not be called: a query object was passed to every "
        "fetch call in this test module"
    )


@pytest.fixture
def blocked_build_query(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make engine.yahoo_client.build_query raise if it is ever called.

    Every test in this module passes query=<a FakeYfpyQuery instance> to
    every yc.fetch_* call, which is what should make build_query
    unreachable (see _resolved_query in engine/yahoo_client.py); this
    fixture turns "unreachable" into an enforced assertion instead of an
    assumption, and incidentally proves no credential is ever read, since
    build_query is the only path that reads one.
    """
    monkeypatch.setattr(yc, "build_query", _raise_build_query)


def _patched_sleeper_fetch(payload: dict[str, Any]) -> Any:
    return patch(
        "engine.sources.sleeper.fetch_cached_json",
        return_value=(payload, "2026-09-10T12:00:00Z", False),
    )


def _patched_injuries_fetch(payload: dict[str, Any]) -> Any:
    return patch(
        "engine.sources.injuries.fetch_cached_json",
        return_value=(payload, "2026-09-10T12:00:00Z", False),
    )


# ---------------------------------------------------------------------------
# 1. The full path: fetch_player_list -> identity_result.
# ---------------------------------------------------------------------------


def test_yahoo_player_list_feeds_the_identity_join(
    blocked_build_query: None, tmp_path: Path
) -> None:
    fake = FakeYfpyQuery()

    player_list_result = yc.fetch_player_list(
        league_id="524458", query=fake, player_count_limit=10
    )
    assert player_list_result["available"] is True
    yahoo_players = player_list_result["data"]
    # Sanity: this is really yahoo_shapes' own parse output, not something
    # this test hand built, so a later parser change is what this
    # assertion would actually catch.
    assert yahoo_players == yahoo_shapes.parse_player_list(_players_fixture())

    with _patched_sleeper_fetch(_sleeper_players_payload()):
        sleeper_envelope = sources_sleeper.fetch_player_index(cache_root=tmp_path)
    assert sleeper_envelope["available"] is True

    with _patched_injuries_fetch(_espn_injuries_payload()):
        injuries_envelope = sources_injuries.fetch_injuries(cache_root=tmp_path)
    assert injuries_envelope["available"] is True

    result = identity.identity_result(
        yahoo_players,
        sleeper_index_data=sleeper_envelope["data"],
        injuries_data=injuries_envelope["data"],
    )
    _assert_envelope_shape(result)

    data = result["data"]
    assert data["count"] == 6

    players_by_name = {record["name"]: record for record in data["players"].values()}

    josh_allen = players_by_name["Josh Allen"]
    assert josh_allen["sleeper_player_id"] is not None
    assert isinstance(josh_allen["sleeper_player_id"], str)

    james_conner = players_by_name["James Conner"]
    assert james_conner["injury"] is not None
    assert james_conner["injury"]["normalized_name"] == "james conner"

    # The specific assertion this test exists for: if fetch_player_list's
    # output shape ever drifted from what identity_result expects, the
    # join above would either raise or quietly join nothing, and this
    # would catch it either way, since a silent zero-count join would
    # fail the count == 6 assertion above already.
    json.dumps(result)

    assert fake.calls == ["get_league_players"]


# ---------------------------------------------------------------------------
# 2. fetch_league_settings' scoring keys agree with fixtures/sample_league/,
#    and "Targets" surfaces as unmapped rather than vanishing.
# ---------------------------------------------------------------------------


def test_league_settings_scoring_keys_match_sample_league_and_targets_is_unmapped(
    blocked_build_query: None,
) -> None:
    fake = FakeYfpyQuery()

    result = yc.fetch_league_settings(league_id="524458", query=fake)
    assert result["available"] is True
    data = result["data"]

    sample_league = _sample_league_fixture()
    expected_keys = set(sample_league["settings"]["scoring"]["stats"].keys())
    assert set(data["scoring"]["stats"].keys()) == expected_keys

    # "Targets" (stat_id 78) is deliberately given a modifier value of "0"
    # in fixtures/yahoo/league_settings.json's stat_modifiers but has no
    # matching key among the 15 valid scoring keys, so it must surface in
    # unmapped_stat_categories rather than silently disappear.
    unmapped_names = {entry["name"] for entry in data["unmapped_stat_categories"]}
    assert "Targets" in unmapped_names


# ---------------------------------------------------------------------------
# 3. fetch_matchups and fetch_rosters agree with fetch_league_settings on
#    slot naming.
# ---------------------------------------------------------------------------


def test_matchups_and_rosters_agree_with_settings_on_slot_names(
    blocked_build_query: None,
) -> None:
    fake = FakeYfpyQuery()

    matchups_result = yc.fetch_matchups(league_id="524458", week=3, query=fake)
    assert matchups_result["available"] is True
    assert matchups_result["data"]["owner_team_id"] == "1"
    assert len(matchups_result["data"]["matchups"]) == 2

    rosters_result = yc.fetch_rosters(
        league_id="524458", week=3, team_ids=["1"], query=fake
    )
    assert rosters_result["available"] is True
    assert rosters_result["data"]["failed_team_ids"] == []
    roster = rosters_result["data"]["rosters"][0]

    settings_result = yc.fetch_league_settings(league_id="524458", query=fake)
    assert settings_result["available"] is True
    slot_names = {slot["slot"] for slot in settings_result["data"]["roster_slots"]}

    selected_slots = {entry["selected_slot"] for entry in roster["roster"]}
    # This is the specific claim this test exists to check: the roster
    # parse and the settings parse must agree on slot naming, since
    # lineup.py has to look a roster's selected_slot up against the
    # league's own roster_slots to validate a start/sit call.
    assert selected_slots
    assert selected_slots <= slot_names


# ---------------------------------------------------------------------------
# 4. Package-wide guard: only engine/yahoo_client.py talks to Yahoo's host.
# ---------------------------------------------------------------------------


def test_only_yahoo_client_mentions_the_yahoo_api_host() -> None:
    # Enforces docs/plan.md's rule that engine/yahoo_client.py is the only
    # module in this repo allowed to talk to Yahoo. Every other module
    # under engine/ must parse or consume already-fetched data instead of
    # embedding Yahoo's own API host, so a future change that starts
    # calling Yahoo from a second module would be caught here.
    host = "fantasysports.yahooapis.com"
    yahoo_client_path = Path("engine") / "yahoo_client.py"
    offending_files = []
    for path in sorted((REPO_ROOT / "engine").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if host in text and path.relative_to(REPO_ROOT) != yahoo_client_path:
            offending_files.append(str(path.relative_to(REPO_ROOT)))

    assert offending_files == []
    # Not vacuous: the host string really is present in yahoo_client.py,
    # so this guard is actually exercising something.
    assert host in (REPO_ROOT / "engine" / "yahoo_client.py").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 5. Package-wide guard: no engine module contains a write-verb call site.
# ---------------------------------------------------------------------------


def test_no_engine_module_contains_a_write_verb_call_site() -> None:
    # Every module that talks to Yahoo is read only by construction, so
    # none of them should ever contain a call-site substring for an HTTP
    # write verb. Deliberately written as call-site substrings (".post(",
    # not the bare word "post") because Yahoo's own league settings
    # legitimately contain a key named post_draft_players (see
    # fixtures/yahoo/league_settings.json and engine/yahoo_shapes.py's
    # docstrings), and a bare-word check would false-positive on that key
    # name appearing in a docstring or comment.
    #
    # Scoped to the Yahoo-facing modules on purpose, not every module
    # under engine/: engine/notify.py is a deliberate, legitimate write
    # call site (it POSTs to Brevo's API, and curl-uploads to SMTP), so it
    # is excluded here rather than left to dodge this scan with a
    # deliberately omitted method="POST" kwarg, which is what it used to
    # do (see engine/notify.py's own send_via_brevo, which spells the
    # kwarg out).
    write_verb_call_sites = (
        ".post(",
        ".put(",
        ".patch(",
        ".delete(",
        'method="POST"',
        'method="PUT"',
        'method="DELETE"',
    )
    scanned_paths = [
        REPO_ROOT / "engine" / "yahoo_client.py",
        REPO_ROOT / "engine" / "yahoo_shapes.py",
        *sorted((REPO_ROOT / "engine" / "sources").rglob("*.py")),
    ]
    offenders: list[tuple[str, str]] = []
    for path in scanned_paths:
        text = path.read_text(encoding="utf-8")
        for verb in write_verb_call_sites:
            if verb in text:
                offenders.append((str(path.relative_to(REPO_ROOT)), verb))

    assert offenders == []
