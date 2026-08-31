"""The per-job event log: atomic writes, positional sequence numbers, markers.

:mod:`app.events` is the only thing between the pipeline and the SSE relay, so
its two guarantees are load-bearing everywhere else. First, a record is stored
in one atomic step — there is no moment at which the replay list holds a row
that does not yet say what it is, because the sequence number a record gets *is*
its position in the list rather than something written into it afterwards.
Second, the ``job:{id}:started`` marker is what lets the stream endpoint tell a
real job from a made-up id.

Everything here runs against fakeredis; nothing needs a live server.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fakeredis.aioredis import FakeRedis

from app.events import (
    EVENTS_KEY,
    JOB_TTL_SECONDS,
    STARTED_KEY,
    job_exists,
    mark_job_started,
    publish_event,
    replay_and_stream,
)

JOB_ID = "job-under-test"


async def drain(redis: FakeRedis, job_id: str) -> list[dict[str, Any]]:
    """Collect every record :func:`replay_and_stream` yields for a finished job."""
    records: list[dict[str, Any]] = []
    async with asyncio.timeout(5.0):
        async for record in replay_and_stream(redis, job_id):
            records.append(record)
    return records


# ------------------------------------------------------------ sequence numbers


async def test_sequence_numbers_start_at_one_and_never_skip(fake_redis: FakeRedis) -> None:
    """``publish_event`` returns the number the record was given, 1-based."""
    seqs = []
    for n in range(1, 4):
        seqs.append(await publish_event(fake_redis, JOB_ID, "claim", {"id": f"c{n}"}))
    assert seqs == [1, 2, 3]


async def test_a_stored_record_is_written_whole_in_one_step(fake_redis: FakeRedis) -> None:
    """No placeholder row ever exists — the regression this file was added for.

    The old implementation pushed ``{"seq": 0, "event": "", "data": {}}`` and
    rewrote it with a second round trip, so a process that died in between left
    a permanently unusable row in the middle of the job's replay list. Now the
    row goes in complete and its number is its position, so there is nothing to
    rewrite and nothing to half-write.
    """
    await publish_event(fake_redis, JOB_ID, "claim", {"id": "c1"})

    stored = await fake_redis.lrange(EVENTS_KEY.format(job_id=JOB_ID), 0, -1)
    assert [json.loads(raw) for raw in stored] == [{"event": "claim", "data": {"id": "c1"}}]


async def test_replay_numbers_records_by_their_position(fake_redis: FakeRedis) -> None:
    """A replayed record's ``seq`` comes from where it sits in the list."""
    for n in range(1, 4):
        await publish_event(fake_redis, JOB_ID, "claim", {"id": f"c{n}"})
    await publish_event(fake_redis, JOB_ID, "done", {"type": "done"})

    records = await drain(fake_redis, JOB_ID)

    assert [record["seq"] for record in records] == [1, 2, 3, 4]
    assert [record["event"] for record in records] == ["claim", "claim", "claim", "done"]
    assert records[0]["data"] == {"id": "c1"}


async def test_the_published_record_carries_its_sequence_number(fake_redis: FakeRedis) -> None:
    """A live subscriber gets ``seq`` in the payload; only replay uses positions."""
    pubsub = fake_redis.pubsub()
    await pubsub.subscribe(f"job:{JOB_ID}")
    try:
        await publish_event(fake_redis, JOB_ID, "claim", {"id": "c1"})
        async with asyncio.timeout(5.0):
            while True:
                message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.1)
                if message is not None:
                    break
    finally:
        await pubsub.aclose()

    assert json.loads(message["data"]) == {"seq": 1, "event": "claim", "data": {"id": "c1"}}


async def test_storing_an_event_also_sets_the_job_ttl(fake_redis: FakeRedis) -> None:
    """The TTL rides in the same transaction, so a stored event never lacks one."""
    await publish_event(fake_redis, JOB_ID, "claim", {"id": "c1"})

    ttl = await fake_redis.ttl(EVENTS_KEY.format(job_id=JOB_ID))
    assert 0 < ttl <= JOB_TTL_SECONDS


async def test_replay_stops_after_a_terminal_event(fake_redis: FakeRedis) -> None:
    """Nothing follows ``done`` or ``error``; the iterator ends there."""
    await publish_event(fake_redis, JOB_ID, "claims_found", {"type": "claims_found"})
    await publish_event(fake_redis, JOB_ID, "error", {"type": "error", "code": "internal"})
    await publish_event(fake_redis, JOB_ID, "claim", {"id": "c1"})

    records = await drain(fake_redis, JOB_ID)
    assert [record["event"] for record in records] == ["claims_found", "error"]


async def test_a_malformed_stored_record_is_skipped_not_fatal(fake_redis: FakeRedis) -> None:
    """One unreadable row must not take a whole stream down with it."""
    await publish_event(fake_redis, JOB_ID, "claims_found", {"type": "claims_found"})
    await fake_redis.rpush(EVENTS_KEY.format(job_id=JOB_ID), "not json at all")
    await publish_event(fake_redis, JOB_ID, "done", {"type": "done"})

    records = await drain(fake_redis, JOB_ID)
    assert [record["event"] for record in records] == ["claims_found", "done"]
    # The bad row still occupies position 2, so `done` keeps the number it was
    # given: positions are never renumbered to close a gap.
    assert [record["seq"] for record in records] == [1, 3]


# -------------------------------------------------------------- the job marker


async def test_an_unstarted_job_does_not_exist(fake_redis: FakeRedis) -> None:
    """The marker is the only thing that makes a job id real."""
    assert await job_exists(fake_redis, "never-handed-out") is False


async def test_marking_a_job_started_makes_it_exist_with_a_ttl(fake_redis: FakeRedis) -> None:
    """``POST /check`` writes this before spawning the worker."""
    await mark_job_started(fake_redis, JOB_ID)

    assert await job_exists(fake_redis, JOB_ID) is True
    ttl = await fake_redis.ttl(STARTED_KEY.format(job_id=JOB_ID))
    assert 0 < ttl <= JOB_TTL_SECONDS


async def test_the_marker_is_per_job(fake_redis: FakeRedis) -> None:
    """One started job does not vouch for another id."""
    await mark_job_started(fake_redis, JOB_ID)
    assert await job_exists(fake_redis, JOB_ID + "-other") is False
