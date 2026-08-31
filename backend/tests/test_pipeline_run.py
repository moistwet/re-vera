"""The orchestrator: the event contract, end to end, entirely offline.

Every test here runs the *whole* real pipeline — extraction, retrieval, stance,
judge, aggregation — with both of its outbound seams faked: a scripted
:class:`~app.llm.LLMTransport` under the LLM client and fake providers behind
retrieval. Nothing opens a socket, and nothing needs a key. That is not a
convenience: this repository has no ``OPENAI_API_KEY``, no
``GOOGLE_FACTCHECK_API_KEY`` and no route to either service, so the injectable
:class:`~app.pipeline.run.PipelineDeps` is the only way any of this can be
exercised at all. **No live API call was made anywhere in this milestone**, so
nothing here is evidence about how a real model behaves — only about what the
orchestrator does with an answer.

What is pinned here is the contract every pipeline owes the stream and that the
milestone-1 popup was written against: ``claims_found`` first, carrying every id
in **article** order; one ``claim`` per claim as it resolves, in whatever order
they finish; the result in the 7-day cache; then ``done``. Or ``error``, so a
reader is never left on a stream that will not end.

The article, its claims and every outlet, URL and figure in the fake passages are
fictional, like everything under ``tests/fixtures/``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from fakeredis.aioredis import FakeRedis

import app.pipeline.run as run_module
from app.cache import get_check
from app.config import Settings
from app.events import EVENTS_KEY
from app.invariants import validate_claim
from app.llm import (
    LLMBadRequest,
    LLMClient,
    LLMResponse,
    LLMUnavailable,
)
from app.pipeline.providers import (
    NullCitedSourceProvider,
    NullPassageProvider,
    Providers,
)
from app.pipeline.run import (
    ERROR_CODE,
    FAILED_CLAIM_EVIDENCE,
    LLMMeter,
    PipelineDeps,
    run_pipeline,
)
from app.pipeline.types import Passage
from app.routes.check import (
    pipeline_for,
    real_pipeline_budget_seconds,
    stream_deadline_seconds,
)
from app.schema_models import CheckRequest, Claim, ClaimsFoundEvent, DoneEvent, ErrorEvent

from .conftest import build_settings

FIXTURES = Path(__file__).parent / "fixtures"

JOB_ID = "job-under-test"

EXPECTED_CLAIM_IDS = ["c1", "c2", "c3", "c4", "c5", "c6", "c7"]
"""What ``tests/fixtures/extract/hawker_claims.json`` yields against the fixture
article, in article order — the ids ``claims_found`` must announce."""

NO_EVIDENCE_QUOTE = "eight in ten"
"""The claim the fake search provider finds nothing for.

One claim per run with no passages, so every test also covers the honest
abstention path — and the cost rule that goes with it: no passages means no
stance call and no judge call.
"""


# ---------------------------------------------------------------- fake evidence


OFFICIAL_PASSAGE = Passage(
    text=(
        "The revised rents take effect on 1 January, the board said in its release. "
        "Stallholders were notified by letter in November."
    ),
    url="https://data.example.test/releases/hawker-rent-review",
    outlet="Hawker Centres Board",
    date="2026-03-11",
    wire=False,
    origin="official",
    rating=None,
)
"""A primary source. Fictional, like the board that supposedly published it."""

WEB_PASSAGE = Passage(
    text=(
        "The board published the new rent schedule in November, ahead of the "
        "1 January start date, according to people who received the letters."
    ),
    url="https://example-news.test/hawker-rents-what-changed",
    outlet="Island Wire",
    date="2026-03-12",
    wire=False,
    origin="web",
    rating=None,
)
"""An independent report of the same thing. Also fictional."""

STANCE_ANSWER = {
    "scores": [
        {
            "index": 1,
            "stance": "supports",
            "quote": "The revised rents take effect on 1 January",
        },
        {
            "index": 2,
            "stance": "supports",
            "quote": "The board published the new rent schedule in November",
        },
    ]
}
"""What the stance model would plausibly return for those two passages. Both
quotes are really in them, which is what stage 3 verifies."""

JUDGE_ANSWER = {
    "verdict": "supported",
    "confidence": "high",
    "evidence": (
        "The Hawker Centres Board release and Island Wire both say the new rents "
        "take effect on 1 January."
    ),
    "cited_spans": [
        "The revised rents take effect on 1 January",
        "The board published the new rent schedule in November",
    ],
}
"""A decided verdict whose cited spans are genuinely in the passages, so it
survives verification in stage 4 and again in stage 5."""


# ---------------------------------------------------------------- fake seams


@dataclass
class ScriptedTransport:
    """An :class:`~app.llm.LLMTransport` that answers by schema, not by turn.

    :class:`~app.llm.ReplayTransport` consumes a list in order, which is exactly
    wrong here: the whole point of the orchestrator is that claims are worked
    concurrently, so the *order* the three schemas are asked for is not
    knowable. This one dispatches on ``schema_name`` instead, which makes it
    order-independent, and records every call so a test can prove the cost rules
    (one extraction per article; no model call for a claim with no passages).

    ``fail_for`` maps a substring of the user message to an exception raised
    instead of an answer — how a test scripts "this one claim's provider is
    down" without touching the others. It is matched on the per-claim calls
    only: the extraction call carries the whole article, so every needle would
    match it. ``fail_extraction`` is the separate switch for that one.
    """

    answers: dict[str, dict[str, Any]]
    fail_for: dict[str, Exception] = field(default_factory=dict)
    fail_extraction: Exception | None = None
    calls: list[tuple[str, str]] = field(default_factory=list)

    async def complete(
        self,
        *,
        model: str,
        system: str,
        user: str,
        schema_name: str,
        json_schema: dict[str, Any],
        timeout: float,
    ) -> LLMResponse:
        """Record the call, then raise or answer for ``schema_name``."""
        self.calls.append((schema_name, model))
        if schema_name == "ExtractionResponse":
            if self.fail_extraction is not None:
                raise self.fail_extraction
        else:
            for needle, error in self.fail_for.items():
                if needle in user:
                    raise error
        if schema_name not in self.answers:
            raise AssertionError(f"no scripted answer for schema {schema_name!r}")
        return LLMResponse(
            content=json.dumps(self.answers[schema_name]),
            prompt_tokens=100,
            completion_tokens=20,
        )

    def count(self, schema_name: str) -> int:
        """How many calls asked for ``schema_name``."""
        return sum(1 for name, _ in self.calls if name == schema_name)


@dataclass
class FakeSearch:
    """A :class:`~app.pipeline.providers.SearchProvider` with no network.

    Returns the same two fictional passages for every claim except the one whose
    quote contains :data:`NO_EVIDENCE_QUOTE`, which gets nothing — an empty web
    is a normal outcome and must produce an honest ``unverifiable``, not a
    failure.

    It also tracks how many searches are in flight at once, which is how
    :func:`test_concurrency_is_bounded_by_the_setting` sees the semaphore
    working: retrieval is the first thing every claim does, so peak concurrency
    here is peak concurrency in the pipeline.
    """

    delays: dict[str, float] = field(default_factory=dict)
    in_flight: int = 0
    peak: int = 0
    queries: list[str] = field(default_factory=list)

    async def search(self, query: str, *, limit: int) -> list[Passage]:
        """Answer one claim's query, pausing if this test asked for a pause."""
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        self.queries.append(query)
        try:
            for needle, delay in self.delays.items():
                if needle in query:
                    await asyncio.sleep(delay)
                    break
            else:
                # Always yield, so "concurrent" is exercised rather than assumed:
                # without an await the whole run would complete claim by claim.
                await asyncio.sleep(0)
            if NO_EVIDENCE_QUOTE in query:
                return []
            return [OFFICIAL_PASSAGE, WEB_PASSAGE][:limit]
        finally:
            self.in_flight -= 1


@dataclass
class RaisingSearch(FakeSearch):
    """A :class:`FakeSearch` that fails every call — the shape of an expired key.

    BLOCKER B5's repro: an expired key, a wrong tool name, or a quota block all
    surface the same way to this pipeline — the search provider raises on
    every call. A subclass of :class:`FakeSearch` rather than a fresh fake so
    it drops straight into :func:`make_deps`'s ``search=`` seam and
    :func:`fake_providers`'s typing unchanged.
    """

    calls: int = 0

    async def search(self, query: str, *, limit: int) -> list[Passage]:
        self.calls += 1
        raise RuntimeError("HTTP 401")


def fake_providers(search: FakeSearch) -> Providers:
    """The four providers, with only web search doing anything.

    The others are the same :class:`~app.pipeline.providers.NullPassageProvider`
    production uses when a key is missing, so retrieval takes exactly the path it
    takes without a ``GOOGLE_FACTCHECK_API_KEY``: fact-check finds nothing, web
    search answers, and the per-kind supplement adds nothing.
    """
    return Providers(
        factcheck=NullPassageProvider(reason="no fact-check key in tests"),
        search=search,
        official=NullPassageProvider(reason="no official-data provider in tests"),
        cited=NullCitedSourceProvider(reason="no cited-source provider in tests"),
        timeout_seconds=5.0,
    )


def extraction_answer() -> dict[str, Any]:
    """The recorded extraction answer for the fixture article.

    Reused from stage 1's own fixtures rather than copied: if the claims that
    file yields ever change, this file's expectations must change with them.
    """
    with (FIXTURES / "extract" / "hawker_claims.json").open(encoding="utf-8") as handle:
        payload: dict[str, Any] = json.load(handle)
    answer: dict[str, Any] = payload["json"]
    return answer


def make_deps(
    *,
    search: FakeSearch | None = None,
    fail_for: dict[str, Exception] | None = None,
    fail_extraction: Exception | None = None,
    answers: dict[str, dict[str, Any]] | None = None,
) -> tuple[PipelineDeps, ScriptedTransport, FakeSearch]:
    """Build fully offline dependencies, plus the two fakes to assert against."""
    transport = ScriptedTransport(
        answers=answers
        or {
            "ExtractionResponse": extraction_answer(),
            "StanceResponse": STANCE_ANSWER,
            "JudgeResponse": JUDGE_ANSWER,
        },
        fail_for=dict(fail_for or {}),
        fail_extraction=fail_extraction,
    )
    provider = search if search is not None else FakeSearch()
    meter = LLMMeter()
    client = LLMClient(
        api_key="test-key-never-used",
        timeout=5.0,
        max_retries=0,
        transport=meter.instrument(transport),
        retry_base_delay=0.0,
    )
    deps = PipelineDeps(llm=client, providers=fake_providers(provider), meter=meter)
    return deps, transport, provider


def pipeline_settings(**overrides: Any) -> Settings:
    """Settings for the real pipeline, ignoring any developer's ``backend/.env``."""
    overrides.setdefault("use_mock_pipeline", False)
    return build_settings(**overrides)


def check_request(**overrides: Any) -> CheckRequest:
    """A ``CheckRequest`` for the fictional hawker article."""
    with (FIXTURES / "article.json").open(encoding="utf-8") as handle:
        article = json.load(handle)
    payload = {
        "url": article["url"],
        "title": article["title"],
        "text": article["text"],
        "install_id": "11111111-2222-3333-4444-555555555555",
    }
    payload.update(overrides)
    return CheckRequest.model_validate(payload)


async def published(redis: FakeRedis, job_id: str = JOB_ID) -> list[dict[str, Any]]:
    """Every event the job stored, in order, as ``{"event", "data"}`` records.

    Read from the replay list rather than from a live subscription: the events
    are the same bytes either way (``app/events.py`` publishes the stored record
    with its sequence number prepended), and a test that does not have to race a
    subscriber is a test that cannot flake.
    """
    raw = await redis.lrange(EVENTS_KEY.format(job_id=job_id), 0, -1)
    return [json.loads(item) for item in raw]


def events_of(records: list[dict[str, Any]], name: str) -> list[dict[str, Any]]:
    """Just the ``data`` payloads of the records named ``name``."""
    return [record["data"] for record in records if record["event"] == name]


# ---------------------------------------------------------------- the happy path


async def test_the_event_sequence_is_claims_found_then_claims_then_done(
    fake_redis: FakeRedis,
) -> None:
    """The contract the popup was written against, in order, from the real stages.

    ``claims_found`` first with every id, one ``claim`` each, ``done`` last —
    and nothing after ``done``, because the relay closes the stream on it.
    """
    deps, _, _ = make_deps()

    await run_pipeline(fake_redis, JOB_ID, check_request(), settings=pipeline_settings(), deps=deps)

    records = await published(fake_redis)
    assert [record["event"] for record in records] == (
        ["claims_found"] + ["claim"] * len(EXPECTED_CLAIM_IDS) + ["done"]
    )

    found = ClaimsFoundEvent.model_validate(records[0]["data"])
    assert found.claim_ids == EXPECTED_CLAIM_IDS
    assert found.count == len(found.claim_ids)

    done = DoneEvent.model_validate(records[-1]["data"])
    # Six claims the fake web supports, and the one it has nothing on.
    assert done.counts.model_dump() == {
        "supported": 6,
        "contradicted": 0,
        "missing_context": 0,
        "unverifiable": 1,
    }


async def test_claim_ids_are_article_ordered_even_though_claims_arrive_out_of_order(
    fake_redis: FakeRedis,
) -> None:
    """The reason ``claims_found`` carries ids at all (``docs/decisions.md`` §15).

    Claims are worked concurrently and published the moment each resolves, so
    their arrival order is the order the evidence came back in — here forced by
    making the first claim's search slow. The id list is still article order, so
    a client lays out its rows up front and fills each one when its own claim
    lands, and the live run renders exactly like the cached replay.
    """
    search = FakeSearch(delays={"rise by 40%": 0.05})
    deps, _, _ = make_deps(search=search)

    await run_pipeline(
        fake_redis,
        JOB_ID,
        check_request(),
        settings=pipeline_settings(pipeline_concurrency=8),
        deps=deps,
    )

    records = await published(fake_redis)
    arrival = [claim["id"] for claim in events_of(records, "claim")]

    assert arrival != EXPECTED_CLAIM_IDS
    assert arrival[-1] == "c1"
    assert sorted(arrival) == EXPECTED_CLAIM_IDS
    assert ClaimsFoundEvent.model_validate(records[0]["data"]).claim_ids == EXPECTED_CLAIM_IDS


async def test_every_published_claim_obeys_the_schema_and_the_product_rules(
    fake_redis: FakeRedis,
) -> None:
    """The last gate before a reader, asserted on the wire rather than trusted.

    Each claim validates against the generated model *and* against the two
    invariants the schema cannot express: confidence null iff ``unverifiable``,
    sources empty iff ``unverifiable``. Offsets are checked against the article
    the client actually sent, which is the promise milestone 1's mock could not
    keep and milestone 3's anchoring is built on.
    """
    deps, _, _ = make_deps()
    request = check_request()

    await run_pipeline(fake_redis, JOB_ID, request, settings=pipeline_settings(), deps=deps)

    claims = events_of(await published(fake_redis), "claim")
    assert claims
    for payload in claims:
        model = Claim.model_validate(payload)
        validate_claim(payload)
        assert request.text[model.start : model.end] == model.quote
        assert model.evidence.strip()
        assert model.trail


async def test_a_claim_with_no_evidence_is_unverifiable_and_costs_no_tokens(
    fake_redis: FakeRedis,
) -> None:
    """An empty web is an answer, and it is a free one.

    Stage 3 returns nothing without a call and stage 4 answers ``unverifiable``
    without asking anyone, so the claim nobody found anything on costs one
    retrieval and no tokens (``CLAUDE.md`` cost rules). The claim still ships an
    explanation and a provenance trail, and no sources.
    """
    deps, transport, _ = make_deps()

    await run_pipeline(fake_redis, JOB_ID, check_request(), settings=pipeline_settings(), deps=deps)

    claims = {claim["id"]: claim for claim in events_of(await published(fake_redis), "claim")}
    unverifiable = [claim for claim in claims.values() if claim["verdict"] == "unverifiable"]

    assert len(unverifiable) == 1
    assert unverifiable[0]["sources"] == []
    assert unverifiable[0]["confidence"] is None
    assert unverifiable[0]["evidence"].strip()
    assert unverifiable[0]["trail"]
    # Six claims had passages; the seventh had none and was never sent anywhere.
    assert transport.count("StanceResponse") == 6
    assert transport.count("JudgeResponse") == 6


async def test_one_article_costs_exactly_one_extraction_call(fake_redis: FakeRedis) -> None:
    """Stage 1 is the only per-article call and the only one that cannot be capped.

    Everything after it is per claim and bounded by ``MAX_CLAIMS``; a second
    extraction call would double the fixed cost of every check.
    """
    deps, transport, _ = make_deps()

    await run_pipeline(fake_redis, JOB_ID, check_request(), settings=pipeline_settings(), deps=deps)

    assert transport.count("ExtractionResponse") == 1


async def test_each_stage_uses_its_own_configured_model(fake_redis: FakeRedis) -> None:
    """Per-stage model configuration reaches the wire (``docs/decisions.md`` §7).

    Swapping one stage to a stronger model during a test run must need no code
    change, which is only true if the orchestrator passes the settings down
    untouched.
    """
    deps, transport, _ = make_deps()
    settings = pipeline_settings(
        openai_model_extract="model-extract",
        openai_model_stance="model-stance",
        openai_model_judge="model-judge",
    )

    await run_pipeline(fake_redis, JOB_ID, check_request(), settings=settings, deps=deps)

    used = dict(transport.calls)
    assert used == {
        "ExtractionResponse": "model-extract",
        "StanceResponse": "model-stance",
        "JudgeResponse": "model-judge",
    }


# ---------------------------------------------------------------- the cache


async def test_the_result_is_cached_before_done(
    fake_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Order matters: ``done`` is the client's cue that the check is finished.

    A ``done`` published before the cache write leaves a window in which a second
    reader of the same article misses the entry and pays for the whole check
    again — and, worse, the two runs can announce different claim ids for one
    article. The claims are cached in article order, which is what makes a cache
    hit replay identically to the live run that produced it.
    """
    timeline: list[str] = []
    real_set_check = run_module.set_check
    real_publish = run_module.publish_event

    async def recording_set_check(redis: Any, url: str, result: dict[str, Any]) -> None:
        timeline.append("cache")
        await real_set_check(redis, url, result)

    async def recording_publish(
        redis: Any, job_id: str, event: str, data: dict[str, Any]
    ) -> int:
        timeline.append(event)
        return await real_publish(redis, job_id, event, data)

    monkeypatch.setattr(run_module, "set_check", recording_set_check)
    monkeypatch.setattr(run_module, "publish_event", recording_publish)

    deps, _, _ = make_deps()
    request = check_request()
    await run_pipeline(fake_redis, JOB_ID, request, settings=pipeline_settings(), deps=deps)

    assert timeline[0] == "claims_found"
    assert timeline[-2:] == ["cache", "done"]

    cached = await get_check(fake_redis, str(request.url))
    assert cached is not None
    assert [claim["id"] for claim in cached["claims"]] == EXPECTED_CLAIM_IDS
    assert cached["counts"]["supported"] == 6
    assert cached["checked_at"]


async def test_a_run_with_one_idiosyncratic_claim_failure_is_still_cached(
    fake_redis: FakeRedis,
) -> None:
    """MAJOR M14: one claim's bad luck must not force every other claim's evidence
    to be re-bought on the next reader's visit.

    **Deliberately rewritten from the old behaviour.** This test used to be
    named ``test_a_run_with_a_failed_claim_is_not_cached`` and asserted
    ``get_check(...) is None`` here — i.e. that *any* claim failing skipped
    caching the whole run. That was the bug MAJOR M14 reports: six good
    verdicts and one flaky stance call meant every subsequent reader of this
    URL paid to re-check all seven claims, forever, until one lucky run
    finished with zero failures. The new policy
    (:class:`~app.pipeline.run._ClaimBatch` /
    :attr:`~app.pipeline.run._ClaimBatch.cacheable`) caches a run whenever at
    least one claim succeeded, so the six real verdicts are kept and only the
    one honest abstention is what a retry would improve on.

    This is *not* the same scenario as BLOCKER B5 (below): retrieval itself
    succeeds here — the fake web search answers normally — and it is stage 3
    (stance) that fails for one claim, a stand-in for a stage bug or a
    one-off provider hiccup rather than a systemic outage.
    """
    deps, _, _ = make_deps(fail_for={"200 stalls": LLMUnavailable("stance: provider down")})
    request = check_request()

    await run_pipeline(fake_redis, JOB_ID, request, settings=pipeline_settings(), deps=deps)

    records = await published(fake_redis)
    assert records[-1]["event"] == "done"

    cached = await get_check(fake_redis, str(request.url))
    assert cached is not None
    cached_by_id = {claim["id"]: claim for claim in cached["claims"]}
    assert cached_by_id["c2"]["verdict"] == "unverifiable"
    assert cached_by_id["c2"]["evidence"] == FAILED_CLAIM_EVIDENCE
    # The other six claims are cached with their real verdicts, not silently
    # dropped or downgraded because one sibling claim failed. c5 is the fixture's
    # own honestly-empty-web claim (NO_EVIDENCE_QUOTE) and is unverifiable on
    # every run, injected failure or not — the other five are genuinely supported.
    assert cached_by_id["c5"]["verdict"] == "unverifiable"
    assert cached_by_id["c5"]["evidence"] != FAILED_CLAIM_EVIDENCE
    other_ids = [cid for cid in EXPECTED_CLAIM_IDS if cid not in {"c2", "c5"}]
    assert [cached_by_id[cid]["verdict"] for cid in other_ids] == ["supported"] * len(other_ids)


async def test_a_run_where_every_claims_retrieval_is_broken_is_not_cached(
    fake_redis: FakeRedis,
) -> None:
    """BLOCKER B5, reproduced end to end: a search provider raising on every call
    (an expired key, a wrong tool name, a quota block) must not be reported —
    or cached — as a completed search that found nothing.

    Before the fix, every claim's retrieval failure was swallowed to ``[]`` by
    :func:`~app.pipeline.retrieve._guarded`, so this scenario published seven
    ``unverifiable`` claims that all looked like an honest empty web *and*
    wrote that to the 7-day cache, telling every later reader of this article
    "nothing to see here" for a week. Now every claim's :func:`check_claim`
    raises :class:`~app.pipeline.run.RetrievalFailedError`, is published with
    :data:`FAILED_CLAIM_EVIDENCE` (which never claims a completed search), and
    the run as a whole — nothing succeeded — is not cached.
    """
    deps, _, _ = make_deps(search=RaisingSearch())
    request = check_request()

    await run_pipeline(fake_redis, JOB_ID, request, settings=pipeline_settings(), deps=deps)

    records = await published(fake_redis)
    claims = events_of(records, "claim")
    assert sorted(claim["id"] for claim in claims) == EXPECTED_CLAIM_IDS
    assert all(claim["verdict"] == "unverifiable" for claim in claims)
    assert all(claim["evidence"] == FAILED_CLAIM_EVIDENCE for claim in claims)
    assert all(claim["sources"] == [] for claim in claims)
    assert all(claim["confidence"] is None for claim in claims)
    for claim in claims:
        validate_claim(claim)

    assert records[-1]["event"] == "done"
    assert DoneEvent.model_validate(records[-1]["data"]).counts.unverifiable == len(
        EXPECTED_CLAIM_IDS
    )
    # The BLOCKER B5 assertion: nothing succeeded, so nothing is cached.
    assert await get_check(fake_redis, str(request.url)) is None


# ---------------------------------------------------------------- resource cleanup


@dataclass
class FakeOpenAIClient:
    """Stands in for ``openai.AsyncOpenAI`` — just enough to prove it got closed."""

    closed: bool = False

    async def close(self) -> None:
        self.closed = True


@dataclass
class FakeRawTransport:
    """Stands in for :class:`~app.llm.OpenAIChatTransport` — just its ``_client``.

    ``PipelineDeps.build`` reaches into this private attribute (see its class
    docstring for why); this fake exists so the test can observe that without
    needing the real SDK or a key.
    """

    _client: FakeOpenAIClient

    async def complete(self, **kwargs: Any) -> LLMResponse:
        raise AssertionError("not exercised by this test")


async def test_pipeline_deps_build_closes_the_openai_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MAJOR M12: the resource leak. ``PipelineDeps.aclose`` must close the OpenAI
    client's connection pool, not just the plain HTTP client.

    Before the fix, :meth:`~app.pipeline.run.PipelineDeps.build` never kept a
    handle on the ``openai.AsyncOpenAI`` instance
    :func:`~app.llm.build_openai_transport` opens, so
    :meth:`~app.pipeline.run.PipelineDeps.aclose` — called on every path
    through :func:`~app.pipeline.run.run_pipeline`, success or failure — closed
    only ``owned_http`` and silently leaked one OpenAI client (and its
    connection pool) per check. ``build_openai_transport`` is monkeypatched
    here to a fake so the test can observe the close without a key or the real
    SDK.
    """
    fake_client = FakeOpenAIClient()
    monkeypatch.setattr(
        run_module,
        "build_openai_transport",
        lambda api_key, timeout: FakeRawTransport(_client=fake_client),
    )

    deps = run_module.PipelineDeps.build(pipeline_settings(openai_api_key="test-key-unused"))

    assert deps.owned_openai_client is fake_client
    assert fake_client.closed is False

    await deps.aclose()

    assert fake_client.closed is True


# ---------------------------------------------------------------- robustness


async def test_one_failing_claim_does_not_kill_the_run(fake_redis: FakeRedis) -> None:
    """The single most important robustness property: one claim, not the article.

    A 4xx on one claim's stance call means that claim cannot be judged. It is
    published as ``unverifiable`` with an evidence sentence that says the check
    did not finish — never one implying we looked and the web was empty — and
    every other claim still gets a real verdict.
    """
    deps, _, _ = make_deps(
        fail_for={"200 stalls": LLMBadRequest("gpt-test: provider returned 400")}
    )

    await run_pipeline(fake_redis, JOB_ID, check_request(), settings=pipeline_settings(), deps=deps)

    records = await published(fake_redis)
    claims = {claim["id"]: claim for claim in events_of(records, "claim")}

    assert sorted(claims) == EXPECTED_CLAIM_IDS
    assert records[-1]["event"] == "done"

    failed = claims["c2"]
    assert failed["verdict"] == "unverifiable"
    assert failed["evidence"] == FAILED_CLAIM_EVIDENCE
    assert failed["sources"] == []
    assert failed["confidence"] is None
    assert failed["trail"]
    validate_claim(failed)

    assert claims["c1"]["verdict"] == "supported"
    assert DoneEvent.model_validate(records[-1]["data"]).counts.unverifiable == 2


async def test_zero_extracted_claims_still_ends_the_stream(fake_redis: FakeRedis) -> None:
    """An article with nothing check-worthy is a real answer, not a hang.

    The extension allocates its rows from ``claims_found`` and waits for
    ``done``; an opinion column that yields no claims must produce both, with a
    zeroed tally, immediately.
    """
    deps, transport, _ = make_deps(
        answers={
            "ExtractionResponse": {"claims": []},
            "StanceResponse": STANCE_ANSWER,
            "JudgeResponse": JUDGE_ANSWER,
        }
    )
    request = check_request()

    await run_pipeline(fake_redis, JOB_ID, request, settings=pipeline_settings(), deps=deps)

    records = await published(fake_redis)
    assert [record["event"] for record in records] == ["claims_found", "done"]

    found = ClaimsFoundEvent.model_validate(records[0]["data"])
    assert found.count == 0
    assert found.claim_ids == []

    done = DoneEvent.model_validate(records[-1]["data"])
    assert done.counts.model_dump() == {
        "supported": 0,
        "contradicted": 0,
        "missing_context": 0,
        "unverifiable": 0,
    }
    # Nothing to check means nothing to pay for beyond the one extraction call…
    assert transport.count("StanceResponse") == 0
    # …and the empty result is cached, so re-reading the column is free.
    assert await get_check(fake_redis, str(request.url)) == {
        "claims": [],
        "counts": done.counts.model_dump(),
        "checked_at": done.checked_at,
    }


async def test_a_failure_before_the_claims_are_known_publishes_error(
    fake_redis: FakeRedis,
) -> None:
    """Extraction failing ends the job with ``error``, never with a silent stop.

    And never with ``claims_found: 0``: telling a reader "nothing here is worth
    checking" when nothing was checked is a lie the shape of the event contract
    would otherwise let through.
    """
    deps, _, _ = make_deps(
        fail_extraction=LLMBadRequest("gpt-test: provider returned 401")
    )

    await run_pipeline(fake_redis, JOB_ID, check_request(), settings=pipeline_settings(), deps=deps)

    records = await published(fake_redis)
    assert [record["event"] for record in records] == ["error"]

    error = ErrorEvent.model_validate(records[0]["data"])
    assert error.code == ERROR_CODE
    assert error.message.strip()


async def test_a_failure_after_the_claims_still_terminates_the_stream(
    fake_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Whatever breaks, the stream ends. Milestone 1's relay depends on it.

    A stream that never receives ``done`` or ``error`` is only closed by the
    relay's deadline, minutes later, with the reader staring at a spinner.
    """

    async def broken_set_check(redis: Any, url: str, result: dict[str, Any]) -> None:
        raise RuntimeError("redis fell over")

    monkeypatch.setattr(run_module, "set_check", broken_set_check)
    deps, _, _ = make_deps()

    await run_pipeline(fake_redis, JOB_ID, check_request(), settings=pipeline_settings(), deps=deps)

    records = await published(fake_redis)
    assert records[-1]["event"] == "error"
    assert ErrorEvent.model_validate(records[-1]["data"]).code == ERROR_CODE


async def test_a_broken_publish_ends_the_job_instead_of_leaking_late_claims(
    fake_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the run is over, it is over for every claim still in flight.

    Publishing is the one thing in the per-claim path that is not caught and
    turned into an abstention: if the stream itself is broken, this job is
    finished. The claims still being worked are cancelled rather than left to
    publish into a stream that has already carried ``error`` — an event after the
    terminal one is a contract violation, and the relay would drop it anyway.
    """
    real_publish = run_module.publish_event

    async def flaky_publish(
        redis: Any, job_id: str, event: str, data: dict[str, Any]
    ) -> int:
        if event == "claim" and data["id"] == "c1":
            raise RuntimeError("redis fell over mid-claim")
        return await real_publish(redis, job_id, event, data)

    monkeypatch.setattr(run_module, "publish_event", flaky_publish)
    # c1's search resolves first, so the failure lands while other claims are
    # still in flight — which is the situation worth pinning.
    search = FakeSearch(delays=dict.fromkeys(["200 stalls", "briefing", "in 2024"], 0.05))
    deps, _, _ = make_deps(search=search)

    await run_pipeline(
        fake_redis,
        JOB_ID,
        check_request(),
        settings=pipeline_settings(pipeline_concurrency=8),
        deps=deps,
    )

    records = await published(fake_redis)
    assert records[-1]["event"] == "error"
    assert "done" not in [record["event"] for record in records]
    # The delayed claims never made it to the wire: they were cancelled, not
    # published after the error.
    assert len(events_of(records, "claim")) < len(EXPECTED_CLAIM_IDS)


async def test_run_pipeline_never_raises(fake_redis: FakeRedis) -> None:
    """The route spawns this and never awaits it, so a raise would be unheard.

    Only :class:`asyncio.CancelledError` — the job being shut down, not the job
    failing — is allowed through.
    """
    deps, _, _ = make_deps(answers={"ExtractionResponse": {"not": "the schema"}})

    await run_pipeline(fake_redis, JOB_ID, check_request(), settings=pipeline_settings(), deps=deps)

    assert (await published(fake_redis))[-1]["event"] in {"done", "error"}


# ---------------------------------------------------------------- the caps


async def test_concurrency_is_bounded_by_the_setting(fake_redis: FakeRedis) -> None:
    """Claims run concurrently — but never more than ``PIPELINE_CONCURRENCY``.

    Unbounded, ``MAX_CLAIMS`` claims each fanning out to several providers and
    two model calls would hit provider rate limits and let one reader's check
    starve everyone else's. Retrieval is the first thing each claim does, so the
    peak measured in the search provider is the pipeline's peak.
    """
    search = FakeSearch(delays=dict.fromkeys(["rise by", "200 stalls", "briefing"], 0.02))
    deps, _, _ = make_deps(search=search)

    await run_pipeline(
        fake_redis,
        JOB_ID,
        check_request(),
        settings=pipeline_settings(pipeline_concurrency=2),
        deps=deps,
    )

    assert search.peak <= 2
    # …and it really is concurrent: a serial run would never see two at once.
    assert search.peak == 2
    assert len(search.queries) == len(EXPECTED_CLAIM_IDS)


async def test_a_zero_concurrency_setting_cannot_stall_a_check(fake_redis: FakeRedis) -> None:
    """A misconfiguration degrades to serial work, never to a deadlock.

    ``asyncio.Semaphore(0)`` would block for ever and the reader would sit on a
    stream until its deadline, so the floor is 1 and the log says so.
    """
    deps, _, _ = make_deps()

    await run_pipeline(
        fake_redis,
        JOB_ID,
        check_request(),
        settings=pipeline_settings(pipeline_concurrency=0),
        deps=deps,
    )

    records = await published(fake_redis)
    assert records[-1]["event"] == "done"
    assert len(events_of(records, "claim")) == len(EXPECTED_CLAIM_IDS)


async def test_max_claims_caps_what_the_pipeline_pays_for(fake_redis: FakeRedis) -> None:
    """``MAX_CLAIMS`` is the biggest lever on the cost of one check.

    Extraction enforces it, but the orchestrator is where it turns into money
    not spent: three claims means three retrievals and six model calls, whatever
    the model offered.
    """
    deps, transport, search = make_deps()

    await run_pipeline(
        fake_redis,
        JOB_ID,
        check_request(),
        settings=pipeline_settings(max_claims=3),
        deps=deps,
    )

    found = ClaimsFoundEvent.model_validate((await published(fake_redis))[0]["data"])
    assert found.count == 3
    assert len(search.queries) == 3
    assert transport.count("StanceResponse") + transport.count("JudgeResponse") <= 6


# ---------------------------------------------------------------- the bill


async def test_the_run_logs_its_totals_without_any_article_text(
    fake_redis: FakeRedis, caplog: pytest.LogCaptureFixture
) -> None:
    """One line per run: claims, the tally, the LLM bill, the wall clock.

    And nothing else. A quote, a passage or a URL in a log line is article text
    next to a job identifier (privacy rule 6), so the assertion is not that the
    line is short but that no sentence of the article is in it.
    """
    caplog.set_level(logging.INFO, logger="app.pipeline.run")
    deps, _, _ = make_deps()
    request = check_request()

    await run_pipeline(fake_redis, JOB_ID, request, settings=pipeline_settings(), deps=deps)

    ours = [record.getMessage() for record in caplog.records if record.name == "app.pipeline.run"]
    line = next(message for message in ours if "finished" in message)

    assert JOB_ID in line
    assert "claims=7" in line
    assert "supported=6" in line
    assert "llm_calls=13" in line  # one extraction + six stance + six judge
    assert "tokens=" in line
    assert "elapsed_ms=" in line

    for message in ours:
        assert str(request.url) not in message
        for sentence in request.text.split("\n\n"):
            assert sentence[:40] not in message


async def test_the_meter_counts_attempts_and_tokens(fake_redis: FakeRedis) -> None:
    """The per-run bill comes from the transport, which is the only place it can.

    The stages log their own calls and return their :class:`~app.llm.Usage` to
    nobody, so a per-job total has to be assembled underneath them.
    """
    deps, transport, _ = make_deps()

    await run_pipeline(fake_redis, JOB_ID, check_request(), settings=pipeline_settings(), deps=deps)

    assert deps.meter is not None
    assert deps.meter.calls == len(transport.calls) == 13
    assert deps.meter.prompt_tokens == 13 * 100
    assert deps.meter.completion_tokens == 13 * 20
    assert deps.meter.total_tokens == 13 * 120


async def test_an_unmetered_client_is_reported_as_unknown_not_as_zero(
    fake_redis: FakeRedis, caplog: pytest.LogCaptureFixture
) -> None:
    """A confident zero in a cost log is worse than an admitted gap."""
    caplog.set_level(logging.INFO, logger="app.pipeline.run")
    deps, _, _ = make_deps()
    unmetered = PipelineDeps(llm=deps.llm, providers=deps.providers)

    await run_pipeline(
        fake_redis, JOB_ID, check_request(), settings=pipeline_settings(), deps=unmetered
    )

    line = next(
        record.getMessage()
        for record in caplog.records
        if record.name == "app.pipeline.run" and "finished" in record.getMessage()
    )
    assert "llm_calls=unmetered" in line


# ---------------------------------------------------------------- dependencies


def test_building_dependencies_without_a_key_fails_loudly_and_early() -> None:
    """No key is a deployment fact, and it is reported as one.

    Demanded before any client is opened, so the commonest misconfiguration
    costs no sockets — and it must never degrade into the mock, because a reader
    shown fixture verdicts for their own article cannot tell.
    """
    from app.config import MissingSettingError

    with pytest.raises(MissingSettingError) as caught:
        PipelineDeps.build(pipeline_settings(openai_api_key=None))

    assert caught.value.env_var == "OPENAI_API_KEY"


async def test_a_missing_key_ends_the_job_with_error_not_with_a_hang(
    fake_redis: FakeRedis,
) -> None:
    """The path this repository is actually on: no key, no network.

    ``run_pipeline`` builds its own dependencies when none are injected, and a
    missing key must surface as one ``error`` event rather than an exception
    nobody is awaiting.
    """
    await run_pipeline(
        fake_redis,
        JOB_ID,
        check_request(),
        settings=pipeline_settings(openai_api_key=None),
    )

    records = await published(fake_redis)
    assert [record["event"] for record in records] == ["error"]
    assert ErrorEvent.model_validate(records[0]["data"]).code == ERROR_CODE


async def test_injected_dependencies_are_not_closed_by_the_run(fake_redis: FakeRedis) -> None:
    """Whoever opened a client closes it. The run closes only what it opened.

    ``PipelineDeps.build`` owns an HTTP client and closes it; injected
    dependencies belong to the caller — the eval harness reuses one set across a
    whole golden set — and closing them would break the next run.
    """
    closed: list[str] = []

    class RecordingHttp:
        async def aclose(self) -> None:
            closed.append("closed")

    deps, _, _ = make_deps()
    await run_pipeline(fake_redis, JOB_ID, check_request(), settings=pipeline_settings(), deps=deps)
    assert closed == []

    owned = PipelineDeps(
        llm=deps.llm,
        providers=deps.providers,
        meter=deps.meter,
        owned_http=RecordingHttp(),  # type: ignore[arg-type]
    )
    await owned.aclose()
    assert closed == ["closed"]


# ---------------------------------------------------------------- the route seam


def test_the_route_picks_the_pipeline_from_the_setting() -> None:
    """``USE_MOCK_PIPELINE`` is a switch, not a fallback.

    The two callables are interchangeable at the route because they take the
    same arguments and owe the stream the same events; which one runs is a
    deliberate configuration choice, never something the real pipeline decides
    for itself when a key is missing.
    """
    from app.pipeline.mock import run_mock_pipeline

    assert pipeline_for(pipeline_settings()) is run_pipeline
    assert pipeline_for(pipeline_settings(use_mock_pipeline=True)) is run_mock_pipeline


def test_the_stream_deadline_covers_a_real_check() -> None:
    """The relay's backstop must outlast the slowest honest run, not the median.

    Cutting off a check that is still working would be a far worse bug than
    holding one connection open a little longer, so the budget is the arithmetic
    worst case of the pipeline's own timeouts: one extraction call, then a batch
    of claims per ``PIPELINE_CONCURRENCY``, each claim capped at three provider
    calls plus a stance and a judge call, each model call retried
    ``LLM_MAX_RETRIES`` times.
    """
    settings = pipeline_settings(
        max_claims=8,
        pipeline_concurrency=4,
        llm_timeout_seconds=30.0,
        llm_max_retries=2,
    )

    budget = real_pipeline_budget_seconds(settings)
    # 90s extraction, then 2 batches of (30s of providers + 180s of model calls).
    assert budget == pytest.approx(90.0 + 2 * (30.0 + 180.0))
    assert stream_deadline_seconds(settings) == budget

    # The mock's pacing is unrelated to any of that, and still governs the mock.
    mock = pipeline_settings(use_mock_pipeline=True, max_claims=8, mock_step_delay=1.0)
    assert stream_deadline_seconds(mock) < budget
