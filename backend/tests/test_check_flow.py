"""The end-to-end check: POST, stream, cache hit, daily limit.

This is the milestone-1 walking skeleton exercised through the real HTTP
surface — ``POST /check`` spawns the mock pipeline, ``GET /check/{job_id}/stream``
relays what it publishes as SSE, and the popup's whole job is to render that
stream. The wire format is asserted literally (``id:`` / ``event:`` / ``data:``,
blank-line terminated) because both sides were written to it independently.

``httpx.ASGITransport`` buffers a response before handing it back, so each test
reads a finished stream rather than consuming it incrementally. That is fine
here: the stream is guaranteed to close after ``done`` or ``error``, and every
read is wrapped in a timeout so a regression that leaves it open fails the test
instead of hanging the suite.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from datetime import datetime
from itertools import pairwise
from typing import Any

import httpx
import pytest
from fakeredis.aioredis import FakeRedis
from fastapi import FastAPI

import app.routes.check as check_route
from app.cache import cache_key, get_check, set_check
from app.config import Settings
from app.events import mark_job_started
from app.invariants import validate_claims
from app.limits import CAP_KEY, singapore_today
from app.pipeline.mock import RESOLVE_ORDER, load_fixture_claims, tally
from app.schema_models import Claim, ClaimsFoundEvent, DoneEvent, ErrorEvent

from .conftest import TEST_DAILY_CAP, TEST_MAX_CLAIMS, build_settings

EXPECTED_COUNTS = {"supported": 2, "contradicted": 2, "missing_context": 1, "unverifiable": 1}
"""The fixture's verdict tally — what the ``done`` event must carry."""

EXPECTED_CLAIM_COUNT = 6

EXPECTED_CLAIM_IDS = ["c1", "c2", "c3", "c4", "c5", "c6"]
"""The fixture's claim ids in article order — what ``claims_found`` announces."""

STREAM_TIMEOUT_SECONDS = 20.0
"""Generous, since it only ever bites on a hang."""


def other_url(n: int) -> str:
    """A URL the cache has never seen, so a POST for it is a genuine miss.

    The daily cap is charged only on a cache miss, so any test that means to
    spend quota has to ask for a different article each time — twenty POSTs for
    one URL are one paid check and nineteen free replays.
    """
    return f"https://www.channelnewsasia.com/singapore/story-{n}"


# --------------------------------------------------------------------- helpers


def parse_sse(text: str) -> list[dict[str, Any]]:
    """Parse an SSE response body into ``{"id", "event", "data"}`` records.

    Deliberately written to the spec rather than to what the backend happens to
    emit: comment lines (the keep-alives) are dropped, ``data:`` lines within one
    message are joined with a newline, and the optional single space after each
    colon is stripped.
    """
    records: list[dict[str, Any]] = []
    for block in text.replace("\r\n", "\n").split("\n\n"):
        fields: dict[str, str] = {}
        data_lines: list[str] = []
        for line in block.split("\n"):
            if not line or line.startswith(":"):
                continue
            name, _, value = line.partition(":")
            value = value[1:] if value.startswith(" ") else value
            if name == "data":
                data_lines.append(value)
            else:
                fields[name] = value
        if not fields and not data_lines:
            continue
        records.append(
            {
                "id": int(fields["id"]) if "id" in fields else None,
                "event": fields.get("event"),
                "data": json.loads("\n".join(data_lines)) if data_lines else None,
            }
        )
    return records


async def read_stream(client: httpx.AsyncClient, job_id: str) -> list[dict[str, Any]]:
    """Consume a job's SSE stream to its close and return the parsed records."""
    async with asyncio.timeout(STREAM_TIMEOUT_SECONDS):
        response = await client.get(f"/check/{job_id}/stream")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["x-accel-buffering"] == "no"
    # `Connection` is hop-by-hop: the ASGI server owns it, the application must
    # not emit it. Asserted on the response as well as on SSE_HEADERS so a
    # reintroduction anywhere in the chain fails here.
    assert "connection" not in response.headers
    return parse_sse(response.text)


def error_payload(response: httpx.Response) -> dict[str, Any]:
    """Pull ``{"code", "message"}`` out of an error response.

    FastAPI nests an ``HTTPException`` detail under ``detail``; a hand-rolled
    handler would return it flat. Both are accepted so this test pins the
    contract (a ``daily_limit`` code and a reader-facing message) rather than
    one framework's envelope.
    """
    body = response.json()
    payload = body.get("detail", body)
    assert isinstance(payload, dict), body
    return payload


# ------------------------------------------------------------------ the stream


async def test_post_check_starts_a_job(
    client: httpx.AsyncClient, check_request_body: dict[str, str]
) -> None:
    """A cache miss returns a job id, ``cached: false`` and no claim count yet.

    The count is unknown until the pipeline has extracted claims, so the popup
    learns it from the stream's ``claims_found`` event.
    """
    response = await client.post("/check", json=check_request_body)

    assert response.status_code == 200
    job = response.json()
    assert set(job) == {"job_id", "cached", "claim_count"}
    assert job["cached"] is False
    assert job["claim_count"] is None
    assert job["job_id"]


async def test_the_stream_delivers_claims_found_then_six_claims_then_done(
    client: httpx.AsyncClient,
    check_request_body: dict[str, str],
    fixture_claims: list[dict[str, Any]],
) -> None:
    """The whole happy path, in order, over the wire."""
    job = (await client.post("/check", json=check_request_body)).json()
    records = await read_stream(client, job["job_id"])

    assert [record["event"] for record in records] == (
        ["claims_found"] + ["claim"] * EXPECTED_CLAIM_COUNT + ["done"]
    )

    found = ClaimsFoundEvent.model_validate(records[0]["data"])
    assert found.count == EXPECTED_CLAIM_COUNT
    # Article order, not resolve order: the ids arrive before any claim does so
    # the popup can lay out every row up front and fill each one when its own
    # claim lands. `count` is derived from the list, so it can never disagree.
    assert found.claim_ids == EXPECTED_CLAIM_IDS
    assert found.count == len(found.claim_ids)

    claim_records = records[1:-1]
    streamed_ids = [record["data"]["id"] for record in claim_records]
    assert sorted(streamed_ids) == sorted(claim["id"] for claim in fixture_claims)
    assert sorted(streamed_ids) == ["c1", "c2", "c3", "c4", "c5", "c6"]

    done = DoneEvent.model_validate(records[-1]["data"])
    assert done.counts.model_dump() == EXPECTED_COUNTS
    assert datetime.fromisoformat(done.checked_at)


async def test_every_streamed_claim_matches_the_fixture_and_the_schema(
    client: httpx.AsyncClient,
    check_request_body: dict[str, str],
    fixture_claims: list[dict[str, Any]],
) -> None:
    """Claims reach the client byte-for-byte as the fixture holds them.

    Validating each payload against the generated ``Claim`` also re-checks the
    product invariants on the wire, not just in the file: ``extra='forbid'``
    means a stray key would fail here too.
    """
    job = (await client.post("/check", json=check_request_body)).json()
    records = await read_stream(client, job["job_id"])

    by_id = {claim["id"]: claim for claim in fixture_claims}
    for record in records[1:-1]:
        payload = record["data"]
        model = Claim.model_validate(payload)
        assert payload == by_id[model.id]
        assert (model.confidence is None) is (model.verdict == "unverifiable")
        assert (model.sources == []) is (model.verdict == "unverifiable")


async def test_claims_resolve_in_the_demo_order(
    client: httpx.AsyncClient,
    check_request_body: dict[str, str],
    fixture_claims: list[dict[str, Any]],
) -> None:
    """Rows 3, 1, 6, 4, 2, 5 — the prototype's out-of-order fill.

    Claims arriving out of article order is the whole reason the popup places
    rows by ``start`` offset rather than by arrival, so the skeleton had better
    actually deliver them that way.
    """
    job = (await client.post("/check", json=check_request_body)).json()
    records = await read_stream(client, job["job_id"])

    expected = [fixture_claims[index]["id"] for index in RESOLVE_ORDER]
    assert expected == ["c3", "c1", "c6", "c4", "c2", "c5"]
    assert [record["data"]["id"] for record in records[1:-1]] == expected


async def test_sequence_numbers_are_strictly_increasing_from_one(
    client: httpx.AsyncClient, check_request_body: dict[str, str]
) -> None:
    """Every message carries an ``id:``, gap-free from 1.

    That number is what lets a reconnecting service worker replay a job and drop
    the messages it has already applied.
    """
    job = (await client.post("/check", json=check_request_body)).json()
    records = await read_stream(client, job["job_id"])

    ids = [record["id"] for record in records]
    assert ids == list(range(1, len(records) + 1))
    assert all(later > earlier for earlier, later in pairwise(ids))


async def test_the_raw_wire_format_is_id_event_data(
    client: httpx.AsyncClient, check_request_body: dict[str, str]
) -> None:
    """The literal bytes, since the extension's parser was written to them."""
    job = (await client.post("/check", json=check_request_body)).json()
    async with asyncio.timeout(STREAM_TIMEOUT_SECONDS):
        response = await client.get(f"/check/{job['job_id']}/stream")

    body = response.text
    assert body.startswith(
        "id: 1\nevent: claims_found\n"
        'data: {"type":"claims_found","count":6,'
        '"claim_ids":["c1","c2","c3","c4","c5","c6"]}\n\n'
    )
    assert body.endswith("\n\n")

    blocks = [block for block in body.split("\n\n") if block]
    assert len(blocks) == EXPECTED_CLAIM_COUNT + 2
    for index, block in enumerate(blocks, start=1):
        id_line, event_line, data_line = block.split("\n")
        assert id_line == f"id: {index}"
        assert event_line.startswith("event: ")
        assert data_line.startswith("data: ")
        # One message is always one `data:` line: json.dumps escapes any newline
        # inside the payload, so a client never has to join continuation lines.
        assert json.loads(data_line.removeprefix("data: "))


async def test_a_late_subscriber_still_receives_the_whole_job(
    client: httpx.AsyncClient, check_request_body: dict[str, str]
) -> None:
    """Opening the stream after the job finished replays everything.

    This is the popup being closed for the whole check and reopened at the end,
    and it is why events go to a Redis list as well as pub/sub.
    """
    job = (await client.post("/check", json=check_request_body)).json()
    await asyncio.sleep(0.3)  # let the pipeline run to completion first

    records = await read_stream(client, job["job_id"])
    assert [record["event"] for record in records] == (
        ["claims_found"] + ["claim"] * EXPECTED_CLAIM_COUNT + ["done"]
    )


# ------------------------------------------------------------------- the cache


async def test_a_second_check_of_the_same_url_is_a_cache_hit(
    client: httpx.AsyncClient, check_request_body: dict[str, str]
) -> None:
    """The repeat POST knows the claim count up front and replays instantly."""
    first = (await client.post("/check", json=check_request_body)).json()
    await read_stream(client, first["job_id"])

    response = await client.post("/check", json=check_request_body)
    assert response.status_code == 200
    second = response.json()
    assert second["cached"] is True
    assert second["claim_count"] == EXPECTED_CLAIM_COUNT
    assert second["job_id"] != first["job_id"]


async def test_a_cache_hit_replays_all_six_claims(
    client: httpx.AsyncClient,
    check_request_body: dict[str, str],
    fixture_claims: list[dict[str, Any]],
) -> None:
    """The replayed stream is the same shape as the live one, in article order.

    ``replay_cached`` publishes claims as they were stored, so a client that
    placed rows by arrival order would render a cache hit differently from a
    fresh check — another reason rows are placed by offset.
    """
    first = (await client.post("/check", json=check_request_body)).json()
    await read_stream(client, first["job_id"])

    second = (await client.post("/check", json=check_request_body)).json()
    records = await read_stream(client, second["job_id"])

    assert [record["event"] for record in records] == (
        ["claims_found"] + ["claim"] * EXPECTED_CLAIM_COUNT + ["done"]
    )
    assert [record["data"]["id"] for record in records[1:-1]] == [
        claim["id"] for claim in fixture_claims
    ]
    assert DoneEvent.model_validate(records[-1]["data"]).counts.model_dump() == EXPECTED_COUNTS


async def test_a_cache_hit_announces_the_same_claim_ids_in_the_same_order(
    client: httpx.AsyncClient, check_request_body: dict[str, str]
) -> None:
    """The replay's ``claims_found`` is identical to the live one.

    That identity is the whole point of sending the ids up front: a client
    allocates rows from ``claim_ids``, so if the cached path announced a
    different order the same article would render two different ways depending
    on whether anyone had checked it before.
    """
    first = (await client.post("/check", json=check_request_body)).json()
    live = await read_stream(client, first["job_id"])

    second = (await client.post("/check", json=check_request_body)).json()
    replayed = await read_stream(client, second["job_id"])

    live_found = ClaimsFoundEvent.model_validate(live[0]["data"])
    replayed_found = ClaimsFoundEvent.model_validate(replayed[0]["data"])
    assert replayed_found.claim_ids == live_found.claim_ids == EXPECTED_CLAIM_IDS
    assert live_found.count == replayed_found.count == EXPECTED_CLAIM_COUNT


async def test_a_different_url_is_not_served_from_the_cache(
    client: httpx.AsyncClient, check_request_body: dict[str, str]
) -> None:
    """The cache is keyed by URL, so a different article still runs the pipeline."""
    first = (await client.post("/check", json=check_request_body)).json()
    await read_stream(client, first["job_id"])

    other = {**check_request_body, "url": "https://www.channelnewsasia.com/singapore/other-story"}
    assert (await client.post("/check", json=other)).json()["cached"] is False


# --------------------------------------------------------- the poisoned cache


def poisoned_claim(claim: dict[str, Any]) -> dict[str, Any]:
    """A claim that is schema-valid but breaks a product invariant.

    ``unverifiable`` with the confidence and sources it kept from a real
    verdict — the exact shape ``shared/schema.json`` cannot forbid and
    :mod:`app.invariants` exists to catch (rules 2 and 3, decisions 4 and 5).
    """
    poisoned = {**claim, "verdict": "unverifiable"}
    assert poisoned["confidence"] is not None
    assert poisoned["sources"]
    return poisoned


async def seed_poisoned_cache(
    fake_redis: FakeRedis, url: str, fixture_claims: list[dict[str, Any]]
) -> dict[str, Any]:
    """Write a cache entry holding one illegal claim, as a bad build would have."""
    entry = {
        "claims": [poisoned_claim(fixture_claims[0]), *fixture_claims[1:]],
        "counts": EXPECTED_COUNTS,
        "checked_at": "2026-08-24T00:00:00Z",
    }
    await set_check(fake_redis, url, entry)
    assert await fake_redis.get(cache_key(url)) is not None
    return entry


async def test_a_poisoned_cache_entry_is_re_checked_not_replayed(
    client: httpx.AsyncClient,
    fake_redis: FakeRedis,
    check_request_body: dict[str, str],
    fixture_claims: list[dict[str, Any]],
) -> None:
    """A cache entry that breaks an invariant must not become a seven-day dead end.

    ``replay_cached`` validates every claim on its way out, so an entry holding
    an illegal claim publishes ``error`` instead of the check. If the POST had
    already committed to the hit — returned before charging the cap and before
    spawning the pipeline — nothing would ever re-run the article, and every
    reader who opened that URL would get the same error until the 7-day TTL
    expired. So the entry is validated *before* the hit is taken: a breach
    deletes it and falls through to the miss branch.
    """
    url = check_request_body["url"]
    await seed_poisoned_cache(fake_redis, url, fixture_claims)

    response = await client.post("/check", json=check_request_body)
    assert response.status_code == 200
    job = response.json()

    # Treated as a miss, not as a hit whose replay happens to fail.
    assert job["cached"] is False
    assert job["claim_count"] is None

    # The reader gets a normal, successful check.
    records = await read_stream(client, job["job_id"])
    assert [record["event"] for record in records] == (
        ["claims_found"] + ["claim"] * EXPECTED_CLAIM_COUNT + ["done"]
    )
    assert "error" not in [record["event"] for record in records]
    assert DoneEvent.model_validate(records[-1]["data"]).counts.model_dump() == EXPECTED_COUNTS

    # …and it was charged as a miss: the pipeline really ran.
    key = CAP_KEY.format(install_id=check_request_body["install_id"], day=singapore_today())
    assert await fake_redis.get(key) == "1"


async def test_a_poisoned_cache_entry_is_removed_and_replaced(
    client: httpx.AsyncClient,
    fake_redis: FakeRedis,
    check_request_body: dict[str, str],
    fixture_claims: list[dict[str, Any]],
) -> None:
    """The corrupt entry is gone afterwards, and the cache heals itself.

    The re-check writes its own result over the key, so the *next* reader gets a
    clean cache hit rather than paying for the same re-check again.
    """
    url = check_request_body["url"]
    await seed_poisoned_cache(fake_redis, url, fixture_claims)

    first = (await client.post("/check", json=check_request_body)).json()
    await read_stream(client, first["job_id"])

    healed = await get_check(fake_redis, url)
    assert healed is not None
    validate_claims(healed["claims"])  # raises if the poison survived
    assert [claim["id"] for claim in healed["claims"]] == EXPECTED_CLAIM_IDS
    assert healed["claims"] == fixture_claims
    assert healed["counts"] == EXPECTED_COUNTS

    second = (await client.post("/check", json=check_request_body)).json()
    assert second["cached"] is True
    assert second["claim_count"] == EXPECTED_CLAIM_COUNT
    replayed = await read_stream(client, second["job_id"])
    assert [record["event"] for record in replayed] == (
        ["claims_found"] + ["claim"] * EXPECTED_CLAIM_COUNT + ["done"]
    )


async def test_usable_cache_entry_deletes_the_key_it_rejects(
    fake_redis: FakeRedis,
    check_request_body: dict[str, str],
    fixture_claims: list[dict[str, Any]],
) -> None:
    """The helper itself: a breach returns None *and* clears the key.

    Asserted directly rather than through the endpoint because the re-check
    immediately writes a fresh entry over the same key, which would hide a
    version of this that returned None without deleting anything — and that
    version leaves the poison in place for any request that fails before the
    pipeline finishes.
    """
    url = check_request_body["url"]
    await seed_poisoned_cache(fake_redis, url, fixture_claims)

    assert await check_route.usable_cache_entry(fake_redis, url) is None
    assert await fake_redis.get(cache_key(url)) is None
    assert await get_check(fake_redis, url) is None


async def test_usable_cache_entry_keeps_a_healthy_entry(
    fake_redis: FakeRedis,
    check_request_body: dict[str, str],
    fixture_claims: list[dict[str, Any]],
) -> None:
    """The other half: a legal entry is returned untouched, key and all.

    Without this, "delete everything" would pass the test above and quietly
    turn the 7-day cache off.
    """
    url = check_request_body["url"]
    entry = {
        "claims": fixture_claims,
        "counts": EXPECTED_COUNTS,
        "checked_at": "2026-08-24T00:00:00Z",
    }
    await set_check(fake_redis, url, entry)

    assert await check_route.usable_cache_entry(fake_redis, url) == entry
    assert await fake_redis.get(cache_key(url)) is not None


async def test_an_unknown_url_is_still_a_plain_miss(
    fake_redis: FakeRedis, check_request_body: dict[str, str]
) -> None:
    """No entry at all is None, with nothing to delete."""
    assert await check_route.usable_cache_entry(fake_redis, check_request_body["url"]) is None


# ------------------------------------------------------------- the daily limit


async def test_a_check_past_the_daily_cap_is_a_429(
    client: httpx.AsyncClient, check_request_body: dict[str, str]
) -> None:
    """Past the cap the API refuses with ``daily_limit`` and a reader-facing line.

    The message is what the popup's error state shows, so it has to read as a
    sentence and must not slip into vocabulary the product does not use.

    A different article each time, because only a cache miss costs an allowance.
    """
    for n in range(TEST_DAILY_CAP):
        body = {**check_request_body, "url": other_url(n)}
        assert (await client.post("/check", json=body)).status_code == 200

    response = await client.post("/check", json={**check_request_body, "url": other_url(99)})
    assert response.status_code == 429

    payload = error_payload(response)
    assert payload["code"] == "daily_limit"
    message = payload["message"]
    assert isinstance(message, str) and message.strip()
    assert str(TEST_DAILY_CAP) in message
    assert "flagged" not in message.lower()


async def test_the_cap_is_per_install_id(
    client: httpx.AsyncClient, check_request_body: dict[str, str]
) -> None:
    """One install ID hitting the cap must not lock out another reader.

    School laptops share networks; they must not share an allowance.
    """
    for n in range(TEST_DAILY_CAP):
        await client.post("/check", json={**check_request_body, "url": other_url(n)})
    exhausted = {**check_request_body, "url": other_url(99)}
    assert (await client.post("/check", json=exhausted)).status_code == 429

    other = {**exhausted, "install_id": "99999999-8888-7777-6666-555555555555"}
    assert (await client.post("/check", json=other)).status_code == 200


async def test_a_cache_hit_does_not_spend_a_daily_check(
    client: httpx.AsyncClient, fake_redis: FakeRedis, check_request_body: dict[str, str]
) -> None:
    """Two POSTs for one URL consume exactly one unit of quota.

    The cap bounds LLM spend (``docs/decisions.md`` §10) and a replay from the
    7-day URL cache spends nothing, so charging for it would ration the wrong
    thing — a class of thirty reading the same article would exhaust their
    allowances on work the backend never did.
    """
    first = (await client.post("/check", json=check_request_body)).json()
    await read_stream(client, first["job_id"])

    second = await client.post("/check", json=check_request_body)
    assert second.status_code == 200
    assert second.json()["cached"] is True

    key = CAP_KEY.format(install_id=check_request_body["install_id"], day=singapore_today())
    assert await fake_redis.get(key) == "1"


async def test_cached_replays_keep_working_past_the_cap(
    client: httpx.AsyncClient, check_request_body: dict[str, str]
) -> None:
    """A reader who has spent every check can still re-open a cached article.

    The behavioural half of the same rule: quota buys pipeline runs, not reads.
    """
    # One paid check for the article under test, then the rest of the allowance
    # spent on other articles.
    assert (await client.post("/check", json=check_request_body)).status_code == 200
    for n in range(TEST_DAILY_CAP - 1):
        assert (
            await client.post("/check", json={**check_request_body, "url": other_url(n)})
        ).status_code == 200

    spent = {**check_request_body, "url": other_url(99)}
    assert (await client.post("/check", json=spent)).status_code == 429
    assert (await client.post("/check", json=check_request_body)).status_code == 200


async def test_an_uncached_url_is_charged_before_any_work_is_spawned(
    client: httpx.AsyncClient, fake_redis: FakeRedis, check_request_body: dict[str, str]
) -> None:
    """Consulting the cache first must not open a bypass.

    A miss pays up front, before a job id exists and before the pipeline is
    spawned, so there is no ordering in which the expensive path runs free.
    """
    response = await client.post("/check", json=check_request_body)
    assert response.status_code == 200

    key = CAP_KEY.format(install_id=check_request_body["install_id"], day=singapore_today())
    assert await fake_redis.get(key) == "1"


async def test_a_malformed_request_is_rejected(client: httpx.AsyncClient) -> None:
    """A body that is not a ``CheckRequest`` never reaches the pipeline."""
    response = await client.post("/check", json={"url": "not a url", "title": "t"})
    assert response.status_code == 422


# ------------------------------------------------------- the stream lifecycle


def test_the_stream_never_sets_a_hop_by_hop_header() -> None:
    """``Connection`` is the ASGI server's to manage, not the application's.

    An application that sets a hop-by-hop header is speaking for a connection it
    does not own; behind a proxy it is the header the proxy has to overrule.
    """
    assert "Connection" not in check_route.SSE_HEADERS
    assert not any(name.lower() == "connection" for name in check_route.SSE_HEADERS)
    assert check_route.SSE_HEADERS["Cache-Control"] == "no-cache"
    assert check_route.SSE_HEADERS["X-Accel-Buffering"] == "no"


async def test_an_unknown_job_id_is_a_404_not_a_subscription(
    client: httpx.AsyncClient,
) -> None:
    """A job id nobody was ever given must not open a stream.

    This route is not covered by the daily cap, so a stream that subscribed to
    any id handed anyone an unauthenticated way to hold a Redis pub/sub
    connection open — one per request, forever, since no worker would ever
    publish the ``done`` that ends it.
    """
    async with asyncio.timeout(STREAM_TIMEOUT_SECONDS):
        response = await client.get("/check/2b0f9a3e-0000-4000-8000-000000000000/stream")

    assert response.status_code == 404
    assert not response.headers["content-type"].startswith("text/event-stream")

    payload = error_payload(response)
    assert payload["code"] == "unknown_job"
    assert isinstance(payload["message"], str) and payload["message"].strip()
    assert "flagged" not in payload["message"].lower()


async def test_a_job_whose_worker_never_publishes_times_out(
    client: httpx.AsyncClient,
    fake_redis: FakeRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A started job that goes silent ends in ``error``/``timeout``, not a hang.

    The real case: ``POST /check`` returns a job id and spawns the pipeline, then
    the uvicorn process restarts. The task is process-local, so it is simply
    gone and nothing will ever publish ``done`` for that id. Before the deadline
    existed the connected client sat there collecting a keep-alive every twenty
    seconds for as long as it cared to wait.
    """
    monkeypatch.setattr(check_route, "KEEPALIVE_SECONDS", 0.05)
    monkeypatch.setattr(check_route, "STREAM_DEADLINE_FACTOR", 0.0)
    monkeypatch.setattr(check_route, "MIN_STREAM_DEADLINE_SECONDS", 0.3)

    # A job that was started and then abandoned: the marker is there, the worker
    # is not, and the event list stays empty forever.
    job_id = "9f1c7b52-1111-4000-8000-111111111111"
    await mark_job_started(fake_redis, job_id)

    async with asyncio.timeout(STREAM_TIMEOUT_SECONDS):
        response = await client.get(f"/check/{job_id}/stream")

    assert response.status_code == 200
    records = parse_sse(response.text)
    assert [record["event"] for record in records] == ["error"]

    error = ErrorEvent.model_validate(records[0]["data"])
    assert error.code == "timeout"
    assert error.message.strip()
    assert "flagged" not in error.message.lower()

    # The relay invented this event, so it borrows no job sequence number: no
    # `id:` line, and the client's last event id is left where it was.
    assert records[0]["id"] is None
    assert "\nid:" not in response.text and not response.text.startswith("id:")


async def test_the_deadline_is_derived_from_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """The budget scales with the configured job shape, and never hits zero."""
    monkeypatch.setattr(check_route, "STREAM_DEADLINE_FACTOR", 4.0)
    monkeypatch.setattr(check_route, "MIN_STREAM_DEADLINE_SECONDS", 10.0)

    slow = build_settings(max_claims=8, mock_step_delay=1.0)
    assert check_route.stream_deadline_seconds(slow) == 32.0

    # A configuration with no per-claim pacing still gets a usable budget.
    instant = build_settings(max_claims=8, mock_step_delay=0.0)
    assert check_route.stream_deadline_seconds(instant) == 10.0


async def test_a_live_job_is_not_cut_off_by_the_deadline(
    client: httpx.AsyncClient, check_request_body: dict[str, str]
) -> None:
    """The deadline is a backstop, not a service-level target.

    Cutting a slow-but-alive check off would be a far worse bug than holding one
    connection open a little longer, so the real settings must leave a job that
    is still publishing plenty of room.
    """
    job = (await client.post("/check", json=check_request_body)).json()
    records = await read_stream(client, job["job_id"])

    assert [record["event"] for record in records][-1] == "done"


# --------------------------------------------------------------- the keepalive


async def test_the_stream_sends_keep_alive_comments_without_dying(
    make_app: Callable[[Settings], FastAPI],
    check_request_body: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Silence produces ``: keep-alive`` comments, and the stream survives them.

    The MV3 service worker's ``fetch`` is idle-killed without this. It is also
    the subtle failure mode in the relay: cancelling the in-flight pull on each
    keep-alive would close the source generator and end the stream early, so the
    assertion that all six claims *and* ``done`` still arrive is the real test.
    """
    monkeypatch.setattr(check_route, "KEEPALIVE_SECONDS", 0.05)
    app = make_app(
        build_settings(
            daily_cap=TEST_DAILY_CAP,
            max_claims=TEST_MAX_CLAIMS,
            mock_step_delay=0.25,
        )
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        job = (await client.post("/check", json=check_request_body)).json()
        async with asyncio.timeout(STREAM_TIMEOUT_SECONDS):
            response = await client.get(f"/check/{job['job_id']}/stream")

    assert ": keep-alive\n\n" in response.text

    records = parse_sse(response.text)
    assert [record["event"] for record in records] == (
        ["claims_found"] + ["claim"] * EXPECTED_CLAIM_COUNT + ["done"]
    )
    assert [record["id"] for record in records] == list(range(1, len(records) + 1))


# ------------------------------------------------------------------- the tally


def test_tally_counts_the_fixture_verdicts() -> None:
    """``tally`` is the only place the ``done`` counts come from."""
    claims = load_fixture_claims(TEST_MAX_CLAIMS)

    assert len(claims) == EXPECTED_CLAIM_COUNT
    assert tally(claims) == EXPECTED_COUNTS
    assert sum(tally(claims).values()) == EXPECTED_CLAIM_COUNT
    assert set(tally(claims)) == set(EXPECTED_COUNTS)


def test_tally_reports_zero_for_absent_verdicts() -> None:
    """All four verdicts are always present, zero included — the popup renders a
    counts line, not a sparse map."""
    assert tally([]) == {
        "supported": 0,
        "contradicted": 0,
        "missing_context": 0,
        "unverifiable": 0,
    }
