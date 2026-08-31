"""Stage 5 — aggregation: rules, not a model, and the provenance trail.

Everything before this stage is a model's opinion. Extraction chose the claims,
stance scoring read each passage, the judge wrote a sentence. This stage decides
what a reader is actually told, and it decides it with ``if`` statements
(``CLAUDE.md``: "Aggregation is RULES, not a model"). No LLM call happens here
and none may be added: the verdict is the one thing in the pipeline that must be
explainable line by line, reproducible from the same inputs, and impossible for
a hostile web page to talk into being stronger than the evidence.

The four rules, as the brief states them, and what each means in code:

1. *A high-confidence refutation from a credible source* → ``contradicted``.
   :func:`is_credible`, and "high-confidence" is structural
   (:func:`side_strength`): a primary document, a published fact-check, or two
   independent sources — never one anonymous page.
2. *Two or more independent supporting sources, or one primary source* →
   ``supported``. :func:`source_group` decides what "independent" means and
   :func:`is_primary` what "primary" means; both are spelled out below.
3. *True-but-misleading signals (tiny sample, outdated, cherry-picked)* →
   ``missing_context``. :func:`detect_signals` looks for those signals in the
   passages themselves, and this branch is checked **before** ``supported`` —
   the whole point of the verdict is that a claim can be technically supported
   and still mislead.
4. Otherwise → ``unverifiable``, with no sources and no confidence.

Where the judge and the rules disagree
--------------------------------------
Deliberate and documented, because it is the decision this stage exists to make:
**the judge may only weaken the rules' verdict, never strengthen it**
(:func:`reconcile`). The rules read structured facts about the evidence — who
published it, how many independent sources there are, whether any of them is
primary — which a passage's *prose* cannot change. The judge reads the prose,
which is written by strangers and may be trying to move the verdict. So a judge
that abstains further than the rules is believed (it saw something in the text
the rules cannot see); a judge that claims more than the rules is not; and a
judge that points the opposite way at equal strength (``supported`` against
``contradicted``) leaves the claim ``unverifiable``, because two of our own
components reading the same passages and disagreeing about their direction is
exactly the situation in which we should not be picking a side in front of a
reader. Every disagreement therefore resolves *toward* abstention.

The judge's own words are used only where they still fit: its evidence sentence
survives only when the final verdict is the verdict it wrote that sentence about
and the sentence actually names one of the sources we kept
(:func:`_judge_evidence`); otherwise the sentence is composed here from passage
metadata. A "CNA confirms this" under a Missing context badge would be a lie
told in the product's own voice.

Verification, again
-------------------
The judge's ``cited_spans`` are re-checked here against the passage texts with
:func:`~app.pipeline.types.span_occurs_in`, even though stage 4 checks them too.
This is the last code that runs before a claim is published, the check is
microseconds, and the property it defends — the judge may only use retrieved
passages, never its own knowledge — is the most important correctness property
in the milestone. A claim whose citations do not check out is ``unverifiable``.

Output
------
A plain dict matching :class:`app.schema_models.Claim`, built through the
generated model so it cannot drift from ``shared/schema.json``, and run through
:func:`app.invariants.validate_claim` before it is returned, so a bug in the
rules above fails here rather than on a reader's screen.

Privacy
-------
Nothing here logs the claim quote, a passage body or a URL (``CLAUDE.md`` rule
6). The decision line carries the claim id, the verdict, the counts and how many
signals fired — not the signals themselves, since one of them quotes a
fact-checker's rating.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Sequence
from datetime import date as date_type
from typing import Any

from app.config import Settings
from app.invariants import ALLOWED_CONFIDENCES, ALLOWED_VERDICTS, UNVERIFIABLE, validate_claim
from app.pipeline.providers.base import domain_of, is_http_url, outlet_from_url
from app.pipeline.retrieve import AGGREGATOR_DOMAINS, ORIGIN_PRIORITY
from app.pipeline.types import (
    ExtractedClaim,
    Judgement,
    Passage,
    ScoredPassage,
    normalize_for_match,
    span_occurs_in,
)
from app.schema_models import Claim, Confidence, Source, Stance, TrailNode, Verdict

logger = logging.getLogger(__name__)

__all__ = [
    "CONFIDENCE_ORDER",
    "GOVERNMENT_LABELS",
    "MAX_EVIDENCE_CHARS",
    "OUTDATED_GAP_DAYS",
    "PRIMARY_ORIGINS",
    "SMALL_SAMPLE_MAX",
    "VERDICT_STRENGTH",
    "WIRE_GROUP",
    "aggregate",
    "build_trail",
    "detect_signals",
    "is_credible",
    "is_primary",
    "reconcile",
    "side_strength",
    "source_group",
]


PRIMARY_ORIGINS = frozenset({"official", "cited_source"})
"""Origins that make a passage *primary* — the document itself, not a report of it.

``official`` is an official dataset or release (data.gov.sg today);
``cited_source`` is the document an attribution claim points at, fetched
directly. Both are the thing the reporting is *about*, which is what "primary
source" means and why one of them alone can carry a ``supported`` verdict.

The honest limit: ``cited_source`` is primary only in the sense that it is the
document the article itself invoked. An article that cites a bad source gets a
primary passage from a bad source — but it is then genuinely true that the
article's own citation says what we report it says, which is what an attribution
claim asks.
"""

GOVERNMENT_LABELS = frozenset({"gov", "govt"})
"""Host labels that mark a government domain, e.g. ``gov.sg``, ``data.gov.sg``.

A government page that arrives through web search rather than through the
official-data provider is still a primary source, and the domain is the only
part of it we can check cheaply. Matched as a whole label so ``gov.sg`` and
``www.gov.uk`` count and ``government-news.example`` does not.
"""

WIRE_GROUP = "wire"
"""The single independence group every wire passage falls into.

**All syndicated copy counts as one source, whatever the domain.**
``wire=True`` is retrieval's statement that this text was published by an agency
and reprinted; the reader sees five mastheads, but there is one newsroom behind
them, and letting five reprints satisfy "two or more independent sources" is the
precise mistake that makes a wire error look corroborated (``CLAUDE.md`` rule 2,
``docs/decisions.md`` §9).

``retrieve.dedupe_wire_copy`` already collapses near-identical copies before
this stage sees them, so in practice a passage still marked ``wire`` here is one
survivor of one syndication group. Merging *all* of them is nonetheless the
deliberate choice: it over-merges two genuinely different agency stories
(Reuters and AFP on the same event) into one source, which costs a claim some
strength and pushes it toward abstention. That is the direction this stage is
allowed to be wrong in.
"""

SMALL_SAMPLE_MAX = 100
"""Below this many respondents, a survey is a tiny-sample signal (rule 3).

Not a statistical threshold and not pretending to be one: a round number, below
which a figure quoted as if it described a population is worth telling a reader
about. It fires the ``missing_context`` branch, never a stronger verdict.
"""

OUTDATED_GAP_DAYS = 365
"""How much newer other material must be before support counts as outdated.

Compared *between passages* — the newest supporting passage against the newest
refuting or neutral one — and never against the wall clock, so the same inputs
always give the same verdict and a test written today still passes next year.
A year is long enough that ordinary follow-up reporting does not trip it and
short enough to catch a figure that a later revision has moved on from.
"""

MIXED_RATINGS = (
    "partly",
    "half true",
    "mixture",
    "misleading",
    "missing context",
    "lacks context",
    "exaggerat",
    "outdated",
    "cherry",
)
"""Fragments of a ClaimReview's own textual rating that mean "true, but…".

A fact-checker that rated a claim "Partly true" has done the work of spotting
the missing context; matching its own words is the cheapest correct signal we
have. Matched as substrings of the normalised rating, so "Mostly true —
misleading framing" fires too. A rating is never shown to a reader as a verdict:
Re-Vera has four verdicts and "Partly true" is not one of them.
"""

MAX_EVIDENCE_CHARS = 400
"""Longest judge sentence accepted as the reader-facing evidence line.

Rule 2 asks for *one* plain-language sentence. Anything past this is an essay,
a leaked prompt or a page's text repeated back at us, and the composed sentence
below is a better thing to show than a truncated one.
"""

VERDICT_STRENGTH = {
    UNVERIFIABLE: 0,
    Verdict.missing_context.value: 1,
    Verdict.supported.value: 2,
    Verdict.contradicted.value: 2,
}
"""How much each verdict claims, for :func:`reconcile`.

``supported`` and ``contradicted`` sit at the same height and point opposite
ways, which is why a disagreement between them cannot be resolved by taking the
lower one.
"""

CONFIDENCE_ORDER = (Confidence.low, Confidence.medium, Confidence.high)
"""``low`` < ``medium`` < ``high``, for capping. Never a percentage (rule 3)."""

_UNCHECKED = "unchecked"
_CONFLICT = "conflict"
_INSUFFICIENT = "insufficient"
_UNCITED = "uncited"
"""Why a claim ended ``unverifiable``. Chooses the wording of the explanation the
verdict must ship (rule 2 / ``docs/decisions.md`` §5); never leaves the backend."""

_SAMPLE_SIZE = re.compile(
    r"\b(\d{1,4})\s+(?:respondents|participants|stallholders|hawkers|people|households)\s+"
    r"(?:were\s+)?(?:surveyed|polled|responded|took part|answered)\b"
    r"|\bsurvey(?:ed)?\s+(?:only\s+)?(\d{1,4})\s+"
    r"(?:respondents|participants|stallholders|hawkers|people|households)\b"
    r"|\bsample\s+(?:size\s+)?of\s+(?:just\s+|only\s+)?(\d{1,4})\b"
    r"|\b(\d{1,4})\s+(?:respondents|participants)\b"
)
"""Sample sizes stated in a passage, in the few shapes news prose actually uses.

Deliberately narrow. A regex that matched any number near the word "survey"
would fire on "a survey of 40 hawker centres" and mark a perfectly good figure
as missing context; these patterns require the number to be counting *people who
answered*.
"""

_SAMPLE_PHRASES = (
    "self-selected",
    "self selected",
    "opt-in survey",
    "opt in survey",
    "non-representative",
    "not representative",
    "unscientific poll",
    "small sample",
    "tiny sample",
)
"""Phrases that say "this sample does not stand for the population" outright."""

_SENTENCE_STANCE = {
    Verdict.supported.value: Stance.supports,
    Verdict.contradicted.value: Stance.refutes,
    Verdict.missing_context.value: Stance.supports,
}
"""Which sources a composed sentence names, per verdict.

``missing_context`` names the supporting ones: the sentence says the claim is
backed *but*, and the "but" clause is the signal, not another outlet.
"""

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
"""Short month names for trail notes ("12 Mar"). English only, like the UI."""

_TRAIL_OUTLET_LIMIT = 3
"""Outlets named in one trail note before it says "and N more". The note is a
muted one-line detail, not the source list; the chips carry the full set."""

_PROSE_OUTLET_LIMIT = 3
"""Outlets named in a composed evidence sentence before it says "and N others"."""


# ---------------------------------------------------------------- passage policy


def is_primary(passage: Passage) -> bool:
    """True when ``passage`` is the original document rather than a report of it.

    Primary means one of two concrete things, both checkable without a judgement
    call: it came from a provider that only returns originals
    (:data:`PRIMARY_ORIGINS`), or it lives on a government domain
    (:data:`GOVERNMENT_LABELS`). One primary source is enough for ``supported``
    on its own, so this predicate is deliberately narrow — a news article about a
    press release is not the press release.
    """
    if passage.origin in PRIMARY_ORIGINS:
        return True
    labels = domain_of(passage.url).split(".")
    return any(label in GOVERNMENT_LABELS for label in labels)


def is_credible(passage: Passage) -> bool:
    """True when ``passage`` may be counted as a source at all.

    Credible here means *provenance we can stand behind*, not *a brand we like*:

    * anything primary (:func:`is_primary`) — an official document;
    * anything from the Fact Check Tools API, which lists reviews by registered
      fact-checking publishers;
    * any other page with a real ``http(s)`` domain that is not a known
      aggregator (:data:`~app.pipeline.retrieve.AGGREGATOR_DOMAINS`) — a
      republisher is evidence of nothing but republishing, and the reader may
      well be reading the aggregator's copy right now.

    There is deliberately **no allowlist of trusted mastheads.** Maintaining one
    would mean this repository quietly ruling on which newsrooms count, it would
    be wrong at the edges the moment it was written, and it would silently
    discard evidence from any outlet nobody thought of. The honest limit of what
    is here: it cannot tell a careful newsroom from a content farm with a tidy
    domain. What stops a lone content farm deciding a verdict is not this
    predicate but :func:`side_strength`, which will not let any single
    non-primary, non-fact-check page reach a verdict by itself.
    """
    if not is_http_url(passage.url):
        return False
    if is_primary(passage) or passage.origin == "factcheck":
        return True
    return not _is_aggregator(passage.url)


def source_group(passage: Passage) -> str:
    """The independence key for ``passage``: passages sharing one are one source.

    Wire copy is :data:`WIRE_GROUP` regardless of domain; everything else is its
    domain, so a site's five pages about a story are one source and not five.
    """
    if passage.wire:
        return WIRE_GROUP
    return domain_of(passage.url) or passage.url


def side_strength(scored: Sequence[ScoredPassage], *, refutation: bool) -> int:
    """How strongly one side of the evidence is held: 0 none, 1 weak, 2 strong.

    Strong (2) means the brief's bar is met — a primary source, or two or more
    independent sources (:func:`source_group`). For a *refutation* a published
    fact-check also counts as strong: a ClaimReview is a fact-checker's finished
    work on this exact claim, and "a high-confidence refutation from a credible
    source" is precisely what it is.

    That asymmetry is intentional. It is the brief's own: rule 1 admits a single
    high-confidence refutation, rule 2 demands two independent sources or a
    primary one. Affirming a claim on one page's say-so is the failure that
    would hurt a reader most, because it is the answer they were hoping for.

    Weak (1) is at least one credible source that nothing else corroborates. It
    is never enough for ``supported`` or ``contradicted`` on its own.
    """
    if not scored:
        return 0
    passages = [item.passage for item in scored]
    if any(is_primary(passage) for passage in passages):
        return 2
    if refutation and any(passage.origin == "factcheck" for passage in passages):
        return 2
    if len({source_group(passage) for passage in passages}) >= 2:
        return 2
    return 1


def detect_signals(scored: Sequence[ScoredPassage]) -> list[str]:
    """Find true-but-misleading signals in the passages, as reader-facing clauses.

    Three signals, each derived from something actually present in the retrieved
    material rather than from a model's impression of it:

    * **tiny sample** — a stated sample size under :data:`SMALL_SAMPLE_MAX`, or a
      phrase that says the sample was self-selected (:data:`_SAMPLE_PHRASES`);
    * **outdated** — the newest refuting or neutral passage is at least
      :data:`OUTDATED_GAP_DAYS` newer than the newest supporting one, so the
      support may have been overtaken;
    * **rated "partly true"** — a fact-checker's own rating already says the
      claim needs context (:data:`MIXED_RATINGS`).

    Returned as clauses that can be dropped into a sentence after "but", in a
    fixed order so the same evidence always produces the same wording. An empty
    list means no signal fired, not that none exists: cherry-picking that shows
    only in the prose is what the judge is for, and it reaches the reader through
    :func:`reconcile` weakening a ``supported`` verdict to ``missing_context``.
    """
    signals: list[str] = []
    credible = [item for item in scored if is_credible(item.passage)]

    if any(_has_small_sample(item.passage.text) for item in credible):
        signals.append("it rests on a very small sample")

    if _support_is_outdated(credible):
        signals.append("more recent material has since been published")

    for item in credible:
        rating = item.passage.rating
        if rating and _is_mixed_rating(rating):
            signals.append(f'a fact-checker rated it "{rating.strip()}"')
            break

    return signals


def reconcile(rule_verdict: str, judge_verdict: str) -> str:
    """Combine the rules' verdict with the judge's, resolving toward abstention.

    * A judge verdict that is not one of the four is not a verdict —
      ``unverifiable``.
    * The two agree — that verdict.
    * One abstains further than the other (:data:`VERDICT_STRENGTH`) — the more
      abstaining one wins, whichever component it came from.
    * They claim equal strength in opposite directions (``supported`` against
      ``contradicted``) — ``unverifiable``. Neither is discounted in favour of
      the other; the disagreement itself is the finding.

    The module docstring explains why the judge is never allowed to strengthen a
    verdict. The short version: the rules read who published the evidence, which
    a hostile page cannot rewrite, and the judge reads the page.
    """
    if judge_verdict not in ALLOWED_VERDICTS:
        return UNVERIFIABLE
    if judge_verdict == rule_verdict:
        return rule_verdict
    judge_strength = VERDICT_STRENGTH[judge_verdict]
    rule_strength = VERDICT_STRENGTH[rule_verdict]
    if judge_strength < rule_strength:
        return judge_verdict
    if rule_strength < judge_strength:
        return rule_verdict
    return UNVERIFIABLE


# ---------------------------------------------------------------- the stage


def aggregate(
    claim: ExtractedClaim,
    scored: Sequence[ScoredPassage],
    judgement: Judgement,
    *,
    article_url: str,
    settings: Settings,
) -> dict[str, Any]:
    """Turn one claim's scored passages and judgement into a wire-ready claim dict.

    Applies the four rules (module docstring), reconciles them with the judge,
    re-verifies the judge's citations, and builds the sources, the evidence
    sentence, the confidence and the provenance trail from the passages that
    survived. The result is validated against
    :class:`~app.schema_models.Claim` and :func:`~app.invariants.validate_claim`
    before it is returned: sources are empty if and only if the verdict is
    ``unverifiable``, and confidence is null on exactly the same condition.

    No network, no model, no clock: the same inputs always produce the same
    claim.
    """
    usable = _usable(scored, article_url)
    supporting = [item for item in usable if item.stance is Stance.supports]
    refuting = [item for item in usable if item.stance is Stance.refutes]
    signals = detect_signals(usable)

    support_strength = side_strength(supporting, refutation=False)
    refute_strength = side_strength(refuting, refutation=True)

    rule_verdict, relied, reason = _apply_rules(
        supporting=supporting,
        refuting=refuting,
        usable=usable,
        signals=signals,
        support_strength=support_strength,
        refute_strength=refute_strength,
    )

    verdict = reconcile(rule_verdict, judgement.verdict)
    if verdict != rule_verdict and verdict == UNVERIFIABLE:
        reason = _CONFLICT if VERDICT_STRENGTH[rule_verdict] == 2 else _INSUFFICIENT

    if verdict != UNVERIFIABLE and not _citations_verified(judgement, usable):
        verdict, reason = UNVERIFIABLE, _UNCITED

    sources = [] if verdict == UNVERIFIABLE else _sources(relied, settings)
    if verdict != UNVERIFIABLE and not sources:
        # Cannot happen while the rules only reach a verdict through credible
        # passages, and is a downgrade rather than a crash if it ever does: rule
        # 2 says no evidence means unverifiable, and that is true of a rule bug
        # as much as of an empty web.
        logger.warning("aggregate: claim=%s reached %s with no sources", claim.id, verdict)
        verdict, reason, relied = UNVERIFIABLE, _INSUFFICIENT, []

    if verdict == UNVERIFIABLE:
        relied = []
        confidence: Confidence | None = None
        evidence = _unverifiable_evidence(usable, reason)
    else:
        opposing = refuting if verdict != Verdict.contradicted.value else supporting
        confidence = _confidence(verdict, relied, opposing, judgement)
        evidence = _judge_evidence(judgement, verdict, sources) or _composed_evidence(
            verdict, sources, signals
        )

    trail = build_trail(
        verdict=verdict,
        relied=relied,
        usable=usable,
        article_url=article_url,
    )

    model = Claim(
        id=claim.id,
        quote=claim.quote,
        start=claim.start,
        end=claim.end,
        verdict=Verdict(verdict),
        confidence=confidence,
        evidence=evidence,
        sources=sources,
        trail=trail,
    )
    payload: dict[str, Any] = model.model_dump(mode="json")

    # The last gate before the wire. `validate_claim` raises, and a raise here is
    # the correct outcome: a claim that breaks a product rule is a failed job,
    # never something a reader is shown.
    validate_claim(payload)

    logger.info(
        "aggregate: claim=%s verdict=%s confidence=%s sources=%d "
        "support=%d/%d refute=%d/%d signals=%d reason=%s",
        claim.id,
        verdict,
        confidence.value if confidence else "none",
        len(sources),
        len(supporting),
        support_strength,
        len(refuting),
        refute_strength,
        len(signals),
        reason if verdict == UNVERIFIABLE else "-",
    )
    return payload


def _apply_rules(
    *,
    supporting: list[ScoredPassage],
    refuting: list[ScoredPassage],
    usable: list[ScoredPassage],
    signals: list[str],
    support_strength: int,
    refute_strength: int,
) -> tuple[str, list[ScoredPassage], str]:
    """The four rules, in the order the brief states them.

    Returns the verdict, the passages it rests on, and — for an
    ``unverifiable`` — why, which decides the wording of the explanation that
    verdict must ship.

    ``missing_context`` is checked *before* ``supported`` and requires supporting
    evidence to exist: the verdict means "true, but misleading", so a claim
    nothing supports cannot earn it, and a claim two outlets support on the back
    of a 42-person survey must not be called ``supported`` just because the
    counting worked out.
    """
    if refute_strength == 2 and refute_strength > support_strength:
        return Verdict.contradicted.value, refuting, ""

    if signals and supporting:
        return Verdict.missing_context.value, supporting + _signal_context(usable), ""

    if support_strength == 2 and support_strength > refute_strength:
        return Verdict.supported.value, supporting, ""

    if not usable:
        return UNVERIFIABLE, [], _UNCHECKED
    if support_strength == 2 and refute_strength == 2:
        return UNVERIFIABLE, [], _CONFLICT
    return UNVERIFIABLE, [], _INSUFFICIENT


def _signal_context(usable: list[ScoredPassage]) -> list[ScoredPassage]:
    """The neutral passages that carry a signal, so the reader can see them too.

    A ``missing_context`` verdict is only as good as the reader's ability to
    check it, and the document that says "42 respondents" is usually the neutral
    one nobody scored as support. Included as sources with their real
    ``neutral`` stance, never relabelled.
    """
    return [
        item
        for item in usable
        if item.stance is Stance.neutral
        and (_has_small_sample(item.passage.text) or _is_mixed_rating(item.passage.rating or ""))
    ]


def _usable(scored: Sequence[ScoredPassage], article_url: str) -> list[ScoredPassage]:
    """Drop what cannot be a source: bad URLs, aggregator copy, the article itself.

    A passage from the very page being checked is not evidence about it —
    retrieval can land back on the article through a search result or an
    aggregator's reprint — and citing an article to itself is the most confident
    form of no evidence at all. Everything kept here has passed
    :func:`is_credible`, so every later count is a count of credible sources.
    """
    article_key = _url_key(article_url)
    return [
        item
        for item in scored
        if is_credible(item.passage) and _url_key(item.passage.url) != article_key
    ]


def _citations_verified(judgement: Judgement, usable: Sequence[ScoredPassage]) -> bool:
    """True when every span the judge claims to have quoted is really in a passage.

    Checked against the passages *as retrieved*, with
    :func:`~app.pipeline.types.span_occurs_in` (forgiving about typography,
    strict about words). A judgement with no cited spans at all fails too: a
    verdict resting on nothing quotable is a verdict resting on the model's own
    knowledge, which rule 2 forbids outright.

    Stage 4 runs this check as well. It runs again here because this is the last
    code before the wire, it costs microseconds, and the alternative to
    duplicating it is trusting that no future refactor of stage 4 drops it.
    """
    if not judgement.cited_spans:
        return False
    texts = [item.passage.text for item in usable]
    if not texts:
        return False
    return all(span_occurs_in(span, texts) for span in judgement.cited_spans)


# ---------------------------------------------------------------- sources


def _sources(relied: Sequence[ScoredPassage], settings: Settings) -> list[Source]:
    """Build the reader-facing source chips from the passages actually relied on.

    Ordered primary first, then by :data:`~app.pipeline.retrieve.ORIGIN_PRIORITY`
    — the reader should meet the strongest thing we have first — and de-duplicated
    by URL. Capped at ``settings.max_passages_per_claim`` so a caller handing this
    stage more passages than retrieval's cap cannot put twenty chips on one card.

    ``date`` is the empty string when the source stated none. The schema types it
    as a required string, and a blank is the one honest value available: inventing
    a date would put a fabricated fact on the chip, and dropping the source would
    throw away real evidence over a missing field.
    """
    ordered = sorted(
        relied,
        key=lambda item: (
            0 if is_primary(item.passage) else 1,
            ORIGIN_PRIORITY.get(item.passage.origin, len(ORIGIN_PRIORITY)),
        ),
    )
    sources: list[Source] = []
    seen: set[str] = set()
    for item in ordered:
        key = _url_key(item.passage.url)
        if key in seen:
            continue
        seen.add(key)
        sources.append(
            Source(
                url=item.passage.url,
                outlet=item.passage.outlet.strip() or outlet_from_url(item.passage.url),
                date=item.passage.date or "",
                wire=item.passage.wire,
                stance=item.stance,
            )
        )
        if len(sources) >= settings.max_passages_per_claim:
            break
    return sources


# ---------------------------------------------------------------- confidence


def _confidence(
    verdict: str,
    relied: Sequence[ScoredPassage],
    opposing: Sequence[ScoredPassage],
    judgement: Judgement,
) -> Confidence:
    """Pick ``low``/``medium``/``high`` from the shape of the evidence, then cap it.

    The base level counts what is actually there — a primary source, and how many
    independent groups agree:

    ======================================  ========
    Evidence                                Base
    ======================================  ========
    primary + another independent source    ``high``
    three or more independent sources       ``high``
    a primary source, or two independent    ``medium``
    one credible source                     ``low``
    ======================================  ========

    Then two caps, both downward only:

    * anything credible pointing the other way demotes one level — a contested
      claim is not a confident one;
    * ``missing_context`` never exceeds ``medium``, because that verdict is an
      assertion about what the evidence *leaves out*, which is a harder thing to
      be sure of than what it says.

    Finally the judge: when the final verdict is the one the judge wrote, its
    stated confidence is a ceiling (never a floor — it cannot raise what the
    evidence does not support). When we overruled it, its confidence was about a
    different verdict and the result is capped at ``low``: our own two components
    disagreed, and the meter should say so.
    """
    groups = {source_group(item.passage) for item in relied}
    primary = any(is_primary(item.passage) for item in relied)

    if (primary and len(groups) >= 2) or len(groups) >= 3:
        level = Confidence.high
    elif primary or len(groups) >= 2:
        level = Confidence.medium
    else:
        level = Confidence.low

    if opposing:
        level = _demote(level)
    if verdict == Verdict.missing_context.value:
        level = _cap(level, Confidence.medium)

    stated = judgement.confidence
    if judgement.verdict != verdict:
        level = Confidence.low
    elif stated is not None and stated in ALLOWED_CONFIDENCES:
        level = _cap(level, Confidence(stated))
    return level


def _demote(level: Confidence) -> Confidence:
    """One step down the ladder; ``low`` stays ``low``."""
    index = CONFIDENCE_ORDER.index(level)
    return CONFIDENCE_ORDER[max(index - 1, 0)]


def _cap(level: Confidence, ceiling: Confidence) -> Confidence:
    """``level``, but never above ``ceiling``."""
    return min(level, ceiling, key=CONFIDENCE_ORDER.index)


# ---------------------------------------------------------------- evidence


def _judge_evidence(judgement: Judgement, verdict: str, sources: Sequence[Source]) -> str | None:
    """The judge's sentence, if it is still the right sentence to show.

    Three conditions, all of them about honesty rather than style:

    * the final verdict is the verdict the judge wrote about — a sentence
      arguing for ``supported`` under a Missing context badge would be the
      product contradicting itself;
    * it is one plain sentence's worth of text (:data:`MAX_EVIDENCE_CHARS`) and
      not empty;
    * it names at least one source we kept, which is what rule 2 asks of it, and
      which no composed fallback can supply on the judge's behalf.

    ``None`` means "compose one instead". Nothing is edited or patched up: a
    sentence that fails any of these is not a sentence to repair.
    """
    if verdict == UNVERIFIABLE or judgement.verdict != verdict:
        return None
    text = " ".join(judgement.evidence.split())
    if not text or len(text) > MAX_EVIDENCE_CHARS:
        return None
    if not any(span_occurs_in(source.outlet, [text]) for source in sources):
        return None
    return text


def _composed_evidence(verdict: str, sources: Sequence[Source], signals: list[str]) -> str:
    """One sentence built from the sources we kept, when the judge's cannot be used.

    Every word of it comes from metadata: the outlets are the outlets, the verb
    is the verdict, and the ``missing_context`` clause is whichever signal
    :func:`detect_signals` actually found in a passage. Nothing here describes
    the content of the evidence, because this function has not read it — it says
    who said something and what our rules made of it, which is true and is the
    least a reader needs.

    Only the sources that actually point the verdict's way are named. A
    ``missing_context`` claim usually cites the document that *undercuts* it as
    well — the survey with 42 respondents — and writing "the survey backs this
    claim" would misdescribe the one source the reader most needs to read
    correctly. The chips still carry every source, each with its own stance.
    """
    wanted = _SENTENCE_STANCE[verdict]
    names = [source.outlet for source in sources if source.stance is wanted] or [
        source.outlet for source in sources
    ]
    subject = _prose_join(names)
    plural = len(names) != 1

    if verdict == Verdict.supported.value:
        return f"{subject} {'support' if plural else 'supports'} this claim."
    if verdict == Verdict.contradicted.value:
        return f"{subject} {'contradict' if plural else 'contradicts'} this claim."
    clause = signals[0] if signals else "important context is missing"
    return f"{subject} {'back' if plural else 'backs'} this claim, but {clause}."


def _unverifiable_evidence(usable: Sequence[ScoredPassage], reason: str) -> str:
    """Explain what was searched and not found (rule 2 / ``docs/decisions.md`` §5).

    An ``unverifiable`` claim carries no sources, so this sentence is the only
    account the reader gets of the work done on their behalf. It names the
    outlets that were actually read, and says which of the four dead ends this
    was: nothing retrieved, sources that disagree, sources that do not settle it,
    or evidence that could not be tied back to what those sources say.
    """
    names = _unique(item.passage.outlet for item in _outlets(usable))
    if reason == _UNCHECKED or not names:
        return (
            "Searched fact-check databases and the web and found nothing that "
            "addresses this claim."
        )
    subject = _prose_join(names)
    if reason == _CONFLICT:
        return f"{subject} disagree about this claim, so it is left unresolved."
    if reason == _UNCITED:
        return f"Checked {subject}, but the evidence could not be tied back to what they say."
    return f"Checked {subject}, but nothing found there settles this claim."


# ---------------------------------------------------------------- trail


def build_trail(
    *,
    verdict: str,
    relied: Sequence[ScoredPassage],
    usable: Sequence[ScoredPassage],
    article_url: str,
) -> list[TrailNode]:
    """Build the two or three provenance nodes, entirely from real metadata.

    * **This article** — where the reader is, from ``article_url``'s domain, and
      whether that domain is a republisher (
      :data:`~app.pipeline.retrieve.AGGREGATOR_DOMAINS`).
    * **Independent reports** — the non-primary outlets behind the verdict,
      separated by ``·``. Present whenever there are any, and present with
      "none found" on an ``unverifiable`` claim, so the trail always ends
      somewhere honest rather than stopping mid-sentence.
    * **Original source** — the primary document, with its date when it stated
      one ("gov.sg release, 12 Mar"). Omitted when there is no primary source,
      and omitted on an ``unverifiable`` claim, where showing an "original
      source" beside an empty source list would promise evidence that verdict
      does not have.

    No note is written that is not read off a passage: outlet names, dates, wire
    flags and origins only. A trail is a chain of custody, and an invented link
    in it is worse than a short chain.
    """
    trail = [TrailNode(label="This article", note=_article_note(article_url))]

    if verdict == UNVERIFIABLE:
        looked_at = _unique(item.passage.outlet for item in _outlets(usable))
        note = f"{_join_outlets(looked_at)} · nothing conclusive" if looked_at else "none found"
        trail.append(TrailNode(label="Independent reports", note=note))
        return trail

    independent = _unique(
        item.passage.outlet
        for item in _outlets(relied)
        if not is_primary(item.passage) or item.passage.origin == "factcheck"
    )
    if independent:
        trail.append(TrailNode(label="Independent reports", note=_join_outlets(independent)))

    primary = next((item.passage for item in relied if is_primary(item.passage)), None)
    if primary is not None:
        trail.append(TrailNode(label="Original source", note=_primary_note(primary)))

    if len(trail) == 1:
        trail.append(TrailNode(label="Independent reports", note="none found"))
    return trail


def _article_note(article_url: str) -> str:
    """The muted line under "This article": which domain it is, and whether it
    republishes other people's reporting."""
    domain = domain_of(article_url)
    if not domain:
        return "the page you are reading"
    if _is_aggregator(article_url):
        return f"republished on {domain}"
    return f"published on {domain}"


def _primary_note(passage: Passage) -> str:
    """"gov.sg release, 12 Mar" — the outlet, and its date when it stated one."""
    outlet = passage.outlet.strip() or outlet_from_url(passage.url)
    human = _human_date(passage.date)
    return f"{outlet}, {human}" if human else outlet


# ---------------------------------------------------------------- small helpers


def _outlets(items: Iterable[ScoredPassage]) -> list[ScoredPassage]:
    """``items`` with a usable outlet name, filling in the domain where a provider
    left the name blank. Never invents a masthead — a domain is ugly and true."""
    named: list[ScoredPassage] = []
    for item in items:
        if item.passage.outlet.strip():
            named.append(item)
        else:
            named.append(
                ScoredPassage(
                    passage=_renamed(item.passage, outlet_from_url(item.passage.url)),
                    stance=item.stance,
                    rationale_quote=item.rationale_quote,
                )
            )
    return named


def _renamed(passage: Passage, outlet: str) -> Passage:
    """``passage`` with ``outlet`` replaced. Frozen dataclass, so a new one."""
    return Passage(
        text=passage.text,
        url=passage.url,
        outlet=outlet,
        date=passage.date,
        wire=passage.wire,
        origin=passage.origin,
        rating=passage.rating,
    )


def _unique(names: Iterable[str]) -> list[str]:
    """De-duplicate ``names`` case-insensitively, keeping the first spelling."""
    seen: set[str] = set()
    kept: list[str] = []
    for name in names:
        key = normalize_for_match(name)
        if not key or key in seen:
            continue
        seen.add(key)
        kept.append(name.strip())
    return kept


def _join_outlets(names: Sequence[str]) -> str:
    """Trail-note form: ``CNA · Reuters``, truncated with "and N more"."""
    head = list(names[:_TRAIL_OUTLET_LIMIT])
    joined = " · ".join(head)
    remaining = len(names) - len(head)
    return f"{joined} and {remaining} more" if remaining > 0 else joined


def _prose_join(names: Sequence[str]) -> str:
    """Sentence form: ``CNA``, ``CNA and Reuters``, ``CNA, Reuters and gov.sg``."""
    head = list(names[:_PROSE_OUTLET_LIMIT])
    remaining = len(names) - len(head)
    if remaining > 0:
        head.append(f"{remaining} other{'s' if remaining > 1 else ''}")
    if not head:
        return "the sources checked"
    if len(head) == 1:
        return head[0]
    return f"{', '.join(head[:-1])} and {head[-1]}"


def _human_date(iso: str | None) -> str | None:
    """``"2026-03-12"`` → ``"12 Mar"``; anything unparseable → ``None``."""
    if not iso:
        return None
    try:
        parsed = date_type.fromisoformat(iso[:10])
    except ValueError:
        return None
    return f"{parsed.day} {_MONTHS[parsed.month - 1]}"


def _url_key(url: str) -> str:
    """A URL folded for comparison: lower-cased, without a trailing slash."""
    return url.strip().rstrip("/").casefold()


def _is_aggregator(url: str) -> bool:
    """True when ``url``'s domain is, or is under, a known republisher.

    The domain list is retrieval's (:data:`~app.pipeline.retrieve.AGGREGATOR_DOMAINS`),
    imported rather than copied so the two stages can never drift into disagreeing
    about what an aggregator is.
    """
    domain = domain_of(url)
    return any(
        domain == aggregator or domain.endswith(f".{aggregator}")
        for aggregator in AGGREGATOR_DOMAINS
    )


def _has_small_sample(text: str) -> bool:
    """True when ``text`` states a sample size under :data:`SMALL_SAMPLE_MAX`, or
    says in words that the sample does not stand for a population."""
    folded = normalize_for_match(text)
    if any(phrase in folded for phrase in _SAMPLE_PHRASES):
        return True
    return any(
        int(digits) < SMALL_SAMPLE_MAX
        for match in _SAMPLE_SIZE.finditer(folded)
        for digits in match.groups()
        if digits
    )


def _is_mixed_rating(rating: str) -> bool:
    """True when a ClaimReview's own rating says "true, but…" (:data:`MIXED_RATINGS`)."""
    folded = normalize_for_match(rating)
    return any(fragment in folded for fragment in MIXED_RATINGS)


def _support_is_outdated(scored: Sequence[ScoredPassage]) -> bool:
    """True when the newest non-supporting passage is a year or more newer than the
    newest supporting one — measured between passages, never against the clock."""
    supporting = _newest(item for item in scored if item.stance is Stance.supports)
    other = _newest(item for item in scored if item.stance is not Stance.supports)
    if supporting is None or other is None:
        return False
    return (other - supporting).days >= OUTDATED_GAP_DAYS


def _newest(items: Iterable[ScoredPassage]) -> date_type | None:
    """The latest parseable date among ``items``, or ``None`` if none stated one."""
    dates = [parsed for item in items if (parsed := _parse_date(item.passage.date)) is not None]
    return max(dates) if dates else None


def _parse_date(iso: str | None) -> date_type | None:
    """``"2026-03-12"`` → a ``date``; anything unparseable → ``None``."""
    if not iso:
        return None
    try:
        return date_type.fromisoformat(iso[:10])
    except ValueError:
        return None
