"""Web search — the MVP's OpenAI built-in ``web_search`` tool, behind the protocol.

The most expensive step in the pipeline per claim, and the reason a ClaimReview
hit short-circuits it (``docs/decisions.md`` §9). It is consulted only when the
fact-check provider found nothing.

Why this file talks to OpenAI over plain HTTP
---------------------------------------------
``CLAUDE.md`` says one thin client wraps the OpenAI API and nothing else imports
the SDK. This module keeps that rule — it imports no SDK, only the shared
:class:`~app.pipeline.providers.base.AsyncHttpClient` — but it does reach the
same vendor, because :class:`app.llm.LLMClient` deliberately exposes exactly one
entry point (``structured``: chat completion, JSON schema, no tools) and the
built-in web search is a *tool call on the Responses API*. Widening ``LLMClient``
to carry tools would put a second, differently-shaped call path through the
module whose whole value is that it has one.

So the vendor-specific knowledge here is confined to this file, and the prompt
lives in ``app/prompts/websearch.md`` like every other prompt. If web search is
ever swapped for a search API (Brave, Serper, Bing), this file is what gets
replaced and nothing else moves — that is what :class:`SearchProvider` is for.
The alternative worth revisiting once a key exists: give ``app/llm.py`` a
``responses``-shaped transport and let this provider call *that*.

The assumed wire shape
----------------------
**Never verified: this environment has no ``OPENAI_API_KEY`` and no route to the
API.** One place to fix when it turns out to be wrong.

Request::

    POST https://api.openai.com/v1/responses
    Authorization: Bearer <OPENAI_API_KEY>
    {
      "model": "<mini-tier model>",
      "instructions": "<app/prompts/websearch.md body>",
      "input": "<<<CLAIM\\n<the claim>\\nCLAIM>>>\\nReturn at most N results.",
      "tools": [{"type": "web_search"}],
      "max_output_tokens": 1500
    }

Response (200)::

    {
      "output": [
        {"type": "web_search_call", "status": "completed"},
        {"type": "message",
         "content": [{"type": "output_text",
                      "text": "{\\"results\\": [...]}",
                      "annotations": [{"type": "url_citation",
                                       "url": "https://example.com/story",
                                       "title": "…"}]}]}
      ],
      "usage": {"input_tokens": 812, "output_tokens": 143}
    }

The answer is asked for as JSON *in the prompt* rather than through the
Responses API's ``text.format`` structured-output block. That is a deliberate
trade: the prompt-shaped ask is one fewer unverified request-shape assumption,
and a model that answers with prose instead of JSON costs us a parse failure and
an empty result — logged, and honestly ``unverifiable`` downstream — rather than
a 400 on every single claim. Revisit once a key exists and the shape can be
checked.

What a web passage actually IS
-------------------------------
Read this before trusting anything a :class:`~app.pipeline.types.Passage` built
here says. It is **model-summarised text**, not retrieved page content: the
search model reads whatever it read and writes a sentence about it, and that
sentence — not a quoted excerpt the tool itself extracted — is what becomes
``Passage.text``. Two independent things are checked before a reader ever sees
it, and both matter, because for a long time this module checked only the
first one and treated that as enough:

1. **The URL is one the search tool actually returned** (the citation gate,
   below). This says the model read *something*; it says nothing about whether
   it accurately reported what that something said.
2. **The text is confirmed to occur on the page at that URL** (provenance
   verification, added for BLOCKER B1 below, via
   :func:`~app.pipeline.types.span_occurs_in` against a page this module fetches
   itself). Only a passage that passes this has ``provenance_verified=True``.
   One that could not be checked — a timeout, a dead link, a non-HTML body —
   keeps ``provenance_verified=False`` and is *not* proof of anything wrong;
   :mod:`app.pipeline.aggregate` is responsible for not letting such a passage
   decide a verdict alone. One whose text was checked and is genuinely absent
   from the page is dropped outright, here, because that is not "unverified" —
   it is a caught fabrication.

The citation gate
------------------
A model asked to report search results can report results it never saw. When the
response carries ``url_citation`` annotations, any result whose URL is not among
them is **dropped**: the model claims to have read a page the search tool never
returned, and a fabricated URL under a real-looking quote is precisely the
failure this product exists to prevent. When there are no annotations at all
(the tool did not run), the whole answer is discarded.

This alone is not the correctness property the module name implies, and never
was: it verifies that a *real page* was involved, not that the *sentence
attributed to it* is what the page says. See "What a web passage actually IS"
above and "Provenance verification" below for the check that closes that gap.

Provenance verification (BLOCKER B1)
-------------------------------------
For every passage that survives the citation gate — already capped to at most
``limit`` (``settings.max_passages_per_claim``, 6 by default; cap happens
*before* any fetch, never after) — this module fetches the cited URL itself,
through the same :class:`AsyncHttpClient` seam every provider uses (the
cited-source provider, :mod:`app.pipeline.providers.cited`, does the same kind
of direct fetch for a different reason; this module writes its own minimal
HTML-to-text helper rather than importing that provider's private one, so the
two stay independently correct). ``Passage.text`` is compared against the
fetched page with :func:`~app.pipeline.types.span_occurs_in` — the same
function the judge stage's citation check uses, so "verified" means the same
thing everywhere in this pipeline.

Fetches are bounded (:data:`MAX_CONCURRENT_VERIFICATION_FETCHES` at a time),
individually timed out (:data:`VERIFICATION_TIMEOUT_SECONDS`) and never
retried — a slow or dead page is not fabrication, it is a passage this module
could not settle, and it is kept, unverified, rather than punished. See
:meth:`OpenAIWebSearchProvider._verify_provenance` for the full three-way
outcome and exactly what each one costs.

The unverified response shape, and how parsing fails
------------------------------------------------------
**This environment has no ``OPENAI_API_KEY`` and no route to the API, so the
Responses payload shape above (see "The assumed wire shape") has never been
checked against a live response — that is a documented assumption, not a
tested fact, and it is the one thing about this module most likely to be wrong
the first time it runs for real.** The parsing in
:func:`_output_text_and_citations` and :func:`_parse_results` is written
defensively against that uncertainty: a missing ``output`` key, an empty
``output`` list, no ``message`` item, no ``output_text`` part, no
``annotations`` at all, a differently-nested payload, or a non-2xx / non-JSON
error body all fall through to "no text, no citations" and the whole answer is
discarded as ``[]`` — logged at WARNING, never guessed at. Nothing in this
module tries to salvage a passage out of a shape it does not recognise; a
wrong guess here is a fabricated passage by another name.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import re
import time
from dataclasses import dataclass, field, replace
from typing import Any

from app.config import DEFAULT_MODEL
from app.llm import PromptError, load_prompt
from app.pipeline.providers.base import (
    PROVIDER_TIMEOUT_SECONDS,
    AsyncHttpClient,
    clean_text,
    domain_of,
    is_http_url,
    iso_date,
    outlet_from_url,
    url_key,
)
from app.pipeline.types import Passage, span_occurs_in

logger = logging.getLogger(__name__)

__all__ = ["RESPONSES_ENDPOINT", "OpenAIWebSearchProvider", "WebSearchStats"]

RESPONSES_ENDPOINT = "https://api.openai.com/v1/responses"
"""The Responses endpoint. See the module docstring for the assumed shape."""

MAX_OUTPUT_TOKENS = 1_500
"""Ceiling on one search answer. Six passages of a few sentences each, plus the
JSON around them, fits comfortably; anything longer is a model padding the list,
which the prompt forbids and this cap makes cheap."""

VERIFICATION_TIMEOUT_SECONDS = 4.0
"""Hard per-fetch ceiling for a provenance-verification GET (BLOCKER B1):
confirming that a web-search passage's text really appears on the page it
cites. Independent of :data:`~app.pipeline.providers.base.PROVIDER_TIMEOUT_SECONDS`
— that ceiling belongs to the ``search`` tool call itself. Deliberately short:
retrieval gives the *entire* ``search()`` call — the tool call and every
verification fetch it triggers — ``providers.timeout_seconds`` (10s by default,
:mod:`app.pipeline.retrieve`) to finish; a slow page must cost its own
verification, not the whole claim's evidence.
"""

MAX_CONCURRENT_VERIFICATION_FETCHES = 3
"""How many provenance-verification GETs run at once.

Bounded so a claim with a full ``limit``-worth of results (up to
``settings.max_passages_per_claim``, 6 by default) does not open six
connections simultaneously; not 1, so up to six individually-short timeouts do
not simply add up to six times :data:`VERIFICATION_TIMEOUT_SECONDS` end to end.
"""


@dataclass(slots=True)
class WebSearchStats:
    """How many web-search calls one :class:`OpenAIWebSearchProvider` instance has
    actually made. MAJOR M9: web search is the most expensive per-claim step,
    billed by OpenAI, and no cost assertion anywhere in the repo counted it —
    this is the counter that lets one exist.

    :func:`~app.pipeline.retrieve.build_providers` constructs one
    :class:`OpenAIWebSearchProvider` per pipeline run, so ``.calls`` on the
    instance a run used *is* that run's web-search count; whatever owns the run
    (:mod:`app.pipeline.run`, which this module does not own) can read it after
    the fact to log or assert a per-run total, the same way it already reads
    :class:`~app.llm.Usage` after each LLM call.

    ``calls`` increments the moment a request is actually issued
    (``self.http.post_json`` is reached) — a 4xx, a 5xx and a transport failure
    still spent a call for billing purposes, so they count. A query too short to
    send at all (empty after stripping, or ``limit < 1``) never reaches that
    line and is correctly not counted: it is exactly the kind of call the
    ClaimReview short-circuit in :mod:`app.pipeline.retrieve` exists to avoid
    paying for in the first place.
    """

    calls: int = 0


@dataclass(frozen=True, slots=True)
class OpenAIWebSearchProvider:
    """Search the web for a claim through the OpenAI built-in ``web_search`` tool.

    The model, unlike the three pipeline stages, has no ``OPENAI_MODEL_*``
    setting of its own — search is not one of the three stages
    ``docs/decisions.md`` §7 makes swappable — so it defaults to
    :data:`~app.config.DEFAULT_MODEL` and can be overridden per instance.
    Worth promoting to a setting the moment anyone wants to A/B it.

    Never raises: transport failure, non-2xx, non-JSON body, a model that
    answered with prose, a model that cited nothing, and a failed provenance
    fetch are all handled — either "no passages" (``[]``) or "this one passage
    stays unverified" — logged, never an exception into the caller.
    """

    http: AsyncHttpClient
    api_key: str
    model: str = DEFAULT_MODEL
    timeout: float = PROVIDER_TIMEOUT_SECONDS
    endpoint: str = RESPONSES_ENDPOINT
    prompt_name: str = "websearch"
    verification_timeout: float = VERIFICATION_TIMEOUT_SECONDS
    stats: WebSearchStats = field(default_factory=WebSearchStats)
    """MAJOR M9's exposed counter. See :class:`WebSearchStats`. Mutating a field
    on an otherwise-frozen dataclass is fine here: ``frozen`` stops this
    instance's own attributes from being reassigned, not the mutable object one
    of those attributes happens to hold — and this counter is one instance per
    provider, shared across every ``search()`` call that provider makes, which
    is the whole point."""

    async def search(self, query: str, *, limit: int) -> list[Passage]:
        """Return up to ``limit`` web passages for ``query``, or ``[]``.

        ``query`` is article text and is never logged (``CLAUDE.md`` rule 6).
        The log line carries what an LLM call must carry — prompt name and
        version, model, both token counts, latency — plus this stage's own
        cost signal (MAJOR M9): how many passages survived provenance
        verification, and how many calls this provider has now made in total.
        """
        if not query.strip() or limit < 1:
            return []
        try:
            prompt = load_prompt(self.prompt_name)
        except PromptError:
            # A packaging/authoring bug, not a runtime condition. Loud in the
            # log, but still not an exception into the caller: one broken
            # provider must not fail the claim.
            logger.exception("web search: prompt %r could not be loaded", self.prompt_name)
            return []

        body = {
            "model": self.model,
            "instructions": prompt.text,
            # The claim is fenced and labelled as data; the prompt tells the
            # model these markers delimit material, never instructions.
            "input": f"<<<CLAIM\n{query}\nCLAIM>>>\nReturn at most {limit} results.",
            "tools": [{"type": "web_search"}],
            "max_output_tokens": MAX_OUTPUT_TOKENS,
        }

        started = time.perf_counter()
        # Counted here, not after the response comes back: a rejected or
        # dropped request still went out over the wire and is still a call for
        # billing purposes (see WebSearchStats.calls). Short-circuited attempts
        # above (empty query, limit < 1) never reach this line.
        self.stats.calls += 1
        try:
            response = await self.http.post_json(
                self.endpoint,
                json_body=body,
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=self.timeout,
            )
        except Exception:
            logger.warning("web search: request failed", exc_info=True)
            return []
        latency_ms = (time.perf_counter() - started) * 1000

        if not response.ok:
            # 401/429/400 included. No retry: the cost rule, and a repeated
            # rejection is the same rejection billed twice.
            logger.warning("web search: HTTP %s", response.status_code)
            return []
        try:
            payload = response.json()
        except ValueError:
            logger.warning("web search: response was not JSON")
            return []

        text, citations = _output_text_and_citations(payload)
        prompt_tokens, completion_tokens = _usage(payload)
        candidates = _passages_from_answer(text, citations, limit=limit)
        passages = await self._verify_provenance(candidates)
        verified_count = sum(1 for passage in passages if passage.provenance_verified)
        logger.info(
            "web search: prompt=%s v%s model=%s prompt_tokens=%d completion_tokens=%d "
            "latency_ms=%.0f candidates=%d passages=%d verified=%d run_calls=%d",
            prompt.name,
            prompt.version,
            self.model,
            prompt_tokens,
            completion_tokens,
            latency_ms,
            len(candidates),
            len(passages),
            verified_count,
            self.stats.calls,
        )
        return passages

    async def _verify_provenance(self, candidates: list[Passage]) -> list[Passage]:
        """Confirm each candidate passage's text against the page it cites (BLOCKER B1).

        The citation gate (:func:`_passages_from_answer`) only checked that the
        *URL* was one the search tool actually returned; ``text`` next to it is
        free-form model output and was never checked against anything. This is
        the check that closes that gap.

        ``candidates`` is already capped to at most ``limit`` by the caller —
        cap first, fetch second, so this never fetches more pages than the
        claim was going to keep regardless of how it resolves. Each fetch goes
        through the same :class:`AsyncHttpClient` seam every provider uses, is
        bounded to :data:`MAX_CONCURRENT_VERIFICATION_FETCHES` at a time, gets
        its own :data:`VERIFICATION_TIMEOUT_SECONDS` hard ceiling, and is never
        retried (see :meth:`_verify_one` for the three-way outcome).
        """
        if not candidates:
            return candidates
        semaphore = asyncio.Semaphore(MAX_CONCURRENT_VERIFICATION_FETCHES)

        async def verify(passage: Passage) -> Passage | None:
            async with semaphore:
                return await self._verify_one(passage)

        settled = await asyncio.gather(*(verify(passage) for passage in candidates))
        return [passage for passage in settled if passage is not None]

    async def _verify_one(self, passage: Passage) -> Passage | None:
        """Fetch one passage's cited page and settle its ``provenance_verified``.

        Three outcomes, and only one of them drops the passage:

        * **Fetched, and the text is on the page** → ``provenance_verified=True``.
        * **Fetched, and the text is genuinely NOT on the page** → dropped
          (``None``). The fetch succeeded, so this is not a network hiccup —
          the model's summary does not appear on the page it cited, which is
          proof of fabrication, the exact failure BLOCKER B1 exists to catch.
        * **Could not be fetched or read at all** — timeout, transport error,
          non-2xx, a body that is not text/HTML, or a page with no extractable
          text — → kept with ``provenance_verified=False``. A failed fetch says
          nothing about whether the model told the truth; treating it as
          fabrication would punish a passage for a slow or blocked page.
          Aggregation is responsible for not letting an unverified passage
          decide a verdict by itself.

        Never raises (:class:`asyncio.CancelledError` aside — job shutdown, not
        a fetch failure). Never logs the passage text or the full URL
        (``CLAUDE.md`` rule 6): only the domain.
        """
        try:
            response = await asyncio.wait_for(
                self.http.get(passage.url, timeout=self.verification_timeout),
                timeout=self.verification_timeout,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.info(
                "web search: provenance fetch failed for %s; keeping unverified",
                domain_of(passage.url),
            )
            return passage
        if not response.ok:
            logger.info(
                "web search: provenance fetch got HTTP %s from %s; keeping unverified",
                response.status_code,
                domain_of(passage.url),
            )
            return passage
        content_type = response.headers.get("content-type", "")
        if (
            content_type
            and "html" not in content_type.lower()
            and "text" not in content_type.lower()
        ):
            logger.info(
                "web search: cited page from %s is not text/HTML; keeping unverified",
                domain_of(passage.url),
            )
            return passage
        page_text = _extract_text(response.text)
        if not page_text:
            return passage
        if span_occurs_in(passage.text, page_text):
            return replace(passage, provenance_verified=True)
        logger.warning(
            "web search: dropped a passage whose text did not appear on the page it "
            "cited (%s)",
            domain_of(passage.url),
        )
        return None


def _output_text_and_citations(payload: Any) -> tuple[str, set[str]]:
    """Pull the assistant's text and its cited URLs out of a Responses body.

    Walks ``output`` → message items → ``output_text`` parts, joining their text
    and collecting every ``url_citation`` annotation. Falls back to a top-level
    ``output_text`` convenience field if one is present. Anything unexpected is
    skipped rather than raising: this is third-party JSON.
    """
    if not isinstance(payload, dict):
        return "", set()

    chunks: list[str] = []
    citations: set[str] = set()

    output = payload.get("output")
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, dict) or item.get("type") != "message":
                continue
            content = item.get("content")
            if not isinstance(content, list):
                continue
            for part in content:
                if not isinstance(part, dict) or part.get("type") != "output_text":
                    continue
                if isinstance(part.get("text"), str):
                    chunks.append(part["text"])
                annotations = part.get("annotations")
                if not isinstance(annotations, list):
                    continue
                for annotation in annotations:
                    if not isinstance(annotation, dict):
                        continue
                    if annotation.get("type") != "url_citation":
                        continue
                    url = annotation.get("url")
                    if isinstance(url, str) and is_http_url(url):
                        citations.add(url_key(url))

    if not chunks and isinstance(payload.get("output_text"), str):
        chunks.append(payload["output_text"])
    return "\n".join(chunks), citations


def _usage(payload: Any) -> tuple[int, int]:
    """Token counts from a Responses body, or ``(0, 0)`` if it carried none."""
    if not isinstance(payload, dict):
        return 0, 0
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return 0, 0
    prompt_tokens = usage.get("input_tokens")
    completion_tokens = usage.get("output_tokens")
    return (
        prompt_tokens if isinstance(prompt_tokens, int) else 0,
        completion_tokens if isinstance(completion_tokens, int) else 0,
    )


def _passages_from_answer(text: str, citations: set[str], *, limit: int) -> list[Passage]:
    """Parse the model's JSON answer into passages, dropping anything uncited.

    Two gates, in order, and both of them exist because the passage text
    produced here is quoted to a reader as evidence:

    1. **The search tool must have run.** No ``url_citation`` annotations at all
       means the model answered from its own knowledge, which the prompt forbids
       and which is worthless as evidence. The whole answer is discarded.
    2. **Every result must point at a cited page.** A URL the search tool never
       returned is one the model invented.
    """
    results = _parse_results(text)
    if not results:
        return []
    if not citations:
        logger.warning("web search: answer carried no url_citation annotations; discarding it")
        return []

    passages: list[Passage] = []
    for result in results:
        if len(passages) >= limit:
            break
        if not isinstance(result, dict):
            continue
        url = result.get("url")
        if not isinstance(url, str) or not is_http_url(url):
            continue
        if url_key(url) not in citations:
            logger.warning("web search: dropped a result whose URL was never cited")
            continue
        body = clean_text(result.get("text"))
        if not body:
            continue
        passages.append(
            Passage(
                text=body,
                url=url,
                outlet=clean_text(result.get("outlet"), limit=120) or outlet_from_url(url),
                date=iso_date(result.get("date")),
                # Whether this is syndicated wire copy is decided by comparing it
                # with its neighbours, in retrieve.py — not by asking the model.
                wire=False,
                origin="web",
                rating=None,
            )
        )
    return passages


def _parse_results(text: str) -> list[Any]:
    """Parse ``{"results": [...]}`` out of the model's answer, forgivingly.

    Tolerates a Markdown code fence around the JSON (models add them even when
    told not to) and a bare list instead of the wrapper object. Tolerates
    nothing else: prose that merely *contains* something JSON-shaped is a failed
    answer, and guessing at it would be guessing at evidence.
    """
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[-1]
        if stripped.rstrip().endswith("```"):
            stripped = stripped.rstrip()[: -len("```")]
    stripped = stripped.strip()
    if not stripped:
        return []
    try:
        parsed = json.loads(stripped)
    except ValueError:
        logger.warning("web search: the model's answer was not the JSON object we asked for")
        return []
    if isinstance(parsed, dict):
        results = parsed.get("results")
        return results if isinstance(results, list) else []
    return parsed if isinstance(parsed, list) else []


_SCRIPT_OR_STYLE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.I | re.S)
_TAG = re.compile(r"<[^>]+>")


def _extract_text(markup: str) -> str:
    """Best-effort visible text of a fetched page, for provenance verification only.

    Not a general HTML-to-text pipeline, and deliberately not the one
    :mod:`app.pipeline.providers.cited` already has (its ``_strip_html`` is a
    private helper of a provider this module does not own — importing it would
    couple two providers' correctness to each other for no benefit here). All
    this needs is enough fidelity that a passage genuinely reproduced from the
    page still matches after :func:`~app.pipeline.types.normalize_for_match`'s
    typography folding: drop ``<script>``/``<style>`` blocks (their contents are
    never page text a reader sees), strip every remaining tag, unescape HTML
    entities, and collapse whitespace. Nothing here tries to be exact about
    layout or ordering — :func:`~app.pipeline.types.span_occurs_in` only needs
    the words to be there, in order, as flowing text.
    """
    without_scripts = _SCRIPT_OR_STYLE.sub(" ", markup)
    stripped_tags = _TAG.sub(" ", without_scripts)
    return html.unescape(" ".join(stripped_tags.split()))
