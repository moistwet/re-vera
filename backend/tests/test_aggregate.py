"""Stage 5 — aggregation, which is where the product's promises are kept or broken.

Nothing here needs a key, a network or a model, because stage 5 uses none of the
three: it is ``if`` statements over the scored passages and the judgement. That
makes it the one stage whose behaviour can be pinned down completely, and these
tests try to.

What is covered, and why each one is here rather than left to review:

* **The four verdict paths**, each from the rule the brief states for it — a
  high-confidence refutation, two independent supporters, one primary source, a
  true-but-misleading signal, and nothing at all.
* **Wire copy is one source.** Five mastheads carrying one agency story must not
  add up to "two or more independent sources". This is the failure that makes a
  wire error look corroborated, and it is a single ``frozenset`` away at all
  times.
* **The judge may only weaken.** Every combination of rule verdict and judge
  verdict resolves toward abstention, and a judge that claims more than the
  evidence supports changes nothing.
* **Fabricated citations downgrade the claim.** The judge may only use retrieved
  passages; a span that is not in one, or no span at all, ends at
  ``unverifiable`` however confident the judgement was.
* **Every produced dict passes :func:`app.invariants.validate_claim`** — sources
  empty iff unverifiable, confidence null iff unverifiable — because that is what
  the pipeline asserts immediately before publishing it.
* **The trail is derived, never invented.** Every note is built from an outlet,
  a date, a wire flag or a domain that is really in the input.
* **No article text, passage body or URL reaches a log line** (``CLAUDE.md``
  privacy rule 6).

Every claim, passage, outlet, figure and rating in ``tests/fixtures/aggregate/``
is fictional and none of it may be presented as real reporting.
"""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from app.config import Settings
from app.invariants import ALLOWED_VERDICTS, UNVERIFIABLE, validate_claim
from app.pipeline.aggregate import (
    aggregate,
    detect_signals,
    is_credible,
    is_primary,
    reconcile,
    side_strength,
    source_group,
)
from app.pipeline.types import ExtractedClaim, Judgement, Passage, ScoredPassage
from app.schema_models import Claim, Confidence, Stance, Verdict

from .conftest import build_settings

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "aggregate" / "passages.json"
"""``{_note, claim, article_url, sets, judgements}`` — see the directory's README."""

PASSAGE_FIELDS = (
    "text",
    "url",
    "outlet",
    "date",
    "wire",
    "origin",
    "rating",
    "provenance_verified",
)
"""The :class:`~app.pipeline.types.Passage` fields each fixture entry carries; the
remaining two (``stance``, ``rationale_quote``) belong to the scored wrapper.

``provenance_verified`` is set per entry to match the contract every provider
is meant to honour: ``official``/``factcheck``/``cited_source`` passages are
built from a structured API field or bytes fetched directly, so they are
verified by construction; ``web``-origin passages are a model's free-form
summary, so they are not (``app.pipeline.types.Passage``'s own docstring)."""


# ---------------------------------------------------------------- fixtures


def _document() -> dict[str, Any]:
    with FIXTURE_PATH.open(encoding="utf-8") as handle:
        payload: dict[str, Any] = json.load(handle)
    return payload


DOCUMENT = _document()
SET_NAMES = sorted(DOCUMENT["sets"])
ARTICLE_URL: str = DOCUMENT["article_url"]


def scored(name: str) -> list[ScoredPassage]:
    """The named set of scored passages, as the stance stage would hand them over."""
    return [
        ScoredPassage(
            passage=Passage(**{field: entry[field] for field in PASSAGE_FIELDS}),
            stance=Stance(entry["stance"]),
            rationale_quote=entry["rationale_quote"],
        )
        for entry in DOCUMENT["sets"][name]
    ]


def judgement(name: str, **overrides: Any) -> Judgement:
    """The judgement recorded against the named set, with optional overrides."""
    fields = dict(DOCUMENT["judgements"][name])
    fields.update(overrides)
    return Judgement(
        verdict=fields["verdict"],
        confidence=fields["confidence"],
        evidence=fields["evidence"],
        cited_spans=list(fields["cited_spans"]),
    )


def claim() -> ExtractedClaim:
    """The one fictional claim every set is scored against."""
    return ExtractedClaim(**DOCUMENT["claim"])


@pytest.fixture
def settings() -> Settings:
    """Production caps, with no ``.env`` and no key in sight."""
    return build_settings(max_passages_per_claim=6)


def run(name: str, settings: Settings, **overrides: Any) -> dict[str, Any]:
    """Aggregate the named set and return the claim dict."""
    return aggregate(
        claim(),
        scored(name),
        judgement(name, **overrides),
        article_url=ARTICLE_URL,
        settings=settings,
    )


def outlets(payload: dict[str, Any]) -> list[str]:
    return [source["outlet"] for source in payload["sources"]]


def notes(payload: dict[str, Any]) -> dict[str, str]:
    return {node["label"]: node["note"] for node in payload["trail"]}


# ---------------------------------------------------------------- the four paths


def test_high_confidence_refutation_from_a_credible_source_contradicts(
    settings: Settings,
) -> None:
    """Rule 1: an official release plus two reports refuting the claim.

    The verdict is ``contradicted``, all three passages become source chips with
    the official one first, and the judge's own sentence survives because it is
    about this verdict and names a source we kept.
    """
    payload = run("contradicted", settings)

    assert payload["verdict"] == Verdict.contradicted.value
    assert payload["confidence"] == Confidence.high.value
    assert outlets(payload) == ["Hawker Centres Board", "Island Wire", "Harbour Post"]
    assert payload["evidence"] == DOCUMENT["judgements"]["contradicted"]["evidence"]
    assert all(source["stance"] == Stance.refutes.value for source in payload["sources"])


def test_two_independent_supporters_reach_supported(settings: Settings) -> None:
    """Rule 2, first half: two ordinary newsrooms on two domains agreeing."""
    payload = run("supported_independent", settings)

    assert payload["verdict"] == Verdict.supported.value
    assert outlets(payload) == ["Island Wire", "Harbour Daily"]
    # Two independent sources and no primary one: real support, held medium.
    assert payload["confidence"] == Confidence.medium.value


def test_one_primary_source_alone_reaches_supported(settings: Settings) -> None:
    """Rule 2, second half: one government press release, with nothing else, is
    enough -- a *genuine* primary document, not a dataset catalogue entry
    (see :func:`test_a_dataset_catalogue_listing_alone_is_not_enough` below for
    the M8 fix this deliberately distinguishes from)."""
    payload = run("supported_primary_press_release", settings)

    assert payload["verdict"] == Verdict.supported.value
    assert len(payload["sources"]) == 1
    assert payload["sources"][0]["outlet"] == "National Environment Agency"
    # A single source, primary or not, is never a high-confidence answer, so the
    # judge's stated "high" is capped rather than believed.
    assert payload["confidence"] == Confidence.medium.value


def test_a_dataset_catalogue_listing_alone_is_not_enough(settings: Settings) -> None:
    """M8: data.gov.sg's own provider never reads a figure out of a dataset --
    every passage it returns is a title, an agency name and a description, and
    that is not "the original document that states the fact". A lone catalogue
    entry must not reach ``supported`` on the strength of being on a
    government-flavoured domain."""
    payload = run("supported_primary", settings)

    assert payload["verdict"] == UNVERIFIABLE
    assert payload["sources"] == []
    assert payload["confidence"] is None


def test_one_ordinary_page_is_not_enough_to_support(settings: Settings) -> None:
    """The other half of rule 2: one non-primary page supports nothing on its own."""
    only_one = scored("supported_independent")[:1]
    payload = aggregate(
        claim(),
        only_one,
        judgement("supported_independent"),
        article_url=ARTICLE_URL,
        settings=settings,
    )

    assert payload["verdict"] == UNVERIFIABLE
    assert payload["sources"] == []


def test_tiny_sample_makes_a_supported_claim_missing_context(settings: Settings) -> None:
    """Rule 3: the figure is reported by two independent outlets, but the survey
    behind it had 42 respondents. ``missing_context`` requires the same
    strength ``supported`` would (M7 -- see
    :func:`test_a_weak_uncorroborated_page_with_a_signal_is_unverifiable`
    below for what happens with only one), so this fixture corroborates the
    figure across two domains before the signal is checked.

    The survey document is cited alongside the two reports — a reader cannot
    check "small sample" without it — and keeps its real ``neutral`` stance.
    """
    payload = run("missing_context_small_sample", settings)

    assert payload["verdict"] == Verdict.missing_context.value
    assert outlets(payload) == ["Hawker Sentiment Survey", "Island Wire", "Harbour Daily"]
    assert payload["evidence"] == (
        "Island Wire and Harbour Daily back this claim, but it rests on a very small sample."
    )
    assert [source["stance"] for source in payload["sources"]] == [
        Stance.neutral.value,
        Stance.supports.value,
        Stance.supports.value,
    ]


def test_a_weak_uncorroborated_page_with_a_signal_is_unverifiable(settings: Settings) -> None:
    """M7: ``side_strength``'s own docstring says weak (uncorroborated) evidence
    "is never enough for supported or contradicted on its own"; a signal must
    not lower that bar for ``missing_context`` either. One non-primary
    supporting page, however clearly flagged, stays ``unverifiable``."""
    passages = scored("missing_context_small_sample")
    one_supporter_and_the_signal = [passages[0], passages[2]]  # Island Wire + the survey
    payload = aggregate(
        claim(),
        one_supporter_and_the_signal,
        judgement("missing_context_small_sample"),
        article_url=ARTICLE_URL,
        settings=settings,
    )

    assert payload["verdict"] == UNVERIFIABLE
    assert payload["sources"] == []


def test_outdated_support_is_missing_context(settings: Settings) -> None:
    """Rule 3 again: the supporting report is two years older than the correction.

    Measured between the passages' own dates, never against the wall clock, so
    this test means the same thing next year.
    """
    payload = run("missing_context_outdated", settings)

    assert payload["verdict"] == Verdict.missing_context.value
    assert "more recent material has since been published" in payload["evidence"]


def test_a_fact_checkers_partly_true_rating_is_missing_context(settings: Settings) -> None:
    """Rule 3 again: the publisher's own rating already says "true, but…".

    Here the judge agrees with the rules, so its confidence is a live ceiling —
    and ``missing_context`` is still capped at medium, because a claim about what
    the evidence leaves out is a harder thing to be sure of than what it says.
    """
    payload = run("missing_context_rating", settings, verdict="missing_context")

    assert payload["verdict"] == Verdict.missing_context.value
    assert payload["confidence"] == Confidence.medium.value
    # The rating is quoted as the fact-checker's words, never as a verdict.
    assert '"Partly true"' in payload["evidence"] or "Fact Check Desk" in payload["evidence"]
    assert "Partly true" not in payload["verdict"]


def test_nothing_retrieved_is_unverifiable_with_an_explanation(settings: Settings) -> None:
    """Rule 4: no passages at all — no sources, no confidence, still a trail.

    The explanation deliberately does not claim fact-check databases and the
    web were specifically searched (M20): this stage cannot know whether
    every provider actually ran or genuinely came back empty, and asserting a
    search that may not have happened is exactly what rule 2 forbids.
    """
    payload = aggregate(
        claim(),
        [],
        judgement("contradicted"),
        article_url=ARTICLE_URL,
        settings=settings,
    )

    assert payload["verdict"] == UNVERIFIABLE
    assert payload["sources"] == []
    assert payload["confidence"] is None
    assert payload["evidence"] == "No evidence was found that addresses this claim."
    assert notes(payload)["Independent reports"] == "none found"


def test_credible_sources_that_disagree_leave_the_claim_unverifiable(
    settings: Settings,
) -> None:
    """Both sides clear their bar: the board refutes, two newsrooms support.

    Neither side is quietly discounted. The disagreement is the finding, and the
    explanation says so.
    """
    payload = run("conflict", settings)

    assert payload["verdict"] == UNVERIFIABLE
    assert payload["sources"] == []
    assert "disagree about this claim" in payload["evidence"]
    assert "Hawker Centres Board" in payload["evidence"]


# ---------------------------------------------------------------- independence


def test_wire_copy_on_five_domains_counts_as_one_source(settings: Settings) -> None:
    """The rule that stops a wire error looking corroborated.

    Five mastheads, five domains, one agency story: one independence group, so
    the "two or more independent sources" bar is not met and the claim stays
    ``unverifiable`` even though the judge called it supported with high
    confidence.
    """
    passages = scored("wire_on_five_domains")
    assert len({item.passage.url for item in passages}) == 5
    assert len({source_group(item.passage) for item in passages}) == 1
    assert side_strength(passages, refutation=False) == 1

    payload = run("wire_on_five_domains", settings)

    assert payload["verdict"] == UNVERIFIABLE
    assert payload["sources"] == []
    assert payload["confidence"] is None


def test_two_pages_on_one_domain_are_one_source(settings: Settings) -> None:
    """Independence is per domain, not per URL: a site's second page adds nothing."""
    first, second = scored("supported_independent")
    same_site = [
        first,
        replace(second, passage=replace(second.passage, url="https://island-wire.test/again")),
    ]
    assert len({source_group(item.passage) for item in same_site}) == 1

    payload = aggregate(
        claim(),
        same_site,
        judgement("supported_independent"),
        article_url=ARTICLE_URL,
        settings=settings,
    )
    assert payload["verdict"] == UNVERIFIABLE


def test_aggregator_copy_and_the_article_itself_are_not_sources(settings: Settings) -> None:
    """A republisher is evidence of republishing, and an article cannot cite itself.

    Three passages go in — an aggregator's reprint, the very page being checked,
    and one ordinary report — and only the report survives, which is not enough
    to support anything.
    """
    payload = run("aggregator_and_self", settings)

    assert payload["verdict"] == UNVERIFIABLE
    assert "Island Wire" in payload["evidence"]
    assert "Yahoo" not in payload["evidence"]
    assert "Example News" not in payload["evidence"]


def test_two_subdomains_of_one_publisher_are_one_independent_source() -> None:
    """M3: independence is keyed on the *registrable* domain, so
    ``news.example.com`` and ``shop.example.com`` collapse to one source —
    the raw host used to treat them as two (a redteam finding)."""
    first, second = scored("supported_independent")
    subdomains = [
        replace(first, passage=replace(first.passage, url="https://news.hawker-media.test/a")),
        replace(second, passage=replace(second.passage, url="https://shop.hawker-media.test/b")),
    ]
    assert len({source_group(item.passage) for item in subdomains}) == 1
    assert side_strength(subdomains, refutation=False) == 1


def test_a_sibling_page_on_the_articles_own_site_is_not_evidence(settings: Settings) -> None:
    """B3 (BLOCKER): an article links to another page on its own site. Even
    though the cited-source provider fetched it and it plainly supports the
    claim, a publisher cannot corroborate itself: it is dropped before it can
    be counted as primary or as an independent source at all, so the claim
    stays ``unverifiable`` -- even though the judge (in this fixture) called
    it ``supported``, because the rules may only be weakened by the judge,
    never overruled by one that tries to strengthen an abstention."""
    payload = run("self_site_citation", settings)

    assert payload["verdict"] == UNVERIFIABLE
    assert payload["sources"] == []
    assert payload["confidence"] is None
    assert "Example News" not in payload["evidence"]


def test_same_page_under_a_tracking_parameter_is_still_the_article(settings: Settings) -> None:
    """B4: the self-citation guard now uses the canonical
    ``providers.base.same_page``, which survives a tracking parameter, a
    ``www.`` prefix, a scheme change or a trailing slash -- none of which the
    old raw-string comparison did (see ``tests/test_types.py`` for the pinned
    demonstration of the old bug still living in :func:`app.pipeline.aggregate._url_key`)."""
    passages = scored("aggregator_and_self")
    self_citation = passages[1]  # the article, verbatim, in the fixture
    tracked = replace(
        self_citation,
        passage=replace(
            self_citation.passage,
            url=f"{self_citation.passage.url}?utm_source=share",
        ),
    )
    payload = aggregate(
        claim(),
        [tracked, passages[2]],  # the tracked self-citation, plus one real report
        judgement("aggregator_and_self"),
        article_url=ARTICLE_URL,
        settings=settings,
    )

    assert payload["verdict"] == UNVERIFIABLE
    assert "Example News" not in payload["evidence"]


def test_an_unverified_primary_passage_cannot_alone_decide(settings: Settings) -> None:
    """A single passage deciding a verdict alone (a primary source, or a
    ClaimReview refuting the claim) must have text confirmed to really appear
    on the page it names. An otherwise-primary government page whose text is
    only a model's unverified summary informs but does not, alone, decide."""
    primary = scored("supported_primary_press_release")[0]
    unverified = replace(primary, passage=replace(primary.passage, provenance_verified=False))

    assert side_strength([unverified], refutation=False) == 1

    payload = aggregate(
        claim(),
        [unverified],
        judgement("supported_primary_press_release"),
        article_url=ARTICLE_URL,
        settings=settings,
    )
    assert payload["verdict"] == UNVERIFIABLE
    assert payload["sources"] == []


def test_two_unverified_independent_sources_can_still_decide(settings: Settings) -> None:
    """The corroboration path is not gated on ``provenance_verified`` -- two
    unverified but independent web-search passages agreeing is itself the
    safeguard, and today's web-search provider never sets this field at all,
    so gating this path too would make ``supported`` unreachable by ordinary
    reporting."""
    passages = scored("supported_independent")
    assert all(not item.passage.provenance_verified for item in passages)
    assert side_strength(passages, refutation=False) == 2


# ---------------------------------------------------------------- the judge


@pytest.mark.parametrize(
    ("rule_verdict", "judge_verdict", "expected"),
    [
        # Agreement.
        ("supported", "supported", "supported"),
        ("contradicted", "contradicted", "contradicted"),
        # The judge abstains further than the rules — believed.
        ("supported", "missing_context", "missing_context"),
        ("contradicted", "unverifiable", "unverifiable"),
        ("missing_context", "unverifiable", "unverifiable"),
        # The judge claims more than the rules — ignored.
        ("unverifiable", "supported", "unverifiable"),
        ("unverifiable", "contradicted", "unverifiable"),
        ("missing_context", "supported", "missing_context"),
        # Equal strength, opposite directions — neither wins.
        ("supported", "contradicted", "unverifiable"),
        ("contradicted", "supported", "unverifiable"),
        # Not a verdict at all.
        ("supported", "TRUE", "unverifiable"),
        ("supported", "flagged", "unverifiable"),
        ("supported", "", "unverifiable"),
    ],
)
def test_reconcile_always_resolves_toward_abstention(
    rule_verdict: str, judge_verdict: str, expected: str
) -> None:
    """The judge may weaken the rules' verdict and may never strengthen it."""
    assert reconcile(rule_verdict, judge_verdict) == expected


def test_a_judge_that_abstains_weakens_the_rules(settings: Settings) -> None:
    """Two independent supporters, but the judge saw something in the prose."""
    payload = run("supported_independent", settings, verdict="missing_context")

    assert payload["verdict"] == Verdict.missing_context.value
    assert len(payload["sources"]) == 2


def test_a_judge_verdict_outside_the_vocabulary_is_not_a_verdict(settings: Settings) -> None:
    """There are four verdicts. "TRUE" is not one of them, and never becomes one."""
    payload = run("contradicted", settings, verdict="TRUE")

    assert payload["verdict"] == UNVERIFIABLE
    assert payload["sources"] == []


def test_a_tied_refutation_does_not_soften_to_missing_context(settings: Settings) -> None:
    """B2 (BLOCKER): a ClaimReview rated "False" (a strong refutation) ties
    against two independent supporting pages, one of which is self-selected
    (a missing-context signal). The old code checked ``missing_context``
    before the tie, so it fell through to the friendlier verdict and silently
    dropped the refuting source; the fix checks the tie first, so this stays
    ``unverifiable`` and the refutation is not thrown away."""
    payload = run("conflict_with_signal", settings)

    assert payload["verdict"] == UNVERIFIABLE
    assert payload["sources"] == []
    assert payload["confidence"] is None
    assert "Fact Check Desk" in payload["evidence"]


def test_same_outlet_on_both_sides_uses_singular_conflict_grammar(settings: Settings) -> None:
    """M16: a genuine tie can still name only one outlet -- the same board
    contradicting itself across two releases -- and "X disagree" is
    ungrammatical when X is one name."""
    payload = run("single_outlet_conflict", settings)

    assert payload["verdict"] == UNVERIFIABLE
    assert notes(payload)["This article"] == "published on example-news.test"
    assert payload["evidence"] == (
        "Hawker Centres Board published evidence on both sides of this claim, "
        "so it is left unresolved."
    )
    assert "disagree" not in payload["evidence"]


def test_an_invalid_judge_verdict_is_not_reported_as_a_conflict(settings: Settings) -> None:
    """M16: the judge returning something outside the four verdicts is the
    judge saying nothing usable, not the sources disagreeing -- the old reason
    logic said "disagree" whenever the rules alone had reached a decided
    verdict, regardless of why the judge's answer was discarded."""
    payload = run("supported_independent", settings, verdict="TRUE")

    assert payload["verdict"] == UNVERIFIABLE
    assert "disagree" not in payload["evidence"]
    assert "nothing found there settles this claim" in payload["evidence"]


def test_a_fabricated_citation_downgrades_the_claim(settings: Settings) -> None:
    """The judge quoted a sentence that is in none of the passages.

    The verdict the rules reached is discarded: a citation that does not exist is
    the judge using its own knowledge, which rule 2 forbids outright.
    """
    payload = run(
        "contradicted",
        settings,
        cited_spans=["The board later admitted the 40 per cent figure was correct."],
    )

    assert payload["verdict"] == UNVERIFIABLE
    assert payload["sources"] == []
    assert payload["confidence"] is None
    assert "could not be tied back" in payload["evidence"]


def test_a_judgement_citing_nothing_is_not_believed(settings: Settings) -> None:
    """No quoted span at all is the emptiest possible citation, and fails too."""
    assert run("contradicted", settings, cited_spans=[])["verdict"] == UNVERIFIABLE


def test_one_bad_span_among_good_ones_is_still_a_downgrade(settings: Settings) -> None:
    """Every cited span must check out, not merely one of them."""
    payload = run(
        "contradicted",
        settings,
        cited_spans=[
            "median stall rent adjustment is 4 per cent",
            "a sentence that appears in none of the passages",
        ],
    )
    assert payload["verdict"] == UNVERIFIABLE


def test_typography_does_not_count_as_fabrication(settings: Settings) -> None:
    """A curly apostrophe retyped straight is not an invented citation."""
    payload = run(
        "missing_context_small_sample",
        settings,
        cited_spans=["self-selected through a stallholders' mailing list"],
    )
    assert payload["verdict"] == Verdict.missing_context.value


# ---------------------------------------------------------------- evidence sentence


def test_the_judges_sentence_is_dropped_when_the_verdict_changed(settings: Settings) -> None:
    """"Island Wire reports the eight-in-ten figure" under a Missing context badge
    would be the product contradicting itself, so it is composed instead."""
    payload = run("missing_context_small_sample", settings)

    assert payload["evidence"] != DOCUMENT["judgements"]["missing_context_small_sample"]["evidence"]
    assert payload["evidence"].endswith("small sample.")


def test_a_sentence_naming_no_source_is_replaced(settings: Settings) -> None:
    """Rule 2 asks the evidence line to name the sources; this one names none."""
    payload = run(
        "contradicted",
        settings,
        evidence="Two newsrooms and an official body say otherwise.",
    )

    assert payload["evidence"] == (
        "Hawker Centres Board, Island Wire and Harbour Post contradict this claim."
    )


def test_an_essay_is_not_a_sentence(settings: Settings) -> None:
    """A judge that returned a page of text (or a page's text) is not quoted."""
    payload = run("contradicted", settings, evidence="Hawker Centres Board. " * 40)

    assert len(payload["evidence"]) < 200
    assert "contradict this claim" in payload["evidence"]


def test_an_empty_sentence_is_replaced(settings: Settings) -> None:
    payload = run("contradicted", settings, evidence="   ")
    assert payload["evidence"].endswith("contradict this claim.")


def test_unverifiable_evidence_names_what_was_checked(settings: Settings) -> None:
    """The explanation an unverifiable verdict must ship, built from real outlets."""
    payload = run("wire_on_five_domains", settings)

    assert payload["evidence"].startswith("Checked ")
    assert "nothing found there settles this claim" in payload["evidence"]
    assert "Harbour Post" in payload["evidence"]


def test_nothing_retrieved_does_not_claim_a_search_that_may_not_have_happened(
    settings: Settings,
) -> None:
    """M20: this stage cannot know whether every provider actually ran (one can
    be unconfigured, or every call can have failed) or genuinely came back
    empty, so the "nothing retrieved" sentence must not assert that fact-check
    databases and the web were specifically searched -- only that no evidence
    was found."""
    payload = aggregate(
        claim(),
        [],
        judgement("contradicted"),
        article_url=ARTICLE_URL,
        settings=settings,
    )

    assert payload["evidence"] == "No evidence was found that addresses this claim."
    assert "search" not in payload["evidence"].lower()


def test_stance_mismatched_source_is_never_described_as_backing_the_claim(
    settings: Settings,
) -> None:
    """M5: the per-claim cap can strand only a *refuting* source in ``sources``
    (``missing_context``'s ``relied`` always includes refuting evidence, and a
    weak fact-check outranks ordinary web reports in the source-chip
    ordering). The old fallback named it as "backing" the claim whenever no
    kept source matched the wanted stance; the fix never mislabels a source's
    stance, composing a sentence that names no direction at all instead."""
    capped = build_settings(max_passages_per_claim=1)
    payload = run("stance_mismatch_cap", capped)

    assert payload["verdict"] == Verdict.missing_context.value
    assert len(payload["sources"]) == 1
    assert payload["sources"][0]["stance"] == Stance.refutes.value
    assert payload["evidence"] == (
        "The evidence found does not settle this claim, and it rests on a very small sample."
    )
    assert "Fact Check Desk" not in payload["evidence"]
    assert "backs this claim" not in payload["evidence"]


def test_a_foreign_verdict_word_never_reaches_the_reader_verbatim(settings: Settings) -> None:
    """M6/M18/M19 (BLOCKER-adjacent): the judge's own sentence can legitimately
    quote a passage that itself contains a ClaimReview's rating -- "FALSE",
    "pants on fire" -- without fabricating anything. That sentence must still
    never reach a reader looking like it is Re-Vera's own judgement; the four
    verdicts are supported/contradicted/missing_context/unverifiable and none
    of them is spelled like a tabloid rating."""
    payload = run(
        "contradicted",
        settings,
        evidence='Hawker Centres Board says this is FALSE, not the 40 per cent figure.',
    )

    assert "FALSE" not in payload["evidence"]
    assert payload["evidence"] == (
        "Hawker Centres Board, Island Wire and Harbour Post contradict this claim."
    )


def test_outlet_text_is_sanitised_before_it_reaches_a_reader(settings: Settings) -> None:
    """M6/M18/M19: an outlet name is third-party text, up to 120 characters, and
    is never interpolated raw -- control characters and newlines are stripped
    and the length is re-bounded here regardless of what a provider already
    enforced."""
    passages = scored("contradicted")
    hostile_outlet = "Hawker\x07 Centres\nBoard" + ("!" * 200)
    hostile = replace(
        passages[0], passage=replace(passages[0].passage, outlet=hostile_outlet)
    )
    payload = aggregate(
        claim(),
        [hostile, *passages[1:]],
        judgement("contradicted"),
        article_url=ARTICLE_URL,
        settings=settings,
    )

    shown = payload["sources"][0]["outlet"]
    assert "\x07" not in shown
    assert "\n" not in shown
    assert len(shown) <= 120


def test_a_short_outlet_name_does_not_match_by_accident(settings: Settings) -> None:
    """M17: the outlet-mentioned gate is a word-boundary match, not the old raw
    substring test -- a short outlet name like "AP" must not count as
    "mentioned" merely because it appears inside an unrelated word ("tap")."""
    ap_claim = ExtractedClaim(
        id="c1",
        quote="rise by 40% from 1 January",
        start=60,
        end=86,
        kind="numeric",
        checkworthiness=0.9,
    )
    ap_passage = Passage(
        text=(
            "The board's tap water notice confirms the true adjustment is "
            "4 per cent, not 40."
        ),
        url="https://www.moh.gov.sg/press/ap-report",
        outlet="AP",
        date="2026-03-12",
        wire=False,
        origin="official",
        rating=None,
        provenance_verified=True,
    )
    scored_ap = [
        ScoredPassage(
            passage=ap_passage,
            stance=Stance.refutes,
            rationale_quote="true adjustment is 4 per cent",
        )
    ]
    judgement_ap = Judgement(
        verdict="contradicted",
        confidence="high",
        evidence="The tap water notice confirms this is wrong.",
        cited_spans=["true adjustment is 4 per cent"],
    )

    payload = aggregate(
        ap_claim,
        scored_ap,
        judgement_ap,
        article_url="https://example-news.test/unrelated-article",
        settings=settings,
    )

    assert payload["verdict"] == Verdict.contradicted.value
    # The judge sentence never actually names "AP" as a word (only "tap"), so
    # the raw substring match the old gate used would have accepted it; the
    # word-boundary gate must reject it and compose a sentence instead.
    assert payload["evidence"] == "AP contradicts this claim."


# ---------------------------------------------------------------- sources


def test_a_source_without_a_stated_date_keeps_an_empty_date(settings: Settings) -> None:
    """No date is an empty string, never a guess: a fabricated date on a source
    chip is a fabricated fact. A blank outlet falls back to the domain."""
    payload = run("undated_and_unnamed", settings)

    assert payload["verdict"] == Verdict.contradicted.value
    assert [source["date"] for source in payload["sources"]] == ["", ""]
    assert outlets(payload) == ["island-wire.test", "Harbour Daily"]
    assert payload["evidence"] == "island-wire.test and Harbour Daily contradict this claim."


def test_sources_honour_the_per_claim_cap() -> None:
    """``max_passages_per_claim`` bounds the chips as well as the prompts."""
    payload = run("contradicted", build_settings(max_passages_per_claim=1))

    assert len(payload["sources"]) == 1
    assert payload["sources"][0]["outlet"] == "Hawker Centres Board"


def test_wire_flag_survives_onto_the_source_chip(settings: Settings) -> None:
    """The reader is told which chip is syndicated copy."""
    payload = run("contradicted", settings)
    wire_flags = {source["outlet"]: source["wire"] for source in payload["sources"]}

    assert wire_flags == {
        "Hawker Centres Board": False,
        "Island Wire": False,
        "Harbour Post": True,
    }


# ---------------------------------------------------------------- the trail


def test_the_trail_is_built_from_real_passage_metadata(settings: Settings) -> None:
    """Three nodes, every note read off the input: the article's domain, the
    independent outlets, and the primary document with the date it stated."""
    payload = run("contradicted", settings)

    assert notes(payload) == {
        "This article": "published on example-news.test",
        "Independent reports": "Island Wire · Harbour Post",
        "Original source": "Hawker Centres Board, 12 Mar",
    }


def test_a_claim_with_no_primary_source_has_no_original_source_node(
    settings: Settings,
) -> None:
    payload = run("supported_independent", settings)

    assert [node["label"] for node in payload["trail"]] == [
        "This article",
        "Independent reports",
    ]
    assert notes(payload)["Independent reports"] == "Island Wire · Harbour Daily"


def test_a_claim_resting_only_on_a_primary_source_says_so(settings: Settings) -> None:
    payload = run("supported_primary_press_release", settings)

    assert [node["label"] for node in payload["trail"]] == ["This article", "Original source"]
    assert notes(payload)["Original source"] == "National Environment Agency, 1 Apr"


def test_an_unverifiable_claim_still_gets_a_trail(settings: Settings) -> None:
    """It ends in what was looked at and the fact that none of it settled anything —
    and never in an "Original source", which would promise evidence this verdict
    does not have."""
    payload = run("wire_on_five_domains", settings)
    labels = [node["label"] for node in payload["trail"]]

    assert labels == ["This article", "Independent reports"]
    assert notes(payload)["Independent reports"].endswith("· nothing conclusive")
    assert "Harbour Post" in notes(payload)["Independent reports"]


def test_an_aggregator_article_is_described_as_a_republisher(settings: Settings) -> None:
    """The reader on Yahoo is told what they are reading, from the domain alone."""
    payload = aggregate(
        claim(),
        scored("contradicted"),
        judgement("contradicted"),
        article_url="https://sg.news.yahoo.com/hawker-stall-rents-rise-vendors",
        settings=settings,
    )

    assert notes(payload)["This article"] == "republished on sg.news.yahoo.com"


def test_a_trail_note_never_names_an_outlet_that_was_not_retrieved(
    settings: Settings,
) -> None:
    """The whole trail, across every set, is checkable against the fixture."""
    known = {entry["outlet"].strip() for group in DOCUMENT["sets"].values() for entry in group}
    known |= {"island-wire.test", "harbour-daily.test"}

    for name in SET_NAMES:
        payload = run(name, build_settings(max_passages_per_claim=6))
        for node in payload["trail"]:
            if node["label"] == "This article":
                continue
            for fragment in node["note"].replace(" · ", "|").split("|"):
                head = fragment.split(",")[0].split(" and ")[0].strip()
                if head in {"none found", "nothing conclusive"}:
                    continue
                assert head in known, f"{name}: invented outlet {head!r}"


# ---------------------------------------------------------------- invariants


@pytest.mark.parametrize("name", SET_NAMES)
def test_every_set_produces_a_valid_claim(name: str, settings: Settings) -> None:
    """The gate the pipeline runs immediately before publishing: sources empty iff
    unverifiable, confidence null iff unverifiable, verdict one of the four."""
    payload = run(name, settings)

    validate_claim(payload)
    Claim.model_validate(payload)
    assert payload["verdict"] in ALLOWED_VERDICTS
    assert payload["trail"]


@pytest.mark.parametrize("name", SET_NAMES)
@pytest.mark.parametrize("judge_verdict", [*sorted(ALLOWED_VERDICTS), "TRUE", "fake"])
def test_no_judgement_can_produce_an_invalid_claim(
    name: str, judge_verdict: str, settings: Settings
) -> None:
    """Every set against every judge verdict, valid or not, still validates.

    The judgement is model output shaped by whatever a web page said; no value of
    it may produce a claim the product's rules forbid.
    """
    payload = run(name, settings, verdict=judge_verdict)

    validate_claim(payload)
    unverifiable = payload["verdict"] == UNVERIFIABLE
    assert (payload["sources"] == []) is unverifiable
    assert (payload["confidence"] is None) is unverifiable


def test_the_claim_carries_the_extracted_quote_and_offsets(settings: Settings) -> None:
    """Aggregation never touches the anchor a client highlights on."""
    payload = run("contradicted", settings)
    source_claim = claim()

    assert payload["id"] == source_claim.id
    assert payload["quote"] == source_claim.quote
    assert (payload["start"], payload["end"]) == (source_claim.start, source_claim.end)


def test_no_verdict_vocabulary_leaks_from_a_rating_or_a_judgement(
    settings: Settings,
) -> None:
    """"Partly true" is a fact-checker's word for its own rating, not a verdict, and
    nothing all-caps or "flagged" reaches a reader-facing field."""
    for name in SET_NAMES:
        payload = run(name, build_settings(max_passages_per_claim=6))
        surface = " ".join(
            [payload["verdict"], payload["evidence"], *(node["note"] for node in payload["trail"])]
        )
        assert "flagged" not in surface.lower()
        assert "TRUE" not in surface
        assert "FALSE" not in surface


# ---------------------------------------------------------------- policy helpers


def test_primary_means_the_document_itself() -> None:
    """A government domain — nothing else, and never merely a subdomain label
    an attacker can spoof."""
    official, report, wire_copy = (item.passage for item in scored("contradicted"))

    assert is_primary(official)
    assert not is_primary(report)
    assert not is_primary(wire_copy)
    assert not is_primary(replace(official, origin="web", url="https://government-news.example/x"))
    assert is_primary(replace(report, url="https://www.moh.gov.sg/press-releases/rentals"))
    # A hostile subdomain crafted to smuggle the word "gov" in must not count:
    # the registrable domain of "gov.sg.evil.com" is "evil.com".
    assert not is_primary(replace(report, url="https://gov.sg.evil.com/press-releases/rentals"))


def test_a_dataset_catalogue_domain_is_never_primary() -> None:
    """M8/M21/M22: data.gov.sg is a government-flavoured domain, but its only
    provider (``app.pipeline.providers.official``) never reads a figure out of
    a dataset -- every passage it returns is a title, an agency name and a
    description. Treating "on a .gov.sg domain" as sufficient would make every
    catalogue listing primary; the domain carve-out stops that specifically,
    without weakening the government-domain check for anything else."""
    catalogue = scored("supported_primary")[0].passage
    assert catalogue.origin == "official"
    assert not is_primary(catalogue)
    assert not is_primary(replace(catalogue, url="https://data.gov.sg/datasets/anything"))
    # A genuine agency press release, not on the catalogue domain, still counts.
    assert is_primary(replace(catalogue, url="https://www.nea.gov.sg/press/anything"))


def test_cited_source_is_primary_only_for_attribution_claims() -> None:
    """B3: a fetched citation only answers "does the document say what the
    article says it says" -- narrow to attribution claims, never a
    general-purpose primary source for a numeric or general claim that
    happens to link somewhere (the second half of B3's fix, and the reason
    for the ``claim_kind`` keyword)."""
    survey = scored("missing_context_small_sample")[-1].passage
    assert survey.origin == "cited_source"

    assert is_primary(survey, claim_kind="attribution")
    assert not is_primary(survey, claim_kind="numeric")
    assert not is_primary(survey)  # default claim_kind is never "attribution"


def test_credibility_is_about_provenance_not_brand() -> None:
    """Anything with a real domain that is not an aggregator counts; a
    ``javascript:`` URL or a reprinting site does not."""
    report = scored("contradicted")[1].passage

    assert is_credible(report)
    assert not is_credible(replace(report, url="javascript:alert(1)"))
    assert not is_credible(replace(report, url="https://sg.news.yahoo.com/x"))
    # An aggregator's copy of an official document is still official.
    assert is_credible(replace(report, url="https://news.google.com/x", origin="official"))


def test_side_strength_is_asymmetric_for_fact_checks() -> None:
    """A published ClaimReview is a high-confidence refutation on its own; the same
    review supporting a claim still needs corroboration (the brief's own asymmetry)."""
    review = scored("missing_context_rating")[:1]

    assert side_strength(review, refutation=True) == 2
    assert side_strength(review, refutation=False) == 1
    assert side_strength([], refutation=True) == 0


def test_detect_signals_reads_the_passages_not_the_judgement() -> None:
    """Each signal comes from something really present in the retrieved material."""
    assert detect_signals(scored("missing_context_small_sample")) == [
        "it rests on a very small sample"
    ]
    assert detect_signals(scored("missing_context_outdated")) == [
        "more recent material has since been published"
    ]
    # M6/M18/M19: the fact-checker's own rating word is never quoted verbatim
    # in Re-Vera's copy -- the signal is Re-Vera's own phrase, attributed to
    # the fact-checker by name, so a rating like "FALSE" or "pants on fire"
    # can never ride through this clause onto a reader's screen.
    assert detect_signals(scored("missing_context_rating")) == [
        "Fact Check Desk rated it only partly true"
    ]
    assert detect_signals(scored("contradicted")) == []


def test_a_large_sample_is_not_a_signal() -> None:
    """The tiny-sample regex must not fire on an ordinary, adequately sized survey."""
    survey = scored("missing_context_small_sample")[-1]  # the survey document
    assert survey.passage.origin == "cited_source"
    big = replace(
        survey,
        passage=replace(
            survey.passage,
            text="The survey drew 1200 respondents, drawn at random from the register.",
        ),
    )
    assert detect_signals([scored("missing_context_small_sample")[0], big]) == []


# ---------------------------------------------------------------- privacy


def test_nothing_logged_carries_article_text_a_passage_or_a_url(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    """``CLAUDE.md`` rule 6: a log line may carry the claim id and counts, never the
    reader's article, the retrieved text, or a URL."""
    caplog.set_level(logging.DEBUG, logger="app.pipeline.aggregate")
    payload = run("contradicted", settings)
    logged = "\n".join(record.getMessage() for record in caplog.records)

    assert "c1" in logged
    assert payload["quote"] not in logged
    assert "http" not in logged
    for item in scored("contradicted"):
        assert item.passage.text not in logged


# ---------------------------------------------------------------- determinism


def test_the_same_inputs_always_produce_the_same_claim(settings: Settings) -> None:
    """No clock, no randomness, no model: stage 5 is a pure function of its inputs.

    Worth pinning because the eval harness and the 7-day cache both assume a
    re-run of the same evidence produces the same verdict.
    """
    first = run("missing_context_small_sample", settings)
    second = run("missing_context_small_sample", settings)

    assert first == second
