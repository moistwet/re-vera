"""The cited source — for an attribution claim, fetch what was actually said.

Consulted in addition to the fact-check/web-search chain when a claim is tagged
``attribution``: *"the ministry said X"*, *"according to a CNA report, Y"*. What
settles that kind of claim is the thing being quoted, not what a search engine
thinks about it, and news articles usually link to it.

How the cited source is found
-----------------------------
:class:`~app.pipeline.types.ExtractedClaim` carries no "cited URL" field — an
extraction model cannot reliably produce one, and a URL invented by a model is a
fabricated source. So this provider derives it from the page the reader is on:

1. fetch the article;
2. pull out its outbound links and their anchor text;
3. keep the ones whose anchor text or URL slug shares significant words with the
   claim (a link labelled "the ministry's press release" scores on *ministry*
   and *release*), dropping navigation, share buttons and same-page links;
4. fetch the best one and take the part of it that mentions those same words.

It is a heuristic, and it is meant to be: a claim whose citation cannot be found
this way simply gets no ``cited_source`` passage, which is a weaker answer, not
a wrong one. What it must never do is return a passage from a page that is not
the cited source, so the scoring gate is deliberately strict — at least one
substantial shared word — and a page that shares nothing is skipped.

HTML parsing without a parser
-----------------------------
Links, metadata and body text are pulled out with regular expressions rather
than an HTML parser. That is a dependency decision (``CLAUDE.md``: ask before
adding one; the standard library's ``html.parser`` would need a subclass per
extraction), and it is safe *here* specifically because every failure mode is
"we find nothing" rather than "we find the wrong thing": a missed link is a
missed citation, and the text extraction is only ever used as evidence text that
a human sees attributed to the URL it came from. It is not safe as a general
HTML strategy and should not be copied to anywhere that matters more.

The assumed shape
-----------------
Ordinary HTML, and nothing about any particular site. Publication dates are read
from the three places news sites actually put them — JSON-LD ``datePublished``,
``<meta property="article:published_time">``, ``<time datetime="…">`` — and the
outlet from ``og:site_name``, falling back to the domain. **No live page was
fetched in this environment**; the fixtures are hand-written HTML.

Budget
------
At most :attr:`LinkedCitationProvider.max_fetches` HTTP requests per claim, the
article page included — two by default, so one candidate is tried. Retrieval is
the expensive stage and a link-following crawler is how it would stop being
affordable.
"""

from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

from app.pipeline.providers.base import (
    MAX_PASSAGE_CHARS,
    PROVIDER_TIMEOUT_SECONDS,
    AsyncHttpClient,
    HttpResponse,
    clean_text,
    domain_of,
    is_http_url,
    iso_date,
    outlet_from_url,
)
from app.pipeline.types import Passage, normalize_for_match

logger = logging.getLogger(__name__)

__all__ = ["SKIP_DOMAINS", "LinkedCitationProvider"]

SKIP_DOMAINS = frozenset(
    {
        "facebook.com",
        "instagram.com",
        "linkedin.com",
        "pinterest.com",
        "reddit.com",
        "t.me",
        "telegram.me",
        "threads.net",
        "tiktok.com",
        "twitter.com",
        "wa.me",
        "whatsapp.com",
        "x.com",
    }
)
"""Domains that are never a cited source on a news page.

Every one of them is a share button or a follow link. Fetching one spends the
claim's single candidate fetch on a login wall."""

MIN_WORD_LENGTH = 4
"""Shortest token that counts as a significant word.

Below this the vocabulary is mostly grammar — *said*, *the*, *from* — and every
link on the page matches every claim."""

_STOPWORDS = frozenset(
    {
        "about",
        "after",
        "also",
        "been",
        "being",
        "could",
        "does",
        "from",
        "have",
        "here",
        "into",
        "more",
        "most",
        "must",
        "over",
        "said",
        "says",
        "some",
        "such",
        "than",
        "that",
        "them",
        "then",
        "there",
        "these",
        "they",
        "this",
        "told",
        "were",
        "what",
        "when",
        "which",
        "will",
        "with",
        "would",
        "your",
    }
)
"""Common words long enough to pass :data:`MIN_WORD_LENGTH` but carrying no topic.

Small and hand-picked rather than a linguistic stopword list: the job is to stop
"said" and "would" from matching every link on the page, not to do NLP."""

_ANCHOR = re.compile(r"<a\b[^>]*?\bhref\s*=\s*[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", re.I | re.S)
_TAG = re.compile(r"<[^>]+>")
_SCRIPT_OR_STYLE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.I | re.S)
_COMMENT = re.compile(r"<!--.*?-->", re.S)
_WORD = re.compile(r"[a-z0-9]+")
_META_CONTENT = (
    r"<meta[^>]+(?:property|name)\s*=\s*[\"']{key}[\"'][^>]*?content\s*=\s*[\"']([^\"']*)[\"']"
)
_META_CONTENT_REVERSED = (
    r"<meta[^>]+content\s*=\s*[\"']([^\"']*)[\"'][^>]*?(?:property|name)\s*=\s*[\"']{key}[\"']"
)
_JSON_LD_DATE = re.compile(r"\"datePublished\"\s*:\s*\"([^\"]+)\"", re.I)
_TIME_DATETIME = re.compile(r"<time[^>]+datetime\s*=\s*[\"']([^\"']+)[\"']", re.I)

WINDOW_LEAD_CHARS = 200
"""Characters kept before the first matching word in the cited page.

Enough to carry the start of the sentence and usually the one before it, so the
passage reads as prose rather than starting mid-clause."""


@dataclass(frozen=True, slots=True)
class LinkedCitationProvider:
    """Fetch the document an attribution claim points at, via the article's own links.

    Needs no API key. Never raises: a failed fetch, a page with no links, a page
    whose links match nothing, and a cited page that turns out to be a PDF are
    all ``[]``.
    """

    http: AsyncHttpClient
    timeout: float = PROVIDER_TIMEOUT_SECONDS
    max_fetches: int = 2

    async def fetch(self, quote: str, *, article_url: str, limit: int) -> list[Passage]:
        """Return up to ``limit`` passages from the source ``quote`` attributes to.

        Neither the quote nor a URL is logged (``CLAUDE.md`` rule 6): log lines
        carry a domain and a count.
        """
        budget = min(limit, self.max_fetches - 1)
        if budget < 1 or not is_http_url(article_url):
            return []
        keywords = _keywords(quote)
        if not keywords:
            return []

        article = await self._get(article_url)
        if article is None:
            return []

        candidates = _rank_candidates(article.text, base_url=article.url or article_url,
                                      keywords=keywords)
        passages: list[Passage] = []
        for candidate_url in candidates[:budget]:
            page = await self._get(candidate_url)
            if page is None:
                continue
            passage = _passage_from_page(page, keywords=keywords, fallback_url=candidate_url)
            if passage is not None:
                passages.append(passage)
        logger.info(
            "cited-source provider: %d of %d candidate(s) fetched, %d passage(s) kept",
            min(len(candidates), budget),
            len(candidates),
            len(passages),
        )
        return passages

    async def _get(self, url: str) -> HttpResponse | None:
        """One guarded fetch. ``None`` for anything that is not usable HTML."""
        try:
            response = await self.http.get(url, timeout=self.timeout)
        except Exception:
            logger.warning("cited-source provider: fetch failed for %s", domain_of(url))
            return None
        if not response.ok:
            logger.warning(
                "cited-source provider: HTTP %s from %s", response.status_code, domain_of(url)
            )
            return None
        content_type = response.headers.get("content-type", "")
        if content_type and "html" not in content_type.lower():
            # A PDF press release is a real cited source and a real limitation:
            # this provider reads HTML, and pretending otherwise would produce a
            # passage of binary noise.
            logger.info("cited-source provider: skipped non-HTML from %s", domain_of(url))
            return None
        return response


def _keywords(quote: str) -> set[str]:
    """The significant words of ``quote``: folded, long enough, not grammar."""
    folded = normalize_for_match(quote)
    return {
        word
        for word in _WORD.findall(folded)
        if len(word) >= MIN_WORD_LENGTH and word not in _STOPWORDS
    }


def _strip_html(markup: str) -> str:
    """Reduce markup to readable text: no scripts, no comments, no tags, no runs."""
    without_scripts = _SCRIPT_OR_STYLE.sub(" ", markup)
    without_comments = _COMMENT.sub(" ", without_scripts)
    text = _TAG.sub(" ", without_comments)
    return " ".join(html.unescape(text).split())


def _rank_candidates(markup: str, *, base_url: str, keywords: set[str]) -> list[str]:
    """Outbound links from ``markup``, best citation candidate first.

    Scored on how many of the claim's significant words appear in the anchor
    text or the URL's own path — the two places a news site says what it is
    linking to. A link that shares nothing scores zero and is dropped entirely,
    because "some link on the page" is not a citation.
    """
    article_key = _page_key(base_url)
    scored: list[tuple[int, int, str]] = []
    seen: set[str] = set()

    for order, match in enumerate(_ANCHOR.finditer(markup)):
        href, anchor_markup = match.group(1), match.group(2)
        url = urljoin(base_url, html.unescape(href.strip()))
        if not is_http_url(url):
            continue
        key = _page_key(url)
        if key == article_key or key in seen:
            continue
        if domain_of(url) in SKIP_DOMAINS:
            continue
        if not urlsplit(url).path.strip("/"):
            # A bare domain is a masthead or a nav link, never a cited document.
            continue
        anchor_text = _strip_html(anchor_markup)
        haystack = f"{anchor_text} {urlsplit(url).path.replace('-', ' ').replace('/', ' ')}"
        score = len(keywords & set(_WORD.findall(normalize_for_match(haystack))))
        if score < 1:
            continue
        seen.add(key)
        # Negative score sorts best-first; `order` keeps ties in page order, so
        # the same page always yields the same candidate.
        scored.append((-score, order, url))

    scored.sort()
    return [url for _, _, url in scored]


def _passage_from_page(
    page: HttpResponse, *, keywords: set[str], fallback_url: str
) -> Passage | None:
    """Build one ``cited_source`` passage from a fetched page, or ``None``.

    Returns ``None`` when the page's text shares none of the claim's significant
    words: the link looked right, the page is about something else, and a
    passage from it would be evidence about the wrong subject.
    """
    text = _strip_html(page.text)
    if not text:
        return None
    window = _best_window(text, keywords)
    if not window:
        return None
    url = page.url or fallback_url
    return Passage(
        text=clean_text(window),
        url=url,
        outlet=_meta(page.text, "og:site_name") or outlet_from_url(url),
        date=_published_date(page.text),
        wire=False,
        origin="cited_source",
        rating=None,
    )


def _best_window(text: str, keywords: set[str]) -> str:
    """The most relevant :data:`MAX_PASSAGE_CHARS` of ``text``, or ``""``.

    Finds the earliest occurrence of any of the claim's significant words and
    keeps a window around it. Earliest rather than densest on purpose: a news
    page states its subject at the top, and the cheapest correct answer beats a
    scoring pass over the whole document.
    """
    folded = normalize_for_match(text)
    positions = [folded.find(word) for word in keywords]
    hits = [position for position in positions if position >= 0]
    if not hits:
        return ""
    # The folded text collapses whitespace, so an offset in it can sit slightly
    # ahead of the same word in the original. The window is generous enough
    # (200 characters of lead) that the drift never pushes the match out of it.
    start = max(0, min(hits) - WINDOW_LEAD_CHARS)
    return text[start : start + MAX_PASSAGE_CHARS]


def _meta(markup: str, key: str) -> str:
    """One ``<meta>`` value by ``property``/``name``, tolerating attribute order."""
    for pattern in (_META_CONTENT, _META_CONTENT_REVERSED):
        match = re.search(pattern.format(key=re.escape(key)), markup, re.I)
        if match:
            return clean_text(html.unescape(match.group(1)), limit=120)
    return ""


def _published_date(markup: str) -> str | None:
    """The page's publication date as ``YYYY-MM-DD``, or ``None`` if it states none.

    Tries the three places news sites put it, in order of how deliberate they
    are: JSON-LD, the Open Graph article metadata, then a ``<time>`` element. A
    page with none of them gets ``None`` rather than a guess — the date is shown
    on the reader's source chip.
    """
    match = _JSON_LD_DATE.search(markup)
    if match:
        parsed = iso_date(match.group(1))
        if parsed:
            return parsed
    meta_date = _meta(markup, "article:published_time")
    parsed = iso_date(meta_date)
    if parsed:
        return parsed
    time_match = _TIME_DATETIME.search(markup)
    if time_match:
        return iso_date(time_match.group(1))
    return None


def _page_key(url: str) -> str:
    """Identity of a page for de-duplication: host without ``www.`` plus path."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    host = host[4:] if host.startswith("www.") else host
    return f"{host}{parts.path.rstrip('/').lower()}"
