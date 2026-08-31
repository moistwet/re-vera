"""Runtime configuration.

Every setting is read from the process environment (and from ``backend/.env``
when present) by pydantic-settings; field names map case-insensitively onto the
env var of the same name, so ``redis_url`` comes from ``REDIS_URL``. See
``backend/.env.example`` for the documented set.

**No secret has a default here and none belongs in this file.** The two API keys
milestone 2 needs (``OPENAI_API_KEY``, ``GOOGLE_FACTCHECK_API_KEY``) default to
``None`` and live only in the gitignored ``backend/.env``. They are deliberately
*optional* on the model: the whole backend — every milestone-1 route, the whole
test suite, and the mock pipeline — must import and run with neither key set,
and it does. A key is demanded at the moment it is first needed, by
:meth:`Settings.require_openai_api_key` and
:meth:`Settings.require_google_factcheck_api_key`, which raise a
:class:`MissingSettingError` naming the variable and what wanted it. Making them
required fields instead would turn a missing key into an import-time crash of
the whole service, including the paths that never call an API at all.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["DEFAULT_MODEL", "MissingSettingError", "Settings", "get_settings"]

DEFAULT_MODEL = "gpt-5-mini"
"""Default model id for all three LLM stages — the cheapest tier we will accept.

``docs/decisions.md`` §7: every stage defaults to a mini-tier model and any one
of them can be pointed at a stronger one through its own env var, so a failing
golden-set eval is answered by changing configuration rather than code.

**This id is account-dependent and cannot be verified from this repository.**
It was chosen because it is a mini-tier id enumerated by the pinned ``openai``
SDK, not because anyone here confirmed the project's account can call it. An
account that cannot gets a 4xx, which :class:`~app.llm.LLMBadRequest` surfaces
loudly and never retries; the fix is to set the three ``OPENAI_MODEL_*`` vars.
"""


class MissingSettingError(RuntimeError):
    """A setting that has no safe default was needed and is not configured.

    Raised at the point of use rather than at import, so the parts of the
    service that need no key keep working. The message names the environment
    variable, where it belongs (``backend/.env``) and what asked for it, because
    the person who sees it is usually setting the project up for the first time.
    """

    def __init__(self, env_var: str, needed_for: str) -> None:
        self.env_var = env_var
        """The environment variable to set, e.g. ``OPENAI_API_KEY``."""
        self.needed_for = needed_for
        """What wanted it, as a phrase — e.g. "claim extraction"."""
        super().__init__(
            f"{env_var} is not set, and {needed_for} needs it. "
            f"Add `{env_var}=...` to backend/.env (copy backend/.env.example if you have "
            f"no .env yet). backend/.env is gitignored: never commit a key, and never put "
            f"one in the extension."
        )


class Settings(BaseSettings):
    """Backend settings, loaded from the environment."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ---------------------------------------------------------------- milestone 1

    redis_url: str = "redis://localhost:6379/0"
    """Redis connection URL — cache, daily cap, and the per-job event stream."""

    allowed_extension_origin: str = "chrome-extension://*"
    """Single CORS origin allowed to call this API. Pin to the real extension id
    before shipping; the wildcard form exists because an unpacked extension's id
    is not known during development."""

    daily_cap: int = 20
    """Checks allowed per install ID per day (Asia/Singapore). A cost control as
    much as an abuse control — never bypassed outside local dev."""

    max_claims: int = 8
    """Maximum claims verified per article.

    The single biggest lever on the cost of one check: everything after
    extraction — retrieval, stance, judge — is paid per claim
    (``docs/decisions.md`` §8)."""

    mock_step_delay: float = 0.85
    """Seconds between mock claim events in the milestone-1 fake pipeline."""

    # ---------------------------------------------------------------- milestone 2

    use_mock_pipeline: bool = False
    """Run the milestone-1 mock pipeline instead of the real one.

    Set ``USE_MOCK_PIPELINE=true`` to demo, or to develop the extension, without
    an API key and without spending anything: the mock replays the six fictional
    fixture claims with the prototype's pacing. It is a deliberate switch, not a
    fallback — the real pipeline never quietly degrades into the mock, because a
    reader shown fixture verdicts for their article would have no way to tell.
    """

    openai_api_key: str | None = None
    """OpenAI key, for extraction, stance scoring, judging and web search.

    Optional on the model so the service imports and the suite runs without it;
    demanded by :meth:`require_openai_api_key` at first use. Lives only in the
    gitignored ``backend/.env``."""

    google_factcheck_api_key: str | None = None
    """Google Fact Check Tools (ClaimReview) key.

    Optional in the same way, and optional in a second sense: retrieval degrades
    to web search without it (losing the short-circuit that makes a fact-checked
    claim cheap), so it is demanded only by the fact-check provider itself."""

    openai_model_extract: str = DEFAULT_MODEL
    """Model for stage 1, claim extraction. See :data:`DEFAULT_MODEL`."""

    openai_model_stance: str = DEFAULT_MODEL
    """Model for stage 3, per-passage stance scoring. See :data:`DEFAULT_MODEL`."""

    openai_model_judge: str = DEFAULT_MODEL
    """Model for stage 4, the verdict. See :data:`DEFAULT_MODEL`.

    The likeliest of the three to be worth escalating, since it is the one whose
    output a reader reads — but escalate only on a golden-set failure
    (``docs/decisions.md`` §7), never on a hunch."""

    llm_timeout_seconds: float = 30.0
    """Hard per-call ceiling on one LLM request, in seconds.

    Enforced by :class:`~app.llm.LLMClient` itself, not only handed to the SDK,
    so a transport that ignores its timeout still cannot hang a claim forever.
    A check that has already made the reader wait half a minute for one call has
    failed at being a fast answer; better to say ``unverifiable`` than to stall.
    """

    llm_max_retries: int = 2
    """Retries *after* the first attempt, so at most three tries in total.

    Only :class:`~app.llm.LLMUnavailable` (5xx, timeout, connection failure) is
    ever retried. A 4xx is never retried under any setting — a bad request
    repeated is the same bad request, billed twice."""

    max_passages_per_claim: int = 6
    """Passages kept per claim after retrieval and de-duplication.

    Caps what stage 3 and stage 4 are billed for, since both are paid by the
    length of the passages they read (``docs/decisions.md`` §9)."""

    max_article_chars: int = 12_000
    """Article text is truncated to this many characters before extraction.

    Roughly 3,000 tokens at English news prose's ~4 characters per token, or
    about 2,000 words. Singapore news articles run 400-1,200 words (2,500-7,500
    characters), so the budget clears a typical story with room to spare and
    bites only on long features and liveblogs — where the check-worthy claims
    are near the top anyway, and where an uncapped article would otherwise be
    the one call in the whole pipeline with no ceiling on its size. Extraction
    happens exactly once per article, so this number is the entire input cost of
    stage 1."""

    pipeline_concurrency: int = 4
    """Claims verified at once.

    Claims are worked concurrently so they stream in as they resolve — that
    progressive fill is the product's signature interaction, and it is why the
    ``claim`` events are allowed to arrive in any order. Bounded because
    ``max_claims`` claims times several providers each, all launched at once, would
    hit provider rate limits and give one reader's check the ability to starve
    everyone else's."""

    def require_openai_api_key(self, needed_for: str = "the OpenAI API") -> str:
        """Return :attr:`openai_api_key`, or raise :class:`MissingSettingError`.

        Call this at the point of use — building a client, making a call — never
        at import time, so that a deployment without a key still serves every
        route that does not need one (and so milestone-1's tests keep passing).
        """
        if not self.openai_api_key:
            raise MissingSettingError("OPENAI_API_KEY", needed_for)
        return self.openai_api_key

    def require_google_factcheck_api_key(
        self, needed_for: str = "the Google Fact Check Tools API"
    ) -> str:
        """Return :attr:`google_factcheck_api_key`, or raise :class:`MissingSettingError`.

        Retrieval is expected to *catch* this one and carry on with web search:
        a missing fact-check key makes checks more expensive, not impossible.
        """
        if not self.google_factcheck_api_key:
            raise MissingSettingError("GOOGLE_FACTCHECK_API_KEY", needed_for)
        return self.google_factcheck_api_key


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide :class:`Settings`, built once and cached.

    Tests that need different values should call ``get_settings.cache_clear()``
    after patching the environment, or construct ``Settings(...)`` directly.
    """
    return Settings()
