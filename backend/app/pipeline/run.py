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

**One claim failing is not a failed job.** Retrieval itself still never raises
(:func:`~app.pipeline.retrieve.retrieve_passages`, per its own docstring), and
stages 3 and 4 turn a malformed model answer into an honest abstention by
themselves; what still reaches here is retrieval coming back *broken* rather
than empty (:attr:`~app.pipeline.retrieve.RetrievalOutcome.retrieval_broken` —
:func:`check_claim` raises :class:`RetrievalFailedError` for that), the
deployment being wrong (a bad key, a model this account cannot call), or a bug.
Any of those is caught per claim, published as ``unverifiable`` with an
evidence sentence that says the check did not finish, and the run carries on.
Only a failure *before* the claims are known — extraction itself, or a missing
key — ends the job with ``error``.

Two decisions worth knowing, both about honesty rather than mechanism:

* **A run in which nothing succeeded is not cached; a run with only some
  claims failing is.** (BLOCKER B5, MAJOR M14 — see :class:`_ClaimBatch` and
  :attr:`_ClaimBatch.cacheable` for the reasoning in full.) An entry lives for
  seven days and is served to every later reader of that URL without
  re-running anything, so freezing a total outage into it — "the search
  provider's key is bad, so every claim came back honestly unable to check
  itself" — would tell every subsequent reader the same false-feeling nothing
  for a week. But a *partial* failure — six claims judged normally, one lost
  to a flaky provider or a stage bug — has real value in the other six, and
  refusing to cache it would re-buy that evidence for every reader until the
  URL happens to check clean, which is a bigger and more common cost than the
  one honest abstention it protects. We cache results with real work in them,
  never a run that produced nothing but abstentions.
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
    "RetrievalFailedError",
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
not a sentence implying we looked and the web was empty. This is exactly the
wording BLOCKER B5 asks for: it never claims a completed search, which is what
made the previous, silently-swallowed failure mode possible in the first
place — see :class:`RetrievalFailedError`.
"""


class RetrievalFailedError(RuntimeError):
    """Raised by :func:`check_claim` when retrieval for a claim is *broken*, not empty.

    See :attr:`~app.pipeline.retrieve.RetrievalOutcome.retrieval_broken`: every
    provider actually consulted for this claim raised, timed out, or returned
    garbage, and nothing was retrieved. That is not "the web has nothing on
    this" — it is "the search did not happen" — and BLOCKER B5 is exactly the
    bug this distinction exists to close: before it, a total provider outage
    (an expired key, a wrong tool name, a quota block) was silently reported to
    a reader as a completed, empty search, and the resulting all-``unverifiable``
    run was written to the 7-day cache as if it were a real answer.

    A distinct type rather than reusing an existing one so that it stays
    recognisable through ``_check_claims.work``'s ``except Exception`` (which
    catches it exactly like any other stage failure and publishes the same
    honest :data:`FAILED_CLAIM_EVIDENCE` abstention) and through
    :func:`_safe_reason` (so an operator's log line names which providers
    failed, not just "RetrievalFailedError"). The message names only provider
    labels (``"fact-check"``, ``"web-search"``, …) and the claim id — never
    article text, a quote or a URL (``CLAUDE.md`` rule 6).
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

    ``owned_openai_client`` is the same idea for the LLM side (MAJOR M12): every
    job built production dependencies without this used to leak one
    ``openai.AsyncOpenAI`` and its connection pool, because only ``owned_http``
    was ever closed. Typed ``Any`` rather than ``openai.AsyncOpenAI`` on
    purpose — this repository's rule that only :mod:`app.llm` imports the
    OpenAI SDK (``tests/test_llm.py::test_only_the_llm_module_imports_the_openai_sdk``)
    would otherwise be broken by this module needing the symbol just to type a
    field. :mod:`app.llm` does not expose a way to close what
    :func:`~app.llm.build_openai_transport` opens
    (:class:`~app.llm.OpenAIChatTransport` has no ``aclose``/``close``), so
    :meth:`build` reaches into its private ``_client`` once, at construction
    time, rather than leaking the client for the life of the process. This is a
    workaround, not the fix: ``app.llm`` should grow a public way to close what
    it opens, and :meth:`aclose` here should be simplified once it does — noted
    in this milestone's interface notes since ``app/llm.py`` is outside this
    module's owned files.
    """

    llm: LLMClient
    providers: Providers
    meter: LLMMeter | None = None
    owned_http: HttpxClient | None = None
    owned_openai_client: Any | None = None

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

        The returned object owns an HTTP client and an OpenAI client; call
        :meth:`aclose` when the job ends.
        """
        api_key = settings.require_openai_api_key("the claim-checking pipeline")
        meter = LLMMeter()
        http = HttpxClient()
        raw_transport = build_openai_transport(api_key, settings.llm_timeout_seconds)
        return cls(
            llm=LLMClient(
                api_key=api_key,
                timeout=settings.llm_timeout_seconds,
                max_retries=settings.llm_max_retries,
                transport=meter.instrument(raw_transport),
            ),
            providers=build_providers(settings, http=http),
            meter=meter,
            owned_http=http,
            # See the class docstring: reaching into a private attribute
            # because app.llm exposes no public way to get the client back out
            # of the transport it just built, or to close it.
            owned_openai_client=getattr(raw_transport, "_client", None),
        )

    async def aclose(self) -> None:
        """Close whatever this object opened. A no-op for injected dependencies.

        Both clients are closed even if one of them errors — a fake client
        raising on close in a test must never leave the real HTTP pool open,
        and vice versa — and neither ever raises out of here: this runs from
        ``run_pipeline``'s ``finally`` (MAJOR M12: "on every path, including
        the error path and cancellation"), where an exception would either
        mask whichever failure the job is already reporting or, if none is in
        flight, escape the pipeline entirely and leave the reader's stream
        exactly as hung as an unclosed connection pool would. Cancellation
        itself is the one thing still allowed through, so a shutdown is not
        blocked waiting on a socket that will not close.
        """
        if self.owned_http is not None:
            try:
                await self.owned_http.aclose()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("failed to close the HTTP client cleanly", exc_info=True)
        if self.owned_openai_client is not None:
            close = getattr(self.owned_openai_client, "close", None)
            if callable(close):
                try:
                    await close()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.warning("failed to close the OpenAI client cleanly", exc_info=True)


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

        batch = await _check_claims(
            redis, job_id, claims, request=request, settings=settings, deps=deps
        )

        counts = tally(batch.results)
        checked_at = _now_iso()
        if batch.cacheable:
            await set_check(
                redis,
                # AnyUrl normalises, so hash the string form — the cache read in
                # `start_check` and this write must agree on one spelling.
                str(request.url),
                {"claims": batch.results, "counts": counts, "checked_at": checked_at},
            )
            if batch.ok_count < batch.total:
                logger.info(
                    "job %s: cached despite %d/%d claim(s) failing — the rest is real "
                    "evidence, and one idiosyncratic failure should not force every claim "
                    "to be re-bought on the next reader's visit (MAJOR M14)",
                    job_id,
                    batch.total - batch.ok_count,
                    batch.total,
                )
        else:
            logger.info(
                "job %s: not cached — every claim's check failed (%d/%d); a seven-day "
                "entry made entirely of abstentions looks like a real answer but is "
                "almost certainly an outage, and would serve that outage to every later "
                "reader (BLOCKER B5)",
                job_id,
                batch.total - batch.ok_count,
                batch.total,
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

    Retrieval never raises, and both model stages already turn an unusable
    answer into an abstention, so what escapes this function is retrieval
    coming back *broken* (:class:`RetrievalFailedError`, when
    :attr:`~app.pipeline.retrieve.RetrievalOutcome.retrieval_broken` is true —
    BLOCKER B5), the deployment being wrong, or a bug — see
    :func:`unverifiable_claim`, which is where all of those land.

    No passages means no model calls: stage 3 returns an empty list and stage 4
    answers ``unverifiable`` without asking anyone, so a claim the web has
    nothing on costs one retrieval and no tokens. A claim whose retrieval is
    broken costs even less: it never reaches stage 3 at all.
    """
    outcome = await retrieve_passages(
        claim, article_url=article_url, providers=deps.providers, settings=settings
    )
    if outcome.retrieval_broken:
        # Every provider we actually asked about this claim failed outright —
        # not "the web has nothing", but "the search did not happen". Raising
        # here (rather than letting an empty passage list flow into an honest
        # "nothing found" verdict) is BLOCKER B5's fix: it is caught the same
        # way every other stage failure is, in `_check_claims.work`, which
        # marks the claim `ok=False` and publishes it with wording that says
        # the search did not finish rather than that it finished and found
        # nothing.
        raise RetrievalFailedError(
            f"claim {claim.id}: every provider consulted failed "
            f"({', '.join(outcome.providers_failed)}); nothing was retrieved"
        )
    scored = await score_passages(claim, outcome.passages, client=deps.llm, settings=settings)
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


@dataclass(frozen=True, slots=True)
class _ClaimBatch:
    """What one job's fan-out over its claims produced, and whether it should be cached.

    ``results`` is every claim, already validated and published, **in article
    order** — the order they are cached and replayed in, which is what makes a
    cache hit render identically to the live run that produced it. The events
    themselves went out in whatever order the claims finished in; that is the
    point of doing this concurrently at all.
    """

    results: list[dict[str, Any]]
    ok_count: int
    total: int

    @property
    def cacheable(self) -> bool:
        """Whether this run belongs in the 7-day cache. See BLOCKER B5 and MAJOR M14.

        **Never cache a run in which nothing succeeded.** For a non-empty claim
        list, ``ok_count == 0`` means every claim ended in a failure — whatever
        mix of reasons produced them, a result made entirely of "could not be
        checked" claims is indistinguishable from "the pipeline itself is
        broken right now" (a bad key, an outage, a bug hit on every claim), and
        freezing that for seven days is exactly BLOCKER B5's trap: the next
        reader of the URL is told the same failure as if it were a real,
        completed answer.

        **Cache whenever at least one claim genuinely succeeded.** The run then
        has real value — real evidence, a real verdict — for most of its
        claims, and MAJOR M14 is precisely the demand that one idiosyncratic
        failure (a flaky provider on one query, a stage bug on one claim) must
        not force every other claim's evidence to be re-bought on the next
        reader's visit. The failed claim is cached too, honestly reporting that
        it could not be checked (:data:`FAILED_CLAIM_EVIDENCE`) — which can go
        stale before the next attempt, but is never false, so caching it is the
        safe direction this reconciliation asks for.

        An empty claim list (nothing to check) is trivially cacheable — there
        is nothing that could have failed.
        """
        return self.total == 0 or self.ok_count > 0


async def _check_claims(
    redis: Redis,
    job_id: str,
    claims: list[ExtractedClaim],
    *,
    request: CheckRequest,
    settings: Settings,
    deps: PipelineDeps,
) -> _ClaimBatch:
    """Work every claim concurrently, publishing each as it resolves.

    Returns a :class:`_ClaimBatch` holding the finished claims **in article
    order** and how many of them succeeded, which is what
    :attr:`_ClaimBatch.cacheable` needs to decide the run's caching policy.

    Concurrency is bounded because ``max_claims`` claims, each fanning out to
    several providers and two model calls, all launched at once would hit
    provider rate limits and let one reader's check starve everyone else's.
    """
    if not claims:
        return _ClaimBatch(results=[], ok_count=0, total=0)

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
    return _ClaimBatch(
        results=[payload for payload, _ in finished],
        ok_count=sum(1 for _, ok in finished if ok),
        total=len(finished),
    )


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
    subclasses, :class:`~app.config.MissingSettingError`,
    :class:`RetrievalFailedError` — write their own messages out of the model
    id, the status code, the schema name, the environment variable, or (for
    :class:`RetrievalFailedError`) the claim id and the provider names that
    failed, and each says so in its docstring. Those are exactly what an
    operator needs on the first live run ("provider returned 404", or "every
    provider consulted failed (fact-check, web-search)" — the shape a
    misconfigured key takes, per BLOCKER B5), and they carry no article text,
    so they are quoted in full.

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
    if isinstance(exc, LLMError | MissingSettingError | RetrievalFailedError):
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
