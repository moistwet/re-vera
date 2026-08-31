"""Stage 4 — the judge: one verdict, from the retrieved passages and nothing else.

This is the stage the milestone's most important correctness property lives in.
``CLAUDE.md`` rule 2: *the LLM judge may only use retrieved passages, never its
own knowledge*. A prompt can ask for that; it cannot deliver it. What delivers it
is here, in code: the judge must quote the spans it relied on, and every one of
those spans is searched for in the passages it was actually shown. A span that is
not there is a citation the model invented, and an invented citation discards the
whole answer — verdict, confidence, sentence and all — in favour of
``unverifiable``.

That downgrade is deliberately total rather than partial. A model that fabricated
one quotation has told us the reasoning behind this verdict is not the reasoning
it reported, and there is no way to keep the parts of such an answer that happen
to look sound. Losing a verdict costs a reader one highlight; keeping a confident
verdict built on words nobody published costs them the thing Re-Vera exists to
give them.

What the judge is shown, and what it is not
-------------------------------------------
It sees the claim, and each passage's **text** and **outlet name**. It does not
see URLs, dates, or the stance stage's scores.

The outlet name is a deviation from ``app.pipeline.types.Passage``'s "text is the
only field the model sees", and it is here for one reason: rule 2 also requires
the evidence sentence to *name the sources*, which cannot be done by a writer who
does not know them. The prompt is explicit that the name is a label and never a
reason to believe a passage, and the name is sanitised before it goes anywhere
near the fence (:func:`source_label`) because an outlet string is itself derived
from an untrusted URL.

The stance scores are withheld on purpose. Counting how many passages support or
refute a claim is stage 5's job, done by rules, deterministically, and it would
be strictly worse done by a model reading its own upstream labels. Withholding
them keeps the judge's read of the passages independent of the stance stage's,
which gives aggregation two signals instead of one echo — and saves tokens.

The verification rule, exactly
------------------------------
A cited span is accepted when, after
:func:`~app.pipeline.types.normalize_for_match` (NFKC, curly quotes and dashes
folded to ASCII, whitespace runs collapsed, casefolded), it occurs as a substring
of **one** passage's normalised text. That is forgiving about typography, which a
model changes routinely when it retypes a passage, and strict about everything
else: no stemming, no fuzzy distance, no near-miss allowance. Spans are checked
against each passage separately rather than against their concatenation, so a
span stitched from the end of one passage and the start of another matches
nothing. A span shorter than :data:`MIN_CITED_SPAN_CHARS` is rejected outright:
"the" occurs in every passage ever published and citing it is not citing
anything — and that floor is measured on the *normalised* span, not the raw
one, so padding a short fragment with extra blank space cannot buy it past the
floor only to have the padding vanish at match time. :func:`verified_span`,
imported from :mod:`app.pipeline.stance` rather than reimplemented here, is
both checks in one place, so stage 3's citation floor and this one cannot
quietly drift apart.

Failure policy
--------------
Every failure that is *about this claim* ends at ``unverifiable`` with a
code-written explanation naming what was searched — an unusable answer
(:class:`~app.llm.LLMInvalidOutput`), an unknown verdict, an impossible
confidence, a missing sentence, a fabricated span. A failure that is *not* about
this claim — no answer at all (:class:`~app.llm.LLMBadRequest`, a bad key or an
unusable model, and :class:`~app.llm.LLMUnavailable`, a provider outage) —
propagates, because it is a fact about the deployment and the caller, not this
stage, decides what a reader is told about it. Stage 3 draws the same line for
the same reason.

The model's own sentence is never kept on a downgrade. If a judge claimed CNA
confirmed something and then could not show us where, showing a reader "CNA
confirms this" under an ``unverifiable`` badge would print the fabrication we
just caught.

Privacy
-------
No log line here carries the claim quote (article text), a passage body, a source
URL or the evidence sentence (which is prose about passage text). What is logged
is the claim id, counts, verdicts, the model and the prompt version
(``CLAUDE.md`` rule 6).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence

from pydantic import BaseModel

from app.config import Settings
from app.invariants import ALLOWED_CONFIDENCES, ALLOWED_VERDICTS, UNVERIFIABLE
from app.llm import LLMClient, LLMInvalidOutput, load_prompt
from app.pipeline.providers.base import MAX_PASSAGE_CHARS
from app.pipeline.stance import MIN_CITED_SPAN_CHARS, verified_span
from app.pipeline.types import (
    ExtractedClaim,
    Judgement,
    ScoredPassage,
    normalize_for_match,
)

logger = logging.getLogger(__name__)

__all__ = [
    "CLAIM_CLOSE",
    "CLAIM_OPEN",
    "MAX_EVIDENCE_CHARS",
    "MAX_OUTLET_CHARS",
    "MIN_CITED_SPAN_CHARS",
    "NAMED_SOURCE_LIMIT",
    "PASSAGE_CLOSE",
    "PROMPT_NAME",
    "UNNAMED_SOURCE",
    "JudgeResponse",
    "build_user_content",
    "judge_claim",
    "passage_open",
    "source_label",
]

PROMPT_NAME = "judge"
"""The prompt file this stage loads: ``app/prompts/judge.md``."""

CLAIM_OPEN = "<claim>"
CLAIM_CLOSE = "</claim>"
PASSAGE_CLOSE = "</passage>"
"""The fences :func:`build_user_content` puts around the untrusted content.

Passages open with ``<passage index="N" source="...">`` (:func:`passage_open`).
As in stages 1 and 3, a marker in a message is a signpost and not a wall: a
passage containing ``</passage>`` closes its own fence and nothing here can stop
it. The fence is the last layer of the injection defence. The layers that hold
are the prompt's instruction to treat all of this as data, the client's
separation of roles, and the span verification below — and only the last of those
is a guarantee rather than a request.

Spelled out here rather than imported from :mod:`app.pipeline.stance`, even
though the two currently agree: each stage's fences belong to its own prompt
file, and a stage that changes its markers must bump its own prompt version, not
another stage's.
"""

# MIN_CITED_SPAN_CHARS and verified_span are imported from app.pipeline.stance,
# not redefined here — the same floor and the same normalised-substring check
# have to hold in both stages, and a constant copied into two files is a
# constant that can silently drift when only one of the two is edited. See
# app.pipeline.stance.MIN_CITED_SPAN_CHARS and app.pipeline.stance.verified_span
# for the number and the reasoning; re-exported here (see __all__) because this
# module's own docstrings, and code outside it, refer to
# ``judge.MIN_CITED_SPAN_CHARS`` as this stage's citation floor.

MAX_EVIDENCE_CHARS = 320
"""Longest evidence sentence accepted before the answer is treated as unusable.

Rule 2 asks for *a one-sentence plain-language summary*, and the claim card shows
it to a teenager on a school laptop; 320 characters is already two or three lines
there. An answer that runs past it is not the thing the rule asks for, and it is
also the shape a successful injection takes — a passage's own paragraphs
reproduced into the reader-facing sentence. Truncating would leave a sentence cut
off mid-word under a confident badge, so the whole answer is discarded instead.
"""

MAX_OUTLET_CHARS = 60
"""Longest outlet name carried into the prompt or into a sentence.

Outlet strings are derived from retrieved URLs and provider payloads, so they are
untrusted too — a 4,000-character "outlet" is a way to push text into a prompt
without ever writing a passage.
"""

NAMED_SOURCE_LIMIT = 3
"""How many outlets a code-written explanation names before it says "and N more".

Keeps the sentence readable at the six passages a claim may carry, without
hiding how much was actually searched: the count is always stated.
"""

UNNAMED_SOURCE = "an unnamed source"
"""Stands in when a passage's outlet is empty or is nothing but punctuation."""

_WHITESPACE = re.compile(r"\s+")
_FENCE_UNSAFE = re.compile(r'["<>\n\r]')
"""Characters removed from an outlet name before it becomes a fence attribute.

Not a security boundary — see :data:`CLAIM_OPEN` — but there is no reason to let
a source name close its own quoted attribute for free.
"""


class JudgeResponse(BaseModel):
    """The judge's answer, as the model returns it and before anything believes it.

    ``verdict`` and ``confidence`` are bare strings rather than
    :class:`~app.schema_models.Verdict` and
    :class:`~app.schema_models.Confidence` on purpose. Typing them as the enums
    would make an out-of-range value a parse failure inside
    :class:`~app.llm.LLMClient`, which is a *transport* problem; here it is a
    *product* problem with a specific right answer — downgrade this claim to
    ``unverifiable`` — and this stage cannot give that answer if the value never
    reaches it. It also keeps the enum out of the request schema, where a model
    can be nudged into picking a value it has no evidence for simply because the
    schema offered it.

    Four fields, all of them used (``CLAUDE.md`` cost rules): every property is
    tokens in the request schema and in the reply, once per claim per article.
    """

    verdict: str
    confidence: str | None
    evidence: str
    cited_spans: list[str]


def source_label(outlet: str) -> str:
    """Clean one outlet name for use as a fence attribute and in a sentence.

    Whitespace collapsed, fence-breaking characters dropped, length capped at
    :data:`MAX_OUTLET_CHARS`, and :data:`UNNAMED_SOURCE` when nothing usable is
    left. The result is shown to the model and can end up in front of a reader,
    so it is the one piece of provider metadata this stage sanitises rather than
    passes through.
    """
    cleaned = _FENCE_UNSAFE.sub(" ", outlet)
    cleaned = _WHITESPACE.sub(" ", cleaned).strip()
    return cleaned[:MAX_OUTLET_CHARS].strip() or UNNAMED_SOURCE


def passage_open(index: int, source: str) -> str:
    """The opening fence for the passage numbered ``index`` (1-based)."""
    return f'<passage index="{index}" source="{source}">'


def build_user_content(claim: ExtractedClaim, shown: Sequence[tuple[str, str]]) -> str:
    """Fence the claim and the numbered passages into one user message.

    ``shown`` is ``(source label, passage text)`` in order, **as the model will
    see them**: the passage at position *i* is numbered ``i + 1``, and its text is
    the only text a cited span may come from. Nothing is summarised, reordered or
    annotated on the way in — no stance labels, no URLs, no dates.
    """
    blocks = [f"{CLAIM_OPEN}\n{claim.quote}\n{CLAIM_CLOSE}"]
    blocks.extend(
        f"{passage_open(number, source)}\n{text}\n{PASSAGE_CLOSE}"
        for number, (source, text) in enumerate(shown, start=1)
    )
    return "\n\n".join(blocks)


async def judge_claim(
    claim: ExtractedClaim,
    scored: Sequence[ScoredPassage],
    *,
    client: LLMClient,
    settings: Settings,
) -> Judgement:
    """Judge one claim from its scored passages, and verify the answer.

    Returns a :class:`~app.pipeline.types.Judgement` that has already been
    checked: ``verdict`` is one of the four, ``confidence`` is one of the three
    or ``None``, ``confidence`` is ``None`` exactly when the verdict is
    ``unverifiable``, ``evidence`` is a non-empty single-line sentence, and every
    string in ``cited_spans`` has been found in a passage the model was shown.
    Stage 5 may still override the verdict with its rules; it never has to
    re-check any of that.

    An empty ``scored`` returns ``unverifiable`` **without a model call** — a
    claim with no evidence is already answered, and paying to be told so would be
    paying for nothing (``CLAUDE.md`` cost rules).

    Raises :class:`~app.llm.LLMBadRequest` or :class:`~app.llm.LLMUnavailable`
    when the provider never answered. Every other failure — including a
    fabricated citation — comes back as an ``unverifiable`` judgement whose
    evidence sentence names what was searched.
    """
    if not scored:
        logger.info("judge: claim=%s had no passages; unverifiable without a call", claim.id)
        return _unverifiable(_searched_nothing())

    if len(scored) > settings.max_passages_per_claim:
        # Retrieval owns the cap. Judging everything it hands over beats silently
        # dropping evidence, but a batch over the cap means one of the two is
        # wrong and the bill lands here.
        logger.warning(
            "judge: claim=%s was given %d passages but max_passages_per_claim is %d; "
            "judging all of them",
            claim.id,
            len(scored),
            settings.max_passages_per_claim,
        )

    # What the model is shown, and therefore the only text a cited span may come
    # from. Retrieval already caps passage length; re-applying its constant here
    # guards against a passage arriving from somewhere else (the eval harness, a
    # provider written later), not against retrieval.
    shown = [
        (source_label(item.passage.outlet), item.passage.text[:MAX_PASSAGE_CHARS])
        for item in scored
    ]
    sources = [source for source, _ in shown]
    texts = [text for _, text in shown]

    prompt = load_prompt(PROMPT_NAME)
    try:
        response, usage = await client.structured(
            model=settings.openai_model_judge,
            prompt=prompt,
            user_content=build_user_content(claim, shown),
            schema=JudgeResponse,
        )
    except LLMInvalidOutput as exc:
        # `str(exc)` only, deliberately no `exc_info`: the chained cause of an
        # invalid answer is a pydantic ValidationError whose message quotes the
        # offending values — here, spans of passage text and a sentence about
        # them — back at you.
        logger.warning(
            "judge: claim=%s got an unusable answer from prompt=%s@v%s; unverifiable (%s)",
            claim.id,
            prompt.name,
            prompt.version,
            exc,
        )
        return _unverifiable(_searched_and_found_nothing(sources))

    judgement = _verify(claim.id, response, texts, sources)
    logger.info(
        "judge: claim=%s verdict=%s confidence=%s spans=%d passages=%d "
        "(model=%s prompt=%s@v%s completion_tokens=%d)",
        claim.id,
        judgement.verdict,
        judgement.confidence,
        len(judgement.cited_spans),
        len(shown),
        usage.model,
        prompt.name,
        usage.prompt_version,
        usage.completion_tokens,
    )
    return judgement


def _verify(
    claim_id: str,
    response: JudgeResponse,
    texts: Sequence[str],
    sources: Sequence[str],
) -> Judgement:
    """Check the model's answer against the passages, or downgrade it.

    The checks, in order, and every one of them a downgrade to ``unverifiable``
    rather than a repair:

    1. **the verdict is one of the four** — case and surrounding space are
       normalised first, because ``"Supported"`` is the same answer badly typed
       and rejecting it would cost a claim for nothing. Anything that is still
       not one of the four (``"true"``, ``"fake"``, ``"flagged"``, an empty
       string) is not a verdict this product has;
    2. **every cited span is real** — long enough to be a citation, and present
       in one of the passages the model was shown (:func:`_verified_spans`);
    3. **a decided verdict cites something** — ``supported``, ``contradicted``
       and ``missing_context`` each rest on evidence by rule 2, and a decided
       verdict with no cited span rests on nothing anyone can check;
    4. **its confidence is one of the three**, and
    5. **its evidence is one usable sentence** — present, and no longer than
       :data:`MAX_EVIDENCE_CHARS`.

    An ``unverifiable`` answer skips 3-5: it is allowed to cite nothing, its
    confidence is forced to ``None`` whatever the model said (the iff-rule of
    ``CLAUDE.md`` rule 3, enforced here at the boundary rather than left for
    :func:`app.invariants.validate_claim` to reject on the way to the wire), and
    an evidence sentence it did not write is written for it.
    """
    verdict = response.verdict.strip().lower()
    if verdict not in ALLOWED_VERDICTS:
        logger.warning(
            "judge: claim=%s returned a verdict that is not one of the four (%d chars); "
            "downgrading to unverifiable",
            claim_id,
            len(response.verdict),
        )
        return _unverifiable(_searched_and_found_nothing(sources))

    spans = _verified_spans(claim_id, response.cited_spans, texts)
    if spans is None:
        return _unverifiable(_searched_and_found_nothing(sources))

    evidence = _WHITESPACE.sub(" ", response.evidence).strip()

    if verdict == UNVERIFIABLE:
        # A missing or over-long explanation is replaced rather than trimmed:
        # this sentence is the whole of what the reader is told about an
        # abstention, and a sentence cut off mid-word is worse than a plain one.
        usable = evidence and len(evidence) <= MAX_EVIDENCE_CHARS
        return Judgement(
            verdict=UNVERIFIABLE,
            confidence=None,
            evidence=evidence if usable else _searched_and_found_nothing(sources),
            cited_spans=spans,
        )

    confidence = response.confidence.strip().lower() if response.confidence else None
    problem = _decided_verdict_problem(spans, confidence, evidence)
    if problem is not None:
        logger.warning(
            "judge: claim=%s returned %s but %s; downgrading to unverifiable",
            claim_id,
            verdict,
            problem,
        )
        return _unverifiable(_searched_and_found_nothing(sources))

    return Judgement(
        verdict=verdict, confidence=confidence, evidence=evidence, cited_spans=spans
    )


def _decided_verdict_problem(
    spans: Sequence[str], confidence: str | None, evidence: str
) -> str | None:
    """Why a ``supported``/``contradicted``/``missing_context`` answer is unusable, or None.

    A phrase for the log line, never containing model text: the reasons are about
    the *shape* of the answer, and quoting the answer to explain it would put
    passage prose in the logs.
    """
    if not spans:
        return "cited no passage"
    if confidence is None:
        return "gave no confidence, which only an unverifiable verdict may do"
    if confidence not in ALLOWED_CONFIDENCES:
        return "gave a confidence that is not low, medium or high"
    if not evidence:
        return "gave no evidence sentence"
    if len(evidence) > MAX_EVIDENCE_CHARS:
        return f"gave a {len(evidence)}-character evidence sentence, which is not one sentence"
    return None


def _verified_spans(
    claim_id: str, cited: Sequence[str], texts: Sequence[str]
) -> list[str] | None:
    """Return the cited spans if **all** of them are real, else ``None``.

    All or nothing on purpose. Keeping the spans that happen to check out would
    mean a model can fabricate freely as long as it also quotes one real
    sentence, and the verdict those spans supposedly support was reached with the
    fabricated one in hand.

    Each span goes through :func:`~app.pipeline.stance.verified_span` — the same
    floor-then-occurrence check stage 3 runs on ``rationale_quote`` — checked
    against ``texts`` (the passages this claim was actually shown; never a wider
    or narrower set). The span kept is the model's own string, not the matching
    text cut out of the passage: matching is forgiving about typography, the two
    differ only in those ways, and keeping the model's wording means every later
    check sees the string that passed this one.
    """
    spans: list[str] = []
    for raw in cited:
        span = raw.strip()
        verified = verified_span(span, list(texts))
        if verified is None:
            # Never log the span: it is either passage text or something the
            # model invented, and neither belongs in a log line. The two
            # messages below share one check (verified_span); the length
            # computed for the message is normalize_for_match's output, the
            # same string the floor and the match both already use, not a
            # second implementation of either.
            if len(normalize_for_match(span)) < MIN_CITED_SPAN_CHARS:
                logger.warning(
                    "judge: claim=%s cited a span that normalises to fewer than %d "
                    "characters, under the citation floor; downgrading to unverifiable",
                    claim_id,
                    MIN_CITED_SPAN_CHARS,
                )
            else:
                logger.warning(
                    "judge: claim=%s cited a %d-character span that is in none of the %d "
                    "passages it was shown; downgrading to unverifiable",
                    claim_id,
                    len(span),
                    len(texts),
                )
            return None
        spans.append(verified)
    return spans


def _unverifiable(evidence: str) -> Judgement:
    """The one shape every failure in this stage ends in.

    No confidence and no cited spans: a judgement that could not be verified
    rests on nothing, and saying so is the honest answer. Stage 5 turns this into
    a claim with ``sources: []`` and a provenance trail
    (``docs/decisions.md`` §5).
    """
    return Judgement(verdict=UNVERIFIABLE, confidence=None, evidence=evidence, cited_spans=[])


def _searched_nothing() -> str:
    """The evidence sentence for a claim retrieval found nothing at all for."""
    return (
        "Searched for published reporting and official figures on this claim and found "
        "none, so there was nothing to check it against."
    )


def _searched_and_found_nothing(sources: Sequence[str]) -> str:
    """The evidence sentence for a claim whose passages settled nothing.

    Names what was searched, which is what rule 2 asks an ``unverifiable`` verdict
    to explain, and is true of every path that reaches it: the passages were
    retrieved and read, and nothing in them was *found* — by a model whose
    citations we could verify — to state the claim.
    """
    if not sources:
        return _searched_nothing()
    names = _name_sources(sources)
    subject = "it" if len(sources) == 1 else "them"
    return (
        f"Searched {names}; nothing in {subject} was found to state this, "
        f"so the claim could not be checked."
    )


def _name_sources(sources: Sequence[str]) -> str:
    """List outlet names for a reader: de-duplicated, capped, "and N more"."""
    unique = list(dict.fromkeys(sources))
    if len(unique) > NAMED_SOURCE_LIMIT:
        head = unique[:NAMED_SOURCE_LIMIT]
        return f"{', '.join(head)} and {len(unique) - NAMED_SOURCE_LIMIT} more"
    if len(unique) == 1:
        return unique[0]
    return f"{', '.join(unique[:-1])} and {unique[-1]}"
