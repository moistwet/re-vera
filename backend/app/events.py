"""Per-job event fan-out: a Redis list for replay, pub/sub for live delivery.

Every event a job produces is written twice — appended to the list
``job:{job_id}:events`` (what a late or reconnecting client replays) and
published on the channel ``job:{job_id}`` (what a connected client follows).
The published record carries its sequence number explicitly::

    {"seq": 3, "event": "claim", "data": {...}}

``seq`` is 1-based and gap-free, so a client that replays the list and then
follows the channel can drop anything it has already seen. The SSE layer copies
it into the ``id:`` field of each message.

A record's sequence number *is* its 1-based position in the replay list, so the
stored copy leaves ``seq`` out and :func:`replay_and_stream` reads it back from
the position. That is what makes storing an event a single atomic step: one
``RPUSH`` both appends the record and reports the number it was given, and a
process that dies immediately afterwards leaves a complete record behind rather
than a half-written one.

A job also gets a marker key, ``job:{job_id}:started``, written by ``POST
/check`` before the worker is spawned. It is what lets the stream endpoint tell
"this job exists and may still have events coming" from "nobody ever started
this id", which it must, because a stream opened on an id nobody started would
otherwise wait for events that can never arrive.

Nothing here reads settings or builds a Redis client: the caller passes the
client in, so tests can hand these functions a fakeredis instance.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import AsyncIterator
from typing import Any

from redis.asyncio import Redis
from redis.asyncio.client import PubSub

logger = logging.getLogger(__name__)

EVENTS_KEY = "job:{job_id}:events"
"""Redis list holding every record published for a job, in order."""

CHANNEL = "job:{job_id}"
"""Pub/sub channel carrying the same records live."""

STARTED_KEY = "job:{job_id}:started"
"""Marker proving a job id was actually handed out and a worker spawned for it.

Written by ``POST /check``; read by the stream endpoint, which 404s without it
rather than holding a pub/sub connection open for a job that will never speak.
"""

JOB_TTL_SECONDS = 3600
"""How long a job's event list and its started marker survive after the job's
last event. Both use the same window so a stream can never find the marker for a
job whose events have already expired, or the reverse."""

TERMINAL_EVENTS = frozenset({"done", "error"})
"""Event names that close the stream; nothing follows them."""

_POLL_INTERVAL_SECONDS = 1.0
"""How long a single pub/sub read waits before looping. Bounded rather than
infinite so the generator returns to the event loop regularly and stays
promptly cancellable when a client disconnects."""

_SUBSCRIBE_TIMEOUT_SECONDS = 5.0
"""How long to wait for the server to confirm a SUBSCRIBE before giving up on
the confirmation and reading the replay list anyway."""


async def mark_job_started(redis: Redis, job_id: str) -> None:
    """Record that ``job_id`` was handed out and a worker was spawned for it.

    Called by ``POST /check`` before the background task starts, so a client
    that opens the stream the instant it gets the job id always finds the
    marker.
    """
    await redis.set(STARTED_KEY.format(job_id=job_id), "1", ex=JOB_TTL_SECONDS)


async def job_exists(redis: Redis, job_id: str) -> bool:
    """Return True when ``job_id`` was started and has not yet expired."""
    return bool(await redis.exists(STARTED_KEY.format(job_id=job_id)))


async def publish_event(redis: Redis, job_id: str, event: str, data: dict[str, Any]) -> int:
    """Store ``event`` in the job's replay list and publish it live.

    Returns the sequence number assigned to the record — RPUSH's new list
    length, so sequence numbers start at 1 and never skip.

    Assigning the number and storing the record is one atomic step: ``RPUSH``
    appends the finished record *and* reports the position it landed in, so
    there is no window in which the list holds a row that does not yet say what
    it is. The number is therefore never written into the stored copy — it is
    that row's position, and :func:`replay_and_stream` reads it back from there.
    ``EXPIRE`` rides along in the same ``MULTI`` so a stored event is never left
    without its TTL.

    ``PUBLISH`` follows, deliberately outside the transaction: it is delivery,
    not storage. A publish that fails costs a connected client nothing but a
    wait, because the record is already in the replay list that every stream
    reads first.
    """
    key = EVENTS_KEY.format(job_id=job_id)
    channel = CHANNEL.format(job_id=job_id)

    async with redis.pipeline(transaction=True) as pipe:
        pipe.rpush(key, json.dumps({"event": event, "data": data}))
        pipe.expire(key, JOB_TTL_SECONDS)
        length, _ = await pipe.execute()

    seq = int(length)
    await redis.publish(channel, json.dumps({"seq": seq, "event": event, "data": data}))
    return seq


async def replay_and_stream(redis: Redis, job_id: str) -> AsyncIterator[dict[str, Any]]:
    """Yield every event of ``job_id``: the stored ones first, then live ones.

    Yields ``{"seq": int, "event": str, "data": dict}`` records. The pub/sub
    subscription is opened *before* the stored list is read, so an event
    published in between reaches the subscriber instead of falling into the gap
    between the two; records already replayed are then dropped by sequence
    number. The iterator ends after a ``done`` or ``error`` record, and always
    tears the subscription down on the way out.
    """
    key = EVENTS_KEY.format(job_id=job_id)
    channel = CHANNEL.format(job_id=job_id)
    last_seq = 0

    def accept(record: dict[str, Any]) -> bool:
        """Return True the first time a sequence number is seen."""
        nonlocal last_seq
        if record["seq"] <= last_seq:
            return False
        last_seq = record["seq"]
        return True

    pubsub = redis.pubsub()
    try:
        await pubsub.subscribe(channel)
        # redis-py writes SUBSCRIBE without waiting for its reply, so wait for
        # the confirmation here: until the server has processed it, a publish
        # would be missed by this connection and could also land after the
        # replay read. Anything that arrives ahead of the confirmation is kept
        # and yielded below rather than dropped.
        pending = await _await_subscription(pubsub, job_id)

        # A record's sequence number is its 1-based position in this list, which
        # is why publish_event does not store one: enumerate() is the authority.
        for position, raw in enumerate(await redis.lrange(key, 0, -1), start=1):
            record = _decode_record(raw, job_id, seq=position)
            if record is None or not accept(record):
                continue
            yield record
            if record["event"] in TERMINAL_EVENTS:
                return

        while True:
            if pending:
                record = pending.pop(0)
            else:
                message = await pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=_POLL_INTERVAL_SECONDS
                )
                if message is None:
                    continue
                decoded = _decode_record(message.get("data"), job_id)
                if decoded is None:
                    continue
                record = decoded
            if not accept(record):
                continue
            yield record
            if record["event"] in TERMINAL_EVENTS:
                return
    finally:
        await pubsub.aclose()


async def _await_subscription(pubsub: PubSub, job_id: str) -> list[dict[str, Any]]:
    """Wait for the SUBSCRIBE confirmation, returning any records seen first.

    Messages should never overtake the confirmation on the same connection, but
    buffering them costs nothing and keeps the "no event is ever lost"
    guarantee independent of that assumption. Gives up after
    ``_SUBSCRIBE_TIMEOUT_SECONDS``: the replay list still covers stored events,
    so proceeding beats hanging.
    """
    buffered: list[dict[str, Any]] = []
    deadline = time.monotonic() + _SUBSCRIBE_TIMEOUT_SECONDS
    while True:
        message = await pubsub.get_message(timeout=_POLL_INTERVAL_SECONDS)
        if message is None:
            if time.monotonic() >= deadline:
                logger.warning("job %s: no SUBSCRIBE confirmation; replaying anyway", job_id)
                return buffered
            continue
        if message.get("type") == "subscribe":
            return buffered
        record = _decode_record(message.get("data"), job_id)
        if record is not None:
            buffered.append(record)


def _decode_record(raw: object, job_id: str, seq: int | None = None) -> dict[str, Any] | None:
    """Parse one stored or published record, or return None if it is unusable.

    ``seq`` supplies the sequence number for a record read from the replay list,
    where it is the row's position rather than part of the payload. Published
    records carry their own and are read with ``seq=None``.

    A malformed record is dropped rather than allowed to kill a live stream;
    only the job id is logged, never the payload (it carries article text).
    """
    if not isinstance(raw, str | bytes | bytearray):
        return None
    try:
        decoded = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        logger.warning("job %s: dropping an event record that is not valid JSON", job_id)
        return None
    if not isinstance(decoded, dict) or not isinstance(decoded.get("event"), str):
        logger.warning("job %s: dropping an event record with an unexpected shape", job_id)
        return None
    if seq is None:
        seq = decoded.get("seq")
    if not isinstance(seq, int):
        logger.warning("job %s: dropping an event record with no sequence number", job_id)
        return None
    data = decoded.get("data")
    return {
        "seq": seq,
        "event": decoded["event"],
        "data": data if isinstance(data, dict) else {},
    }
