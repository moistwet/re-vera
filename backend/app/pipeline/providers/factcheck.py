"""Google Fact Check Tools (ClaimReview) — the first provider, and the cheap one.

Tried before web search for every claim, and a hit here means web search is
never called for that claim (``docs/decisions.md`` §9). That short-circuit is
the largest single cost saving in the pipeline, and it is also the best
evidence available: a ClaimReview is a professional fact-checker's published
review of *this* claim, with a rating and a URL, rather than a page that happens
to contain similar words.

The assumed wire shape
----------------------
**Never verified against the live API — there is no ``GOOGLE_FACTCHECK_API_KEY``
and no route to Google in this environment.** This docstring is the single place
the guess is written down, so there is one place to fix when it turns out to be
wrong.

Request::

    GET https://factchecktools.googleapis.com/v1alpha1/claims:search
        ?query=<the claim, trimmed>
        &key=<GOOGLE_FACTCHECK_API_KEY>
        &languageCode=en
        &pageSize=<limit>

Response (200)::

    {
      "claims": [
        {
          "text": "Hawker stall rents will rise 40% next year.",
          "claimant": "social media posts",
          "claimDate": "2026-03-01T00:00:00Z",
          "claimReview": [
            {
              "publisher": {"name": "Example Fact Check", "site": "factcheck.example"},
              "url": "https://factcheck.example/hawker-rents",
              "title": "No, hawker rents are not rising 40%",
              "reviewDate": "2026-03-12T00:00:00Z",
              "textualRating": "False",
              "languageCode": "en"
            }
          ]
        }
      ]
    }

An empty result is ``{}`` or ``{"claims": []}`` — both are a normal miss, not an
error. A 4xx (bad key, quota) and a 5xx are both misses too: this provider never
retries, and a claim that finds no review simply goes on to web search.

What a ClaimReview is *not*
---------------------------
``textualRating`` is the publisher's own word — "False", "Mostly true",
"Pants on fire". It is carried on :attr:`~app.pipeline.types.Passage.rating` for
the judge and for aggregation's credibility rules, and it is **never** shown to
a reader as a verdict. Re-Vera has four verdicts and "Pants on fire" is not one
of them (``CLAUDE.md`` rule 1).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

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

__all__ = ["FACTCHECK_ENDPOINT", "GoogleFactCheckProvider"]

FACTCHECK_ENDPOINT = "https://factchecktools.googleapis.com/v1alpha1/claims:search"
"""The ClaimReview search endpoint. See the module docstring for the shape."""


@dataclass(frozen=True, slots=True)
class GoogleFactCheckProvider:
    """Look a claim up in the Google Fact Check Tools API.

    Holds the API key rather than reading it from settings, so that the key is
    handed over once, at the point where :func:`app.pipeline.retrieve.build_providers`
    has already decided it exists — and so this class needs no settings object to
    be constructed in a test.

    Never raises: a transport failure, a non-2xx, a body that is not JSON and a
    body of an unexpected shape are all "no reviews found", logged at warning
    and returned as ``[]``.
    """

    http: AsyncHttpClient
    api_key: str
    timeout: float = PROVIDER_TIMEOUT_SECONDS
    language_code: str = "en"

    async def search(self, query: str, *, limit: int) -> list[Passage]:
        """Return up to ``limit`` ClaimReview passages for ``query``, or ``[]``.

        The query is article text, so it is never logged (``CLAUDE.md`` rule 6);
        the log line carries the outcome and nothing that reconstructs it.
        """
        if not query.strip() or limit < 1:
            return []
        try:
            response = await self.http.get(
                FACTCHECK_ENDPOINT,
                params={
                    "query": query,
                    "key": self.api_key,
                    "languageCode": self.language_code,
                    "pageSize": str(limit),
                },
                timeout=self.timeout,
            )
        except Exception:
            # Timeout, DNS, connection reset. One dead provider must not fail the
            # claim: the caller carries on to web search.
            logger.warning("fact-check provider: request failed", exc_info=True)
            return []

        if not response.ok:
            # Includes 400/403 (bad or unauthorised key) and 429 (quota). No
            # retry, by the cost rule — the same request would be rejected again.
            logger.warning("fact-check provider: HTTP %s", response.status_code)
            return []

        try:
            payload = response.json()
        except ValueError:
            logger.warning("fact-check provider: response was not JSON")
            return []

        passages = _passages_from_payload(payload)
        logger.info("fact-check provider: %d review(s) kept", len(passages[:limit]))
        return passages[:limit]


def _passages_from_payload(payload: Any) -> list[Passage]:
    """Turn a ``claims:search`` body into passages, skipping anything unusable.

    Defensive at every level: the payload is third-party JSON and a missing or
    wrongly-typed field is a skipped review, never an exception. A review is
    usable only when it has a fetchable URL **and** a textual rating — a review
    with neither is not evidence of anything, and keeping it would spend stage 3
    and stage 4 tokens on nothing.
    """
    if not isinstance(payload, dict):
        return []
    claims = payload.get("claims")
    if not isinstance(claims, list):
        return []

    passages: list[Passage] = []
    for entry in claims:
        if not isinstance(entry, dict):
            continue
        claim_text = clean_text(entry.get("text"))
        claimant = clean_text(entry.get("claimant"), limit=120)
        reviews = entry.get("claimReview")
        if not isinstance(reviews, list):
            continue
        for review in reviews:
            passage = _passage_from_review(review, claim_text=claim_text, claimant=claimant)
            if passage is not None:
                passages.append(passage)
    return passages


def _passage_from_review(review: Any, *, claim_text: str, claimant: str) -> Passage | None:
    """Build one passage from one ``claimReview`` entry, or ``None`` if unusable."""
    if not isinstance(review, dict):
        return None
    url = review.get("url")
    if not isinstance(url, str) or not is_http_url(url):
        return None
    rating = clean_text(review.get("textualRating"), limit=120)
    if not rating:
        return None

    publisher = review.get("publisher")
    outlet = ""
    if isinstance(publisher, dict):
        outlet = clean_text(publisher.get("name"), limit=120) or clean_text(
            publisher.get("site"), limit=120
        )
    outlet = outlet or outlet_from_url(url)

    title = clean_text(review.get("title"), limit=300)
    text = _review_text(
        outlet=outlet, rating=rating, title=title, claim_text=claim_text, claimant=claimant
    )
    if not text:
        return None

    return Passage(
        text=text,
        url=url,
        outlet=outlet,
        date=iso_date(review.get("reviewDate")),
        wire=False,
        origin="factcheck",
        rating=rating,
        # Built entirely from the ClaimReview API's own structured fields
        # (rating, publisher, review text) — never fetched-and-guessed — so
        # this passage's text is verified by construction.
        provenance_verified=True,
    )


def _review_text(
    *, outlet: str, rating: str, title: str, claim_text: str, claimant: str
) -> str:
    """Compose the passage body the stance and judge models will read.

    Every part comes from the API — the reviewed claim, who made it, the
    publisher, its rating, its headline. Nothing is inferred and nothing is
    softened: a rating of "False" is reported as the publisher's word about the
    reviewed claim, and it is stage 4 and stage 5 that decide what that means for
    *our* claim, which may not be quite the same one.
    """
    parts = [f"{outlet} fact check."]
    if claim_text:
        attributed = f" (claimed by {claimant})" if claimant else ""
        parts.append(f'Reviewed claim{attributed}: "{claim_text}".')
    parts.append(f'{outlet} rates this claim "{rating}".')
    if title:
        parts.append(f"Review headline: {title}.")
    return clean_text(" ".join(parts))
