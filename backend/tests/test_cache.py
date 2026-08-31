"""The 7-day per-URL cache.

Two properties matter beyond "it stores things". The key is a hash of the URL
and nothing else — no install ID, no user — which is what makes the cache
privacy-safe under brief rule 6 and shareable between readers. And the URL must
be hashed in exactly one spelling: ``CheckRequest.url`` is a ``pydantic.AnyUrl``
that normalises, so a caller who hashes the raw string in one place and
``str(request.url)`` in another would write to one key and read from another.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from fakeredis.aioredis import FakeRedis

from app.cache import CACHE_TTL_SECONDS, cache_key, get_check, set_check
from app.schema_models import CheckRequest

URL = "https://news.yahoo.com/hawker-stall-rents-rise-vendors"

RESULT: dict[str, Any] = {
    "claims": [
        {
            "id": "c1",
            "quote": "rents will rise by 40 per cent",
            "start": 60,
            "end": 90,
            "verdict": "contradicted",
            "confidence": "high",
            "evidence": "An official release puts the median adjustment at 4%, not 40%.",
            "sources": [
                {
                    "url": "https://www.channelnewsasia.com/singapore/hawker-rents",
                    "outlet": "CNA",
                    "date": "2026-03-12",
                    "wire": False,
                    "stance": "refutes",
                }
            ],
            "trail": [{"label": "This article", "note": "wire copy · republished on Yahoo"}],
        }
    ],
    "counts": {"supported": 0, "contradicted": 1, "missing_context": 0, "unverifiable": 0},
    "checked_at": "2026-08-31T04:15:09Z",
}
"""A realistic cached payload, non-ASCII included (the trail note has a middot)."""


def test_cache_key_is_check_plus_the_sha256_of_the_url() -> None:
    """The documented key: ``check:{sha256(url)}``."""
    assert cache_key(URL) == "check:" + hashlib.sha256(URL.encode()).hexdigest()
    assert cache_key(URL).startswith("check:")
    assert len(cache_key(URL)) == len("check:") + 64


def test_cache_key_is_deterministic_and_url_specific() -> None:
    """Same URL, same key; a different URL, a different key.

    The key carries no user identity — two readers checking the same article
    share one entry, which is the point.
    """
    assert cache_key(URL) == cache_key(URL)
    assert cache_key(URL) != cache_key(URL + "?utm_source=x")


def test_cache_key_agrees_with_a_parsed_check_request() -> None:
    """``cache_key(str(request.url))`` matches the key for the raw URL string.

    ``AnyUrl`` normalisation is the trap here: it appends a trailing slash to an
    origin-only URL. Hashing ``str(request.url)`` everywhere makes reads and
    writes agree whatever the URL looks like.
    """
    request = CheckRequest.model_validate(
        {"url": URL, "title": "t", "text": "x", "install_id": "i"}
    )
    assert cache_key(str(request.url)) == cache_key(URL)

    origin_only = CheckRequest.model_validate(
        {"url": "https://example.com", "title": "t", "text": "x", "install_id": "i"}
    )
    assert str(origin_only.url) == "https://example.com/"
    assert cache_key(str(origin_only.url)) == cache_key("https://example.com/")


async def test_a_miss_returns_none(fake_redis: FakeRedis) -> None:
    """Nothing cached means None, not an exception and not an empty dict."""
    assert await get_check(fake_redis, URL) is None


async def test_set_then_get_round_trips(fake_redis: FakeRedis) -> None:
    """The stored structure comes back equal, nesting and non-ASCII intact."""
    await set_check(fake_redis, URL, RESULT)
    loaded = await get_check(fake_redis, URL)

    assert loaded == RESULT
    assert loaded is not None
    assert loaded["claims"][0]["sources"][0]["stance"] == "refutes"
    assert "·" in loaded["claims"][0]["trail"][0]["note"]


async def test_the_entry_is_written_under_the_hashed_key(fake_redis: FakeRedis) -> None:
    """The value really does live at ``check:{sha256(url)}``, as JSON."""
    await set_check(fake_redis, URL, RESULT)

    raw = await fake_redis.get(cache_key(URL))
    assert raw is not None
    assert json.loads(raw) == RESULT
    assert await fake_redis.keys("check:*") == [cache_key(URL)]


async def test_the_entry_expires_after_seven_days(fake_redis: FakeRedis) -> None:
    """A TTL is set, and it is the seven days the brief asks for."""
    assert CACHE_TTL_SECONDS == 7 * 24 * 3600

    await set_check(fake_redis, URL, RESULT)
    ttl = await fake_redis.ttl(cache_key(URL))

    assert 0 < ttl <= CACHE_TTL_SECONDS
    assert CACHE_TTL_SECONDS - ttl < 5, "the entry should get the full seven days"


async def test_two_urls_do_not_collide(fake_redis: FakeRedis) -> None:
    """Caching one article never shadows another."""
    other = {"claims": [], "counts": {}, "checked_at": "2026-08-30T00:00:00Z"}

    await set_check(fake_redis, URL, RESULT)
    await set_check(fake_redis, "https://www.channelnewsasia.com/singapore/other", other)

    assert await get_check(fake_redis, URL) == RESULT
    assert await get_check(fake_redis, "https://www.channelnewsasia.com/singapore/other") == other


async def test_overwriting_replaces_the_entry(fake_redis: FakeRedis) -> None:
    """A second check of the same URL replaces the stored result outright."""
    await set_check(fake_redis, URL, RESULT)
    replacement = {**RESULT, "checked_at": "2026-09-01T00:00:00Z"}
    await set_check(fake_redis, URL, replacement)

    assert await get_check(fake_redis, URL) == replacement
