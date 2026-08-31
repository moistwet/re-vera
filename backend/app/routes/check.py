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
import hashlib
import json
import logging
import time
from collections.abc import AsyncGenerator, AsyncIterator, Coroutine
from contextlib import suppress
from math import ceil
from typing import Annotated, Any, Protocol
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from redis.asyncio import Redis

from app.cache import delete_check, get_check
from app.config import Settings, get_settings
from app.events import job_exists, mark_job_started, replay_and_stream
from app.invariants import ClaimInvariantError, validate_claims
from app.limits import DailyCapExceeded, check_daily_cap
from app.pipeline.mock import MOCK_CACHE_SOURCE, replay_cached, run_mock_pipeline
from app.pipeline.providers import PROVIDER_TIMEOUT_SECONDS
from app.pipeline.run import run_pipeline
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

CACHE_HIT_BURST_LIMIT = 120
"""Most cache-hit replays honoured for one cached URL within
:data:`CACHE_HIT_BURST_WINDOW_SECONDS` (M11).

A cache hit is deliberately uncapped by the *daily* cap — cost-free reads
should not ration a reader's twenty checks (``docs/decisions.md`` §10) — but
"uncapped" is not the same as "unlimited": every hit still allocates a job id,
a started marker and a full published event stream (:mod:`app.events`), so an
unauthenticated script that just replays one popular URL as fast as it can is
free amplification, not a legitimate re-read. This is a coarse circuit
breaker against exactly that, not a fairness mechanism — the loose, NAT-aware
per-IP backstop that *is* a fairness mechanism is milestone 5's job
(``CLAUDE.md``). Twelve replays a second, sustained, is nothing a person
reading one article does; it is comfortably above what a whole school
refreshing a trending article at once would produce, and comfortably below
what a script hammering the endpoint would produce.
"""

CACHE_HIT_BURST_WINDOW_SECONDS = 10
"""The fixed window :data:`CACHE_HIT_BURST_LIMIT` is measured over, per URL."""

CACHE_HIT_RATE_LIMITED_MESSAGE = (
    "This article is being checked a lot right now. Please try again in a few seconds."
)
"""Reader-facing sentence for the ``rate_limited`` 429 (M11)."""

_CACHE_HIT_BURST_KEY = "cachehit:{url_hash}"
"""Redis key for the per-URL cache-hit burst counter. A fixed window (not a
sliding one): simplicity is fine here, this is a coarse circuit breaker, not a
precise fairness meter."""

_INFLIGHT_KEY = "inflight:{url_hash}"
"""Redis key naming the job id currently running the pipeline for one
uncached URL (M13's single-flight lock). Its value is a ``job_id`` a stream
can be opened on, never article text or an install id."""


def _url_hash(url: str) -> str:
    """The identity a burst counter or an in-flight lock keys on: a URL's
    sha256, matching :func:`app.cache.cache_key`'s own hashing so the two
    namespaces are trivially distinct without either leaking the URL itself
    into a Redis key name."""
    return hashlib.sha256(url.encode()).hexdigest()


async def _cache_hit_within_budget(redis: Redis, url: str) -> bool:
    """True unless ``url`` has already been replayed
    :data:`CACHE_HIT_BURST_LIMIT` times within :data:`CACHE_HIT_BURST_WINDOW_SECONDS`.

    One ``INCR`` (creating the key with its window's ``EXPIRE`` on the first
    hit of a fresh window) — cheap, and the only new Redis work a cache hit
    pays for M11.
    """
    key = _CACHE_HIT_BURST_KEY.format(url_hash=_url_hash(url))
    count = await redis.incr(key)
    if count == 1:
        await redis.expire(key, CACHE_HIT_BURST_WINDOW_SECONDS)
    return count <= CACHE_HIT_BURST_LIMIT


async def _claim_inflight_leadership(
    redis: Redis, url: str, job_id: str, ttl_seconds: float
) -> bool:
    """Try to become the one job that runs the pipeline for ``url``.

    ``SET NX`` — atomic: exactly one concurrent caller for the same ``url``
    ever gets True. ``ttl_seconds`` self-heals a leader that dies without
    calling :func:`_release_inflight` (a worker crash, a process restart): once
    it elapses, the next miss claims leadership fresh rather than waiting out a
    lock nothing will ever release.
    """
    key = _INFLIGHT_KEY.format(url_hash=_url_hash(url))
    return bool(await redis.set(key, job_id, nx=True, ex=max(1, ceil(ttl_seconds))))


async def _inflight_leader(redis: Redis, url: str) -> str | None:
    """The ``job_id`` currently leading ``url``'s check, or None if there isn't one."""
    key = _INFLIGHT_KEY.format(url_hash=_url_hash(url))
    value = await redis.get(key)
    return str(value) if value is not None else None


async def _release_inflight(redis: Redis, url: str, job_id: str) -> None:
    """Release the lock, but only if it is still ours.

    A plain ``GET`` then ``DELETE`` rather than one atomic compare-and-delete:
    this lock is a cost-saving circuit breaker, not a correctness-critical
    mutex (nothing downstream trusts "exactly one leader" for safety, only for
    cost — the real safety property, "at most one *cache write* per finished
    run", is already guaranteed by ``app.cache.set_check`` being an idempotent
    overwrite). The tiny race this leaves — releasing a lock a brand-new leader
    claimed in between our read and our delete — costs at most one extra
    pipeline run of the kind this feature exists to reduce, never a correctness
    bug, so it is not worth a Lua script here.
    """
    key = _INFLIGHT_KEY.format(url_hash=_url_hash(url))
    current = await redis.get(key)
    if current == job_id:
        await redis.delete(key)


_LEAD_OR_JOIN_ATTEMPTS = 3
"""Bound on :func:`_lead_or_join`'s claim/read retry.

One retry covers the realistic race (the leader we lost to releases the lock
between our failed claim and our read of it); the second is slack for an
equally unlucky second collision. Bounded rather than looped forever so a
pathological sequence of collisions fails toward "run our own pipeline" —
at worst one redundant run, never a hang — rather than toward retrying
indefinitely inside a request handler.
"""


async def _lead_or_join(redis: Redis, url: str, job_id: str, ttl_seconds: float) -> str:
    """Return the ``job_id`` that will run (or is running) the pipeline for
    ``url``: this call's own ``job_id`` if it wins the single-flight lock, or
    the existing leader's if it does not.

    Guarantees the id it returns is one **a pipeline is actually running
    for** — this function never hands back a follower a leader id that turned
    out to have already vanished, because the caller could not spawn anything
    for that id either. The tiny remaining race (two collisions in a row,
    :data:`_LEAD_OR_JOIN_ATTEMPTS` exhausted) resolves toward running this
    call's own pipeline rather than toward returning a job id nobody is
    driving — one possible redundant run in an outcome this improbable is a
    far smaller cost than a client polling a stream that will only ever time
    out.
    """
    for _ in range(_LEAD_OR_JOIN_ATTEMPTS):
        if await _claim_inflight_leadership(redis, url, job_id, ttl_seconds):
            return job_id
        leader_job_id = await _inflight_leader(redis, url)
        if leader_job_id is not None:
            return leader_job_id
        # Lost the claim, but by the time we looked, whoever won had already
        # released it — nobody is actually leading `url` right now. Loop
        # around and try to claim it ourselves again.
    return job_id


async def _run_leader_pipeline(
    coro: Coroutine[Any, Any, None], redis: Redis, url: str, job_id: str
) -> None:
    """Run the leader's pipeline coroutine, then release the single-flight lock.

    Runs regardless of how the pipeline finished — success, a caught failure
    that published ``error``, or (in principle) an escaped exception — so a
    lock is never held for its full TTL after the run it was guarding is
    actually over. Deliberately outside :func:`app.pipeline.mock.run_mock_pipeline`
    and :func:`app.pipeline.run.run_pipeline` themselves (files this task does
    not own): wrapping the call here keeps M13 entirely inside the file that
    owns single-flight, whichever pipeline is chosen.
    """
    try:
        await coro
    finally:
        await _release_inflight(redis, url, job_id)


class Pipeline(Protocol):
    """What this route needs of a pipeline, and all it needs.

    The seam milestone 2 turns on: :func:`~app.pipeline.run.run_pipeline` and
    :func:`~app.pipeline.mock.run_mock_pipeline` are interchangeable here because
    they take the same arguments and owe the stream the same events — a
    ``claims_found``, one ``claim`` per claim, a cache write, then ``done`` or
    ``error``. Neither may raise: the route spawns it and never awaits it.
    """

    def __call__(
        self,
        redis: Redis,
        job_id: str,
        request: CheckRequest,
        *,
        settings: Settings,
    ) -> Coroutine[Any, Any, None]:
        """Run one check for ``job_id``, publishing everything it produces."""
        ...


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

    **A cache hit is rate-limited per URL** (:func:`_cache_hit_within_budget`,
    M11): a popular article being replayed is free, but not *infinitely* free —
    every hit still allocates a job id and a published event stream, so an
    unbounded flood of hits for one URL is amplification, not reading. Refused
    with a 429 whose ``detail`` is ``{"code": "rate_limited", "message": ...}``,
    distinct from the daily-cap 429 so a client can tell "you personally are
    out of checks" from "this article is hot right now, try again shortly"
    apart.

    **Concurrent misses of the same uncached URL share one pipeline run**
    (M13): the first request to claim the URL's single-flight lock
    (:func:`_claim_inflight_leadership`) spawns the pipeline as before and
    returns its own ``job_id``; every other concurrent miss for that same URL
    gets back *that* leader's ``job_id`` instead of starting a second, third,
    fourth run of the same claims. Both are still charged the daily cap — the
    lock only dedupes the LLM spend, not each reader's own allowance, so it
    creates no way to check more than the cap by racing yourself. A follower's
    ``GET /check/{job_id}/stream`` works exactly like any other client of that
    job (:func:`app.events.replay_and_stream` already supports more than one
    subscriber), and is bounded by the same
    :func:`stream_deadline_seconds` as the leader's own stream — so a leader
    that dies mid-run without publishing anything cannot leave a follower
    waiting forever either.
    """
    job_id = str(uuid4())
    url_str = str(payload.url)
    # AnyUrl normalises (it can append a trailing slash), so hash the string
    # form consistently — cache reads and writes must agree on one spelling.
    cached = await usable_cache_entry(redis, url_str, settings=settings)

    if cached is not None:
        if not await _cache_hit_within_budget(redis, url_str):
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail={"code": "rate_limited", "message": CACHE_HIT_RATE_LIMITED_MESSAGE},
            )
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

    # This request's own candidate id is marked started *before* it attempts
    # to claim leadership, unconditionally: if it wins, a follower reading the
    # now-visible inflight key is guaranteed the marker already exists (no
    # race with `stream_check`'s `job_exists` 404 check). If it loses, the
    # marker is simply unused and expires with the rest of a job's keys —
    # exactly the cost every miss already paid before single-flight existed.
    await mark_job_started(redis, job_id)
    ttl_seconds = stream_deadline_seconds(settings)
    leader_job_id = await _lead_or_join(redis, url_str, job_id, ttl_seconds)
    if leader_job_id == job_id:
        _spawn(
            _run_leader_pipeline(
                pipeline_for(settings)(redis, job_id, payload, settings=settings),
                redis,
                url_str,
                job_id,
            )
        )
    return CheckJob(job_id=leader_job_id, cached=False, claim_count=None)


def pipeline_for(settings: Settings) -> Pipeline:
    """Choose the pipeline this job runs: the real five stages, or the mock.

    ``USE_MOCK_PIPELINE=true`` selects :func:`~app.pipeline.mock.run_mock_pipeline`,
    which replays the six fictional fixture claims with the prototype's pacing
    and makes no API call — that is how the extension is developed and how the
    demo runs without a key.

    It is a **switch, not a fallback**. The real pipeline never degrades into it:
    a check that cannot run publishes ``error``, because a reader shown fixture
    verdicts for the article they are actually reading has no way to tell that is
    what happened. Both callables have the same signature, which is the whole
    reason this function can exist.
    """
    return run_mock_pipeline if settings.use_mock_pipeline else run_pipeline


async def usable_cache_entry(
    redis: Redis, url: str, *, settings: Settings
) -> dict[str, Any] | None:
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

    The same self-healing applies to M25's poisoned-cache case: an entry
    tagged :data:`~app.pipeline.mock.MOCK_CACHE_SOURCE` — the six fictional
    fixture claims a ``USE_MOCK_PIPELINE=true`` demo or dev run wrote under
    this *real* article URL — is refused exactly when the *current* request is
    not itself running the mock (``settings.use_mock_pipeline`` is False): the
    one case where the reader in front of us would otherwise be told invented
    verdicts about the article they are actually reading. It is deleted like
    any other unusable entry, so the real pipeline runs once and the cache
    heals to a real result. A mock run reusing another mock run's entry (both
    sides tagged) is unaffected — that is dev/demo replaying itself, not a
    poisoning risk — and an entry with no tag at all (the real pipeline never
    sets one) is unaffected either way.

    Only the offending claim's id and the rule it broke are logged. Neither is
    article text, and no install id is in scope here, so this cannot join a URL
    to a reader (brief, privacy rule 6).
    """
    cached = await get_check(redis, url)
    if cached is None:
        return None
    if cached.get("source") == MOCK_CACHE_SOURCE and not settings.use_mock_pipeline:
        logger.warning(
            "dropping a mock-pipeline cache entry for a real-pipeline request; re-checking"
        )
        await delete_check(redis, url)
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

    Derived from the configured job shape rather than picked out of the air, and
    the shape depends on which pipeline is running:

    * **The mock** publishes at most ``MAX_CLAIMS`` claims spaced
      ``MOCK_STEP_DELAY`` apart. :data:`STREAM_DEADLINE_FACTOR` multiplies that
      to leave room for a slow machine.
    * **The real pipeline** is bounded by its own timeouts rather than by a
      pacing constant, so :func:`real_pipeline_budget_seconds` adds them up. No
      multiplier is applied there: every term is already a worst case the
      pipeline enforces on itself, and multiplying a worst case by twenty would
      hold a dead job's pub/sub connection open for hours.

    :data:`MIN_STREAM_DEADLINE_SECONDS` floors both, so a configuration with no
    per-claim pacing still gets a usable budget rather than a zero-length one.
    """
    if not settings.use_mock_pipeline:
        return max(MIN_STREAM_DEADLINE_SECONDS, real_pipeline_budget_seconds(settings))
    expected = settings.max_claims * settings.mock_step_delay
    return max(MIN_STREAM_DEADLINE_SECONDS, STREAM_DEADLINE_FACTOR * expected)


def real_pipeline_budget_seconds(settings: Settings) -> float:
    """The arithmetic worst case for one real check, in seconds.

    Every term is a timeout the pipeline enforces itself, so this is a genuine
    ceiling rather than an estimate: one extraction call, then
    ``ceil(MAX_CLAIMS / PIPELINE_CONCURRENCY)`` batches of claims, each claim
    costing at most three provider calls (fact-check, then web search, then the
    numeric or attribution supplement) plus a stance call and a judge call. Each
    model call may be attempted ``1 + LLM_MAX_RETRIES`` times, and each attempt is
    capped at ``LLM_TIMEOUT_SECONDS`` by :class:`~app.llm.LLMClient`.

    A real check takes a small fraction of this. The number exists to be
    comfortably larger than the slowest honest run, so that the only stream the
    deadline ever cuts off is one whose worker is gone.
    """
    attempts = 1 + max(0, settings.llm_max_retries)
    per_model_call = settings.llm_timeout_seconds * attempts
    batches = ceil(settings.max_claims / max(1, settings.pipeline_concurrency))
    per_claim = 3 * PROVIDER_TIMEOUT_SECONDS + 2 * per_model_call
    return per_model_call + batches * per_claim


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
