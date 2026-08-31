"""The real pipeline: ``extract`` → ``retrieve`` → ``stance`` → ``judge`` → ``aggregate``.

This module is the milestone-2 replacement for :mod:`app.pipeline.mock`, and it
is deliberately callable at exactly the same shape, so ``app/routes/check.py``
chooses between them with one ``if`` and nothing else about the route changes::

    await run_pipeline(redis, job_id, payload, settings=settings)

It owns no stage logic of its own. Its whole job is the four things a pipeline
owes the stream, in order:

1. ``claims_found`` — count and every claim id, in **article** order
   (``docs/decisions.md`` §15), published as soon as extraction returns so a
   client can allocate its rows before any verdict exists;
2. ``claim`` — one per claim, **as each resolves**. Claims are worked
   concurrently (``settings.pipeline_concurrency`` at a time) precisely so they
   arrive out of order: that progressive fill is the product's signature
   interaction and the reason the event contract allows any order at all;
3. the finished result written to the 7-day URL cache;
4. ``done`` with the per-verdict tally — or ``error``, so a reader waiting on
   the stream is never left hanging.

**One claim failing is not a failed job.** Retrieval already refuses to raise,
and stages 3 and 4 turn a malformed model answer into an honest abstention by
themselves; what still reaches here is the deployment being wrong (a bad key, a
model this account cannot call, a provider outage) or a bug. Any of those is
caught per claim, published as ``unverifiable`` with an evidence sentence that
says the check did not finish, and the run carries on. Only a failure *before*
the claims are known — extraction itself, or a missing key — ends the job with
``error``.

Two decisions worth knowing, both about honesty rather than mechanism:

* **A run with a failed claim is not cached.** Everything else is: the cache is
  written before ``done`` exactly as the contract says. But an entry lives for
  seven days and is served to every later reader of that URL without re-running
  anything, so freezing "the provider was down for ten seconds" into it would
  charge every subsequent reader for one outage — the same trap
  ``usable_cache_entry`` exists to get out of. We cache results, not outages.
* **The mock is never a fallback.** ``settings.use_mock_pipeline`` selects it
  deliberately; the real pipeline failing publishes an error instead of quietly
  streaming fixture verdicts for somebody's actual article, which a reader would
  have no way to tell apart from a real check.

Nothing here logs article text, a quote, a passage or a URL — the per-run line
carries the job id, the tally, the LLM bill and the wall clock, and the stages
log their own ids and verdicts (``CLAUDE.md`` privacy rule 6).
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from redis.asyncio import Redis

from app.cache import set_check
from app.config import MissingSettingError, Settings
from app.events import publish_event
from app.invariants import UNVERIFIABLE, validate_claim
from app.llm import LLMClient, LLMError, LLMResponse, LLMTransport, build_openai_transport
from app.pipeline.aggregate import aggregate, build_trail
from app.pipeline.extract import extract_claims
from app.pipeline.judge import judge_claim
from app.pipeline.mock import FAILURE_MESSAGE, done_payload, error_payload, tally
from app.pipeline.providers import HttpxClient, Providers
from app.pipeline.retrieve import build_providers, retrieve_passages
from app.pipeline.stance import score_passages
from app.pipeline.types import ExtractedClaim
from app.schema_models import CheckRequest, Claim, ClaimsFoundEvent, Verdict

__all__ = [
    "ERROR_CODE",
    "FAILED_CLAIM_EVIDENCE",
    "LLMMeter",
    "PipelineDeps",
    "check_claim",
    "claims_found_payload",
    "run_pipeline",
    "unverifiable_claim",
]

logger = logging.getLogger(__name__)

ERROR_CODE = "internal"
"""``code`` on the ``error`` event for any job-level failure.

One code, because there is one thing a reader can do about a failed check
(try again) and the difference between a missing key and a provider outage is
an operator's problem, visible in the log rather than on the wire. The
extension special-cases only ``daily_limit``; everything else it renders as the
message we send.
"""

FAILED_CLAIM_EVIDENCE = (
    "This claim could not be checked: the search for evidence did not finish, "
    "so nothing was found for or against it."
)
"""Evidence sentence for a claim whose check raised.

Rule 2 requires an ``unverifiable`` claim to explain what was searched and not
found. When the search itself broke, the honest explanation is that it broke —
not a sentence implying we looked and the web was empty.
"""


@dataclass(slots=True)
class LLMMeter:
    """Running total of what one job spent at the LLM.

    The stages return their :class:`~app.llm.Usage` to nobody — each logs its own
    call and moves on — so the only place a *per-run* bill can be assembled is
    the transport underneath them. :meth:`instrument` wraps one, and every call
    made through the wrapped transport lands here.

    ``calls`` counts attempts, including ones that failed: a 5xx that was retried
    twice was three requests, and pretending otherwise would under-report the
    bill. Tokens are counted only when an answer actually came back, because a
    failure carries no usage to count.
    """

    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        """Prompt plus completion tokens across every answered call."""
        return self.prompt_tokens + self.completion_tokens

    def instrument(self, transport: LLMTransport) -> LLMTransport:
        """Return ``transport`` wrapped so its calls and tokens land in this meter."""
        return _MeteredTransport(inner=transport, meter=self)


@dataclass(frozen=True, slots=True)
class _MeteredTransport:
    """An :class:`~app.llm.LLMTransport` that counts what passes through it.

    A decorator rather than a change to :class:`~app.llm.LLMClient`: the client
    is deliberately thin, and a per-job counter is the orchestrator's concern,
    not the client's. Anything the inner transport raises propagates unchanged —
    the attempt has already been counted by then.
    """

    inner: LLMTransport
    meter: LLMMeter

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
        """Count the attempt, delegate, and count the tokens of an answer."""
        self.meter.calls += 1
        response = await self.inner.complete(
            model=model,
            system=system,
            user=user,
            schema_name=schema_name,
            json_schema=json_schema,
            timeout=timeout,
        )
        self.meter.prompt_tokens += response.prompt_tokens
        self.meter.completion_tokens += response.completion_tokens
        return response


@dataclass(frozen=True, slots=True)
class PipelineDeps:
    """Everything the pipeline reaches the outside world through, in one value.

    Injected so a test can run the *whole* pipeline offline: a
    :class:`~app.llm.ReplayTransport` (or any fake) behind ``llm`` and fake
    providers behind ``providers`` cover every outbound call the five stages can
    make. That is not a convenience here — this repository has no API keys and no
    route to OpenAI or Google, so an injectable seam is the only way any of this
    could be exercised at all.

    ``meter`` is optional because a caller that builds its own client may not
    have wrapped its transport; when it is absent the per-run log line says the
    bill is unknown rather than reporting a confident zero.

    ``owned_http`` is the HTTP client :meth:`build` opened and this object must
    therefore close. A caller that passes its own client keeps it None and closes
    its own.
    """

    llm: LLMClient
    providers: Providers
    meter: LLMMeter | None = None
    owned_http: HttpxClient | None = None

    @classmethod
    def build(cls, settings: Settings) -> PipelineDeps:
        """Build the production dependencies from ``settings``.

        Raises :class:`~app.config.MissingSettingError` when ``OPENAI_API_KEY``
        is unset — every stage needs it and so does web search, so there is no
        useful degraded mode. The key is demanded *first*, before any client is
        opened, so the common misconfiguration costs no sockets and leaks
        nothing. A missing ``GOOGLE_FACTCHECK_API_KEY`` is not fatal:
        :func:`~app.pipeline.retrieve.build_providers` catches it and every claim
        falls through to web search, which is correct and more expensive.

        The returned object owns an HTTP client; call :meth:`aclose` when the job
        ends.
        """
        api_key = settings.require_openai_api_key("the claim-checking pipeline")
        meter = LLMMeter()
        http = HttpxClient()
        return cls(
            llm=LLMClient(
                api_key=api_key,
                timeout=settings.llm_timeout_seconds,
                max_retries=settings.llm_max_retries,
                transport=meter.instrument(
                    build_openai_transport(api_key, settings.llm_timeout_seconds)
                ),
            ),
            providers=build_providers(settings, http=http),
            meter=meter,
            owned_http=http,
        )

    async def aclose(self) -> None:
        """Close whatever this object opened. A no-op for injected dependencies."""
        if self.owned_http is not None:
            await self.owned_http.aclose()


async def run_pipeline(
    redis: Redis,
    job_id: str,
    request: CheckRequest,
    *,
    settings: Settings,
    deps: PipelineDeps | None = None,
) -> None:
    """Check one article and stream the result. The drop-in for the mock.

    Publishes ``claims_found`` → one ``claim`` per claim as it resolves → the
    cache write → ``done``; or ``error`` if the job could not start. Never
    raises: a pipeline that raised would leave its reader on a stream that ends
    only at the relay's deadline.

    ``deps`` is the offline seam. Left None, production dependencies are built
    from ``settings`` and closed again when the job ends; passed in, they are the
    caller's to close.
    """
    started = time.perf_counter()
    owned = deps is None
    counts: dict[str, int] = {}
    try:
        if deps is None:
            deps = PipelineDeps.build(settings)

        claims = await extract_claims(request.text, client=deps.llm, settings=settings)
        await publish_event(redis, job_id, "claims_found", claims_found_payload(claims))

        results, complete = await _check_claims(
            redis, job_id, claims, request=request, settings=settings, deps=deps
        )

        counts = tally(results)
        checked_at = _now_iso()
        if complete:
            await set_check(
                redis,
                # AnyUrl normalises, so hash the string form — the cache read in
                # `start_check` and this write must agree on one spelling.
                str(request.url),
                {"claims": results, "counts": counts, "checked_at": checked_at},
            )
        else:
            logger.info(
                "job %s: not cached — at least one claim's check failed, and a "
                "seven-day entry would serve that failure to every later reader",
                job_id,
            )
        await publish_event(redis, job_id, "done", done_payload(counts, checked_at))
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.error("pipeline failed for job %s: %s", job_id, _safe_reason(exc))
        await _publish_failure(redis, job_id)
    finally:
        if owned and deps is not None:
            await deps.aclose()
        _log_totals(job_id, counts, deps, time.perf_counter() - started)


def claims_found_payload(claims: list[ExtractedClaim]) -> dict[str, Any]:
    """Build ``claims_found`` for ``claims``, which extraction returns in article order.

    ``count`` is derived from the id list rather than counted separately, so the
    two can never disagree, and the payload is built through the generated
    :class:`~app.schema_models.ClaimsFoundEvent` so the wire format cannot drift
    from ``shared/schema.json``.
    """
    claim_ids = [claim.id for claim in claims]
    event = ClaimsFoundEvent(type="claims_found", count=len(claim_ids), claim_ids=claim_ids)
    return event.model_dump(mode="json")


async def check_claim(
    claim: ExtractedClaim,
    *,
    article_url: str,
    settings: Settings,
    deps: PipelineDeps,
) -> dict[str, Any]:
    """Run stages 2 to 5 for one claim and return the wire-ready claim dict.

    Retrieval never raises and both model stages already turn an unusable answer
    into an abstention, so what escapes this function is the deployment being
    wrong or a bug — see :func:`unverifiable_claim`, which is where those land.

    No passages means no model calls: stage 3 returns an empty list and stage 4
    answers ``unverifiable`` without asking anyone, so a claim the web has
    nothing on costs one retrieval and no tokens.
    """
    passages = await retrieve_passages(
        claim, article_url=article_url, providers=deps.providers, settings=settings
    )
    scored = await score_passages(claim, passages, client=deps.llm, settings=settings)
    judgement = await judge_claim(claim, scored, client=deps.llm, settings=settings)
    return aggregate(claim, scored, judgement, article_url=article_url, settings=settings)


def unverifiable_claim(claim: ExtractedClaim, *, article_url: str) -> dict[str, Any]:
    """The claim published when its check failed: ``unverifiable``, and honest about it.

    Carries no sources and no confidence (the two product invariants), an
    evidence sentence saying the search did not finish rather than one implying
    an empty web, and the same provenance trail an ``unverifiable`` claim always
    gets — built by :func:`~app.pipeline.aggregate.build_trail` from no passages,
    so the trail is real metadata about where the reader is rather than an
    invented chain.
    """
    model = Claim(
        id=claim.id,
        quote=claim.quote,
        start=claim.start,
        end=claim.end,
        verdict=Verdict(UNVERIFIABLE),
        confidence=None,
        evidence=FAILED_CLAIM_EVIDENCE,
        sources=[],
        trail=build_trail(verdict=UNVERIFIABLE, relied=[], usable=[], article_url=article_url),
    )
    payload: dict[str, Any] = model.model_dump(mode="json")
    return payload


async def _check_claims(
    redis: Redis,
    job_id: str,
    claims: list[ExtractedClaim],
    *,
    request: CheckRequest,
    settings: Settings,
    deps: PipelineDeps,
) -> tuple[list[dict[str, Any]], bool]:
    """Work every claim concurrently, publishing each as it resolves.

    Returns the finished claims **in article order** — the order they are cached
    and replayed in, which is what makes a cache hit render identically to the
    live run that produced it — and whether every one of them completed. The
    events themselves went out in whatever order the claims finished in, which is
    the point of doing this concurrently at all.

    Concurrency is bounded because ``max_claims`` claims, each fanning out to
    several providers and two model calls, all launched at once would hit
    provider rate limits and let one reader's check starve everyone else's.
    """
    if not claims:
        return [], True

    limit = settings.pipeline_concurrency
    if limit < 1:
        logger.warning(
            "PIPELINE_CONCURRENCY is %d, which would stall every check; using 1", limit
        )
        limit = 1
    slots = asyncio.Semaphore(limit)
    article_url = str(request.url)

    async def work(claim: ExtractedClaim) -> tuple[dict[str, Any], bool]:
        async with slots:
            try:
                payload = await check_claim(
                    claim, article_url=article_url, settings=settings, deps=deps
                )
                ok = True
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # One claim's outage is not the article's. The reader gets an
                # honest abstention for this one and real verdicts for the rest.
                logger.error(
                    "job %s: claim %s failed; publishing it as unverifiable (%s)",
                    job_id,
                    claim.id,
                    _safe_reason(exc),
                )
                payload = unverifiable_claim(claim, article_url=article_url)
                ok = False
        # Outside the semaphore: the slot belongs to the work, not to the wire,
        # and holding it across a publish would delay the next claim's start.
        #
        # The last gate before a reader. `validate_claim` raises, and that raise
        # is correct: a claim breaking a product rule is a failed job, never
        # something published and then apologised for.
        validate_claim(payload)
        await publish_event(redis, job_id, "claim", payload)
        return payload, ok

    # A task group rather than `gather`: if publishing one claim fails, or a rule
    # bug trips `validate_claim`, the run is over, and the sibling tasks must be
    # cancelled rather than left to publish claim events after the job's `error`.
    tasks: list[asyncio.Task[tuple[dict[str, Any], bool]]] = []
    async with asyncio.TaskGroup() as group:
        tasks = [group.create_task(work(claim)) for claim in claims]
    finished = [task.result() for task in tasks]
    return [payload for payload, _ in finished], all(ok for _, ok in finished)


def _log_totals(
    job_id: str, counts: dict[str, int], deps: PipelineDeps | None, elapsed: float
) -> None:
    """The one per-run line: what was checked, what it cost, how long it took.

    No article text, no quote, no URL and no install id — the job id ties this to
    the stage lines and to nothing else (privacy rule 6). The bill is reported as
    unknown rather than as zero when the caller's client was not metered, because
    a confident zero in a cost log is worse than an admitted gap.
    """
    meter = None if deps is None else deps.meter
    spend = (
        "llm_calls=unmetered tokens=unmetered"
        if meter is None
        else (
            f"llm_calls={meter.calls} tokens={meter.total_tokens} "
            f"(prompt={meter.prompt_tokens} completion={meter.completion_tokens})"
        )
    )
    logger.info(
        "job %s finished: claims=%d %s %s elapsed_ms=%.0f",
        job_id,
        sum(counts.values()),
        " ".join(f"{verdict}={count}" for verdict, count in counts.items()) or "counts=none",
        spend,
        elapsed * 1000,
    )


def _safe_reason(exc: BaseException) -> str:
    """Describe ``exc`` for a log line without risking untrusted content in it.

    The failures this pipeline expects — :class:`~app.llm.LLMError` and its
    subclasses, :class:`~app.config.MissingSettingError` — write their own
    messages out of the model id, the status code, the schema name and the
    environment variable, and each says so in its docstring. Those are exactly
    what an operator needs on the first live run ("provider returned 404"), and
    they carry no article text, so they are quoted in full.

    Anything else is reported by class name only, and no traceback is logged.
    An unexpected exception here is most likely a ``ValidationError``, whose
    message embeds the values that failed — for these models a quote from the
    article or the body of a retrieved passage. A log line is the wrong place for
    either (``CLAUDE.md`` rule 6), and a class name is enough to find the bug.

    An exception group carrying exactly one failure — what a
    :class:`asyncio.TaskGroup` raises when one claim's publish failed — is
    unwrapped, so the group envelope does not hide the one thing worth reading.
    """
    if isinstance(exc, BaseExceptionGroup) and len(exc.exceptions) == 1:
        return _safe_reason(exc.exceptions[0])
    if isinstance(exc, LLMError | MissingSettingError):
        return f"{type(exc).__name__}: {exc}"
    return type(exc).__name__


def _now_iso() -> str:
    """Current UTC time as an ISO 8601 instant, e.g. ``2026-08-31T04:15:09Z``."""
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


async def _publish_failure(redis: Redis, job_id: str) -> None:
    """Best-effort ``error`` event, so the stream always terminates.

    If Redis itself is what failed there is nowhere to put this, and the
    stream's own deadline is all that will close the reader's connection.
    """
    try:
        await publish_event(redis, job_id, "error", error_payload(ERROR_CODE, FAILURE_MESSAGE))
    except Exception:
        logger.exception("could not publish the error event for job %s", job_id)
