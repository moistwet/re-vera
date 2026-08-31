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

The anti-fabrication guard
--------------------------
A model asked to report search results can report results it never saw. When the
response carries ``url_citation`` annotations, any result whose URL is not among
them is **dropped**: the model claims to have read a page the search tool never
returned, and a fabricated URL under a real-looking quote is precisely the
failure this product exists to prevent. When there are no annotations at all
(the tool did not run), the whole answer is discarded.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from app.config import DEFAULT_MODEL
from app.llm import PromptError, load_prompt
from app.pipeline.providers.base import (
    PROVIDER_TIMEOUT_SECONDS,
    AsyncHttpClient,
    clean_text,
    is_http_url,
    iso_date,
    outlet_from_url,
)
from app.pipeline.types import Passage

logger = logging.getLogger(__name__)

__all__ = ["RESPONSES_ENDPOINT", "OpenAIWebSearchProvider"]

RESPONSES_ENDPOINT = "https://api.openai.com/v1/responses"
"""The Responses endpoint. See the module docstring for the assumed shape."""

MAX_OUTPUT_TOKENS = 1_500
"""Ceiling on one search answer. Six passages of a few sentences each, plus the
JSON around them, fits comfortably; anything longer is a model padding the list,
which the prompt forbids and this cap makes cheap."""


@dataclass(frozen=True, slots=True)
class OpenAIWebSearchProvider:
    """Search the web for a claim through the OpenAI built-in ``web_search`` tool.

    The model, unlike the three pipeline stages, has no ``OPENAI_MODEL_*``
    setting of its own — search is not one of the three stages
    ``docs/decisions.md`` §7 makes swappable — so it defaults to
    :data:`~app.config.DEFAULT_MODEL` and can be overridden per instance.
    Worth promoting to a setting the moment anyone wants to A/B it.

    Never raises: transport failure, non-2xx, non-JSON body, a model that
    answered with prose, and a model that cited nothing are all "no passages",
    logged and returned as ``[]``.
    """

    http: AsyncHttpClient
    api_key: str
    model: str = DEFAULT_MODEL
    timeout: float = PROVIDER_TIMEOUT_SECONDS
    endpoint: str = RESPONSES_ENDPOINT
    prompt_name: str = "websearch"

    async def search(self, query: str, *, limit: int) -> list[Passage]:
        """Return up to ``limit`` web passages for ``query``, or ``[]``.

        ``query`` is article text and is never logged (``CLAUDE.md`` rule 6).
        The log line carries what an LLM call must carry — prompt name and
        version, model, both token counts, latency — and nothing else.
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
        passages = _passages_from_answer(text, citations, limit=limit)
        logger.info(
            "web search: prompt=%s v%s model=%s prompt_tokens=%d completion_tokens=%d "
            "latency_ms=%.0f passages=%d",
            prompt.name,
            prompt.version,
            self.model,
            prompt_tokens,
            completion_tokens,
            latency_ms,
            len(passages),
        )
        return passages


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
                        citations.add(_url_key(url))

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
        if _url_key(url) not in citations:
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


def _url_key(url: str) -> str:
    """Comparison key for two URLs: host without ``www.``, plus path without a
    trailing slash, lower-cased. Query strings and fragments are ignored so that
    a tracking parameter on one copy of a link does not make it a different page.
    """
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    host = host[4:] if host.startswith("www.") else host
    path = parts.path.rstrip("/").lower()
    return f"{host}{path}"
