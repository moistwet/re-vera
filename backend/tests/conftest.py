"""Shared fixtures for the backend test suite.

Nothing in here touches the network or needs a live Redis. Every test runs
against a :class:`fakeredis.aioredis.FakeRedis` instance injected in place of
the client :func:`app.main.lifespan` would normally open, and the FastAPI app is
driven in-process through ``httpx.ASGITransport`` — no socket, no server.

Two things are deliberately pinned rather than inherited from the environment:

* :class:`~app.config.Settings` is constructed with ``_env_file=None`` and every
  field this suite cares about passed explicitly, so a developer's local
  ``backend/.env`` (or a stray ``DAILY_CAP`` in the shell) can never change a
  test outcome. ``get_settings`` is overridden as a FastAPI dependency, which
  also sidesteps its ``lru_cache``.
* The mock pipeline's two lead-in sleeps are patched to zero by
  :func:`fast_pipeline`, so the end-to-end streaming tests finish in
  milliseconds instead of the demo's ~7 seconds. The gap *between* claim events
  stays configurable through ``Settings.mock_step_delay``, because one test
  needs a slow stream to prove the keep-alive works.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from fakeredis.aioredis import FakeRedis
from fastapi import FastAPI

import app.pipeline.mock as mock_pipeline
from app.config import Settings, get_settings
from app.main import create_app
from app.routes.check import get_redis

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "article.json"
"""The fictional hawker-rents article the whole milestone-1 skeleton runs on."""

TEST_DAILY_CAP = 3
"""Daily cap used by the app fixtures.

Small on purpose: the end-to-end tests exhaust it in a handful of requests
instead of twenty. :mod:`tests.test_limits` exercises the real default of 20
against :func:`app.limits.check_daily_cap` directly.
"""

TEST_MAX_CLAIMS = 8
"""``MAX_CLAIMS`` for the app fixtures — the production default, and comfortably
above the fixture's six claims, so nothing is truncated."""

BASE_URL = "http://testserver"
"""Base URL for the in-process client. Never resolved; ASGITransport short-circuits."""


@pytest.fixture
def fixture_article() -> dict[str, Any]:
    """The raw fixture document: ``{url, title, text, claims}``.

    Read as UTF-8 explicitly — the article deliberately contains an em dash,
    curly quotes and a middot.
    """
    with FIXTURE_PATH.open(encoding="utf-8") as handle:
        payload: dict[str, Any] = json.load(handle)
    return payload


@pytest.fixture
def fixture_claims(fixture_article: dict[str, Any]) -> list[dict[str, Any]]:
    """The fixture's six claims, in article order (c1 … c6)."""
    claims: list[dict[str, Any]] = fixture_article["claims"]
    return claims


@pytest.fixture
def check_request_body(fixture_article: dict[str, Any]) -> dict[str, str]:
    """A ``CheckRequest`` JSON body for the fixture article."""
    return {
        "url": fixture_article["url"],
        "title": fixture_article["title"],
        "text": fixture_article["text"],
        "install_id": "11111111-2222-3333-4444-555555555555",
    }


@pytest.fixture
async def fake_redis() -> AsyncIterator[FakeRedis]:
    """A fresh in-memory Redis per test.

    ``decode_responses=True`` mirrors what :func:`app.main.lifespan` opens, so
    every value that comes back is ``str`` — the cache, the daily-cap counters
    and the job event records are all JSON text.
    """
    client = FakeRedis(decode_responses=True)
    try:
        yield client
    finally:
        await client.aclose()


def build_settings(**overrides: Any) -> Settings:
    """Build :class:`Settings` that ignore ``backend/.env`` entirely.

    Constructor arguments win over both the environment and ``.env`` in
    pydantic-settings, and ``_env_file=None`` stops the file being read at all —
    so a developer's local settings can never decide whether a test passes.
    ``_env_file`` is a pydantic-settings runtime keyword that its type stubs do
    not declare, hence the one narrow ignore.
    """
    return Settings(_env_file=None, **overrides)  # type: ignore[call-arg]


@pytest.fixture
def settings() -> Settings:
    """Deterministic settings for the app fixtures."""
    return build_settings(
        daily_cap=TEST_DAILY_CAP,
        max_claims=TEST_MAX_CLAIMS,
        mock_step_delay=0.0,
    )


@pytest.fixture
def fast_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Collapse the mock pipeline's two lead-in sleeps to nothing.

    ``run_mock_pipeline`` waits 1.4 s before ``claims_found`` and another 0.7 s
    before the first claim, which is the demo's pacing, not something worth
    paying for on every test run.
    """
    monkeypatch.setattr(mock_pipeline, "CLAIMS_FOUND_DELAY_SECONDS", 0.0)
    monkeypatch.setattr(mock_pipeline, "FIRST_CLAIM_DELAY_SECONDS", 0.0)


@pytest.fixture
def make_app(
    fake_redis: FakeRedis,
    fast_pipeline: None,
) -> Callable[[Settings], FastAPI]:
    """Return a factory building an app wired to this test's fake Redis.

    Both injection points are used: ``app.state.redis`` is set directly (the
    lifespan never runs under ASGITransport) *and* the ``get_redis`` dependency
    is overridden, so the app behaves the same whichever route a future handler
    reaches for.
    """

    def _make(settings: Settings) -> FastAPI:
        application = create_app()
        application.state.redis = fake_redis
        application.dependency_overrides[get_redis] = lambda: fake_redis
        application.dependency_overrides[get_settings] = lambda: settings
        return application

    return _make


@pytest.fixture
def app(make_app: Callable[[Settings], FastAPI], settings: Settings) -> FastAPI:
    """The application under test, with fakeredis and the pinned settings."""
    return make_app(settings)


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """An HTTP client that speaks to the app in-process."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=BASE_URL,
    ) as http_client:
        yield http_client
