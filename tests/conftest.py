"""Shared fixtures for the whole tests/ suite.

engine.sources.* modules make real network calls at runtime by design, but
the test suite must never make one: a flaky or rate limited external API
must never make CI flaky, and a test that silently reaches the real network
is not actually testing the mocked behavior it claims to test. The
block_real_network fixture below is autouse, so it applies to every test in
this directory without any test having to opt in.

A test that legitimately exercises the HTTP path patches
urllib.request.urlopen itself, typically with unittest.mock.patch. Because
that patch is applied after this fixture already ran, it cleanly overrides
the fixture's patch for the duration of that one test, so the guard costs
those tests nothing.
"""
from __future__ import annotations

import urllib.request

import pytest


@pytest.fixture(autouse=True)
def block_real_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make a real urllib.request.urlopen call fail every test, by default."""

    def _refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError(
            "the test suite must never make a real network call; "
            "patch urllib.request.urlopen or engine.sources.base.fetch_json instead"
        )

    monkeypatch.setattr(urllib.request, "urlopen", _refuse)
