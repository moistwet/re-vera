"""The four retrieval providers, each against a recorded payload. No network.

Every test here replays a hand-written fixture through
:class:`~app.pipeline.providers.base.RecordedHttpClient`, which never opens a
socket and which records the requests it was given — because half of what these
providers owe is about the request (the right endpoint, the key, the query sent
once) and not only about what comes back.

Two properties are asserted over and over, deliberately:

* **A provider never raises.** A transport failure, a 4xx, a 5xx, a body that is
  not JSON and a body of the wrong shape are all ``[]``. One dead service must
  not fail a whole check.
* **A provider never retries.** After a rejection the request count is exactly
  one. Retrieval is the dominant per-claim cost and a repeated rejection is the
  same rejection, billed twice (``CLAUDE.md`` cost rules).

The fixtures are inventions, not captures: this environment has no API key and
no route to any of these services. See ``tests/fixtures/retrieve/README.md``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.llm import load_prompt
from app.pipeline.providers.base import (
    HttpResponse,
    NullCitedSourceProvider,
    NullPassageProvider,
    RecordedHttpClient,
    clean_text,
    domain_of,
    is_http_url,
    iso_date,
    load_recorded_http,
    outlet_from_url,
)
from app.pipeline.providers.cited import LinkedCitationProvider
from app.pipeline.providers.factcheck import FACTCHECK_ENDPOINT, GoogleFactCheckProvider
from app.pipeline.providers.official import DataGovSgProvider
from app.pipeline.providers.websearch import RESPONSES_ENDPOINT, OpenAIWebSearchProvider

FIXTURES = Path(__file__).parent / "fixtures" / "retrieve"
"""Recorded provider answers. Every one of them is fictional."""

ARTICLE_URL = "https://news.example/hawker-rents"
"""The fictional page ``cited_article.html`` stands for."""

ATTRIBUTION_QUOTE = "The agency said the median stall rental adjustment would be 4 per cent."
"""An attribution claim's quote, matching the link in ``cited_article.html``."""


def recorded(name: str) -> HttpResponse:
    """Load one recorded JSON answer from ``tests/fixtures/retrieve``."""
    return load_recorded_http(FIXTURES / name)


def html_page(name: str, url: str) -> HttpResponse:
    """Wrap one fixture HTML file as a 200 ``text/html`` response."""
    return HttpResponse(
        status_code=200,
        text=(FIXTURES / name).read_text(encoding="utf-8"),
        url=url,
        headers={"content-type": "text/html; charset=utf-8"},
    )


WEBSEARCH_PASSAGE_1 = (
    "The median stall rental adjustment for the coming year is 4 per cent, the agency "
    "said in a written reply."
)
WEBSEARCH_URL_1 = "https://gov.example/press/stall-rental-adjustment"
WEBSEARCH_PASSAGE_2 = (
    "The agency put the median adjustment at 4 per cent, well below the 40 per cent "
    "circulating online."
)
WEBSEARCH_URL_2 = "https://news.example/hawker-rent-adjustment"
WEBSEARCH_PASSAGE_3 = (
    "Stallholders at three centres said they had been told of an increase at a "
    "briefing last month."
)
WEBSEARCH_URL_3 = "https://daily.example/hawkers-briefing"
"""The three (URL, text) pairs ``websearch_results.json`` carries, spelled out
so provenance-verification fixtures below can be built to genuinely agree —
or, deliberately, disagree — with them."""


def verified_page(
    text: str, url: str, *, content_type: str = "text/html; charset=utf-8"
) -> HttpResponse:
    """A GET response for BLOCKER B1's provenance-verification fetch.

    Unlike ``html_page`` (which loads a fixture file for the cited-source
    provider's link-following), this builds the body inline so a test can say
    in one place exactly what page text a passage is being checked against —
    the whole point of the tests that use it.
    """
    return HttpResponse(
        status_code=200,
        text=f"<html><body><p>{text}</p></body></html>",
        url=url,
        headers={"content-type": content_type},
    )


def responses_payload(results: list[dict[str, Any]], citations: list[str]) -> HttpResponse:
    """Build a Responses answer carrying ``results`` and citing ``citations``.

    Used for the cases a fixture cannot show honestly — a model citing nothing,
    or reporting a page the search tool never returned.
    """
    return HttpResponse(
        status_code=200,
        text=json.dumps(
            {
                "output": [
                    {"type": "web_search_call", "status": "completed"},
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                "text": json.dumps({"results": results}),
                                "annotations": [
                                    {"type": "url_citation", "url": url} for url in citations
                                ],
                            }
                        ],
                    },
                ],
                "usage": {"input_tokens": 10, "output_tokens": 20},
            }
        ),
        url=RESPONSES_ENDPOINT,
    )


# ---------------------------------------------------------------- shared helpers


def test_iso_date_normalises_timestamps_and_refuses_guesses() -> None:
    """A date reaches a reader on a source chip, so anything doubtful is ``None``."""
    assert iso_date("2026-03-12") == "2026-03-12"
    assert iso_date("2026-03-12T09:00:00Z") == "2026-03-12"
    assert iso_date("2026-03-12T09:00:00+08:00") == "2026-03-12"
    assert iso_date("2026") is None
    assert iso_date("12 March 2026") is None
    assert iso_date(None) is None
    assert iso_date(20260312) is None


def test_url_helpers() -> None:
    """``www.`` is not part of a domain, and only http(s) URLs are fetchable."""
    assert domain_of("https://www.CNA.example/story") == "cna.example"
    assert domain_of("not a url") == ""
    assert is_http_url("https://example.com/a")
    assert not is_http_url("javascript:alert(1)")
    assert not is_http_url("/relative/path")
    assert outlet_from_url("https://gov.example/press/x") == "gov.example"


def test_clean_text_collapses_and_caps() -> None:
    """Passage text is billed by the token; indentation is not evidence."""
    assert clean_text("  a\n\n  b  ") == "a b"
    assert len(clean_text("x " * 5000)) <= 1200
    assert clean_text(None) == ""


async def test_null_providers_return_nothing() -> None:
    """An unconfigured provider is empty, not broken."""
    assert await NullPassageProvider(reason="no key").search("q", limit=3) == []
    assert await NullCitedSourceProvider(reason="no key").fetch(
        "q", article_url=ARTICLE_URL, limit=3
    ) == []


async def test_recorded_client_refuses_an_unscripted_request() -> None:
    """An unexpected request is the bug the fixture exists to catch — and money."""
    http = RecordedHttpClient([])
    with pytest.raises(AssertionError):
        await http.get("https://example.com", timeout=1.0)


# ---------------------------------------------------------------- fact check


async def test_factcheck_parses_a_review_and_skips_one_without_a_rating() -> None:
    """A ClaimReview is only evidence when it has a URL *and* a rating."""
    http = RecordedHttpClient([recorded("factcheck_hit.json")])
    provider = GoogleFactCheckProvider(http=http, api_key="test-key")

    passages = await provider.search("hawker stall rents 40%", limit=6)

    assert len(passages) == 1
    passage = passages[0]
    assert passage.origin == "factcheck"
    assert passage.rating == "False"
    assert passage.outlet == "Example Fact Check SG"
    assert passage.url == "https://factcheck.example/hawker-stall-rents-40-percent"
    assert passage.date == "2026-03-12"
    assert passage.wire is False
    assert "rates this claim" in passage.text


async def test_factcheck_sends_the_key_and_the_query_to_the_documented_endpoint() -> None:
    """The request shape is the part of this provider nobody can verify live."""
    http = RecordedHttpClient([recorded("factcheck_empty.json")])
    provider = GoogleFactCheckProvider(http=http, api_key="test-key")

    assert await provider.search("some claim", limit=4) == []

    assert len(http.requests) == 1
    request = http.requests[0]
    assert request.method == "GET"
    assert request.url == FACTCHECK_ENDPOINT
    assert request.params == {
        "query": "some claim",
        "key": "test-key",
        "languageCode": "en",
        "pageSize": "4",
    }


async def test_factcheck_caps_at_the_limit() -> None:
    """``limit`` is a ceiling on what leaves the provider, not only on what is asked."""
    http = RecordedHttpClient([recorded("factcheck_hit.json")])
    provider = GoogleFactCheckProvider(http=http, api_key="test-key")

    assert await provider.search("hawker stall rents", limit=0) == []
    assert http.requests == []


@pytest.mark.parametrize("status", [400, 403, 429, 500])
async def test_factcheck_treats_every_failure_as_a_miss_and_never_retries(status: int) -> None:
    """A bad key, a quota and an outage are all "no reviews" — for exactly one call."""
    http = RecordedHttpClient([HttpResponse(status_code=status, text="{}", url=FACTCHECK_ENDPOINT)])
    provider = GoogleFactCheckProvider(http=http, api_key="test-key")

    assert await provider.search("claim", limit=3) == []
    assert len(http.requests) == 1


async def test_factcheck_survives_a_body_that_is_not_json() -> None:
    """A captive portal's HTML must not take the claim down with it."""
    http = RecordedHttpClient([recorded("factcheck_not_json.json")])
    provider = GoogleFactCheckProvider(http=http, api_key="test-key")

    assert await provider.search("claim", limit=3) == []


async def test_factcheck_survives_a_transport_failure() -> None:
    """A timeout is a missing passage, never an exception into retrieval."""
    http = RecordedHttpClient([TimeoutError("factchecktools took too long")])
    provider = GoogleFactCheckProvider(http=http, api_key="test-key")

    assert await provider.search("claim", limit=3) == []


async def test_factcheck_survives_an_unexpected_payload_shape() -> None:
    """The one assumption that cannot be checked here is the shape; it must not crash."""
    http = RecordedHttpClient(
        [HttpResponse(status_code=200, text='{"claims": "not a list"}', url=FACTCHECK_ENDPOINT)]
    )
    provider = GoogleFactCheckProvider(http=http, api_key="test-key")

    assert await provider.search("claim", limit=3) == []


# ---------------------------------------------------------------- web search


async def test_websearch_parses_cited_results() -> None:
    """The happy path: the tool ran, the answer was JSON, every URL was cited,
    and every passage's text is confirmed to actually be on the page it cites."""
    http = RecordedHttpClient(
        [
            recorded("websearch_results.json"),
            verified_page(WEBSEARCH_PASSAGE_1, WEBSEARCH_URL_1),
            verified_page(WEBSEARCH_PASSAGE_2, WEBSEARCH_URL_2),
            verified_page(WEBSEARCH_PASSAGE_3, WEBSEARCH_URL_3),
        ]
    )
    provider = OpenAIWebSearchProvider(http=http, api_key="test-key")

    passages = await provider.search("hawker stall rents 40%", limit=6)

    assert [passage.url for passage in passages] == [
        WEBSEARCH_URL_1,
        WEBSEARCH_URL_2,
        WEBSEARCH_URL_3,
    ]
    assert all(passage.origin == "web" for passage in passages)
    assert all(passage.wire is False for passage in passages)
    # BLOCKER B1: every passage's text was fetched and confirmed, not merely
    # cited — this is what makes them fit to decide a verdict on their own.
    assert all(passage.provenance_verified for passage in passages)
    assert passages[0].date == "2026-03-12"
    assert passages[1].outlet == "Example News"
    # The model gave no date for the third; a guessed one would be a fabricated fact.
    assert passages[2].date is None
    # One POST to the search tool, one GET per passage to verify it.
    assert len(http.requests) == 4


async def test_websearch_request_carries_the_prompt_the_tool_and_the_key() -> None:
    """The assumed request shape, written down once here and once in the module."""
    http = RecordedHttpClient(
        [
            recorded("websearch_results.json"),
            verified_page(WEBSEARCH_PASSAGE_1, WEBSEARCH_URL_1),
            verified_page(WEBSEARCH_PASSAGE_2, WEBSEARCH_URL_2),
            verified_page(WEBSEARCH_PASSAGE_3, WEBSEARCH_URL_3),
        ]
    )
    provider = OpenAIWebSearchProvider(http=http, api_key="test-key", model="test-model")

    await provider.search("hawker stall rents", limit=3)

    request = http.requests[0]
    assert request.method == "POST"
    assert request.url == RESPONSES_ENDPOINT
    assert request.headers["Authorization"] == "Bearer test-key"
    assert request.json_body is not None
    assert request.json_body["model"] == "test-model"
    assert request.json_body["tools"] == [{"type": "web_search"}]
    assert request.json_body["instructions"] == load_prompt("websearch").text
    # The claim travels as fenced data in the user input, never inside the prompt.
    assert "hawker stall rents" in request.json_body["input"]
    assert "hawker stall rents" not in request.json_body["instructions"]


async def test_websearch_honours_the_limit() -> None:
    """Passages cost tokens in stages 3 and 4; the cap is a budget, not a preference.

    It is also BLOCKER B1's "cap first, fetch second" budget: only the two
    results ``limit`` keeps get a provenance-verification fetch at all — the
    third scripted GET below would trip
    ``RecordedHttpClient``'s "ran out of scripted outcomes" guard if it were
    ever requested, so its absence from the request count proves the cap runs
    before, not after, the fetch.
    """
    http = RecordedHttpClient(
        [
            recorded("websearch_results.json"),
            verified_page(WEBSEARCH_PASSAGE_1, WEBSEARCH_URL_1),
            verified_page(WEBSEARCH_PASSAGE_2, WEBSEARCH_URL_2),
        ]
    )
    provider = OpenAIWebSearchProvider(http=http, api_key="test-key")

    passages = await provider.search("hawker stall rents", limit=2)

    assert len(passages) == 2
    assert all(passage.provenance_verified for passage in passages)
    # One POST plus exactly two verification GETs — never a third.
    assert len(http.requests) == 3


async def test_websearch_drops_a_result_the_search_tool_never_returned() -> None:
    """A URL the model invented is the exact failure this product exists to prevent."""
    http = RecordedHttpClient(
        [
            responses_payload(
                results=[
                    {
                        "text": "The adjustment is 4 per cent.",
                        "url": "https://gov.example/press/real",
                        "outlet": "gov.example",
                        "date": "2026-03-12",
                    },
                    {
                        "text": "An authoritative-sounding sentence from nowhere.",
                        "url": "https://gov.example/press/never-returned",
                        "outlet": "gov.example",
                        "date": "2026-03-12",
                    },
                ],
                citations=["https://gov.example/press/real"],
            ),
            # Only the one surviving result ever gets a verification fetch — the
            # invented one was dropped by the citation gate before reaching it.
            verified_page("The adjustment is 4 per cent.", "https://gov.example/press/real"),
        ]
    )
    provider = OpenAIWebSearchProvider(http=http, api_key="test-key")

    passages = await provider.search("claim", limit=6)

    assert [passage.url for passage in passages] == ["https://gov.example/press/real"]
    assert passages[0].provenance_verified is True
    assert len(http.requests) == 2


async def test_websearch_discards_an_answer_that_cites_nothing() -> None:
    """No annotations means the tool never ran and the model answered from memory."""
    http = RecordedHttpClient(
        [
            responses_payload(
                results=[{"text": "A confident sentence.", "url": "https://news.example/a"}],
                citations=[],
            )
        ]
    )
    provider = OpenAIWebSearchProvider(http=http, api_key="test-key")

    assert await provider.search("claim", limit=6) == []


async def test_websearch_survives_a_model_that_answered_with_prose() -> None:
    """A failed parse is an empty result, logged — never a guess at what it meant."""
    http = RecordedHttpClient(
        [
            HttpResponse(
                status_code=200,
                text=json.dumps(
                    {
                        "output": [
                            {
                                "type": "message",
                                "content": [
                                    {
                                        "type": "output_text",
                                        "text": "I could not find anything about this.",
                                        "annotations": [
                                            {
                                                "type": "url_citation",
                                                "url": "https://news.example/a",
                                            }
                                        ],
                                    }
                                ],
                            }
                        ]
                    }
                ),
                url=RESPONSES_ENDPOINT,
            )
        ]
    )
    provider = OpenAIWebSearchProvider(http=http, api_key="test-key")

    assert await provider.search("claim", limit=6) == []


async def test_websearch_accepts_a_fenced_json_answer() -> None:
    """Models add code fences even when told not to; that is not a reason to lose evidence."""
    http = RecordedHttpClient(
        [
            HttpResponse(
                status_code=200,
                text=json.dumps(
                    {
                        "output": [
                            {
                                "type": "message",
                                "content": [
                                    {
                                        "type": "output_text",
                                        "text": (
                                            "```json\n"
                                            '{"results": [{"text": "Four per cent.", '
                                            '"url": "https://gov.example/press/x", '
                                            '"outlet": "gov.example", "date": "2026-03-12"}]}\n'
                                            "```"
                                        ),
                                        "annotations": [
                                            {
                                                "type": "url_citation",
                                                "url": "https://gov.example/press/x",
                                            }
                                        ],
                                    }
                                ],
                            }
                        ]
                    }
                ),
                url=RESPONSES_ENDPOINT,
            ),
            verified_page("Four per cent.", "https://gov.example/press/x"),
        ]
    )
    provider = OpenAIWebSearchProvider(http=http, api_key="test-key")

    passages = await provider.search("claim", limit=6)

    assert [passage.text for passage in passages] == ["Four per cent."]
    assert passages[0].provenance_verified is True


@pytest.mark.parametrize("status", [401, 429, 500])
async def test_websearch_never_retries_a_rejection(status: int) -> None:
    """The most expensive call in the pipeline is made at most once per claim."""
    http = RecordedHttpClient([HttpResponse(status_code=status, text="{}", url=RESPONSES_ENDPOINT)])
    provider = OpenAIWebSearchProvider(http=http, api_key="test-key")

    assert await provider.search("claim", limit=6) == []
    assert len(http.requests) == 1


async def test_websearch_survives_a_transport_failure() -> None:
    """A dead connection is a missing passage."""
    http = RecordedHttpClient([TimeoutError("responses took too long")])
    provider = OpenAIWebSearchProvider(http=http, api_key="test-key")

    assert await provider.search("claim", limit=6) == []


# --------------------------------------------- BLOCKER B1: provenance verification


async def test_websearch_drops_a_passage_whose_text_never_appears_on_the_cited_page() -> None:
    """The headline correctness property, made real: the citation gate alone
    (a URL the search tool actually returned) is not enough — a real page can
    still sit under a hallucinated summary. This is the case B1 exists for: the
    fetch succeeds, and the model's sentence is nowhere on the page it names.
    That is proof of fabrication, not a network hiccup, so the passage is
    dropped outright rather than merely marked unverified.
    """
    http = RecordedHttpClient(
        [
            responses_payload(
                results=[
                    {
                        "text": "The agency confirmed the adjustment would be 40 per cent.",
                        "url": "https://gov.example/press/real",
                        "outlet": "gov.example",
                        "date": "2026-03-12",
                    }
                ],
                citations=["https://gov.example/press/real"],
            ),
            # The page the URL actually points to says nothing of the kind.
            verified_page(
                "The agency's press release covers hawker centre opening hours.",
                "https://gov.example/press/real",
            ),
        ]
    )
    provider = OpenAIWebSearchProvider(http=http, api_key="test-key")

    assert await provider.search("claim", limit=6) == []


async def test_websearch_keeps_a_passage_unverified_when_the_fetch_fails() -> None:
    """A dead or slow cited page is not evidence the model lied.

    The passage survives with ``provenance_verified=False`` rather than being
    dropped, and the fetch is never retried (cost rule) — the request count
    below would be 3, not 2, if it were.
    """
    http = RecordedHttpClient(
        [
            responses_payload(
                results=[
                    {
                        "text": "The adjustment is 4 per cent.",
                        "url": "https://gov.example/press/real",
                        "outlet": "gov.example",
                        "date": "2026-03-12",
                    }
                ],
                citations=["https://gov.example/press/real"],
            ),
            TimeoutError("gov.example took too long"),
        ]
    )
    provider = OpenAIWebSearchProvider(http=http, api_key="test-key")

    passages = await provider.search("claim", limit=6)

    assert len(passages) == 1
    assert passages[0].text == "The adjustment is 4 per cent."
    assert passages[0].provenance_verified is False
    assert len(http.requests) == 2


async def test_websearch_keeps_a_passage_unverified_on_a_non_2xx_fetch() -> None:
    """A 404 on the cited page settles nothing about the passage's honesty."""
    http = RecordedHttpClient(
        [
            responses_payload(
                results=[
                    {
                        "text": "The adjustment is 4 per cent.",
                        "url": "https://gov.example/press/real",
                        "outlet": "gov.example",
                        "date": "2026-03-12",
                    }
                ],
                citations=["https://gov.example/press/real"],
            ),
            HttpResponse(status_code=404, text="not found", url="https://gov.example/press/real"),
        ]
    )
    provider = OpenAIWebSearchProvider(http=http, api_key="test-key")

    passages = await provider.search("claim", limit=6)

    assert len(passages) == 1
    assert passages[0].provenance_verified is False


async def test_websearch_keeps_a_passage_unverified_for_a_non_html_body() -> None:
    """A PDF press release cannot be checked by this module's text extraction,
    so it is left unverified rather than guessed at."""
    http = RecordedHttpClient(
        [
            responses_payload(
                results=[
                    {
                        "text": "The adjustment is 4 per cent.",
                        "url": "https://gov.example/press/real.pdf",
                        "outlet": "gov.example",
                        "date": "2026-03-12",
                    }
                ],
                citations=["https://gov.example/press/real.pdf"],
            ),
            HttpResponse(
                status_code=200,
                text="%PDF-1.7 the adjustment is 4 per cent",
                url="https://gov.example/press/real.pdf",
                headers={"content-type": "application/pdf"},
            ),
        ]
    )
    provider = OpenAIWebSearchProvider(http=http, api_key="test-key")

    passages = await provider.search("claim", limit=6)

    assert len(passages) == 1
    assert passages[0].provenance_verified is False


# --------------------------------------------- MAJOR M9: web-search call accounting


async def test_websearch_stats_counts_only_calls_that_actually_query() -> None:
    """The exposed counter is what a per-run cost report is built on.

    It must count every request that actually reached the network — mirroring
    what ``retrieve.py``'s ClaimReview short-circuit predicts for a run: a
    claim resolved by a fact-check hit never calls this provider's ``search``
    at all, so it must never be counted here either. This module cannot see
    that short-circuit (it lives in ``retrieve.py``, which this module does
    not own) — what it must guarantee is that its own short-circuits (an empty
    claim, a limit of zero) are equally invisible to the counter, since both
    mean "no request was ever going to be sent".
    """
    http = RecordedHttpClient(
        [
            responses_payload(results=[], citations=["https://gov.example/press/x"]),
            responses_payload(results=[], citations=["https://gov.example/press/y"]),
            responses_payload(results=[], citations=["https://gov.example/press/z"]),
        ]
    )
    provider = OpenAIWebSearchProvider(http=http, api_key="test-key")

    assert provider.stats.calls == 0

    assert await provider.search("", limit=6) == []
    assert await provider.search("a claim", limit=0) == []
    assert provider.stats.calls == 0, "an unsent request must not be counted"

    await provider.search("claim one", limit=6)
    await provider.search("claim two", limit=6)
    await provider.search("claim three", limit=6)

    assert provider.stats.calls == 3
    assert len(http.requests) == 3


async def test_websearch_stats_counts_a_call_even_when_it_is_rejected() -> None:
    """A 429 is still a call OpenAI billed; the counter must not under-report it."""
    http = RecordedHttpClient([HttpResponse(status_code=429, text="{}", url=RESPONSES_ENDPOINT)])
    provider = OpenAIWebSearchProvider(http=http, api_key="test-key")

    assert await provider.search("claim", limit=6) == []

    assert provider.stats.calls == 1


# ---------------------------------------------------------------- official data


async def test_official_data_parses_datasets_and_builds_a_missing_url() -> None:
    """A dataset with no URL of its own still needs a link a reader can click."""
    http = RecordedHttpClient([recorded("official_datasets.json")])
    provider = DataGovSgProvider(http=http)

    passages = await provider.search("hawker stall rents rise 40%", limit=2)

    assert len(passages) == 2
    first, second = passages
    assert first.origin == "official"
    assert first.outlet == "Example Environment Agency"
    assert first.date == "2026-02-01"
    assert first.url == "https://data.gov.sg/datasets/hawker-centre-stall-rentals"
    assert "Official dataset on data.gov.sg" in first.text
    assert second.url == "https://data.gov.sg/datasets/hawker-stall-vacancies"


async def test_official_data_strips_query_syntax_from_a_quote() -> None:
    """A claim quote is prose; an unbalanced quotation mark would be a 400."""
    http = RecordedHttpClient([recorded("official_datasets.json")])
    provider = DataGovSgProvider(http=http)

    await provider.search('rents rose 40% (per "briefing") — vendors said: yes', limit=2)

    query = http.requests[0].params["q"]
    assert not set(query) & set('+-!(){}[]^"~*?:\\/&|')
    assert "rents rose 40%" in query


async def test_official_data_treats_an_unsuccessful_body_as_empty() -> None:
    """CKAN reports failure in a 200 body; that is still a miss."""
    http = RecordedHttpClient(
        [HttpResponse(status_code=200, text='{"success": false}', url="https://data.gov.sg")]
    )
    provider = DataGovSgProvider(http=http)

    assert await provider.search("hawker rents", limit=2) == []


async def test_official_data_survives_a_transport_failure() -> None:
    """One dead provider must not fail the claim."""
    http = RecordedHttpClient([ConnectionError("data.gov.sg unreachable")])
    provider = DataGovSgProvider(http=http)

    assert await provider.search("hawker rents", limit=2) == []


# ---------------------------------------------------------------- cited source


async def test_cited_source_follows_the_matching_link_and_ignores_the_rest() -> None:
    """Navigation, share buttons and unrelated links are not citations."""
    http = RecordedHttpClient(
        [
            html_page("cited_article.html", ARTICLE_URL),
            html_page("cited_source.html", "https://gov.example/press/stall-rental-adjustment"),
        ]
    )
    provider = LinkedCitationProvider(http=http)

    passages = await provider.fetch(ATTRIBUTION_QUOTE, article_url=ARTICLE_URL, limit=2)

    assert len(http.requests) == 2
    assert http.requests[1].url == "https://gov.example/press/stall-rental-adjustment"
    assert len(passages) == 1
    passage = passages[0]
    assert passage.origin == "cited_source"
    assert passage.outlet == "Example Environment Agency"
    assert passage.date == "2026-03-12"
    assert "4 per cent" in passage.text
    # Scripts and styles are not evidence.
    assert "not the passage text" not in passage.text
    assert "font-family" not in passage.text


async def test_cited_source_stops_at_its_fetch_budget() -> None:
    """A link-follower is how the expensive stage stops being affordable."""
    http = RecordedHttpClient([html_page("cited_article.html", ARTICLE_URL)])
    provider = LinkedCitationProvider(http=http, max_fetches=1)

    assert await provider.fetch(ATTRIBUTION_QUOTE, article_url=ARTICLE_URL, limit=2) == []
    assert http.requests == []


async def test_cited_source_gives_up_when_the_article_cannot_be_fetched() -> None:
    """No article, no links, no second request."""
    http = RecordedHttpClient([HttpResponse(status_code=404, text="", url=ARTICLE_URL)])
    provider = LinkedCitationProvider(http=http)

    assert await provider.fetch(ATTRIBUTION_QUOTE, article_url=ARTICLE_URL, limit=2) == []
    assert len(http.requests) == 1


async def test_cited_source_skips_a_page_that_is_not_html() -> None:
    """A PDF press release is a real limitation, not a passage of binary noise."""
    http = RecordedHttpClient(
        [
            html_page("cited_article.html", ARTICLE_URL),
            HttpResponse(
                status_code=200,
                text="%PDF-1.7 …",
                url="https://gov.example/press/stall-rental-adjustment",
                headers={"content-type": "application/pdf"},
            ),
        ]
    )
    provider = LinkedCitationProvider(http=http)

    assert await provider.fetch(ATTRIBUTION_QUOTE, article_url=ARTICLE_URL, limit=2) == []


async def test_cited_source_ignores_a_page_about_something_else() -> None:
    """The link looked right, the page is not; a passage from it is evidence of nothing."""
    http = RecordedHttpClient(
        [
            html_page("cited_article.html", ARTICLE_URL),
            HttpResponse(
                status_code=200,
                text="<html><body><p>Weekend cinema listings.</p></body></html>",
                url="https://gov.example/press/stall-rental-adjustment",
                headers={"content-type": "text/html"},
            ),
        ]
    )
    provider = LinkedCitationProvider(http=http)

    assert await provider.fetch(ATTRIBUTION_QUOTE, article_url=ARTICLE_URL, limit=2) == []


async def test_cited_source_refuses_a_url_it_cannot_fetch() -> None:
    """An article URL that is not an absolute http(s) URL is not a starting point."""
    http = RecordedHttpClient([])
    provider = LinkedCitationProvider(http=http)

    assert await provider.fetch(ATTRIBUTION_QUOTE, article_url="not a url", limit=2) == []
    assert http.requests == []
