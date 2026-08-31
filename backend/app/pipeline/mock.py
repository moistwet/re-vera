"""Milestone-1 mock pipeline — zero LLM calls, six fictional fixture claims.

This module stands in for the real five-stage pipeline (``extract`` →
``retrieve`` → ``stance`` → ``judge`` → ``aggregate``, milestone 2). It reads
``backend/tests/fixtures/article.json`` and publishes the six claims it holds
onto the job's event stream with the pacing of the design prototype:

    t = 0.00s   job starts
    t = 1.40s   ``claims_found`` (count = 6, claim_ids = c1 … c6 in article order)
    t = 2.10s   first ``claim``          (fixture order: c3)
    t = 2.95s   …then one every ``settings.mock_step_delay``
    t = 6.35s   sixth ``claim``          (fixture order: c5)
    t = 6.35s   result written to the 7-day URL cache, then ``done``

The claims resolve out of article order (:data:`RESOLVE_ORDER` — rows 3, 1, 6,
4, 2, 5) exactly as the prototype does, so the popup's row-by-row fill is
exercised for real. ``claims_found`` therefore carries ``claim_ids`` — every id
the job will send, in **article** order (decision 15) — so a client allocates
all six rows up front and writes each one when its own claim lands, whatever
order the claim events arrive in. The live path and the cached replay publish
the same ids in the same order; that identity is the point.

**The submitted article text is ignored, and so are the offsets that follow from
it.** ``run_mock_pipeline`` never reads ``request.text``: it streams the fixture's
claims verbatim, which means every ``start``/``end`` it publishes is a character
offset into ``backend/tests/fixtures/article.json``'s *own* ``text``, not into
the text the client sent. ``shared/schema.json`` documents those fields as
offsets into ``CheckRequest.text`` and milestone 3's anchoring builds on that
contract (``docs/decisions.md`` §12), so the two disagree for as long as the
pipeline is a mock. This is deliberate: faking or recomputing offsets against the
submitted text would make milestone 1 look like it honours a contract it cannot
honour — the verdicts are fixture data and have nothing to do with the submitted
article either. A client must therefore **not** resolve these offsets against the
article it sent until the real pipeline lands in milestone 2; until then treat a
claim's ``quote`` as the only thing tying it to any real text.

Every payload this module publishes is built by constructing the generated
model (``ClaimsFoundEvent``/``DoneEvent``/``ErrorEvent``) and dumping it, so the
wire format cannot drift from ``shared/schema.json`` without a test failing
here. Every claim is put through :func:`app.invariants.validate_claim` on its
way out, so a claim that breaks the confidence or sources rule never reaches a
reader.

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
from app.invariants import validate_claim, validate_claims
from app.schema_models import (
    CheckRequest,
    Claim,
    ClaimsFoundEvent,
    Counts,
    DoneEvent,
    ErrorEvent,
    Verdict,
)

logger = logging.getLogger(__name__)

FIXTURE_PATH = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "article.json"
"""The fictional hawker-rents article: ``{_fictional, url, title, text, claims}``.

``_fictional`` is the standing warning that the article, its claims and every
source in it are invented; only ``claims`` is read here."""

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

    The product's two cross-field rules — confidence null iff ``unverifiable``,
    sources empty iff ``unverifiable`` — are not expressible in the schema, so
    :func:`app.invariants.validate_claims` enforces them here as well; a fixture
    edited into a state the product forbids fails on load rather than on a
    reader's screen.

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
    validate_claims(claims)
    return claims[:max_claims]


def article_order(claims: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return ``claims`` sorted by ``start`` — the article order clients render in.

    The fixture and the cache already store claims this way, so this is a no-op
    in practice; it exists so the ``claim_ids`` contract ("ascending by the
    claim's ``start`` offset", decision 15) is guaranteed by construction rather
    than by two separate files remembering to stay sorted. A claim with a
    non-integer ``start`` sorts first rather than raising: the generated model
    has already rejected that shape everywhere it matters.
    """
    return sorted(claims, key=_start_of)


def claims_found_payload(claims: list[dict[str, Any]]) -> dict[str, Any]:
    """Build the ``claims_found`` payload for ``claims`` (already in article order).

    ``count`` is derived from the id list, never counted separately, so the two
    can never disagree. Built through the generated
    :class:`~app.schema_models.ClaimsFoundEvent` so the wire format tracks
    ``shared/schema.json``.
    """
    claim_ids = [str(claim["id"]) for claim in claims]
    event = ClaimsFoundEvent(type="claims_found", count=len(claim_ids), claim_ids=claim_ids)
    return event.model_dump(mode="json")


def done_payload(counts: dict[str, int], checked_at: str) -> dict[str, Any]:
    """Build the ``done`` payload, via the generated :class:`DoneEvent`.

    ``counts`` goes through :class:`~app.schema_models.Counts`, so a tally
    missing a verdict (or carrying an unknown one) fails here instead of
    reaching a client's counts line.
    """
    event = DoneEvent(type="done", counts=Counts.model_validate(counts), checked_at=checked_at)
    return event.model_dump(mode="json")


def error_payload(code: str, message: str) -> dict[str, Any]:
    """Build an ``error`` payload, via the generated :class:`ErrorEvent`."""
    return ErrorEvent(type="error", code=code, message=message).model_dump(mode="json")


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

    Publishes ``claims_found`` (carrying every claim id in article order) → one
    ``claim`` per fixture claim in :data:`RESOLVE_ORDER` → ``done``. The
    finished result is written to the 7-day URL cache before ``done`` — in
    article order, so :func:`replay_cached` announces exactly the same
    ``claim_ids`` — and a repeat check of the same URL replays through it
    instead.

    Any unexpected failure is published as an ``error`` event: a client waiting
    on the stream must never be left hanging.
    """
    try:
        claims = article_order(load_fixture_claims(settings.max_claims))

        await asyncio.sleep(CLAIMS_FOUND_DELAY_SECONDS)
        await publish_event(redis, job_id, "claims_found", claims_found_payload(claims))
        await asyncio.sleep(FIRST_CLAIM_DELAY_SECONDS)

        for position, index in enumerate(resolve_order(len(claims))):
            if position:
                await asyncio.sleep(settings.mock_step_delay)
            claim = claims[index]
            # Last gate before the wire: a claim that breaks a product rule is a
            # failed job, not something a reader gets to see.
            validate_claim(claim)
            await publish_event(redis, job_id, "claim", claim)

        checked_at = _now_iso()
        counts = tally(claims)
        await set_check(
            redis,
            str(request.url),
            {"claims": claims, "counts": counts, "checked_at": checked_at},
        )
        await publish_event(redis, job_id, "done", done_payload(counts, checked_at))
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("mock pipeline failed for job %s", job_id)
        await _publish_failure(redis, job_id)


async def replay_cached(redis: Redis, job_id: str, cached: dict[str, Any]) -> None:
    """Publish a cached result onto ``job_id``'s stream immediately, with no delays.

    ``cached`` is a :func:`app.cache.get_check` payload —
    ``{"claims": [...], "counts": {...}, "checked_at": "..."}``. Claims replay in
    article order, which is the order they were stored in; the ``claim_ids`` on
    ``claims_found`` are therefore identical to the ones the live path announced
    for the same article, and a client renders a cache hit exactly as it
    rendered the original check.
    """
    try:
        claims: list[dict[str, Any]] = article_order(list(cached.get("claims") or []))
        await publish_event(redis, job_id, "claims_found", claims_found_payload(claims))
        for claim in claims:
            # A cache entry written by an older build could hold a claim the
            # product rules now forbid; it fails the job rather than streaming.
            validate_claim(claim)
            await publish_event(redis, job_id, "claim", claim)
        await publish_event(
            redis,
            job_id,
            "done",
            done_payload(
                cached.get("counts") or tally(claims),
                cached.get("checked_at") or _now_iso(),
            ),
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


def _start_of(claim: dict[str, Any]) -> int:
    """The claim's ``start`` offset, or 0 for anything that is not an integer."""
    start = claim.get("start")
    return start if isinstance(start, int) else 0


def _now_iso() -> str:
    """Current UTC time as an ISO 8601 instant, e.g. ``2026-08-31T04:15:09Z``."""
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


async def _publish_failure(redis: Redis, job_id: str) -> None:
    """Best-effort ``error`` event. If Redis itself is down there is nowhere to
    put it, and the stream's keep-alives are all the client will see."""
    try:
        await publish_event(redis, job_id, "error", error_payload("internal", FAILURE_MESSAGE))
    except Exception:
        logger.exception("could not publish the error event for job %s", job_id)
