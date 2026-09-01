"""Late breaking NFL news for a named set of players, read by Claude.

This is the one source module that has no API behind it. Sleeper, ESPN and
Open-Meteo all answer a fixed question with a fixed shape; "did anything
happen to these players that the numbers do not show yet" has no endpoint,
so it is asked of Claude, which searches the web and answers in a JSON
shape this module defines and then validates.

It is also the one source module with a per-run cost. Every other source is
free and no-auth; this one spawns a `claude` subprocess. That is why it is
called by exactly one routine (engine.weekly_run, whose plan item 4 is
"anything the news pass turned up that the numbers do not show"), never on
a --fixtures run, and never more than once per run.

DETERMINISM: this module does not break docs/plan.md's split, it feeds it.
Claude reports what it read; it never produces a number, a projection or a
verdict, and nothing here is allowed to change one. The items this module
returns are context handed to the prose step, exactly like trade ideas
are, and engine.prose_gate still checks the finished draft against the
brief Python computed.

THE SHAPE CLAUDE MUST RETURN, stated in NEWS_PROMPT_TEMPLATE and enforced
by _parse_items:

    {"items": [{"player": "Dax Voss", "note": "limited in Wednesday
      practice with a shoulder injury", "source": "espn.com"}, ...]}

Every item is validated independently: an item that is not an object, or
whose "player" or "note" is missing or not a non-empty string, is dropped
rather than failing the whole fetch, the same rule
engine.sources.schedule._parse_event follows for one malformed event in a
sixteen game response. "source" is optional and becomes "" when absent.
A response that does not decode as JSON at all, or has no "items" list,
degrades to unavailable_result(...) like any other dead source.

Cache: one entry per distinct set of player names, keyed by a hash of that
set, through engine.sources.base's read_cache / write_cache rather than
fetch_cached_json, since there is no URL to fetch. A fresh entry skips the
subprocess entirely; a stale entry is served only when a fresh call fails,
which is the same "stale data beats no data" rule the other sources use
and is safe here because a day-old injury note is still worth reading.

Public names: SOURCE_NAME, NEWS_MAX_AGE_SECONDS, MAX_PLAYERS,
NEWS_PROMPT_TEMPLATE, fetch_news.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Callable

from engine.common import timestamp
from engine.sources.base import (
    cache_age_seconds, disabled_result, read_cache, source_result,
    unavailable_result, write_cache,
)

SOURCE_NAME = "news"

# One hour. A weekly run asks once, so this mostly matters when the same
# run is repeated by hand while debugging, and when a routine reruns after
# a transient failure.
NEWS_MAX_AGE_SECONDS = 3600

# A cap on how many players are named in one prompt. A full roster plus
# waiver targets is comfortably under this; the cap exists so a caller that
# passes something unexpected cannot build an enormous prompt.
MAX_PLAYERS = 60

NEWS_PROMPT_TEMPLATE = """Search the web for late breaking NFL news about these players, for \
the current week:

{players}

Report only news that a fantasy manager would act on and that a projection would not already \
show: an injury or a change in injury status, practice participation, a snap count or role \
change, a depth chart move, a suspension, a coach's comment about usage, or a weather or \
travel issue specific to that player's game.

Rules:
- Only report a player from the list above.
- Report nothing at all for a player with no such news. Most players will have none.
- Never state a projection, a point total, or a start/sit verdict. Report only what happened.
- Do not guess or infer. If you did not read it, leave it out.
- One short factual sentence per item.

Output ONLY a JSON object in exactly this shape, with no other text before or after it:

{{"items": [{{"player": "<name exactly as written above>", "note": "<one sentence>", \
"source": "<domain you read it on>"}}]}}

An empty list is a valid and expected answer: {{"items": []}}
"""

# The shape of the runner this module calls, matching
# engine.run_common.run_claude: (prompt, *, claude_bin=..., timeout=...) ->
# (returncode, stdout, stderr).
_RunnerFn = Callable[..., tuple[int, str, str]]

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


def _cache_key(players: list[str]) -> str:
    """Return a filename-safe cache key for one exact set of player names.

    Order and letter case do not change the key, since neither changes the
    question being asked. The digest is truncated to 16 hex characters,
    which is far more than enough to keep one owner's handful of distinct
    rosters apart and keeps the filename readable.
    """
    normalized = sorted({name.strip().lower() for name in players})
    digest = hashlib.sha256("\n".join(normalized).encode("utf-8")).hexdigest()
    return f"news-{digest[:16]}"


def _strip_fences(text: str) -> str:
    """Return text with one leading and one trailing markdown code fence removed.

    Models wrap JSON in ```json ... ``` often enough that not handling it
    would turn a correct answer into a parse failure.
    """
    stripped = text.strip()
    if "```" not in stripped:
        return stripped
    first = stripped.find("```")
    last = stripped.rfind("```")
    if last > first:
        stripped = stripped[first:last]
    return _FENCE_RE.sub("", stripped).strip()


def _parse_items(raw: str) -> list[dict[str, str]] | None:
    """Return the validated items in raw, or None when raw is not usable.

    None means "this response was not the documented shape at all" (it did
    not decode as JSON, was not an object, or carried no "items" list) and
    becomes an unavailable result. An empty list is a real answer, not a
    failure: most weeks most players have no news.

    Each item is validated on its own and a bad one is dropped, never
    raised on. "player" and "note" must both be non-empty strings;
    "source" is optional and becomes "".
    """
    text = _strip_fences(raw)
    if not text:
        return None
    try:
        payload = json.loads(text)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    raw_items = payload.get("items")
    if not isinstance(raw_items, list):
        return None

    items: list[dict[str, str]] = []
    for raw_item in raw_items:
        if not isinstance(raw_item, dict):
            continue
        player = raw_item.get("player")
        note = raw_item.get("note")
        if not isinstance(player, str) or not player.strip():
            continue
        if not isinstance(note, str) or not note.strip():
            continue
        source = raw_item.get("source")
        items.append({
            "player": player.strip(),
            "note": note.strip(),
            "source": source.strip() if isinstance(source, str) else "",
        })
    return items


def fetch_news(
    players: list[str],
    *,
    enabled: bool = True,
    claude_bin: str = "claude",
    timeout: int = 600,
    cache_root: Path | None = None,
    max_age_seconds: int = NEWS_MAX_AGE_SECONDS,
    runner: _RunnerFn | None = None,
    force_refresh: bool = False,
) -> dict[str, Any]:
    """Return the result envelope for late breaking news about players.

    players is a list of player names, normally every name the brief
    already knows (see engine.prose_gate.brief_player_names). Blank names
    are dropped and duplicates collapse; more than MAX_PLAYERS names is a
    caller error and degrades to unavailable rather than building an
    enormous prompt.

    enabled=False returns disabled_result(SOURCE_NAME) immediately, with
    no subprocess and no disk access at all. An empty players list does
    the same thing as an empty answer: an available result with no items,
    without spawning anything.

    On success, envelope["data"] is:
        {"players": [<the names asked about, sorted>],
         "items": [{"player", "note", "source"}, ...],
         "count": len(items)}

    runner defaults to engine.run_common.run_claude, imported inside this
    function rather than at module scope: run_claude is the only place in
    this repo allowed to spawn a claude subprocess, and a leaf source
    module must not take a top level import on the wrapper layer that
    imports it back. A test passes its own runner and never spawns
    anything.

    Every failure degrades to unavailable_result(...) rather than raising:
    a non-zero exit from claude, empty output, output that is not the
    documented JSON shape, or claude not being installed at all. A news
    pass that could not run must cost the weekly email its news section,
    never the email itself.
    """
    if not enabled:
        return disabled_result(SOURCE_NAME)

    cleaned = sorted({name.strip() for name in players if isinstance(name, str) and name.strip()})
    if not cleaned:
        return source_result(
            SOURCE_NAME,
            data={"players": [], "items": [], "count": 0},
            fetched_at=timestamp(),
        )
    if len(cleaned) > MAX_PLAYERS:
        return unavailable_result(
            SOURCE_NAME, f"too many players to ask about ({len(cleaned)} > {MAX_PLAYERS})"
        )

    cache_key = _cache_key(cleaned)
    entry = read_cache(cache_key, cache_root)
    if entry is not None and not force_refresh and cache_age_seconds(entry) <= max_age_seconds:
        payload = entry["payload"]
        if isinstance(payload, dict) and isinstance(payload.get("items"), list):
            return source_result(
                SOURCE_NAME,
                data={
                    "players": cleaned,
                    "items": payload["items"],
                    "count": len(payload["items"]),
                },
                fetched_at=entry["fetched_at"],
            )

    def _stale_or_unavailable(reason: str) -> dict[str, Any]:
        """Serve a stale cache entry if there is one, else report unavailable."""
        if entry is not None:
            payload = entry["payload"]
            if isinstance(payload, dict) and isinstance(payload.get("items"), list):
                return source_result(
                    SOURCE_NAME,
                    data={
                        "players": cleaned,
                        "items": payload["items"],
                        "count": len(payload["items"]),
                    },
                    stale=True,
                    fetched_at=entry["fetched_at"],
                )
        return unavailable_result(SOURCE_NAME, reason)

    if runner is None:
        from engine.run_common import run_claude

        runner = run_claude

    prompt = NEWS_PROMPT_TEMPLATE.format(players="\n".join(f"- {name}" for name in cleaned))

    try:
        returncode, stdout, stderr = runner(prompt, claude_bin=claude_bin, timeout=timeout)
    except OSError as exc:
        # claude is not installed, or is not on PATH. engine.run_common's
        # own prose call does not catch this, but a source module must:
        # a missing news section is not a reason to lose the email.
        return _stale_or_unavailable(f"could not run {claude_bin}: {exc}")

    if returncode != 0:
        detail = (stderr or stdout or "").strip() or "(no output)"
        return _stale_or_unavailable(f"claude exited {returncode}: {detail}")

    items = _parse_items(stdout or "")
    if items is None:
        return _stale_or_unavailable("claude did not return the documented news JSON shape")

    fetched_at = timestamp()
    write_cache(
        cache_key,
        "claude:news",
        {"players": cleaned, "items": items},
        cache_root=cache_root,
        fetched_at=fetched_at,
    )
    return source_result(
        SOURCE_NAME,
        data={"players": cleaned, "items": items, "count": len(items)},
        fetched_at=fetched_at,
    )
