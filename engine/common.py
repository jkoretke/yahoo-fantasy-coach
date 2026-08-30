"""Shared filesystem, configuration and secrets helpers for the engine package."""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    raise SystemExit(
        "Missing dependency: install with `python3 -m pip install -r requirements.txt`."
    )

REPO_ROOT = Path(__file__).resolve().parents[1]
SECRETS_ENV_VAR = "YAHOO_FANTASY_COACH_SECRETS_FILE"
DEFAULT_SECRETS_PATH = Path.home() / ".config" / "yahoo-fantasy-coach" / "secrets.env"


class EngineError(RuntimeError):
    """An expected operational failure, reported without a traceback."""


def load_yaml(path: Path) -> dict[str, Any]:
    """Load a YAML file and return its top level mapping."""
    if not path.exists():
        raise EngineError(f"{path} does not exist")
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise EngineError(f"{path} must contain a YAML mapping")
    return data


def load_json(path: Path) -> dict[str, Any]:
    """Load a JSON file and return its top level object."""
    if not path.exists():
        raise EngineError(f"{path} does not exist")
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise EngineError(f"{path} must contain a JSON object")
    return data


def atomic_write(path: Path, text: str) -> None:
    """Write text to path atomically, creating parent directories as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=str(path.parent), delete=False
    ) as tmp:
        tmp.write(text)
        tmp_path = tmp.name
    Path(tmp_path).replace(path)


def write_json(path: Path, value: Any) -> None:
    """Serialize value as pretty printed JSON and write it atomically."""
    text = json.dumps(value, indent=2, sort_keys=False) + "\n"
    atomic_write(path, text)


def load_secrets(path: Path | None = None) -> dict[str, str]:
    """Load KEY=VALUE secrets from a file, with environment variables taking priority.

    The file is resolved in this order: the given path, then the file named by
    SECRETS_ENV_VAR, then DEFAULT_SECRETS_PATH. A missing file returns an empty
    mapping rather than raising, since Phase 1 has no required secret keys yet.
    """
    if path is None:
        env_path = os.getenv(SECRETS_ENV_VAR)
        if env_path:
            path = Path(env_path)
        else:
            path = DEFAULT_SECRETS_PATH

    if not path.exists():
        return {}

    values: dict[str, str] = {}
    with path.open("r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, raw_value = line.partition("=")
            key = key.strip()
            value = raw_value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            values[key] = value

    for key in list(values.keys()):
        env_value = os.getenv(key)
        if env_value:
            values[key] = env_value

    return values


def require_secret(values: dict[str, str], key: str) -> str:
    """Return the stripped value for key, or raise EngineError if it is missing or blank."""
    result = values.get(key, "").strip()
    if not result:
        raise EngineError(f"Missing required secret/config value: {key}")
    return result


def timestamp(value: datetime | None = None) -> str:
    """Return the UTC ISO 8601 string for value, or for now if value is omitted.

    A naive datetime (no tzinfo) raises EngineError, since it is ambiguous which
    timezone it was meant to represent.
    """
    if value is None:
        value = datetime.now(timezone.utc)
    else:
        if value.tzinfo is None:
            raise EngineError("timestamp requires a timezone-aware datetime")
        value = value.astimezone(timezone.utc)
    return value.isoformat().replace("+00:00", "Z")


def round_points(value: float) -> float:
    """Round a point total to two decimal places, the single rounding contract for the repo."""
    return round(float(value), 2)
