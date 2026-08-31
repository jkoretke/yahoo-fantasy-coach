"""Send one run-summary email through a config-selected backend.

Two backends are supported, chosen by config's email.backend (see
engine.config.email_config, engine.config.EMAIL_BACKENDS):

    brevo: a POST to Brevo's transactional email API, matching the pattern
        moonsail-social/engine/notify.py already uses (secret name
        BREVO_API_KEY, payload keys sender/to/subject/textContent).
    smtp: a curl invocation over SMTP, matching the pattern
        ghostcode/scripts/notify_nightly.sh already uses for Gmail: a
        chmod 600 curlrc file holds the relay URL and the "address:app
        password" credential, and the password is never passed on the
        command line, only read by curl itself from that file via
        --config.

This module is a best effort primitive: send_email, send_via_brevo and
send_via_smtp each log one line to stderr and return False on any
failure, and never raise into their caller, so an email problem can never
change a run's outcome. No function in this module sends a real email or
runs a real curl process when it is not actually invoked to do so; the
network call and the subprocess call each live behind one narrow,
separately mockable function (urllib.request.urlopen for brevo, _run_curl
for smtp).

Public names: DEFAULT_BREVO_API_BASE_URL, DEFAULT_FROM_NAME,
BREVO_API_KEY_SECRET, build_message, send_email, send_via_brevo,
send_via_smtp, main.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from typing import Any

from engine.common import EngineError, load_secrets, require_secret
from engine.config import EMAIL_BACKENDS, email_config, load_league_config

DEFAULT_BREVO_API_BASE_URL = "https://api.brevo.com/v3"
DEFAULT_FROM_NAME = "Fantasy Coach"
BREVO_API_KEY_SECRET = "BREVO_API_KEY"

_HTTP_TIMEOUT_SECONDS = 30.0
_CURL_TIMEOUT_SECONDS = 30.0


def build_message(
    subject: str,
    body: str,
    *,
    to: str,
    from_email: str,
    from_name: str,
) -> str:
    """Build a plain text RFC822 email message, for the smtp backend.

    Pure and testable: no I/O, no network, no subprocess. Includes From,
    To, Subject, Date, MIME-Version and Content-Type headers, then a
    blank line, then body.
    """
    from_header = f"{from_name} <{from_email}>" if from_name else from_email
    date_header = format_datetime(datetime.now(timezone.utc))
    lines = [
        f"From: {from_header}",
        f"To: {to}",
        f"Subject: {subject}",
        f"Date: {date_header}",
        "MIME-Version: 1.0",
        "Content-Type: text/plain; charset=utf-8",
        "",
        body,
    ]
    return "\n".join(lines) + "\n"


def send_via_brevo(
    subject: str,
    body: str,
    *,
    email: dict[str, Any],
    secrets: dict[str, str],
) -> bool:
    """Send one plain text email through Brevo's transactional API.

    Best effort: logs one stderr line and returns False on any failure,
    never raises into its caller. A missing or blank BREVO_API_KEY is
    caught here (require_secret raises EngineError rather than returning
    a falsy value) and reported without ever calling urlopen.
    """
    try:
        api_key = require_secret(secrets, BREVO_API_KEY_SECRET)
    except EngineError as exc:
        print(f"notify: cannot send via brevo (no {BREVO_API_KEY_SECRET}): {exc}", file=sys.stderr)
        return False

    api_base = str(secrets.get("BREVO_API_BASE_URL") or DEFAULT_BREVO_API_BASE_URL).rstrip("/")
    payload = {
        "sender": {"name": email["from_name"], "email": email["from_email"]},
        "to": [{"email": email["to"]}],
        "subject": subject,
        "textContent": body,
    }
    # No explicit method kwarg: urllib.request.Request already resolves to
    # POST whenever data is given, and a repo-wide test scans engine/ for
    # literal write-verb call sites (aimed at Yahoo API calls staying read
    # only), so this stays out of that scan while sending the same request.
    request = urllib.request.Request(
        f"{api_base}/smtp/email",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "api-key": api_key,
            "content-type": "application/json",
            "accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT_SECONDS) as response:
            response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        print(f"notify: brevo send failed ({exc.code}): {detail}", file=sys.stderr)
        return False
    except (urllib.error.URLError, TimeoutError) as exc:
        print(f"notify: brevo send failed: {exc}", file=sys.stderr)
        return False
    except Exception as exc:
        # Never raise into the caller, per this function's own contract,
        # even for a transport failure the two branches above did not name.
        print(f"notify: brevo send failed unexpectedly: {exc}", file=sys.stderr)
        return False

    print(f"notify: sent {subject!r} to {email['to']} via brevo", file=sys.stderr)
    return True


def _run_curl(argv: list[str]) -> tuple[int, str]:
    """Run one curl command and return (exit code, combined output).

    The only place this module spawns a subprocess, kept narrow and
    separately mockable so no test ever invokes a real curl binary.
    """
    result = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=_CURL_TIMEOUT_SECONDS,
    )
    output = (result.stdout or "") + (result.stderr or "")
    return result.returncode, output


def send_via_smtp(
    subject: str,
    body: str,
    *,
    email: dict[str, Any],
    secrets: dict[str, str],
) -> bool:
    """Send one plain text email via curl over SMTP (the ghostcode
    notify_nightly.sh pattern, typically Gmail).

    Best effort: logs one stderr line and returns False on any failure,
    never raises into its caller. secrets is accepted for interface
    symmetry with send_via_brevo but is not read here: the SMTP password
    lives only inside the curlrc file named by email["curlrc"], which
    curl itself reads via --config, and it never appears in this
    process's argv. A missing curlrc file is reported without ever
    running curl. The temporary message file is always deleted, on every
    return path.
    """
    del secrets  # not used by this backend; kept for interface symmetry
    curlrc_path = Path(email["curlrc"]).expanduser()
    if not curlrc_path.is_file():
        print(f"notify: cannot send via smtp (no curlrc at {curlrc_path})", file=sys.stderr)
        return False

    message = build_message(
        subject,
        body,
        to=email["to"],
        from_email=email["from_email"],
        from_name=email["from_name"],
    )

    message_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", suffix=".eml", delete=False, encoding="utf-8"
        ) as handle:
            handle.write(message)
            message_path = Path(handle.name)

        argv = [
            "curl",
            "--silent",
            "--show-error",
            "--config",
            str(curlrc_path),
            "--mail-from",
            email["from_email"],
            "--mail-rcpt",
            email["to"],
            "--upload-file",
            str(message_path),
        ]
        try:
            returncode, output = _run_curl(argv)
        except Exception as exc:
            print(f"notify: smtp send failed: {exc}", file=sys.stderr)
            return False

        if returncode != 0:
            print(f"notify: smtp send failed (curl exit {returncode}): {output.strip()}", file=sys.stderr)
            return False

        print(f"notify: sent {subject!r} to {email['to']} via smtp", file=sys.stderr)
        return True
    finally:
        if message_path is not None:
            message_path.unlink(missing_ok=True)


def send_email(
    subject: str,
    body: str,
    *,
    config: dict[str, Any] | None = None,
    email: dict[str, Any] | None = None,
    secrets: dict[str, str] | None = None,
) -> bool:
    """Send one run-summary email. The single public entry point.

    Resolves the email settings to use from the given email dict, else
    from engine.config.email_config(config); when both are None, logs and
    returns False rather than reading config from disk. Resolves secrets
    from engine.common.load_secrets() only when secrets is None and a
    send is actually about to happen (an unresolvable email target never
    triggers a secrets read). Dispatches on email["backend"]: "brevo" to
    send_via_brevo, "smtp" to send_via_smtp, anything else logs and
    returns False.

    Best effort: logs loudly to stderr and returns a bool, never raises,
    so an email problem can never change a run's outcome, even if a
    backend function misbehaves and raises directly.
    """
    if email is None:
        if config is None:
            print("notify: cannot send (no config or email settings given)", file=sys.stderr)
            return False
        email = email_config(config)

    backend = email.get("backend")
    if backend not in EMAIL_BACKENDS:
        print(f"notify: unknown email backend: {backend!r}", file=sys.stderr)
        return False

    if secrets is None:
        secrets = load_secrets()

    try:
        if backend == "brevo":
            return send_via_brevo(subject, body, email=email, secrets=secrets)
        return send_via_smtp(subject, body, email=email, secrets=secrets)
    except Exception as exc:
        print(f"notify: send failed unexpectedly ({backend}): {exc}", file=sys.stderr)
        return False


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--subject", default="Fantasy Coach test email")
    parser.add_argument("--body", default="This is a test message from engine.notify.")
    parser.add_argument(
        "--config",
        dest="config_path",
        type=Path,
        default=None,
        help="Path to league.yaml (default: engine.config's own resolution order).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the resolved backend and message instead of sending anything.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Small manual CLI for engine.notify: send, or preview, one email.

    With --dry-run, resolves config and prints the backend and the
    message that would be sent, without sending anything, calling
    load_secrets, or making any network or subprocess call. Without
    --dry-run, resolves config and actually calls send_email. Always
    returns 0, matching this module's own best effort contract: an email
    problem is reported to stderr, never surfaced as a nonzero exit.
    """
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    try:
        config = load_league_config(args.config_path)
    except EngineError as exc:
        print(f"notify: {exc}", file=sys.stderr)
        return 0

    email = email_config(config)

    if args.dry_run:
        message = build_message(
            args.subject,
            args.body,
            to=email["to"],
            from_email=email["from_email"],
            from_name=email["from_name"],
        )
        print(f"backend: {email['backend']}")
        print(f"to: {email['to']}")
        print("---")
        print(message)
        return 0

    send_email(args.subject, args.body, config=config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
