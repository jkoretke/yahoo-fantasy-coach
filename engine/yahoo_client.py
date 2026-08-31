"""The only module in this repo that talks to the Yahoo Fantasy Sports API.

Every function here issues a read only GET request through yfpy's query
client. There is no write path anywhere in this module, so a bug in this
repo cannot submit a roster move, a waiver claim, or any other change to a
real Yahoo league. Parsing a Yahoo response into this repo's own shapes is
deliberately kept out of this file and lives in the pure engine.yahoo_shapes
module instead, so that parsing logic stays testable from recorded fixtures
without ever importing yfpy or touching the network.

Yahoo's Fantasy Sports API access application for this project was still
pending review when this module was written. Until Yahoo approves it, every
real call to a Yahoo Fantasy Sports endpoint fails. So every code path in
this module is verified against a mocked yfpy client only; nothing here has
been exercised against a live Yahoo account, and nothing in this repo's test
suite may attempt to.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from engine.common import EngineError, load_secrets, require_secret, timestamp
from engine.sources.base import (
    SourceUnavailable,
    disabled_result,
    source_result,
    unavailable_result,
)
from engine.yahoo_shapes import (
    parse_free_agents,
    parse_league_metadata,
    parse_league_settings,
    parse_matchups,
    parse_player_list,
    parse_roster,
)

SOURCE_NAME: str = "yahoo"
GAME_CODE: str = "nfl"

# The secret keys the owner already saved to
# ~/.config/yahoo-fantasy-coach/secrets.env for the Yahoo developer app.
YAHOO_CLIENT_ID_KEY: str = "YAHOO_CLIENT_ID"
YAHOO_CLIENT_SECRET_KEY: str = "YAHOO_CLIENT_SECRET"

# Home relative default for the refresh token cache, following the same
# house pattern engine.brief.brief_path's runs_root and
# engine.sources.base's cache_root parameters already use: a home relative
# default with a parameter tests can override with a tmp path.
DEFAULT_TOKEN_DIR: Path = Path.home() / ".config" / "yahoo-fantasy-coach"

# yfpy hardcodes the name ".env" when it loads the token file at
# env_file_location, so this name is not freely changeable without also
# forking yfpy's own read path.
TOKEN_ENV_FILE_NAME: str = ".env"


class YahooUnavailable(SourceUnavailable):
    """A Yahoo read failed this run.

    The most likely cause is that the Fantasy Sports API access application
    for this project has not been approved yet: Yahoo answers 401 to every
    endpoint until it is.
    """


def token_dir_path(token_dir: Path | None = None) -> Path:
    """Return token_dir, or DEFAULT_TOKEN_DIR when token_dir is not given."""
    if token_dir is None:
        return DEFAULT_TOKEN_DIR
    return token_dir


def token_file_path(token_dir: Path | None = None) -> Path:
    """Return the path to the cached OAuth2 token env file under token_dir."""
    return token_dir_path(token_dir) / TOKEN_ENV_FILE_NAME


def token_is_cached(token_dir: Path | None = None) -> bool:
    """Return True when a usable refresh token is already cached on disk.

    This reads the token file looking for a non blank YAHOO_REFRESH_TOKEN
    line. It never raises: a missing file, an unreadable file, or a file
    with no such line all read as False. Neither this function nor any
    caller of it may log or return the token value itself.
    """
    path = token_file_path(token_dir)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() == "YAHOO_REFRESH_TOKEN" and value.strip():
            return True
    return False


def harden_token_file(token_dir: Path | None = None) -> None:
    """Best effort tighten permissions on the token directory and file.

    yfpy writes YAHOO_CONSUMER_KEY and YAHOO_CONSUMER_SECRET into this file
    alongside the rotating refresh token, so it holds a static credential
    and not only a rotating one. It must not be left world readable. This
    chmods the token directory to 0o700 and the token file, if present, to
    0o600, swallowing any OSError: a failure here must never turn an
    otherwise successful call into a failed one.
    """
    directory = token_dir_path(token_dir)
    try:
        directory.chmod(0o700)
    except OSError:
        pass

    path = token_file_path(token_dir)
    try:
        path.chmod(0o600)
    except OSError:
        pass


def yahoo_credentials(secrets_path: Path | None = None) -> tuple[str, str]:
    """Return (client_id, client_secret) loaded from the repo's secrets file.

    Raises EngineError with a clear message when either key is missing or
    blank, so a misconfigured credential fails with an actionable message
    before yfpy ever gets a chance to call sys.exit(1) on it.
    """
    values = load_secrets(secrets_path)
    client_id = require_secret(values, YAHOO_CLIENT_ID_KEY)
    client_secret = require_secret(values, YAHOO_CLIENT_SECRET_KEY)
    return client_id, client_secret


def _query_class() -> Any:
    """Import and return yfpy.query.YahooFantasySportsQuery.

    This is the single seam tests patch; no test in this repo may import
    yfpy itself, only monkeypatch this function. The import is deliberately
    lazy (kept inside this function rather than at module level) because
    yfpy 17.0.0 requires Python 3.10 or newer, while this repo's own
    virtualenv today is Python 3.9, so importing yfpy at module import time
    would break every other module that imports engine.yahoo_client.
    """
    try:
        import yfpy.query
    except ImportError as exc:
        raise EngineError(
            "yfpy is not installed or not importable in this environment. "
            "This repo pins yfpy==17.0.0, which requires Python 3.10 or "
            "newer, but the repo's current virtualenv is Python 3.9. "
            "Recreate .venv on Python 3.10+ and install "
            "yfpy==17.0.0 to use engine.yahoo_client."
        ) from exc
    return yfpy.query.YahooFantasySportsQuery


def build_query(
    *,
    league_id: str,
    season: int | None = None,
    game_id: int | None = None,
    secrets_path: Path | None = None,
    token_dir: Path | None = None,
    browser_callback: bool = True,
    env_var_fallback: bool = True,
    offline: bool = False,
    retries: int = 3,
    backoff: int = 0,
) -> Any:
    """Build and return an authenticated yfpy YahooFantasySportsQuery.

    Constructing this object performs the OAuth2 handshake as a side
    effect. On a first run, with no refresh token already cached under
    token_dir, that handshake requires a one time interactive browser
    sign-in by the account owner: visiting Yahoo's consent screen, approving
    access, and completing the flow. That step cannot be automated by this
    repo or by an agent, and it cannot succeed at all until Yahoo approves
    this project's pending Fantasy Sports API access application.

    season is deliberately not passed to the constructor: yfpy does not
    take it there. A caller instead passes season through to the later
    fetch functions built on top of this query object, which call yfpy's
    own get_league_key(season) to resolve the season specific league key.

    Loading the client id and secret, and creating the token directory, both
    happen outside and before the try block below. A missing credential is
    a configuration error, not a Yahoo outage, and its EngineError must
    propagate unchanged: keeping that call outside the try is what
    guarantees this, since YahooUnavailable is itself an EngineError (and
    an Exception), so any broad except wrapped around it would risk
    swallowing the wrong thing.
    """
    client_id, client_secret = yahoo_credentials(secrets_path)
    token_dir_path(token_dir).mkdir(parents=True, exist_ok=True)

    try:
        query = _query_class()(
            league_id=str(league_id),
            game_code=GAME_CODE,
            game_id=game_id,
            yahoo_consumer_key=client_id,
            yahoo_consumer_secret=client_secret,
            env_var_fallback=env_var_fallback,
            env_file_location=token_dir_path(token_dir),
            save_token_data_to_env_file=True,
            browser_callback=browser_callback,
            retries=retries,
            backoff=backoff,
            offline=offline,
        )
    except SystemExit as exc:
        # yfpy calls sys.exit(1) on a missing or malformed credential or
        # token instead of raising. SystemExit derives from BaseException,
        # not Exception, so it must be caught explicitly here or it would
        # kill the whole run rather than degrade this one source.
        raise YahooUnavailable(
            "yfpy exited the process while authenticating with Yahoo "
            "(a missing or malformed credential or cached token). This is "
            "also the expected failure while this project's Fantasy Sports "
            "API access application is still pending Yahoo's review."
        ) from exc
    except Exception as exc:
        raise YahooUnavailable(f"Yahoo authentication failed: {exc}") from exc

    harden_token_file(token_dir)
    return query


def _jsonify(value: Any) -> Any:
    """Convert a yfpy model, or a list of yfpy models, into plain JSON.

    A value that is already a plain dict, or a list whose elements are
    all plain dicts (an empty list qualifies too, vacuously), is returned
    unchanged, without ever importing yfpy. This is the seam that lets a
    test feed fixture JSON straight through a fake query object with no
    yfpy import anywhere in the process.

    Anything else is assumed to be a real yfpy model object, or a list of
    them, and is converted with
    json.loads(yfpy.utils.jsonify_data(value)). Verified: jsonify_data's
    default JSON encoder calls .serialized() on any object exposing it,
    so this one call works for a single bare model and for a list of
    models alike.

    Raises EngineError, with the same message _query_class raises for a
    missing yfpy, if the lazy yfpy import fails here. That can only
    happen on a real (non fixture) value in an environment where yfpy is
    not importable, which today means this repo's own Python 3.9
    virtualenv.
    """
    if isinstance(value, dict):
        return value
    if isinstance(value, list) and all(isinstance(item, dict) for item in value):
        return value

    try:
        from yfpy.utils import jsonify_data
    except ImportError as exc:
        raise EngineError(
            "yfpy is not installed or not importable in this environment. "
            "This repo pins yfpy==17.0.0, which requires Python 3.10 or "
            "newer, but the repo's current virtualenv is Python 3.9. "
            "Recreate .venv on Python 3.10+ and install "
            "yfpy==17.0.0 to use engine.yahoo_client."
        ) from exc
    return json.loads(jsonify_data(value))


def _resolved_query(query: Any | None, **build_kwargs: Any) -> Any:
    """Return query unchanged when one was given, otherwise build a fresh one.

    This is what lets a caller reuse one authenticated query object across
    a whole run's worth of fetch calls (build_query performs a real OAuth2
    handshake as a side effect, so it should not be called once per fetch
    in normal use), and what lets a test inject a fake query object with
    no yfpy import anywhere: whenever query is not None, build_query is
    never called.
    """
    if query is not None:
        return query
    return build_query(**build_kwargs)


def _run_yahoo_call(call: Any) -> tuple[bool, Any]:
    """Run call() under this module's Yahoo exception contract.

    Returns (True, call()'s return value) on success. Returns (False, an
    unavailable_result(SOURCE_NAME, reason) envelope) when call() failed
    in a way this module treats as a degraded Yahoo source for this run
    rather than a raised error.

    The except clauses below are ordered on purpose and must stay in this
    exact order. YahooUnavailable subclasses SourceUnavailable, which
    subclasses EngineError, which subclasses RuntimeError, so
    YahooUnavailable IS an EngineError and IS an Exception: clause order,
    not type membership, is what makes the intended behavior possible.

      1. YahooUnavailable is caught first and turned into an unavailable
         envelope. This is the expected shape of "Yahoo could not be read
         this run" (for example: this project's Fantasy Sports API access
         application is still pending, so a real call answers 401 to
         every endpoint).
      2. EngineError is re-raised, not caught. A configuration problem
         (a missing or blank credential, or this environment's yfpy not
         being importable) is not a Yahoo outage, so it must surface as a
         real exception instead of quietly degrading like one. This
         clause MUST sit strictly between YahooUnavailable and the bare
         Exception clause below: if it came before YahooUnavailable it
         would swallow every Yahoo outage as an unhandled EngineError
         instead of degrading gracefully, and the bare Exception clause
         below would swallow every configuration error as a degraded
         source instead of raising it if this clause were removed or
         placed after it.
      3. SystemExit gets its own clause because it derives from
         BaseException, not Exception: yfpy calls sys.exit(1) on a bad
         credential or cached token instead of raising, so a bare
         `except Exception` below would not catch it, and it would
         otherwise kill this whole run instead of degrading one source.
      4. Any other Exception is the catch-all for a genuine, unexpected
         Yahoo-side failure (a network error, a malformed response, and
         so on).
    """
    try:
        return True, call()
    except YahooUnavailable as exc:
        return False, unavailable_result(SOURCE_NAME, str(exc))
    except EngineError:
        raise
    except SystemExit as exc:
        return False, unavailable_result(
            SOURCE_NAME, f"Yahoo call exited the process: {exc}"
        )
    except Exception as exc:
        return False, unavailable_result(SOURCE_NAME, str(exc))


def fetch_league_settings(
    *,
    enabled: bool = True,
    query: Any | None = None,
    league_id: str,
    season: int | None = None,
    game_id: int | None = None,
    secrets_path: Path | None = None,
    token_dir: Path | None = None,
    browser_callback: bool = True,
) -> dict[str, Any]:
    """Fetch this league's settings: scoring, roster slots, waiver type.

    Calls query.get_league_settings() on the resolved query (a passed in
    query object is reused as is; otherwise one is built through
    build_query, which performs a real OAuth2 handshake as a side
    effect), converts the result to plain JSON with _jsonify, and returns
    engine.yahoo_shapes.parse_league_settings(payload) as data.

    Returns disabled_result(SOURCE_NAME) immediately, with zero network
    or disk access and no credential read, when enabled is False.
    Otherwise this follows this module's exception contract: a
    YahooUnavailable from resolving the query or from the call itself
    degrades to unavailable_result(SOURCE_NAME, reason); an EngineError
    (a configuration problem, such as a missing credential) is re-raised
    unchanged, never degraded; a SystemExit from yfpy (it calls
    sys.exit(1) on a bad credential or token) is converted to
    unavailable_result rather than killing this run; any other Exception
    also degrades to unavailable_result. See _run_yahoo_call for why the
    except clauses have to stay in that exact order.

    stale is always False in the returned envelope: this module does no
    caching of its own (a later phase may add one).
    """
    if not enabled:
        return disabled_result(SOURCE_NAME)

    def _call() -> dict[str, Any]:
        resolved = _resolved_query(
            query,
            league_id=league_id,
            season=season,
            game_id=game_id,
            secrets_path=secrets_path,
            token_dir=token_dir,
            browser_callback=browser_callback,
        )
        payload = _jsonify(resolved.get_league_settings())
        return parse_league_settings(payload)

    ok, result = _run_yahoo_call(_call)
    if not ok:
        return result
    return source_result(SOURCE_NAME, data=result, fetched_at=timestamp())


def fetch_league_metadata(
    *,
    enabled: bool = True,
    query: Any | None = None,
    league_id: str,
    season: int | None = None,
    game_id: int | None = None,
    secrets_path: Path | None = None,
    token_dir: Path | None = None,
    browser_callback: bool = True,
) -> dict[str, Any]:
    """Fetch this league's scalar metadata: name, season, current week, and so on.

    Calls query.get_league_metadata() on the resolved query (a passed in
    query object is reused as is; otherwise one is built through
    build_query), converts the result to plain JSON with _jsonify, and
    returns engine.yahoo_shapes.parse_league_metadata(payload) as data.

    Returns disabled_result(SOURCE_NAME) immediately, with zero network
    or disk access and no credential read, when enabled is False.
    Otherwise this follows this module's exception contract in full: a
    YahooUnavailable degrades to unavailable_result(SOURCE_NAME, reason);
    an EngineError (a configuration problem) is re-raised unchanged; a
    SystemExit from yfpy is converted to unavailable_result rather than
    killing this run; any other Exception also degrades to
    unavailable_result. See _run_yahoo_call for the exact except clause
    ordering this depends on and why it matters.

    stale is always False in the returned envelope: this module does no
    caching of its own (a later phase may add one).
    """
    if not enabled:
        return disabled_result(SOURCE_NAME)

    def _call() -> dict[str, Any]:
        resolved = _resolved_query(
            query,
            league_id=league_id,
            season=season,
            game_id=game_id,
            secrets_path=secrets_path,
            token_dir=token_dir,
            browser_callback=browser_callback,
        )
        payload = _jsonify(resolved.get_league_metadata())
        return parse_league_metadata(payload)

    ok, result = _run_yahoo_call(_call)
    if not ok:
        return result
    return source_result(SOURCE_NAME, data=result, fetched_at=timestamp())


def fetch_rosters(
    *,
    enabled: bool = True,
    query: Any | None = None,
    league_id: str,
    season: int | None = None,
    game_id: int | None = None,
    secrets_path: Path | None = None,
    token_dir: Path | None = None,
    browser_callback: bool = True,
    week: int,
    team_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Fetch one or more teams' rosters for a single week.

    Calls query.get_team_roster_by_week(team_id, week) once per id in
    team_ids, in order, on the resolved query (a passed in query object
    is reused as is; otherwise one is built through build_query, once,
    before the per-team loop starts). Each result is converted to plain
    JSON with _jsonify and parsed with
    engine.yahoo_shapes.parse_roster(payload, team_id=team_id). Returns
    data shaped {"week": int(week), "rosters": [<parse_roster
    result>, ...], "failed_team_ids": [...]}.

    team_ids is required: this function deliberately does not chain off
    another fetch to discover which teams to ask for, so a caller passes
    it explicitly, typically the team_ids already visible in
    fetch_matchups's own output. Passing team_ids=None returns
    unavailable_result(SOURCE_NAME, "team_ids required") rather than
    fetching anything.

    A single team's roster call failing (a YahooUnavailable, a
    SystemExit, or any other Exception, per this module's exception
    contract; see _run_yahoo_call) does not fail the whole call: that
    team's id is recorded in failed_team_ids and the remaining team_ids
    are still attempted. An EngineError (a configuration problem) still
    propagates unchanged rather than being recorded as a per-team
    failure, whether it comes from resolving the query up front or from
    one team's own call.

    Returns disabled_result(SOURCE_NAME) immediately, with zero network
    or disk access and no credential read, when enabled is False. This
    check, and the team_ids check above it, both happen before the query
    is ever resolved.

    stale is always False in the returned envelope: this module does no
    caching of its own (a later phase may add one).
    """
    if not enabled:
        return disabled_result(SOURCE_NAME)

    if team_ids is None:
        return unavailable_result(SOURCE_NAME, "team_ids required")

    def _resolve() -> Any:
        return _resolved_query(
            query,
            league_id=league_id,
            season=season,
            game_id=game_id,
            secrets_path=secrets_path,
            token_dir=token_dir,
            browser_callback=browser_callback,
        )

    resolved_ok, resolved = _run_yahoo_call(_resolve)
    if not resolved_ok:
        return resolved

    rosters: list[dict[str, Any]] = []
    failed_team_ids: list[str] = []
    for team_id in team_ids:

        def _call(team_id: str = team_id) -> dict[str, Any]:
            payload = _jsonify(resolved.get_team_roster_by_week(team_id, week))
            return parse_roster(payload, team_id=team_id)

        team_ok, team_result = _run_yahoo_call(_call)
        if team_ok:
            rosters.append(team_result)
        else:
            failed_team_ids.append(team_id)

    data = {"week": int(week), "rosters": rosters, "failed_team_ids": failed_team_ids}
    return source_result(SOURCE_NAME, data=data, fetched_at=timestamp())


def fetch_matchups(
    *,
    enabled: bool = True,
    query: Any | None = None,
    league_id: str,
    season: int | None = None,
    game_id: int | None = None,
    secrets_path: Path | None = None,
    token_dir: Path | None = None,
    browser_callback: bool = True,
    week: int,
) -> dict[str, Any]:
    """Fetch every matchup for one week of this league.

    Calls query.get_league_matchups_by_week(int(week)) on the resolved
    query (a passed in query object is reused as is; otherwise one is
    built through build_query), converts the result to plain JSON with
    _jsonify, and returns
    engine.yahoo_shapes.parse_matchups(payload, week=int(week)) as data.

    Returns disabled_result(SOURCE_NAME) immediately, with zero network
    or disk access and no credential read, when enabled is False.
    Otherwise this follows this module's exception contract in full: a
    YahooUnavailable degrades to unavailable_result(SOURCE_NAME, reason);
    an EngineError (a configuration problem) is re-raised unchanged; a
    SystemExit from yfpy is converted to unavailable_result rather than
    killing this run; any other Exception also degrades to
    unavailable_result. See _run_yahoo_call for the exact except clause
    ordering this depends on and why it matters.

    stale is always False in the returned envelope: this module does no
    caching of its own (a later phase may add one).
    """
    if not enabled:
        return disabled_result(SOURCE_NAME)

    def _call() -> dict[str, Any]:
        resolved = _resolved_query(
            query,
            league_id=league_id,
            season=season,
            game_id=game_id,
            secrets_path=secrets_path,
            token_dir=token_dir,
            browser_callback=browser_callback,
        )
        payload = _jsonify(resolved.get_league_matchups_by_week(int(week)))
        return parse_matchups(payload, week=int(week))

    ok, result = _run_yahoo_call(_call)
    if not ok:
        return result
    return source_result(SOURCE_NAME, data=result, fetched_at=timestamp())


# Yahoo's own page size for the generic query.query GET fetch_free_agents
# pages against; not configurable, since it is what Yahoo's endpoint uses
# to decide it has handed back a short (final) page.
_FREE_AGENTS_PAGE_SIZE: int = 25

# A hard stop on fetch_free_agents's paging loop so a misbehaving page (one
# that never comes back short and never raises) can never spin forever.
# 40 iterations at _FREE_AGENTS_PAGE_SIZE each is 1000 players.
_FREE_AGENTS_MAX_PAGES: int = 40


def fetch_free_agents(
    *,
    enabled: bool = True,
    query: Any | None = None,
    league_id: str,
    season: int | None = None,
    game_id: int | None = None,
    secrets_path: Path | None = None,
    token_dir: Path | None = None,
    browser_callback: bool = True,
    limit: int = 50,
) -> dict[str, Any]:
    """Fetch up to limit free agent players.

    yfpy has no dedicated free-agent method, so this pages Yahoo's
    generic query.query(url, ["league", "players"]) GET against
    https://fantasysports.yahooapis.com/fantasy/v2/league/{league_key}
    /players;status=FA;start={n};count=25, _FREE_AGENTS_PAGE_SIZE (25)
    players per HTTP request. Request cost: one call to
    query.get_league_key(season) to resolve the league key, plus one
    query.query call per page actually fetched (a limit of 50 costs 2 of
    those, a limit of 1 still costs 1, since a partial page is still a
    full request).

    Each page's result is normalized with
    page if isinstance(page, list) else [page] before being added to the
    running total: yfpy hands back a bare model, not a one item list,
    when a page happens to contain exactly one player, and this repo's
    own yfpy version check verified that get_league_players guards the
    same way internally. Paging stops as soon as any one of three things
    happens: the running total reaches limit, a page comes back with
    fewer than _FREE_AGENTS_PAGE_SIZE entries (Yahoo's own signal that it
    was the last page), or the per-page call raises. A raise on the very
    first page (nothing collected yet) is a real failure and propagates
    out of this loop into this module's normal exception contract; a
    raise on any later page (after at least one page already succeeded)
    is instead treated as end-of-pages, since that is also how yfpy's own
    get_league_players recognizes the end of the player pool: it catches
    yfpy.exceptions.YahooFantasySportsDataNotFound, a type this module
    cannot import without yfpy itself, so a bare `except Exception`
    stands in for it here. The loop is additionally hard capped at
    _FREE_AGENTS_MAX_PAGES iterations so a page that never comes back
    short, and never raises, cannot spin forever.

    The collected list is trimmed to limit, converted to plain JSON with
    _jsonify, and returned as
    engine.yahoo_shapes.parse_free_agents(payload).

    Returns disabled_result(SOURCE_NAME) immediately, with zero network
    or disk access and no credential read, when enabled is False.
    Otherwise this follows this module's exception contract in full (see
    _run_yahoo_call for the exact except clause ordering and why it
    matters): a YahooUnavailable degrades to unavailable_result; an
    EngineError (a configuration problem) is re-raised unchanged; a
    SystemExit from yfpy is converted to unavailable_result rather than
    killing this run; any other Exception also degrades to
    unavailable_result.

    stale is always False in the returned envelope: this module does no
    caching of its own (a later phase may add one).
    """
    if not enabled:
        return disabled_result(SOURCE_NAME)

    def _call() -> dict[str, Any]:
        resolved = _resolved_query(
            query,
            league_id=league_id,
            season=season,
            game_id=game_id,
            secrets_path=secrets_path,
            token_dir=token_dir,
            browser_callback=browser_callback,
        )
        league_key = resolved.get_league_key(season)

        collected: list[Any] = []
        page_count = 0
        while len(collected) < limit and page_count < _FREE_AGENTS_MAX_PAGES:
            start = page_count * _FREE_AGENTS_PAGE_SIZE
            url = (
                "https://fantasysports.yahooapis.com/fantasy/v2/league/"
                f"{league_key}/players;status=FA;start={start};"
                f"count={_FREE_AGENTS_PAGE_SIZE}"
            )
            try:
                page = resolved.query(url, ["league", "players"])
            except Exception:
                if collected:
                    break
                raise
            page_count += 1
            page_list = page if isinstance(page, list) else [page]
            collected.extend(page_list)
            if len(page_list) < _FREE_AGENTS_PAGE_SIZE:
                break

        payload = _jsonify(collected[:limit])
        return parse_free_agents(payload)

    ok, result = _run_yahoo_call(_call)
    if not ok:
        return result
    return source_result(SOURCE_NAME, data=result, fetched_at=timestamp())


def fetch_player_list(
    *,
    enabled: bool = True,
    query: Any | None = None,
    league_id: str,
    season: int | None = None,
    game_id: int | None = None,
    secrets_path: Path | None = None,
    token_dir: Path | None = None,
    browser_callback: bool = True,
    player_count_limit: int | None = None,
) -> dict[str, Any]:
    """Fetch this league's player pool. THIS IS WHAT FEEDS THE PLAYER IDENTITY JOIN.

    OMITTING player_count_limit PAGES YAHOO'S ENTIRE PLAYER POOL, AT 25
    PLAYERS PER HTTP REQUEST. A single NFL season's Yahoo player pool
    runs to several thousand players, so a call with no
    player_count_limit is several THOUSAND HTTP requests, not a handful.
    A caller should always pass an explicit player_count_limit unless it
    genuinely needs every player Yahoo knows about for this game code;
    never call this with player_count_limit=None as a convenience or a
    default.

    Calls query.get_league_players(player_count_limit=player_count_limit)
    on the resolved query (a passed in query object is reused as is;
    otherwise one is built through build_query). That paging, and its own
    per-batch retry fallback, belong to yfpy and are deliberately not
    reimplemented here. The result is converted to plain JSON with
    _jsonify and returned as
    engine.yahoo_shapes.parse_player_list(payload).

    Returns disabled_result(SOURCE_NAME) immediately, with zero network
    or disk access and no credential read, when enabled is False.
    Otherwise this follows this module's exception contract in full: a
    YahooUnavailable degrades to unavailable_result(SOURCE_NAME, reason);
    an EngineError (a configuration problem) is re-raised unchanged; a
    SystemExit from yfpy is converted to unavailable_result rather than
    killing this run; any other Exception also degrades to
    unavailable_result. See _run_yahoo_call for the exact except clause
    ordering this depends on and why it matters.

    stale is always False in the returned envelope: this module does no
    caching of its own (a later phase may add one).
    """
    if not enabled:
        return disabled_result(SOURCE_NAME)

    def _call() -> dict[str, Any]:
        resolved = _resolved_query(
            query,
            league_id=league_id,
            season=season,
            game_id=game_id,
            secrets_path=secrets_path,
            token_dir=token_dir,
            browser_callback=browser_callback,
        )
        payload = _jsonify(
            resolved.get_league_players(player_count_limit=player_count_limit)
        )
        return parse_player_list(payload)

    ok, result = _run_yahoo_call(_call)
    if not ok:
        return result
    return source_result(SOURCE_NAME, data=result, fetched_at=timestamp())
