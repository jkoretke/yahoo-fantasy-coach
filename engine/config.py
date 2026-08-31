"""Load and validate the league configuration in config/league.yaml.

The schema is fixed so every other module can rely on it: a "league"
mapping (league_id, season, game_id, team_id), a top level "timezone", a
"waiver" mapping (day, time), an "email" mapping (backend, to, from_email,
from_name, curlrc), a "sources" mapping of source name to bool
(sleeper, schedule, injuries, weather), a top level "toss_up_margin_points"
number, and a "claude" mapping (binary, timeout_seconds).

load_league_config resolves which file to read (an explicit path, else
config/league.yaml, else config/league.example.yaml, else no file at all),
loads it through engine.common.load_yaml, and deep merges it over
DEFAULT_LEAGUE_CONFIG so a partial file is legal and every documented key
is always present in the returned dict. The result is validated before it
is returned: an invalid email.backend, a negative toss_up_margin_points, a
non-bool sources value, or an unknown sources key each raise EngineError
naming the offending key.

Public names:
    CONFIG_DIR, LEAGUE_CONFIG_PATH, LEAGUE_EXAMPLE_CONFIG_PATH,
    DEFAULT_LEAGUE_CONFIG, EMAIL_BACKENDS, SOURCE_NAMES,
    load_league_config, source_enabled, toss_up_margin, email_config,
    claude_config.
"""
from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from engine.common import EngineError, REPO_ROOT, load_yaml

CONFIG_DIR = REPO_ROOT / "config"
LEAGUE_CONFIG_PATH = CONFIG_DIR / "league.yaml"
LEAGUE_EXAMPLE_CONFIG_PATH = CONFIG_DIR / "league.example.yaml"

EMAIL_BACKENDS = ("brevo", "smtp")
SOURCE_NAMES = ("sleeper", "schedule", "injuries", "weather")

DEFAULT_LEAGUE_CONFIG: dict[str, Any] = {
    "league": {
        "league_id": "",
        "season": 2026,
        "game_id": None,
        "team_id": "",
    },
    "timezone": "America/Los_Angeles",
    "waiver": {
        "day": "tuesday",
        "time": "23:00",
    },
    "email": {
        "backend": "smtp",
        "to": "",
        "from_email": "you@example.com",
        "from_name": "Fantasy Coach",
        "curlrc": "~/.config/yahoo-fantasy-coach/curlrc",
    },
    "sources": {
        "sleeper": True,
        "schedule": True,
        "injuries": True,
        "weather": True,
    },
    "toss_up_margin_points": 2.0,
    "claude": {
        "binary": "claude",
        "timeout_seconds": 600,
    },
}


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Return a new dict with overlay merged onto base, recursing into nested dicts.

    A key present in both where both values are dicts is merged
    recursively; any other key in overlay replaces the value from base
    outright (including replacing a dict with a non-dict, or vice versa).
    Neither input is mutated.
    """
    result = dict(base)
    for key, value in overlay.items():
        existing = result.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            result[key] = _deep_merge(existing, value)
        else:
            result[key] = value
    return result


def _validate_league_config(config: dict[str, Any]) -> None:
    """Raise EngineError, naming the offending key, if config is invalid.

    Checks email.backend, toss_up_margin_points, and every key under
    sources. Any other key is left unvalidated by this function.
    """
    backend = config["email"]["backend"]
    if backend not in EMAIL_BACKENDS:
        raise EngineError(
            f"email.backend must be one of {EMAIL_BACKENDS}, got {backend!r}"
        )

    margin = config["toss_up_margin_points"]
    if isinstance(margin, bool) or not isinstance(margin, (int, float)):
        raise EngineError(
            f"toss_up_margin_points must be a non-negative number, got {margin!r}"
        )
    if margin < 0:
        raise EngineError(
            f"toss_up_margin_points must be a non-negative number, got {margin!r}"
        )

    for name, value in config["sources"].items():
        if name not in SOURCE_NAMES:
            raise EngineError(f"sources.{name} is not a known source")
        if not isinstance(value, bool):
            raise EngineError(f"sources.{name} must be a bool, got {value!r}")


def load_league_config(path: Path | None = None) -> dict[str, Any]:
    """Load, merge and validate the league configuration.

    The file to read is resolved in this order: the given path, else
    config/league.yaml if it exists, else config/league.example.yaml if it
    exists, else no file is read at all and DEFAULT_LEAGUE_CONFIG is
    validated and returned as is. An explicitly given path that does not
    exist raises EngineError (from engine.common.load_yaml, not swallowed
    here).

    The loaded file, if any, is deep merged over DEFAULT_LEAGUE_CONFIG, so
    a partial file is legal and every documented key is always present in
    the result.
    """
    if path is not None:
        raw = load_yaml(path)
    elif LEAGUE_CONFIG_PATH.exists():
        raw = load_yaml(LEAGUE_CONFIG_PATH)
    elif LEAGUE_EXAMPLE_CONFIG_PATH.exists():
        raw = load_yaml(LEAGUE_EXAMPLE_CONFIG_PATH)
    else:
        raw = {}

    config = _deep_merge(copy.deepcopy(DEFAULT_LEAGUE_CONFIG), raw)
    _validate_league_config(config)
    return config


def source_enabled(config: dict[str, Any], name: str) -> bool:
    """Return whether source name is enabled in config.

    Raises EngineError if name is not one of SOURCE_NAMES.
    """
    if name not in SOURCE_NAMES:
        raise EngineError(f"unknown source: {name}")
    return bool(config["sources"][name])


def toss_up_margin(config: dict[str, Any]) -> float:
    """Return config's toss_up_margin_points as a float."""
    return float(config["toss_up_margin_points"])


def email_config(config: dict[str, Any]) -> dict[str, Any]:
    """Return config's email settings as a dict with exactly the keys
    backend, to, from_email, from_name, curlrc.
    """
    email = config["email"]
    return {
        "backend": email["backend"],
        "to": email["to"],
        "from_email": email["from_email"],
        "from_name": email["from_name"],
        "curlrc": email["curlrc"],
    }


def claude_config(config: dict[str, Any]) -> dict[str, Any]:
    """Return config's claude settings as a dict with exactly the keys
    binary, timeout_seconds.
    """
    claude = config["claude"]
    return {
        "binary": claude["binary"],
        "timeout_seconds": claude["timeout_seconds"],
    }
