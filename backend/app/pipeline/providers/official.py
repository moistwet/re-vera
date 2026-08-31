"""Official data — data.gov.sg's dataset catalogue, for numeric claims only.

Consulted in addition to the fact-check/web-search chain when a claim is tagged
``numeric``, because a figure about Singapore is often checkable against the
agency that publishes it, and a general web search is not good at finding that
agency's dataset. It is never consulted for other claims: a request per claim is
a cost, and a dataset catalogue has nothing to say about a claim with no number
in it.

What this provider does and does not do
---------------------------------------
It **finds the official dataset**, and reports its title, its publisher, when it
was last updated and where it lives. It does **not** read a figure out of it. A
dataset is a table behind a resource endpoint; picking the right row and column
for an arbitrary claim is a research problem, and a wrong pick would put an
authoritative-looking number in front of a reader with an official logo beside
it — the most damaging thing this pipeline could produce.

What the judge gets is therefore a pointer, not a datum: *this figure is the
subject of an official series published by this agency, last updated then*. That
is genuinely useful context — it is what makes ``missing_context`` sayable about
an outdated figure — and it is honest about what was actually retrieved. Reading
values out of a resource is a real follow-up, and it needs its own verification
story before it is worth having.

The assumed wire shape
----------------------
**Never verified — no network here.** data.gov.sg has run more than one API over
the years (a CKAN-compatible ``/api/action/*`` and a newer ``api-open`` service),
so this is the likeliest single place in retrieval to need a fix. It is one
function.

Request::

    GET https://data.gov.sg/api/action/package_search?q=<keywords>&rows=<limit>

Response (200)::

    {
      "success": true,
      "result": {
        "count": 1,
        "results": [
          {
            "name": "hawker-centre-stall-rentals",
            "title": "Hawker Centre Stall Rentals",
            "notes": "Median monthly stall rental by hawker centre.",
            "metadata_modified": "2026-02-01T08:00:00.000Z",
            "organization": {"title": "National Environment Agency"},
            "url": "https://data.gov.sg/datasets/hawker-centre-stall-rentals"
          }
        ]
      }
    }

``success: false``, an empty ``results`` list, a 4xx and a 5xx are all the same
outcome: no official passage for this claim, no retry, on to the rest of
retrieval.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from app.pipeline.providers.base import (
    PROVIDER_TIMEOUT_SECONDS,
    AsyncHttpClient,
    clean_text,
    is_http_url,
    iso_date,
)
from app.pipeline.types import Passage

logger = logging.getLogger(__name__)

__all__ = ["DATA_GOV_SG_ENDPOINT", "DataGovSgProvider"]

DATA_GOV_SG_ENDPOINT = "https://data.gov.sg/api/action/package_search"
"""The dataset search endpoint. See the module docstring for the shape."""

DATASET_URL_TEMPLATE = "https://data.gov.sg/datasets/{name}"
"""Where a dataset lives when the payload gave no ``url`` of its own.

A reader can click a source chip, so the URL must lead somewhere real; if the
payload names no dataset either, the passage is dropped rather than pointed at a
guessed page."""

_SOLR_SPECIAL = re.compile(r'[+\-!(){}\[\]^"~*?:\\/&|]')
"""Characters that mean something to a Solr-backed ``q``.

A claim quote is prose, not a query language, and an unbalanced quotation mark
in it would turn a search into a 400. They are replaced with spaces: the words
are what we are searching for, and the punctuation was never doing any work
here."""

MAX_QUERY_CHARS = 200
"""Ceiling on the keyword string sent. A catalogue search is keyword matching;
a whole paragraph makes it worse, not better."""


@dataclass(frozen=True, slots=True)
class DataGovSgProvider:
    """Search data.gov.sg's dataset catalogue for a numeric claim.

    Needs no API key — the catalogue search is public — which is why it has no
    ``require_*`` call and no null-provider path of its own.

    Never raises: transport failure, non-2xx, non-JSON, ``success: false`` and an
    unexpected shape are all ``[]``.
    """

    http: AsyncHttpClient
    timeout: float = PROVIDER_TIMEOUT_SECONDS
    endpoint: str = DATA_GOV_SG_ENDPOINT

    async def search(self, query: str, *, limit: int) -> list[Passage]:
        """Return up to ``limit`` official-dataset passages for ``query``, or ``[]``.

        ``query`` is article text and is never logged (``CLAUDE.md`` rule 6).
        """
        keywords = _keywords(query)
        if not keywords or limit < 1:
            return []
        try:
            response = await self.http.get(
                self.endpoint,
                params={"q": keywords, "rows": str(limit)},
                timeout=self.timeout,
            )
        except Exception:
            logger.warning("official-data provider: request failed", exc_info=True)
            return []

        if not response.ok:
            logger.warning("official-data provider: HTTP %s", response.status_code)
            return []
        try:
            payload = response.json()
        except ValueError:
            logger.warning("official-data provider: response was not JSON")
            return []

        passages = _passages_from_payload(payload)[:limit]
        logger.info("official-data provider: %d dataset(s) kept", len(passages))
        return passages


def _keywords(query: str) -> str:
    """Reduce a claim quote to a catalogue-safe keyword string."""
    cleaned = _SOLR_SPECIAL.sub(" ", query)
    return " ".join(cleaned.split())[:MAX_QUERY_CHARS].strip()


def _passages_from_payload(payload: Any) -> list[Passage]:
    """Turn a ``package_search`` body into passages, skipping anything unusable."""
    if not isinstance(payload, dict) or payload.get("success") is False:
        return []
    result = payload.get("result")
    if not isinstance(result, dict):
        return []
    datasets = result.get("results")
    if not isinstance(datasets, list):
        return []

    passages: list[Passage] = []
    for dataset in datasets:
        passage = _passage_from_dataset(dataset)
        if passage is not None:
            passages.append(passage)
    return passages


def _passage_from_dataset(dataset: Any) -> Passage | None:
    """Build one passage from one catalogue entry, or ``None`` if unusable."""
    if not isinstance(dataset, dict):
        return None
    title = clean_text(dataset.get("title"), limit=200)
    if not title:
        return None

    url = dataset.get("url")
    if not (isinstance(url, str) and is_http_url(url)):
        name = dataset.get("name")
        if not isinstance(name, str) or not name.strip():
            return None
        url = DATASET_URL_TEMPLATE.format(name=name.strip())

    organization = dataset.get("organization")
    agency = ""
    if isinstance(organization, dict):
        agency = clean_text(organization.get("title"), limit=120) or clean_text(
            organization.get("name"), limit=120
        )
    outlet = agency or "data.gov.sg"
    updated = iso_date(dataset.get("metadata_modified"))
    notes = clean_text(dataset.get("notes"), limit=600)

    parts = [f'Official dataset on data.gov.sg: "{title}", published by {outlet}.']
    if updated:
        parts.append(f"Last updated {updated}.")
    if notes:
        parts.append(f"Dataset description: {notes}")

    return Passage(
        text=clean_text(" ".join(parts)),
        url=url,
        outlet=outlet,
        date=updated,
        wire=False,
        origin="official",
        rating=None,
    )
