"""Kickoff-window math and the on-disk repeat-suppression file for gameday runs.

This module answers three kinds of question a gameday or inactive-player
routine needs, all offline: which NFL games fall on a given calendar date,
whether "now" sits inside a fixed minute window before a given kickoff, and
whether a given routine has already sent an alert for a given kickoff window
so it does not send the same alert twice.

Schedule data is always the "data" dict shape engine.sources.schedule.
fetch_week_schedule puts inside its envelope: {"season", "week",
"season_type", "source_url", "games", "count"}, where each games[] entry has
the keys engine.sources.schedule._parse_event documents (game_id,
kickoff_utc, name, short_name, home_team, away_team, venue, city, state,
country, indoor, neutral_site, status_state, status_detail, completed).
load_fixture_schedule reads exactly that shape from
fixtures/phase4/schedule.json, the offline schedule this repo's Phase 4
gameday and inactive-player wrappers run against in --fixtures mode. Every
team abbreviation this module compares is passed through
engine.sources.base.normalize_team_abbreviation first, so a caller may pass
a raw feed's spelling and still match.

Repeat suppression: a routine that would otherwise send the same alert every
time it runs inside one kickoff window records the keys it has already sent
(one key per line) in a small text file under runs/<season>/wk<NN>/, named
by window_key(). sent_path, read_sent and write_sent are the whole of that
mechanism; read_sent never raises, since a missing file is simply "nothing
sent yet".

Public names: DEFAULT_INACTIVE_WINDOW_MINUTES, FIXTURE_SCHEDULE_PATH,
load_fixture_schedule, parse_iso_utc, starter_nfl_teams, games_on_date,
earliest_kickoff_on_date, next_kickoff, minutes_until, inside_window,
window_key, sent_path, read_sent, write_sent.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from engine.common import REPO_ROOT, EngineError, atomic_write, load_json
from engine.fixtures import get_player, get_team, starting_slot_units
from engine.sources.base import normalize_team_abbreviation

DEFAULT_INACTIVE_WINDOW_MINUTES = 75
FIXTURE_SCHEDULE_PATH: Path = REPO_ROOT / "fixtures" / "phase4" / "schedule.json"


def load_fixture_schedule(path: Path | None = None) -> dict[str, Any]:
    """Return the Phase 4 fixture schedule's "data" dict, from disk.

    path defaults to FIXTURE_SCHEDULE_PATH. Reads through
    engine.common.load_json, so a missing file or a non-object top level
    raises EngineError naming the path, never a bare exception.
    """
    if path is None:
        path = FIXTURE_SCHEDULE_PATH
    return load_json(path)


def parse_iso_utc(value: datetime | str) -> datetime:
    """Return a timezone-aware UTC datetime for value.

    Accepts an already timezone-aware datetime (converted to UTC), a naive
    datetime (treated as already UTC), or a string in any of these forms:
    "2026-09-14T11:45Z", "2026-09-14T11:45:00Z", "2026-09-14T11:45:00+00:00",
    or a bare "2026-09-14" (read as midnight UTC that day). Anything else,
    including an unparseable string, raises EngineError naming the bad
    value; this never returns a naive datetime.
    """
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    if isinstance(value, str) and value.strip():
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise EngineError(f"unparseable timestamp: {value!r}") from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    raise EngineError(f"unparseable timestamp: {value!r}")


def _to_utc_date(value: str | date | datetime) -> date:
    """Return the UTC calendar date value refers to.

    A datetime (naive or aware) is converted through parse_iso_utc first,
    then its .date() is taken. A bare date object is returned as is. A
    string is parsed through parse_iso_utc, which accepts a bare
    "YYYY-MM-DD" string as midnight UTC that day. Anything else raises
    EngineError naming the bad value.
    """
    if isinstance(value, datetime):
        return parse_iso_utc(value).date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        return parse_iso_utc(value).date()
    raise EngineError(f"unparseable date value: {value!r}")


def starter_nfl_teams(
    league: dict[str, Any],
    team_id: str,
    week: int,
    *,
    lineup: dict[str, Any] | None = None,
) -> list[str]:
    """Return the deduped, sorted NFL team codes of team_id's starters.

    week is accepted for interface symmetry with a future live-data
    equivalent of this function; against this repo's fixture-shaped roster
    snapshot it is not otherwise needed, since selected_slot already
    reflects the team's current lineup.

    When lineup is given, its "starter_ids" list names the starters
    directly. Otherwise the starters are read from team_id's roster: every
    entry whose selected_slot names one of the league's starting slots, per
    engine.fixtures.starting_slot_units. A player who is out or on bye is
    still included, since his game's kickoff still defines the window his
    team's alert should watch. Every code is normalized through
    engine.sources.base.normalize_team_abbreviation before being returned.
    """
    del week  # accepted for interface symmetry only, see docstring above.

    if lineup is not None:
        player_ids = list(lineup.get("starter_ids", []))
    else:
        starting_slots = {unit["slot"] for unit in starting_slot_units(league)}
        team = get_team(league, team_id)
        player_ids = [
            entry["player_id"]
            for entry in team["roster"]
            if entry.get("selected_slot") in starting_slots
        ]

    teams: set[str] = set()
    for player_id in player_ids:
        player = get_player(league, player_id)
        normalized = normalize_team_abbreviation(player.get("nfl_team"))
        if normalized:
            teams.add(normalized)
    return sorted(teams)


def _games_for_teams(
    schedule_data: dict[str, Any], teams: list[str] | None
) -> list[dict[str, Any]]:
    """Return schedule_data's games, optionally filtered to teams.

    Mirrors engine.sources.schedule's own defensive style: unusable input
    (schedule_data is not a dict, or has no "games" list) returns [] rather
    than raising. Each entry in teams is normalized before matching, so a
    caller may pass a raw feed's spelling.
    """
    if not isinstance(schedule_data, dict):
        return []
    games = schedule_data.get("games")
    if not isinstance(games, list):
        return []
    usable = [game for game in games if isinstance(game, dict)]
    if teams is None:
        return usable

    normalized_teams = {normalize_team_abbreviation(team) for team in teams}
    return [
        game
        for game in usable
        if game.get("home_team") in normalized_teams
        or game.get("away_team") in normalized_teams
    ]


def games_on_date(
    schedule_data: dict[str, Any],
    date_value: str | date | datetime,
    teams: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Return the games whose kickoff_utc falls on date_value's UTC date.

    Optionally filtered to games involving any of teams (see
    engine.sources.base.normalize_team_abbreviation). Sorted by
    (kickoff_utc, game_id), matching engine.sources.schedule's own games
    ordering.
    """
    target = _to_utc_date(date_value)
    candidates = _games_for_teams(schedule_data, teams)
    matched = [
        game
        for game in candidates
        if parse_iso_utc(game["kickoff_utc"]).date() == target
    ]
    matched.sort(key=lambda game: (game["kickoff_utc"], game["game_id"]))
    return matched


def earliest_kickoff_on_date(
    schedule_data: dict[str, Any],
    date_value: str | date | datetime,
    teams: list[str] | None = None,
) -> str | None:
    """Return the earliest kickoff_utc on date_value, or None if there is none."""
    matched = games_on_date(schedule_data, date_value, teams=teams)
    if not matched:
        return None
    return matched[0]["kickoff_utc"]


def next_kickoff(
    schedule_data: dict[str, Any],
    now_value: str | datetime,
    teams: list[str] | None = None,
) -> str | None:
    """Return the earliest kickoff_utc at or after now_value, among teams.

    Unlike games_on_date, this looks across every game in schedule_data, not
    just one calendar date. Returns None when there is no such game.
    """
    now = parse_iso_utc(now_value)
    candidates = [
        game
        for game in _games_for_teams(schedule_data, teams)
        if parse_iso_utc(game["kickoff_utc"]) >= now
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda game: (game["kickoff_utc"], game["game_id"]))
    return candidates[0]["kickoff_utc"]


def minutes_until(kickoff_value: str | datetime, now_value: str | datetime) -> float:
    """Return how many minutes now_value is before kickoff_value.

    Negative when now_value is after kickoff_value.
    """
    kickoff = parse_iso_utc(kickoff_value)
    now = parse_iso_utc(now_value)
    return (kickoff - now).total_seconds() / 60.0


def inside_window(
    kickoff_value: str | datetime,
    now_value: str | datetime,
    window_minutes: float = DEFAULT_INACTIVE_WINDOW_MINUTES,
) -> bool:
    """Return True when now_value is within window_minutes before kickoff_value.

    Both boundaries are inclusive: exactly at kickoff (0 minutes to go) and
    exactly window_minutes before kickoff both count as inside.
    """
    remaining = minutes_until(kickoff_value, now_value)
    return 0 <= remaining <= window_minutes


def window_key(kickoff_value: str | datetime) -> str:
    """Return a filename-safe key identifying kickoff_value's window.

    "2026-09-14T13:00:00Z" -> "20260914T1300Z".
    """
    kickoff = parse_iso_utc(kickoff_value)
    return kickoff.strftime("%Y%m%dT%H%MZ")


def sent_path(season: int, week: int, window: str, runs_root: Path | None = None) -> Path:
    """Return the repeat-suppression file path for one season/week/window.

    runs_root defaults to REPO_ROOT / "runs" (gitignored). The file itself
    is not created by this function; see write_sent.
    """
    if runs_root is None:
        runs_root = REPO_ROOT / "runs"
    return runs_root / str(season) / ("wk%02d" % week) / f"inactive-{window}.sent"


def read_sent(path: Path) -> set[str]:
    """Return the set of keys already recorded at path.

    A missing, unreadable, or otherwise unusable file reads as "nothing
    sent yet": this never raises. Blank lines are ignored.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return set()
    return {line.strip() for line in text.splitlines() if line.strip()}


def write_sent(path: Path, keys: Iterable[str]) -> None:
    """Write keys to path, one per line, sorted, creating parents as needed.

    Written through engine.common.atomic_write, so a reader never observes
    a half-written file.
    """
    text = "".join(f"{key}\n" for key in sorted(set(keys)))
    atomic_write(path, text)
