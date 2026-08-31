"""``POST /check`` and ``GET /check/{job_id}/stream``.

The POST is deliberately cheap: it looks the article up in the 7-day URL cache,
charges the per-install daily cap only when that misses, starts a background
job, and returns a ``job_id`` straight away. Everything the reader sees arrives
over the SSE stream that the GET relays from Redis. A cached entry whose claims
break a product invariant is deleted and treated as a miss rather than replayed
(:func:`usable_cache_entry`), so a poisoned entry costs one re-check instead of
seven days of errors.

Wire format for the stream — one blank line terminates each message::

    id: 1
    event: claims_found
    data: {"type":"claims_found","count":6,"claim_ids":["c1","c2","c3","c4","c5","c6"]}

``id`` is the job's monotonic sequence number, so a client that reconnects can
drop what it has already applied. A bare ``: keep-alive`` comment goes out after
20 seconds of silence, which keeps the MV3 service worker's ``fetch`` from being
idle-killed mid-check. The stream ends after ``done`` or ``error``.

Two things bound a stream, because an SSE response holds a pub/sub connection
open for as long as it runs and nothing else would ever close it:

* an unknown ``job_id`` is a 404, not a subscription. ``POST /check`` writes a
  marker key for every job it hands out, so a stream opened on an id nobody
  started — a stale id from before a restart, or a guess — is refused instead of
  keep-alived forever. The stream endpoint is not covered by the daily cap, so
  without this it is an unauthenticated way to pin one Redis connection per
  request.
* every stream has an overall deadline (:func:`stream_deadline_seconds`). A job
  whose worker died mid-run publishes nothing further and would otherwise leave
  its reader waiting for a ``done`` that is never coming; past the deadline the
  relay emits ``error`` with code ``timeout`` and closes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncGenerator, AsyncIterator, Coroutine
from contextlib import suppress
from typing import Annotated, Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from redis.asyncio import Redis

from app.cache import delete_check, get_check
from app.config import Settings, get_settings
from app.events import job_exists, mark_job_started, replay_and_stream
from app.invariants import ClaimInvariantError, validate_claims
from app.limits import DailyCapExceeded, check_daily_cap
from app.pipeline.mock import replay_cached, run_mock_pipeline
from app.schema_models import CheckJob, CheckRequest

logger = logging.getLogger(__name__)

router = APIRouter(tags=["check"])

KEEPALIVE_SECONDS = 20.0
"""Seconds of silence on a stream before a keep-alive comment goes out."""

KEEPALIVE_COMMENT = ": keep-alive\n\n"

STREAM_DEADLINE_FACTOR = 20.0
"""How many times the expected job duration a stream is allowed to run.

Deliberately generous — the deadline is a backstop against a worker that will
never finish, not a service-level target, and cutting a slow-but-live check off
would be a far worse bug than holding one connection a little longer.
"""

MIN_STREAM_DEADLINE_SECONDS = 120.0
"""Floor for the deadline, so a configuration with no per-claim delay (the test
settings, or a future pipeline whose pacing is not in ``MOCK_STEP_DELAY``) still
gets a usable budget rather than a zero-length one."""

STREAM_TIMEOUT_MESSAGE = (
    "This check stopped responding before it finished. Please try checking the article again."
)
"""Reader-facing sentence for the ``timeout`` error event."""

UNKNOWN_JOB_MESSAGE = "That check has finished or expired. Please check the article again."
"""Reader-facing sentence for a stream opened on an id we never handed out."""

SSE_HEADERS = {
    "Cache-Control": "no-cache",
    # `Connection` is hop-by-hop and belongs to the ASGI server, not to us: an
    # application that sets it is speaking for a connection it does not own.
    # Tell nginx and friends not to buffer, though: buffering would hold every
    # claim back until the job finished, which is precisely what this endpoint
    # exists to avoid.
    "X-Accel-Buffering": "no",
}

_background_tasks: set[asyncio.Task[None]] = set()
"""Strong references to in-flight pipeline tasks.

``asyncio`` only keeps a weak reference to a running task, so a task that
nothing else holds can be garbage-collected mid-run. Every spawned job stays in
this set until it finishes.
"""


def get_redis(request: Request) -> Redis:
    """FastAPI dependency: the shared Redis client opened by the app lifespan.

    Defined here rather than in :mod:`app.main` because ``app.main`` imports this
    module (and builds the application) at import time; the dependency would
    otherwise be a circular import. ``app.main`` re-exports it, so
    ``from app.main import get_redis`` — including
    ``app.dependency_overrides[get_redis]`` in tests — works as documented.
    """
    client: Redis = request.app.state.redis
    return client


RedisDep = Annotated[Redis, Depends(get_redis)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


@router.post("/check", summary="Start a check for one article")
async def start_check(payload: CheckRequest, redis: RedisDep, settings: SettingsDep) -> CheckJob:
    """Start (or replay) a check and return the job to stream from.

    A cache hit replays the stored claims immediately and reports how many are
    coming; a miss runs the pipeline and leaves ``claim_count`` null until the
    stream's ``claims_found`` event says otherwise. Exceeding the daily cap is a
    429 whose ``detail`` is ``{"code": "daily_limit", "message": ...}``.

    The cache is consulted *before* the cap is charged. The cap exists to bound
    LLM spend (``docs/decisions.md`` §10) and a replay from the 7-day URL cache
    costs nothing, so making a reader pay one of twenty daily checks for it would
    ration the wrong thing. A miss is still charged before any work is spawned,
    so there is no way to spend the pipeline without spending an allowance.

    The hit is only taken once :func:`usable_cache_entry` has checked the stored
    claims against the product's invariants, because committing to an unusable
    entry is a trap with a seven-day fuse — see that function.
    """
    job_id = str(uuid4())
    # AnyUrl normalises (it can append a trailing slash), so hash the string
    # form consistently — cache reads and writes must agree on one spelling.
    cached = await usable_cache_entry(redis, str(payload.url))

    if cached is not None:
        claims = cached.get("claims") or []
        await mark_job_started(redis, job_id)
        _spawn(replay_cached(redis, job_id, cached))
        return CheckJob(job_id=job_id, cached=True, claim_count=len(claims))

    try:
        await check_daily_cap(redis, payload.install_id, settings.daily_cap)
    except DailyCapExceeded as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"code": "daily_limit", "message": daily_limit_message(settings.daily_cap)},
        ) from exc

    await mark_job_started(redis, job_id)
    _spawn(run_mock_pipeline(redis, job_id, payload, settings=settings))
    return CheckJob(job_id=job_id, cached=False, claim_count=None)


async def usable_cache_entry(redis: Redis, url: str) -> dict[str, Any] | None:
    """Return the cached result for ``url``, or None if there is nothing usable.

    A cache hit is a commitment: :func:`start_check` returns on it without
    charging the cap or spawning the pipeline, so whatever the entry holds is
    what every reader of that URL gets for the rest of the seven-day TTL. That
    makes an unusable entry far worse than a miss. If a stored claim breaks one
    of the two product invariants (:mod:`app.invariants`) — an entry written by
    an older build, or corrupted in place — then :func:`replay_cached` raises
    part-way through, the reader sees ``error``, and because nothing re-runs the
    article the next reader sees exactly the same error, and the next, until the
    key expires. Nothing short of manual surgery on Redis could clear it.

    So the claims are validated *before* the hit is taken. On a breach the entry
    is deleted and None is returned, which drops :func:`start_check` into its
    miss branch: the cap is charged, the pipeline runs, and the fresh result
    overwrites what was there. One reader gets a slow check instead of every
    reader getting a dead end, and the cache heals itself.

    Only the offending claim's id and the rule it broke are logged. Neither is
    article text, and no install id is in scope here, so this cannot join a URL
    to a reader (brief, privacy rule 6).
    """
    cached = await get_check(redis, url)
    if cached is None:
        return None
    try:
        validate_claims(cached.get("claims") or [])
    except ClaimInvariantError as exc:
        logger.warning(
            "dropping a cache entry: claim %s breaks a product rule (%s); re-checking",
            exc.claim_id,
            exc.problem,
        )
        await delete_check(redis, url)
        return None
    return cached


@router.get("/check/{job_id}/stream", summary="Server-sent events for one check job")
async def stream_check(job_id: str, redis: RedisDep, settings: SettingsDep) -> StreamingResponse:
    """Relay a job's events as SSE, from the first one published to ``done``.

    Events already stored replay before live ones, so a client that connects
    late — or reconnects after the service worker restarted — still receives the
    whole job.

    A ``job_id`` this service never handed out (or one whose hour-long window has
    passed) is a 404 with ``detail`` ``{"code": "unknown_job", "message": ...}``.
    Subscribing instead would be an unauthenticated way to hold a Redis pub/sub
    connection open indefinitely, since this route is not covered by the cap.
    """
    if not await job_exists(redis, job_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "unknown_job", "message": UNKNOWN_JOB_MESSAGE},
        )
    return StreamingResponse(
        sse_stream(redis, job_id, stream_deadline_seconds(settings)),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


def stream_deadline_seconds(settings: Settings) -> float:
    """How long one stream may run before it gives up on its job.

    Derived from the configured job shape rather than picked out of the air: a
    job publishes at most ``MAX_CLAIMS`` claims spaced ``MOCK_STEP_DELAY`` apart,
    and :data:`STREAM_DEADLINE_FACTOR` multiplies that expected duration to leave
    room for a slow pipeline, with :data:`MIN_STREAM_DEADLINE_SECONDS` as a
    floor. Milestone 2 replaces the numerator with the real pipeline's budget;
    the shape of the calculation stays.
    """
    expected = settings.max_claims * settings.mock_step_delay
    return max(MIN_STREAM_DEADLINE_SECONDS, STREAM_DEADLINE_FACTOR * expected)


def timeout_record() -> dict[str, Any]:
    """The ``error`` event a stream emits when its deadline passes.

    ``seq`` is None: this event is the relay's own, not one of the job's, so it
    borrows no sequence number a real event might later be given. A message with
    no ``id:`` line leaves the client's last event id untouched, which is exactly
    right — nothing here was added to the job's replay list.
    """
    return {
        "seq": None,
        "event": "error",
        "data": {"type": "error", "code": "timeout", "message": STREAM_TIMEOUT_MESSAGE},
    }


async def sse_stream(redis: Redis, job_id: str, deadline_seconds: float) -> AsyncIterator[str]:
    """Yield SSE text for ``job_id``, with a keep-alive comment during silences.

    Runs for at most ``deadline_seconds``. A job whose worker vanished — the
    process restarted, the task was cancelled — publishes no ``done``, and
    without the deadline this loop would emit keep-alives at a disconnected
    client forever; past it the stream emits ``error``/``timeout`` and closes.
    """
    source = replay_and_stream(redis, job_id)
    iterator = aiter(source)
    pending: asyncio.Task[dict[str, Any] | None] | None = None
    expires_at = time.monotonic() + deadline_seconds
    try:
        while True:
            if pending is None:
                # `anext(..., None)` reports exhaustion as a value, so the
                # generator never has to re-raise StopAsyncIteration.
                pending = asyncio.ensure_future(anext(iterator, None))
            remaining = expires_at - time.monotonic()
            if remaining <= 0:
                logger.info("job %s: stream deadline reached, closing with a timeout", job_id)
                yield format_sse(timeout_record())
                break
            try:
                # The shield matters: on timeout `wait_for` cancels what it was
                # given, and cancelling an in-flight `__anext__` would close the
                # source generator — every keep-alive would silently end the
                # stream. Shielded, the same pull survives any number of them.
                record = await asyncio.wait_for(
                    asyncio.shield(pending), min(KEEPALIVE_SECONDS, remaining)
                )
            except TimeoutError:
                # Either the keep-alive interval elapsed or the deadline did.
                # Looping without yielding lets the top of the loop decide, so
                # the timeout event is emitted from exactly one place.
                if time.monotonic() < expires_at:
                    yield KEEPALIVE_COMMENT
                continue
            pending = None
            if record is None:
                break
            yield format_sse(record)
    except asyncio.CancelledError:
        logger.debug("SSE client disconnected from job %s", job_id)
        raise
    finally:
        # Whether the job finished or the reader closed the popup, drop the
        # pull and close the source so its pub/sub subscription is released.
        if pending is not None:
            pending.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await pending
        if isinstance(source, AsyncGenerator):
            with suppress(asyncio.CancelledError, Exception):
                await source.aclose()


def format_sse(record: dict[str, Any]) -> str:
    """Format one ``{"seq", "event", "data"}`` record as an SSE message.

    ``data`` is compact JSON on a single line — :func:`json.dumps` escapes any
    newline inside the payload, so one record is always one ``data:`` line.

    A ``seq`` of None (only the relay's own timeout event) omits the ``id:``
    line: per the SSE spec the client keeps whatever last event id it had, which
    is what we want for a message that was never in the job's replay list.
    """
    data = json.dumps(record["data"], ensure_ascii=False, separators=(",", ":"))
    id_line = "" if record.get("seq") is None else f"id: {record['seq']}\n"
    return f"{id_line}event: {record['event']}\ndata: {data}\n\n"


def daily_limit_message(cap: int) -> str:
    """The reader-facing sentence for a 429. Names the cap and when it resets."""
    return (
        f"You have used all {cap} of today's checks. "
        "The count resets at midnight Singapore time — please try again then."
    )


def _spawn(coro: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
    """Run ``coro`` in the background, holding a reference until it completes."""
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task
