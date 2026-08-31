"""Stage 2 orchestration: the order, the short-circuit, the caps, the failures.

Every provider here is a fake. That is the point: this file is about what
retrieval *asks for* and what it *keeps*, not about any external service — those
live in ``tests/test_providers.py``. Nothing opens a socket.

The load-bearing test in this file is
:func:`test_a_factcheck_hit_means_web_search_is_never_called`. A ClaimReview hit
skipping web search is a cost *guarantee* (``docs/decisions.md`` §9), and a
guarantee that is not asserted is a comment.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from app.config import MissingSettingError, Settings
from app.pipeline.providers.base import NullPassageProvider, Providers, RecordedHttpClient
from app.pipeline.providers.cited import LinkedCitationProvider
from app.pipeline.providers.factcheck import FACTCHECK_ENDPOINT, GoogleFactCheckProvider
from app.pipeline.providers.official import DataGovSgProvider
from app.pipeline.providers.websearch import OpenAIWebSearchProvider
from app.pipeline.retrieve import RetrievalOutcome
from app.pipeline.types import ClaimKind, ExtractedClaim, Passage, PassageOrigin
from tests.conftest import build_settings

ARTICLE_URL = "https://news.example/hawker-rents"
"""The fictional article every claim in this file was extracted from."""

WIRE_STORY = (
    "The agency said the median stall rental adjustment for the coming year is 4 per "
    "cent, and that rentals at 12 centres will not change at all. Stallholders were "
    "briefed at their respective centres last month, it added."
)
"""One agency story, reprinted verbatim under several mastheads below.

Figures are written as digits (``4``, ``12``), not spelled out, on purpose:
:func:`~app.pipeline.retrieve._fingerprint` only recognises a token as a
*number* when it contains a digit character, and several tests below are
specifically about how two passages' numeric tokens are compared.
"""


# ---------------------------------------------------------------- fakes


@dataclass
class FakeProvider:
    """A fact-check, web-search or official-data provider that records its calls.

    :attr:`calls` is what the cost assertions are made against: "web search was
    never called" is a statement about this list, not about the passages.
    """

    passages: list[Passage] = field(default_factory=list)
    calls: list[tuple[str, int]] = field(default_factory=list)

    async def search(self, query: str, *, limit: int) -> list[Passage]:
        self.calls.append((query, limit))
        return list(self.passages)


@dataclass
class FakeCitedSource:
    """A cited-source provider that records its calls (it has its own signature)."""

    passages: list[Passage] = field(default_factory=list)
    calls: list[tuple[str, str, int]] = field(default_factory=list)

    async def fetch(self, quote: str, *, article_url: str, limit: int) -> list[Passage]:
        self.calls.append((quote, article_url, limit))
        return list(self.passages)


@dataclass
class RaisingProvider:
    """A provider that fails the way a real one eventually will."""

    calls: int = 0

    async def search(self, query: str, *, limit: int) -> list[Passage]:
        self.calls += 1
        raise RuntimeError("the provider fell over")


@dataclass
class HangingProvider:
    """A provider that never answers. The reason every call has a timeout."""

    async def search(self, query: str, *, limit: int) -> list[Passage]:
        await asyncio.sleep(30)
        return []


@dataclass
class WrongTypeProvider:
    """A provider returning something that is not a list of passages."""

    async def search(self, query: str, *, limit: int) -> list[Passage]:
        return "not passages"  # type: ignore[return-value]


# ---------------------------------------------------------------- helpers


def passage(
    *,
    text: str = "Some retrieved evidence about the claim.",
    url: str = "https://news.example/story",
    outlet: str = "Example News",
    origin: PassageOrigin = "web",
    date: str | None = "2026-03-12",
    rating: str | None = None,
) -> Passage:
    """A passage with sensible defaults, so each test states only what it is about."""
    return Passage(
        text=text, url=url, outlet=outlet, date=date, wire=False, origin=origin, rating=rating
    )


def claim(
    kind: ClaimKind = "general", quote: str = "Rents will rise 40% next year."
) -> ExtractedClaim:
    """One extracted claim of the given kind."""
    return ExtractedClaim(
        id="c1", quote=quote, start=0, end=len(quote), kind=kind, checkworthiness=0.9
    )


def make_providers(
    *,
    factcheck: object | None = None,
    search: object | None = None,
    official: object | None = None,
    cited: object | None = None,
    timeout_seconds: float = 5.0,
) -> Providers:
    """A :class:`Providers` container of fakes, each defaulting to "found nothing"."""
    return Providers(
        factcheck=factcheck or FakeProvider(),  # type: ignore[arg-type]
        search=search or FakeProvider(),  # type: ignore[arg-type]
        official=official or FakeProvider(),  # type: ignore[arg-type]
        cited=cited or FakeCitedSource(),  # type: ignore[arg-type]
        timeout_seconds=timeout_seconds,
    )


@pytest.fixture
def settings() -> Settings:
    """Production caps, pinned so a developer's ``.env`` cannot change an outcome."""
    return build_settings(max_passages_per_claim=6)


async def retrieve_outcome(
    claim_: ExtractedClaim, providers: Providers, settings_: Settings
) -> RetrievalOutcome:
    """Call the stage under test with the article URL every test shares.

    Returns the full :class:`RetrievalOutcome`, including
    ``providers_failed``/``retrieval_broken`` — what BLOCKER B5's tests need.
    Most tests in this file only care about the passages themselves and use
    :func:`retrieve` instead.
    """
    from app.pipeline.retrieve import retrieve_passages

    return await retrieve_passages(
        claim_, article_url=ARTICLE_URL, providers=providers, settings=settings_
    )


async def retrieve(
    claim_: ExtractedClaim, providers: Providers, settings_: Settings
) -> list[Passage]:
    """Like :func:`retrieve_outcome`, but just the passages — what most tests here want."""
    return (await retrieve_outcome(claim_, providers, settings_)).passages


# ---------------------------------------------------------------- the cost guarantee


async def test_a_factcheck_hit_means_web_search_is_never_called(settings: Settings) -> None:
    """**The cost guarantee.** A ClaimReview short-circuits the expensive step.

    Asserted on the call count, not on the passages: a web search that ran and
    whose results were then discarded has already been paid for.
    """
    factcheck = FakeProvider([passage(origin="factcheck", url="https://factcheck.example/a")])
    search = FakeProvider([passage(url="https://news.example/b")])
    providers = make_providers(factcheck=factcheck, search=search)

    passages = await retrieve(claim(), providers, settings)

    assert search.calls == []
    assert len(factcheck.calls) == 1
    assert [item.origin for item in passages] == ["factcheck"]


async def test_web_search_runs_when_the_factcheck_provider_finds_nothing(
    settings: Settings,
) -> None:
    """The fall-through: no review, so the claim is worth searching for."""
    factcheck = FakeProvider([])
    search = FakeProvider([passage()])
    providers = make_providers(factcheck=factcheck, search=search)

    passages = await retrieve(claim(), providers, settings)

    assert len(search.calls) == 1
    assert [item.origin for item in passages] == ["web"]


async def test_each_provider_is_called_at_most_once_per_claim(settings: Settings) -> None:
    """No retries anywhere in the stage; one claim is one call per provider."""
    factcheck = FakeProvider([])
    search = FakeProvider([passage()])
    official = FakeProvider([passage(origin="official")])
    providers = make_providers(factcheck=factcheck, search=search, official=official)

    await retrieve(claim("numeric"), providers, settings)

    assert len(factcheck.calls) == 1
    assert len(search.calls) == 1
    assert len(official.calls) == 1


async def test_the_short_circuit_holds_with_the_real_providers(settings: Settings) -> None:
    """The same guarantee, one level down: with real providers and one recorded
    ClaimReview answer, exactly one HTTP request leaves the process.

    :class:`RecordedHttpClient` raises on an unscripted request, so a stray call
    to the Responses endpoint fails this test rather than passing quietly — which
    is the whole reason the fixture is scripted rather than permissive.
    """
    from app.pipeline.providers.base import load_recorded_http

    http = RecordedHttpClient(
        [load_recorded_http(Path(__file__).parent / "fixtures" / "retrieve" / "factcheck_hit.json")]
    )
    providers = Providers(
        factcheck=GoogleFactCheckProvider(http=http, api_key="test-key"),
        search=OpenAIWebSearchProvider(http=http, api_key="test-key"),
        official=DataGovSgProvider(http=http),
        cited=LinkedCitationProvider(http=http),
    )

    passages = await retrieve(claim(), providers, settings)

    assert [request.url for request in http.requests] == [FACTCHECK_ENDPOINT]
    assert [item.origin for item in passages] == ["factcheck"]
    assert passages[0].rating == "False"


# ---------------------------------------------------------------- kind routing


async def test_numeric_claims_consult_official_data(settings: Settings) -> None:
    """A figure is checkable against the body that publishes it."""
    official = FakeProvider(
        [passage(origin="official", url="https://data.gov.sg/datasets/x", text="A rental dataset.")]
    )
    cited = FakeCitedSource([passage(origin="cited_source", text="A press release.")])
    providers = make_providers(
        search=FakeProvider([passage(text="A news report about next year's rents.")]),
        official=official,
        cited=cited,
    )

    passages = await retrieve(claim("numeric"), providers, settings)

    assert len(official.calls) == 1
    assert cited.calls == []
    assert {item.origin for item in passages} == {"official", "web"}


async def test_attribution_claims_fetch_the_cited_source(settings: Settings) -> None:
    """What settles "X said Y" is what X published."""
    official = FakeProvider([passage(origin="official", text="A rental dataset.")])
    cited = FakeCitedSource(
        [passage(origin="cited_source", url="https://gov.example/press/x", text="A press release.")]
    )
    providers = make_providers(
        search=FakeProvider([passage(text="A news report about next year's rents.")]),
        official=official,
        cited=cited,
    )

    passages = await retrieve(claim("attribution"), providers, settings)

    assert len(cited.calls) == 1
    assert cited.calls[0][1] == ARTICLE_URL
    assert official.calls == []
    assert {item.origin for item in passages} == {"cited_source", "web"}


async def test_general_claims_consult_neither_supplement(settings: Settings) -> None:
    """A request per claim is a cost; a claim with no number has nothing to ask a catalogue."""
    official = FakeProvider([passage(origin="official", text="A rental dataset.")])
    cited = FakeCitedSource([passage(origin="cited_source", text="A press release.")])
    providers = make_providers(
        search=FakeProvider([passage(text="A news report about next year's rents.")]),
        official=official,
        cited=cited,
    )

    passages = await retrieve(claim("general"), providers, settings)

    assert official.calls == []
    assert cited.calls == []
    assert [item.origin for item in passages] == ["web"]


async def test_the_query_is_the_quote_collapsed_and_capped(settings: Settings) -> None:
    """Every provider gets the same query, and it is never a whole paragraph."""
    from app.pipeline.retrieve import MAX_QUERY_CHARS

    search = FakeProvider([])
    long_quote = "Rents  will\nrise 40% next year. " + ("padding words " * 60)
    providers = make_providers(search=search)

    await retrieve(claim("general", long_quote), providers, settings)

    sent = search.calls[0][0]
    assert len(sent) <= MAX_QUERY_CHARS
    assert sent.startswith("Rents will rise 40% next year.")


async def test_an_empty_quote_costs_nothing(settings: Settings) -> None:
    """No query, no requests. The cheapest claim is the one we never look up."""
    factcheck = FakeProvider([passage(origin="factcheck")])
    search = FakeProvider([passage()])
    providers = make_providers(factcheck=factcheck, search=search)

    assert await retrieve(claim("general", "   "), providers, settings) == []
    assert factcheck.calls == []
    assert search.calls == []


# ---------------------------------------------------------------- wire copy


async def test_one_wire_story_on_five_domains_counts_as_one_source(settings: Settings) -> None:
    """Five reprints are one source, marked as wire, represented by an originating outlet.

    Counting them as five would let a single agency story satisfy aggregation's
    "two or more independent sources" rule on its own — which is exactly how a
    wire error comes to look corroborated.
    """
    search = FakeProvider(
        [
            passage(text=WIRE_STORY, url="https://sg.news.yahoo.com/story-a", outlet="Yahoo News"),
            passage(text=WIRE_STORY, url="https://cna.example/story-a", outlet="CNA"),
            passage(text=WIRE_STORY, url="https://msn.com/story-a", outlet="MSN"),
            passage(text=WIRE_STORY, url="https://straitstimes.example/story-a", outlet="ST"),
            passage(text=WIRE_STORY, url="https://mothership.example/story-a", outlet="Mothership"),
        ]
    )
    providers = make_providers(search=search)

    passages = await retrieve(claim(), providers, settings)

    assert len(passages) == 1
    assert passages[0].wire is True
    assert passages[0].url == "https://cna.example/story-a"


async def test_a_reprint_that_changed_a_word_is_still_the_same_story(
    settings: Settings,
) -> None:
    """Wire copy is re-edited in the reprint; exact equality would miss most of it."""
    reprint = WIRE_STORY.replace("briefed", "informed")
    search = FakeProvider(
        [
            passage(text=WIRE_STORY, url="https://cna.example/story-a"),
            passage(text=reprint, url="https://mothership.example/story-a"),
        ]
    )
    providers = make_providers(search=search)

    passages = await retrieve(claim(), providers, settings)

    assert len(passages) == 1
    assert passages[0].wire is True


async def test_two_copies_whose_numbers_disagree_stay_two_passages(
    settings: Settings,
) -> None:
    """A correction and the story it corrects must never collapse into one source.

    Both figures change (``4`` → ``14``, ``12`` → ``20``), so the two passages'
    numeric tokens share nothing at all — the case MAJOR M4's loosened veto
    (:data:`~app.pipeline.retrieve.NUMBER_AGREEMENT`) still refuses to merge,
    however close the prose otherwise reads.
    """
    corrected = WIRE_STORY.replace("4 per cent", "14 per cent").replace(
        "12 centres", "20 centres"
    )
    search = FakeProvider(
        [
            passage(text=WIRE_STORY, url="https://cna.example/story-a"),
            passage(text=corrected, url="https://cna.example/story-a-corrected"),
        ]
    )
    providers = make_providers(search=search)

    passages = await retrieve(claim(), providers, settings)

    assert len(passages) == 2


async def test_wire_copies_snipped_at_different_points_still_merge(
    settings: Settings,
) -> None:
    """**MAJOR M4.** A shorter pickup that dropped one figure is still the same story.

    Before the fix, :func:`~app.pipeline.retrieve._similarity` vetoed a merge
    the instant the two passages' numeric-token *sets* were not identical —
    which is exactly what happens when one outlet's pickup is snipped shorter
    than another's and drops a clause that named one of the figures. That
    turned one wire story, quoted at two lengths, into two "independent"
    sources: precisely the input aggregation's "two or more independent
    sources" rule must not be fooled by.

    Here the shorter copy keeps the ``4 per cent`` figure but drops the clause
    naming ``12 centres`` — a real, plausible snip, not a fabricated one — so
    the numeric-token sets differ (``{4, 12}`` vs ``{4}``) while the prose is
    otherwise close enough to clear :data:`~app.pipeline.retrieve.WIRE_SIMILARITY`.
    The old exact-equality rule vetoed this pair outright; the loosened
    :data:`~app.pipeline.retrieve.NUMBER_AGREEMENT` check (0.5 overlap here,
    since the snip kept one of the two figures) does not.
    """
    full = (
        "The agency said the median stall rental adjustment for the coming year is 4 "
        "per cent, and that rentals at 12 centres will not change at all, according to "
        "a statement issued to reporters on Tuesday morning ahead of the annual review. "
        "Stallholders were briefed at their respective centres last month, and the "
        "agency said further updates would follow before the new leases take effect, "
        "it added."
    )
    snipped = (
        "The agency said the median stall rental adjustment for the coming year is 4 "
        "per cent, according to a statement issued to reporters on Tuesday morning "
        "ahead of the annual review. Stallholders were briefed at their respective "
        "centres last month, and the agency said further updates would follow before "
        "the new leases take effect, it added."
    )
    search = FakeProvider(
        [
            passage(text=full, url="https://cna.example/story-b"),
            passage(text=snipped, url="https://mothership.example/story-b"),
        ]
    )
    providers = make_providers(search=search)

    passages = await retrieve(claim(), providers, settings)

    assert len(passages) == 1
    assert passages[0].wire is True


async def test_two_independent_reports_stay_two_sources(settings: Settings) -> None:
    """The expensive mistake is the *false* merge: two newsrooms are two sources."""
    search = FakeProvider(
        [
            passage(text=WIRE_STORY, url="https://cna.example/story-a"),
            passage(
                text=(
                    "Hawkers at three centres told this newspaper they had heard nothing "
                    "official about next year's rentals, and said they learned of the figure "
                    "from a message circulating among stallholders."
                ),
                url="https://straitstimes.example/story-b",
            ),
        ]
    )
    providers = make_providers(search=search)

    passages = await retrieve(claim(), providers, settings)

    assert len(passages) == 2


async def test_duplicates_from_one_domain_are_collapsed_but_not_called_wire(
    settings: Settings,
) -> None:
    """One site printing the same passage twice is a duplicate, not syndication."""
    search = FakeProvider(
        [
            passage(text=WIRE_STORY, url="https://cna.example/story-a"),
            passage(text=WIRE_STORY, url="https://cna.example/story-a?amp=1"),
        ]
    )
    providers = make_providers(search=search)

    passages = await retrieve(claim(), providers, settings)

    assert len(passages) == 1
    assert passages[0].wire is False


# ---------------------------------------------------------------- the cap


async def test_the_cap_is_enforced_and_keeps_the_better_sources() -> None:
    """Stages 3 and 4 are billed by what they read, so *which* passages survive matters."""
    capped = build_settings(max_passages_per_claim=2)
    official = FakeProvider(
        [passage(origin="official", url="https://data.gov.sg/datasets/rents", text="Dataset.")]
    )
    search = FakeProvider(
        [
            passage(url="https://sg.news.yahoo.com/a", text="Aggregated reprint one."),
            passage(url="https://news.example/b", text="An independent report."),
            passage(url="https://blog.example/c", text="A third page entirely."),
        ]
    )
    providers = make_providers(search=search, official=official)

    passages = await retrieve(claim("numeric"), providers, capped)

    assert len(passages) == 2
    assert passages[0].origin == "official"
    # Among the web pages, the aggregator is the one dropped.
    assert passages[1].url == "https://news.example/b"


async def test_a_cap_of_zero_retrieves_nothing() -> None:
    """A budget of nothing is spent on nothing — no provider is called."""
    search = FakeProvider([passage()])
    providers = make_providers(search=search)

    assert await retrieve(claim(), providers, build_settings(max_passages_per_claim=0)) == []
    assert search.calls == []


async def test_ranking_prefers_factcheck_then_official_then_cited_then_web() -> None:
    """The order the cap cuts along, stated once here so it cannot drift silently."""
    from app.pipeline.retrieve import rank_and_cap

    web = passage(url="https://news.example/w")
    cited = passage(origin="cited_source", url="https://gov.example/c")
    official = passage(origin="official", url="https://data.gov.sg/o")
    factcheck = passage(origin="factcheck", url="https://factcheck.example/f")

    ranked = rank_and_cap([web, cited, official, factcheck], 4)

    assert [item.origin for item in ranked] == ["factcheck", "official", "cited_source", "web"]


# ---------------------------------------------------------------- failure


async def test_a_provider_that_raises_yields_nothing_and_breaks_nothing(
    settings: Settings,
) -> None:
    """One dead provider costs the claim one source, not the whole check.

    Also **BLOCKER B5**: the fact-check provider that raised is recorded as
    failed, but because the official-data supplement came back with a real
    passage, the outcome as a whole is not ``retrieval_broken`` — there is
    genuine evidence here for the judge to read.
    """
    raising = RaisingProvider()
    official = FakeProvider([passage(origin="official")])
    providers = make_providers(factcheck=raising, search=raising, official=official)

    outcome = await retrieve_outcome(claim("numeric"), providers, settings)

    assert [item.origin for item in outcome.passages] == ["official"]
    # Fact check raised, so web search was still attempted: a failure is not a hit.
    assert raising.calls == 2
    assert set(outcome.providers_failed) == {"fact-check", "web-search"}
    assert outcome.retrieval_broken is False


async def test_a_provider_that_hangs_is_cut_off_and_the_rest_still_answers() -> None:
    """Every provider call has a ceiling; a reader is waiting on the other side."""
    fast = build_settings(max_passages_per_claim=6)
    official = FakeProvider([passage(origin="official")])
    providers = make_providers(
        factcheck=HangingProvider(), official=official, timeout_seconds=0.05
    )

    passages = await retrieve(claim("numeric"), providers, fast)

    assert [item.origin for item in passages] == ["official"]


async def test_a_provider_returning_the_wrong_type_is_ignored(settings: Settings) -> None:
    """A provider is an adapter around somebody else's service; wrong shapes happen."""
    providers = make_providers(search=WrongTypeProvider())

    assert await retrieve(claim(), providers, settings) == []


async def test_no_evidence_is_an_empty_list_not_an_error(settings: Settings) -> None:
    """A claim nothing was found for is honestly unverifiable, which is a valid answer.

    **BLOCKER B5, the other half of the story:** every provider here is a
    :class:`FakeProvider` returning ``[]`` — a provider that was consulted and
    genuinely answered "no results" is not a *failure*, so
    :attr:`RetrievalOutcome.retrieval_broken` must stay ``False``. This is the
    case the fix must not break: an honestly empty web still produces an
    honest ``unverifiable``, not a claim that looks like retrieval broke.
    """
    outcome = await retrieve_outcome(claim(), make_providers(), settings)

    assert outcome.passages == []
    assert outcome.providers_failed == ()
    assert outcome.retrieval_broken is False


# ---------------------------------------------------------------- retrieval_broken (BLOCKER B5)


async def test_every_provider_failing_marks_retrieval_as_broken(settings: Settings) -> None:
    """The exact BLOCKER B5 repro: every provider consulted raises on every call.

    Before the fix this produced ``passages == []`` with no way to tell it
    apart from an honestly empty web, and :mod:`app.pipeline.run` published it
    (and cached it) as a completed search that found nothing. Now the outcome
    says plainly that nothing was consulted successfully.
    """
    raising = RaisingProvider()
    providers = make_providers(factcheck=raising, search=raising)

    outcome = await retrieve_outcome(claim("general"), providers, settings)

    assert outcome.passages == []
    assert set(outcome.providers_failed) == {"fact-check", "web-search"}
    assert outcome.retrieval_broken is True


async def test_a_malformed_answer_also_counts_as_retrieval_broken(settings: Settings) -> None:
    """A provider returning garbage is exactly as broken as one that raises.

    :class:`WrongTypeProvider` never raises — it just answers with something
    that is not a list of passages — and that must be caught by the same
    signal, not treated as "the web has nothing to say".
    """
    providers = make_providers(factcheck=WrongTypeProvider(), search=WrongTypeProvider())

    outcome = await retrieve_outcome(claim("general"), providers, settings)

    assert outcome.passages == []
    assert outcome.retrieval_broken is True


async def test_an_empty_quote_is_not_retrieval_broken(settings: Settings) -> None:
    """No query means no provider was consulted at all — nothing failed, nothing to report."""
    providers = make_providers(
        factcheck=FakeProvider([passage()]), search=FakeProvider([passage()])
    )

    outcome = await retrieve_outcome(claim("general", "   "), providers, settings)

    assert outcome.passages == []
    assert outcome.providers_failed == ()
    assert outcome.retrieval_broken is False


# ---------------------------------------------------------------- construction


def test_build_providers_degrades_to_web_search_without_a_google_key() -> None:
    """A missing fact-check key makes checks more expensive, not impossible."""
    from app.pipeline.retrieve import build_providers

    providers = build_providers(
        build_settings(openai_api_key="test-key", google_factcheck_api_key=None),
        http=RecordedHttpClient([]),
    )

    assert isinstance(providers.factcheck, NullPassageProvider)
    assert isinstance(providers.search, OpenAIWebSearchProvider)
    assert isinstance(providers.official, DataGovSgProvider)
    assert isinstance(providers.cited, LinkedCitationProvider)


def test_build_providers_builds_the_real_factcheck_provider_with_a_key() -> None:
    """With both keys, all four providers are the real ones."""
    from app.pipeline.retrieve import build_providers

    providers = build_providers(
        build_settings(openai_api_key="test-key", google_factcheck_api_key="google-key"),
        http=RecordedHttpClient([]),
    )

    assert isinstance(providers.factcheck, GoogleFactCheckProvider)


def test_build_providers_refuses_to_run_without_an_openai_key() -> None:
    """Web search is not optional, and a silent no-evidence pipeline would be a lie."""
    from app.pipeline.retrieve import build_providers

    with pytest.raises(MissingSettingError) as excinfo:
        build_providers(build_settings(openai_api_key=None), http=RecordedHttpClient([]))

    assert excinfo.value.env_var == "OPENAI_API_KEY"
