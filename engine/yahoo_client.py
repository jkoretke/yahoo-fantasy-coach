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

from pathlib import Path
from typing import Any

from engine.common import EngineError, load_secrets, require_secret
from engine.sources.base import SourceUnavailable

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
