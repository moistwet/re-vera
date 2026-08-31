"""Pipeline-internal types (:mod:`app.pipeline.types`) and the canonical URL
identity primitives (:mod:`app.pipeline.providers.base`), entirely offline.

Two things are pinned here that other stages depend on without re-deriving:

* :class:`~app.pipeline.types.Passage` defaults ``provenance_verified`` to
  ``False``. A provider that forgets to set it produces an *unverified*
  passage, never a silently-trusted one (redteam finding: the web-search
  provider's ``text`` is free-form model output that nothing checks against
  the page it is attributed to).
* :func:`~app.pipeline.providers.base.url_key`, :func:`same_page` and
  :func:`registrable_domain` are the single canonical notion of "the same
  page" / "the same site" for the whole pipeline (redteam finding: the
  self-citation guard in ``aggregate._usable`` compared raw URL strings, so a
  tracking parameter turned "the article citing itself" into "the article
  corroborated by an independent source"; a separate finding is that two
  subdomains of one publisher counted as two independent sources). Both
  findings are demonstrated below against the *old* primitives that are still
  in the codebase (``aggregate._url_key``, ``domain_of``) to show concretely
  what the new functions fix, without editing the files that use them — that
  is other agents' work, tracked in the redteam fix plan.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest
from pydantic import ValidationError

from app.pipeline import aggregate as aggregate_module
from app.pipeline.providers.base import domain_of, registrable_domain, same_page, url_key
from app.pipeline.types import Passage
from app.schema_models import CheckRequest


def _passage(url: str = "https://example.com/story", **overrides: object) -> Passage:
    fields: dict[str, object] = {
        "text": "Some retrieved text.",
        "url": url,
        "outlet": "Example",
        "date": "2026-03-12",
        "wire": False,
        "origin": "web",
        "rating": None,
    }
    fields.update(overrides)
    return Passage(**fields)  # type: ignore[arg-type]


# ---------------------------------------------------------------- Passage.provenance_verified


def test_provenance_verified_defaults_to_false() -> None:
    """A caller that builds a Passage without naming the field gets an
    *unverified* passage — the safe default for a field that means "checked
    against the page it's attributed to".

    Before this field existed, no keyword named ``provenance_verified`` was
    accepted at all — old code calling ``Passage(**fields)`` with this key
    raised ``TypeError: unexpected keyword argument``. This test is therefore
    a regression test for the field's *existence and default* together: it
    fails on the pre-fix code both because the keyword is rejected and,
    hypothetically had it been added without a default, because the attribute
    would not be ``False`` for every existing call site that never mentions it.
    """
    passage = _passage()
    assert passage.provenance_verified is False


def test_provenance_verified_can_be_set_true_by_a_provider_that_checked() -> None:
    """A provider that *has* verified its text against the page may say so."""
    passage = _passage(provenance_verified=True)
    assert passage.provenance_verified is True


def test_passage_is_still_frozen_with_the_new_field() -> None:
    """The new field must not weaken the concurrency guarantee the module
    docstring promises: every Passage is immutable once built."""
    passage = _passage()
    with pytest.raises(FrozenInstanceError):
        passage.provenance_verified = True  # type: ignore[misc]


# ---------------------------------------------------------------- url_key / same_page


@pytest.mark.parametrize(
    "article_url,retrieved_url",
    [
        pytest.param(
            "https://cna.example/news/story-123",
            "https://cna.example/news/story-123?utm_source=google",
            id="tracking-parameter",
        ),
        pytest.param(
            "https://cna.example/news/story-123",
            "https://www.cna.example/news/story-123",
            id="www-prefix",
        ),
        pytest.param(
            "https://cna.example/news/story-123",
            "http://cna.example/news/story-123",
            id="scheme-change",
        ),
        pytest.param(
            "https://cna.example/news/story-123",
            "https://cna.example/news/story-123/",
            id="trailing-slash",
        ),
        pytest.param(
            "https://cna.example/news/story-123",
            "https://cna.example/news/story-123#section-2",
            id="fragment",
        ),
    ],
)
def test_same_page_recognises_the_article_under_a_tracking_or_syndication_variant(
    article_url: str, retrieved_url: str
) -> None:
    """The exact BLOCKER scenario: a search engine hands back the very page
    being checked, dressed in a tracking parameter / www / scheme / slash /
    fragment variant. ``same_page`` must still say "yes, that's the article".
    """
    assert same_page(article_url, retrieved_url) is True


def test_same_page_still_rejects_a_genuinely_different_page() -> None:
    same_article, other_article = (
        "https://cna.example/news/story-123",
        "https://cna.example/news/story-456",
    )
    assert same_page(same_article, other_article) is False

    same_host, other_host = (
        "https://cna.example/news/story-123",
        "https://reuters.example/news/story-123",
    )
    assert same_page(same_host, other_host) is False


def test_same_page_never_matches_two_urls_with_no_host() -> None:
    """Two unknowns are not evidence they are the same unknown."""
    assert same_page("not a url", "also not a url") is False
    assert url_key("not a url") == ""


def test_the_old_raw_string_comparison_misses_every_one_of_these_variants() -> None:
    """Reproduces the BLOCKER exactly: ``aggregate._url_key`` (still the
    function ``aggregate._usable`` uses today) is a raw-string fold — strip and
    casefold, nothing else — so it treats the tracking-parameter copy of the
    article as a *different* URL from the article itself. That is the bug: the
    self-citation guard it backs stops guarding the moment retrieval hands the
    article back with a `?utm_source=` on it, exactly what a search engine
    does. This test pins that the *old* primitive still has the gap that
    :func:`same_page` closes — if this assertion ever starts failing, the old
    helper has been fixed or removed and this whole test (not `same_page`)
    should be revisited.
    """
    article = "https://cna.example/news/story-123"
    tracked = "https://cna.example/news/story-123?utm_source=google"

    # The bug, demonstrated: old key differs, so the old guard does NOT treat
    # these as the same page.
    assert aggregate_module._url_key(article) != aggregate_module._url_key(tracked)

    # The fix, demonstrated: the new canonical key treats them as one page.
    assert url_key(article) == url_key(tracked)
    assert same_page(article, tracked) is True


# ---------------------------------------------------------------- registrable_domain


def test_registrable_domain_collapses_subdomains_of_an_ordinary_site() -> None:
    """``news.example.com`` and ``www.example.com`` are one publisher, one site."""
    assert (
        registrable_domain("https://news.example.com/a")
        == registrable_domain("https://www.example.com/b")
        == "example.com"
    )


def test_registrable_domain_handles_singapore_multi_label_suffixes() -> None:
    """``gov.sg`` is a two-label public suffix: the registrable domain sits one
    label further in (``moh.gov.sg``), and two subdomains of *that* agency
    still collapse to it — but two different agencies under ``gov.sg`` stay
    distinct, because they are genuinely different sites."""
    assert registrable_domain("https://www.moh.gov.sg/press") == "moh.gov.sg"
    assert registrable_domain("https://api.moh.gov.sg/data") == "moh.gov.sg"
    assert registrable_domain("https://www.mom.gov.sg/press") == "mom.gov.sg"
    assert registrable_domain("https://www.moh.gov.sg") != registrable_domain("https://www.mom.gov.sg")


def test_registrable_domain_handles_two_label_tlds_generally() -> None:
    assert registrable_domain("https://news.example.co.uk/a") == "example.co.uk"
    assert registrable_domain("https://shop.example.com.sg/a") == "example.com.sg"


def test_registrable_domain_of_a_hostless_url_is_empty() -> None:
    assert registrable_domain("not a url") == ""


def test_domain_of_does_not_collapse_subdomains_which_is_the_second_bug() -> None:
    """Pins the second finding directly: ``domain_of`` (what
    ``aggregate.source_group`` and ``is_primary`` use today) treats
    ``news.example.com`` and ``shop.example.com`` as two different strings —
    which is exactly how two subdomains of one publisher end up counted as two
    independent sources. ``registrable_domain`` collapses them; ``domain_of``
    by itself does not and was never meant to (it is a display/dedup helper
    for wire-copy grouping, not a site-identity primitive). If this assertion
    ever starts failing, ``domain_of`` has grown subdomain-collapsing behaviour
    and callers relying on ``registrable_domain`` for that should be checked.
    """
    assert domain_of("https://news.example.com/a") != domain_of("https://shop.example.com/b")
    assert registrable_domain("https://news.example.com/a") == registrable_domain(
        "https://shop.example.com/b"
    )


# ---------------------------------------------------------------- CheckRequest size limits


def _check_request(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "url": "https://example.com/article",
        "title": "A headline",
        "text": "Some article text.",
        "install_id": "11111111-1111-4111-8111-111111111111",
    }
    body.update(overrides)
    return body


def test_check_request_rejects_an_oversized_title() -> None:
    """Before this field had a ``maxLength``, an unauthenticated caller could
    send an arbitrarily large ``title`` and it validated cleanly — this is the
    DoS/cost finding. 501 characters is one past the 500-character bound."""
    with pytest.raises(ValidationError):
        CheckRequest(**_check_request(title="t" * 501))


def test_check_request_accepts_a_title_at_the_boundary() -> None:
    CheckRequest(**_check_request(title="t" * 500))


def test_check_request_rejects_oversized_article_text() -> None:
    """60,000 characters is comfortably above ``settings.max_article_chars``
    (12,000) — generous for even a long feature or liveblog — but not
    unbounded. Before this bound existed, the POST body and the Redis cache
    entry built from it were both attacker-sized."""
    with pytest.raises(ValidationError):
        CheckRequest(**_check_request(text="x" * 60_001))


def test_check_request_accepts_article_text_well_above_the_extraction_budget() -> None:
    """A genuinely long feature article (much longer than any real Singapore
    news story) must still be accepted by the schema — truncation to
    ``max_article_chars`` is extraction's job, not the wire contract's."""
    CheckRequest(**_check_request(text="x" * 60_000))


def test_check_request_rejects_an_oversized_install_id() -> None:
    """``install_id`` is a UUID (36 characters) and is folded straight into a
    Redis key (``app.limits.CAP_KEY``); before this bound it was an
    attacker-sized Redis key."""
    with pytest.raises(ValidationError):
        CheckRequest(**_check_request(install_id="1" * 65))


def test_check_request_accepts_a_real_uuid_install_id() -> None:
    CheckRequest(**_check_request(install_id="11111111-1111-4111-8111-111111111111"))
