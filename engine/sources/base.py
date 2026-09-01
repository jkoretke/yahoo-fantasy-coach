"""Shared foundation for every external data source module.

engine.sources.sleeper, engine.sources.schedule, engine.sources.injuries and
engine.sources.weather each wrap one free, no-auth external API (Sleeper,
ESPN's public endpoints, Open-Meteo). All four are built against exactly the
API this module defines, so that each one can be written, tested and
degraded independently of the others. This module is the only place in the
repo that calls urllib.request.urlopen; every other module reaches the
network only through fetch_json or fetch_cached_json below.

Three concerns live here:

HTTP: fetch_json(url, ...) issues one GET request with stdlib urllib and
    returns the decoded JSON body (object or list), converting every kind
    of network or decoding failure into SourceUnavailable so a caller never
    has to catch urllib.error directly.

Cache: a small per-run disk cache under CACHE_ROOT (or an override,
    following the runs_root pattern engine.brief.brief_path already uses
    for this repo's other filesystem root). cache_path, read_cache and
    write_cache manage one JSON envelope file per cache key; cache_age_seconds
    and fetch_cached_json build the "fresh hit skips the network, stale hit
    is the fallback when the network fails" policy that keeps a rate limit
    or an outage from breaking a scheduled run. A corrupt or missing cache
    file always reads as a plain cache miss, never an exception.
    prune_cache deletes entries nothing will ever serve again, so the
    directory does not grow without bound on a long lived box deploy.

Result envelope: source_result, disabled_result and unavailable_result give
    every source module's public function the same plain, JSON serializable
    return shape (SOURCE_RESULT_KEYS), so a dead endpoint degrades that one
    section of a brief instead of raising out of the run.

Two normalizers, normalize_name and normalize_team_abbreviation (backed by
TEAM_ABBREVIATION_ALIASES), live here rather than in either feed module
because engine.sources.sleeper and engine.sources.injuries both need them
and neither may depend on the other.

Public names: SourceUnavailable, CACHE_ROOT, DEFAULT_TIMEOUT_SECONDS,
DEFAULT_MAX_AGE_SECONDS, CACHE_PRUNE_MAX_AGE_SECONDS, DISABLED_REASON,
SOURCE_RESULT_KEYS, TEAM_ABBREVIATION_ALIASES, fetch_json, cache_path,
read_cache, write_cache, cache_age_seconds, prune_cache, fetch_cached_json,
source_result, disabled_result, unavailable_result, normalize_name,
normalize_team_abbreviation.
"""
from __future__ import annotations

import json
import re
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from engine.common import EngineError, REPO_ROOT, timestamp, write_json

CACHE_ROOT: Path = REPO_ROOT / "runs" / "cache"
DEFAULT_TIMEOUT_SECONDS: float = 20.0
DEFAULT_MAX_AGE_SECONDS: int = 21600
# Seven days. Deliberately far longer than any source's own max age: an
# entry this old is past being served fresh AND past being worth serving
# stale by every source in this repo, so deleting it can change no run's
# outcome. A module constant rather than a config key on purpose, since one
# more setting to validate, document and keep true buys nothing here.
CACHE_PRUNE_MAX_AGE_SECONDS: int = 7 * 86400
DISABLED_REASON: str = "disabled"
SOURCE_RESULT_KEYS: tuple[str, ...] = (
    "source",
    "available",
    "stale",
    "reason",
    "fetched_at",
    "data",
)

# A non-canonical NFL team abbreviation, as some feed spells it, mapped to
# the canonical Sleeper-style code. ESPN sends WSH for Washington, some
# feeds still carry the pre-relocation OAK/SD/STL codes, and so on; this is
# the one place that difference is reconciled for every source module.
TEAM_ABBREVIATION_ALIASES: dict[str, str] = {
    "WSH": "WAS",
    "WFT": "WAS",
    "JAC": "JAX",
    "GBP": "GB",
    "KCC": "KC",
    "NOS": "NO",
    "SFO": "SF",
    "TBB": "TB",
    "NEP": "NE",
    "LVR": "LV",
    "OAK": "LV",
    "SD": "LAC",
    "SDG": "LAC",
    "STL": "LAR",
    "ARZ": "ARI",
    "BLT": "BAL",
    "CLV": "CLE",
    "HST": "HOU",
}

_CACHE_KEY_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_GENERATIONAL_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}


class SourceUnavailable(EngineError):
    """A single external source could not be read this run.

    This subclasses EngineError on purpose: any existing code that catches
    EngineError still catches this, while a caller that wants to degrade
    only one source can catch this narrower type instead and leave every
    other EngineError to propagate.
    """


def fetch_json(
    url: str,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    service: str = "source",
    headers: dict[str, str] | None = None,
) -> Any:
    """Fetch url with one GET request and return the decoded JSON body.

    The body may be a JSON object or a JSON list; several of the feeds this
    repo reads return a top level list, so this never requires a dict. The
    given headers are sent as is; a "User-Agent" and "Accept" header are
    added only when the caller did not already supply one.

    Every failure mode (an HTTP error status, a network or timeout error,
    or a body that does not decode as JSON) is converted to
    SourceUnavailable so a caller never has to know about urllib.error.
    """
    # Normalize caller-supplied header names to the same capitalize() form
    # urllib.request.Request stores them under, so a caller that already
    # set (say) "user-agent" is not clobbered by our default under a
    # differently cased key.
    request_headers = {key.capitalize(): value for key, value in (headers or {}).items()}
    request_headers.setdefault("User-agent", "yahoo-fantasy-coach/1.0")
    request_headers.setdefault("Accept", "application/json")
    request = urllib.request.Request(url, method="GET", headers=request_headers)

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        raise SourceUnavailable(f"{service} GET {url} failed ({exc.code})") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise SourceUnavailable(f"{service} GET {url} failed: {exc}") from exc
    except ValueError as exc:
        raise SourceUnavailable(f"{service} GET {url} returned invalid JSON") from exc


def cache_path(cache_key: str, cache_root: Path | None = None) -> Path:
    """Return the on-disk path for cache_key under cache_root.

    cache_root defaults to CACHE_ROOT. cache_key must match
    ^[A-Za-z0-9._-]+$ and must not be "." or "..", or this raises
    EngineError rather than SourceUnavailable: a bad cache key is a
    programmer error in the calling module, not an external outage, and it
    is the guard against writing outside cache_root.
    """
    if cache_root is None:
        cache_root = CACHE_ROOT
    if cache_key in (".", "..") or not _CACHE_KEY_RE.match(cache_key):
        raise EngineError(f"invalid cache key: {cache_key!r}")
    return cache_root / f"{cache_key}.json"


def read_cache(cache_key: str, cache_root: Path | None = None) -> dict[str, Any] | None:
    """Return the cache envelope for cache_key, or None on any kind of miss.

    A miss is: the file does not exist, cannot be read, is not valid JSON,
    is not a JSON object, or is missing "fetched_at" or "payload". This
    never raises for a corrupt or missing file; a caller reads None as a
    plain cache miss. engine.common.load_json is deliberately not used
    here, since it raises on exactly the two cases (missing file, non-dict
    top level) that this function must instead treat as a miss.
    """
    path = cache_path(cache_key, cache_root)
    try:
        with path.open("r", encoding="utf-8") as handle:
            entry = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(entry, dict):
        return None
    if "fetched_at" not in entry or "payload" not in entry:
        return None
    return entry


def write_cache(
    cache_key: str,
    url: str,
    payload: Any,
    cache_root: Path | None = None,
    fetched_at: str | None = None,
) -> None:
    """Write a {"fetched_at", "url", "payload"} envelope for cache_key.

    fetched_at defaults to engine.common.timestamp(). Any failure to write
    (an unwritable directory, a full disk, a payload that is not JSON
    serializable) is swallowed silently: only the fetch that produced
    payload is load bearing, and a cache write failure must never turn a
    successful fetch into a failed one.
    """
    if fetched_at is None:
        fetched_at = timestamp()
    path = cache_path(cache_key, cache_root)
    envelope = {"fetched_at": fetched_at, "url": url, "payload": payload}
    try:
        write_json(path, envelope)
    except (OSError, TypeError, ValueError):
        pass


def cache_age_seconds(entry: dict[str, Any], now: datetime | None = None) -> float:
    """Return how old entry["fetched_at"] is, in seconds, against now.

    now defaults to datetime.now(timezone.utc). fetched_at is a UTC ISO
    8601 string ending in "Z"; it is parsed by replacing that trailing "Z"
    with "+00:00" before datetime.fromisoformat. A missing or unparseable
    fetched_at returns float("inf"), so a broken timestamp always reads as
    stale rather than as fresh.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    raw = entry.get("fetched_at") if entry else None
    if not isinstance(raw, str) or not raw:
        return float("inf")
    text = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return float("inf")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return (now - parsed).total_seconds()


def prune_cache(
    cache_root: Path | None = None,
    *,
    max_age_seconds: int = CACHE_PRUNE_MAX_AGE_SECONDS,
) -> int:
    """Delete cache entries older than max_age_seconds. Return how many went.

    cache_root defaults to CACHE_ROOT. Only files directly inside it whose
    name ends in ".json" are considered, so nothing outside this module's
    own cache format is ever touched.

    A file is deleted when its envelope's "fetched_at" is older than
    max_age_seconds, and also when the envelope cannot be read at all: a
    corrupt or truncated file is already a permanent cache miss to
    read_cache (cache_age_seconds reports it as infinitely old), so
    keeping it only costs disk.

    Never raises. Every OSError is swallowed, one file at a time, exactly
    like write_cache: pruning is housekeeping, and a locked or unreadable
    file must never turn a successful run into a failed one. A missing
    cache directory is simply nothing to do.
    """
    if cache_root is None:
        cache_root = CACHE_ROOT

    try:
        entries = sorted(cache_root.glob("*.json"))
    except OSError:
        return 0

    removed = 0
    for path in entries:
        try:
            if not path.is_file():
                continue
        except OSError:
            continue

        entry: dict[str, Any] | None
        try:
            with path.open("r", encoding="utf-8") as handle:
                loaded = json.load(handle)
            entry = loaded if isinstance(loaded, dict) else None
        except (OSError, ValueError):
            entry = None

        if entry is not None and cache_age_seconds(entry) <= max_age_seconds:
            continue

        try:
            path.unlink()
        except OSError:
            continue
        removed += 1

    return removed


def fetch_cached_json(
    url: str,
    cache_key: str,
    *,
    max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS,
    cache_root: Path | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    service: str = "source",
    headers: dict[str, str] | None = None,
    force_refresh: bool = False,
) -> tuple[Any, str, bool]:
    """Return (payload, fetched_at, stale) for url, using a per-run cache.

    The cache is always read first, even when force_refresh is True: the
    existing entry is the fallback if a forced refresh then fails against a
    dead endpoint, so skipping that read would turn a degrade into a raise.

    A fresh cache entry (age <= max_age_seconds) is returned without
    calling fetch_json at all, unless force_refresh is set. Otherwise
    fetch_json is called; on success the cache is rewritten and
    stale=False. If fetch_json raises SourceUnavailable and a cache entry
    exists (fresh, stale, or forced past), that entry's payload is returned
    with stale=True: stale data beats no data. If no cache entry exists,
    the SourceUnavailable is re-raised.
    """
    entry = read_cache(cache_key, cache_root)

    if entry is not None and not force_refresh and cache_age_seconds(entry) <= max_age_seconds:
        return entry["payload"], entry["fetched_at"], False

    try:
        payload = fetch_json(url, timeout=timeout, service=service, headers=headers)
    except SourceUnavailable:
        if entry is not None:
            return entry["payload"], entry["fetched_at"], True
        raise

    fetched_at = timestamp()
    write_cache(cache_key, url, payload, cache_root=cache_root, fetched_at=fetched_at)
    return payload, fetched_at, False


def source_result(
    source: str,
    *,
    data: Any = None,
    available: bool = True,
    stale: bool = False,
    reason: str | None = None,
    fetched_at: str | None = None,
) -> dict[str, Any]:
    """Return the common result envelope every source module's function uses.

    The returned dict's keys are exactly SOURCE_RESULT_KEYS, in that order,
    and every value is a plain JSON type, so the result always survives
    json.dumps unchanged.
    """
    return {
        "source": source,
        "available": available,
        "stale": stale,
        "reason": reason,
        "fetched_at": fetched_at,
        "data": data,
    }


def disabled_result(source: str) -> dict[str, Any]:
    """Return the envelope for a source that config has switched off."""
    return source_result(source, available=False, reason=DISABLED_REASON, data=None)


def unavailable_result(source: str, reason: str) -> dict[str, Any]:
    """Return the envelope for a source that could not be read this run."""
    return source_result(source, available=False, reason=reason, data=None)


def normalize_name(name: str | None) -> str:
    """Return a deterministic join key for a player name.

    This lets feeds that spell the same player's name differently (accents,
    punctuation, a generational suffix) agree on one key. None or a blank
    string returns "". Otherwise: Unicode accents are folded away (NFKD
    decomposition, then combining marks dropped), the result is
    lowercased, every character that is not a letter, digit or space
    becomes a space, and the whitespace-split tokens have any trailing
    generational suffix ("jr", "sr", "ii", "iii", "iv", "v") dropped, one at
    a time from the end, but never down to zero tokens. The surviving
    tokens are joined with a single space.
    """
    if name is None:
        return ""
    if not name.strip():
        return ""

    decomposed = unicodedata.normalize("NFKD", name)
    without_marks = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    lowered = without_marks.lower()
    cleaned = "".join(ch if (ch.isalnum() or ch.isspace()) else " " for ch in lowered)
    tokens = cleaned.split()

    while len(tokens) > 1 and tokens[-1] in _GENERATIONAL_SUFFIXES:
        tokens.pop()

    return " ".join(tokens)


def normalize_team_abbreviation(value: str | None) -> str:
    """Return the canonical Sleeper-style code for an NFL team abbreviation.

    None or a blank string returns "". Otherwise value is stripped,
    uppercased, and passed through TEAM_ABBREVIATION_ALIASES; an unknown
    code passes through unchanged (uppercased) rather than raising, since a
    new or unmapped code should degrade to "unrecognized" data, not crash
    the module reading it.
    """
    if value is None:
        return ""
    stripped = value.strip()
    if not stripped:
        return ""
    code = stripped.upper()
    return TEAM_ABBREVIATION_ALIASES.get(code, code)
