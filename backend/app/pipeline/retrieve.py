"""Stage 2 — evidence retrieval. The expensive stage, and the one with the budget.

One claim in, a short list of :class:`~app.pipeline.types.Passage` out. Those
passages are the *only* thing stages 3 and 4 are allowed to see (``CLAUDE.md``
rule 2: the judge may use retrieved passages and never its own knowledge), so
this module decides both what the pipeline knows about a claim and what checking
that claim costs.

The order, and the short-circuit
--------------------------------
1. **Google Fact Check Tools (ClaimReview).** If it returns a usable review,
   **web search is not called at all** for this claim. Search is the dominant
   per-claim cost (``docs/decisions.md`` §9), and a published review of the same
   claim is better evidence than a page that happens to share its words. This is
   a cost *guarantee*, not an optimisation: ``tests/test_retrieve.py`` asserts
   the search provider's call count is zero on a fact-check hit.
2. **Web search**, only when step 1 found nothing.
3. **Official data** (data.gov.sg) — additionally, and only for ``numeric``
   claims.
4. **The cited source** — additionally, and only for ``attribution`` claims.

Steps 3 and 4 run *concurrently* with the 1→2 chain: they are a different
question asked of a different service, the chain's short-circuit is internal to
it, and a reader is watching claims fill in. Concurrency here never widens the
budget — each provider is still called at most once per claim.

Then, in order: near-identical syndicated copy across domains is collapsed to
one source (:func:`dedupe_wire_copy`); what is left is ranked and cut to
``settings.max_passages_per_claim`` (:func:`rank_and_cap`), keeping fact checks
and primary sources over aggregated reprints.

Failure is a missing passage, never an exception — but it is not silent
------------------------------------------------------------------------
Every provider call is wrapped: a provider that raises, hangs past
``providers.timeout_seconds`` or returns nonsense contributes ``[]`` to the
passage list and the rest of retrieval carries on. The providers guard
themselves too — this guard exists because a fake, or a provider written
later, may forget, and because one dead service must never fail a whole
check.

But ``[]`` alone throws away *why*. :func:`retrieve_passages` returns a
:class:`RetrievalOutcome`, not a bare list, precisely so that a claim ending
with no passages because the web genuinely has nothing (every consulted
provider answered "no results") can be told apart from a claim ending with no
passages because retrieval itself broke (every consulted provider raised,
timed out, or returned garbage) — see :attr:`RetrievalOutcome.retrieval_broken`.
The first is a completed search and an honest ``unverifiable``. The second is
not a completed search at all, and :mod:`app.pipeline.run` treats it as a
*failed* claim rather than publishing it as one. Before this distinction
existed, a total provider outage (an expired key, a wrong tool name, a quota
block) looked identical to "we searched everywhere and the web is silent" —
which is exactly the trap that let one bad key get frozen into the 7-day
cache and told to every reader of an article as "nothing to see here."

Privacy
-------
A claim's quote is article text. It is passed to providers and never logged
(``CLAUDE.md`` rule 6). Log lines here carry the claim id, provider names and
counts — never a query, a passage body or a URL.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable
from dataclasses import dataclass, replace

from app.config import MissingSettingError, Settings
from app.pipeline.providers.base import (
    AsyncHttpClient,
    FactCheckProvider,
    HttpxClient,
    NullPassageProvider,
    Providers,
    domain_of,
)
from app.pipeline.providers.cited import LinkedCitationProvider
from app.pipeline.providers.factcheck import GoogleFactCheckProvider
from app.pipeline.providers.official import DataGovSgProvider
from app.pipeline.providers.websearch import OpenAIWebSearchProvider
from app.pipeline.types import ExtractedClaim, Passage, PassageOrigin, normalize_for_match

logger = logging.getLogger(__name__)

__all__ = [
    "AGGREGATOR_DOMAINS",
    "MAX_QUERY_CHARS",
    "NUMBER_AGREEMENT",
    "ORIGIN_PRIORITY",
    "SUPPLEMENT_LIMIT",
    "WIRE_SIMILARITY",
    "RetrievalOutcome",
    "build_providers",
    "build_query",
    "dedupe_wire_copy",
    "rank_and_cap",
    "retrieve_passages",
]

MAX_QUERY_CHARS = 240
"""Longest query sent to any provider.

A claim quote is one sentence; anything longer than this is an extraction that
swallowed a paragraph, and sending the paragraph makes every provider's matching
worse as well as costing more."""

SUPPLEMENT_LIMIT = 2
"""Passages asked of the official-data and cited-source providers.

They *supplement* the fact-check/web-search chain rather than replacing it, and
the per-claim cap is small (6 by default). Letting a dataset catalogue fill all
six slots would push out the reporting that actually addresses the claim."""

WIRE_SIMILARITY = 0.85
"""Word-overlap at which two passages are taken to be one story, not two sources.

Wire copy is republished verbatim under a dozen mastheads, occasionally with a
word changed in the re-edit. Exact equality misses those; this is deliberately a
high bar, because the cost of a *false* merge — throwing away a genuinely
independent report — is worse than the cost of a missed one. Two reports of the
same event written by two newsrooms share their vocabulary but not this much of
it; two copies of the same agency story share nearly all of it.

Judged on the words a passage uses (:func:`_fingerprint`) rather than on word
order: order-sensitive shingling is more precise on paper and far too brittle
here, where a single re-edited word moves the score by more than the re-edit
deserves. Chosen by hand against the fixtures, and worth revisiting against real
syndicated articles rather than defending in the abstract.

It does **not** catch two *different* extracts from the same syndicated story —
nothing this cheap does — and it deliberately does not merge two passages whose
numbers substantially disagree, however similar their prose (:func:`_fingerprint`
and :data:`NUMBER_AGREEMENT`).
"""

NUMBER_AGREEMENT = 0.5
"""Numeric-token overlap (Jaccard) below which two passages' figures are taken
to genuinely disagree, vetoing a wire-copy merge outright regardless of how
similar their prose is.

**Not** exact equality of the two numeric-token sets — that was the original,
too-strict rule (MAJOR M4). Two syndicated copies of *one* wire story are
routinely snipped at different points: a shorter pickup drops a clause that
named one of the figures the fuller copy kept, which makes their numeric-token
sets differ even though nothing about the story's numbers has changed. Exact
equality treated that as a disagreement and kept the two snippets as two
"independent" sources — precisely the input aggregation's "two or more
independent sources" rule must not be fooled by, since it is a *false*
independence, not a genuine one.

A snip losing one figure the other kept still shares the numbers it kept
(``{4, 12}`` vs ``{4}`` is 1 of 2, i.e. 0.5), so it clears this bar and the
merge proceeds on word similarity as before. A genuine correction or a wire
error's replacement figure (``{4, 12}`` vs ``{14, 20}``, no numbers in
common) sits at 0.0 and stays vetoed. Chosen, like :data:`WIRE_SIMILARITY`,
by hand against the fixtures rather than derived: 0.5 is "at least half the
smaller side's figures still agree", which passes an honest snip and fails a
correction in every case this module's tests construct, and is worth
revisiting against real syndicated copy rather than defended in the
abstract.

When *either* passage mentions no numbers at all, there is nothing to
disagree about, and this check does not apply — see :func:`_similarity`.
"""

ORIGIN_PRIORITY: dict[PassageOrigin, int] = {
    "factcheck": 0,
    "official": 1,
    "cited_source": 2,
    "web": 3,
}
"""How useful a passage's origin makes it, lowest first.

A published review of this claim beats an official dataset about its subject,
which beats the document a quotation actually points at, which beats a page
search found. It decides both which passages survive the cap and which copy of a
wire story represents its group."""

_WORD = re.compile(r"[a-z0-9]+")
"""Tokeniser for wire-copy comparison. Runs over already-normalised text, so it
needs to know nothing about case, punctuation or curly quotes."""

AGGREGATOR_DOMAINS = frozenset(
    {
        "aol.com",
        "apple.news",
        "flipboard.com",
        "headtopics.com",
        "msn.com",
        "news.google.com",
        "newsbreak.com",
        "smartnews.com",
        "yahoo.com",
    }
)
"""Domains that republish other people's reporting.

Matched on the domain or any subdomain of it (``sg.news.yahoo.com`` is Yahoo).
An aggregator is demoted, never dropped: the reader may well be *on* one — the
brief names Yahoo News as a target site — and a syndicated copy is still
evidence. It just should not be the copy we cite when the originating outlet is
also in hand."""


def build_query(claim: ExtractedClaim) -> str:
    """The provider query for ``claim``: its quote, whitespace-collapsed and capped.

    The quote itself, not a keyword extraction: fact-check APIs match on claim
    text, and the search prompt is written to receive a claim. Cutting at
    :data:`MAX_QUERY_CHARS` is a cost ceiling, not a semantic choice.
    """
    return " ".join(claim.quote.split())[:MAX_QUERY_CHARS].strip()


@dataclass(frozen=True, slots=True)
class RetrievalOutcome:
    """What retrieving evidence for one claim produced, and whether it can be trusted.

    ``passages`` is what stages 3 and 4 read — the same thing this function
    always returned. ``providers_failed`` additionally names every provider
    **consulted** during this claim's retrieval that raised, timed out, or
    answered with something that was not a list of
    :class:`~app.pipeline.types.Passage`, as opposed to a provider that was
    consulted and genuinely reported "nothing here" (not a failure at all —
    see :func:`_guarded`).

    The distinction matters only when ``passages`` is empty, which is where
    :attr:`retrieval_broken` lives. See the module docstring's "Failure is a
    missing passage, never an exception — but it is not silent" and BLOCKER
    B5: before this field existed, a total provider outage (an expired key, a
    wrong tool name, a quota block) was indistinguishable from an honestly
    empty web, and :mod:`app.pipeline.run` cached the former as if it were the
    latter for seven days.
    """

    passages: list[Passage]
    providers_failed: tuple[str, ...] = ()

    @property
    def retrieval_broken(self) -> bool:
        """True exactly when nothing was found *and* at least one consulted
        provider failed outright, rather than legitimately answering "no
        results". A claim with this property is not a completed search that
        found nothing — it is a search that did not happen, and
        :mod:`app.pipeline.run` must treat it as a failed claim, not as an
        honest ``unverifiable``.
        """
        return not self.passages and bool(self.providers_failed)


async def retrieve_passages(
    claim: ExtractedClaim,
    *,
    article_url: str,
    providers: Providers,
    settings: Settings,
) -> RetrievalOutcome:
    """Find the evidence for one claim, within the per-claim budget.

    Runs the fact-check → web-search chain (with the short-circuit) concurrently
    with the kind-specific supplement, de-duplicates wire copy, then ranks and
    caps at ``settings.max_passages_per_claim``.

    Returns a :class:`RetrievalOutcome` with ``passages == []`` when nothing was
    found. That alone is a normal outcome — the input to an honest
    ``unverifiable`` — but check :attr:`~RetrievalOutcome.retrieval_broken`
    before treating it that way: an empty result can also mean every provider
    asked about this claim failed rather than answered. Never raises.
    """
    query = build_query(claim)
    if not query:
        return RetrievalOutcome(passages=[])
    limit = max(0, settings.max_passages_per_claim)
    if limit == 0:
        return RetrievalOutcome(passages=[])

    (primary, primary_failed), (supplement, supplement_failed) = await asyncio.gather(
        _factcheck_then_search(query, claim_id=claim.id, providers=providers, limit=limit),
        _supplement(query, claim=claim, article_url=article_url, providers=providers),
    )

    deduped = dedupe_wire_copy([*primary, *supplement])
    kept = rank_and_cap(deduped, limit)
    failed = (*primary_failed, *supplement_failed)
    logger.info(
        "claim %s: %d passage(s) retrieved, %d after de-duplication, %d kept, %s",
        claim.id,
        len(primary) + len(supplement),
        len(deduped),
        len(kept),
        f"provider(s) failed: {', '.join(failed)}" if failed else "no provider failures",
    )
    return RetrievalOutcome(passages=kept, providers_failed=failed)


async def _factcheck_then_search(
    query: str, *, claim_id: str, providers: Providers, limit: int
) -> tuple[list[Passage], list[str]]:
    """Step 1, then step 2 only if step 1 found nothing. **The cost short-circuit.**

    Returns the passages found and the names of any provider in this chain
    that failed outright (as opposed to answering "no results") — see
    :class:`RetrievalOutcome`.
    """
    failed: list[str] = []
    reviews = await _guarded(
        providers.factcheck.search(query, limit=limit),
        timeout=providers.timeout_seconds,
        provider="fact-check",
        claim_id=claim_id,
    )
    if reviews.failed:
        failed.append("fact-check")
    if reviews.passages:
        logger.info(
            "claim %s: %d fact-check review(s) found, skipping web search",
            claim_id,
            len(reviews.passages),
        )
        return reviews.passages, failed
    search = await _guarded(
        providers.search.search(query, limit=limit),
        timeout=providers.timeout_seconds,
        provider="web-search",
        claim_id=claim_id,
    )
    if search.failed:
        failed.append("web-search")
    return search.passages, failed


async def _supplement(
    query: str, *, claim: ExtractedClaim, article_url: str, providers: Providers
) -> tuple[list[Passage], list[str]]:
    """Step 3 or step 4, whichever the claim's kind calls for — or neither.

    Same return shape as :func:`_factcheck_then_search`: passages found, and
    the provider's name if it was consulted and failed.
    """
    if claim.kind == "numeric":
        official = await _guarded(
            providers.official.search(query, limit=SUPPLEMENT_LIMIT),
            timeout=providers.timeout_seconds,
            provider="official-data",
            claim_id=claim.id,
        )
        return official.passages, (["official-data"] if official.failed else [])
    if claim.kind == "attribution":
        cited = await _guarded(
            providers.cited.fetch(query, article_url=article_url, limit=SUPPLEMENT_LIMIT),
            timeout=providers.timeout_seconds,
            provider="cited-source",
            claim_id=claim.id,
        )
        return cited.passages, (["cited-source"] if cited.failed else [])
    return [], []


@dataclass(frozen=True, slots=True)
class _Attempt:
    """One provider call's outcome, before it is folded into a claim's evidence.

    Internal to this module: :func:`_factcheck_then_search` and
    :func:`_supplement` each make one or two of these and reduce them to the
    ``(passages, failed_provider_names)`` shape :func:`retrieve_passages`
    assembles into a :class:`RetrievalOutcome`.
    """

    passages: list[Passage]
    failed: bool


async def _guarded(
    call: Awaitable[list[Passage]], *, timeout: float, provider: str, claim_id: str
) -> _Attempt:
    """Run one provider call so that it cannot fail, hang or poison the claim.

    A timeout, an exception and a return value that is not a list of passages
    all become an empty, ``failed=True`` :class:`_Attempt` plus a log line —
    the provider is an adapter around somebody else's service, and any of
    these is that service having a bad afternoon rather than "no results".
    Cancellation of the *whole job* still propagates — that is the pipeline
    being shut down, not a provider failing.

    ``failed=True`` here is exactly the signal :attr:`RetrievalOutcome.retrieval_broken`
    is built from (BLOCKER B5): before it existed, this function silently
    returned ``[]`` for a provider outage and a provider's honest "nothing
    found" alike, and a reader could not tell a completed search from a
    misconfigured key.
    """
    try:
        result = await asyncio.wait_for(call, timeout=timeout)
    except asyncio.CancelledError:
        raise
    except TimeoutError:
        logger.warning(
            "claim %s: %s provider timed out after %.1fs", claim_id, provider, timeout
        )
        return _Attempt([], failed=True)
    except Exception:
        logger.warning("claim %s: %s provider failed", claim_id, provider, exc_info=True)
        return _Attempt([], failed=True)

    if not isinstance(result, list) or not all(isinstance(item, Passage) for item in result):
        # A provider is an adapter around somebody else's service; a wrong return
        # type here would otherwise surface three stages later as a mystery.
        logger.warning("claim %s: %s provider returned a non-passage result", claim_id, provider)
        return _Attempt([], failed=True)
    return _Attempt(result, failed=False)


# ---------------------------------------------------------------- wire copy


def dedupe_wire_copy(passages: list[Passage]) -> list[Passage]:
    """Collapse near-identical passages, marking cross-domain groups as wire copy.

    Syndicated agency copy appears verbatim on many sites. Counting it as many
    sources would let one story satisfy aggregation's "two or more independent
    sources" rule on its own (``CLAUDE.md`` stage 5), which is exactly the
    mistake that makes a wire error look corroborated.

    The comparison is cheap and explainable — word-overlap of the normalised
    text at :data:`WIRE_SIMILARITY`, with disagreeing numbers vetoing a merge
    outright (:func:`_fingerprint`), clustered greedily in retrieval order. No
    model, no embeddings: this runs per claim inside a reader's wait, and a
    similarity anyone can reason about is worth more here than a better one
    nobody can.

    One representative survives each group, chosen by :func:`ORIGIN_PRIORITY`
    then by not being an aggregator (so the originating outlet wins over the site
    that reprinted it), and it is marked ``wire=True`` **only** when the group
    spanned more than one domain — several passages from one site are duplicates,
    not syndication. Groups keep the position of their first member, so the
    result is deterministic.
    """
    kept: list[Passage] = []
    clusters: list[list[Passage]] = []
    fingerprints: list[tuple[frozenset[str], frozenset[str]]] = []

    for passage in passages:
        fingerprint = _fingerprint(passage.text)
        for index, existing in enumerate(fingerprints):
            if _similarity(fingerprint, existing) >= WIRE_SIMILARITY:
                clusters[index].append(passage)
                break
        else:
            clusters.append([passage])
            fingerprints.append(fingerprint)

    for cluster in clusters:
        if len(cluster) == 1:
            kept.append(cluster[0])
            continue
        representative = min(cluster, key=_representative_key)
        domains = {domain_of(member.url) for member in cluster}
        kept.append(replace(representative, wire=True) if len(domains) > 1 else representative)
    return kept


def _fingerprint(text: str) -> tuple[frozenset[str], frozenset[str]]:
    """Reduce a passage to what de-duplication compares: its words and its numbers.

    Both are taken from :func:`~app.pipeline.types.normalize_for_match`, so
    typography and case never make two copies of one story look different.

    The numbers are kept apart from the words and judged separately, by
    :func:`_similarity`, as an overlap rather than an equality (MAJOR M4): two
    passages whose prose is otherwise identical but whose figures substantially
    disagree are the one case where near-identical text must not be collapsed —
    a correction, a revised estimate and the story it corrects would otherwise
    become a single source, and the surviving copy might be the wrong one.
    Numeric claims are half of what this product checks.
    """
    tokens = _WORD.findall(normalize_for_match(text))
    numbers = frozenset(token for token in tokens if any(char.isdigit() for char in token))
    return frozenset(tokens) - numbers, numbers


def _similarity(
    left: tuple[frozenset[str], frozenset[str]], right: tuple[frozenset[str], frozenset[str]]
) -> float:
    """How alike two fingerprints are: Jaccard over words, unless the numbers disagree.

    Two passages that both cite figures and share fewer than
    :data:`NUMBER_AGREEMENT` of them (Jaccard again, over the numeric tokens
    alone) are vetoed to 0.0 regardless of how similar their prose is — a wire
    correction reads almost identically to the story it corrects, and that is
    exactly the pair this veto exists to keep apart (MAJOR M4).

    The veto applies only when **both** sides mention at least one number: a
    shorter snip that happens to mention none has nothing to disagree about,
    and a passage citing no figures is not evidence that the other passage's
    figures are wrong. Word similarity alone decides that case, same as it
    always has.
    """
    left_words, left_numbers = left
    right_words, right_numbers = right
    if not left_words or not right_words:
        return 0.0
    if left_numbers and right_numbers:
        agreement = len(left_numbers & right_numbers) / len(left_numbers | right_numbers)
        if agreement < NUMBER_AGREEMENT:
            return 0.0
    return len(left_words & right_words) / len(left_words | right_words)


def _representative_key(passage: Passage) -> tuple[int, int, int, str]:
    """Sort key picking which copy of a syndicated story to keep (lowest wins).

    Origin first, then a non-aggregator over an aggregator, then the longer text
    (the fuller copy of the same story), then the URL — which decides nothing on
    merit and everything on determinism, so the same input always yields the same
    citation.
    """
    return (
        ORIGIN_PRIORITY.get(passage.origin, len(ORIGIN_PRIORITY)),
        1 if _is_aggregator(passage.url) else 0,
        -len(passage.text),
        passage.url,
    )


def _is_aggregator(url: str) -> bool:
    """True when ``url``'s domain is, or is a subdomain of, a known aggregator."""
    domain = domain_of(url)
    return any(
        domain == aggregator or domain.endswith(f".{aggregator}")
        for aggregator in AGGREGATOR_DOMAINS
    )


# ---------------------------------------------------------------- ranking


def rank_and_cap(passages: list[Passage], limit: int) -> list[Passage]:
    """Keep the ``limit`` most useful passages: best origin first, aggregators last.

    The cap is a cost control — stages 3 and 4 are billed by what they read
    (``docs/decisions.md`` §9) — so what it keeps matters as much as how many.
    Ranking is by :data:`ORIGIN_PRIORITY`, then by not being an aggregator, and
    the sort is stable, so passages of equal standing stay in retrieval order and
    the result never depends on how a provider happened to order its answer.
    """
    if limit < 1:
        return []
    ranked = sorted(
        passages,
        key=lambda passage: (
            ORIGIN_PRIORITY.get(passage.origin, len(ORIGIN_PRIORITY)),
            1 if _is_aggregator(passage.url) else 0,
        ),
    )
    return ranked[:limit]


# ---------------------------------------------------------------- construction


def build_providers(settings: Settings, *, http: AsyncHttpClient | None = None) -> Providers:
    """Build the real four providers for a run.

    The one place a key is turned into a provider, and the one place a *missing*
    key is decided about:

    * **No ``OPENAI_API_KEY``** raises :class:`~app.config.MissingSettingError`.
      Web search is not optional — without it most claims have no evidence at all
      — and the same key is needed by stages 1, 3 and 4 anyway, so failing here
      is both honest and early.
    * **No ``GOOGLE_FACTCHECK_API_KEY``** is caught and degrades to
      :class:`~app.pipeline.providers.base.NullPassageProvider`: every claim
      falls through to web search, which is more expensive but still correct.
      The warning is the only place anyone will notice the bill changing.

    ``http`` should be a client the caller owns and closes. When it is omitted
    this builds an :class:`~app.pipeline.providers.base.HttpxClient` whose
    lifetime nobody manages, which is convenient in a script and wrong in a
    long-running service.
    """
    client = http if http is not None else HttpxClient()

    factcheck: FactCheckProvider
    try:
        factcheck = GoogleFactCheckProvider(
            http=client,
            api_key=settings.require_google_factcheck_api_key(),
        )
    except MissingSettingError:
        logger.warning(
            "GOOGLE_FACTCHECK_API_KEY is not set: every claim will fall through to web "
            "search, which is the most expensive step per claim."
        )
        factcheck = NullPassageProvider(reason="GOOGLE_FACTCHECK_API_KEY is not set")

    return Providers(
        factcheck=factcheck,
        search=OpenAIWebSearchProvider(
            http=client,
            api_key=settings.require_openai_api_key("web search during evidence retrieval"),
        ),
        official=DataGovSgProvider(http=client),
        cited=LinkedCitationProvider(http=client),
    )
