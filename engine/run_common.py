"""Shared machinery for the four run wrappers (weekly, gameday, waiver, inactive).

Every wrapper follows the same shape, proven in moonsail-social/engine/blog_run.py: build a
brief, ask Claude to write the prose from it, check the prose against the brief, send exactly
one email, always exit 0. The wrapper's underlying work ends with a machine readable
`STATUS <token>` line as its last output; STATUS_PREFIX / find_status_line / strip_status_line /
status_line / print_status are the shared contract for producing and consuming that line.

run_claude is the ONLY place in this repo that spawns a subprocess for `claude --print`. It is
kept narrow and separately mockable, exactly like engine.notify's own `_run_curl`, so no test in
this repo, in this file or any other, ever invokes a real claude process. draft_body and
compose_email both accept a `runner` override for the same reason.

compose_email is where docs/plan.md's determinism split is enforced end to end: it drafts prose
with Claude, checks it with engine.prose_gate against the same brief the draft was written from,
and falls back to a fully deterministic engine.email_render rendering on any rejection, because
the plan says the email always goes out. In `--fixtures` mode (or with prose="plain") it never
drafts anything, so a fixtures run never spawns a subprocess at all.

apply_toss_up_margin lets a wrapper apply config's toss_up_margin_points to a brief that
engine.brief.build_brief already assembled at the module default, without engine.brief or
engine.lineup or engine.waivers needing to change. See its own docstring for the faab/priority
branch this depends on.

Public names: STATUS_PREFIX, PROMPTS_DIR, PROSE_MODES, prompt_path, load_prompt,
find_status_line, strip_status_line, status_line, print_status, run_claude, draft_body,
apply_toss_up_margin, compose_email, deliver.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

from engine import notify
from engine.brief import ROUTINES
from engine.common import EngineError, REPO_ROOT
from engine.config import claude_config
from engine.email_render import render_plain_email, subject_for
from engine.lineup import lineup_changes
from engine.prose_gate import check_draft, format_violations

# Every STATUS line, in either direction, starts with this. Note the trailing
# space: status_line builds a line by concatenating this directly onto the
# outcome and tokens, so callers never need to add their own separator.
STATUS_PREFIX = "STATUS "

# Where the four prompt files live: prompts/weekly.md, gameday.md, waiver.md, inactive.md.
PROMPTS_DIR = REPO_ROOT / "prompts"

# compose_email's prose modes. "auto" resolves to "plain" only when the caller
# passes fixtures=True; it never inspects anything else to decide.
PROSE_MODES = ("auto", "claude", "plain")

# The shape run_claude and draft_body's runner override both satisfy:
# (prompt, *, claude_bin=..., timeout=...) -> (returncode, stdout, stderr). Not
# part of this module's public surface (see the module docstring's "Public
# names" line), so it stays underscore prefixed like this repo's other
# unlisted module-level names (_VALID_WAIVER_TYPES, _HTTP_TIMEOUT_SECONDS).
_RunnerFn = Callable[..., tuple[int, str, str]]


def prompt_path(routine: str) -> Path:
    """Return the prompt file path for routine.

    routine must be one of engine.brief.ROUTINES ("weekly", "gameday", "waiver",
    "inactive"); anything else raises EngineError.
    """
    if routine not in ROUTINES:
        raise EngineError(f"unknown routine: {routine!r} (must be one of {ROUTINES})")
    return PROMPTS_DIR / f"{routine}.md"


def load_prompt(routine: str) -> str:
    """Return the text of routine's prompt file.

    Raises EngineError if routine is not one of ROUTINES (via prompt_path), or
    if the prompt file itself does not exist on disk.
    """
    path = prompt_path(routine)
    if not path.exists():
        raise EngineError(f"prompt file does not exist: {path}")
    return path.read_text(encoding="utf-8")


def find_status_line(text: str) -> str | None:
    """Return the LAST `STATUS ...` payload found in text, or None if there is none.

    Each candidate line is stripped of surrounding whitespace before the prefix
    check, so leading indentation or trailing carriage returns never hide a
    real status line.
    """
    last: str | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith(STATUS_PREFIX):
            last = line[len(STATUS_PREFIX):].strip()
    return last


def strip_status_line(text: str) -> str:
    """Return text with every line starting with STATUS_PREFIX removed.

    Trailing whitespace left behind by the removal is trimmed from the result,
    so a model's trailing STATUS line never leaves a dangling blank line at the
    end of an email body.
    """
    kept = [line for line in text.splitlines() if not line.strip().startswith(STATUS_PREFIX)]
    return "\n".join(kept).rstrip()


def status_line(outcome: str, *tokens: str) -> str:
    """Build one STATUS line, e.g. status_line("skipped", "no-games") ==
    "STATUS skipped no-games"."""
    return STATUS_PREFIX + " ".join((outcome, *tokens))


def print_status(outcome: str, *tokens: str) -> None:
    """Print status_line(outcome, *tokens) to stdout.

    Every wrapper calls this exactly once, as its LAST output, so a scheduler
    or a human reading the run's stdout always finds the outcome on the final
    line.
    """
    print(status_line(outcome, *tokens))


def run_claude(
    prompt: str, *, claude_bin: str = "claude", timeout: int = 600
) -> tuple[int, str, str]:
    """Run `claude --print` with prompt and return (returncode, stdout, stderr).

    THE ONLY PLACE IN THIS REPO THAT SPAWNS A SUBPROCESS FOR CLAUDE. Kept as
    its own narrow function precisely so tests monkeypatch it instead of ever
    invoking a real claude process. A timeout is turned into (124, partial
    stdout captured before the timeout, stderr plus a note), matching
    moonsail-social/engine/blog_run.py's own run_skill.
    """
    cmd = [claude_bin, "--print", "--dangerously-skip-permissions", prompt]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
        if isinstance(stdout, bytes):
            stdout = stdout.decode("utf-8", errors="replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")
        return 124, stdout, stderr + f"\n[run_common: claude run exceeded {timeout}s timeout]"


def draft_body(
    routine: str,
    brief: dict[str, Any],
    config: dict[str, Any],
    *,
    extra_context: str = "",
    runner: _RunnerFn | None = None,
) -> tuple[str | None, str]:
    """Ask Claude to draft routine's email body from brief. Returns (body, reason).

    The prompt sent to claude is routine's prompt file text, then brief
    rendered as indented JSON in a fenced block, then extra_context (only
    when it is non-empty). runner defaults to run_claude; a test passes its
    own to avoid ever spawning a real claude process. binary and timeout come
    from engine.config.claude_config(config).

    Returns (None, reason) on a non-zero exit (a timeout included, since
    run_claude reports one as exit code 124) or on stdout that is empty once
    its trailing STATUS line is stripped. Otherwise returns (body, "ok") with
    body already run through strip_status_line, so a machine token never ends
    up inside the email.
    """
    prompt_text = load_prompt(routine)
    fenced_brief = "```json\n" + json.dumps(brief, indent=2) + "\n```"
    pieces = [prompt_text, fenced_brief]
    if extra_context:
        pieces.append(extra_context)
    prompt = "\n\n".join(pieces)

    claude = claude_config(config)
    run = runner or run_claude
    returncode, stdout, stderr = run(
        prompt, claude_bin=claude["binary"], timeout=claude["timeout_seconds"]
    )

    if returncode != 0:
        detail = stderr.strip() or stdout.strip() or "(no output)"
        return None, f"claude exited {returncode}: {detail}"

    body = strip_status_line(stdout).strip()
    if not body:
        return None, "claude produced no output"

    return body, "ok"


def apply_toss_up_margin(brief: dict[str, Any], margin: float) -> dict[str, Any]:
    """Re-band brief's toss ups at margin, mutating and returning the same dict.

    engine.brief.build_brief always assembles a brief at the module default
    margin (engine.lineup.DEFAULT_TOSS_UP_MARGIN_POINTS, which
    engine.waivers imports rather than redeclaring). This lets a wrapper
    apply config's own toss_up_margin_points afterward, without changing
    build_brief's, lineup_changes's, or rank_waiver_targets's signatures.

    (a) brief["lineup_changes"] is recomputed via engine.lineup.lineup_changes
    using its existing public third parameter, toss_up_margin_points.

    (b) Branches first on brief["waivers"]["waiver_type"], because
    engine.waivers.rank_waiver_targets's own docstring documents the two
    branches as genuinely different shapes, not one relabelled:

    - "priority": every target in brief["waivers"]["targets"] is re-tagged.
      Any existing "toss_up", "toss_up_margin" and "toss_up_options" keys are
      removed first, then re-added, with "toss_up_options": ["claim", "skip"],
      exactly when abs(target["points_gained"] - brief["waivers"]
      ["required_gain"]) < margin. This mirrors rank_waiver_targets's own
      banding rule ("Priority also flags docs/plan.md's toss up band...") at
      a caller supplied margin instead of the module default.
    - anything else (in particular "faab"): nothing happens. FAAB targets
      never carry a toss up (rank_waiver_targets's docstring: "FAAB never
      declines a real upgrade... there is no close claim/skip line to
      flag"), and a faab result carries no "required_gain" key at all, so
      reading one here would be a KeyError.

    Because engine.lineup.DEFAULT_TOSS_UP_MARGIN_POINTS and engine.waivers's
    imported copy of that same constant are both 2.0, calling this with
    margin=2.0 on a brief built from the module defaults is a proven no-op:
    see tests/test_run_common.py's drift guard, which asserts exact deep
    equality before and after, for both the faab and priority branches.
    """
    brief["lineup_changes"] = lineup_changes(
        brief["current_lineup"], brief["optimal_lineup"], margin
    )

    waivers = brief["waivers"]
    if waivers["waiver_type"] == "priority":
        required_gain = waivers["required_gain"]
        retagged: list[dict[str, Any]] = []
        for target in waivers["targets"]:
            target = dict(target)
            target.pop("toss_up", None)
            target.pop("toss_up_margin", None)
            target.pop("toss_up_options", None)
            if abs(target["points_gained"] - required_gain) < margin:
                target["toss_up"] = True
                target["toss_up_margin"] = margin
                target["toss_up_options"] = ["claim", "skip"]
            retagged.append(target)
        waivers["targets"] = retagged
    # Any other waiver_type (in particular "faab"): left untouched, per this
    # function's own contract above.

    return brief


def compose_email(
    routine: str,
    brief: dict[str, Any],
    config: dict[str, Any],
    *,
    trades: dict[str, Any] | None = None,
    inactive_changes: list[dict[str, Any]] | None = None,
    prose: str = "auto",
    fixtures: bool = False,
    runner: _RunnerFn | None = None,
) -> tuple[str, str, str]:
    """Compose one routine's (subject, body, source) email for brief.

    prose is one of PROSE_MODES. "auto" (the default) resolves to "plain"
    when fixtures is True and to "claude" otherwise; an explicit "claude" or
    "plain" is never overridden by fixtures, so a caller can still force
    Claude prose in a fixtures run if it chooses to. Any other value of
    prose raises EngineError.

    subject always comes from engine.email_render.subject_for, so it is
    identical and deterministic on both the claude and the plain path.

    On the plain path, engine.email_render.render_plain_email builds the
    body directly from brief (plus trades / inactive_changes); run_claude is
    never called, so a --fixtures run never spawns a subprocess.

    On the claude path, draft_body drafts a body, then
    engine.prose_gate.check_draft validates it against this SAME brief. A
    rejection, or draft_body returning no body at all, is logged to stderr
    (the gate's own violations when there were any to check, the draft
    failure reason otherwise) and falls back to render_plain_email, because
    the plan says the email always goes out.

    source is "claude" or "plain": which path actually produced body.
    """
    if prose not in PROSE_MODES:
        raise EngineError(f"invalid prose mode: {prose!r} (must be one of {PROSE_MODES})")

    resolved = "plain" if (prose == "auto" and fixtures) else prose
    if resolved == "auto":
        resolved = "claude"

    subject = subject_for(routine, brief, extra=inactive_changes)

    def _plain_body() -> str:
        _, rendered = render_plain_email(
            routine, brief, trades=trades, inactive_changes=inactive_changes, config=config
        )
        return rendered

    if resolved == "plain":
        return subject, _plain_body(), "plain"

    body, reason = draft_body(routine, brief, config, runner=runner)
    if body is None:
        print(f"run_common: no claude draft used ({reason})", file=sys.stderr)
    else:
        result = check_draft(body, brief)
        if result["ok"]:
            return subject, body, "claude"
        print(format_violations(result), file=sys.stderr)

    return subject, _plain_body(), "plain"


def deliver(
    subject: str,
    body: str,
    config: dict[str, Any],
    *,
    dry_run: bool,
    secrets: dict[str, str] | None = None,
) -> bool:
    """Send exactly one email (subject, body) for config, or preview it.

    When dry_run is true, prints "[dry-run] would send:", the Subject line
    and body to stdout, and returns True immediately, before any
    engine.common.load_secrets call and before any engine.notify.send_email
    call. This ordering is mandatory: load_secrets falls back to the
    owner's real ~/.config/yahoo-fantasy-coach/secrets.env, and a --dry-run
    invocation must genuinely need no credentials.

    Otherwise calls engine.notify.send_email exactly once and returns its
    bool. Exactly one email per call, never two.
    """
    if dry_run:
        print("[dry-run] would send:")
        print(f"Subject: {subject}")
        print(body)
        return True

    return notify.send_email(subject, body, config=config, secrets=secrets)
