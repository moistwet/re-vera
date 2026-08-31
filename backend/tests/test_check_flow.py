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
from fastapi import FastAPI

import app.routes.check as check_route
from app.config import Settings
from app.pipeline.mock import RESOLVE_ORDER, load_fixture_claims, tally
from app.schema_models import Claim, ClaimsFoundEvent, DoneEvent

from .conftest import TEST_DAILY_CAP, TEST_MAX_CLAIMS, build_settings

EXPECTED_COUNTS = {"supported": 2, "contradicted": 2, "missing_context": 1, "unverifiable": 1}
"""The fixture's verdict tally — what the ``done`` event must carry."""

EXPECTED_CLAIM_COUNT = 6

STREAM_TIMEOUT_SECONDS = 20.0
"""Generous, since it only ever bites on a hang."""


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
        'id: 1\nevent: claims_found\ndata: {"type":"claims_found","count":6}\n\n'
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


async def test_a_different_url_is_not_served_from_the_cache(
    client: httpx.AsyncClient, check_request_body: dict[str, str]
) -> None:
    """The cache is keyed by URL, so a different article still runs the pipeline."""
    first = (await client.post("/check", json=check_request_body)).json()
    await read_stream(client, first["job_id"])

    other = {**check_request_body, "url": "https://www.channelnewsasia.com/singapore/other-story"}
    assert (await client.post("/check", json=other)).json()["cached"] is False


# ------------------------------------------------------------- the daily limit


async def test_a_check_past_the_daily_cap_is_a_429(
    client: httpx.AsyncClient, check_request_body: dict[str, str]
) -> None:
    """Past the cap the API refuses with ``daily_limit`` and a reader-facing line.

    The message is what the popup's error state shows, so it has to read as a
    sentence and must not slip into vocabulary the product does not use.
    """
    for _ in range(TEST_DAILY_CAP):
        assert (await client.post("/check", json=check_request_body)).status_code == 200

    response = await client.post("/check", json=check_request_body)
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
    for _ in range(TEST_DAILY_CAP):
        await client.post("/check", json=check_request_body)
    assert (await client.post("/check", json=check_request_body)).status_code == 429

    other = {**check_request_body, "install_id": "99999999-8888-7777-6666-555555555555"}
    assert (await client.post("/check", json=other)).status_code == 200


async def test_a_malformed_request_is_rejected(client: httpx.AsyncClient) -> None:
    """A body that is not a ``CheckRequest`` never reaches the pipeline."""
    response = await client.post("/check", json={"url": "not a url", "title": "t"})
    assert response.status_code == 422


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
