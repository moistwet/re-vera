"""Retrieval providers — the four places stage 2 looks for evidence.

Each provider is a small, replaceable adapter for one external service, sitting
behind a protocol from :mod:`app.pipeline.providers.base`:

* :class:`~app.pipeline.providers.factcheck.GoogleFactCheckProvider` —
  existing fact-check reviews (ClaimReview).
* :class:`~app.pipeline.providers.websearch.OpenAIWebSearchProvider` —
  general web search, the expensive one.
* :class:`~app.pipeline.providers.official.DataGovSgProvider` —
  official data, for numeric claims.
* :class:`~app.pipeline.providers.cited.LinkedCitationProvider` —
  the source a quotation points at.

The order they are consulted in, and the short-circuit that keeps a check
affordable, live in :mod:`app.pipeline.retrieve`, not here. A provider knows
about exactly one service and nothing about the pipeline.

Every one of them obeys the two rules in :mod:`app.pipeline.providers.base`:
it never raises into its caller, and it never retries.
"""

from __future__ import annotations

from app.pipeline.providers.base import (
    MAX_PASSAGE_CHARS,
    MAX_RESPONSE_CHARS,
    PROVIDER_TIMEOUT_SECONDS,
    AsyncHttpClient,
    CitedSourceProvider,
    FactCheckProvider,
    HttpResponse,
    HttpxClient,
    NullCitedSourceProvider,
    NullPassageProvider,
    OfficialDataProvider,
    Providers,
    RecordedHttpClient,
    RecordedRequest,
    SearchProvider,
    load_recorded_http,
)
from app.pipeline.providers.cited import LinkedCitationProvider
from app.pipeline.providers.factcheck import GoogleFactCheckProvider
from app.pipeline.providers.official import DataGovSgProvider
from app.pipeline.providers.websearch import OpenAIWebSearchProvider

__all__ = [
    "MAX_PASSAGE_CHARS",
    "MAX_RESPONSE_CHARS",
    "PROVIDER_TIMEOUT_SECONDS",
    "AsyncHttpClient",
    "CitedSourceProvider",
    "DataGovSgProvider",
    "FactCheckProvider",
    "GoogleFactCheckProvider",
    "HttpResponse",
    "HttpxClient",
    "LinkedCitationProvider",
    "NullCitedSourceProvider",
    "NullPassageProvider",
    "OfficialDataProvider",
    "OpenAIWebSearchProvider",
    "Providers",
    "RecordedHttpClient",
    "RecordedRequest",
    "SearchProvider",
    "load_recorded_http",
]
