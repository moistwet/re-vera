"""End-to-end smoke test of the real pipeline against a **real** redis-server.

Not part of ``pytest``. The suite runs on ``fakeredis`` and must stay hermetic,
but ``app/events.py`` publishes through a Lua script and relies on ``RPUSH``
returning the new length inside that script, on ``EXPIRE`` semantics and on
pub/sub delivery ordering — three things a fake can only promise. This script
runs the whole milestone-2 pipeline over the fixture article onto a live Redis
and checks what the fake cannot:

1. the event sequence is ``claims_found`` → one ``claim`` per claim (arriving as
   each resolves, in any order) → ``done``, with ``claim_ids`` in article order
   and ``done.counts`` matching the claims actually published;
2. the 7-day cache entry exists by the time ``done`` reaches a live subscriber
   and did not exist while the claims were still streaming;
3. a second ``POST /check`` for the same URL is served from that cache;
4. one failing claim still yields a complete run, with that claim ``unverifiable``;
5. zero extracted claims yields ``claims_found`` with count 0 and ``done``, and
   does not hang;
6. milestone 1 still holds: the daily cap is a 429 and an unknown job id a 404.

**No API key and no network are involved.** The LLM sits behind a scripted
transport and retrieval behind fake providers — the same fakes
``tests/test_pipeline_run.py`` uses, imported rather than copied so this script
cannot drift from the suite. The only real I/O is the loopback socket to Redis.

Usage (from ``backend/``)::

    redis-server --port 6399 --save '' --appendonly no --daemonize yes
    uv run python scripts/live_redis_smoke.py --redis-url redis://localhost:6399/0
    redis-cli -p 6399 shutdown nosave

Exit status is 0 when every check passed, 1 otherwise.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Callable
from typing import Any
from uuid import uuid4

import httpx
import redis.asyncio as redis_asyncio
from fastapi import FastAPI
from redis.asyncio import Redis
from tests.test_pipeline_run import (
    NO_EVIDENCE_QUOTE,
    FakeSearch,
    check_request,
    extraction_answer,
    make_deps,
    pipeline_settings,
)

import app.pipeline.run as run_module
import app.routes.check as check_routes
from app.cache import cache_key
from app.config import Settings, get_settings
from app.events import CHANNEL, EVENTS_KEY
from app.llm import LLMBadRequest
from app.main import create_app
from app.pipeline.run import FAILED_CLAIM_EVIDENCE, PipelineDeps, run_pipeline
from app.routes.check import get_redis
from app.schema_models import CheckRequest

DEFAULT_REDIS_URL = "redis://localhost:6399/0"

failures: list[str] = []
"""Every check that did not hold. The script's exit status is derived from it."""


def check(label: str, condition: bool, detail: str = "") -> None:
    """Record one assertion and print it, without stopping at the first failure."""
    mark = "PASS" if condition else "FAIL"
    print(f"  [{mark}] {label}{f' — {detail}' if detail else ''}")
    if not condition:
        failures.append(label)


def heading(title: str) -> None:
    """Print a section header."""
    print(f"\n=== {title} ===")


class Subscriber:
    """A live pub/sub subscriber that records what the cache looked like per event.

    Reading the replay list after the fact cannot show *when* the cache was
    written; a subscriber can, because it is woken by each event as it is
    published and can look at the cache key right then.
    """

    def __init__(self, redis: Redis, job_id: str, url: str) -> None:
        self.redis = redis
        self.job_id = job_id
        self.url = url
        self.records: list[dict[str, Any]] = []

    async def collect(self, timeout: float = 30.0) -> list[dict[str, Any]]:
        """Follow the job's channel until ``done`` or ``error``, or ``timeout``."""
        pubsub = self.redis.pubsub()
        await pubsub.subscribe(CHANNEL.format(job_id=self.job_id))
        try:
            async with asyncio.timeout(timeout):
                while True:
                    message = await pubsub.get_message(
                        ignore_subscribe_messages=True, timeout=1.0
                    )
                    if message is None:
                        continue
                    record = json.loads(message["data"])
                    record["cached_now"] = bool(await self.redis.exists(cache_key(self.url)))
                    self.records.append(record)
                    if record["event"] in {"done", "error"}:
                        return self.records
        finally:
            await pubsub.aclose()


def events_named(records: list[dict[str, Any]], name: str) -> list[dict[str, Any]]:
    """The ``data`` payloads of the records called ``name``."""
    return [record["data"] for record in records if record["event"] == name]


async def stored_events(redis: Redis, job_id: str) -> list[dict[str, Any]]:
    """Every record the job wrote to its replay list, in order."""
    raw = await redis.lrange(EVENTS_KEY.format(job_id=job_id), 0, -1)
    return [json.loads(item) for item in raw]


async def flush(redis: Redis) -> None:
    """Empty the scratch database between scenarios."""
    await redis.flushdb()


def make_client(app: FastAPI) -> httpx.AsyncClient:
    """An in-process HTTP client for ``app``."""
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://smoke.test"
    )


def wire_app(
    redis: Redis, settings: Settings, deps_factory: Callable[[], PipelineDeps]
) -> FastAPI:
    """Build the app on the live Redis, with the pipeline's outbound seams faked.

    ``app/routes/check.py`` spawns ``run_pipeline`` without dependencies, which
    would make it build production ones from settings and demand an
    ``OPENAI_API_KEY``. Injecting them here is the same seam the suite uses; the
    route, the pipeline and Redis are all the real thing.
    """

    async def run_with_fakes(
        redis: Redis,
        job_id: str,
        request: CheckRequest,
        *,
        settings: Settings,
        deps: PipelineDeps | None = None,
    ) -> None:
        await run_pipeline(redis, job_id, request, settings=settings, deps=deps_factory())

    check_routes.run_pipeline = run_with_fakes
    application = create_app()
    application.state.redis = redis
    application.dependency_overrides[get_redis] = lambda: redis
    application.dependency_overrides[get_settings] = lambda: settings
    return application


# ---------------------------------------------------------------- scenarios


async def scenario_happy_path(redis: Redis) -> None:
    """The whole pipeline onto live Redis: sequence, ordering, counts, cache."""
    heading("1. run_pipeline over the fixture article, onto real redis")
    await flush(redis)
    request = check_request()
    job_id = f"smoke-happy-{uuid4()}"
    deps, transport, search = make_deps()
    settings = pipeline_settings()

    # What the job had already published at the moment it wrote the cache. Read
    # from the live replay list inside the real `set_check`, so it is an
    # observation of the running pipeline rather than a guess: a subscriber
    # cannot answer this, because pub/sub messages sit in a buffer and are read
    # some time after they were sent.
    events_at_cache_write: list[str] = []
    real_set_check = run_module.set_check

    async def recording_set_check(client: Redis, url: str, result: dict[str, Any]) -> None:
        events_at_cache_write.extend(
            record["event"] for record in await stored_events(client, job_id)
        )
        await real_set_check(client, url, result)

    run_module.set_check = recording_set_check  # type: ignore[assignment]

    watcher = Subscriber(redis, job_id, str(request.url))
    collector = asyncio.create_task(watcher.collect())
    await asyncio.sleep(0.05)  # let SUBSCRIBE land before the first publish
    try:
        await run_pipeline(redis, job_id, request, settings=settings, deps=deps)
    finally:
        run_module.set_check = real_set_check
    records = await collector

    names = [record["event"] for record in records]
    found = events_named(records, "claims_found")
    claims = events_named(records, "claim")
    done = events_named(records, "done")

    print(f"  event sequence: {names}")
    check("claims_found is first", names[0] == "claims_found")
    check("done is last", names[-1] == "done")
    check(
        "exactly one claims_found, one done, no error",
        len(found) == 1 and len(done) == 1 and "error" not in names,
    )
    check(
        "every event between them is a claim",
        set(names[1:-1]) == {"claim"},
        f"{len(claims)} claim events",
    )

    announced = found[0]["claim_ids"]
    print(f"  claims_found: count={found[0]['count']} claim_ids={announced}")
    check("count equals the number of ids", found[0]["count"] == len(announced))
    check(
        "claim_ids are in article order (c1..cN, ascending by start)",
        announced == sorted(announced, key=lambda cid: int(cid[1:])),
        str(announced),
    )
    check(
        "one claim event per announced id, no duplicates",
        sorted(claim["id"] for claim in claims) == sorted(announced),
    )
    starts = {claim["id"]: claim["start"] for claim in claims}
    check(
        "announced order really is ascending start offset",
        [starts[cid] for cid in announced] == sorted(starts[cid] for cid in announced),
        str([starts[cid] for cid in announced]),
    )

    arrival = [claim["id"] for claim in claims]
    print(f"  arrival order:  {arrival}")
    print(f"  article order:  {announced}")
    check(
        "claims arrive as they resolve (order need not match, and is allowed to)",
        sorted(arrival) == sorted(announced),
    )

    tally: dict[str, int] = {}
    for claim in claims:
        tally[claim["verdict"]] = tally.get(claim["verdict"], 0) + 1
    print(f"  done.counts:    {done[0]['counts']}")
    check(
        "done.counts match the claims actually published",
        {verdict: count for verdict, count in done[0]["counts"].items() if count}
        == {verdict: count for verdict, count in tally.items() if count},
        f"published {tally}",
    )
    check("done carries checked_at", bool(done[0].get("checked_at")), done[0].get("checked_at", ""))

    print(f"  events already published when the cache was written: {events_at_cache_write}")
    check(
        "the cache is written after every claim and before done",
        events_at_cache_write == ["claims_found"] + ["claim"] * len(claims),
    )
    check(
        "done had not been published yet when the cache was written",
        "done" not in events_at_cache_write,
    )
    check(
        "the cache exists by the time done reaches a live subscriber",
        records[-1]["event"] == "done" and records[-1]["cached_now"] is True,
    )

    stored = json.loads(str(await redis.get(cache_key(str(request.url)))))
    check(
        "the cached claims are the published claims, in article order",
        [claim["id"] for claim in stored["claims"]] == announced,
    )
    check("the cached counts equal the done counts", stored["counts"] == done[0]["counts"])
    ttl = await redis.ttl(cache_key(str(request.url)))
    check("the cache entry has the 7-day TTL", 7 * 24 * 3600 - 60 < ttl <= 7 * 24 * 3600, f"{ttl}s")

    replay = await redis.lrange(EVENTS_KEY.format(job_id=job_id), 0, -1)
    check(
        "the replay list holds the same events in the same order",
        [json.loads(item)["event"] for item in replay] == names,
    )

    heading("1b. the cost guarantees, measured on that run")
    print(f"  transport calls: {[name for name, _ in transport.calls]}")
    print(f"  searches:        {len(search.queries)}")
    extraction = transport.count("ExtractionResponse")
    stance = transport.count("StanceResponse")
    judge = transport.count("JudgeResponse")
    no_evidence = [claim for claim in claims if NO_EVIDENCE_QUOTE in claim["quote"]]
    check("exactly one extraction call per article", extraction == 1, f"{extraction}")
    check(
        "one stance call per claim that had passages",
        stance == len(claims) - len(no_evidence),
        f"{stance} stance calls for {len(claims)} claims, {len(no_evidence)} with no passages",
    )
    check(
        "one judge call per claim that had passages",
        judge == len(claims) - len(no_evidence),
        f"{judge}",
    )
    check(
        "a claim with no passages costs no model call at all",
        len(no_evidence) == 1 and no_evidence[0]["verdict"] == "unverifiable",
        f"{[claim['id'] for claim in no_evidence]}",
    )
    check(
        "no claim carries more than max_passages_per_claim sources",
        all(len(claim["sources"]) <= settings.max_passages_per_claim for claim in claims),
        f"max sources on a claim: {max(len(claim['sources']) for claim in claims)}",
    )
    check(
        "no more than max_claims claims were checked",
        len(claims) <= settings.max_claims,
        f"{len(claims)} <= {settings.max_claims}",
    )
    print(
        f"  metered bill: calls={deps.meter.calls} tokens={deps.meter.total_tokens}"
        if deps.meter
        else "  unmetered"
    )


async def scenario_route_and_cache(redis: Redis) -> None:
    """``POST /check`` twice through the real route: miss, then cache hit."""
    heading("2. the route: a real check, then the same URL from the cache")
    await flush(redis)
    settings = pipeline_settings(daily_cap=20, mock_step_delay=0.0)
    application = wire_app(redis, settings, lambda: make_deps()[0])
    body = json.loads(check_request().model_dump_json())

    async with make_client(application) as client:
        first = await client.post("/check", json=body)
        check("first POST is 200", first.status_code == 200, str(first.status_code))
        first_job = first.json()
        print(f"  first  POST -> {first_job}")
        check("first POST is a cache miss", first_job["cached"] is False)
        check("a miss reports no claim count yet", first_job["claim_count"] is None)

        stream = await read_stream(client, first_job["job_id"])
        names = [name for name, _ in stream]
        print(f"  first  stream: {names}")
        check("the live stream ends with done", names[-1] == "done")
        live_claims = [data for name, data in stream if name == "claim"]

        second = await client.post("/check", json=body)
        second_job = second.json()
        print(f"  second POST -> {second_job}")
        check("second POST is a cache hit", second_job["cached"] is True)
        check(
            "the hit reports the claim count up front",
            second_job["claim_count"] == len(live_claims),
            f"{second_job['claim_count']} vs {len(live_claims)}",
        )

        replay = await read_stream(client, second_job["job_id"])
        replay_names = [name for name, _ in replay]
        print(f"  second stream: {replay_names}")
        check("the cached path streams the same event shape", replay_names == names)
        replay_claims = [data for name, data in replay if name == "claim"]
        check(
            "the cached claims are identical to the live ones",
            sorted(replay_claims, key=lambda claim: claim["id"])
            == sorted(live_claims, key=lambda claim: claim["id"]),
        )
        replay_found = next(data for name, data in replay if name == "claims_found")
        live_found = next(data for name, data in stream if name == "claims_found")
        check(
            "the cached path announces the same claim_ids in the same order",
            replay_found["claim_ids"] == live_found["claim_ids"],
        )


async def read_stream(client: httpx.AsyncClient, job_id: str) -> list[tuple[str, Any]]:
    """Read one SSE stream to its terminal event, as ``(event, data)`` pairs."""
    events: list[tuple[str, Any]] = []
    name = ""
    async with client.stream("GET", f"/check/{job_id}/stream", timeout=60.0) as response:
        response.raise_for_status()
        async for line in response.aiter_lines():
            if line.startswith("event: "):
                name = line[len("event: ") :]
            elif line.startswith("data: "):
                events.append((name, json.loads(line[len("data: ") :])))
                if name in {"done", "error"}:
                    break
    return events


async def scenario_one_failing_claim(redis: Redis) -> None:
    """A claim whose check raises is published unverifiable; the run completes."""
    heading("3. one failing claim still yields a complete run")
    await flush(redis)
    request = check_request()
    job_id = f"smoke-fail-{uuid4()}"
    deps, transport, _ = make_deps(
        fail_for={"200 stalls": LLMBadRequest("model refused this claim (scripted)")}
    )
    await run_pipeline(redis, job_id, request, settings=pipeline_settings(), deps=deps)

    records = await stored_events(redis, job_id)
    names = [record["event"] for record in records]
    claims = events_named(records, "claim")
    found = events_named(records, "claims_found")
    print(f"  event sequence: {names}")
    check("the run still ends with done, not error", names[-1] == "done")
    check(
        "every announced claim was still published",
        sorted(claim["id"] for claim in claims) == sorted(found[0]["claim_ids"]),
    )
    failed = [claim for claim in claims if claim["evidence"] == FAILED_CLAIM_EVIDENCE]
    print(f"  failed claim: {[claim['id'] for claim in failed]}")
    check("exactly one claim carries the failed-check evidence", len(failed) == 1)
    check("it is unverifiable", bool(failed) and failed[0]["verdict"] == "unverifiable")
    check("it carries no confidence", bool(failed) and failed[0]["confidence"] is None)
    check("it carries no sources", bool(failed) and failed[0]["sources"] == [])
    check("it still carries a provenance trail", bool(failed and failed[0]["trail"]))
    check(
        "the run containing a failed claim was NOT cached",
        not await redis.exists(cache_key(str(request.url))),
    )
    print(f"  transport calls: {[name for name, _ in transport.calls]}")


async def scenario_zero_claims(redis: Redis) -> None:
    """An article extraction finds nothing in ends cleanly and instantly."""
    heading("4. zero extracted claims: claims_found(0) then done, no hang")
    await flush(redis)
    request = check_request()
    job_id = f"smoke-empty-{uuid4()}"
    deps, transport, search = make_deps(answers={"ExtractionResponse": {"claims": []}})
    await asyncio.wait_for(
        run_pipeline(redis, job_id, request, settings=pipeline_settings(), deps=deps),
        timeout=10.0,
    )
    records = await stored_events(redis, job_id)
    names = [record["event"] for record in records]
    found = events_named(records, "claims_found")
    done = events_named(records, "done")
    print(f"  event sequence: {names}")
    print(f"  claims_found:   {found[0]}")
    print(f"  done:           {done[0]['counts']}")
    check("exactly two events: claims_found then done", names == ["claims_found", "done"])
    check("claims_found reports zero", found[0]["count"] == 0 and found[0]["claim_ids"] == [])
    check("every count is zero", set(done[0]["counts"].values()) == {0})
    check("no search was made", not search.queries, str(search.queries))
    check(
        "only the one extraction call was paid for",
        [name for name, _ in transport.calls] == ["ExtractionResponse"],
    )
    check("an empty result is still cached", bool(await redis.exists(cache_key(str(request.url)))))


async def scenario_factcheck_short_circuit(redis: Redis) -> None:
    """A ClaimReview hit must cost zero web searches — the dominant per-claim bill."""
    heading("5. a Google Fact Check hit short-circuits web search")
    await flush(redis)
    from app.pipeline.providers import NullCitedSourceProvider, NullPassageProvider, Providers
    from app.pipeline.types import Passage

    class FakeFactCheck:
        """Answers every claim, so no claim should ever reach web search."""

        def __init__(self) -> None:
            self.calls = 0

        async def search(self, query: str, *, limit: int) -> list[Passage]:
            self.calls += 1
            return [
                Passage(
                    text=(
                        "The board's release puts the median adjustment at 4 per cent "
                        "from 1 January, not 40 per cent."
                    ),
                    url="https://factcheck.example.test/hawker-rents",
                    outlet="Example Fact Check",
                    date="2026-03-12",
                    wire=False,
                    origin="factcheck",
                    rating="False",
                )
            ][:limit]

    factcheck = FakeFactCheck()
    search = FakeSearch()
    deps, transport, _ = make_deps(
        search=search,
        answers={
            "ExtractionResponse": extraction_answer(),
            # Keyed to the ClaimReview passage above, so this scenario proves a
            # real verdict comes out of the short-circuit rather than only that
            # web search went unpaid.
            "StanceResponse": {
                "scores": [
                    {
                        "index": 1,
                        "stance": "refutes",
                        "quote": "the median adjustment at 4 per cent",
                    }
                ]
            },
            "JudgeResponse": {
                "verdict": "contradicted",
                "confidence": "high",
                "evidence": (
                    "Example Fact Check puts the median adjustment at 4 per cent "
                    "from 1 January, not 40 per cent."
                ),
                "cited_spans": ["the median adjustment at 4 per cent from 1 January"],
            },
        },
    )
    deps = PipelineDeps(
        llm=deps.llm,
        providers=Providers(
            factcheck=factcheck,
            search=search,
            official=NullPassageProvider(reason="smoke"),
            cited=NullCitedSourceProvider(reason="smoke"),
            timeout_seconds=5.0,
        ),
        meter=deps.meter,
    )
    request = check_request()
    job_id = f"smoke-fc-{uuid4()}"
    await run_pipeline(redis, job_id, request, settings=pipeline_settings(), deps=deps)
    records = await stored_events(redis, job_id)
    claims = events_named(records, "claim")
    print(
        f"  claims: {len(claims)}  factcheck calls: {factcheck.calls}  "
        f"web searches: {len(search.queries)}"
    )
    check("the fact-check provider was asked for every claim", factcheck.calls == len(claims))
    check("web search was never called", not search.queries, str(search.queries))
    check(
        "stance was still one call per claim",
        transport.count("StanceResponse") == len(claims),
        f"{transport.count('StanceResponse')} for {len(claims)} claims",
    )
    verdicts = sorted({claim["verdict"] for claim in claims})
    print(f"  verdicts from the ClaimReview evidence: {verdicts}")
    check(
        "the short-circuit still produces a decided verdict, not an abstention",
        verdicts == ["contradicted"],
        str(verdicts),
    )
    check(
        "each decided claim ships a source and an evidence sentence naming it",
        all(claim["sources"] and "Example Fact Check" in claim["evidence"] for claim in claims),
    )


async def scenario_many_passages(redis: Redis) -> None:
    """Twenty available passages must still cost one stance call and six sources."""
    heading("6. passages are capped, and stance is one call however many there are")
    await flush(redis)
    from app.pipeline.types import Passage

    class FloodSearch(FakeSearch):
        """Offers far more passages than the cap allows."""

        async def search(self, query: str, *, limit: int) -> list[Passage]:
            self.queries.append(query)
            await asyncio.sleep(0)
            return [
                Passage(
                    text=(
                        f"Report {index}: the new rents take effect on 1 January, "
                        "the board confirmed."
                    ),
                    url=f"https://outlet{index}.example.test/hawker-rents",
                    outlet=f"Outlet {index}",
                    date="2026-03-12",
                    wire=False,
                    origin="web",
                    rating=None,
                )
                for index in range(20)
            ][:limit]

    flood = FloodSearch()
    settings = pipeline_settings()
    stance_answer = {
        "scores": [
            {"index": index + 1, "stance": "supports", "quote": "the new rents take effect"}
            for index in range(settings.max_passages_per_claim)
        ]
    }
    judge_answer = {
        "verdict": "supported",
        "confidence": "high",
        "evidence": "Outlet 0 and Outlet 1 both report the new rents take effect on 1 January.",
        "cited_spans": ["the new rents take effect on 1 January"],
    }
    deps, transport, _ = make_deps(
        search=flood,
        answers={
            "ExtractionResponse": extraction_answer(),
            "StanceResponse": stance_answer,
            "JudgeResponse": judge_answer,
        },
    )
    request = check_request()
    job_id = f"smoke-flood-{uuid4()}"
    await run_pipeline(redis, job_id, request, settings=settings, deps=deps)
    records = await stored_events(redis, job_id)
    claims = events_named(records, "claim")
    widest = max(len(claim["sources"]) for claim in claims)
    print(
        f"  claims: {len(claims)}  widest source list: {widest}  "
        f"stance calls: {transport.count('StanceResponse')}"
    )
    check(
        "no claim exceeds max_passages_per_claim sources",
        widest <= settings.max_passages_per_claim,
        f"{widest} <= {settings.max_passages_per_claim}",
    )
    check(
        "stance is still exactly one call per claim",
        transport.count("StanceResponse") == len(claims),
    )
    check(
        "judge is still exactly one call per claim",
        transport.count("JudgeResponse") == len(claims),
    )


async def scenario_max_claims(redis: Redis) -> None:
    """``max_claims`` bounds what an article can cost, however many it contains."""
    heading("7. max_claims caps the bill")
    await flush(redis)
    settings = pipeline_settings(max_claims=3)
    deps, transport, search = make_deps()
    request = check_request()
    job_id = f"smoke-cap-{uuid4()}"
    await run_pipeline(redis, job_id, request, settings=settings, deps=deps)
    records = await stored_events(redis, job_id)
    claims = events_named(records, "claim")
    found = events_named(records, "claims_found")
    print(f"  MAX_CLAIMS=3 -> claims={len(claims)} searches={len(search.queries)}")
    check("only max_claims claims were announced", found[0]["count"] == 3, str(found[0]["count"]))
    check("only max_claims claims were published", len(claims) == 3)
    check("only max_claims claims were searched for", len(search.queries) == 3)
    check("still exactly one extraction call", transport.count("ExtractionResponse") == 1)


async def scenario_milestone_one_guards(redis: Redis) -> None:
    """The daily cap and the unknown-job 404, on live Redis."""
    heading("8. milestone 1 still holds: daily cap and unknown job")
    await flush(redis)
    settings = pipeline_settings(daily_cap=2)
    application = wire_app(redis, settings, lambda: make_deps()[0])

    async with make_client(application) as client:
        statuses: list[int] = []
        for index in range(3):
            body = json.loads(check_request().model_dump_json())
            body["url"] = f"https://example.com/re-vera-fixture/fictional-news/cap-{index}"
            response = await client.post("/check", json=body)
            statuses.append(response.status_code)
            if response.status_code == 429:
                detail = response.json()["detail"]
                print(f"  429 detail: {detail}")
                check("the 429 carries the daily_limit code", detail["code"] == "daily_limit")
                check("the 429 carries a reader-facing message", bool(detail["message"]))
        print(f"  POST statuses with DAILY_CAP=2: {statuses}")
        check("the third check past a cap of 2 is refused", statuses == [200, 200, 429])

        unknown = await client.get(f"/check/{uuid4()}/stream")
        print(f"  unknown job -> {unknown.status_code} {unknown.json()}")
        check("an unknown job id is a 404", unknown.status_code == 404, str(unknown.status_code))
        check(
            "the 404 carries the unknown_job code",
            unknown.json()["detail"]["code"] == "unknown_job",
        )


# ---------------------------------------------------------------- entry point


async def main(redis_url: str) -> int:
    """Run every scenario against ``redis_url`` and report."""
    client: Redis = redis_asyncio.from_url(redis_url, decode_responses=True)
    print(f"connected to {redis_url}: {await client.ping()}")
    info = await client.info("server")
    print(f"redis_version={info['redis_version']} tcp_port={info['tcp_port']}")
    try:
        await scenario_happy_path(client)
        await scenario_route_and_cache(client)
        await scenario_one_failing_claim(client)
        await scenario_zero_claims(client)
        await scenario_factcheck_short_circuit(client)
        await scenario_many_passages(client)
        await scenario_max_claims(client)
        await scenario_milestone_one_guards(client)
    finally:
        await flush(client)
        await client.aclose()

    heading("result")
    if failures:
        print(f"{len(failures)} CHECK(S) FAILED:")
        for label in failures:
            print(f"  - {label}")
        return 1
    print("all checks passed")
    return 0


def _parse(argv: list[str] | None) -> argparse.Namespace:
    """Parse the command line."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--redis-url", default=DEFAULT_REDIS_URL)
    return parser.parse_args(argv)


if __name__ == "__main__":
    sys.exit(asyncio.run(main(_parse(None).redis_url)))
