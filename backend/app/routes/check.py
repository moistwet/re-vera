"""``POST /check`` and ``GET /check/{job_id}/stream``.

The POST is deliberately cheap: it enforces the per-install daily cap, looks the
article up in the 7-day URL cache, starts a background job, and returns a
``job_id`` straight away. Everything the reader sees arrives over the SSE stream
that the GET relays from Redis.

Wire format for the stream — one blank line terminates each message::

    id: 1
    event: claims_found
    data: {"type":"claims_found","count":6}

``id`` is the job's monotonic sequence number, so a client that reconnects can
drop what it has already applied. A bare ``: keep-alive`` comment goes out after
20 seconds of silence, which keeps the MV3 service worker's ``fetch`` from being
idle-killed mid-check. The stream ends after ``done`` or ``error``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncGenerator, AsyncIterator, Coroutine
from contextlib import suppress
from typing import Annotated, Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from redis.asyncio import Redis

from app.cache import get_check
from app.config import Settings, get_settings
from app.events import replay_and_stream
from app.limits import DailyCapExceeded, check_daily_cap
from app.pipeline.mock import replay_cached, run_mock_pipeline
from app.schema_models import CheckJob, CheckRequest

logger = logging.getLogger(__name__)

router = APIRouter(tags=["check"])

KEEPALIVE_SECONDS = 20.0
"""Seconds of silence on a stream before a keep-alive comment goes out."""

KEEPALIVE_COMMENT = ": keep-alive\n\n"

SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    # Tell nginx and friends not to buffer: buffering would hold every claim
    # back until the job finished, which is precisely what this endpoint exists
    # to avoid.
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
    """
    try:
        await check_daily_cap(redis, payload.install_id, settings.daily_cap)
    except DailyCapExceeded as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"code": "daily_limit", "message": daily_limit_message(settings.daily_cap)},
        ) from exc

    job_id = str(uuid4())
    # AnyUrl normalises (it can append a trailing slash), so hash the string
    # form consistently — cache reads and writes must agree on one spelling.
    cached = await get_check(redis, str(payload.url))

    if cached is not None:
        claims = cached.get("claims") or []
        _spawn(replay_cached(redis, job_id, cached))
        return CheckJob(job_id=job_id, cached=True, claim_count=len(claims))

    _spawn(run_mock_pipeline(redis, job_id, payload, settings=settings))
    return CheckJob(job_id=job_id, cached=False, claim_count=None)


@router.get("/check/{job_id}/stream", summary="Server-sent events for one check job")
async def stream_check(job_id: str, redis: RedisDep) -> StreamingResponse:
    """Relay a job's events as SSE, from the first one published to ``done``.

    Events already stored replay before live ones, so a client that connects
    late — or reconnects after the service worker restarted — still receives the
    whole job.
    """
    return StreamingResponse(
        sse_stream(redis, job_id),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


async def sse_stream(redis: Redis, job_id: str) -> AsyncIterator[str]:
    """Yield SSE text for ``job_id``, with a keep-alive comment during silences."""
    source = replay_and_stream(redis, job_id)
    iterator = aiter(source)
    pending: asyncio.Task[dict[str, Any] | None] | None = None
    try:
        while True:
            if pending is None:
                # `anext(..., None)` reports exhaustion as a value, so the
                # generator never has to re-raise StopAsyncIteration.
                pending = asyncio.ensure_future(anext(iterator, None))
            try:
                # The shield matters: on timeout `wait_for` cancels what it was
                # given, and cancelling an in-flight `__anext__` would close the
                # source generator — every keep-alive would silently end the
                # stream. Shielded, the same pull survives any number of them.
                record = await asyncio.wait_for(asyncio.shield(pending), KEEPALIVE_SECONDS)
            except TimeoutError:
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
    """
    data = json.dumps(record["data"], ensure_ascii=False, separators=(",", ":"))
    return f"id: {record['seq']}\nevent: {record['event']}\ndata: {data}\n\n"


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
