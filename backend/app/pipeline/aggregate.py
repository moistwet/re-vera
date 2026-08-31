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
   passages themselves. This branch requires the same strength rule 2 does
   (:func:`side_strength` again) — a single uncorroborated page with a signal
   attached is not "technically supported", it is unsupported *and* flagged,
   which is ``unverifiable``, not a decided verdict (a redteam finding: the
   old code let one weak page reach ``missing_context`` on a signal alone).
4. Otherwise → ``unverifiable``, with no sources and no confidence.

Two sides tied at full strength — a credible refutation *and* credible
support, both strong enough on their own to decide — are the one case none of
the four rules gets to pick between, and it is checked **before** any of them:
a tie is a real disagreement in the evidence, not a reason to soften to
``missing_context`` (a redteam finding: the old order let a tied refutation
fall through to the friendlier verdict and silently drop the refuting source).

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
reader. Every disagreement therefore resolves *toward* abstention. That
downgrade is reported to the reader as a genuine disagreement only when it
really is one — the judge returning garbage or simply abstaining further is not
"the sources disagree" (a redteam finding: the old reason logic said so anyway
whenever the rules alone had reached a decided verdict).

Primary sources, narrowly
--------------------------
"Primary" is the strongest single signal this stage has — the module docstring
above already spends most of one rule on it because one primary passage is
*alone* enough for ``supported``. :func:`is_primary` is deliberately narrow
about what qualifies, for two separate redteam findings:

* **A publisher cannot corroborate itself.** A passage whose registrable
  domain is the *article's* registrable domain is dropped in :func:`_usable`
  before it ever reaches a strength calculation — not merely excluded from
  "primary", excluded from being counted as a source at all, so it cannot
  supply even the weak "at least one credible source" floor. The exact
  article URL (:func:`~app.pipeline.providers.base.same_page`) and a sibling
  page on the same site (:func:`~app.pipeline.providers.base.registrable_domain`)
  are both covered.
* **A cited document only answers the narrow question it was fetched for.**
  ``origin == "cited_source"`` is the document an *attribution* claim points
  at ("X said Y"), fetched directly. It counts as primary only when the claim
  actually is an attribution claim — never as a general-purpose primary
  source for a numeric or general claim that happens to link somewhere.
* **A dataset catalogue entry is not the data.** data.gov.sg's own provider
  never reads a figure out of a dataset — every passage it returns is a
  title, an agency name and a description (see
  ``app.pipeline.providers.official``'s module docstring). Treating that as
  "the original document that states the fact" is exactly backwards, so
  :data:`CATALOGUE_ONLY_DOMAINS` excludes it from primary status outright.
  Government primariness is decided by domain (:data:`GOVERNMENT_LABELS`),
  which still covers a genuine agency page or press release reached by any
  other route.

Unverified text may inform, never alone decide
------------------------------------------------
:attr:`~app.pipeline.types.Passage.provenance_verified` says whether a
passage's *text* is known to really appear on the page it is attributed to,
rather than being a model's free-form summary of it. A single passage —
primary, or a published fact-check refuting the claim — is enough on its own
to decide a verdict (rules 1 and 2), so :func:`side_strength` requires that
*single* deciding passage to be verified before it counts for that. Two or
more independently corroborating passages are a different kind of evidence —
the corroboration is the safeguard, not any one passage's provenance — so
that path is not gated the same way; today's web-search provider never sets
this field; if it stayed gated, no article could ever be marked ``supported``
by ordinary reporting.

The judge's own words are used only where they still fit: its evidence sentence
survives only when the final verdict is the verdict it wrote that sentence about,
it is one plain sentence's worth of text with no third-party formatting tricks,
it actually names one of the sources we kept, and it does not reproduce a
foreign fact-checker's own verdict word as if it were ours (:func:`_judge_evidence`
— a redteam finding: a ClaimReview's "False" or "pants on fire", present
verbatim in the retrieved passage, could otherwise ride straight through a
judge's paraphrase onto a reader's screen under any of our four badges).
Otherwise the sentence is composed here from passage metadata, and only from
sources that actually point the verdict's own way — never describing a
refuting source as if it backed the claim (:func:`_composed_evidence`, another
redteam finding).

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
from app.pipeline.providers.base import (
    domain_of,
    is_http_url,
    outlet_from_url,
    registrable_domain,
    same_page,
    url_key,
)
from app.pipeline.retrieve import AGGREGATOR_DOMAINS, ORIGIN_PRIORITY
from app.pipeline.types import (
    ClaimKind,
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
    "CATALOGUE_ONLY_DOMAINS",
    "CONFIDENCE_ORDER",
    "GOVERNMENT_LABELS",
    "MAX_EVIDENCE_CHARS",
    "OUTDATED_GAP_DAYS",
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


CATALOGUE_ONLY_DOMAINS = frozenset({"data.gov.sg"})
"""Domains known to host a dataset *catalogue* only, never the document that
states a claim's figure.

``app.pipeline.providers.official`` — this project's only ``origin ==
"official"`` provider — is explicit that it "does not read a figure out of"
a dataset: every passage it returns is a title, a publishing agency and a
description, never a number from the table itself. That passage still sits on
a ``.gov.sg`` domain, so the ordinary government-domain check
(:data:`GOVERNMENT_LABELS`) would otherwise grant it primary status anyway —
this is the domain-level carve-out that stops it (a redteam finding: a
catalogue listing was alone sufficient for ``supported``, and rendered to the
reader as "Original source").

Deliberately a domain, not an origin check: origin alone cannot distinguish a
genuine future official-data provider that *does* serve real document text
from this one. If such a provider is added on a different domain, no change
is needed here; if it is added *on* data.gov.sg, this set needs a companion
allowance, not removal.
"""

GOVERNMENT_LABELS = frozenset({"gov", "govt"})
"""Host labels that mark a government domain, e.g. ``gov.sg``, ``data.gov.sg``.

A government page that arrives through web search rather than through the
official-data provider is still a primary source, and the domain is the only
part of it we can check cheaply. Matched as a whole label of the *registrable*
domain (:func:`~app.pipeline.providers.base.registrable_domain`, not the raw
host) so ``gov.sg`` and ``www.gov.uk`` count, ``government-news.example``
does not, and a subdomain crafted to smuggle the word in — ``gov.sg.evil.com``,
whose registrable domain is ``evil.com`` — does not either (a redteam
finding: the old check split the raw host, which a hostile subdomain label
could spoof)."""

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

_RATING_SIGNAL_PHRASES: tuple[tuple[str, str], ...] = (
    ("partly", "rated it only partly true"),
    ("half true", "rated it only half true"),
    ("mixture", "rated it a mixture of true and false"),
    ("misleading", "said the framing is misleading"),
    ("missing context", "said it is missing context"),
    ("lacks context", "said it lacks context"),
    ("exaggerat", "said the figure is exaggerated"),
    ("outdated", "said the figure is outdated"),
    ("cherry", "said the figure is cherry-picked"),
)
"""Fragments of a ClaimReview's own textual rating that mean "true, but…",
mapped to a clause **Re-Vera writes**, never the fact-checker's own words.

A fact-checker that rated a claim "Partly true" has done the work of spotting
the missing context, and matching its own words is the cheapest correct signal
we have — but the rating string itself is attacker-controlled third-party text
(a publisher's own wording, up to and including "FALSE" or "pants on fire" in
whatever case they chose), and printing it back verbatim in Re-Vera's own
sentence is exactly how a foreign verdict word ends up under one of our four
badges (a redteam finding). The phrase on the right is ours; the only thing
taken from the rating is *which* one applies, via a substring match on the
normalised fragment on the left. Order matters: the first fragment that
matches wins, so the tuple order is the resolution order for a rating that
happens to match more than one.
"""

MAX_EVIDENCE_CHARS = 400
"""Longest judge sentence accepted as the reader-facing evidence line.

Rule 2 asks for *one* plain-language sentence. Anything past this is an essay,
a leaked prompt or a page's text repeated back at us, and the composed sentence
below is a better thing to show than a truncated one.
"""

_OUTLET_MAX_CHARS = 120
"""Longest outlet name accepted from a passage before it is shown anywhere.

Matches the bound retrieval's providers already apply on the way in
(``clean_text(..., limit=120)``); re-applied here defensively, because this
stage must not simply trust that every provider enforced it (a redteam
finding: aggregation interpolated an outlet name straight into product copy
with no length or character check of its own)."""

_RATING_MAX_CHARS = 120
"""Same bound as :data:`_OUTLET_MAX_CHARS`, for a ClaimReview's rating text
before it is even inspected for a signal phrase."""

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

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
"""C0/DEL control characters — not ordinary whitespace, which
:func:`_sanitize` already collapses, but the kind of byte a hostile page or a
copy-pasted rating can carry that a card should never render as-is."""

_FOREIGN_VERDICT_CAPS = re.compile(r"\b(TRUE|FALSE|FAKE|HOAX)\b")
"""A foreign source's own verdict word, shouted — the shape a ClaimReview
rating or a headline actually takes ("FALSE", "FAKE NEWS"), as opposed to the
ordinary, lower-case use of "true"/"false" inside a normal explanatory
sentence, which this deliberately does not flag. Case-sensitive on purpose:
catching only the shouted form keeps this narrow to the real attack (a
rating's own words riding through a judge's paraphrase) instead of rejecting
any judge sentence that happens to use "true" or "false" as an ordinary
English word."""

_FOREIGN_VERDICT_PHRASES = (
    "pants on fire",
    "mostly false",
    "mostly true",
    "half true",
    "barely true",
    "hoax",
    "debunked",
    "misinformation",
    "disinformation",
)
"""Fact-checker rating vocabulary that is unambiguous wherever it appears,
unlike a bare "true"/"false" — checked case-insensitively via
:func:`~app.pipeline.types.normalize_for_match` against the whole judge
sentence, alongside :data:`_FOREIGN_VERDICT_CAPS`, in :func:`_judge_evidence`.
Re-Vera has four verdicts; none of them is spelled like this."""


# ---------------------------------------------------------------- passage policy


def is_primary(passage: Passage, *, claim_kind: ClaimKind = "general") -> bool:
    """True when ``passage`` is the original document rather than a report of it.

    Narrowly and testably defined, none of it a judgement call:

    * never a dataset catalogue domain (:data:`CATALOGUE_ONLY_DOMAINS`) — a
      title, an agency name and a description is not the document that states
      the fact, whatever else is true of it;
    * a ``cited_source`` passage — the document an *attribution* claim points
      at — only when ``claim_kind`` really is ``"attribution"``. The article's
      own citation settles "did X say Y", and nothing wider;
    * otherwise, a government domain (:data:`GOVERNMENT_LABELS`), which covers
      an agency page or press release reached by any route (search, a
      citation on an attribution claim, direct fetch).

    One primary source is enough for ``supported`` on its own, so this
    predicate is deliberately narrow — a news article about a press release is
    not the press release, and neither is a link the article happened to
    include.
    """
    domain = registrable_domain(passage.url)
    if domain in CATALOGUE_ONLY_DOMAINS:
        return False
    if passage.origin == "cited_source":
        return claim_kind == "attribution"
    labels = domain.split(".") if domain else []
    return any(label in GOVERNMENT_LABELS for label in labels)


def is_credible(passage: Passage, *, claim_kind: ClaimKind = "general") -> bool:
    """True when ``passage`` may be counted as a source at all.

    Credible here means *provenance we can stand behind*, not *a brand we like*:

    * anything primary (:func:`is_primary`);
    * anything from the official-data or Fact Check Tools providers
      (``origin in {"official", "factcheck"}``) — structured data an API
      returned as fact, trustworthy as *provenance* even where
      :func:`is_primary` correctly declines to call a catalogue entry
      decisive on its own; a reprint of an official record on an aggregator
      domain is still an official record;
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
    if is_primary(passage, claim_kind=claim_kind):
        return True
    if passage.origin in {"official", "factcheck"}:
        return True
    return not _is_aggregator(passage.url)


def source_group(passage: Passage) -> str:
    """The independence key for ``passage``: passages sharing one are one source.

    Wire copy is :data:`WIRE_GROUP` regardless of domain; everything else is
    its **registrable** domain
    (:func:`~app.pipeline.providers.base.registrable_domain`), so a site's
    five pages about a story — on any number of subdomains — are one source
    and not five (a redteam finding: keying on the raw host let
    ``news.example.com`` and ``shop.example.com`` count as two independent
    sources)."""
    if passage.wire:
        return WIRE_GROUP
    return registrable_domain(passage.url) or passage.url


def side_strength(
    scored: Sequence[ScoredPassage], *, refutation: bool, claim_kind: ClaimKind = "general"
) -> int:
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

    **A single deciding passage must be verified.** The primary-source and
    fact-check paths each let *one* passage reach strength 2 alone, so that one
    passage's text must be confirmed to really appear on the page it names
    (:attr:`~app.pipeline.types.Passage.provenance_verified`) before it is
    trusted that far — a passage nothing has checked should inform a claim, not
    alone decide it. The two-or-more-independent-sources path is not gated the
    same way: the corroboration between two passages is itself the safeguard,
    and today's web-search provider — the pipeline's main source of ordinary
    reporting — never sets this field at all, so gating that path on it would
    make ``supported`` unreachable by ordinary corroborating reports.
    """
    if not scored:
        return 0
    passages = [item.passage for item in scored]
    if any(
        is_primary(passage, claim_kind=claim_kind) and passage.provenance_verified
        for passage in passages
    ):
        return 2
    if refutation and any(
        passage.origin == "factcheck" and passage.provenance_verified for passage in passages
    ):
        return 2
    if len({source_group(passage) for passage in passages}) >= 2:
        return 2
    return 1


def detect_signals(
    scored: Sequence[ScoredPassage], *, claim_kind: ClaimKind = "general"
) -> list[str]:
    """Find true-but-misleading signals in the passages, as reader-facing clauses.

    Three signals, each derived from something actually present in the retrieved
    material rather than from a model's impression of it:

    * **tiny sample** — a stated sample size under :data:`SMALL_SAMPLE_MAX`, or a
      phrase that says the sample was self-selected (:data:`_SAMPLE_PHRASES`);
    * **outdated** — the newest refuting or neutral passage is at least
      :data:`OUTDATED_GAP_DAYS` newer than the newest supporting one, so the
      support may have been overtaken;
    * **rated "partly true"** — a fact-checker's own rating already says the
      claim needs context, reported in Re-Vera's own words and attributed to
      the fact-checker by name (:data:`_RATING_SIGNAL_PHRASES`) rather than
      quoting the rating's own text — the rating is attacker-controlled up to
      120 characters and printing it back verbatim is how a foreign verdict
      word ("FALSE", "pants on fire") ends up in Re-Vera's own sentence (a
      redteam finding).

    Returned as clauses that can be dropped into a sentence after "but", in a
    fixed order so the same evidence always produces the same wording. An empty
    list means no signal fired, not that none exists: cherry-picking that shows
    only in the prose is what the judge is for, and it reaches the reader through
    :func:`reconcile` weakening a ``supported`` verdict to ``missing_context``.
    """
    signals: list[str] = []
    credible = [item for item in scored if is_credible(item.passage, claim_kind=claim_kind)]

    if any(_has_small_sample(item.passage.text) for item in credible):
        signals.append("it rests on a very small sample")

    if _support_is_outdated(credible):
        signals.append("more recent material has since been published")

    for item in credible:
        rating = item.passage.rating
        phrase = _mixed_rating_phrase(rating) if rating else None
        if phrase:
            signals.append(f"{_clean_outlet(item.passage)} {phrase}")
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
    claim_kind = claim.kind
    usable = _usable(claim, scored, article_url)
    supporting = [item for item in usable if item.stance is Stance.supports]
    refuting = [item for item in usable if item.stance is Stance.refutes]
    signals = detect_signals(usable, claim_kind=claim_kind)

    support_strength = side_strength(supporting, refutation=False, claim_kind=claim_kind)
    refute_strength = side_strength(refuting, refutation=True, claim_kind=claim_kind)

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
        # A genuine conflict is specifically the rules and the judge pointing
        # opposite ways at equal (full) strength. Every other route to this
        # branch — the judge returning something outside the four verdicts, or
        # simply abstaining further than the rules — is the judge saying
        # nothing usable, not the sources disagreeing, and must not be
        # reported to the reader as if they did (a redteam finding).
        genuinely_opposed = (
            judgement.verdict in ALLOWED_VERDICTS
            and judgement.verdict != rule_verdict
            and VERDICT_STRENGTH[judgement.verdict] == 2
            and VERDICT_STRENGTH[rule_verdict] == 2
        )
        reason = _CONFLICT if genuinely_opposed else _INSUFFICIENT

    if verdict != UNVERIFIABLE and not _citations_verified(judgement, usable):
        verdict, reason = UNVERIFIABLE, _UNCITED

    sources = [] if verdict == UNVERIFIABLE else _sources(relied, settings, claim_kind=claim_kind)
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
        confidence = _confidence(verdict, relied, opposing, judgement, claim_kind=claim_kind)
        evidence = _judge_evidence(judgement, verdict, sources) or _composed_evidence(
            verdict, sources, signals
        )

    trail = build_trail(
        verdict=verdict,
        relied=relied,
        usable=usable,
        article_url=article_url,
        claim_kind=claim_kind,
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
    """The four rules, in the order the brief states them — with the tie checked
    first (module docstring).

    Returns the verdict, the passages it rests on, and — for an
    ``unverifiable`` — why, which decides the wording of the explanation that
    verdict must ship.

    Once the tie is out of the way, every branch below is checked knowing the
    two sides are *not* both at full strength, so "refute is strong" and
    "support is strong" cannot both be true past this point — the comparisons
    the brief's prose describes (`refute_strength > support_strength`, and the
    reverse) hold automatically and do not need to be spelled out again.

    ``missing_context`` requires supporting evidence to actually be strong
    (``support_strength == 2``), matching :func:`side_strength`'s own claim
    that weak evidence "is never enough for supported or contradicted on its
    own" — a signal does not get to lower that bar (a redteam finding: the old
    code let a single uncorroborated page reach a decided verdict as soon as a
    signal was attached to it). The passages relied on for a
    ``missing_context`` verdict always include any refuting evidence found,
    not only the supporting side: the chip the reader most needs to see is the
    one that pushes back, and dropping it silently is worse than showing it
    alongside the "but" (a redteam finding).
    """
    if support_strength == 2 and refute_strength == 2:
        return UNVERIFIABLE, [], _CONFLICT

    if refute_strength == 2:
        return Verdict.contradicted.value, refuting, ""

    if signals and supporting and support_strength == 2:
        return (
            Verdict.missing_context.value,
            supporting + refuting + _signal_context(usable),
            "",
        )

    if support_strength == 2:
        return Verdict.supported.value, supporting, ""

    if not usable:
        return UNVERIFIABLE, [], _UNCHECKED
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
        and (
            _has_small_sample(item.passage.text)
            or _mixed_rating_phrase(item.passage.rating or "") is not None
        )
    ]


def _usable(
    claim: ExtractedClaim, scored: Sequence[ScoredPassage], article_url: str
) -> list[ScoredPassage]:
    """Drop what cannot be a source: bad URLs, aggregator copy, and the article's
    own site.

    A passage from the very page being checked, or from another page on the
    same publisher's site, is not independent evidence about it — retrieval
    can land back on the article through a search result or a syndicated
    reprint, and a citation the article's own author picked can just as
    easily point at a sibling page on the same domain. Citing an article to
    itself, or to its own newsroom, is the most confident form of no evidence
    at all (a redteam finding: an article linking to another page on its own
    site reached ``is_primary`` on origin alone and the claim came back
    ``supported``).

    Two checks, deliberately overlapping: the exact page
    (:func:`~app.pipeline.providers.base.same_page`, which survives a tracking
    parameter, a ``www.`` prefix, a scheme change or a fragment — none of
    which used to be true, when this guard compared raw strings) and the whole
    site (:func:`~app.pipeline.providers.base.registrable_domain`). Everything
    kept here has also passed :func:`is_credible`, so every later count is a
    count of credible, independent sources.
    """
    article_domain = registrable_domain(article_url)
    kept: list[ScoredPassage] = []
    for item in scored:
        passage = item.passage
        if not is_credible(passage, claim_kind=claim.kind):
            continue
        if same_page(passage.url, article_url):
            continue
        if article_domain and registrable_domain(passage.url) == article_domain:
            continue
        kept.append(item)
    return kept


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


def _sources(
    relied: Sequence[ScoredPassage], settings: Settings, *, claim_kind: ClaimKind = "general"
) -> list[Source]:
    """Build the reader-facing source chips from the passages actually relied on.

    Ordered primary first, then by :data:`~app.pipeline.retrieve.ORIGIN_PRIORITY`
    — the reader should meet the strongest thing we have first — and
    de-duplicated by :func:`~app.pipeline.providers.base.url_key` (the same
    "same page" identity used everywhere else in the pipeline, so a tracking
    parameter cannot smuggle a source chip past this de-dup twice). Capped at
    ``settings.max_passages_per_claim`` so a caller handing this stage more
    passages than retrieval's cap cannot put twenty chips on one card.

    ``date`` is the empty string when the source stated none. The schema types it
    as a required string, and a blank is the one honest value available: inventing
    a date would put a fabricated fact on the chip, and dropping the source would
    throw away real evidence over a missing field.
    """
    cleaned = _outlets(relied)
    ordered = sorted(
        cleaned,
        key=lambda item: (
            0 if is_primary(item.passage, claim_kind=claim_kind) else 1,
            ORIGIN_PRIORITY.get(item.passage.origin, len(ORIGIN_PRIORITY)),
        ),
    )
    sources: list[Source] = []
    seen: set[str] = set()
    for item in ordered:
        key = url_key(item.passage.url)
        # An empty key means the URL had no recognisable host at all; two such
        # URLs are not evidence they are the same page (mirrors `same_page`'s
        # own rule), so an empty key is never treated as a duplicate.
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        sources.append(
            Source(
                url=item.passage.url,
                outlet=item.passage.outlet,
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
    *,
    claim_kind: ClaimKind = "general",
) -> Confidence:
    """Pick ``low``/``medium``/``high`` from the shape of the evidence, then cap it.

    The base level counts what is actually there — a *verified* primary
    source (see :func:`side_strength`'s docstring for why an unverified one
    does not get the bonus), and how many independent groups agree:

    ======================================  ========
    Evidence                                Base
    ======================================  ========
    verified primary + another independent  ``high``
    three or more independent sources       ``high``
    a verified primary, or two independent  ``medium``
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
    primary = any(
        is_primary(item.passage, claim_kind=claim_kind) and item.passage.provenance_verified
        for item in relied
    )

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

    All of the following, and every one of them about honesty rather than
    style:

    * the final verdict is the verdict the judge wrote about — a sentence
      arguing for ``supported`` under a Missing context badge would be the
      product contradicting itself;
    * after sanitising (control characters and newlines stripped, whitespace
      collapsed), it is one plain sentence's worth of text
      (:data:`MAX_EVIDENCE_CHARS`) and not empty;
    * it actually names one of the sources we kept — a real, word-boundary
      match against an outlet's name, not a raw substring test that a short
      common outlet name (or word) could pass by accident naming nobody (a
      redteam finding: ``span_occurs_in`` on the raw outlet let a sentence
      that named no source through whenever the outlet happened to be a
      common enough string);
    * it does not reproduce a foreign fact-checker's own verdict word — the
      retrieved passage for a ClaimReview genuinely contains its rating, and a
      judge quoting or paraphrasing that passage can carry "FALSE" or "pants
      on fire" straight into its own sentence without fabricating anything;
      that sentence must still never reach a reader looking like it is
      Re-Vera's own judgement (:data:`_FOREIGN_VERDICT_CAPS`,
      :data:`_FOREIGN_VERDICT_PHRASES` — a redteam finding).

    ``None`` means "compose one instead". Nothing is edited or patched up: a
    sentence that fails any of these is not a sentence to repair.
    """
    if verdict == UNVERIFIABLE or judgement.verdict != verdict:
        return None
    text = _sanitize(judgement.evidence, limit=MAX_EVIDENCE_CHARS + 1)
    if not text or len(text) > MAX_EVIDENCE_CHARS:
        return None
    if _leaks_foreign_verdict(text):
        return None
    if not any(_mentions_outlet(text, source.outlet) for source in sources):
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

    Only the sources that actually point the verdict's way are named, and if
    none of the sources we kept does — the per-claim cap can strand only a
    refuting chip when a claim is ``missing_context``, since ``relied`` always
    includes any refuting evidence found — the sentence names **no one's
    stance**: describing a refuting source as if it backed the claim (or the
    reverse) is worse than a sentence that names no outlet at all (a redteam
    finding). The chips still carry every source, each with its own stance.
    """
    wanted = _SENTENCE_STANCE[verdict]
    matching = [source.outlet for source in sources if source.stance is wanted]

    if not matching:
        logger.warning("aggregate: composed evidence found no %s-stance source", wanted.value)
        if verdict == Verdict.missing_context.value:
            clause = signals[0] if signals else "important context is missing"
            return f"The evidence found does not settle this claim, and {clause}."
        return "The evidence found does not clearly settle this claim."

    subject = _prose_join(matching)
    plural = len(matching) != 1

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

    The "nothing retrieved" sentence claims only that no evidence was found —
    never that fact-check databases and the web were specifically searched.
    This stage has no way to know whether every provider actually ran (a
    provider can be unconfigured, or every provider call can have failed) or
    genuinely came back empty, and asserting a search that may not have
    happened is exactly the kind of claim rule 2 forbids (a redteam finding).
    A future retrieval-side signal for *why* nothing was found (not
    configured vs. found nothing vs. every call failed) would let this
    sentence be more specific without becoming dishonest; today it is
    deliberately generic instead.
    """
    names = _unique(item.passage.outlet for item in _outlets(usable))
    if reason == _UNCHECKED or not names:
        return "No evidence was found that addresses this claim."
    subject = _prose_join(names)
    if reason == _CONFLICT:
        if len(names) == 1:
            return (
                f"{subject} published evidence on both sides of this claim, "
                "so it is left unresolved."
            )
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
    claim_kind: ClaimKind = "general",
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
    flags and origins only, and every outlet name is sanitised before it is
    joined into a note (:func:`_outlets`) — the same third-party text that
    reaches a source chip reaches a trail note, and both must be safe. A
    trail is a chain of custody, and an invented (or unsanitised) link in it
    is worse than a short chain.
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
        if not is_primary(item.passage, claim_kind=claim_kind) or item.passage.origin == "factcheck"
    )
    if independent:
        trail.append(TrailNode(label="Independent reports", note=_join_outlets(independent)))

    primary = next(
        (item.passage for item in relied if is_primary(item.passage, claim_kind=claim_kind)),
        None,
    )
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
    outlet = _clean_outlet(passage)
    human = _human_date(passage.date)
    return f"{outlet}, {human}" if human else outlet


# ---------------------------------------------------------------- small helpers


def _sanitize(value: str | None, *, limit: int) -> str:
    """Third-party text made safe for Re-Vera's own copy.

    Applied to every outlet name, ClaimReview rating and judge sentence before
    it reaches a reader — none of that text is Re-Vera's own (M6/M18/M19, a
    redteam finding). Control characters are stripped, whitespace (including
    newlines, so a passage cannot break a card's layout) is collapsed to
    single spaces, and the result is bounded to ``limit`` characters. Cutting
    mid-word is accepted, the same trade-off retrieval's own
    ``clean_text`` makes: the alternative is a sentence-boundary search that
    would sometimes drop the part that mattered.
    """
    if not isinstance(value, str):
        return ""
    stripped = _CONTROL_CHARS.sub(" ", value)
    collapsed = " ".join(stripped.split())
    return collapsed[:limit].strip()


def _clean_outlet(passage: Passage) -> str:
    """The outlet name to show a reader: sanitised third-party text, or the
    domain when a provider left the name blank. Never invents a masthead."""
    return _sanitize(passage.outlet, limit=_OUTLET_MAX_CHARS) or outlet_from_url(passage.url)


def _mentions_outlet(text: str, outlet: str) -> bool:
    """True when ``outlet`` genuinely appears in ``text`` as itself — a
    word-boundary match on the normalised forms, not the raw substring test
    that let a short, common outlet name pass by naming nobody in particular
    (a redteam finding). An outlet name shorter than two characters is never
    treated as "mentioned": nothing that short can uniquely name a source."""
    name = normalize_for_match(outlet)
    if len(name) < 2:
        return False
    pattern = r"\b" + re.escape(name) + r"\b"
    return re.search(pattern, normalize_for_match(text)) is not None


def _leaks_foreign_verdict(text: str) -> bool:
    """True when ``text`` carries a fact-checker's own verdict word rather than
    Re-Vera's four — a shouted TRUE/FALSE/FAKE/HOAX, or an unambiguous rating
    phrase like "pants on fire" (:data:`_FOREIGN_VERDICT_CAPS`,
    :data:`_FOREIGN_VERDICT_PHRASES`). Deliberately does not flag the ordinary,
    lower-case use of "true" or "false" inside normal explanatory prose —
    only the shouted or unambiguous forms a real rating actually takes."""
    if _FOREIGN_VERDICT_CAPS.search(text):
        return True
    folded = normalize_for_match(text)
    return any(phrase in folded for phrase in _FOREIGN_VERDICT_PHRASES)


def _outlets(items: Iterable[ScoredPassage]) -> list[ScoredPassage]:
    """``items`` with every outlet sanitised (:func:`_clean_outlet`) — control
    characters stripped, length bounded, and a blank name filled from the
    domain. Never invents a masthead; always makes the name safe to print."""
    result: list[ScoredPassage] = []
    for item in items:
        result.append(
            ScoredPassage(
                passage=_renamed(item.passage, _clean_outlet(item.passage)),
                stance=item.stance,
                rationale_quote=item.rationale_quote,
            )
        )
    return result


def _renamed(passage: Passage, outlet: str) -> Passage:
    """``passage`` with ``outlet`` replaced. Frozen dataclass, so a new one.

    Carries ``provenance_verified`` through unchanged — a passage's outlet
    display name says nothing about whether its text was confirmed against
    the page it names, and silently resetting that bit here would be a bug
    the moment anything downstream of :func:`_outlets` relies on it (it does
    not, today: every call site reads outlet names for display, never
    verification — but the field is real metadata about this exact passage
    and rebuilding it without carrying every field forward is the kind of
    quiet loss that is cheap to avoid now and easy to miss later)."""
    return Passage(
        text=passage.text,
        url=passage.url,
        outlet=outlet,
        date=passage.date,
        wire=passage.wire,
        origin=passage.origin,
        rating=passage.rating,
        provenance_verified=passage.provenance_verified,
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
    """A URL folded for comparison: lower-cased, without a trailing slash.

    **Retained only as the subject of a pinning regression test** in
    ``tests/test_types.py`` (search it for ``_url_key``), which demonstrates
    exactly the gap :func:`~app.pipeline.providers.base.url_key`
    closes by pointing at this function. Nothing in this module calls it any
    more — :func:`_usable` and :func:`_sources` use the canonical
    ``url_key``/``same_page`` from ``providers.base`` instead (a redteam
    finding: this function, still in use, missed a tracking parameter, a
    ``www.`` prefix, a scheme change and a trailing slash, any of which let
    the article being checked "corroborate" itself). Do not call this from
    new code; it exists so the historical bug stays demonstrable.
    """
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


def _mixed_rating_phrase(rating: str) -> str | None:
    """The Re-Vera-authored phrase for a ClaimReview rating that says "true,
    but…" (:data:`_RATING_SIGNAL_PHRASES`), or ``None`` if it says no such
    thing. ``rating`` is sanitised and length-bounded before it is even
    inspected, so neither the match nor (via :func:`detect_signals`) anything
    built from it can carry a control character or run past
    :data:`_RATING_MAX_CHARS` — though what actually reaches a reader is
    always the mapped phrase, never the rating's own words."""
    folded = normalize_for_match(_sanitize(rating, limit=_RATING_MAX_CHARS))
    for fragment, phrase in _RATING_SIGNAL_PHRASES:
        if fragment in folded:
            return phrase
    return None


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
