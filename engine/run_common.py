"""Shared machinery for the four run wrappers (weekly, gameday, waiver, inactive).

Every wrapper follows the same shape, proven in moonsail-social/engine/blog_run.py: build a
brief, ask Claude to write the prose from it, check the prose against the brief, send exactly
one email, then exit on a code that matches what it just reported. The wrapper's underlying
work ends with a machine readable `STATUS <token>` line as its last output; STATUS_PREFIX /
find_status_line / strip_status_line / status_line / print_status / exit_code are the shared
contract for producing and consuming that line.

run_claude is the ONLY place in this repo that spawns a subprocess for `claude --print`. It is
kept narrow and separately mockable, exactly like engine.notify's own `_run_curl`, so no test in
this repo, in this file or any other, ever invokes a real claude process. draft_body and
compose_email both accept a `runner` override for the same reason.

compose_email is where docs/plan.md's determinism split is enforced end to end: it drafts prose
with Claude (from the brief PLUS trades and inactive_changes, each appended as its own fenced
JSON block so weekly's trade ideas and inactive's change list actually reach the model), checks
it with engine.prose_gate against that same data, and falls back to a fully deterministic
engine.email_render rendering on any rejection, because the plan says the email always goes out.
In `--fixtures` mode (or with prose="plain") it never drafts anything, so a fixtures run never
spawns a subprocess at all.

apply_toss_up_margin lets a wrapper apply config's toss_up_margin_points to a brief that
engine.brief.build_brief already assembled at the module default, without engine.brief or
engine.lineup or engine.waivers needing to change. See its own docstring for the faab/priority
branch this depends on.

resolve_week holds the whole "which week is this live run for" policy, in one place rather
than repeated in four wrappers: an explicit --week always wins, otherwise ESPN's own current
week is read through engine.sources.schedule.fetch_current_week, and anything that cannot be
resolved confidently raises EngineError rather than guessing. A fixtures run never calls it.

exit_code turns a wrapper's STATUS outcome into that wrapper's process exit code. Only
"failed" is non-zero. "skipped" is a success: gameday_run reports it on every day with no
game, and inactive_run on nearly every five-minute fire, so treating it as a failure would
mean an alert every five minutes on a quiet Sunday.

Public names: STATUS_PREFIX, PROMPTS_DIR, PROSE_MODES, FAILED_OUTCOME, prompt_path,
load_prompt, find_status_line, strip_status_line, status_line, print_status, exit_code,
error_status_token, run_claude, draft_body, apply_toss_up_margin, resolve_week, compose_email,
deliver.
"""
from __future__ import annotations

import json
import re
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
from engine.sources import schedule as schedule_source

# Every STATUS line, in either direction, starts with this. Note the trailing
# space: status_line builds a line by concatenating this directly onto the
# outcome and tokens, so callers never need to add their own separator.
STATUS_PREFIX = "STATUS "

# The one STATUS outcome that means the run did not do its job. Everything
# else ("emailed", "dry-run", "skipped") is a success; see exit_code.
FAILED_OUTCOME = "failed"

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


def print_status(outcome: str, *tokens: str) -> int:
    """Print status_line(outcome, *tokens) to stdout, return exit_code(outcome).

    Every wrapper calls this exactly once, as its LAST output, so a scheduler
    or a human reading the run's stdout always finds the outcome on the final
    line. The return value is what the wrapper's main() then returns, so the
    printed outcome and the process's exit code can never disagree.
    """
    print(status_line(outcome, *tokens))
    return exit_code(outcome)


def exit_code(outcome: str) -> int:
    """Return the process exit code for a STATUS outcome: 1 for failed, else 0.

    Only FAILED_OUTCOME is non-zero. "emailed" and "dry-run" are obvious
    successes. "skipped" is one too, and deliberately so: gameday_run prints
    "STATUS skipped no-games" on every day none of the owner's players play,
    and inactive_run prints "STATUS skipped not-yet" on nearly every one of
    its five-minute fires. Those are the routines working correctly, so a
    non-zero exit there would mean systemd's OnFailure= alert firing every
    five minutes on a quiet Sunday, which is exactly the noise that would
    get the alert muted and defeat the point of having one.
    """
    return 1 if outcome == FAILED_OUTCOME else 0


def error_status_token(error: Exception) -> str:
    """Return a short, fixed, kebab-case token naming error's class.

    STATUS lines are meant to be machine-readable tokens a scheduler
    parses, not free English prose: str(an EngineError) can be an
    arbitrary multi-word message (docs/plan.md's own example is "unknown
    team_id: nope"), and could in principle even contain a newline, which
    would push text after what is supposed to be the run's last line.
    Every wrapper's except block uses this instead of str(error) for the
    STATUS payload, e.g. "EngineError" -> "engine-error",
    "SourceUnavailable" -> "source-unavailable". The full message is not
    lost: it is still printed to stderr, on the line immediately before
    the STATUS line, which is where a human or a log reader belongs.
    """
    name = type(error).__name__
    return re.sub(r"(?<!^)(?=[A-Z])", "-", name).lower()


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


def resolve_week(
    explicit: int | None,
    config: dict[str, Any],
    *,
    cache_root: Path | None = None,
) -> int:
    """Return the NFL week a live run is for. Never guesses, never returns 0.

    ONLY FOR A LIVE RUN. A --fixtures run takes its week from the fixture
    league's own "current_week" and must never reach this function, which
    talks to the network.

    Resolution order:

    1. explicit (the wrapper's --week) wins whenever it is not None, with
       no network call at all. This is the escape hatch for a backfill, or
       for any week ESPN and the owner's league disagree about.
    2. Otherwise engine.sources.schedule.fetch_current_week is asked. It is
       reached through the module, not imported by name, so a test can
       monkeypatch it in place, the same seam engine.live_league uses.

    Everything below raises EngineError rather than returning a number it
    is not sure of, because every later fetch in the run is keyed on this
    week: a wrong week here is not a degraded run, it is a confident email
    about the wrong week's games.

    - The source being unavailable (including a stale cache entry, which
      fetch_current_week reports as unavailable on purpose) raises.
    - ESPN's season year disagreeing with config's league.season raises. In
      January those genuinely differ, and silently taking ESPN's would run
      against a season the config was never pointed at.
    - season_type 1 (preseason) resolves to week 1: the fantasy season has
      not started, and week 1 is the next real week either way.
    - season_type 2 (regular season) resolves to ESPN's own week number.
    - season_type 3 (postseason) raises. ESPN restarts week numbering at 1
      for the wild card round, so taking that number would silently run
      the regular season's week 1 in January.
    - Any other season_type, or a week number below 1, raises.
    """
    if explicit is not None:
        return explicit

    result = schedule_source.fetch_current_week(cache_root=cache_root)
    if not result["available"]:
        raise EngineError(
            "could not resolve the current NFL week from ESPN "
            f"({result['reason']}); pass --week NN explicitly"
        )

    data = result["data"]
    season = data["season"]
    season_type = data["season_type"]
    week = data["week"]

    configured_season = config["league"]["season"]
    if int(season) != int(configured_season):
        raise EngineError(
            f"ESPN reports season {season} but config's league.season is "
            f"{configured_season}; pass --week NN explicitly, or point the "
            "config at the season you mean"
        )

    if season_type == schedule_source.PRESEASON_TYPE:
        return 1
    if season_type == schedule_source.POSTSEASON_TYPE:
        raise EngineError(
            "ESPN reports the postseason (season_type 3), where week numbering "
            "restarts at 1 and does not mean the same thing; pass --week NN "
            "explicitly if this league is still running"
        )
    if season_type != schedule_source.REGULAR_SEASON_TYPE:
        raise EngineError(
            f"ESPN reports an unrecognized season_type {season_type!r}; "
            "pass --week NN explicitly"
        )

    if week < 1:
        raise EngineError(
            f"ESPN reports week {week!r}, which is not a usable week number; "
            "pass --week NN explicitly"
        )
    return week


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

    On the claude path, draft_body drafts a body from brief PLUS trades and
    inactive_changes (each rendered as its own labelled fenced JSON block
    appended after the brief, via draft_body's extra_context parameter, only
    when the corresponding argument here is not None) so weekly's trade
    ideas and inactive's change list actually reach the model instead of
    being silently dropped. engine.prose_gate.check_draft then validates
    the draft against this SAME brief, widened with a "trades" key when
    trades is not None (see engine.prose_gate.brief_player_names, which
    reads it) so a trade partner's player, named nowhere else in brief,
    does not trip an unknown-player violation just for having been offered
    in a trade idea. A rejection, or draft_body returning no body at all,
    is logged to stderr (the gate's own violations when there were any to
    check, the draft failure reason otherwise) and falls back to
    render_plain_email, because the plan says the email always goes out.

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

    extra_context = _build_extra_context(trades, inactive_changes)
    body, reason = draft_body(routine, brief, config, extra_context=extra_context, runner=runner)
    if body is None:
        print(f"run_common: no claude draft used ({reason})", file=sys.stderr)
    else:
        brief_for_check = brief
        if trades is not None:
            # trades names players on other teams' rosters, which brief
            # itself never carries (brief only names the owner's own team
            # and this week's opponent); brief_player_names reads a
            # "trades" key exactly this shape when present, so a widened
            # copy is passed here without mutating the caller's brief.
            brief_for_check = dict(brief)
            brief_for_check["trades"] = trades
        result = check_draft(body, brief_for_check)
        if result["ok"]:
            return subject, body, "claude"
        print(format_violations(result), file=sys.stderr)

    return subject, _plain_body(), "plain"


def _build_extra_context(
    trades: dict[str, Any] | None, inactive_changes: list[dict[str, Any]] | None
) -> str:
    """Return draft_body's extra_context string for trades and inactive_changes.

    Each argument that is not None becomes its own labelled fenced JSON
    block; both, either, or neither may be present. Returns "" when both
    are None, which draft_body already treats as "nothing to append".
    """
    parts: list[str] = []
    if trades is not None:
        parts.append("TRADE IDEAS:\n```json\n" + json.dumps(trades, indent=2) + "\n```")
    if inactive_changes is not None:
        parts.append(
            "INACTIVE CHANGES:\n```json\n" + json.dumps(inactive_changes, indent=2) + "\n```"
        )
    return "\n\n".join(parts)


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
