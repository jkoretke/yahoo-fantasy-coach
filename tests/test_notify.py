"""Tests for engine.notify: the brevo and smtp email backends.

No test in this file sends a real email or shells out to a real curl
process. The brevo path is proven by patching urllib.request.urlopen; the
smtp path is proven by patching engine.notify._run_curl. Both patches
happen after tests/conftest.py's autouse block_real_network fixture
already patched urlopen to fail any unpatched call, so neither test
defeats that guard, it just overrides it for the one call under test.
"""
from __future__ import annotations

import json
import subprocess
import urllib.error
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from engine.notify import (
    BREVO_API_KEY_SECRET,
    DEFAULT_BREVO_API_BASE_URL,
    DEFAULT_FROM_NAME,
    build_message,
    main,
    send_email,
    send_via_brevo,
    send_via_smtp,
)


def _email(**overrides: Any) -> dict[str, Any]:
    base = {
        "backend": "brevo",
        "to": "owner@example.com",
        "from_email": "coach@example.com",
        "from_name": "Fantasy Coach",
        "curlrc": "~/.config/yahoo-fantasy-coach/curlrc",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# build_message
# ---------------------------------------------------------------------------


def test_build_message_contains_headers_subject_and_body() -> None:
    message = build_message(
        "Week 1 lineup",
        "Start Josh Allen.",
        to="owner@example.com",
        from_email="coach@example.com",
        from_name="Fantasy Coach",
    )

    assert "From: Fantasy Coach <coach@example.com>" in message
    assert "To: owner@example.com" in message
    assert "Subject: Week 1 lineup" in message
    assert "MIME-Version: 1.0" in message
    assert "Content-Type: text/plain; charset=utf-8" in message
    assert message.strip().endswith("Start Josh Allen.")


def test_build_message_without_from_name_uses_bare_email() -> None:
    message = build_message(
        "s", "b", to="owner@example.com", from_email="coach@example.com", from_name=""
    )
    assert "From: coach@example.com" in message


# ---------------------------------------------------------------------------
# send_via_brevo
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    def read(self) -> bytes:
        return b"{}"


@patch("engine.notify.urllib.request.urlopen")
def test_send_via_brevo_posts_expected_url_header_and_payload(mock_urlopen) -> None:
    mock_urlopen.return_value = _FakeResponse()
    secrets = {"BREVO_API_KEY": "brevo-secret-key"}

    result = send_via_brevo(
        "Week 1 lineup",
        "Start Josh Allen.",
        email=_email(backend="brevo"),
        secrets=secrets,
    )

    assert result is True
    assert mock_urlopen.call_count == 1
    request = mock_urlopen.call_args.args[0]
    assert request.full_url == f"{DEFAULT_BREVO_API_BASE_URL}/smtp/email"
    assert request.get_method() == "POST"
    assert request.get_header("Api-key") == "brevo-secret-key"

    payload = json.loads(request.data.decode("utf-8"))
    assert payload["sender"] == {"name": "Fantasy Coach", "email": "coach@example.com"}
    assert payload["to"] == [{"email": "owner@example.com"}]
    assert payload["subject"] == "Week 1 lineup"
    assert payload["textContent"] == "Start Josh Allen."


@patch("engine.notify.urllib.request.urlopen")
def test_send_via_brevo_missing_api_key_returns_false_without_urlopen(mock_urlopen) -> None:
    result = send_via_brevo(
        "subject", "body", email=_email(backend="brevo"), secrets={}
    )

    assert result is False
    mock_urlopen.assert_not_called()


@patch("engine.notify.urllib.request.urlopen")
def test_send_via_brevo_http_error_returns_false(mock_urlopen) -> None:
    mock_urlopen.side_effect = urllib.error.HTTPError(
        "url", 400, "bad request", hdrs=None, fp=__import__("io").BytesIO(b"nope")
    )

    result = send_via_brevo(
        "subject", "body", email=_email(backend="brevo"), secrets={"BREVO_API_KEY": "k"}
    )

    assert result is False


@patch("engine.notify.urllib.request.urlopen")
def test_send_via_brevo_url_error_returns_false(mock_urlopen) -> None:
    mock_urlopen.side_effect = urllib.error.URLError("boom")

    result = send_via_brevo(
        "subject", "body", email=_email(backend="brevo"), secrets={"BREVO_API_KEY": "k"}
    )

    assert result is False


@patch("engine.notify.urllib.request.urlopen")
def test_send_via_brevo_timeout_returns_false(mock_urlopen) -> None:
    mock_urlopen.side_effect = TimeoutError("timed out")

    result = send_via_brevo(
        "subject", "body", email=_email(backend="brevo"), secrets={"BREVO_API_KEY": "k"}
    )

    assert result is False


# ---------------------------------------------------------------------------
# send_via_smtp
# ---------------------------------------------------------------------------


@patch("engine.notify._run_curl")
def test_send_via_smtp_runs_curl_with_expected_argv_and_no_password(
    mock_run_curl, tmp_path: Path
) -> None:
    curlrc = tmp_path / "curlrc"
    curlrc.write_text(
        'url = "smtps://smtp.gmail.com:465"\n'
        "ssl-reqd\n"
        'user = "coach@example.com:super-secret-app-password"\n'
    )

    captured: dict[str, Any] = {}

    def _fake_run_curl(argv: list[str]) -> tuple[int, str]:
        captured["argv"] = argv
        message_path = Path(argv[argv.index("--upload-file") + 1])
        captured["message_text"] = message_path.read_text(encoding="utf-8")
        assert message_path.exists()
        return 0, ""

    mock_run_curl.side_effect = _fake_run_curl

    result = send_via_smtp(
        "Week 1 lineup",
        "Start Josh Allen.",
        email=_email(backend="smtp", curlrc=str(curlrc)),
        secrets={},
    )

    assert result is True
    argv = captured["argv"]
    assert "--config" in argv
    assert argv[argv.index("--config") + 1] == str(curlrc)
    assert "--mail-from" in argv
    assert argv[argv.index("--mail-from") + 1] == "coach@example.com"
    assert "--mail-rcpt" in argv
    assert argv[argv.index("--mail-rcpt") + 1] == "owner@example.com"
    assert "--upload-file" in argv

    assert "Week 1 lineup" in captured["message_text"]
    assert "Start Josh Allen." in captured["message_text"]

    for arg in argv:
        assert "super-secret-app-password" not in arg

    # The temp message file is deleted once send_via_smtp returns.
    message_path = Path(argv[argv.index("--upload-file") + 1])
    assert not message_path.exists()


def test_send_via_smtp_expands_tilde_in_curlrc_path(tmp_path: Path, monkeypatch) -> None:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    curlrc = fake_home / "curlrc"
    curlrc.write_text('url = "smtps://smtp.gmail.com:465"\n')

    with patch("engine.notify._run_curl") as mock_run_curl:
        mock_run_curl.return_value = (0, "")
        result = send_via_smtp(
            "s", "b", email=_email(backend="smtp", curlrc="~/curlrc"), secrets={}
        )

    assert result is True
    argv = mock_run_curl.call_args.args[0]
    assert argv[argv.index("--config") + 1] == str(curlrc)


@patch("engine.notify._run_curl")
def test_send_via_smtp_missing_curlrc_returns_false_without_running_curl(
    mock_run_curl, tmp_path: Path
) -> None:
    missing = tmp_path / "no-such-curlrc"

    result = send_via_smtp(
        "s", "b", email=_email(backend="smtp", curlrc=str(missing)), secrets={}
    )

    assert result is False
    mock_run_curl.assert_not_called()


@patch("engine.notify._run_curl")
def test_send_via_smtp_nonzero_curl_exit_returns_false_and_deletes_temp_file(
    mock_run_curl, tmp_path: Path
) -> None:
    curlrc = tmp_path / "curlrc"
    curlrc.write_text('url = "smtps://smtp.gmail.com:465"\n')

    captured_path: dict[str, Path] = {}

    def _fake_run_curl(argv: list[str]) -> tuple[int, str]:
        captured_path["path"] = Path(argv[argv.index("--upload-file") + 1])
        return 1, "550 relay denied"

    mock_run_curl.side_effect = _fake_run_curl

    result = send_via_smtp(
        "s", "b", email=_email(backend="smtp", curlrc=str(curlrc)), secrets={}
    )

    assert result is False
    assert not captured_path["path"].exists()


@patch("engine.notify._run_curl")
def test_send_via_smtp_curl_raising_returns_false_and_deletes_temp_file(
    mock_run_curl, tmp_path: Path
) -> None:
    curlrc = tmp_path / "curlrc"
    curlrc.write_text('url = "smtps://smtp.gmail.com:465"\n')

    captured_path: dict[str, Path] = {}

    def _fake_run_curl(argv: list[str]) -> tuple[int, str]:
        captured_path["path"] = Path(argv[argv.index("--upload-file") + 1])
        raise subprocess.TimeoutExpired(cmd=argv, timeout=30)

    mock_run_curl.side_effect = _fake_run_curl

    result = send_via_smtp(
        "s", "b", email=_email(backend="smtp", curlrc=str(curlrc)), secrets={}
    )

    assert result is False
    assert not captured_path["path"].exists()


# ---------------------------------------------------------------------------
# send_email: dispatch, resolution, and the "never raises" contract
# ---------------------------------------------------------------------------


def test_send_email_dispatches_to_brevo() -> None:
    with patch("engine.notify.send_via_brevo", return_value=True) as mock_brevo, patch(
        "engine.notify.send_via_smtp"
    ) as mock_smtp:
        result = send_email(
            "s", "b", email=_email(backend="brevo"), secrets={"BREVO_API_KEY": "k"}
        )

    assert result is True
    mock_brevo.assert_called_once()
    mock_smtp.assert_not_called()


def test_send_email_dispatches_to_smtp() -> None:
    with patch("engine.notify.send_via_smtp", return_value=True) as mock_smtp, patch(
        "engine.notify.send_via_brevo"
    ) as mock_brevo:
        result = send_email("s", "b", email=_email(backend="smtp"), secrets={})

    assert result is True
    mock_smtp.assert_called_once()
    mock_brevo.assert_not_called()


def test_send_email_unknown_backend_returns_false() -> None:
    with patch("engine.notify.send_via_brevo") as mock_brevo, patch(
        "engine.notify.send_via_smtp"
    ) as mock_smtp:
        result = send_email("s", "b", email=_email(backend="carrier-pigeon"), secrets={})

    assert result is False
    mock_brevo.assert_not_called()
    mock_smtp.assert_not_called()


def test_send_email_no_config_and_no_email_returns_false() -> None:
    result = send_email("s", "b")
    assert result is False


def test_send_email_uses_email_config_from_given_config_dict() -> None:
    config = {
        "email": {
            "backend": "brevo",
            "to": "owner@example.com",
            "from_email": "coach@example.com",
            "from_name": DEFAULT_FROM_NAME,
            "curlrc": "~/curlrc",
        }
    }
    with patch("engine.notify.send_via_brevo", return_value=True) as mock_brevo:
        result = send_email("s", "b", config=config, secrets={"BREVO_API_KEY": "k"})

    assert result is True
    _, kwargs = mock_brevo.call_args
    assert kwargs["email"]["to"] == "owner@example.com"


def test_send_email_resolves_secrets_only_when_a_send_is_about_to_happen() -> None:
    with patch("engine.notify.load_secrets") as mock_load_secrets:
        # Unknown backend: must never even look for secrets.
        result = send_email("s", "b", email=_email(backend="nope"))
        assert result is False
        mock_load_secrets.assert_not_called()


def test_send_email_never_raises_when_transport_raises_arbitrary_exception() -> None:
    with patch("engine.notify.send_via_brevo", side_effect=RuntimeError("kaboom")):
        result = send_email(
            "s", "b", email=_email(backend="brevo"), secrets={"BREVO_API_KEY": "k"}
        )
    assert result is False


def test_send_email_never_raises_when_smtp_transport_raises_arbitrary_exception() -> None:
    with patch("engine.notify.send_via_smtp", side_effect=ValueError("nope")):
        result = send_email("s", "b", email=_email(backend="smtp"), secrets={})
    assert result is False


# ---------------------------------------------------------------------------
# main: dry run never sends, never touches secrets or the network
# ---------------------------------------------------------------------------


def test_main_dry_run_prints_message_and_never_sends(
    tmp_path: Path, monkeypatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path = tmp_path / "league.yaml"
    config_path.write_text(
        "email:\n"
        "  backend: brevo\n"
        "  to: owner@example.com\n"
        "  from_email: coach@example.com\n"
        "  from_name: Fantasy Coach\n"
        "  curlrc: ~/curlrc\n"
    )

    with patch("engine.notify.send_email") as mock_send_email, patch(
        "engine.notify.load_secrets"
    ) as mock_load_secrets:
        exit_code = main(
            [
                "--config",
                str(config_path),
                "--subject",
                "Week 1 lineup",
                "--body",
                "Start Josh Allen.",
                "--dry-run",
            ]
        )

    assert exit_code == 0
    mock_send_email.assert_not_called()
    mock_load_secrets.assert_not_called()

    out = capsys.readouterr().out
    assert "backend: brevo" in out
    assert "Week 1 lineup" in out
    assert "Start Josh Allen." in out


def test_main_missing_explicit_config_returns_zero(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist.yaml"
    exit_code = main(["--config", str(missing), "--dry-run"])
    assert exit_code == 0
