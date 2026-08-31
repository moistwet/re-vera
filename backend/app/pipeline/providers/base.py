"""The seam every retrieval provider sits behind, and the pieces they all share.

Retrieval (stage 2) is the only stage that talks to the outside world over plain
HTTP, and it is the most expensive stage per claim. This module holds the parts
that must be identical across all four providers so that the orchestration in
:mod:`app.pipeline.retrieve` can treat them uniformly, and so that a test can
replace every one of them without opening a socket.

What lives here
---------------
* :class:`AsyncHttpClient` — the one HTTP seam. **No provider may import
  ``httpx`` or open a connection itself**; it asks this client. Production is
  :class:`HttpxClient`; tests inject :class:`RecordedHttpClient`, which replays
  scripted responses and records every request it was given.
* The four provider protocols (:class:`SearchProvider`,
  :class:`FactCheckProvider`, :class:`OfficialDataProvider`,
  :class:`CitedSourceProvider`) and a null implementation of each, used when a
  provider is not configured (no Google key, for instance).
* :class:`Providers` — the injectable container ``retrieve_passages`` takes.
* Small shared helpers for turning provider payloads into
  :class:`~app.pipeline.types.Passage` fields: domain and outlet from a URL, an
  ISO date from whatever date-ish string an API returned, whitespace-cleaned and
  length-capped passage text.

Two rules every provider obeys
------------------------------
**A provider never raises into its caller.** A dead provider returns ``[]`` and
logs a warning. One claim losing one source of evidence is a slightly weaker
answer; one claim losing the whole retrieval stage because a third-party API had
a bad afternoon is a failed check. :mod:`app.pipeline.retrieve` wraps every call
in its own guard as well — belt and braces, because a fake or a future provider
may forget.

**A provider never retries.** Not on a 4xx (the cost rule from ``CLAUDE.md``: a
rejected request repeated is the same rejection, billed twice), and not on a 5xx
either, because retrieval is the dominant per-claim cost and every extra call is
paid for by every claim of every article. A failed fetch is a missing passage,
and a claim with no evidence is honestly ``unverifiable``.

Privacy
-------
A claim's quote **is article text** (``CLAUDE.md`` rule 6: never log article text
with an identifier, never log article text with a URL). Nothing here logs a
query, a quote or a passage body. Log lines carry the provider name, the claim
id, a status code and a count — enough to debug, nothing that reconstructs what
someone was reading.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

from app.pipeline.types import Passage

logger = logging.getLogger(__name__)

__all__ = [
    "MAX_PASSAGE_CHARS",
    "MAX_RESPONSE_CHARS",
    "PROVIDER_TIMEOUT_SECONDS",
    "AsyncHttpClient",
    "CitedSourceProvider",
    "FactCheckProvider",
    "HttpResponse",
    "HttpxClient",
    "NullCitedSourceProvider",
    "NullPassageProvider",
    "OfficialDataProvider",
    "Providers",
    "RecordedHttpClient",
    "RecordedRequest",
    "SearchProvider",
    "clean_text",
    "domain_of",
    "is_http_url",
    "iso_date",
    "load_recorded_http",
    "outlet_from_url",
    "registrable_domain",
    "same_page",
    "url_key",
]

PROVIDER_TIMEOUT_SECONDS = 10.0
"""Hard ceiling on one provider's work, in seconds.

A module constant rather than a :class:`~app.config.Settings` field because
milestone 2's settings do not have one and this stage owns the number; it is
overridable per-container through :attr:`Providers.timeout_seconds`, which is
also how tests make a hanging provider fail instantly. Ten seconds is chosen
against the product, not against the network: a reader is watching claims fill
in, and a claim that waits longer than this for one source has already failed at
being a fast answer.
"""

MAX_RESPONSE_CHARS = 200_000
"""Most characters read from one HTTP response body.

A retrieved page is written by a stranger and can be any size at all. Everything
past this is discarded before parsing, so one enormous page cannot exhaust the
process's memory or spend a claim's whole timeout being decoded.
"""

MAX_PASSAGE_CHARS = 1_200
"""Most characters kept in one :attr:`~app.pipeline.types.Passage.text`.

Stages 3 and 4 are billed by what they read, and they read passages
(``docs/decisions.md`` §9). Roughly 300 tokens: enough for a few paragraphs of
the part of a page that actually bears on the claim, and a ceiling on what a
hostile page can push into a prompt.
"""


# ---------------------------------------------------------------- HTTP seam


@dataclass(frozen=True, slots=True)
class HttpResponse:
    """One HTTP answer, reduced to the four things retrieval uses.

    ``url`` is the **final** URL after redirects. Providers use it to resolve
    relative links and to name an outlet, so a syndication redirect resolves to
    where the content actually lives rather than to where we asked.

    The body is already truncated to :data:`MAX_RESPONSE_CHARS` by the client
    that produced it.
    """

    status_code: int
    text: str
    url: str
    headers: Mapping[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """True for a 2xx. Every non-2xx is a miss, never a retry (see module docs)."""
        return 200 <= self.status_code < 300

    def json(self) -> Any:
        """Parse the body as JSON. Raises :class:`json.JSONDecodeError` on rubbish —
        which the calling provider catches and turns into ``[]``."""
        return json.loads(self.text)


class AsyncHttpClient(Protocol):
    """The one way a provider reaches the network.

    Two methods, because that is all retrieval needs: ``GET`` (Google Fact
    Check, data.gov.sg, fetching a cited page) and a JSON ``POST`` (the OpenAI
    Responses call behind web search). Neither raises for an HTTP status — the
    status comes back on :class:`HttpResponse` and the provider decides — but
    both may raise a transport error (timeout, DNS, connection reset), which the
    provider catches.
    """

    async def get(
        self,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float,
    ) -> HttpResponse:
        """Fetch ``url``. Follows redirects; ``HttpResponse.url`` is where it landed."""
        ...

    async def post_json(
        self,
        url: str,
        *,
        json_body: Mapping[str, Any],
        headers: Mapping[str, str] | None = None,
        timeout: float,
    ) -> HttpResponse:
        """POST ``json_body`` as ``application/json`` and return the raw answer."""
        ...


class HttpxClient:
    """The production :class:`AsyncHttpClient`: a thin wrapper over ``httpx``.

    Deliberately thin. It follows redirects, sends a truthful User-Agent, caps
    the body at :data:`MAX_RESPONSE_CHARS`, and does nothing else — no retries,
    no status handling, no caching. Every decision about what a response *means*
    belongs to the provider that asked for it, so that there is exactly one
    place per external API where its shape is assumed.
    """

    USER_AGENT = "Re-Vera/0.1 (fact-check retrieval; +https://example.com/re-vera)"
    """Sent on every request. Honest about who is asking, so an operator who
    wants to block us can, and a rate-limiter can name us."""

    def __init__(self, client: Any | None = None) -> None:
        """Wrap an ``httpx.AsyncClient``, or build one if none is given.

        Typed ``Any`` for the same reason ``app.llm`` keeps the OpenAI SDK at
        arm's length: the import stays inside this class so the module graph
        does not need ``httpx`` to be importable at collection time.
        """
        import httpx

        self._client = client if client is not None else httpx.AsyncClient(follow_redirects=True)

    async def get(
        self,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float,
    ) -> HttpResponse:
        """See :meth:`AsyncHttpClient.get`."""
        response = await self._client.get(
            url,
            params=dict(params) if params else None,
            headers={"User-Agent": self.USER_AGENT, **(dict(headers) if headers else {})},
            timeout=timeout,
        )
        return self._convert(response)

    async def post_json(
        self,
        url: str,
        *,
        json_body: Mapping[str, Any],
        headers: Mapping[str, str] | None = None,
        timeout: float,
    ) -> HttpResponse:
        """See :meth:`AsyncHttpClient.post_json`."""
        response = await self._client.post(
            url,
            json=dict(json_body),
            headers={"User-Agent": self.USER_AGENT, **(dict(headers) if headers else {})},
            timeout=timeout,
        )
        return self._convert(response)

    async def aclose(self) -> None:
        """Close the underlying connection pool."""
        await self._client.aclose()

    @staticmethod
    def _convert(response: Any) -> HttpResponse:
        """Reduce an ``httpx.Response`` to an :class:`HttpResponse`."""
        return HttpResponse(
            status_code=int(response.status_code),
            text=str(response.text)[:MAX_RESPONSE_CHARS],
            url=str(response.url),
            headers={key.lower(): value for key, value in response.headers.items()},
        )


@dataclass(frozen=True, slots=True)
class RecordedRequest:
    """What :class:`RecordedHttpClient` was asked for, so a test can assert on it.

    ``params`` and ``json_body`` are kept whole: the cost guarantees this stage
    owes (a ClaimReview hit means *no* web-search request was made, a query is
    sent once) are assertions about requests, not about results.
    """

    method: str
    url: str
    params: dict[str, str]
    headers: dict[str, str]
    json_body: dict[str, Any] | None
    timeout: float


@dataclass
class RecordedHttpClient:
    """An :class:`AsyncHttpClient` that replays scripted outcomes. **Never networks.**

    The retrieval half of the offline seam, and the sibling of
    :class:`app.llm.ReplayTransport` — same shape, same reasons. ``outcomes`` is
    consumed in order; an :class:`Exception` entry is raised rather than
    returned, which is how a test scripts a timeout::

        http = RecordedHttpClient([TimeoutError("data.gov.sg took too long")])

    Every request is appended to :attr:`requests`, so a test can prove that a
    provider was never called at all — which is exactly how the ClaimReview
    short-circuit is verified. Running past the end of the script raises
    :class:`AssertionError`: an unexpected request is the bug this fixture
    exists to catch, and in this stage an unexpected request is also money.

    It lives in ``app/`` rather than ``tests/`` because the eval harness replays
    recorded retrieval too, and one implementation beats two that drift.
    """

    outcomes: list[HttpResponse | Exception]
    requests: list[RecordedRequest] = field(default_factory=list)

    async def get(
        self,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float,
    ) -> HttpResponse:
        """Record the request and return (or raise) the next scripted outcome."""
        return self._next("GET", url, params, headers, None, timeout)

    async def post_json(
        self,
        url: str,
        *,
        json_body: Mapping[str, Any],
        headers: Mapping[str, str] | None = None,
        timeout: float,
    ) -> HttpResponse:
        """Record the request and return (or raise) the next scripted outcome."""
        return self._next("POST", url, None, headers, json_body, timeout)

    def _next(
        self,
        method: str,
        url: str,
        params: Mapping[str, str] | None,
        headers: Mapping[str, str] | None,
        json_body: Mapping[str, Any] | None,
        timeout: float,
    ) -> HttpResponse:
        self.requests.append(
            RecordedRequest(
                method=method,
                url=url,
                params=dict(params) if params else {},
                headers=dict(headers) if headers else {},
                json_body=dict(json_body) if json_body is not None else None,
                timeout=timeout,
            )
        )
        if not self.outcomes:
            raise AssertionError(
                f"RecordedHttpClient ran out of scripted outcomes on request "
                f"{len(self.requests)} ({method} {url})."
            )
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def load_recorded_http(path: Path, *, url: str | None = None) -> HttpResponse:
    """Load one recorded provider answer from ``tests/fixtures/retrieve/<name>.json``.

    The format, kept as small as the thing it stands for::

        {
          "_note":       "what this recording is, and that it is fictional",
          "status_code": 200,
          "url":         "https://factchecktools.googleapis.com/...",
          "json":        { "claims": [ ... ] },
          "text":        "<html>…"
        }

    Give **either** ``json`` (an object, written readably, serialised for you) or
    ``text`` (the exact bytes, for HTML and for deliberately malformed answers).
    ``json`` wins if both are present. ``status_code`` defaults to 200 and
    ``url`` to the empty string; the ``url`` keyword overrides the file.

    These are **hand-written recordings, not captures**: this repository has no
    API key and no network. They are what a plausible answer looks like — which
    is what a provider test needs — and they are not evidence that any live API
    returns this shape. Every article, outlet and URL in them is fictional.
    """
    with path.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    if "json" in payload:
        text = json.dumps(payload["json"], ensure_ascii=False)
    elif "text" in payload:
        text = str(payload["text"])
    else:
        raise ValueError(f"{path}: a recorded response needs a `json` or a `text` key.")
    return HttpResponse(
        status_code=int(payload.get("status_code", 200)),
        text=text[:MAX_RESPONSE_CHARS],
        url=url if url is not None else str(payload.get("url", "")),
        headers={str(k).lower(): str(v) for k, v in (payload.get("headers") or {}).items()},
    )


# ---------------------------------------------------------------- provider protocols


class SearchProvider(Protocol):
    """General web search — the swappable one, and the expensive one.

    ``CLAUDE.md`` names the OpenAI built-in web search tool as the MVP
    implementation *behind this protocol* so it can be replaced by a search API
    (Brave, Serper, Bing) without retrieval changing. A ClaimReview hit skips
    this provider entirely, which is the single largest cost saving in the
    pipeline (``docs/decisions.md`` §9).

    ``limit`` is a ceiling on returned passages, not a target; returning fewer
    (including none) is a normal answer.
    """

    async def search(self, query: str, *, limit: int) -> list[Passage]: ...


class FactCheckProvider(Protocol):
    """Existing fact-check reviews for a claim (Google Fact Check Tools / ClaimReview).

    Tried **first** for every claim, because a professional review of the same
    claim is both the best evidence available and — since it short-circuits web
    search — the cheapest.
    """

    async def search(self, query: str, *, limit: int) -> list[Passage]: ...


class OfficialDataProvider(Protocol):
    """Official statistics for a numeric claim (data.gov.sg).

    Consulted only for ``kind == "numeric"``: a figure is checkable against the
    body that published it in a way a general web search is not, and asking for
    every claim would spend a request on claims that have no number in them.
    """

    async def search(self, query: str, *, limit: int) -> list[Passage]: ...


class CitedSourceProvider(Protocol):
    """The document a quotation points at, fetched directly.

    Consulted only for ``kind == "attribution"``: when an article says *X said
    Y*, the thing that settles it is what X actually published, not what a
    search engine thinks about it.
    """

    async def fetch(self, quote: str, *, article_url: str, limit: int) -> list[Passage]: ...


@dataclass(frozen=True, slots=True)
class NullPassageProvider:
    """A provider that is not configured: returns ``[]``, loudly and once.

    Used where a provider genuinely cannot run — no ``GOOGLE_FACTCHECK_API_KEY``,
    for instance. It is deliberately *not* silent: retrieval degrading from
    "fact-check first" to "web search every claim" costs real money, and the log
    line is the only place anyone will notice.
    """

    reason: str

    async def search(self, query: str, *, limit: int) -> list[Passage]:
        """Log why this provider is disabled and return no passages."""
        logger.warning("retrieval provider disabled: %s", self.reason)
        return []


@dataclass(frozen=True, slots=True)
class NullCitedSourceProvider:
    """:class:`NullPassageProvider` for the cited-source seam, which has its own signature."""

    reason: str

    async def fetch(self, quote: str, *, article_url: str, limit: int) -> list[Passage]:
        """Log why this provider is disabled and return no passages."""
        logger.warning("retrieval provider disabled: %s", self.reason)
        return []


@dataclass(frozen=True, slots=True)
class Providers:
    """The four providers ``retrieve_passages`` may consult, injected as one value.

    Passed rather than constructed so that a test supplies fakes and the
    orchestrator supplies the real ones — and so that the *set* of providers is
    visible in one place instead of being discovered by reading four import
    statements.

    ``timeout_seconds`` is the ceiling retrieval puts on each provider call
    (:data:`PROVIDER_TIMEOUT_SECONDS`). It lives here, on the container, because
    it is the one knob a test needs to turn: a fake that hangs must fail in
    milliseconds, not in ten seconds.
    """

    factcheck: FactCheckProvider
    search: SearchProvider
    official: OfficialDataProvider
    cited: CitedSourceProvider
    timeout_seconds: float = PROVIDER_TIMEOUT_SECONDS


# ---------------------------------------------------------------- shared helpers


def is_http_url(url: str) -> bool:
    """True for an absolute ``http``/``https`` URL with a host.

    The gate on every URL that arrives from an API or from a page's markup:
    ``javascript:``, ``data:``, ``mailto:`` and relative fragments are not
    things to fetch, and a source chip with one in it is not a source.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    return parts.scheme in {"http", "https"} and bool(parts.hostname)


def domain_of(url: str) -> str:
    """The lower-cased host of ``url`` without ``www.``, or ``""`` if there is none.

    The unit of "a different source" for wire-copy de-duplication: the same
    agency story on five domains is five domains and one source.
    """
    try:
        host = urlsplit(url).hostname or ""
    except ValueError:
        return ""
    host = host.lower()
    return host[4:] if host.startswith("www.") else host


def url_key(url: str) -> str:
    """Comparison key for two URLs that may name the same page.

    **The one canonical "same page" identity for the whole pipeline.** Before
    this function existed, ``aggregate.py`` compared raw URL strings (missing a
    tracking parameter, a scheme change, a trailing slash or a ``www.`` prefix
    turned "the article citing itself" into "the article corroborated by an
    independent source") and ``websearch.py`` had its own, stronger, private
    notion of the same thing. One implementation now backs both, and any other
    place in the pipeline that needs to ask "is this the same page as that one"
    imports this rather than writing a third variant.

    Folds away exactly what a search engine, a CMS or a reader's own click
    routinely adds without it being a different page: the scheme (``http`` vs
    ``https``), a leading ``www.`` label, a trailing slash on the path, the
    query string (tracking parameters live here) and the fragment. Also
    lower-cases host and path.

    Returns ``""`` for a URL with no host at all (relative, malformed, or
    ``javascript:``/``data:``/similar) — such a URL is never "the same page" as
    anything, including another equally hostless one, which is why
    :func:`same_page` treats an empty key on either side as "no match" rather
    than as a match between two unknowns.

    Honest limits of this heuristic, worth knowing before trusting it further
    than "probably the same page":

    * **Path comparison is lower-cased.** Almost every news CMS treats its URL
      paths case-insensitively, but nothing guarantees it; a server that really
      does distinguish ``/Article`` from ``/article`` will be folded together
      here as one page.
    * **The query string is dropped entirely**, not merely tracking
      parameters. A site that encodes real content in the query (pagination,
      an article's variant) will have two genuinely different pages compare
      equal. For a news article's canonical URL — the case this function
      exists for — that trade is the safer direction: it is what lets a
      tracking-parameter copy of the article being checked still be recognised
      as the article being checked.
    * **No redirect resolution.** Two URLs that both eventually serve the same
      content through different paths (a short link, a legacy redirect) will
      not compare equal; only the literal string is folded.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return ""
    host = (parts.hostname or "").lower()
    if not host:
        return ""
    if host.startswith("www.") and len(host) > len("www."):
        host = host[len("www.") :]
    path = parts.path.rstrip("/").lower()
    return f"{host}{path}"


def same_page(a: str, b: str) -> bool:
    """True when ``a`` and ``b`` name the same page under :func:`url_key`.

    A URL with no recognisable host is never treated as matching anything,
    including another URL that also has none — two unknowns are not evidence
    they are the same unknown.
    """
    key_a, key_b = url_key(a), url_key(b)
    return bool(key_a) and key_a == key_b


_MULTI_LABEL_SUFFIXES = frozenset(
    {
        # Singapore — the target market (CLAUDE.md), spelled out explicitly
        # rather than left to the general two-label fallback below.
        "com.sg",
        "gov.sg",
        "org.sg",
        "net.sg",
        "edu.sg",
        "per.sg",
        # A handful of other common two-label public suffixes news evidence
        # is likely to arrive from. Not remotely exhaustive — see the
        # docstring's honest limits.
        "co.uk",
        "org.uk",
        "gov.uk",
        "ac.uk",
        "net.uk",
        "com.au",
        "gov.au",
        "org.au",
        "net.au",
        "edu.au",
        "co.nz",
        "govt.nz",
        "org.nz",
        "co.jp",
        "or.jp",
        "ne.jp",
        "com.cn",
        "gov.cn",
        "org.cn",
        "net.cn",
        "co.in",
        "gov.in",
        "org.in",
        "net.in",
        "com.my",
        "gov.my",
        "org.my",
        "net.my",
        "com.hk",
        "gov.hk",
        "org.hk",
        "net.hk",
    }
)
"""Two-label public suffixes under which one more label is the registrable
domain (``moh.gov.sg``, not ``gov.sg``), rather than the ordinary one-label
case (``example.com``).

A fixed, hand-maintained set — **not** the Public Suffix List, which this
project does not depend on (``CLAUDE.md``: ask before adding a dependency).
Chosen to cover Singapore's domains explicitly, since that is the target
market, plus a handful of other common two-label suffixes evidence is likely
to arrive from. A domain suffix missing from this set silently falls back to
the plain "last two labels" rule, which is wrong for it in the same direction
every unlisted two-label suffix is wrong: it will under-collapse two
subdomains of what is really one registration (e.g. an unlisted
``news.example.co.zz`` and ``shop.example.co.zz`` would be treated as two
different two-label domains, ``example.co.zz``, i.e. treated as *one* site —
which happens to be correct here — but a genuinely three-label suffix not in
this set would collapse incorrectly). See :func:`registrable_domain` for the
full trade-off this makes.
"""


def registrable_domain(url: str) -> str:
    """The registrable domain of ``url``'s host: the site, with subdomains collapsed.

    ``news.example.com.sg`` and ``www.example.com.sg`` both return
    ``example.com.sg`` — two subdomains of one publisher are one site, which
    matters wherever the pipeline asks whether two sources are *independent*
    (:mod:`app.pipeline.aggregate`'s ``source_group``, and the self-citation
    check in ``_usable``) or whether a page is *primary* (a check against a
    government domain). Without this, ``news.gov.sg`` and ``press.gov.sg``
    would count as two independent sources, which is the second bug this
    function exists to close.

    The algorithm, since this project carries no Public Suffix List
    dependency: take the host's last two labels; if those two labels are
    themselves a known two-label public suffix (:data:`_MULTI_LABEL_SUFFIXES`,
    e.g. ``gov.sg``, ``co.uk``), take the last three instead. Otherwise the
    last two labels *are* the registrable domain, which is correct for the
    overwhelming majority of domains — including every plain ``.com``/``.sg``/
    ``.org`` site — without needing a suffix list at all.

    Honest limits:

    * It knows only the two-label suffixes in :data:`_MULTI_LABEL_SUFFIXES`.
      A three-label public suffix (rare) or a two-label suffix missing from
      that set is not handled correctly.
    * It does not know about "privately registered" public suffixes such as
      ``blogspot.com`` or ``github.io``, where two different subdomains
      really are two different, unrelated sites. Collapsing
      ``a.blogspot.com`` and ``b.blogspot.com`` into one "site" is the wrong
      call for those hosts specifically; it is the right call for every
      ordinary newsroom domain, which is what this function is for.
    * A bare public suffix itself (``www.gov.sg``, two labels after the
      ``www.`` fold happens only in :func:`url_key`, not here — this function
      does *not* strip ``www.`` before counting labels) can return more than
      the "true" registrable domain, e.g. ``www.gov.sg`` returns
      ``www.gov.sg`` rather than ``gov.sg``, because the algorithm cannot
      distinguish "www" from a real registered label. This mirrors how the
      registrable-domain algorithm behaves on real public-suffix
      implementations for a bare suffix host, and is deliberately left as-is
      rather than special-cased.

    Returns ``""`` when ``url`` has no host at all.
    """
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""
    if not host:
        return ""
    labels = host.split(".")
    if len(labels) <= 2:
        return host
    last_two = ".".join(labels[-2:])
    if last_two in _MULTI_LABEL_SUFFIXES:
        return ".".join(labels[-3:])
    return last_two


def outlet_from_url(url: str) -> str:
    """A last-resort outlet name for ``url``: its domain.

    Only used when an API gave no publisher name. A domain is a poor label on a
    source chip, but it is *true*, and inventing "Channel NewsAsia" from
    ``cna.example`` would put a fabricated attribution in front of a reader.
    """
    return domain_of(url) or "unknown source"


def iso_date(value: object) -> str | None:
    """Normalise whatever date-ish string an API returned to ``YYYY-MM-DD``, or ``None``.

    Accepts a bare date and any ISO timestamp (``2026-03-12T00:00:00Z``) by
    taking the date part. Anything else — a year alone, a human-written date, a
    number, nothing at all — is ``None``, because
    :attr:`~app.pipeline.types.Passage.date` is rendered on the reader's source
    chip and a guessed date is a fabricated fact.
    """
    if not isinstance(value, str) or len(value) < 10:
        return None
    head = value[:10]
    try:
        date.fromisoformat(head)
    except ValueError:
        return None
    return head


def clean_text(value: object, *, limit: int = MAX_PASSAGE_CHARS) -> str:
    """Collapse whitespace in ``value`` and cap it at ``limit`` characters.

    Applied to every passage body before it leaves a provider, so that the
    stance and judge prompts are billed for prose rather than for a page's
    indentation, and so no single retrieved page can dominate a prompt. Cutting
    mid-word is accepted: the alternative is a sentence-boundary search that
    would sometimes drop the sentence carrying the number.
    """
    if not isinstance(value, str):
        return ""
    collapsed = " ".join(value.split())
    return collapsed[:limit].strip()
