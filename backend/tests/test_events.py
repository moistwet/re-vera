"""The per-job event log: atomic writes, positional sequence numbers, markers.

:mod:`app.events` is the only thing between the pipeline and the SSE relay, so
its guarantees are load-bearing everywhere else.

* A record is stored in one atomic step — there is no moment at which the replay
  list holds a row that does not yet say what it is, because the sequence number
  a record gets *is* its position in the list rather than something written into
  it afterwards.
* Delivery cannot be separated from that step. Two workers publishing for the
  same job can never let a higher number reach the channel first, which would
  not be a late event but a lost one: :func:`replay_and_stream` drops anything
  numbered at or below what it has already yielded.
* The ``job:{id}:started`` marker is what lets the stream endpoint tell a real
  job from a made-up id, and it is refreshed by every event, so it cannot expire
  under a job that is still talking.

Everything here runs against fakeredis; nothing needs a live server.
"""

from __future__ import annotations

import asyncio
import json
import time
import tomllib
from pathlib import Path
from typing import Any

import pytest
from fakeredis.aioredis import FakeRedis

from app.events import (
    CHANNEL,
    EVENTS_KEY,
    JOB_TTL_SECONDS,
    STARTED_KEY,
    job_exists,
    mark_job_started,
    publish_event,
    replay_and_stream,
)

JOB_ID = "job-under-test"

PYPROJECT_PATH = Path(__file__).resolve().parents[1] / "pyproject.toml"


# ------------------------------------------------- the environment these need


def test_the_dev_extra_asks_for_fakeredis_with_lua() -> None:
    """``publish_event`` runs a Lua script, so the test Redis must speak Lua.

    fakeredis can only answer ``EVAL``/``EVALSHA`` when ``lupa`` is installed,
    and it pulls ``lupa`` only through its own ``lua`` extra. A plain
    ``fakeredis`` pin therefore leaves a fresh checkout — anyone following
    README step 1 — with twenty-five red tests in this file and
    ``ResponseError: unknown command 'evalsha'`` as the only clue.

    Pinned here rather than left to whoever next tidies the dependency list:
    ``[lua]`` looks like an optional nicety and is not one. It is a test-time
    dependency of the code under test.
    """
    dev_extra = tomllib.loads(PYPROJECT_PATH.read_text(encoding="utf-8"))["project"][
        "optional-dependencies"
    ]["dev"]

    fakeredis_pins = [pin for pin in dev_extra if pin.split(">=")[0].startswith("fakeredis")]
    assert fakeredis_pins, "the dev extra must pin fakeredis"
    assert all(pin.startswith("fakeredis[lua]") for pin in fakeredis_pins), fakeredis_pins


async def test_the_test_redis_can_actually_run_a_script(fake_redis: FakeRedis) -> None:
    """The behavioural half of the test above, in one line and one error.

    Asserts the capability rather than the pin, so an install that satisfies the
    pin but still cannot run Lua (a broken ``lupa`` wheel, say) fails here — with
    a test whose name says what is missing — instead of scattering
    ``unknown command 'evalsha'`` across every other test in the file.
    """
    assert await fake_redis.eval("return 7", 0) == 7


async def drain(redis: FakeRedis, job_id: str) -> list[dict[str, Any]]:
    """Collect every record :func:`replay_and_stream` yields for a finished job."""
    records: list[dict[str, Any]] = []
    async with asyncio.timeout(5.0):
        async for record in replay_and_stream(redis, job_id):
            records.append(record)
    return records


async def subscribed(redis: FakeRedis, job_id: str) -> Any:
    """Return a pub/sub handle that is confirmed subscribed to ``job_id``.

    Waits for the SUBSCRIBE reply, so a publish issued after this returns cannot
    be missed and a test never races the subscription.
    """
    pubsub = redis.pubsub()
    await pubsub.subscribe(CHANNEL.format(job_id=job_id))
    async with asyncio.timeout(5.0):
        while True:
            message = await pubsub.get_message(timeout=0.1)
            if message is not None and message.get("type") == "subscribe":
                return pubsub


async def collect(pubsub: Any, count: int) -> list[dict[str, Any]]:
    """Read ``count`` published records off ``pubsub``, in delivery order."""
    received: list[dict[str, Any]] = []
    async with asyncio.timeout(5.0):
        while len(received) < count:
            message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.1)
            if message is not None:
                received.append(json.loads(message["data"]))
    return received


class LaggingRedis:
    """A Redis view that waits ``delay`` seconds before each command it is given.

    fakeredis answers in-process, so concurrent publishers barely interleave;
    a real server is a round trip away and they interleave constantly. Handing
    each publisher its own view of the *same* fakeredis, with its own latency,
    puts that scheduling window back deterministically — a publisher whose
    command is slow is overtaken by one whose command is fast.

    The delay lands before a command is sent, never in the middle of one, so an
    implementation that assigns a sequence number and delivers it in a single
    server-side step is untouched by it, while one that takes two round trips
    is torn apart: the second publisher's delivery lands ahead of the first's.
    """

    def __init__(self, inner: FakeRedis, delay: float) -> None:
        self._inner = inner
        self._delay = delay

    def register_script(self, script: str) -> Any:
        """Register on the wrapped client, then point the script back at us.

        ``AsyncScript`` sends its ``EVALSHA`` through whatever client it was
        registered with, so without this the script call would slip past the
        delay and the test would prove nothing.
        """
        registered = self._inner.register_script(script)
        registered.registered_client = self
        return registered

    def __getattr__(self, name: str) -> Any:
        attribute = getattr(self._inner, name)
        if not callable(attribute):
            return attribute

        def call(*args: Any, **kwargs: Any) -> Any:
            result = attribute(*args, **kwargs)
            return self._after_the_delay(result) if asyncio.iscoroutine(result) else result

        return call

    async def _after_the_delay(self, awaitable: Any) -> Any:
        await asyncio.sleep(self._delay)
        return await awaitable


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


async def test_the_published_record_is_the_stored_record_plus_its_number(
    fake_redis: FakeRedis,
) -> None:
    """The two copies never drift: the script prepends ``seq`` and nothing else.

    The published record is built inside Lua by splicing the number in front of
    the stored JSON rather than by re-encoding the payload, which is what keeps
    an empty ``sources`` list an empty *list*. Re-encoding through a Lua JSON
    codec would hand a client ``"sources": {}`` for every unverifiable claim.
    """
    payload = {"id": "c1", "verdict": "unverifiable", "confidence": None, "sources": []}
    pubsub = await subscribed(fake_redis, JOB_ID)
    try:
        await publish_event(fake_redis, JOB_ID, "claim", payload)
        published = (await collect(pubsub, 1))[0]
    finally:
        await pubsub.aclose()

    stored = await fake_redis.lrange(EVENTS_KEY.format(job_id=JOB_ID), 0, -1)
    assert published == {"seq": 1, "event": "claim", "data": payload}
    assert json.loads(stored[0]) == {"event": "claim", "data": payload}
    assert published["data"]["sources"] == []


# ------------------------------------------------------- ordering under load


async def test_the_lagging_proxy_really_delays_the_scripted_call(fake_redis: FakeRedis) -> None:
    """Pins the mechanism the two ordering tests below depend on.

    :class:`LaggingRedis` forces its window by reassigning
    ``AsyncScript.registered_client``, which is redis-py's own attribute and not
    part of its documented surface. If a future redis-py renames it, the script
    would send its ``EVALSHA`` through the *undelayed* client: the ordering tests
    would keep passing while quietly no longer forcing the interleaving they
    exist to force — green, and proving nothing.

    So assert the delay is actually felt on the path ``publish_event`` takes.
    A single publish costs one delay; the assertion is one-sided (at least the
    delay, never at most) so a slow machine cannot make it flaky.
    """
    delay = 0.05
    started = time.monotonic()
    await publish_event(LaggingRedis(fake_redis, delay=delay), JOB_ID, "claim", {"id": "c1"})
    elapsed = time.monotonic() - started

    assert elapsed >= delay, (
        f"the scripted publish took {elapsed:.3f}s, less than the {delay}s the proxy injects — "
        "LaggingRedis is no longer intercepting the script's client"
    )


async def test_delivery_is_never_reordered_against_sequence_assignment(
    fake_redis: FakeRedis,
) -> None:
    """Concurrent publishers deliver every number once, in ascending order.

    The regression: an earlier fix made ``publish_event`` do ``MULTI(RPUSH +
    EXPIRE)`` and then ``PUBLISH`` as a second round trip. That is atomic but no
    longer ordered — with two coroutines publishing for the same job, one could
    be assigned seq 3 and be overtaken on the channel by seq 4. A reader drops
    anything at or below the highest number it has yielded, so the overtaken
    claim was not merely late, it never arrived at all.

    Eight publishers run concurrently, alternating slow and fast, which under
    the old implementation delivered ``[2, 4, 6, 8, 1, 3, 5, 7]`` — four claims
    silently lost. Storing, expiring and publishing in one Lua step makes that
    impossible: the channel order is exactly the replay-list order.
    """
    publisher_count = 8
    pubsub = await subscribed(fake_redis, JOB_ID)
    try:
        publishers = [
            publish_event(
                LaggingRedis(fake_redis, delay=0.02 if n % 2 == 0 else 0.0),
                JOB_ID,
                "claim",
                {"id": f"c{n}"},
            )
            for n in range(publisher_count)
        ]
        assigned = await asyncio.gather(*publishers)
        received = await collect(pubsub, publisher_count)
    finally:
        await pubsub.aclose()

    assert sorted(assigned) == list(range(1, publisher_count + 1))
    assert [record["seq"] for record in received] == list(range(1, publisher_count + 1))

    stored = await fake_redis.lrange(EVENTS_KEY.format(job_id=JOB_ID), 0, -1)
    assert [{"event": r["event"], "data": r["data"]} for r in received] == [
        json.loads(raw) for raw in stored
    ]


async def test_a_scrambled_burst_still_reaches_a_reader_whole(fake_redis: FakeRedis) -> None:
    """The end the bug was felt at: every claim reaches the stream exactly once.

    Same interleaving as above, read through :func:`replay_and_stream` — the
    thing that de-duplicates by sequence number and would therefore have thrown
    the overtaken claims away.
    """
    claim_count = 6
    reader = asyncio.create_task(drain(fake_redis, JOB_ID))
    await asyncio.sleep(0)  # let the reader subscribe before anything is published

    await asyncio.gather(
        *(
            publish_event(
                LaggingRedis(fake_redis, delay=0.02 if n % 2 == 0 else 0.0),
                JOB_ID,
                "claim",
                {"id": f"c{n}"},
            )
            for n in range(claim_count)
        )
    )
    await publish_event(fake_redis, JOB_ID, "done", {"type": "done"})

    records = await reader

    assert [record["seq"] for record in records] == list(range(1, claim_count + 2))
    assert [record["event"] for record in records] == ["claim"] * claim_count + ["done"]
    assert sorted(record["data"]["id"] for record in records[:claim_count]) == [
        f"c{n}" for n in range(claim_count)
    ]


# ------------------------------------------------------------------ the TTLs


async def test_storing_an_event_also_sets_the_job_ttl(fake_redis: FakeRedis) -> None:
    """The TTL rides in the same transaction, so a stored event never lacks one."""
    await publish_event(fake_redis, JOB_ID, "claim", {"id": "c1"})

    ttl = await fake_redis.ttl(EVENTS_KEY.format(job_id=JOB_ID))
    assert 0 < ttl <= JOB_TTL_SECONDS


async def test_publishing_an_event_refreshes_the_started_marker(fake_redis: FakeRedis) -> None:
    """Both of a job's keys get their window pushed out by the same write.

    ``mark_job_started`` sets the marker once, at POST time, and never touches
    it again; only this refresh keeps it in step with the event list, whose TTL
    is re-issued on every event.
    """
    await mark_job_started(fake_redis, JOB_ID)
    marker = STARTED_KEY.format(job_id=JOB_ID)
    await fake_redis.expire(marker, 5)  # stand in for an hour of a long job elapsing

    await publish_event(fake_redis, JOB_ID, "claim", {"id": "c1"})

    assert await fake_redis.ttl(marker) > 5
    assert await fake_redis.ttl(marker) == await fake_redis.ttl(EVENTS_KEY.format(job_id=JOB_ID))


async def test_a_job_that_keeps_publishing_keeps_its_marker_alive(
    fake_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A live job outlives the window its marker was originally written with.

    The regression: the marker was written once with a fixed hour and never
    refreshed, while the event list's TTL slid forward on every write. A job
    that ran longer than the window therefore lost its marker while its events
    were still there, and the stream endpoint — which 404s on a missing marker —
    would refuse a job that was alive and still publishing.

    Run against a one-second window rather than the real hour: ``idle-job``
    marks the moment a never-refreshed marker dies, and the job that keeps
    publishing must still be there well past it.
    """
    monkeypatch.setattr("app.events.JOB_TTL_SECONDS", 1)
    idle_job = "idle-job"
    await mark_job_started(fake_redis, JOB_ID)
    await mark_job_started(fake_redis, idle_job)

    for n in range(3):
        await asyncio.sleep(0.5)
        await publish_event(fake_redis, JOB_ID, "claim", {"id": f"c{n}"})

    # 1.5 s in: a marker written once with a one-second window is long gone.
    assert await job_exists(fake_redis, idle_job) is False
    assert await job_exists(fake_redis, JOB_ID) is True


async def test_publishing_never_invents_a_marker_for_an_unstarted_job(
    fake_redis: FakeRedis,
) -> None:
    """Refreshing is not creating: only ``POST /check`` makes a job id real.

    The script refreshes the marker with ``EXPIRE``, which is a no-op on a key
    that does not exist, so events published for an id nobody started leave it
    just as non-existent as it was.
    """
    await publish_event(fake_redis, JOB_ID, "claim", {"id": "c1"})

    assert await job_exists(fake_redis, JOB_ID) is False


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
