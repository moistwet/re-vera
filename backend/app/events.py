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
the position. One ``RPUSH`` therefore both appends the finished record and
reports the number it was given, and a process that dies immediately afterwards
leaves a complete record behind rather than a half-written one.

Storing that record, refreshing the job's TTLs and publishing the record all
happen inside one Lua script (:data:`_PUBLISH_SCRIPT`), which is what keeps two
separate promises at once. *Atomicity*: nothing can observe the list grown but
un-expiring, and no client can be told about an event that is not in the replay
list. *Ordering*: the number a record gets and the delivery of that record are
one indivisible step, so two workers publishing for the same job can never have
one overtake the other on the channel — which would be permanent data loss,
because :func:`replay_and_stream` drops anything numbered at or below what it
has already seen.

A job also gets a marker key, ``job:{job_id}:started``, written by ``POST
/check`` before the worker is spawned. It is what lets the stream endpoint tell
"this job exists and may still have events coming" from "nobody ever started
this id", which it must, because a stream opened on an id nobody started would
otherwise wait for events that can never arrive. The same script refreshes that
marker, so it never expires out from under a job that is still talking.

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
"""How long a job's event list and its started marker survive its last event.

The window slides: every :func:`publish_event` re-issues ``EXPIRE`` on *both*
keys in the same atomic step, so an hour is measured from the job's most recent
event rather than from ``POST /check``. Refreshing them together is the point —
a marker on a shorter clock than the events would let the stream endpoint 404 a
job that is alive and still publishing, and events outliving their marker would
be unreachable anyway. A job that never publishes anything expires an hour after
:func:`mark_job_started` wrote the marker.
"""

TERMINAL_EVENTS = frozenset({"done", "error"})
"""Event names that close the stream; nothing follows them."""

_POLL_INTERVAL_SECONDS = 1.0
"""How long a single pub/sub read waits before looping. Bounded rather than
infinite so the generator returns to the event loop regularly and stays
promptly cancellable when a client disconnects."""

_SUBSCRIBE_TIMEOUT_SECONDS = 5.0
"""How long to wait for the server to confirm a SUBSCRIBE before giving up on
the confirmation and reading the replay list anyway."""

_PUBLISH_SCRIPT = """
-- Append one event to a job's replay list, refresh both of the job's TTLs and
-- deliver the event to live subscribers — one indivisible, correctly ordered
-- step. Returns the sequence number the record was given.
--
--   KEYS[1]  job:{id}:events    the replay list
--   KEYS[2]  job:{id}:started   the marker POST /check wrote
--   ARGV[1]  the record as JSON, with no "seq" key: {"event": ..., "data": ...}
--   ARGV[2]  TTL in seconds for both keys
--   ARGV[3]  the pub/sub channel (a channel is not a keyspace key, so it is an
--            argument rather than a KEYS entry)
local seq = redis.call('RPUSH', KEYS[1], ARGV[1])
redis.call('EXPIRE', KEYS[1], ARGV[2])
-- EXPIRE on a missing key is a no-op, so this refreshes a marker that exists
-- and never invents one for a job nobody started.
redis.call('EXPIRE', KEYS[2], ARGV[2])
-- The published copy is the stored record with its number in front: ARGV[1] is
-- a JSON object whose first key is "event", so replacing its opening brace with
-- '{"seq": <n>, ' yields exactly the stored bytes plus the seq field.
redis.call('PUBLISH', ARGV[3], '{"seq": ' .. seq .. ', ' .. string.sub(ARGV[1], 2))
return seq
"""
"""Lua source for :func:`publish_event`.

Lua rather than ``MULTI`` because only a script can use ``RPUSH``'s reply — the
sequence number — inside the same atomic step that publishes it. A ``MULTI``
cannot read its own results, which is why the previous implementation had to
publish in a second round trip, and why two concurrent publishers could then
deliver seq 4 before seq 3 and lose seq 3 to the stream's de-duplication.
"""


async def mark_job_started(redis: Redis, job_id: str) -> None:
    """Record that ``job_id`` was handed out and a worker was spawned for it.

    Called by ``POST /check`` before the background task starts, so a client
    that opens the stream the instant it gets the job id always finds the
    marker. Written with the same TTL the job's events get, and refreshed by
    every subsequent :func:`publish_event`, so the two keys expire together.
    """
    await redis.set(STARTED_KEY.format(job_id=job_id), "1", ex=JOB_TTL_SECONDS)


async def job_exists(redis: Redis, job_id: str) -> bool:
    """Return True when ``job_id`` was started and has not yet expired."""
    return bool(await redis.exists(STARTED_KEY.format(job_id=job_id)))


async def publish_event(redis: Redis, job_id: str, event: str, data: dict[str, Any]) -> int:
    """Store ``event`` in the job's replay list and publish it live.

    Returns the sequence number assigned to the record — RPUSH's new list
    length, so sequence numbers start at 1 and never skip.

    Storing, expiring and publishing happen inside :data:`_PUBLISH_SCRIPT`, in
    one indivisible step, which buys two things a two-round-trip version cannot
    have together:

    * **Atomicity.** ``RPUSH`` appends the finished record *and* reports the
      position it landed in, so there is no window in which the list holds a row
      that does not yet say what it is; the number is never written into the
      stored copy, because it is that row's position and
      :func:`replay_and_stream` reads it back from there. Both ``EXPIRE`` calls
      ride along, so a stored event never sits without its TTL and the started
      marker never expires while its job is still publishing.
    * **Ordering.** The number a record is given and the delivery of that record
      cannot be separated, so two coroutines publishing for the same job can
      never let seq 4 reach the channel ahead of seq 3. That matters because
      :func:`replay_and_stream` drops anything numbered at or below what it has
      already yielded: an overtaken event would not be late, it would be gone.

    The script is registered per call. ``register_script`` only wraps the source
    with its SHA — no round trip — and the wrapper sends ``EVALSHA``, falling
    back to ``EVAL`` once per server that has not cached it yet. That keeps this
    module free of per-client state, which is what lets tests hand it a
    throwaway fakeredis instance.
    """
    record = json.dumps({"event": event, "data": data})
    publish = redis.register_script(_PUBLISH_SCRIPT)
    seq = await publish(
        keys=[EVENTS_KEY.format(job_id=job_id), STARTED_KEY.format(job_id=job_id)],
        args=[record, JOB_TTL_SECONDS, CHANNEL.format(job_id=job_id)],
    )
    return int(seq)


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
