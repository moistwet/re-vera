"""The real pipeline behind the real route, and the cost caps end to end.

:mod:`tests.test_pipeline_run` pins the orchestrator's contract by calling
:func:`~app.pipeline.run.run_pipeline` directly; :mod:`tests.test_check_flow`
pins the route against the milestone-1 mock. This file joins the two: it drives
``POST /check`` and the SSE stream with ``USE_MOCK_PIPELINE`` **off**, so the
five real stages run under the real route, and it measures the per-claim cost
rules on that whole path rather than on one stage.

Both outbound seams are faked exactly as in ``tests/test_pipeline_run.py`` —
the same :class:`~tests.test_pipeline_run.ScriptedTransport` under the LLM
client and the same fake providers under retrieval — so nothing here opens a
socket or needs a key. Redis is ``fakeredis``, which keeps the suite hermetic;
``backend/scripts/live_redis_smoke.py`` runs these same scenarios against a real
``redis-server`` for the things a fake cannot promise (the Lua publish script,
pub/sub ordering, TTLs).

The route builds its dependencies from settings, which would demand an
``OPENAI_API_KEY``. :func:`real_pipeline_app` injects the fakes at that seam and
changes nothing else: the route, the pipeline, the cache and the event fan-out
are all the shipped code.

Everything quoted here is fictional, like the rest of ``tests/fixtures/``.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fakeredis.aioredis import FakeRedis
from fastapi import FastAPI
from redis.asyncio import Redis

import app.pipeline.run as run_module
import app.routes.check as check_routes
from app.config import Settings, get_settings
from app.main import create_app
from app.pipeline.providers import (
    NullCitedSourceProvider,
    NullPassageProvider,
    Providers,
)
from app.pipeline.run import PipelineDeps, run_pipeline
from app.pipeline.types import Passage
from app.routes.check import get_redis
from app.schema_models import CheckRequest

from .conftest import BASE_URL
from .test_pipeline_run import (
    JOB_ID,
    FakeSearch,
    ScriptedTransport,
    check_request,
    events_of,
    extraction_answer,
    make_deps,
    pipeline_settings,
    published,
)

INSTALL_ID = "11111111-2222-3333-4444-555555555555"


# ---------------------------------------------------------------- the route seam


def real_pipeline_app(
    redis: Redis,
    settings: Settings,
    deps: PipelineDeps,
    monkeypatch: pytest.MonkeyPatch,
) -> FastAPI:
    """The application, wired to ``redis``, running the **real** pipeline on fakes.

    ``app/routes/check.py`` spawns ``run_pipeline(redis, job_id, payload,
    settings=settings)`` with no dependencies, which makes it build production
    ones and demand a key. Patching the name the route resolves — rather than
    the route, the pipeline or any stage — injects the offline seam and leaves
    every line of shipped logic in the path.
    """

    async def run_with_fakes(
        client: Redis, job_id: str, request: CheckRequest, *, settings: Settings
    ) -> None:
        await run_pipeline(client, job_id, request, settings=settings, deps=deps)

    monkeypatch.setattr(check_routes, "run_pipeline", run_with_fakes)
    application = create_app()
    application.state.redis = redis
    application.dependency_overrides[get_redis] = lambda: redis
    application.dependency_overrides[get_settings] = lambda: settings
    return application


async def http_client(application: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """An in-process client for ``application``."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url=BASE_URL
    ) as client:
        yield client


async def read_stream(client: httpx.AsyncClient, job_id: str) -> list[tuple[str, Any]]:
    """Read one SSE stream to its terminal event, as ``(event, data)`` pairs."""
    events: list[tuple[str, Any]] = []
    name = ""
    async with client.stream("GET", f"/check/{job_id}/stream", timeout=30.0) as response:
        assert response.status_code == 200
        async for line in response.aiter_lines():
            if line.startswith("event: "):
                name = line.removeprefix("event: ")
            elif line.startswith("data: "):
                events.append((name, json.loads(line.removeprefix("data: "))))
                if name in {"done", "error"}:
                    break
    return events


def request_body(**overrides: Any) -> dict[str, Any]:
    """A ``POST /check`` body for the fictional fixture article."""
    body: dict[str, Any] = json.loads(check_request().model_dump_json())
    body["install_id"] = INSTALL_ID
    body.update(overrides)
    return body


# ------------------------------------------------- the route runs the real pipeline


async def test_the_route_streams_a_real_check_and_then_serves_it_from_the_cache(
    fake_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One article, checked once and replayed thereafter — through the real route.

    The path the reader actually takes, with the real five stages behind it: a
    miss returns ``cached: false`` and no claim count, the stream delivers
    ``claims_found`` → every claim → ``done``, and the second check of the same
    URL is served from the 7-day cache with the same claims, the same ids and
    the same order. The cached path rendering identically to the live one is the
    whole reason ``claims_found`` carries ``claim_ids`` (``docs/decisions.md``
    §15), and it is only really tested where both paths exist.
    """
    deps, transport, _ = make_deps()
    settings = pipeline_settings(daily_cap=20)
    application = real_pipeline_app(fake_redis, settings, deps, monkeypatch)

    async for client in http_client(application):
        first = (await client.post("/check", json=request_body())).json()
        assert first["cached"] is False
        assert first["claim_count"] is None

        live = await read_stream(client, first["job_id"])
        names = [name for name, _ in live]
        assert names[0] == "claims_found"
        assert names[-1] == "done"
        assert set(names[1:-1]) == {"claim"}
        live_claims = [data for name, data in live if name == "claim"]
        live_found = next(data for name, data in live if name == "claims_found")
        assert sorted(claim["id"] for claim in live_claims) == sorted(live_found["claim_ids"])

        second = (await client.post("/check", json=request_body())).json()
        assert second["cached"] is True
        assert second["claim_count"] == len(live_claims)

        replay = await read_stream(client, second["job_id"])
        replay_found = next(data for name, data in replay if name == "claims_found")
        replay_claims = [data for name, data in replay if name == "claim"]
        assert replay_found["claim_ids"] == live_found["claim_ids"]
        assert sorted(replay_claims, key=lambda claim: claim["id"]) == sorted(
            live_claims, key=lambda claim: claim["id"]
        )

    # The replay cost nothing: the transport saw one article's worth of calls.
    assert transport.count("ExtractionResponse") == 1


async def test_a_cached_article_is_never_checked_twice(
    fake_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The 7-day cache is a cost control, so a hit must reach no model at all.

    Asserted on the transport rather than on the response body: ``cached: true``
    only says what the route believed, while a call count says what was
    actually paid for.
    """
    deps, transport, search = make_deps()
    application = real_pipeline_app(fake_redis, pipeline_settings(), deps, monkeypatch)

    async for client in http_client(application):
        first = (await client.post("/check", json=request_body())).json()
        await read_stream(client, first["job_id"])
        after_first = len(transport.calls)
        searches_after_first = len(search.queries)

        for _ in range(3):
            job = (await client.post("/check", json=request_body())).json()
            assert job["cached"] is True
            await read_stream(client, job["job_id"])

    assert len(transport.calls) == after_first
    assert len(search.queries) == searches_after_first


# ---------------------------------------------------------------- the cost caps


class FloodSearch(FakeSearch):
    """A search provider offering far more passages than the cap allows.

    Retrieval asks for a bounded number and must keep no more than
    ``max_passages_per_claim``; this is the provider that would break that if it
    were not enforced.
    """

    async def search(self, query: str, *, limit: int) -> list[Passage]:
        """Return twenty distinct fictional passages, truncated to ``limit``."""
        self.queries.append(query)
        await asyncio.sleep(0)
        return [
            Passage(
                text=f"Report {index}: the new rents take effect on 1 January, the board said.",
                url=f"https://outlet{index}.example.test/hawker-rents",
                outlet=f"Outlet {index}",
                date="2026-03-12",
                wire=False,
                origin="web",
                rating=None,
            )
            for index in range(20)
        ][:limit]


def flooded_deps(settings: Settings) -> tuple[PipelineDeps, ScriptedTransport, FloodSearch]:
    """Dependencies whose search offers twenty passages per claim."""
    flood = FloodSearch()
    stance = {
        "scores": [
            {"index": index + 1, "stance": "supports", "quote": "the new rents take effect"}
            for index in range(settings.max_passages_per_claim)
        ]
    }
    judge = {
        "verdict": "supported",
        "confidence": "high",
        "evidence": "Outlet 0 and Outlet 1 both report the new rents take effect on 1 January.",
        "cited_spans": ["the new rents take effect on 1 January"],
    }
    deps, transport, _ = make_deps(
        search=flood,
        answers={
            "ExtractionResponse": extraction_answer(),
            "StanceResponse": stance,
            "JudgeResponse": judge,
        },
    )
    return deps, transport, flood


async def test_a_flood_of_passages_is_capped_before_anything_is_paid_for(
    fake_redis: FakeRedis,
) -> None:
    """Twenty available passages per claim still cost one batch and six sources.

    The cap is the per-claim cost control: passages are what stage 3's prompt is
    made of, so an uncapped retrieval multiplies the token bill by however many
    results the web happened to return. It is also what a reader sees — the
    source chips on the claim card come from the same list.
    """
    settings = pipeline_settings()
    deps, transport, flood = flooded_deps(settings)
    await run_pipeline(fake_redis, JOB_ID, check_request(), settings=settings, deps=deps)

    claims = events_of(await published(fake_redis), "claim")
    assert claims
    assert max(len(claim["sources"]) for claim in claims) <= settings.max_passages_per_claim
    assert all(claim["sources"] for claim in claims)
    # One stance call and one judge call per claim, however wide the evidence.
    assert transport.count("StanceResponse") == len(claims)
    assert transport.count("JudgeResponse") == len(claims)
    assert transport.count("ExtractionResponse") == 1
    assert len(flood.queries) == len(claims)


async def test_lowering_the_passage_cap_lowers_what_a_claim_costs(
    fake_redis: FakeRedis,
) -> None:
    """``MAX_PASSAGES_PER_CLAIM`` is honoured end to end, not just in retrieval.

    A separate test from the one above because a cap that is merely *not
    exceeded* could be an accident of how much evidence the fake offers. Turning
    the knob down and watching the source lists follow proves the setting is
    what bounds them.
    """
    settings = pipeline_settings(max_passages_per_claim=2)
    deps, transport, _ = flooded_deps(settings)
    await run_pipeline(fake_redis, JOB_ID, check_request(), settings=settings, deps=deps)

    claims = events_of(await published(fake_redis), "claim")
    assert claims
    assert max(len(claim["sources"]) for claim in claims) <= 2
    assert transport.count("StanceResponse") == len(claims)


class OneHitFactCheck:
    """A :class:`~app.pipeline.providers.FactCheckProvider` that answers every claim.

    Every claim getting a ClaimReview hit means no claim has any business
    reaching web search — the short-circuit that ``docs/decisions.md`` §9 exists
    for, since search is the dominant per-claim cost.
    """

    def __init__(self) -> None:
        self.calls = 0

    async def search(self, query: str, *, limit: int) -> list[Passage]:
        """Return one fictional fact-check passage, and count the call."""
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


async def test_a_claimreview_hit_costs_no_web_search_anywhere_in_the_pipeline(
    fake_redis: FakeRedis,
) -> None:
    """A fact-check hit short-circuits web search for that claim, end to end.

    ``tests/test_retrieve.py`` pins this inside stage 2. Pinned again here on the
    whole run, because the cost rule is about what an *article* costs: a future
    change that re-queried search from the orchestrator, or supplemented a
    fact-checked claim "just to be safe", would leave stage 2's test green.

    The verdict is asserted too, so this stays a test about a working
    short-circuit rather than one about a claim that failed early enough to skip
    the search.
    """
    factcheck = OneHitFactCheck()
    search = FakeSearch()
    base, transport, _ = make_deps(
        search=search,
        answers={
            "ExtractionResponse": extraction_answer(),
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
        llm=base.llm,
        providers=Providers(
            factcheck=factcheck,
            search=search,
            official=NullPassageProvider(reason="no official-data provider in tests"),
            cited=NullCitedSourceProvider(reason="no cited-source provider in tests"),
            timeout_seconds=5.0,
        ),
        meter=base.meter,
    )
    settings = pipeline_settings()
    await run_pipeline(fake_redis, JOB_ID, check_request(), settings=settings, deps=deps)

    claims = events_of(await published(fake_redis), "claim")
    assert claims
    assert factcheck.calls == len(claims)
    assert search.queries == []
    assert {claim["verdict"] for claim in claims} == {"contradicted"}
    assert all("Example Fact Check" in claim["evidence"] for claim in claims)
    assert transport.count("StanceResponse") == len(claims)


# ---------------------------------------------------------- what a whole run costs


async def test_one_article_costs_one_extraction_and_two_calls_per_evidenced_claim(
    fake_redis: FakeRedis,
) -> None:
    """The whole per-article bill, stated as one arithmetic identity.

    Every cost rule that governs the number of LLM calls, in one place and on one
    run: one extraction for the article, then a stance call and a judge call for
    each claim that actually had evidence — and nothing at all for a claim the
    web had nothing on. If a change adds a call anywhere, this is the assertion
    that names it.
    """
    settings = pipeline_settings()
    deps, transport, search = make_deps()
    await run_pipeline(fake_redis, JOB_ID, check_request(), settings=settings, deps=deps)

    claims = events_of(await published(fake_redis), "claim")
    evidenced = [claim for claim in claims if claim["sources"]]
    unevidenced = [claim for claim in claims if not claim["sources"]]

    assert len(claims) <= settings.max_claims
    assert len(search.queries) == len(claims)
    assert transport.count("ExtractionResponse") == 1
    assert transport.count("StanceResponse") == len(evidenced)
    assert transport.count("JudgeResponse") == len(evidenced)
    assert len(transport.calls) == 1 + 2 * len(evidenced)
    assert unevidenced, "the fake search leaves one claim without evidence on purpose"
    assert all(claim["verdict"] == "unverifiable" for claim in unevidenced)
    assert deps.meter is not None
    assert deps.meter.calls == len(transport.calls)


async def test_run_pipeline_still_goes_through_check_claim(
    fake_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``eval/run_eval.py`` scores :func:`~app.pipeline.run.check_claim` directly.

    That is the right seam — it means the harness measures the shipped per-claim
    path rather than a copy of it — but it is a coupling nothing else guards. If
    a later change made ``run_pipeline`` bypass it (a caching shortcut, a
    batching rewrite), every eval number would quietly describe a function the
    service no longer calls, and no test in either tree would notice.

    Asserted by counting, not by replacing: the real ``check_claim`` still runs,
    so this stays a test about the call graph rather than a second mock of the
    pipeline.
    """
    seen: list[str] = []
    real_check_claim = run_module.check_claim

    async def counting_check_claim(
        claim: Any, *, article_url: str, settings: Settings, deps: PipelineDeps
    ) -> dict[str, Any]:
        seen.append(claim.id)
        result: dict[str, Any] = await real_check_claim(
            claim, article_url=article_url, settings=settings, deps=deps
        )
        return result

    monkeypatch.setattr(run_module, "check_claim", counting_check_claim)
    deps, _, _ = make_deps()
    await run_pipeline(fake_redis, JOB_ID, check_request(), settings=pipeline_settings(), deps=deps)

    claims = events_of(await published(fake_redis), "claim")
    assert claims
    assert sorted(seen) == sorted(claim["id"] for claim in claims)
