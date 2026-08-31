"""Milestone-1 mock pipeline — zero LLM calls, six fictional fixture claims.

This module stands in for the real five-stage pipeline (``extract`` →
``retrieve`` → ``stance`` → ``judge`` → ``aggregate``, milestone 2). It reads
``backend/tests/fixtures/article.json`` and publishes the six claims it holds
onto the job's event stream with the pacing of the design prototype:

    t = 0.00s   job starts
    t = 1.40s   ``claims_found`` (count = 6)
    t = 2.10s   first ``claim``          (fixture order: c3)
    t = 2.95s   …then one every ``settings.mock_step_delay``
    t = 6.35s   sixth ``claim``          (fixture order: c5)
    t = 6.35s   result written to the 7-day URL cache, then ``done``

The claims resolve out of article order (:data:`RESOLVE_ORDER` — rows 3, 1, 6,
4, 2, 5) exactly as the prototype does, so the popup's row-by-row fill is
exercised for real. Every claim carries its ``start``/``end`` offsets into the
request text, so a client places rows by offset rather than by arrival.

The article and its claims are fictional. They are demo and test material and
must never be presented as real reporting.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from redis.asyncio import Redis

from app.cache import set_check
from app.config import Settings, get_settings
from app.events import publish_event
from app.schema_models import CheckRequest, Claim, Verdict

logger = logging.getLogger(__name__)

FIXTURE_PATH = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "article.json"
"""The fictional hawker-rents article: ``{url, title, text, claims}``."""

RESOLVE_ORDER = [2, 0, 5, 3, 1, 4]
"""Indices into the fixture's article-order claim list, in the order the demo
resolves them — rows 3, 1, 6, 4, 2, 5."""

CLAIMS_FOUND_DELAY_SECONDS = 1.4
"""Pause before ``claims_found``, standing in for extraction."""

FIRST_CLAIM_DELAY_SECONDS = 0.7
"""Pause between ``claims_found`` and the first ``claim``."""

FAILURE_MESSAGE = "Something went wrong while checking this article. Please try again."
"""Reader-facing text for the ``error`` event published on an unexpected failure."""


def load_fixture_claims(max_claims: int | None = None) -> list[dict[str, Any]]:
    """Return the fixture's claims, validated against the shared schema.

    Each claim round-trips through :class:`~app.schema_models.Claim`, so a
    fixture that drifts from ``shared/schema.json`` fails loudly here instead of
    reaching a client, and the dicts returned are canonical JSON (no enum
    objects, no stray keys).

    ``max_claims`` caps the list, honouring the ``MAX_CLAIMS`` cost rule; it
    defaults to ``get_settings().max_claims``. The default is 8 and the fixture
    holds 6, so today the cap never bites — but callers with settings in hand
    (see :func:`run_mock_pipeline`) should pass them rather than rely on the
    process-wide ones.
    """
    if max_claims is None:
        max_claims = get_settings().max_claims
    with FIXTURE_PATH.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    claims: list[dict[str, Any]] = [
        Claim.model_validate(claim).model_dump(mode="json") for claim in payload["claims"]
    ]
    return claims[:max_claims]


def tally(claims: list[dict[str, Any]]) -> dict[str, int]:
    """Count claims per verdict, as the ``Counts`` object the ``done`` event carries.

    Every one of the four verdicts is present, zero included, in schema order.
    An unrecognised verdict raises :class:`ValueError` rather than being dropped
    silently.
    """
    counts = {verdict.value: 0 for verdict in Verdict}
    for claim in claims:
        counts[Verdict(claim["verdict"]).value] += 1
    return counts


async def run_mock_pipeline(
    redis: Redis,
    job_id: str,
    request: CheckRequest,
    *,
    settings: Settings,
) -> None:
    """Stream the fixture claims for ``job_id``, then cache the result and finish.

    Publishes ``claims_found`` → one ``claim`` per fixture claim in
    :data:`RESOLVE_ORDER` → ``done``. The finished result is written to the
    7-day URL cache before ``done``, so a repeat check of the same URL replays
    through :func:`replay_cached` instead.

    Any unexpected failure is published as an ``error`` event: a client waiting
    on the stream must never be left hanging.
    """
    try:
        claims = load_fixture_claims(settings.max_claims)

        await asyncio.sleep(CLAIMS_FOUND_DELAY_SECONDS)
        await publish_event(
            redis,
            job_id,
            "claims_found",
            {"type": "claims_found", "count": len(claims)},
        )
        await asyncio.sleep(FIRST_CLAIM_DELAY_SECONDS)

        for position, index in enumerate(resolve_order(len(claims))):
            if position:
                await asyncio.sleep(settings.mock_step_delay)
            await publish_event(redis, job_id, "claim", claims[index])

        checked_at = _now_iso()
        counts = tally(claims)
        await set_check(
            redis,
            str(request.url),
            {"claims": claims, "counts": counts, "checked_at": checked_at},
        )
        await publish_event(
            redis,
            job_id,
            "done",
            {"type": "done", "counts": counts, "checked_at": checked_at},
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("mock pipeline failed for job %s", job_id)
        await _publish_failure(redis, job_id)


async def replay_cached(redis: Redis, job_id: str, cached: dict[str, Any]) -> None:
    """Publish a cached result onto ``job_id``'s stream immediately, with no delays.

    ``cached`` is a :func:`app.cache.get_check` payload —
    ``{"claims": [...], "counts": {...}, "checked_at": "..."}``. Claims replay in
    the order they were stored (article order), which is why clients place rows
    by ``start`` offset rather than by arrival.
    """
    try:
        claims: list[dict[str, Any]] = list(cached.get("claims") or [])
        await publish_event(
            redis,
            job_id,
            "claims_found",
            {"type": "claims_found", "count": len(claims)},
        )
        for claim in claims:
            await publish_event(redis, job_id, "claim", claim)
        await publish_event(
            redis,
            job_id,
            "done",
            {
                "type": "done",
                "counts": cached.get("counts") or tally(claims),
                "checked_at": cached.get("checked_at") or _now_iso(),
            },
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("cached replay failed for job %s", job_id)
        await _publish_failure(redis, job_id)


def resolve_order(count: int) -> list[int]:
    """Return :data:`RESOLVE_ORDER` adapted to a claim list of length ``count``.

    Indices past the end are dropped and any claim the fixed order misses is
    appended, so truncating to ``MAX_CLAIMS`` (or swapping in a longer fixture)
    can never drop or duplicate a claim event.
    """
    order = [index for index in RESOLVE_ORDER if index < count]
    seen = set(order)
    order.extend(index for index in range(count) if index not in seen)
    return order


def _now_iso() -> str:
    """Current UTC time as an ISO 8601 instant, e.g. ``2026-08-31T04:15:09Z``."""
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


async def _publish_failure(redis: Redis, job_id: str) -> None:
    """Best-effort ``error`` event. If Redis itself is down there is nowhere to
    put it, and the stream's keep-alives are all the client will see."""
    try:
        await publish_event(
            redis,
            job_id,
            "error",
            {"type": "error", "code": "internal", "message": FAILURE_MESSAGE},
        )
    except Exception:
        logger.exception("could not publish the error event for job %s", job_id)
