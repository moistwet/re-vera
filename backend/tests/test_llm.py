"""The LLM client, the prompt loader, and the two verification primitives.

Everything the milestone-2 pipeline knows about talking to a model funnels
through :mod:`app.llm`, and this environment has no API key and no route to any
provider — so every one of these tests drives that module through
:class:`~app.llm.ReplayTransport` or a small purpose-built fake. Nothing here
touches the network, which is both the project's standing rule and, here, the
only way to work at all.

What is asserted, and why each one earns its place:

* **Prompt loading.** A prompt with no version makes every eval number
  unattributable, and a header copy-pasted between files mislabels every log
  line for a stage. Both are refused loudly.
* **One successful call.** The transport is handed the prompt as ``system`` and
  the untrusted content as ``user`` (never concatenated), the schema goes out
  strict, and :class:`~app.llm.Usage` comes back with real numbers.
* **A 4xx is never retried** — the transport is called *exactly once*. This is a
  cost rule, not a style preference: a rejected request repeated is the same
  rejected request, billed twice.
* **A 5xx is retried and can succeed**, and a timeout surfaces as
  :class:`~app.llm.LLMUnavailable` rather than hanging a reader's check.
* **Nothing outside ``app/llm.py`` imports the OpenAI SDK.** Enforced by walking
  the tree, because no linter enforces it and the property is load-bearing:
  provider assumptions have exactly one home.
* **User content is never logged**, even at INFO. It is article text and
  retrieved passages (``CLAUDE.md`` privacy rule 6).

The last two sections cover :mod:`app.pipeline.types`'s verification helpers and
:mod:`app.config`'s missing-key errors. They live here rather than in files of
their own because this task owns this test module; a later split into
``test_pipeline_types.py`` would be a tidy-up, not a change of meaning.
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, Field

from app.config import DEFAULT_MODEL, MissingSettingError
from app.llm import (
    LLMBadRequest,
    LLMClient,
    LLMInvalidOutput,
    LLMResponse,
    LLMUnavailable,
    Prompt,
    PromptError,
    ReplayTransport,
    load_prompt,
    load_recorded_response,
    strict_json_schema,
)
from app.pipeline.types import (
    ExtractedClaim,
    claim_id,
    normalize_for_match,
    quote_is_exact,
    span_occurs_in,
)

from .conftest import build_settings

BACKEND_ROOT = Path(__file__).resolve().parents[1]
"""``backend/`` — the tree the SDK-import guard walks."""

LLM_FIXTURES = Path(__file__).parent / "fixtures" / "llm"
"""Hand-written recorded responses; see that directory's README for the format."""

SECRET_CONTENT = "SINGAPORE — hawker stall rents are set to rise by 40% from 1 January."
"""Stand-in for article text. Fictional, and asserted never to reach a log line."""


class Answer(BaseModel):
    """The throwaway response model these tests ask for.

    Deliberately tiny and deliberately *not* one of the pipeline's real stage
    schemas: this module is testing the client, and coupling it to a stage's
    schema would make a stage's prompt change break the client's tests.
    """

    verdict: str
    cited_spans: list[str]


def prompt(version: str = "1") -> Prompt:
    """A :class:`~app.llm.Prompt` built in memory, for tests that need no file."""
    return Prompt(name="judge", version=version, text="Use only the passages below.")


def recorded() -> LLMResponse:
    """The worked-example recording from ``tests/fixtures/llm``."""
    return load_recorded_response(LLM_FIXTURES / "example_response.json")


def make_client(
    outcomes: list[LLMResponse | Exception],
    *,
    max_retries: int = 2,
) -> tuple[LLMClient, ReplayTransport]:
    """A client wired to a scripted transport, with backoff collapsed to nothing."""
    transport = ReplayTransport(list(outcomes))
    client = LLMClient(
        api_key="test-key-never-used",
        timeout=5.0,
        max_retries=max_retries,
        transport=transport,
        retry_base_delay=0.0,
    )
    return client, transport


# ---------------------------------------------------------------- prompt loading


def write_prompt(directory: Path, filename: str, body: str) -> Path:
    """Write a prompt file into ``directory`` and return its path."""
    path = directory / filename
    path.write_text(body, encoding="utf-8")
    return path


def test_load_prompt_parses_the_front_matter(tmp_path: Path) -> None:
    """Name, version and body come back separated, with the body stripped."""
    write_prompt(
        tmp_path,
        "extract.md",
        "---\n"
        "name: extract\n"
        "# a comment inside the header is ignored\n"
        "version: 3\n"
        "---\n"
        "\n"
        "You are extracting check-worthy claims.\n",
    )

    loaded = load_prompt("extract", directory=tmp_path)

    assert loaded.name == "extract"
    assert loaded.version == "3"
    assert loaded.text == "You are extracting check-worthy claims."


def test_load_prompt_keeps_the_body_intact(tmp_path: Path) -> None:
    """Only the outer blank lines are stripped; the body's own shape survives."""
    write_prompt(
        tmp_path,
        "judge.md",
        "---\nname: judge\nversion: 2.1\n---\nLine one.\n\nLine two.\n",
    )

    loaded = load_prompt("judge", directory=tmp_path)

    assert loaded.version == "2.1", "version is a string, so 2.1 is sayable"
    assert loaded.text == "Line one.\n\nLine two."


def test_load_prompt_caches_by_path(tmp_path: Path) -> None:
    """A second load of the same file is the same object, not a second read."""
    write_prompt(tmp_path, "stance.md", "---\nname: stance\nversion: 1\n---\nScore each passage.")

    assert load_prompt("stance", directory=tmp_path) is load_prompt("stance", directory=tmp_path)


@pytest.mark.parametrize(
    ("filename", "body", "expected"),
    [
        pytest.param(
            "noversion.md",
            "---\nname: noversion\n---\nBody.",
            "version",
            id="no version header",
        ),
        pytest.param(
            "noname.md",
            "---\nversion: 1\n---\nBody.",
            "name",
            id="no name header",
        ),
        pytest.param(
            "mismatch.md",
            "---\nname: something-else\nversion: 1\n---\nBody.",
            "must match",
            id="name does not match the filename",
        ),
        pytest.param(
            "bare.md",
            "You are a helpful assistant.",
            "front-matter",
            id="no front matter at all",
        ),
        pytest.param(
            "unclosed.md",
            "---\nname: unclosed\nversion: 1\n",
            "never closed",
            id="front matter is never closed",
        ),
        pytest.param(
            "emptybody.md",
            "---\nname: emptybody\nversion: 1\n---\n\n\n",
            "no prompt body",
            id="header but no body",
        ),
        pytest.param(
            "notpairs.md",
            "---\nname: notpairs\nthis line is not a pair\nversion: 1\n---\nBody.",
            "key: value",
            id="front-matter line is not key: value",
        ),
    ],
)
def test_load_prompt_rejects_a_malformed_file(
    tmp_path: Path, filename: str, body: str, expected: str
) -> None:
    """Every authoring mistake is a loud :class:`PromptError`, never a silent default.

    An unversioned or mislabelled prompt does not fail at the point of use — it
    fails a week later, when nobody can say which prompt produced which eval
    number. Failing at load is the whole point.
    """
    write_prompt(tmp_path, filename, body)

    with pytest.raises(PromptError, match=expected):
        load_prompt(filename.removesuffix(".md"), directory=tmp_path)


def test_load_prompt_reports_a_missing_file(tmp_path: Path) -> None:
    """A missing prompt names the path and points at the convention."""
    with pytest.raises(PromptError, match="no prompt file at"):
        load_prompt("absent", directory=tmp_path)


# ---------------------------------------------------------------- schema derivation


class Nested(BaseModel):
    """A nested object, so the strict rules are checked inside ``$defs`` too."""

    outlet: str
    date: str | None


class Constrained(BaseModel):
    """Carries the keywords a strict structured-output mode is assumed to refuse."""

    quote: str = Field(min_length=1)
    score: float = Field(default=0.5, ge=0.0, le=1.0)
    source: Nested


def test_strict_schema_forbids_extra_properties_and_requires_everything() -> None:
    """Strict mode's two rules, applied at every object node including ``$defs``."""
    schema = strict_json_schema(Constrained)

    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"quote", "score", "source"}, (
        "a field with a default is still required — strict mode expresses optional "
        "as nullable, not as absent"
    )
    nested = schema["$defs"]["Nested"]
    assert nested["additionalProperties"] is False
    assert set(nested["required"]) == {"outlet", "date"}


def test_strict_schema_strips_the_unsupported_keywords() -> None:
    """Constraint keywords are removed from the wire, not from the guarantee.

    They are stripped because the provider is assumed to reject them outright,
    and nothing is lost: the answer is re-validated against the full pydantic
    model on the way back, so the constraint is enforced by us instead.
    """
    rendered = repr(strict_json_schema(Constrained))

    for keyword in ("minLength", "minimum", "maximum", "default"):
        assert keyword not in rendered


async def test_a_constraint_stripped_from_the_schema_is_still_enforced() -> None:
    """The re-validation on the way back is what makes stripping safe."""
    client, _ = make_client([LLMResponse(content='{"quote": "", "score": 0.5}', **_TOKENS)])

    with pytest.raises(LLMInvalidOutput, match="not a valid"):
        await client.structured(
            model="m", prompt=prompt(), user_content="x", schema=_QuoteOnly
        )


_TOKENS = {"prompt_tokens": 10, "completion_tokens": 2}


class _QuoteOnly(BaseModel):
    """One constrained field, for the round-trip enforcement test above."""

    quote: str = Field(min_length=1)


# ---------------------------------------------------------------- a successful call


async def test_structured_returns_the_parsed_answer_and_its_usage() -> None:
    """The happy path: a parsed model plus a populated :class:`~app.llm.Usage`."""
    client, transport = make_client([recorded()])

    answer, usage = await client.structured(
        model=DEFAULT_MODEL,
        prompt=prompt(version="4"),
        user_content=SECRET_CONTENT,
        schema=Answer,
    )

    assert answer.verdict == "contradicted"
    assert answer.cited_spans == ["the median adjustment is 4 per cent"]
    assert usage.model == DEFAULT_MODEL
    assert usage.prompt_version == "4"
    assert usage.prompt_tokens == 812
    assert usage.completion_tokens == 143
    assert usage.latency_ms >= 0.0
    assert len(transport.calls) == 1


async def test_the_prompt_and_the_untrusted_content_stay_in_separate_roles() -> None:
    """The structural half of the prompt-injection defence.

    The prompt body goes out as ``system`` and the untrusted article text as
    ``user``; they are never concatenated, so no article can append itself to
    the instructions. (The prompt file's fencing is the other half — see
    ``app/prompts/README.md``.)
    """
    client, transport = make_client([recorded()])
    system_prompt = prompt()

    await client.structured(
        model="m", prompt=system_prompt, user_content=SECRET_CONTENT, schema=Answer
    )

    call = transport.calls[0]
    assert call.system == system_prompt.text
    assert call.user == SECRET_CONTENT
    assert SECRET_CONTENT not in call.system
    assert call.schema_name == "Answer"
    assert call.json_schema["additionalProperties"] is False
    assert call.timeout == 5.0


async def test_a_successful_call_logs_the_accounting_but_never_the_content(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Prompt version, model, both token counts and latency at INFO — and nothing else.

    ``CLAUDE.md`` privacy rule 6: article text is never logged with an
    identifier. This module holds no identifier at all, and the simplest way to
    keep it that way is never to log the content.
    """
    client, _ = make_client([recorded()])

    with caplog.at_level(logging.INFO, logger="app.llm"):
        await client.structured(
            model="gpt-test-mini",
            prompt=prompt(version="7"),
            user_content=SECRET_CONTENT,
            schema=Answer,
        )

    logged = caplog.text
    assert "model=gpt-test-mini" in logged
    assert "prompt=judge@v7" in logged
    assert "prompt_tokens=812" in logged
    assert "completion_tokens=143" in logged
    assert "latency_ms=" in logged
    assert SECRET_CONTENT not in logged
    assert "hawker" not in logged.lower()


# ---------------------------------------------------------------- failure policy


async def test_a_4xx_is_never_retried() -> None:
    """The cost rule, asserted as a call count.

    A rejected request repeated is the same rejected request with a second bill,
    so ``LLMBadRequest`` propagates from the first attempt even though this
    client is configured for two retries.
    """
    client, transport = make_client([LLMBadRequest("400 invalid schema")], max_retries=2)

    with pytest.raises(LLMBadRequest, match="400"):
        await client.structured(model="m", prompt=prompt(), user_content="x", schema=Answer)

    assert len(transport.calls) == 1, "a 4xx must cost exactly one call"


async def test_an_invalid_answer_is_not_retried_either() -> None:
    """A malformed answer is a real answer: paying again buys the same one.

    The recorded fixture is truncated JSON, which is what a length-capped
    completion looks like. The caller's correct response is ``unverifiable``.
    """
    malformed = load_recorded_response(LLM_FIXTURES / "malformed_response.json")
    client, transport = make_client([malformed], max_retries=2)

    with pytest.raises(LLMInvalidOutput, match="not a valid Answer"):
        await client.structured(model="m", prompt=prompt(), user_content="x", schema=Answer)

    assert len(transport.calls) == 1


async def test_a_5xx_is_retried_and_can_succeed() -> None:
    """Two failures then an answer, inside ``max_retries=2`` — three calls in all."""
    client, transport = make_client(
        [LLMUnavailable("503"), LLMUnavailable("502"), recorded()], max_retries=2
    )

    answer, usage = await client.structured(
        model="m", prompt=prompt(), user_content="x", schema=Answer
    )

    assert answer.verdict == "contradicted"
    assert usage.prompt_tokens == 812
    assert len(transport.calls) == 3


async def test_retries_are_bounded() -> None:
    """One attempt plus ``max_retries``, then the last failure is raised."""
    client, transport = make_client([LLMUnavailable(f"503 #{n}") for n in range(5)], max_retries=2)

    with pytest.raises(LLMUnavailable, match="503 #2"):
        await client.structured(model="m", prompt=prompt(), user_content="x", schema=Answer)

    assert len(transport.calls) == 3


async def test_max_retries_zero_means_one_attempt() -> None:
    """The setting is retries *after* the first call, not total calls."""
    client, transport = make_client([LLMUnavailable("503")], max_retries=0)

    with pytest.raises(LLMUnavailable):
        await client.structured(model="m", prompt=prompt(), user_content="x", schema=Answer)

    assert len(transport.calls) == 1


def test_a_negative_retry_count_is_refused() -> None:
    """Misconfiguration fails at construction, not at the first call."""
    with pytest.raises(ValueError, match="max_retries"):
        LLMClient(api_key="k", timeout=1.0, max_retries=-1, transport=ReplayTransport([]))


# ---------------------------------------------------------------- timeouts


class SlowTransport:
    """A transport that sleeps, to exercise the client's own hard timeout.

    ``delays`` is consumed one entry per call, so a test can script "hang, then
    answer" and prove that a timeout is retryable.
    """

    def __init__(self, delays: list[float], answer: LLMResponse) -> None:
        self.delays = delays
        self.answer = answer
        self.calls = 0

    async def complete(
        self,
        *,
        model: str,
        system: str,
        user: str,
        schema_name: str,
        json_schema: dict[str, Any],
        timeout: float,
    ) -> LLMResponse:
        """Sleep for the next scripted delay, then answer."""
        delay = self.delays[self.calls] if self.calls < len(self.delays) else 0.0
        self.calls += 1
        await asyncio.sleep(delay)
        return self.answer


async def test_a_hang_surfaces_as_unavailable_rather_than_hanging() -> None:
    """The client's ceiling holds even when the transport ignores its own timeout.

    A reader's check must not be able to stall on one call: a stalled claim is
    worth far less than an ``unverifiable`` one delivered promptly.
    """
    transport = SlowTransport([10.0], recorded())
    client = LLMClient(
        api_key="k", timeout=0.02, max_retries=0, transport=transport, retry_base_delay=0.0
    )

    with pytest.raises(LLMUnavailable, match="client-side timeout"):
        await client.structured(model="m", prompt=prompt(), user_content="x", schema=Answer)

    assert transport.calls == 1


async def test_a_timeout_is_retryable() -> None:
    """A hang is a transient failure, so the next attempt gets to answer."""
    transport = SlowTransport([10.0], recorded())
    client = LLMClient(
        api_key="k", timeout=0.05, max_retries=1, transport=transport, retry_base_delay=0.0
    )

    answer, _ = await client.structured(
        model="m", prompt=prompt(), user_content="x", schema=Answer
    )

    assert answer.verdict == "contradicted"
    assert transport.calls == 2


# ---------------------------------------------------------------- the SDK guard


SDK_IMPORT = re.compile(r"^\s*(?:from|import)\s+openai\b", re.MULTILINE)
"""Matches ``import openai`` and ``from openai import ...`` at any indentation.

Any indentation on purpose: ``app/llm.py``'s own imports are deliberately inside
functions, and a second module hiding one inside a function would be exactly the
leak this guard exists to catch.
"""

ALLOWED_SDK_IMPORTERS = {BACKEND_ROOT / "app" / "llm.py"}
"""The one module allowed to import the OpenAI SDK."""


def python_sources() -> list[Path]:
    """Every checked-in Python file under ``backend/``, ignoring virtualenvs."""
    ignored = {".venv", "venv", "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache"}
    return [
        path
        for path in BACKEND_ROOT.rglob("*.py")
        if not ignored.intersection(path.relative_to(BACKEND_ROOT).parts)
    ]


def test_only_the_llm_module_imports_the_openai_sdk() -> None:
    """Provider assumptions have exactly one home, and no linter enforces that.

    ``app/llm.py`` is the only place that may know the SDK exists: everything
    else talks to :class:`~app.llm.LLMTransport`. A second import anywhere would
    scatter provider-specific behaviour into a stage, where a swap of provider
    could not find it — and where nobody would think to look when the assumed
    request shape turns out to be wrong. Retrieval's search provider included:
    it goes behind the same seam.
    """
    sources = python_sources()
    assert len(sources) > 10, "the source walk found almost nothing; the glob is wrong"

    offenders = sorted(
        str(path.relative_to(BACKEND_ROOT))
        for path in sources
        if path not in ALLOWED_SDK_IMPORTERS and SDK_IMPORT.search(path.read_text(encoding="utf-8"))
    )

    assert offenders == [], (
        f"these modules import the OpenAI SDK directly: {offenders}. "
        f"Only app/llm.py may; everything else goes through LLMTransport."
    )


def test_the_guard_would_catch_an_offender(tmp_path: Path) -> None:
    """The guard's own regex, checked against the shapes it has to catch."""
    assert SDK_IMPORT.search("import openai\n")
    assert SDK_IMPORT.search("from openai import AsyncOpenAI\n")
    assert SDK_IMPORT.search("def f():\n    import openai\n"), "an indented import still counts"
    assert not SDK_IMPORT.search("# import openai — mentioned in a comment\n")
    assert not SDK_IMPORT.search("import openailike\n")


# ---------------------------------------------------------------- pipeline types


def extracted(quote: str, start: int, end: int) -> ExtractedClaim:
    """An :class:`ExtractedClaim` with the fields these tests care about."""
    return ExtractedClaim(
        id="c1", quote=quote, start=start, end=end, kind="numeric", checkworthiness=0.9
    )


ARTICLE = "Rents are set to rise by 40% from 1 January, vendors said."


def test_an_exact_quote_passes_and_a_drifted_one_does_not() -> None:
    """Extraction's gate: the offsets are a promise about these exact characters.

    A model that paraphrases, or reports offsets that slid by one, must fail
    here — accepting it would put milestone 3's highlight over the wrong words.
    """
    assert quote_is_exact(extracted("rise by 40%", 17, 28), ARTICLE)
    assert not quote_is_exact(extracted("rise by 40%", 18, 29), ARTICLE), "offsets drifted by one"
    assert not quote_is_exact(extracted("rise by 40 percent", 17, 28), ARTICLE), "paraphrase"


@pytest.mark.parametrize(
    ("start", "end"),
    [(-1, 5), (0, len(ARTICLE) + 1), (5, 5), (9, 4)],
    ids=["negative start", "past the end", "empty span", "reversed"],
)
def test_impossible_offsets_are_refused_rather_than_raising(start: int, end: int) -> None:
    """Out-of-range offsets are False, not an ``IndexError`` — this runs on model output."""
    assert not quote_is_exact(extracted("anything", start, end), ARTICLE)


def test_claim_ids_are_one_based() -> None:
    """``c1 … cN`` in article order, spelled the same way by every stage."""
    assert claim_id(1) == "c1"
    assert claim_id(8) == "c8"
    with pytest.raises(ValueError, match="1-based"):
        claim_id(0)


PASSAGE = "The median adjustment is 4 per cent — not 40 per cent, the release said."


def test_a_real_citation_is_found_through_typography_and_whitespace() -> None:
    """A judge retyping a passage changes case, spacing and quote marks routinely.

    None of those changes what was said, so folding them is the difference
    between a check that works and one that downgrades every honest answer.
    """
    assert span_occurs_in("The median adjustment is 4 per cent", PASSAGE)
    assert span_occurs_in("the   MEDIAN\nadjustment is 4 per cent", PASSAGE)
    assert span_occurs_in("4 per cent — not 40 per cent", PASSAGE), "em dash folds to a hyphen"
    assert span_occurs_in("4 per cent - not 40 per cent", PASSAGE)


def test_an_invented_citation_is_not_found() -> None:
    """The property the whole milestone turns on.

    The judge may use the retrieved passages and nothing else (``CLAUDE.md``
    rule 2). A span it did not get from a passage is a fabricated citation, and
    the caller's answer to that is ``unverifiable``.
    """
    assert not span_occurs_in("the median adjustment is 40 per cent", PASSAGE)
    assert not span_occurs_in("officials confirmed the rise", PASSAGE)


def test_an_empty_citation_is_never_a_match() -> None:
    """Citing nothing is not citing everything.

    Returning True for an empty span would let the emptiest possible answer
    through the tightest gate the pipeline has.
    """
    assert not span_occurs_in("", PASSAGE)
    assert not span_occurs_in("   \n  ", PASSAGE)


def test_a_span_may_come_from_any_of_the_passages() -> None:
    """A claim is judged on a batch, so the check is against the whole batch."""
    passages = ["Nothing relevant here.", PASSAGE, "Nor here."]

    assert span_occurs_in("not 40 per cent", passages)
    assert not span_occurs_in("not 400 per cent", passages)


def test_normalisation_is_for_comparison_only() -> None:
    """Documented shape of the fold, so a change to it is a deliberate one."""
    assert normalize_for_match("  The  “Quote”  —  here ") == 'the "quote" - here'


# ---------------------------------------------------------------- missing keys


def test_settings_import_and_run_with_no_keys_at_all() -> None:
    """The whole point of the keys being optional fields.

    Milestone 1's routes, the mock pipeline and this entire suite must work in
    an environment with no ``.env`` and no keys — as this one does. Making the
    keys required fields would turn that into an import-time crash.
    """
    settings = build_settings()

    assert settings.openai_api_key is None
    assert settings.google_factcheck_api_key is None
    assert settings.use_mock_pipeline is False
    assert settings.openai_model_extract == DEFAULT_MODEL
    assert settings.openai_model_stance == DEFAULT_MODEL
    assert settings.openai_model_judge == DEFAULT_MODEL
    assert settings.max_passages_per_claim == 6
    assert settings.pipeline_concurrency == 4
    assert settings.llm_max_retries == 2


def test_a_missing_key_fails_at_the_point_of_use_with_an_actionable_message() -> None:
    """The person reading this error is usually setting the project up.

    So it names the variable, the file it belongs in, and what wanted it — and
    it says not to commit it.
    """
    settings = build_settings()

    with pytest.raises(MissingSettingError) as caught:
        settings.require_openai_api_key("claim extraction")

    message = str(caught.value)
    assert "OPENAI_API_KEY" in message
    assert "backend/.env" in message
    assert "claim extraction" in message
    assert "gitignored" in message


def test_a_configured_key_is_returned() -> None:
    """Both accessors hand back the configured value."""
    settings = build_settings(
        openai_api_key="sk-not-a-real-key", google_factcheck_api_key="not-a-real-key"
    )

    assert settings.require_openai_api_key() == "sk-not-a-real-key"
    assert settings.require_google_factcheck_api_key() == "not-a-real-key"


def test_the_factcheck_key_reports_itself_by_name() -> None:
    """Retrieval catches this one and falls back to web search, so it must be
    distinguishable from the OpenAI key's error."""
    with pytest.raises(MissingSettingError, match="GOOGLE_FACTCHECK_API_KEY"):
        build_settings().require_google_factcheck_api_key()


# ---------------------------------------------------------------- recorded fixtures


def test_the_recorded_fixture_format_round_trips() -> None:
    """The ``json`` form is serialised for the caller; token counts come through."""
    response = recorded()

    assert response.prompt_tokens == 812
    assert response.completion_tokens == 143
    assert Answer.model_validate_json(response.content).verdict == "contradicted"


def test_a_recording_needs_a_json_or_content_key(tmp_path: Path) -> None:
    """A fixture that records nothing fails loudly rather than replaying ``""``."""
    path = tmp_path / "empty.json"
    path.write_text('{"_note": "no answer here"}', encoding="utf-8")

    with pytest.raises(ValueError, match="`json` or a `content` key"):
        load_recorded_response(path)


async def test_the_replay_transport_refuses_an_unscripted_call() -> None:
    """An extra call is the bug the fixture exists to catch, so it is never silent."""
    client, _ = make_client([recorded()])

    await client.structured(model="m", prompt=prompt(), user_content="x", schema=Answer)

    with pytest.raises(AssertionError, match="ran out of scripted outcomes"):
        await client.structured(model="m", prompt=prompt(), user_content="x", schema=Answer)
