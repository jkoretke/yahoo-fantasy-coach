"""Shared fixtures for the whole tests/ suite.

Two autouse fixtures apply to every test in this directory, together
enforcing three guards, none of them requiring a test to opt in:

block_real_network (guard 1): engine.sources.* modules make real network
    calls at runtime by design, but the test suite must never make one: a
    flaky or rate limited external API must never make CI flaky, and a test
    that silently reaches the real network is not actually testing the
    mocked behavior it claims to test. A test that legitimately exercises
    the HTTP path patches urllib.request.urlopen itself, typically with
    unittest.mock.patch. Because that patch is applied after this fixture
    already ran, it cleanly overrides the fixture's patch for the duration
    of that one test, so the guard costs those tests nothing.

block_real_yahoo_token_dir enforces the other two guards:

    guard 2, the token directory redirect: engine.yahoo_client.
    DEFAULT_TOKEN_DIR is monkeypatched to a temporary directory and
    YAHOO_FANTASY_COACH_SECRETS_FILE is set to a nonexistent temp path, so
    a test that forgets to pass an explicit token_dir or secrets_path can
    never read or write the owner's real ~/.config/yahoo-fantasy-coach/.

    guard 3, the credential env var clearing: every YAHOO_* environment
    variable this repo or yfpy reads is deleted for the duration of each
    test, so a value left over in the real shell environment can never
    leak into a test.

    See that fixture's own docstring for why each half is required.

engine.yahoo_client is imported at module level below. That import is safe
even on this repo's Python 3.9 virtualenv, where yfpy is not installed,
because engine.yahoo_client only imports yfpy lazily, inside its own
_query_class function.
"""
from __future__ import annotations

import urllib.request

import pytest

import engine.yahoo_client


@pytest.fixture(autouse=True)
def block_real_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make a real urllib.request.urlopen call fail every test, by default."""

    def _refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError(
            "the test suite must never make a real network call; "
            "patch urllib.request.urlopen or engine.sources.base.fetch_json instead"
        )

    monkeypatch.setattr(urllib.request, "urlopen", _refuse)


@pytest.fixture(autouse=True)
def block_real_yahoo_token_dir(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep every test off the owner's real Yahoo credentials and token cache.

    engine.yahoo_client.build_query passes save_token_data_to_env_file=True
    to yfpy, so any test that fell through to the real
    engine.yahoo_client.DEFAULT_TOKEN_DIR would write a live OAuth token
    file into the owner's real ~/.config/yahoo-fantasy-coach/. Part (a)
    below redirects DEFAULT_TOKEN_DIR to a session scoped temp directory so
    that can never happen, even for a test that does not pass an explicit
    token_dir.

    Part (b) SETS YAHOO_FANTASY_COACH_SECRETS_FILE to a path inside that
    same temp directory that does not exist. Setting it, rather than
    deleting it, is mandatory: engine.common.load_secrets falls back to
    ~/.config/yahoo-fantasy-coach/secrets.env when that environment
    variable is absent, and on this machine that file exists and holds the
    owner's real YAHOO_CLIENT_ID and YAHOO_CLIENT_SECRET. Deleting the
    variable instead of pointing it at a nonexistent path would make every
    test that omits an explicit secrets_path silently read those real
    credentials, so whether the missing-credential tests pass or fail would
    depend on which developer's machine ran them.

    Part (c) deletes every YAHOO_* environment variable yfpy or this
    module's own key names read, so a variable left over in the shell
    environment from a real sign-in can never leak into a test.
    """
    temp_root = tmp_path_factory.mktemp("yahoo-fantasy-coach-token-dir")
    monkeypatch.setattr(engine.yahoo_client, "DEFAULT_TOKEN_DIR", temp_root)
    monkeypatch.setenv(
        "YAHOO_FANTASY_COACH_SECRETS_FILE", str(temp_root / "no-such-secrets.env")
    )

    for name in (
        "YAHOO_ACCESS_TOKEN",
        "YAHOO_ACCESS_TOKEN_JSON",
        "YAHOO_REFRESH_TOKEN",
        "YAHOO_GUID",
        "YAHOO_TOKEN_TIME",
        "YAHOO_TOKEN_TYPE",
        "YAHOO_CONSUMER_KEY",
        "YAHOO_CONSUMER_SECRET",
        "YAHOO_CLIENT_ID",
        "YAHOO_CLIENT_SECRET",
    ):
        monkeypatch.delenv(name, raising=False)
