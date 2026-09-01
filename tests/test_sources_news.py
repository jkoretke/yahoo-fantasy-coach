"""Tests for engine.sources.news: the Claude-backed late breaking news pass.

Every test passes its own runner, so no test in this file ever spawns a
real claude process; tests/conftest.py's autouse block_real_network fixture
covers the network side. Every test that touches disk passes an explicit
cache_root=tmp_path, so the suite never writes into the repo's own
runs/cache/ directory.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from engine.sources import news

PLAYERS = ["Dax Voss", "Colt Blackwood"]


def _runner(stdout: str, returncode: int = 0, stderr: str = "", seen: list | None = None):
    """Return a run_claude-shaped callable that answers with stdout."""

    def _run(prompt: str, *, claude_bin: str = "claude", timeout: int = 600):
        if seen is not None:
            seen.append({"prompt": prompt, "claude_bin": claude_bin, "timeout": timeout})
        return returncode, stdout, stderr

    return _run


def _answer(*items: dict) -> str:
    return json.dumps({"items": list(items)})


ITEM = {
    "player": "Dax Voss",
    "note": "limited in Wednesday practice with a shoulder injury",
    "source": "espn.com",
}


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------


def test_fetch_news_parses_the_documented_shape(tmp_path: Path) -> None:
    result = news.fetch_news(PLAYERS, cache_root=tmp_path, runner=_runner(_answer(ITEM)))

    assert result["source"] == "news"
    assert result["available"] is True
    assert result["stale"] is False
    assert result["data"]["items"] == [ITEM]
    assert result["data"]["count"] == 1
    assert result["data"]["players"] == ["Colt Blackwood", "Dax Voss"]


def test_fetch_news_empty_items_is_a_real_answer_not_a_failure(tmp_path: Path) -> None:
    # Most weeks, most players have no news. That is an available result
    # with nothing in it, never an unavailable one.
    result = news.fetch_news(PLAYERS, cache_root=tmp_path, runner=_runner(_answer()))

    assert result["available"] is True
    assert result["data"]["items"] == []
    assert result["data"]["count"] == 0


def test_fetch_news_names_every_player_in_the_prompt(tmp_path: Path) -> None:
    seen: list = []
    news.fetch_news(PLAYERS, cache_root=tmp_path, runner=_runner(_answer(), seen=seen))

    prompt = seen[0]["prompt"]
    for name in PLAYERS:
        assert name in prompt
    # The names go in as written, not lowercased: the model is asked about
    # real people and its answer is matched back by name.
    assert "dax voss" not in prompt


def test_fetch_news_passes_the_configured_binary_and_timeout(tmp_path: Path) -> None:
    seen: list = []
    news.fetch_news(
        PLAYERS,
        claude_bin="/opt/claude",
        timeout=42,
        cache_root=tmp_path,
        runner=_runner(_answer(), seen=seen),
    )
    assert seen[0]["claude_bin"] == "/opt/claude"
    assert seen[0]["timeout"] == 42


@pytest.mark.parametrize(
    "wrapped",
    [
        "```json\n{\"items\": []}\n```",
        "```\n{\"items\": []}\n```",
        "  {\"items\": []}  \n",
    ],
)
def test_fetch_news_tolerates_code_fences_and_whitespace(wrapped: str, tmp_path: Path) -> None:
    result = news.fetch_news(PLAYERS, cache_root=tmp_path, runner=_runner(wrapped))
    assert result["available"] is True
    assert result["data"]["items"] == []


# ---------------------------------------------------------------------------
# Item-level validation: one bad item never costs the others
# ---------------------------------------------------------------------------


def test_fetch_news_drops_bad_items_and_keeps_good_ones(tmp_path: Path) -> None:
    raw = json.dumps({
        "items": [
            ITEM,
            "not an object",
            {"note": "no player name"},
            {"player": "", "note": "blank name"},
            {"player": "Colt Blackwood", "note": ""},
            {"player": "Colt Blackwood", "note": "took first team reps"},
        ]
    })
    result = news.fetch_news(PLAYERS, cache_root=tmp_path, runner=_runner(raw))

    assert result["available"] is True
    assert [item["player"] for item in result["data"]["items"]] == ["Dax Voss", "Colt Blackwood"]


def test_fetch_news_source_is_optional(tmp_path: Path) -> None:
    raw = _answer({"player": "Dax Voss", "note": "ruled out"})
    result = news.fetch_news(PLAYERS, cache_root=tmp_path, runner=_runner(raw))
    assert result["data"]["items"][0]["source"] == ""


# ---------------------------------------------------------------------------
# Degradation: a news pass that cannot run never costs the email
# ---------------------------------------------------------------------------


def test_fetch_news_disabled_makes_no_call_at_all(tmp_path: Path) -> None:
    def _never(*args: object, **kwargs: object):
        raise AssertionError("a disabled news pass must not spawn anything")

    result = news.fetch_news(PLAYERS, enabled=False, cache_root=tmp_path, runner=_never)
    assert result["available"] is False
    assert result["reason"] == "disabled"


def test_fetch_news_no_players_answers_without_spawning(tmp_path: Path) -> None:
    def _never(*args: object, **kwargs: object):
        raise AssertionError("no players means nothing to ask about")

    result = news.fetch_news([" ", ""], cache_root=tmp_path, runner=_never)
    assert result["available"] is True
    assert result["data"]["items"] == []


def test_fetch_news_too_many_players_degrades(tmp_path: Path) -> None:
    many = [f"Player Number{index}" for index in range(news.MAX_PLAYERS + 1)]
    result = news.fetch_news(many, cache_root=tmp_path, runner=_runner(_answer()))
    assert result["available"] is False
    assert "too many players" in result["reason"]


@pytest.mark.parametrize(
    "stdout", ["", "I could not find anything.", "{}", '{"items": "nope"}', "[]"]
)
def test_fetch_news_undocumented_output_degrades(stdout: str, tmp_path: Path) -> None:
    result = news.fetch_news(PLAYERS, cache_root=tmp_path, runner=_runner(stdout))
    assert result["available"] is False
    assert result["data"] is None


def test_fetch_news_nonzero_exit_degrades(tmp_path: Path) -> None:
    result = news.fetch_news(
        PLAYERS, cache_root=tmp_path, runner=_runner("", returncode=1, stderr="usage limit")
    )
    assert result["available"] is False
    assert "usage limit" in result["reason"]


def test_fetch_news_missing_claude_binary_degrades_rather_than_raising(tmp_path: Path) -> None:
    # engine.run_common.run_claude does not catch FileNotFoundError, so a
    # box with no claude on PATH raises OSError out of the runner. A
    # missing news section must not cost the weekly email.
    def _missing(*args: object, **kwargs: object):
        raise FileNotFoundError("No such file or directory: 'claude'")

    result = news.fetch_news(PLAYERS, cache_root=tmp_path, runner=_missing)
    assert result["available"] is False
    assert "could not run" in result["reason"]


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


def test_fetch_news_fresh_cache_entry_skips_the_subprocess(tmp_path: Path) -> None:
    first = news.fetch_news(PLAYERS, cache_root=tmp_path, runner=_runner(_answer(ITEM)))
    assert first["available"] is True

    def _never(*args: object, **kwargs: object):
        raise AssertionError("a fresh cache entry must not spawn claude")

    second = news.fetch_news(PLAYERS, cache_root=tmp_path, runner=_never)
    assert second["available"] is True
    assert second["stale"] is False
    assert second["data"]["items"] == [ITEM]


def test_fetch_news_stale_cache_entry_is_served_when_a_refetch_fails(tmp_path: Path) -> None:
    news.fetch_news(PLAYERS, cache_root=tmp_path, runner=_runner(_answer(ITEM)))

    result = news.fetch_news(
        PLAYERS,
        cache_root=tmp_path,
        max_age_seconds=0,
        runner=_runner("", returncode=1, stderr="down"),
    )
    # A day-old injury note is still worth reading, so stale beats nothing
    # here, unlike the current-week lookup where stale silently runs the
    # wrong week.
    assert result["available"] is True
    assert result["stale"] is True
    assert result["data"]["items"] == [ITEM]


def test_fetch_news_a_different_player_set_is_a_different_cache_entry(tmp_path: Path) -> None:
    news.fetch_news(PLAYERS, cache_root=tmp_path, runner=_runner(_answer(ITEM)))

    seen: list = []
    news.fetch_news(
        PLAYERS + ["Reeve Wexford"], cache_root=tmp_path, runner=_runner(_answer(), seen=seen)
    )
    assert seen, "a new player set must ask again rather than reuse another set's answer"


def test_fetch_news_cache_key_ignores_order_and_case() -> None:
    assert news._cache_key(["Dax Voss", "Colt Blackwood"]) == news._cache_key(
        ["colt blackwood", "DAX VOSS"]
    )
