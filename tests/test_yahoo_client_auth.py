"""Tests for engine.yahoo_client's credentials, token cache and read-only guarantee.

This module never imports yfpy. Yahoo's Fantasy Sports API access
application for this project was pending review when this test module was
written, so every yfpy interaction below is against a fake, monkeypatched
query class, never a real or live one. The block_real_network and
block_real_yahoo_token_dir fixtures in tests/conftest.py apply to every test
here automatically.
"""
from __future__ import annotations

import stat
import sys
from pathlib import Path
from typing import Any

import pytest

import engine.yahoo_client
from engine.common import REPO_ROOT, EngineError
from engine.yahoo_client import (
    YAHOO_CLIENT_ID_KEY,
    YAHOO_CLIENT_SECRET_KEY,
    YahooUnavailable,
    _query_class,
    build_query,
    harden_token_file,
    token_dir_path,
    token_file_path,
    token_is_cached,
    yahoo_credentials,
)


def _write_secrets(path: Path, client_id: str = "id-123", client_secret: str = "secret-456") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"{YAHOO_CLIENT_ID_KEY}={client_id}\n{YAHOO_CLIENT_SECRET_KEY}={client_secret}\n",
        encoding="utf-8",
    )


class _FakeQuery:
    """A stand-in for yfpy.query.YahooFantasySportsQuery that records kwargs."""

    last_kwargs: dict[str, Any] | None = None

    def __init__(self, **kwargs: Any) -> None:
        type(self).last_kwargs = kwargs
        env_file_location = kwargs.get("env_file_location")
        if env_file_location is not None and kwargs.get("save_token_data_to_env_file"):
            token_path = Path(env_file_location) / ".env"
            token_path.write_text("YAHOO_REFRESH_TOKEN=abc\n", encoding="utf-8")


class _SystemExitQuery:
    def __init__(self, **kwargs: Any) -> None:
        raise SystemExit(1)


class _BoomQuery:
    def __init__(self, **kwargs: Any) -> None:
        raise ValueError("boom")


# --- token_dir_path / token_file_path -------------------------------------


def test_token_dir_path_defaults_to_default_token_dir() -> None:
    # Read engine.yahoo_client.DEFAULT_TOKEN_DIR live rather than a name
    # imported at module load time: tests/conftest.py's
    # block_real_yahoo_token_dir fixture monkeypatches that module
    # attribute to a temp directory for the duration of every test, and a
    # copied-in import would still hold the pre-patch value.
    assert token_dir_path(None) == engine.yahoo_client.DEFAULT_TOKEN_DIR


def test_token_dir_path_honors_override(tmp_path: Path) -> None:
    override = tmp_path / "custom-token-dir"
    assert token_dir_path(override) == override


def test_token_file_path_is_dot_env_under_token_dir(tmp_path: Path) -> None:
    assert token_file_path(tmp_path) == tmp_path / ".env"


def test_token_file_path_defaults_under_default_token_dir() -> None:
    assert token_file_path(None) == engine.yahoo_client.DEFAULT_TOKEN_DIR / ".env"


# --- token_is_cached ---------------------------------------------------


def test_token_is_cached_true_for_present_refresh_token(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("YAHOO_REFRESH_TOKEN=abc\n", encoding="utf-8")
    assert token_is_cached(tmp_path) is True


def test_token_is_cached_false_for_blank_refresh_token(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("YAHOO_REFRESH_TOKEN=\n", encoding="utf-8")
    assert token_is_cached(tmp_path) is False


def test_token_is_cached_false_for_missing_file(tmp_path: Path) -> None:
    assert token_is_cached(tmp_path / "does-not-exist") is False


def test_token_is_cached_false_for_garbage_file(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("not a key value file at all {{{\n", encoding="utf-8")
    assert token_is_cached(tmp_path) is False


# --- yahoo_credentials ---------------------------------------------------


def test_yahoo_credentials_returns_pair_from_tmp_secrets_file(tmp_path: Path) -> None:
    secrets_path = tmp_path / "secrets.env"
    _write_secrets(secrets_path, client_id="the-id", client_secret="the-secret")
    assert yahoo_credentials(secrets_path) == ("the-id", "the-secret")


def test_yahoo_credentials_raises_engine_error_when_client_id_missing(tmp_path: Path) -> None:
    secrets_path = tmp_path / "secrets.env"
    secrets_path.write_text(f"{YAHOO_CLIENT_SECRET_KEY}=only-secret\n", encoding="utf-8")
    with pytest.raises(EngineError):
        yahoo_credentials(secrets_path)


def test_yahoo_credentials_raises_engine_error_when_client_secret_blank(tmp_path: Path) -> None:
    secrets_path = tmp_path / "secrets.env"
    secrets_path.write_text(
        f"{YAHOO_CLIENT_ID_KEY}=an-id\n{YAHOO_CLIENT_SECRET_KEY}=\n", encoding="utf-8"
    )
    with pytest.raises(EngineError):
        yahoo_credentials(secrets_path)


# --- _query_class ---------------------------------------------------------


def test_query_class_raises_engine_error_naming_python_310(monkeypatch: pytest.MonkeyPatch) -> None:
    # Force the ImportError deterministically rather than relying on yfpy
    # genuinely being absent from the current venv: setting sys.modules["yfpy"]
    # to None makes _query_class's own lazy yfpy.query load raise ImportError,
    # regardless of whether yfpy happens to be installed. This keeps the test
    # correct even after .venv is someday recreated on Python 3.10+ with yfpy
    # installed.
    monkeypatch.setitem(sys.modules, "yfpy", None)
    with pytest.raises(EngineError, match="3.10"):
        _query_class()


# --- build_query: happy path kwargs ---------------------------------------


def test_build_query_passes_expected_kwargs_to_query_class(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secrets_path = tmp_path / "secrets.env"
    _write_secrets(secrets_path, client_id="cid", client_secret="csecret")
    token_dir = tmp_path / "token-dir"

    monkeypatch.setattr("engine.yahoo_client._query_class", lambda: _FakeQuery)

    build_query(league_id="12345", secrets_path=secrets_path, token_dir=token_dir)

    assert token_dir.is_dir()
    kwargs = _FakeQuery.last_kwargs
    assert kwargs is not None
    assert kwargs["game_code"] == "nfl"
    assert kwargs["save_token_data_to_env_file"] is True
    assert kwargs["env_file_location"] == token_dir
    assert kwargs["yahoo_consumer_key"] == "cid"
    assert kwargs["yahoo_consumer_secret"] == "csecret"


# --- build_query: SystemExit and Exception conversion ----------------------


def test_build_query_converts_system_exit_to_yahoo_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secrets_path = tmp_path / "secrets.env"
    _write_secrets(secrets_path)
    monkeypatch.setattr("engine.yahoo_client._query_class", lambda: _SystemExitQuery)

    # This is the test that matters most: a bare `except Exception` would
    # not catch SystemExit, since SystemExit derives from BaseException, not
    # Exception. build_query must catch it explicitly.
    with pytest.raises(YahooUnavailable):
        build_query(league_id="1", secrets_path=secrets_path, token_dir=tmp_path / "td")


def test_build_query_converts_arbitrary_exception_to_yahoo_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secrets_path = tmp_path / "secrets.env"
    _write_secrets(secrets_path)
    monkeypatch.setattr("engine.yahoo_client._query_class", lambda: _BoomQuery)

    with pytest.raises(YahooUnavailable):
        build_query(league_id="1", secrets_path=secrets_path, token_dir=tmp_path / "td")


# --- build_query: missing credential propagates as EngineError, not YahooUnavailable ---


def test_build_query_propagates_engine_error_for_missing_credential_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secrets_path = tmp_path / "secrets.env"
    # No secrets file written at all: both keys are missing.
    monkeypatch.setattr("engine.yahoo_client._query_class", lambda: _FakeQuery)

    with pytest.raises(EngineError) as excinfo:
        build_query(league_id="1", secrets_path=secrets_path, token_dir=tmp_path / "td")

    # A type check, not isinstance against EngineError alone: YahooUnavailable
    # is itself a subclass of EngineError, so isinstance would pass even if
    # build_query wrongly wrapped this in YahooUnavailable.
    assert type(excinfo.value) is EngineError
    assert type(excinfo.value) is not YahooUnavailable


def test_build_query_propagates_engine_error_from_query_class_resolution_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # _query_class() itself is what raises here (the yfpy-not-importable
    # case), not the query constructor it returns. build_query must not
    # wrap this in YahooUnavailable: _query_class() has to be resolved
    # outside the try block, the same way yahoo_credentials already is,
    # or a broken environment (missing/too-old yfpy) gets misreported as
    # "Yahoo is down" instead of surfacing as a real configuration error.
    secrets_path = tmp_path / "secrets.env"
    _write_secrets(secrets_path)

    def _raise_engine_error() -> Any:
        raise EngineError("yfpy requires Python 3.10 or newer")

    monkeypatch.setattr("engine.yahoo_client._query_class", _raise_engine_error)

    with pytest.raises(EngineError) as excinfo:
        build_query(league_id="1", secrets_path=secrets_path, token_dir=tmp_path / "td")

    assert type(excinfo.value) is EngineError
    assert type(excinfo.value) is not YahooUnavailable


# --- harden_token_file ------------------------------------------------


def test_build_query_leaves_token_file_at_mode_0600(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secrets_path = tmp_path / "secrets.env"
    _write_secrets(secrets_path)
    token_dir = tmp_path / "token-dir"
    monkeypatch.setattr("engine.yahoo_client._query_class", lambda: _FakeQuery)

    build_query(league_id="1", secrets_path=secrets_path, token_dir=token_dir)

    token_path = token_dir / ".env"
    assert token_path.exists()
    mode = stat.S_IMODE(token_path.stat().st_mode)
    assert mode == 0o600


def test_harden_token_file_on_nonexistent_directory_does_not_raise(tmp_path: Path) -> None:
    harden_token_file(tmp_path / "does" / "not" / "exist")


# --- read-only guarantee ------------------------------------------------


def test_yahoo_client_source_has_no_write_verb_call_sites() -> None:
    # REPO_ROOT relative, not a cwd-relative "engine/yahoo_client.py" path,
    # so this test resolves the same file whether pytest is invoked from
    # the repo root or from anywhere else.
    source_text = (REPO_ROOT / "engine" / "yahoo_client.py").read_text(encoding="utf-8")

    # Deliberately checking specific call-site substrings, not bare verbs
    # like "post" or "delete": Yahoo's own settings legitimately contain
    # strings such as "post_draft_players" or "postdraft", so a bare
    # substring assertion would false-positive the moment this module ever
    # references one of those Yahoo field names.
    forbidden = (
        ".post(",
        ".put(",
        ".patch(",
        ".delete(",
        'method="POST"',
        "method='POST'",
        'method="PUT"',
        "method='PUT'",
        'method="DELETE"',
        "method='DELETE'",
    )
    for needle in forbidden:
        assert needle not in source_text, f"found forbidden write call site: {needle!r}"
