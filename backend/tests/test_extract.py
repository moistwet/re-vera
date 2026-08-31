"""Stage 1 — claim extraction, driven entirely offline.

There is no ``OPENAI_API_KEY`` in this repository and no route to any provider,
so every test here replays a hand-written answer through
:class:`~app.llm.ReplayTransport`. That is the project's standing rule (no
network in tests) and, here, the only way to work at all. It also means these
tests prove what *the stage* does with an answer, never what a model would
actually answer — the recordings under ``tests/fixtures/extract/`` are plausible
model output, not captures.

What is covered, and why each one is here rather than left to review:

* **One call per article**, at the configured model, with the prompt in the
  ``system`` role and the fenced article in the ``user`` role. Extraction is one
  of two calls in the pipeline that are not billed per claim, and the cost rules
  put a hard "exactly one" on it.
* **A quote that is not an exact substring is dropped, never repaired.** The
  offsets this stage emits are a promise about which characters milestone 3 will
  highlight; a paraphrase that slipped through would put that highlight over
  words the article never contained.
* **Offsets are computed here**, and a quote occurring twice resolves to its
  first occurrence, deterministically.
* **Ranking, de-duplication and the ``MAX_CLAIMS`` cap**, in that order, because
  everything downstream is billed per claim and one fact stated twice would
  otherwise spend two of a reader's eight.
* **Ids run ``c1 … cN`` in article order** *after* ranking — the order
  ``claims_found`` announces (``docs/decisions.md`` §15).
* **An unusable answer is ``[]``, a provider failure is an exception.** The two
  mean different things to a reader ("nothing to check here" versus "we could
  not check"), so the stage must not flatten them into one.
* **Truncation happens before the call**, and is always a prefix, so the offsets
  mean what ``shared/schema.json`` says they mean.
* **Prompt injection.** An article that closes the fence and issues orders can
  still only produce claims that are exact substrings of itself.
* **Article text never reaches a log line** (``CLAUDE.md`` privacy rule 6).

Every article and every claim in these fixtures is fictional.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from app.config import DEFAULT_MODEL, Settings
from app.llm import (
    LLMBadRequest,
    LLMClient,
    LLMResponse,
    LLMUnavailable,
    ReplayTransport,
    load_prompt,
    load_recorded_response,
)
from app.pipeline.extract import (
    ARTICLE_CLOSE,
    ARTICLE_OPEN,
    MIN_QUOTE_CHARS,
    PROMPT_CANDIDATE_CAP,
    ExtractionResponse,
    extract_claims,
    fence_article,
    truncate_article,
)
from app.pipeline.types import ExtractedClaim, quote_is_exact

from .conftest import build_settings

FIXTURES = Path(__file__).parent / "fixtures"
EXTRACT_FIXTURES = FIXTURES / "extract"
"""Recorded answers and article fixtures; see that directory's README."""


# ---------------------------------------------------------------- helpers


def recorded(name: str) -> LLMResponse:
    """One recorded answer from ``tests/fixtures/extract/<name>.json``."""
    return load_recorded_response(EXTRACT_FIXTURES / f"{name}.json")


def article_text(name: str) -> str:
    """The ``text`` of an article fixture in ``tests/fixtures/extract/``."""
    with (EXTRACT_FIXTURES / f"{name}.json").open(encoding="utf-8") as handle:
        payload = json.load(handle)
    text: str = payload["text"]
    return text


def hawker_text() -> str:
    """The fictional hawker-rents article the whole skeleton runs on."""
    with (FIXTURES / "article.json").open(encoding="utf-8") as handle:
        payload = json.load(handle)
    text: str = payload["text"]
    return text


def make_client(
    outcomes: list[LLMResponse | Exception],
) -> tuple[LLMClient, ReplayTransport]:
    """A client wired to a scripted transport, with backoff collapsed to nothing."""
    transport = ReplayTransport(list(outcomes))
    client = LLMClient(
        api_key="test-key-never-used",
        timeout=5.0,
        max_retries=1,
        transport=transport,
        retry_base_delay=0.0,
    )
    return client, transport


def extract_settings(**overrides: object) -> Settings:
    """Settings for this stage, ignoring any developer's ``backend/.env``."""
    return build_settings(**overrides)


def quotes(claims: list[ExtractedClaim]) -> list[str]:
    """Just the quotes, for readable assertions."""
    return [claim.quote for claim in claims]


# ---------------------------------------------------------------- the call itself


async def test_one_article_costs_exactly_one_call() -> None:
    """The cost rule with the hardest edge: one extraction call per article.

    Everything after this stage is billed per claim and can be capped by
    ``MAX_CLAIMS``; this call is billed once and cannot, which is why the
    article is truncated instead. A second call here would double the fixed
    cost of every check.
    """
    client, transport = make_client([recorded("hawker_claims")])

    await extract_claims(hawker_text(), client=client, settings=extract_settings())

    assert len(transport.calls) == 1


async def test_the_call_uses_the_configured_extraction_model() -> None:
    """Stage 1 reads ``OPENAI_MODEL_EXTRACT``, so one stage can be escalated alone.

    ``docs/decisions.md`` §7: a stage that fails the golden-set eval is answered
    by pointing it at a stronger model in configuration, never by a code change.
    """
    client, transport = make_client([recorded("hawker_claims")])
    settings = extract_settings(openai_model_extract="gpt-4.1-mini")

    await extract_claims(hawker_text(), client=client, settings=settings)

    assert transport.calls[0].model == "gpt-4.1-mini"
    assert transport.calls[0].model != DEFAULT_MODEL, "the override must actually be read"


async def test_the_prompt_and_the_article_travel_in_separate_roles() -> None:
    """The structural half of the injection defence: they are never concatenated.

    The prompt goes out as ``system``; the article — untrusted text written by
    strangers — goes out as ``user``, fenced, and verbatim, because every offset
    this stage reports is an offset into exactly those characters.
    """
    client, transport = make_client([recorded("hawker_claims")])
    text = hawker_text()

    await extract_claims(text, client=client, settings=extract_settings())

    call = transport.calls[0]
    assert call.system == load_prompt("extract").text
    assert call.user == fence_article(text)
    assert text in call.user, "the article is passed through unaltered"
    assert text not in call.system, "the article is never interpolated into the prompt"


async def test_the_response_schema_stays_minimal() -> None:
    """Three fields per claim and no offsets — every property is billed twice.

    Once in the request schema and once in the reply, times however many claims
    the model finds. Offsets are absent because the stage computes them itself
    and would not believe the model's.
    """
    client, transport = make_client([recorded("hawker_claims")])

    await extract_claims(hawker_text(), client=client, settings=extract_settings())

    claim_schema = transport.calls[0].json_schema["$defs"]["_Claim"]
    assert sorted(claim_schema["properties"]) == ["checkworthiness", "kind", "quote"]
    assert "start" not in claim_schema["properties"]
    assert claim_schema["additionalProperties"] is False


# ---------------------------------------------------------------- exact quotes


async def test_every_returned_quote_is_an_exact_substring_at_its_offsets() -> None:
    """The contract milestone 3's anchoring is built on, checked end to end."""
    client, _ = make_client([recorded("hawker_claims")])
    text = hawker_text()

    claims = await extract_claims(text, client=client, settings=extract_settings())

    assert claims
    for claim in claims:
        assert quote_is_exact(claim, text), claim.quote


async def test_a_paraphrased_quote_is_dropped_rather_than_repaired() -> None:
    """Two of these four quotes were re-told rather than copied. They are lost.

    Losing a claim costs a reader one highlight. Repairing the quote — trimming
    to the nearest match, or fuzzy-searching for it — would cost them a
    highlight drawn over words the article never contained, under a verdict
    about words it did.
    """
    client, _ = make_client([recorded("invented_quotes")])
    text = hawker_text()

    claims = await extract_claims(text, client=client, settings=extract_settings())

    assert quotes(claims) == [
        "rise by 40% from 1 January",
        "The last islandwide rent adjustment took effect in 2024",
    ]
    assert "forty per cent" not in " ".join(quotes(claims))
    assert "already shut" not in " ".join(quotes(claims))


async def test_an_answer_of_nothing_but_paraphrase_yields_no_claims() -> None:
    """A well-formed answer whose every quote was invented is still nothing to check."""
    response = LLMResponse(
        content=json.dumps(
            {
                "claims": [
                    {"quote": "rents will climb by two fifths", "kind": "numeric",
                     "checkworthiness": 0.9},
                ]
            }
        ),
        prompt_tokens=10,
        completion_tokens=10,
    )
    client, _ = make_client([response])

    assert await extract_claims(hawker_text(), client=client, settings=extract_settings()) == []


async def test_a_quote_too_short_to_anchor_is_dropped() -> None:
    """A bare figure cannot be found on a page, and is not a claim on its own.

    ``40%`` occurs several times in a story about rents; a highlight placed on
    the first one would be a coin toss dressed up as a citation.
    """
    short = "40%"
    assert len(short) < MIN_QUOTE_CHARS
    response = LLMResponse(
        content=json.dumps(
            {"claims": [{"quote": short, "kind": "numeric", "checkworthiness": 1.0}]}
        ),
        prompt_tokens=10,
        completion_tokens=10,
    )
    client, _ = make_client([response])

    assert await extract_claims(hawker_text(), client=client, settings=extract_settings()) == []


async def test_whitespace_around_a_quote_is_forgiven_but_the_words_are_not() -> None:
    """Stripping the ends changes no words, and what is searched for is still exact."""
    response = LLMResponse(
        content=json.dumps(
            {
                "claims": [
                    {"quote": "\n  rise by 40% from 1 January  \n", "kind": "numeric",
                     "checkworthiness": 0.9},
                ]
            }
        ),
        prompt_tokens=10,
        completion_tokens=10,
    )
    client, _ = make_client([response])
    text = hawker_text()

    claims = await extract_claims(text, client=client, settings=extract_settings())

    assert quotes(claims) == ["rise by 40% from 1 January"]
    assert quote_is_exact(claims[0], text)


async def test_a_repeated_quote_resolves_to_its_first_occurrence() -> None:
    """Deterministic, and honest about what a bare quote can say.

    Nothing in "Rents rose by 4% in 2024" says *which* of the article's two
    identical sentences the model meant. Guessing from context would be
    guessing; asking the model would mean trusting an offset it invented.
    Milestone 3 re-finds the quote on the page with its surrounding context and
    treats these offsets as a hint (``docs/decisions.md`` §12), so first is both
    defensible and free.
    """
    text = article_text("repetition_article")
    assert text.count("Rents rose by 4% in 2024") == 2

    client, _ = make_client([recorded("repeated_quote")])
    claims = await extract_claims(text, client=client, settings=extract_settings())

    assert len(claims) == 1
    assert claims[0].start == text.find("Rents rose by 4% in 2024") == 0
    assert quote_is_exact(claims[0], text)


# ---------------------------------------------------------------- ranking and ids


async def test_the_cap_keeps_the_highest_scoring_claims() -> None:
    """``MAX_CLAIMS`` is the single biggest lever on the cost of one check.

    Seven candidates, three allowed: the three the model rated most worth
    checking survive, and the background detail is what is dropped.
    """
    client, _ = make_client([recorded("hawker_claims")])
    settings = extract_settings(max_claims=3)

    claims = await extract_claims(hawker_text(), client=client, settings=settings)

    assert quotes(claims) == [
        "rise by 40% from 1 January",
        "More than 200 stalls have already closed this year",
        "An official spokesperson was quoted as saying rents "
        "“will not be capped under any circumstances.”",
    ]


async def test_ids_are_article_ordered_after_ranking() -> None:
    """``c1 … cN`` read down the page, whatever order the model ranked them in.

    The recording lists its claims best-first, which is *not* article order — the
    attribution at character 832 outranks the sentence at 445. The ids that come
    out must still follow the page, because they are the ids ``claims_found``
    announces and the rows a client allocates before any claim arrives
    (``docs/decisions.md`` §15).
    """
    client, _ = make_client([recorded("hawker_claims")])

    claims = await extract_claims(hawker_text(), client=client, settings=extract_settings())

    assert [claim.id for claim in claims] == [f"c{n}" for n in range(1, len(claims) + 1)]
    assert [claim.start for claim in claims] == sorted(claim.start for claim in claims)


async def test_ranking_survives_the_cap_and_the_renumbering_together() -> None:
    """The two orderings do not fight: rank to choose, article order to number."""
    client, _ = make_client([recorded("hawker_claims")])
    settings = extract_settings(max_claims=3)

    claims = await extract_claims(hawker_text(), client=client, settings=settings)

    assert [claim.id for claim in claims] == ["c1", "c2", "c3"]
    assert [claim.start for claim in claims] == [60, 144, 832]


async def test_the_claim_kind_survives_untouched() -> None:
    """``kind`` decides how retrieval looks for evidence, so it must pass through."""
    client, _ = make_client([recorded("hawker_claims")])

    claims = await extract_claims(hawker_text(), client=client, settings=extract_settings())

    kinds = {claim.quote: claim.kind for claim in claims}
    assert kinds["rise by 40% from 1 January"] == "numeric"
    assert kinds["they were told of the increase at a briefing last month"] == "attribution"


async def test_an_out_of_range_score_is_clamped_not_fatal() -> None:
    """A mis-scaled score decides a sort order, and nothing else.

    Failing the whole article's extraction over ``95.0`` would trade every claim
    in the story for one number nobody ever reads.
    """
    client, _ = make_client([recorded("out_of_range_score")])

    claims = await extract_claims(hawker_text(), client=client, settings=extract_settings())

    assert len(claims) == 2
    assert all(0.0 <= claim.checkworthiness <= 1.0 for claim in claims)
    assert {claim.checkworthiness for claim in claims} == {0.0, 1.0}


async def test_a_max_claims_of_zero_returns_nothing() -> None:
    """The cap is honoured at its edge rather than wrapping round to "no limit"."""
    client, _ = make_client([recorded("hawker_claims")])

    claims = await extract_claims(
        hawker_text(), client=client, settings=extract_settings(max_claims=0)
    )

    assert claims == []


async def test_a_max_claims_above_the_prompt_cap_says_so(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The prompt asks for at most 12 candidates, so ``MAX_CLAIMS=20`` is a lie.

    Silently capping at the prompt's number would leave someone staring at a
    setting that does nothing. The fix is a prompt edit and a version bump, and
    the warning says so.
    """
    client, _ = make_client([recorded("hawker_claims")])
    settings = extract_settings(max_claims=PROMPT_CANDIDATE_CAP + 8)

    with caplog.at_level(logging.WARNING, logger="app.pipeline.extract"):
        await extract_claims(hawker_text(), client=client, settings=settings)

    assert "app/prompts/extract.md" in caplog.text
    assert str(PROMPT_CANDIDATE_CAP) in caplog.text


# ---------------------------------------------------------------- de-duplication


async def test_overlapping_claims_are_deduplicated_before_the_cap() -> None:
    """One fact quoted as a clause and again as its whole sentence is one claim.

    Two reasons, either sufficient: it would spend two of a reader's eight
    claims on one fact, and milestone 3 would draw two highlights over the same
    words. The higher-scoring of the pair wins.
    """
    client, _ = make_client([recorded("duplicate_fact")])

    claims = await extract_claims(hawker_text(), client=client, settings=extract_settings())

    assert quotes(claims) == [
        "rise by 40% from 1 January",
        "The last islandwide rent adjustment took effect in 2024",
    ]


async def test_near_identical_claims_are_deduplicated() -> None:
    """The same sentence from two places, differing only by a capital letter.

    Both are exact substrings at different offsets, so the overlap rule cannot
    see them; comparing the normalised text can. The higher-scoring one wins,
    and the unrelated third claim is untouched.
    """
    client, _ = make_client([recorded("case_variant_fact")])
    text = article_text("repetition_article")

    claims = await extract_claims(text, client=client, settings=extract_settings())

    assert quotes(claims) == [
        "Rents rose by 4% in 2024",
        "the increase was spread evenly across the year",
    ]


async def test_deduplication_runs_before_the_cap_bites() -> None:
    """Order matters: dedup first, then cap.

    With the cap applied first, the two halves of one fact would fill it and the
    genuinely separate claim would never be reached.
    """
    client, _ = make_client([recorded("duplicate_fact")])
    settings = extract_settings(max_claims=2)

    claims = await extract_claims(hawker_text(), client=client, settings=settings)

    assert len(claims) == 2
    assert "The last islandwide rent adjustment took effect in 2024" in quotes(claims)


# ---------------------------------------------------------------- opinion and prediction


async def test_an_article_of_pure_opinion_yields_no_claims() -> None:
    """Abstaining is a correct answer, not a failure — and it must cost nothing.

    This replays the answer the prompt asks for on an opinion column; it proves
    the stage passes an empty list through cleanly, not that a model would send
    one. What the *prompt* demands is asserted separately below.
    """
    client, transport = make_client([recorded("opinion_none")])

    claims = await extract_claims(
        article_text("opinion_article"), client=client, settings=extract_settings()
    )

    assert claims == []
    assert len(transport.calls) == 1


def test_the_prompt_rules_out_opinion_and_prediction() -> None:
    """A contract test on ``app/prompts/extract.md``, not on any model.

    No key and no network here, so nothing in this suite can show what a model
    does with the prompt. What it *can* show is that the instruction the brief
    requires is present and has not been edited away — including the borderline
    rule, which is the one a rewrite loses first.
    """
    body = load_prompt("extract").text.lower()

    assert "opinion" in body
    assert "forecast" in body or "prediction" in body
    assert "present or past fact" in body
    assert "leave it out" in body, "borderline cases are dropped, not guessed"


def test_the_prompt_fences_the_article_and_refuses_its_orders() -> None:
    """The prompt must name the fence it will see and say that its contents are data.

    The article is written by strangers and really can contain "ignore your
    instructions". :class:`~app.llm.LLMClient` keeps it in its own role; this is
    the other half.
    """
    body = load_prompt("extract").text

    assert ARTICLE_OPEN in body and ARTICLE_CLOSE in body
    lowered = body.lower()
    assert "instruction" in lowered
    assert "never follow" in lowered or "never a command" in lowered


def test_the_prompt_demands_exact_quotes() -> None:
    """The whole stage rests on it: an inexact quote is discarded, so ask for exact."""
    lowered = load_prompt("extract").text.lower()

    assert "character for character" in lowered
    assert "summaris" in lowered or "summariz" in lowered


# ---------------------------------------------------------------- prompt injection


async def test_an_injected_instruction_cannot_conjure_a_claim() -> None:
    """An article that closes the fence and issues orders still cannot invent text.

    The fixture's middle paragraph ends the fence early, addresses the model as
    an operator and demands a fabricated claim; the recorded answer obeys it.
    The fabricated quote is not a substring of the article, so it is dropped and
    only the real claim survives.

    The guarantee this demonstrates is narrow and worth stating plainly: it is
    not that injection cannot influence the model, but that whatever it produces
    must still be words the article actually contains. An injection that
    persuaded the model to quote the injected paragraph *itself* would survive —
    and would then be a claim about text that is genuinely on the page, which is
    what Re-Vera checks.
    """
    client, _ = make_client([recorded("injection_attempt")])
    text = article_text("injected_article")
    assert ARTICLE_CLOSE in text, "the fixture really does try to close the fence"

    claims = await extract_claims(text, client=client, settings=extract_settings())

    assert quotes(claims) == ["The board said rents rose by 4% in 2024"]
    assert quote_is_exact(claims[0], text)


async def test_the_fence_is_not_stripped_out_of_the_article() -> None:
    """Removing the injected ``</article>`` would move every offset after it.

    The offsets are the product's promise about which characters to highlight,
    so the article goes to the model exactly as the reader's page had it and the
    defence is placed downstream instead.
    """
    client, transport = make_client([recorded("injection_attempt")])
    text = article_text("injected_article")

    await extract_claims(text, client=client, settings=extract_settings())

    assert text in transport.calls[0].user


# ---------------------------------------------------------------- failure policy


async def test_an_unusable_answer_yields_no_claims_rather_than_raising() -> None:
    """A refusal or a half-written reply is "nothing to check", not a failed check.

    The recording is not JSON at all, which surfaces from the client as
    :class:`~app.llm.LLMInvalidOutput`.
    """
    client, transport = make_client([recorded("malformed")])

    claims = await extract_claims(hawker_text(), client=client, settings=extract_settings())

    assert claims == []
    assert len(transport.calls) == 1, "an unusable answer is an answer; it is not retried"


async def test_an_empty_but_valid_answer_yields_no_claims() -> None:
    """``{"claims": []}`` parses, and means exactly what it says."""
    response = LLMResponse(content='{"claims": []}', prompt_tokens=5, completion_tokens=5)
    client, _ = make_client([response])

    assert await extract_claims(hawker_text(), client=client, settings=extract_settings()) == []


async def test_a_rejected_request_propagates_instead_of_looking_like_an_empty_article() -> None:
    """A bad key or an unreachable model is an error, not an article with no claims.

    Flattening the two would tell a reader their article contains nothing worth
    checking when in truth nothing was checked. The caller publishes ``error``.
    """
    client, transport = make_client([LLMBadRequest("404 model_not_found")])

    with pytest.raises(LLMBadRequest):
        await extract_claims(hawker_text(), client=client, settings=extract_settings())

    assert len(transport.calls) == 1, "a 4xx is never retried"


async def test_a_provider_outage_propagates_after_its_retries() -> None:
    """Same reasoning, and the retry policy still belongs to the client."""
    client, transport = make_client([LLMUnavailable("503"), LLMUnavailable("503")])

    with pytest.raises(LLMUnavailable):
        await extract_claims(hawker_text(), client=client, settings=extract_settings())

    assert len(transport.calls) == 2


async def test_empty_text_never_reaches_the_model() -> None:
    """No article, no call. The cheapest possible answer to a page with no text."""
    client, transport = make_client([])

    assert await extract_claims("   \n\n  ", client=client, settings=extract_settings()) == []
    assert transport.calls == []


# ---------------------------------------------------------------- truncation


def test_a_short_article_is_untouched() -> None:
    """The budget clears a typical Singapore news story several times over."""
    text = hawker_text()

    assert truncate_article(text, 12_000) == text


def test_truncation_is_always_a_prefix() -> None:
    """The property the offsets rest on.

    Because the cut is a prefix, an offset found in the truncated text is the
    same offset in the caller's original, and ``Claim.start``/``end`` mean what
    ``shared/schema.json`` says they mean without anything being translated back.
    """
    text = "First sentence. " * 400

    for limit in (1, 50, 500, 5_000, len(text) - 1):
        cut = truncate_article(text, limit)
        assert len(cut) <= limit
        assert text.startswith(cut)


def test_truncation_prefers_a_paragraph_break() -> None:
    """A tidy cut is worth a paragraph — it is what stops a claim being half-quoted."""
    text = "A. " * 30 + "\n\n" + "B. " * 30
    limit = len("A. " * 30) + 10

    cut = truncate_article(text, limit)

    assert cut == "A. " * 30
    assert not cut.endswith("B")


def test_truncation_falls_back_to_a_sentence_end() -> None:
    """No paragraph break in reach, so cut where a sentence finished."""
    text = "Rents rose by 4% in 2024. " * 40
    limit = 130

    cut = truncate_article(text, limit)

    assert cut.endswith("2024. ")
    assert len(cut) <= limit


def test_truncation_cuts_hard_rather_than_giving_back_the_article() -> None:
    """One long unpunctuated run: better a blunt cut than a fifth of the budget lost."""
    text = "word " * 4_000
    limit = 1_000

    cut = truncate_article(text, limit)

    assert len(cut) == limit


def test_a_zero_budget_is_not_a_licence_to_send_everything() -> None:
    """A nonsensical ``MAX_ARTICLE_CHARS`` fails closed."""
    assert truncate_article(hawker_text(), 0) == ""
    assert truncate_article(hawker_text(), -5) == ""


async def test_a_long_article_is_truncated_before_the_call() -> None:
    """The truncation is a cost control, so it has to happen on the way *out*.

    This is the one call in the pipeline with no other ceiling on its input
    size: there is no per-claim cap to fall back on, because the claims do not
    exist yet.
    """
    text = "Rents rose by 4% in 2024. " * 400
    settings = extract_settings(max_article_chars=1_000)
    client, transport = make_client([recorded("opinion_none")])

    await extract_claims(text, client=client, settings=settings)

    sent = transport.calls[0].user
    assert len(sent) < len(text)
    body = sent.removeprefix(f"{ARTICLE_OPEN}\n").removesuffix(f"\n{ARTICLE_CLOSE}")
    assert len(body) <= 1_000
    assert text.startswith(body)


async def test_offsets_are_relative_to_the_truncated_text_which_is_a_prefix() -> None:
    """So a client can resolve them against the full article it sent, unchanged."""
    prefix = "Rents rose by 4% in 2024. "
    text = prefix * 40 + "\n\nThe last islandwide rent adjustment took effect in 2024."
    settings = extract_settings(max_article_chars=200)
    response = LLMResponse(
        content=json.dumps(
            {"claims": [{"quote": "Rents rose by 4% in 2024",
                         "kind": "numeric", "checkworthiness": 0.9}]}
        ),
        prompt_tokens=10,
        completion_tokens=10,
    )
    client, _ = make_client([response])

    claims = await extract_claims(text, client=client, settings=settings)

    assert len(claims) == 1
    assert text[claims[0].start : claims[0].end] == claims[0].quote


# ---------------------------------------------------------------- privacy


async def test_no_log_line_carries_the_article(caplog: pytest.LogCaptureFixture) -> None:
    """``CLAUDE.md`` privacy rule 6: article text is never logged.

    Counts, the model and the prompt version are logged, because that is what
    makes a check's cost attributable. The article is not, on the success path
    or the failure path.
    """
    text = hawker_text()
    # The dangerous case is not the unparseable answer but the *nearly* valid
    # one. Pydantic quotes the offending input back in its message
    # (`input_value=...`), and the field most likely to be malformed is the one
    # holding article text. Logging that exception's traceback would put the
    # article in the log; this recording is the shape that would do it.
    leaky_answer = LLMResponse(
        content=json.dumps(
            {
                "claims": [
                    {
                        "quote": {"text": "More than 200 stalls have already closed this year"},
                        "kind": "numeric",
                        "checkworthiness": 0.9,
                    }
                ]
            }
        ),
        prompt_tokens=10,
        completion_tokens=10,
    )

    with caplog.at_level(logging.DEBUG, logger="app.pipeline.extract"):
        client, _ = make_client([recorded("hawker_claims")])
        await extract_claims(text, client=client, settings=extract_settings())
        client, _ = make_client([recorded("malformed")])
        await extract_claims(text, client=client, settings=extract_settings())
        client, _ = make_client([leaky_answer])
        assert await extract_claims(text, client=client, settings=extract_settings()) == []

    assert "hawker" not in caplog.text.lower()
    assert "40%" not in caplog.text
    # Pydantic elides the middle of a long value, so check both visible ends.
    assert "More than 200 st" not in caplog.text
    assert "ready closed this year" not in caplog.text
    for line in text.split("\n\n"):
        assert line[:40] not in caplog.text


async def test_the_success_line_records_what_the_call_cost(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The eval harness and every cost review read these numbers back out."""
    with caplog.at_level(logging.INFO, logger="app.pipeline.extract"):
        client, _ = make_client([recorded("hawker_claims")])
        await extract_claims(hawker_text(), client=client, settings=extract_settings())

    assert "7 candidates" in caplog.text
    assert "completion_tokens=268" in caplog.text


# ---------------------------------------------------------------- the response model


def test_the_response_model_refuses_an_invented_kind() -> None:
    """A fourth claim kind is an invalid answer, not a value to pass downstream."""
    with pytest.raises(ValueError):
        ExtractionResponse.model_validate(
            {"claims": [{"quote": "x" * 20, "kind": "prediction", "checkworthiness": 0.5}]}
        )
