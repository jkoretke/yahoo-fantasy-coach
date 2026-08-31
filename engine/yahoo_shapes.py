"""Pure-function parsing of Yahoo Fantasy Sports API payloads into the
shapes engine/fixtures.py, engine/scoring.py, engine/lineup.py and
engine/waivers.py already consume.

This module does no network access and no disk access, and it must never
import this repo's pinned Yahoo fantasy client library (see
requirements.txt; version 17.0.0 as of this phase). Its input is plain
JSON: a Yahoo Settings or League object as that client library hands it
back, converted to plain JSON (see fixtures/yahoo/README.md for exactly
how). engine/yahoo_client.py (a different module, built in a different
chunk) is the only place in this repo that is allowed to import that
client library or talk to Yahoo; every parsing rule that turns a Yahoo
shape into this repo's shape lives here instead, so that rule can be
tested from a frozen JSON fixture alone, with no live Yahoo access and no
Yahoo client library install required. This split mirrors two
precedents already in the repo: engine/fixtures.py is a helper module that
exists purely to own a frozen fixture schema, and engine/sources/base.py
is a helper module that exists purely to own shared parsing and envelope
conventions for the modules built on top of it. Neither appears in any
plan document's original file list either; a focused helper module is
already how this codebase separates "how do we read this shape" from "who
calls it."

Every public function here takes its Yahoo payload as an explicit
parameter and never raises on bad input: a payload that is missing keys,
has the wrong type, or is outright garbage (None, a string, an int)
returns the documented empty value for that function instead of raising.
This matters because a Yahoo response is external input this module has
never seen live (Yahoo Fantasy Sports API access for this project is
still pending review), so every parser here is written to degrade to an
empty, honest answer rather than crash the caller.
"""
from __future__ import annotations

import re
from typing import Any

# The exact 15 scoring keys engine/scoring.py reads from
# fixtures/sample_league/league.json's settings.scoring.stats. This set is
# hardcoded here (this module cannot read that fixture file, since it does
# no disk access) and is proven to match the fixture by
# tests/test_yahoo_shapes_settings.py, which loads that file at test time
# and asserts the two key sets are equal.
_VALID_SCORING_KEYS = frozenset(
    {
        "defense_fumble_recoveries",
        "defense_interceptions",
        "defense_sacks",
        "defense_touchdowns",
        "extra_points_made",
        "field_goals_made",
        "fumbles_lost",
        "interceptions",
        "passing_touchdowns",
        "passing_yards",
        "receiving_touchdowns",
        "receiving_yards",
        "receptions",
        "rushing_touchdowns",
        "rushing_yards",
    }
)

# Yahoo stat category display names that snake-case to something other
# than the repo's target scoring key. Snake-cased Yahoo name on the left,
# repo scoring key on the right. A name that already snake-cases to a
# valid target key (passing_yards, interceptions, receptions, and so on)
# needs no entry here.
#
# "Interception" (singular) and "Touchdown" (bare) are Yahoo's defense
# stat category names; "Interceptions" (plural) is Yahoo's quarterback
# stat category name and already snake-cases to the valid "interceptions"
# key, so it needs no alias. This table disambiguates the two on the name
# alone, since this project has no verified stat_id mapping (Yahoo Fantasy
# Sports API access is still pending, so stat_id numbers cannot be checked
# against a live response).
YAHOO_STAT_KEY_ALIASES: dict[str, str] = {
    "reception_yards": "receiving_yards",
    "reception_touchdowns": "receiving_touchdowns",
    "sack": "defense_sacks",
    "interception": "defense_interceptions",
    "fumble_recovery": "defense_fumble_recoveries",
    "touchdown": "defense_touchdowns",
}

# Single-letter flex position codes Yahoo uses inside a combined slot name
# such as "W/R/T", mapped to the real position abbreviation.
YAHOO_FLEX_POSITION_ALIASES: dict[str, str] = {
    "W": "WR",
    "R": "RB",
    "T": "TE",
    "Q": "QB",
    "K": "K",
    "D": "DEF",
}

_NON_ALNUM_RUN = re.compile(r"[^a-z0-9]+")

_TRUTHY_STRINGS = {"1", "true", "yes"}
_FALSY_STRINGS = {"0", "false", "no", ""}


def _yahoo_truthy(value: Any) -> bool:
    """Interpret a Yahoo boolean-ish value (an int, a string, or a real bool).

    Yahoo settings send booleans as the strings "1"/"0" as often as real
    JSON booleans or ints. "1"/"true"/"yes" (any case) are true; "0",
    "false", "no", "", and None are false; any other value falls back to
    Python truthiness.
    """
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        text = value.strip().lower()
        if text in _TRUTHY_STRINGS:
            return True
        if text in _FALSY_STRINGS:
            return False
        return bool(text)
    return bool(value)


def _coerce_int(value: Any) -> int | None:
    """Coerce a Yahoo numeric field (often a string) to int, or None on failure."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return int(text)
        except ValueError:
            try:
                return int(float(text))
            except ValueError:
                return None
    return None


def unwrap_yahoo_list(value: Any, key: str) -> list[dict[str, Any]]:
    """Normalize a Yahoo list-valued attribute into a plain list of dicts.

    The Yahoo client library's serialization keeps a single-key wrapper
    dict around every list element (for example a roster_positions entry
    serializes as {"roster_position": {...}}), but a real Yahoo response,
    or a future client library change, may hand back the unwrapped inner
    dicts directly instead.
    This function accepts all four shapes a caller might see: a list of
    {key: {...}} wrapper dicts, a list of bare dicts, a single {key: {...}}
    dict, or a single bare dict.

    Any element that is not a dict is skipped. A wrapper dict whose value
    at key is not itself a dict is also skipped entirely (not kept as a
    bare dict), since its inner shape cannot be trusted. Anything unusable
    as a whole (None, a string, an int) returns an empty list. Never
    raises.
    """
    if isinstance(value, list):
        raw_elements: list[Any] = value
    elif isinstance(value, dict):
        raw_elements = [value]
    else:
        return []

    result: list[dict[str, Any]] = []
    for element in raw_elements:
        if not isinstance(element, dict):
            continue
        if key in element:
            inner = element[key]
            if isinstance(inner, dict):
                result.append(inner)
            # else: a wrapper whose inner value is not a dict is skipped.
        else:
            result.append(element)
    return result


def yahoo_stat_key(name: str | None) -> str:
    """Turn a Yahoo stat category display name into the repo's scoring key.

    The name is lowercased, every run of non-alphanumeric characters
    becomes a single underscore, and leading/trailing underscores are
    stripped, then YAHOO_STAT_KEY_ALIASES is applied if the snake-cased
    result has an entry there. Returns "" for None or a blank/whitespace
    only string. Pure and never raises.
    """
    if name is None:
        return ""
    text = str(name).strip()
    if not text:
        return ""
    key = _NON_ALNUM_RUN.sub("_", text.lower()).strip("_")
    return YAHOO_STAT_KEY_ALIASES.get(key, key)


def _unwrap_stat_list(raw: Any) -> list[dict[str, Any]]:
    """Unwrap a Yahoo stat_categories/stat_modifiers payload down to a list of stat dicts.

    Accepts the fixture's nested wrapper form {"stats": [{"stat": {...}}]}
    as well as an already-unwrapped list or single dict, since a real
    Yahoo response, or a future client library change, may not nest the
    same way.
    """
    if isinstance(raw, dict) and "stats" in raw:
        raw = raw["stats"]
    return unwrap_yahoo_list(raw, "stat")


def parse_scoring_settings(settings_payload: Any) -> dict[str, Any]:
    """Join Yahoo stat_categories to stat_modifiers on stat_id into scoring rules.

    Returns {"stats": {<repo scoring key>: <float>}, "unmapped":
    [{"stat_id": <int|str>, "name": <str>}, ...]}.

    The join key is str(stat_id) on both sides, since Yahoo may send
    stat_id as an int or a string depending on the endpoint; the original
    stat_id value (not the stringified join key) is what is recorded in an
    unmapped entry. Rules, applied in this order per category:
      1. A category with no matching modifier at all is skipped entirely,
         neither in "stats" nor in "unmapped", since it carries no points
         value to report as wrong.
      2. A category whose modifier value will not parse as a float is
         reported in "unmapped".
      3. A category whose yahoo_stat_key is not one of the 15 valid target
         scoring keys is reported in "unmapped".
      4. Otherwise the category's points value is recorded in "stats"
         under its mapped key.
    "unmapped" is sorted by str(stat_id) for determinism.

    Reporting unmapped categories instead of silently dropping them is
    deliberate: a silently dropped scoring category would change every
    projection with no error raised anywhere, which is the same failure
    class an earlier QA pass caught in engine/sources/sleeper.py.

    Garbage input (not a dict, or a dict missing stat_categories/
    stat_modifiers) returns {"stats": {}, "unmapped": []} rather than
    raising.
    """
    if not isinstance(settings_payload, dict):
        return {"stats": {}, "unmapped": []}

    categories = _unwrap_stat_list(settings_payload.get("stat_categories"))
    modifiers = _unwrap_stat_list(settings_payload.get("stat_modifiers"))

    modifier_by_id: dict[str, Any] = {}
    for modifier in modifiers:
        if "stat_id" not in modifier:
            continue
        modifier_by_id[str(modifier["stat_id"])] = modifier.get("value")

    stats: dict[str, float] = {}
    unmapped: list[dict[str, Any]] = []

    for category in categories:
        if "stat_id" not in category:
            continue
        stat_id = category["stat_id"]
        name = category.get("name")
        join_key = str(stat_id)

        if join_key not in modifier_by_id:
            continue

        try:
            points = float(modifier_by_id[join_key])
        except (TypeError, ValueError):
            unmapped.append({"stat_id": stat_id, "name": name})
            continue

        key = yahoo_stat_key(name)
        if key not in _VALID_SCORING_KEYS:
            unmapped.append({"stat_id": stat_id, "name": name})
            continue

        stats[key] = points

    unmapped.sort(key=lambda entry: str(entry["stat_id"]))
    return {"stats": stats, "unmapped": unmapped}


def _eligible_positions(slot_name: str) -> list[str]:
    """Derive eligible_positions from a Yahoo roster slot name."""
    if slot_name in ("BN", "IR"):
        # BN and IR are never started into, so fixtures/sample_league/league.json
        # carries an empty eligible_positions list for both, even though "BN"
        # and "IR" contain no "/" and would otherwise just map to [slot_name].
        # This matches that frozen fixture convention exactly.
        return []
    if "/" in slot_name:
        return [YAHOO_FLEX_POSITION_ALIASES.get(part, part) for part in slot_name.split("/")]
    return [slot_name]


def _slot_starting(slot_name: str, entry: dict[str, Any]) -> bool:
    """Decide a roster slot's "starting" flag from its name and raw entry."""
    if slot_name in ("BN", "IR"):
        return False
    if _yahoo_truthy(entry.get("is_bench")):
        return False
    if "is_starting_position" in entry and not _yahoo_truthy(entry["is_starting_position"]):
        return False
    return True


def parse_roster_slots(settings_payload: Any) -> list[dict[str, Any]]:
    """Return settings.roster_positions in the frozen roster_slots shape.

    Returns [{"slot": str, "count": int, "eligible_positions": list[str],
    "starting": bool}, ...] in the order Yahoo returned them, matching the
    shape fixtures/sample_league/league.json uses for
    settings.roster_slots.

    "slot" is Yahoo's position field (for example "QB", "W/R/T", "BN").
    "count" coerces a string like "2" to int 2 and falls back to 0 on
    garbage. "eligible_positions" is derived from the slot name: a slot
    containing "/" splits on "/" and maps each single-letter flex code
    through YAHOO_FLEX_POSITION_ALIASES (so "W/R/T" becomes
    ["WR", "RB", "TE"]); "BN" and "IR" are special-cased to [] (see
    _eligible_positions); any other slot name maps to [slot_name].
    "starting" is False when the slot is "BN" or "IR", or when the entry's
    is_bench is truthy, or when the entry has an is_starting_position key
    that is falsy; True otherwise.

    A roster_positions entry that is not a dict, or has no usable
    "position" name, is skipped. Garbage top level input (not a dict, or
    missing roster_positions) returns []. Never raises.
    """
    if not isinstance(settings_payload, dict):
        return []

    entries = unwrap_yahoo_list(settings_payload.get("roster_positions"), "roster_position")

    result: list[dict[str, Any]] = []
    for entry in entries:
        slot_name = entry.get("position")
        if not isinstance(slot_name, str) or not slot_name:
            continue
        count = _coerce_int(entry.get("count"))
        result.append(
            {
                "slot": slot_name,
                "count": count if count is not None else 0,
                "eligible_positions": _eligible_positions(slot_name),
                "starting": _slot_starting(slot_name, entry),
            }
        )
    return result


def parse_waiver_settings(settings_payload: Any) -> dict[str, Any]:
    """Return the waiver settings this phase can actually read from Yahoo.

    Returns {"type": "faab" | "priority", "faab_budget": int | None,
    "faab_remaining": {}, "priority_order": [], "waiver_rule": str,
    "waiver_time": int | None}.

    "type" is "faab" when uses_faab is truthy (accepting the strings
    "1"/"0" as well as real ints/bools), else "priority". "faab_budget" is
    coerced from a "faab_budget" key when present and parseable, else
    None. "waiver_rule" is the raw waiver_rule string, or "" if missing or
    not a string. "waiver_time" is coerced to int, or None if missing or
    unparseable.

    faab_remaining AND priority_order ARE ALWAYS EMPTY, AND THAT IS
    DELIBERATE, NOT A BUG. They are included only so this dict's key set
    matches the frozen settings.waiver shape engine/fixtures.py documents
    and engine/waivers.py reads. They cannot be filled in from a Yahoo
    league settings response: verified directly against the pinned Yahoo
    client library's own model definitions (models.py, version 17.0.0),
    its Settings model has a uses_faab attribute but no faab_budget
    attribute at all, and both the per-team FAAB balance (Team.faab_balance)
    and the waiver priority order live on Yahoo TEAM data, which this
    phase does not fetch. As a direct consequence, calling
    engine.waivers.rank_waiver_targets against a league dict built purely
    from this function will raise EngineError, since that function looks
    up a team_id inside settings.waiver.faab_remaining or
    settings.waiver.priority_order and both are empty here. A Phase 4
    caller must fetch Yahoo team data separately and fill in those two
    fields itself before a league dict built from this module is usable
    by engine.waivers.

    Garbage input (not a dict) returns {"type": "priority", "faab_budget":
    None, "faab_remaining": {}, "priority_order": [], "waiver_rule": "",
    "waiver_time": None}. Never raises.
    """
    if not isinstance(settings_payload, dict):
        return {
            "type": "priority",
            "faab_budget": None,
            "faab_remaining": {},
            "priority_order": [],
            "waiver_rule": "",
            "waiver_time": None,
        }

    waiver_type = "faab" if _yahoo_truthy(settings_payload.get("uses_faab")) else "priority"
    faab_budget = _coerce_int(settings_payload.get("faab_budget"))
    waiver_rule = settings_payload.get("waiver_rule")
    if not isinstance(waiver_rule, str):
        waiver_rule = ""
    waiver_time = _coerce_int(settings_payload.get("waiver_time"))

    return {
        "type": waiver_type,
        "faab_budget": faab_budget,
        "faab_remaining": {},
        "priority_order": [],
        "waiver_rule": waiver_rule,
        "waiver_time": waiver_time,
    }


def parse_league_settings(settings_payload: Any) -> dict[str, Any]:
    """Combine scoring, roster slot and waiver parsing into one settings dict.

    Returns {"scoring": {"stats": {...}, "brackets": {}}, "roster_slots":
    [...], "waiver": {...}, "unmapped_stat_categories": [...]}. "scoring"
    is deliberately the same shape engine/scoring.py already reads from
    fixtures/sample_league/league.json: a "stats" map of scoring key to
    points per unit, plus a "brackets" map.

    "brackets" IS ALWAYS AN EMPTY DICT HERE. Yahoo expresses defense
    points-allowed scoring differently from the fixture's
    settings.scoring.brackets.defense_points_allowed range list, and
    mapping that shape into the fixture's [low, high, points] triples
    needs a real Yahoo response to design against, which this phase does
    not have. This does NOT make engine/scoring.py raise: score_stat_line
    reads scoring_rules.get("brackets", {}) and silently skips any stat
    key present in neither the stats map nor the brackets map. The
    concrete consequence is that an empty brackets dict here silently
    drops the points-allowed component of every DEF/ST projection scored
    against a league dict built from this function. A caller that needs
    defense points-allowed scoring must supply its own brackets mapping
    before scoring is correct for DEF/ST players.

    "unmapped_stat_categories" is parse_scoring_settings's "unmapped" list,
    carried up to the top level so a caller can see it without digging
    into a nested "scoring" key that no longer holds it.

    Garbage input (not a dict) returns {"scoring": {"stats": {},
    "brackets": {}}, "roster_slots": [], "waiver": <parse_waiver_settings's
    garbage return>, "unmapped_stat_categories": []}. Never raises.
    """
    scoring_result = parse_scoring_settings(settings_payload)
    return {
        "scoring": {"stats": scoring_result["stats"], "brackets": {}},
        "roster_slots": parse_roster_slots(settings_payload),
        "waiver": parse_waiver_settings(settings_payload),
        "unmapped_stat_categories": scoring_result["unmapped"],
    }


def parse_league_metadata(league_payload: Any) -> dict[str, Any]:
    """Return a Yahoo League object's scalar metadata fields.

    Returns {"league_id": str, "league_key": str, "name": str, "season":
    int | None, "current_week": int | None, "num_teams": int | None,
    "start_week": int | None, "end_week": int | None, "scoring_type": str,
    "url": str}.

    String fields fall back to "" when missing or not a string. Numeric
    fields are coerced from Yahoo's strings (or ints) to int; a value that
    will not parse becomes None. Garbage input (not a dict) returns the
    same key set with every string field "" and every numeric field None.
    Never raises.
    """
    empty: dict[str, Any] = {
        "league_id": "",
        "league_key": "",
        "name": "",
        "season": None,
        "current_week": None,
        "num_teams": None,
        "start_week": None,
        "end_week": None,
        "scoring_type": "",
        "url": "",
    }
    if not isinstance(league_payload, dict):
        return dict(empty)

    def _str_field(key: str) -> str:
        value = league_payload.get(key)
        return value if isinstance(value, str) else ""

    return {
        "league_id": _str_field("league_id"),
        "league_key": _str_field("league_key"),
        "name": _str_field("name"),
        "season": _coerce_int(league_payload.get("season")),
        "current_week": _coerce_int(league_payload.get("current_week")),
        "num_teams": _coerce_int(league_payload.get("num_teams")),
        "start_week": _coerce_int(league_payload.get("start_week")),
        "end_week": _coerce_int(league_payload.get("end_week")),
        "scoring_type": _str_field("scoring_type"),
        "url": _str_field("url"),
    }
