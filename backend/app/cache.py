"""The 7-day per-URL cache of finished check results.

Results are keyed by a hash of the article URL and never by user — re-checking
an article anyone has already checked costs nothing and reveals nothing about
who checked it first (brief, privacy rule 6). A cached entry holds the whole
finished check::

    {"claims": [<Claim>, ...], "counts": {...}, "checked_at": "2026-08-31T..."}

Callers hand in the Redis client, and hand in the URL as a plain ``str``:
``CheckRequest.url`` is a ``pydantic.AnyUrl``, which normalises (it appends a
trailing slash to origin-only URLs), so always hash ``str(request.url)`` and
never mix the raw and parsed forms or the two will hash differently.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from redis.asyncio import Redis

logger = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 7 * 24 * 3600
"""How long a finished check stays cached: seven days."""


def cache_key(url: str) -> str:
    """Return the Redis key for ``url``: ``check:`` + the sha256 of the URL."""
    return "check:" + hashlib.sha256(url.encode()).hexdigest()


async def get_check(redis: Redis, url: str) -> dict[str, Any] | None:
    """Return the cached result for ``url``, or None on a miss.

    An entry that cannot be read back as a JSON object is treated as a miss, so
    a stale or corrupt value makes the check run again instead of failing it.
    """
    raw = await redis.get(cache_key(url))
    if raw is None:
        return None
    try:
        result = json.loads(raw)
    except (TypeError, UnicodeDecodeError, json.JSONDecodeError):
        logger.warning("discarding a cache entry that is not valid JSON")
        return None
    if not isinstance(result, dict):
        logger.warning("discarding a cache entry that is not a JSON object")
        return None
    return result


async def set_check(redis: Redis, url: str, result: dict[str, Any]) -> None:
    """Cache ``result`` for ``url`` for :data:`CACHE_TTL_SECONDS`."""
    await redis.set(cache_key(url), json.dumps(result), ex=CACHE_TTL_SECONDS)
