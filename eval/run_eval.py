#!/usr/bin/env python3
"""Run Re-Vera's pipeline over the golden set and report whether it is any good.

What this measures
------------------
Each line of ``eval/golden/*.jsonl`` is one **claim** with a gold verdict, not
an article, so the harness exercises stages 2-5 — ``retrieve`` → ``stance`` →
``judge`` → ``aggregate`` — on a claim that is handed to it already extracted.
Stage 1 (extraction) is deliberately *not* scored here: the thing extraction
gets right or wrong is *which* sentences it picks and whether it copies them
character for character, which a per-claim golden set cannot express. That
number needs an article-level golden set and is called out as missing in
``eval/README.md`` rather than quietly folded into these ones.

The gate
--------
``precision on contradicted >= 0.90``. Telling a reader that a true statement
has been contradicted is the worst thing this product can do, so it is the one
number that can fail the build. With ~30 claims it swings on single errors —
one false positive out of eight predictions is 0.875 — so the report prints the
whole confusion matrix and every per-verdict count beside it, and the README
says plainly that the gate is **directional, not a statistical guarantee**.

A run in which nothing at all was predicted ``contradicted`` does **not** pass:
precision is undefined on zero predictions, and letting an undefined number
clear a safety gate would make the safest way to pass it "never answer".

Two modes
---------
``--offline`` (the default, and what CI runs)
    Every provider answer and every model answer is replayed from
    ``eval/fixtures/``. No key, no network — a socket guard is installed before
    any work starts and any attempt to open a connection raises
    :class:`NetworkAccessDenied`. Deterministic and free.

    **What an offline number does and does not mean.** The recordings are
    hand-written, so an offline run measures the code that turns evidence into a
    verdict — retrieval's de-duplication and ranking, stance verification, the
    judge's citation check, and aggregation's rules — against evidence somebody
    chose. It is a regression test on *our* logic. It says nothing about whether
    a real model would have produced those answers. Only ``--live`` can say that.

``--live``
    Hits the real OpenAI and Google APIs, and therefore costs money. Requires
    ``OPENAI_API_KEY`` in ``backend/.env``. Run it deliberately, by hand.

``--record`` implies ``--live`` and writes what came back into
``eval/fixtures/`` so the next offline run replays it.

Nothing under ``backend/app`` is modified by this file; it only imports it.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import socket
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

EVAL_ROOT = Path(__file__).resolve().parent
REPO_ROOT = EVAL_ROOT.parent
BACKEND_ROOT = REPO_ROOT / "backend"

if str(BACKEND_ROOT) not in sys.path:  # pragma: no cover - import plumbing
    sys.path.insert(0, str(BACKEND_ROOT))

from app.config import MissingSettingError, Settings  # noqa: E402
from app.invariants import ALLOWED_VERDICTS, UNVERIFIABLE  # noqa: E402
from app.llm import LLMClient, LLMError, LLMResponse, LLMTransport  # noqa: E402
from app.pipeline.providers.base import HttpxClient, Providers, domain_of  # noqa: E402
from app.pipeline.retrieve import build_providers  # noqa: E402
from app.pipeline.run import LLMMeter, PipelineDeps, check_claim  # noqa: E402
from app.pipeline.stance import PASSAGE_CLOSE, passage_open  # noqa: E402
from app.pipeline.types import ClaimKind, ExtractedClaim, Passage  # noqa: E402
from app.schema_models import Stance  # noqa: E402

__all__ = [
    "BUCKET_ORIGIN",
    "CANNOT_RUN",
    "GATE_MIN_PRECISION",
    "GATE_VERDICT",
    "OFFLINE_PINNED",
    "VERDICTS",
    "ClaimFixture",
    "ClaimOutcome",
    "EvalError",
    "FixtureTransport",
    "Gate",
    "GoldenClaim",
    "NetworkAccessDenied",
    "Report",
    "VerdictScore",
    "abstention_rate",
    "build_report",
    "confusion_matrix",
    "eval_settings",
    "format_report",
    "install_network_guard",
    "load_fixtures",
    "load_golden",
    "main",
    "offline_deps",
    "run_golden_set",
    "stance_recording",
    "verdict_score",
]

VERDICTS: tuple[str, ...] = ("supported", "contradicted", "missing_context", "unverifiable")
"""The four verdicts, in the order every table in this file prints them.

Cross-checked against :data:`app.invariants.ALLOWED_VERDICTS` at import so a
fifth verdict (or a renamed one) fails here loudly instead of being silently
left out of the metrics."""

if set(VERDICTS) != ALLOWED_VERDICTS:  # pragma: no cover - a guard, not a branch
    raise RuntimeError(
        f"eval knows verdicts {sorted(VERDICTS)} but the backend allows "
        f"{sorted(ALLOWED_VERDICTS)}; update run_eval.VERDICTS."
    )

GATE_VERDICT = "contradicted"
"""The verdict whose precision can fail the build."""

GATE_MIN_PRECISION = 0.90
"""``CLAUDE.md``: precision on ``contradicted`` must be at least this."""

CANNOT_RUN = 2
"""Exit code for "the harness never ran", as against 1 for "the gate failed".

Worth its own code: a red build that measured nothing and a red build that
measured something bad need different responses, and a CI log is read in a
hurry."""

DEFAULT_GOLDEN = EVAL_ROOT / "golden" / "fictional.jsonl"
DEFAULT_FIXTURES = EVAL_ROOT / "fixtures"

OFFLINE_PINNED: tuple[str, ...] = ("max_passages_per_claim",)
"""Settings an offline run takes from ``app/config.py``, never the environment.

The one knob that changes an offline score: it caps the passages ``retrieve``
keeps, which decides what the judge sees and what ``aggregate`` counts. See
:func:`eval_settings`. Anything else a stage reads offline — the model ids, the
timeouts, the retry budget — cannot move a number that a fixture transport
answers, so it is left alone.
"""

BUCKET_ORIGIN: Mapping[str, str] = {
    "factcheck": "factcheck",
    "web": "web",
    "official": "official",
    "cited": "cited_source",
}
"""Fixture bucket name → :data:`app.pipeline.types.PassageOrigin`.

The bucket a passage sits in *is* its origin, so fixtures never repeat it. The
bucket also decides which provider replays it, which is what keeps the
ClaimReview short-circuit under test: a non-empty ``factcheck`` bucket means
``retrieve`` must never ask the ``web`` one.
"""

_SHORT_LABEL = {
    "supported": "sup",
    "contradicted": "con",
    "missing_context": "mis",
    "unverifiable": "unv",
}


class EvalError(RuntimeError):
    """The harness cannot run: a malformed golden row, or a missing recording.

    Distinct from a pipeline failure. This is always a bug in ``eval/``, and it
    stops the run rather than being folded into the metrics — a harness that
    scores itself on the claims it managed to load would report its own gaps as
    good news.
    """


class NetworkAccessDenied(RuntimeError):
    """Offline mode tried to open a socket. See :func:`install_network_guard`."""


# ---------------------------------------------------------------- the golden set


@dataclass(frozen=True, slots=True)
class GoldenClaim:
    """One labelled claim from ``eval/golden/*.jsonl``.

    ``start``/``end`` are synthetic unless the row states them: a golden row
    carries a quote, not the article it came from, so there is no text for a
    real offset to index into. They exist because
    :class:`~app.pipeline.types.ExtractedClaim` requires them and because
    ``aggregate`` copies them onto the wire claim; **nothing in this harness
    scores them**, and the anchoring contract they belong to
    (``docs/decisions.md`` §12) is milestone 3's to test.
    """

    id: str
    article_id: str
    article_url: str
    quote: str
    kind: ClaimKind
    gold_verdict: str
    gold_sources: tuple[str, ...]
    notes: str
    start: int
    end: int

    def extracted(self) -> ExtractedClaim:
        """The :class:`~app.pipeline.types.ExtractedClaim` stage 2 expects."""
        return ExtractedClaim(
            id=self.id,
            quote=self.quote,
            start=self.start,
            end=self.end,
            kind=self.kind,
            checkworthiness=1.0,
        )


def load_golden(path: Path) -> list[GoldenClaim]:
    """Read a golden JSONL file, rejecting anything malformed.

    Blank lines are skipped. A record carrying a ``_fictional`` key is
    *metadata*, not a claim: it is how the file states in machine-readable form
    that everything in it is invented, and :func:`load_golden` requires at least
    one before the first claim. A golden set that could be mistaken for real
    reporting is the one failure mode of this file that no metric would catch.
    """
    claims: list[GoldenClaim] = []
    seen_note = False
    seen_ids: set[str] = set()

    with path.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                row = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise EvalError(f"{path}:{number}: not valid JSON ({exc})") from exc
            if not isinstance(row, dict):
                raise EvalError(f"{path}:{number}: expected an object, got {type(row)}")
            if "_fictional" in row:
                seen_note = True
                continue
            if not seen_note:
                raise EvalError(
                    f"{path}:{number}: a claim appears before the file's `_fictional` "
                    f"note. The golden set must declare itself invented on its first line."
                )
            claim = _golden_row(row, path, number)
            if claim.id in seen_ids:
                raise EvalError(f"{path}:{number}: duplicate claim id {claim.id!r}")
            seen_ids.add(claim.id)
            claims.append(claim)

    if not claims:
        raise EvalError(f"{path}: no claims found.")
    return claims


def _golden_row(row: Mapping[str, Any], path: Path, number: int) -> GoldenClaim:
    """Validate one golden row into a :class:`GoldenClaim`."""

    def need(key: str) -> Any:
        if key not in row:
            raise EvalError(f"{path}:{number}: missing required key {key!r}")
        return row[key]

    verdict = str(need("gold_verdict"))
    if verdict not in ALLOWED_VERDICTS:
        raise EvalError(
            f"{path}:{number}: gold_verdict {verdict!r} is not one of {sorted(VERDICTS)}"
        )
    kind = str(need("kind"))
    if kind not in ("attribution", "numeric", "general"):
        raise EvalError(f"{path}:{number}: kind {kind!r} is not a ClaimKind")

    sources = need("gold_sources")
    if not isinstance(sources, list):
        raise EvalError(f"{path}:{number}: gold_sources must be a list")
    if verdict == UNVERIFIABLE and sources:
        raise EvalError(
            f"{path}:{number}: an unverifiable claim carries no sources "
            f"(CLAUDE.md rule 2), but {len(sources)} are listed"
        )
    if verdict != UNVERIFIABLE and not sources:
        raise EvalError(f"{path}:{number}: a {verdict!r} claim needs at least one gold source")

    quote = str(need("quote"))
    start = int(row.get("start", 0))
    end = int(row.get("end", start + len(quote)))

    return GoldenClaim(
        id=str(need("id")),
        article_id=str(need("article_id")),
        article_url=str(need("article_url")),
        quote=quote,
        kind=kind,  # type: ignore[arg-type]  # checked against ClaimKind above
        gold_verdict=verdict,
        gold_sources=tuple(str(item) for item in sources),
        notes=str(row.get("notes", "")),
        start=start,
        end=end,
    )




# ---------------------------------------------------------------- the recordings


@dataclass(frozen=True, slots=True)
class ClaimFixture:
    """Everything one claim's offline run replays: its evidence and its answers.

    ``buckets`` maps a :data:`BUCKET_ORIGIN` name to the passages that provider
    returns, so replay goes in at the **provider** seam rather than at the HTTP
    one. That is a deliberate choice with a cost worth stating: the providers'
    own parsing (Google's ClaimReview shape, the Responses API's annotations,
    data.gov.sg's CKAN payload) is **not** exercised here — that is what
    ``backend/tests/test_providers.py`` is for, with its own recorded HTTP. What
    it buys is a replay that does not depend on the order in which two
    *concurrent* provider calls happen to issue their requests, which a
    positional HTTP script does. Everything downstream of the providers —
    ``retrieve_passages``'s ClaimReview short-circuit, its wire-copy
    de-duplication, its ranking and its cap — is real code running on this
    evidence.

    ``stances`` is keyed by passage **text**, because that is the only thing the
    stance model is shown: :func:`stance_recording` reads the indices back out of
    the message the stage actually built, so a fixture never has to predict the
    order ``retrieve`` will return passages in, and a change to ranking cannot
    silently mis-align a recording against the wrong passage.

    ``judge`` is the raw :class:`~app.pipeline.judge.JudgeResponse` object. It is
    ``None`` for a claim whose retrieval finds nothing, because ``judge_claim``
    short-circuits without a model call there — and a recording for a call that
    never happens would be a fixture nobody could ever be wrong about.
    """

    claim_id: str
    buckets: Mapping[str, tuple[Passage, ...]]
    stances: Mapping[str, tuple[Stance, str]]
    judge: Mapping[str, Any] | None

    def passages(self) -> tuple[Passage, ...]:
        """Every recorded passage, in bucket order. For reporting only."""
        return tuple(item for bucket in BUCKET_ORIGIN for item in self.buckets.get(bucket, ()))


def load_fixtures(directory: Path) -> dict[str, ClaimFixture]:
    """Load every ``<article_id>.json`` recording under ``directory``.

    One file per article, each holding a ``claims`` object keyed by golden claim
    id. See ``eval/fixtures/README.md`` for the format and for why every byte of
    it is invented.
    """
    fixtures: dict[str, ClaimFixture] = {}
    for path in sorted(directory.glob("*.json")):
        with path.open(encoding="utf-8") as handle:
            payload = json.load(handle)
        if not isinstance(payload, dict):
            raise EvalError(f"{path}: expected an object at the top level")
        claims = payload.get("claims")
        if not isinstance(claims, dict):
            raise EvalError(f"{path}: no `claims` object")
        for claim_id, body in claims.items():
            if claim_id in fixtures:
                raise EvalError(f"{path}: claim id {claim_id!r} is recorded twice")
            fixtures[str(claim_id)] = _claim_fixture(str(claim_id), body, path)
    return fixtures


def _claim_fixture(claim_id: str, body: Any, path: Path) -> ClaimFixture:
    """Parse one claim's recording."""
    if not isinstance(body, dict):
        raise EvalError(f"{path}: recording for {claim_id!r} is not an object")

    buckets: dict[str, tuple[Passage, ...]] = {}
    stances: dict[str, tuple[Stance, str]] = {}

    for bucket, origin in BUCKET_ORIGIN.items():
        entries = body.get(bucket) or []
        if not isinstance(entries, list):
            raise EvalError(f"{path}: {claim_id}.{bucket} must be a list")
        passages: list[Passage] = []
        for entry in entries:
            if not isinstance(entry, dict):
                raise EvalError(f"{path}: {claim_id}.{bucket} holds a non-object")
            text = str(entry["text"])
            passages.append(
                Passage(
                    text=text,
                    url=str(entry["url"]),
                    outlet=str(entry.get("outlet", "")),
                    date=entry.get("date"),
                    wire=bool(entry.get("wire", False)),
                    origin=origin,  # type: ignore[arg-type]  # from BUCKET_ORIGIN
                    rating=entry.get("rating"),
                )
            )
            stances[text] = (
                Stance(str(entry.get("stance", "neutral"))),
                str(entry.get("quote", "")),
            )
        buckets[bucket] = tuple(passages)

    judge = body.get("judge")
    if judge is not None and not isinstance(judge, dict):
        raise EvalError(f"{path}: {claim_id}.judge must be an object or absent")

    return ClaimFixture(claim_id=claim_id, buckets=buckets, stances=stances, judge=judge)


def _passage_block_pattern() -> re.Pattern[str]:
    """A regex for stage 3's passage fence, derived from the stage's own helpers.

    Built from :func:`app.pipeline.stance.passage_open` rather than written out,
    so that changing the fence breaks this loudly here instead of quietly
    returning "no passages found" and scoring a whole run as neutral.
    """
    prefix, suffix = passage_open(0).split("0")
    return re.compile(
        re.escape(prefix) + r"(\d+)" + re.escape(suffix) + r"\n(.*?)\n" + re.escape(PASSAGE_CLOSE),
        re.DOTALL,
    )


_PASSAGE_BLOCK = _passage_block_pattern()


def stance_recording(user_content: str, stances: Mapping[str, tuple[Stance, str]]) -> str:
    """Build the :class:`~app.pipeline.stance.StanceResponse` JSON for one batch.

    ``user_content`` is the message stage 3 built, so the indices come from the
    stage rather than from a guess about its ordering. A passage with no recorded
    stance is answered ``neutral`` with no quote — exactly what ``score_passages``
    does with a passage the model skipped, so an incomplete fixture degrades to
    an abstention rather than to a made-up verdict.
    """
    scores = []
    for match in _PASSAGE_BLOCK.finditer(user_content):
        index, text = int(match.group(1)), match.group(2)
        stance, quote = stances.get(text, (Stance.neutral, ""))
        scores.append({"index": index, "stance": stance.value, "quote": quote})
    return json.dumps({"scores": scores}, ensure_ascii=False)


# ---------------------------------------------------------------- offline seams


@dataclass
class _FixtureSearchProvider:
    """Replays one bucket. Satisfies Search/FactCheck/OfficialData providers."""

    passages: tuple[Passage, ...]

    async def search(self, query: str, *, limit: int) -> list[Passage]:
        return list(self.passages[: max(0, limit)])


@dataclass
class _FixtureCitedProvider:
    """Replays the ``cited`` bucket for an ``attribution`` claim."""

    passages: tuple[Passage, ...]

    async def fetch(self, quote: str, *, article_url: str, limit: int) -> list[Passage]:
        return list(self.passages[: max(0, limit)])


@dataclass
class FixtureTransport:
    """An :class:`~app.llm.LLMTransport` that answers from a claim's recording.

    Dispatch is by ``schema_name``, not by call order. Two stages call the model
    at most once each, but either may short-circuit without calling at all
    (``score_passages`` on no passages, ``judge_claim`` on no scored passages),
    and a positional script would then hand the judge the stance recording. Every
    call is remembered in :attr:`calls` so a test can prove which stages ran.

    A missing recording is an :class:`EvalError`, never a silent fallback: an
    invented answer here would be an invented number in the report.
    """

    fixture: ClaimFixture
    calls: list[str] = field(default_factory=list)

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
        """Return the recorded answer for ``schema_name``."""
        self.calls.append(schema_name)
        if schema_name == "StanceResponse":
            return LLMResponse(
                content=stance_recording(user, self.fixture.stances),
                prompt_tokens=0,
                completion_tokens=0,
            )
        if schema_name == "JudgeResponse":
            if self.fixture.judge is None:
                raise EvalError(
                    f"claim {self.fixture.claim_id}: the judge was called but the fixture "
                    f"records no `judge` answer. Add one, or remove the claim's passages."
                )
            return LLMResponse(
                content=json.dumps(self.fixture.judge),
                prompt_tokens=0,
                completion_tokens=0,
            )
        raise EvalError(
            f"claim {self.fixture.claim_id}: no recording for schema {schema_name!r}. "
            f"A stage started asking the model something new; record it."
        )


def offline_deps(fixture: ClaimFixture, settings: Settings) -> PipelineDeps:
    """The :class:`~app.pipeline.run.PipelineDeps` one claim replays through.

    ``owned_http`` stays None — nothing is opened, so there is nothing to close —
    and ``meter`` stays None because a replay's token counts are zeros that would
    read as a real bill of nothing.
    """
    return PipelineDeps(
        llm=LLMClient(
            api_key="offline-eval-never-used",
            timeout=settings.llm_timeout_seconds,
            max_retries=0,
            transport=FixtureTransport(fixture),
            retry_base_delay=0.0,
        ),
        providers=Providers(
            factcheck=_FixtureSearchProvider(fixture.buckets.get("factcheck", ())),
            search=_FixtureSearchProvider(fixture.buckets.get("web", ())),
            official=_FixtureSearchProvider(fixture.buckets.get("official", ())),
            cited=_FixtureCitedProvider(fixture.buckets.get("cited", ())),
        ),
    )


def install_network_guard() -> Callable[[], None]:
    """Make every outbound socket raise :class:`NetworkAccessDenied`. Returns an undo.

    Offline mode's promise is "no network call", and the honest way to keep a
    promise like that is to make breaking it impossible rather than to read the
    code and hope. ``connect``, ``connect_ex`` and ``getaddrinfo`` are all
    stubbed, so a DNS lookup fails as loudly as a connection.
    """
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_getaddrinfo = socket.getaddrinfo

    def denied(*args: Any, **kwargs: Any) -> Any:
        raise NetworkAccessDenied(
            "offline eval attempted a network connection. Offline mode replays "
            "eval/fixtures and must never reach OpenAI, Google or anything else."
        )

    socket.socket.connect = denied  # type: ignore[method-assign]
    socket.socket.connect_ex = denied  # type: ignore[method-assign]
    socket.getaddrinfo = denied

    def undo() -> None:
        socket.socket.connect = real_connect  # type: ignore[method-assign]
        socket.socket.connect_ex = real_connect_ex  # type: ignore[method-assign]
        socket.getaddrinfo = real_getaddrinfo

    return undo


# ---------------------------------------------------------------- live recording


@dataclass
class _RecordingSearch:
    """Wraps a live passage provider and keeps what it returned, for ``--record``."""

    inner: Any
    captured: list[Passage]

    async def search(self, query: str, *, limit: int) -> list[Passage]:
        found: list[Passage] = await self.inner.search(query, limit=limit)
        self.captured.extend(found)
        return found


@dataclass
class _RecordingCited:
    """Wraps the live cited-source provider, for ``--record``."""

    inner: Any
    captured: list[Passage]

    async def fetch(self, quote: str, *, article_url: str, limit: int) -> list[Passage]:
        found: list[Passage] = await self.inner.fetch(quote, article_url=article_url, limit=limit)
        self.captured.extend(found)
        return found


@dataclass
class _RecordingTransport:
    """Wraps a live transport and keeps the question and the answer, for ``--record``."""

    inner: LLMTransport
    seen: dict[str, tuple[str, str]] = field(default_factory=dict)

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
        response = await self.inner.complete(
            model=model,
            system=system,
            user=user,
            schema_name=schema_name,
            json_schema=json_schema,
            timeout=timeout,
        )
        self.seen[schema_name] = (user, response.content)
        return response


@dataclass
class _Recorder:
    """Collects one live claim's provider answers and model answers.

    Providers are recorded **before** de-duplication and ranking, which is the
    higher-fidelity place: an offline replay then re-runs those steps on the same
    input the live run had, instead of on their own output.
    """

    buckets: dict[str, list[Passage]] = field(default_factory=dict)
    transport: _RecordingTransport | None = None

    def wrap(self, providers: Providers) -> Providers:
        """Return ``providers`` with every call recorded into :attr:`buckets`."""
        for bucket in BUCKET_ORIGIN:
            self.buckets.setdefault(bucket, [])
        return Providers(
            factcheck=_RecordingSearch(providers.factcheck, self.buckets["factcheck"]),
            search=_RecordingSearch(providers.search, self.buckets["web"]),
            official=_RecordingSearch(providers.official, self.buckets["official"]),
            cited=_RecordingCited(providers.cited, self.buckets["cited"]),
            timeout_seconds=providers.timeout_seconds,
        )

    def stances(self) -> dict[str, tuple[str, str]]:
        """``passage text -> (stance, quote)``, read back out of the live answer.

        The recorded answer is keyed by index; the message that produced it names
        the text each index stood for. Joining the two here is what lets a
        fixture be written in terms a person can check.
        """
        if self.transport is None or "StanceResponse" not in self.transport.seen:
            return {}
        user, content = self.transport.seen["StanceResponse"]
        by_index = {
            int(match.group(1)): match.group(2) for match in _PASSAGE_BLOCK.finditer(user)
        }
        try:
            scores = json.loads(content).get("scores", [])
        except (json.JSONDecodeError, AttributeError):
            return {}
        out: dict[str, tuple[str, str]] = {}
        for score in scores:
            text = by_index.get(int(score.get("index", 0)))
            if text is not None:
                out[text] = (str(score.get("stance", "neutral")), str(score.get("quote", "")))
        return out

    def judge(self) -> dict[str, Any] | None:
        """The live ``JudgeResponse``, or None when the judge was never called."""
        if self.transport is None or "JudgeResponse" not in self.transport.seen:
            return None
        try:
            parsed: dict[str, Any] = json.loads(self.transport.seen["JudgeResponse"][1])
        except json.JSONDecodeError:
            return None
        return parsed

    def to_fixture(self) -> dict[str, Any]:
        """This claim's recording, in ``eval/fixtures`` format."""
        stances = self.stances()
        body: dict[str, Any] = {}
        for bucket, passages in self.buckets.items():
            if passages:
                body[bucket] = [_passage_entry(item, stances) for item in passages]
        judge = self.judge()
        if judge is not None:
            body["judge"] = judge
        return body


def _passage_entry(passage: Passage, stances: Mapping[str, tuple[str, str]]) -> dict[str, Any]:
    """One passage as a fixture entry, with defaulted fields left out."""
    stance, quote = stances.get(passage.text, ("neutral", ""))
    entry: dict[str, Any] = {"outlet": passage.outlet, "url": passage.url}
    if passage.date is not None:
        entry["date"] = passage.date
    if passage.wire:
        entry["wire"] = True
    if passage.rating is not None:
        entry["rating"] = passage.rating
    if stance != "neutral" or quote:
        entry["stance"] = stance
        entry["quote"] = quote
    entry["text"] = passage.text
    return entry


RECORDING_NOTE = (
    "RECORDED FOR OFFLINE REPLAY. Every article, outlet, URL, figure and "
    "quotation reachable from this file belongs to Re-Vera's fictional golden "
    "set; none of it is real reporting and none of it may be presented as such. "
    "See eval/fixtures/README.md."
)


def write_recordings(outcomes: Sequence[ClaimOutcome], directory: Path) -> list[Path]:
    """Write one ``<article_id>.json`` per article. Returns the paths written."""
    by_article: dict[str, dict[str, Any]] = {}
    for outcome in outcomes:
        if outcome.recording is None:
            continue
        by_article.setdefault(outcome.golden.article_id, {})[outcome.golden.id] = (
            outcome.recording
        )

    directory.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for article_id, claims in sorted(by_article.items()):
        path = directory / f"{article_id}.json"
        payload = {"_fictional": RECORDING_NOTE, "claims": claims}
        with path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        written.append(path)
    return written


# ---------------------------------------------------------------- running the set


@dataclass(frozen=True, slots=True)
class ClaimOutcome:
    """What the pipeline said about one golden claim, or why it could not."""

    golden: GoldenClaim
    claim: dict[str, Any] | None
    error: str | None = None
    recording: dict[str, Any] | None = None

    @property
    def predicted(self) -> str | None:
        """The verdict the pipeline reached, or None when the claim errored."""
        if self.claim is None:
            return None
        return str(self.claim["verdict"])

    @property
    def source_domains(self) -> frozenset[str]:
        """Domains of the sources the pipeline cited."""
        if self.claim is None:
            return frozenset()
        return frozenset(
            domain_of(str(source["url"])) for source in self.claim.get("sources", [])
        )


async def run_golden_set(
    golden: Sequence[GoldenClaim],
    *,
    live: bool,
    fixtures: Mapping[str, ClaimFixture],
    settings: Settings,
    record: bool = False,
    meter: LLMMeter | None = None,
) -> list[ClaimOutcome]:
    """Run every golden claim, bounded by ``settings.pipeline_concurrency``.

    Results come back in golden-set order however the coroutines finished, so
    two runs of the same fixtures produce byte-identical reports.

    A claim that raises is recorded as an error and **excluded from the
    metrics** rather than counted as ``unverifiable``. Scoring a claim nobody
    managed to check as an honest abstention would let an outage read as
    caution, and the abstention rate is one of the numbers this report exists to
    show. :class:`EvalError` is never caught: a missing recording is a hole in
    the harness, not a finding about the pipeline.
    """
    slots: list[ClaimOutcome | None] = [None] * len(golden)
    semaphore = asyncio.Semaphore(max(1, settings.pipeline_concurrency))
    live_deps: PipelineDeps | None = None
    live_transport: LLMTransport | None = None
    api_key = ""

    if live:
        # Imported here so an offline run never even reaches the SDK code path.
        from app.llm import build_openai_transport

        api_key = settings.require_openai_api_key("the eval harness in --live mode")
        meter = meter if meter is not None else LLMMeter()
        http = HttpxClient()
        live_transport = meter.instrument(
            build_openai_transport(api_key, settings.llm_timeout_seconds)
        )
        live_deps = PipelineDeps(
            llm=LLMClient(
                api_key=api_key,
                timeout=settings.llm_timeout_seconds,
                max_retries=settings.llm_max_retries,
                transport=live_transport,
            ),
            providers=build_providers(settings, http=http),
            meter=meter,
            owned_http=http,
        )

    async def one(position: int, item: GoldenClaim) -> None:
        async with semaphore:
            recorder: _Recorder | None = None
            if live:
                assert live_deps is not None and live_transport is not None
                deps = live_deps
                if record:
                    # One recorder per claim, so two claims running concurrently
                    # cannot write into each other's fixture.
                    recording = _RecordingTransport(live_transport)
                    recorder = _Recorder(transport=recording)
                    deps = PipelineDeps(
                        llm=LLMClient(
                            api_key=api_key,
                            timeout=settings.llm_timeout_seconds,
                            max_retries=settings.llm_max_retries,
                            transport=recording,
                        ),
                        providers=recorder.wrap(live_deps.providers),
                    )
            else:
                fixture = fixtures.get(item.id)
                if fixture is None:
                    raise EvalError(
                        f"claim {item.id!r} has no recording under eval/fixtures. "
                        f"Add one, or run with --live --record."
                    )
                deps = offline_deps(fixture, settings)

            try:
                claim = await check_claim(
                    item.extracted(),
                    article_url=item.article_url,
                    settings=settings,
                    deps=deps,
                )
            except EvalError:
                raise
            except (LLMError, ValueError, OSError) as exc:
                slots[position] = ClaimOutcome(
                    golden=item, claim=None, error=f"{type(exc).__name__}: {exc}"
                )
                return
            slots[position] = ClaimOutcome(
                golden=item,
                claim=claim,
                recording=None if recorder is None else recorder.to_fixture(),
            )

    try:
        await asyncio.gather(*(one(index, item) for index, item in enumerate(golden)))
    finally:
        if live_deps is not None:
            await live_deps.aclose()

    return [outcome for outcome in slots if outcome is not None]


# ---------------------------------------------------------------- metrics

# The arithmetic below is the part of this harness most likely to be quietly
# wrong, so it is written as small pure functions over a confusion matrix and
# tested against a hand-computed one in eval/tests/test_run_eval.py. The
# zero-division cases are the ones that matter: a verdict that was never
# predicted has *undefined* precision, not perfect precision, and a verdict that
# never appears in the golden set has undefined recall. Both come back None and
# print as "—". Reporting 1.000 for either would make the two failure modes we
# most need to see — a pipeline that never says `contradicted`, and a golden set
# missing a verdict entirely — look like the best possible result.


def confusion_matrix(pairs: Iterable[tuple[str, str]]) -> dict[str, dict[str, int]]:
    """Count ``(gold, predicted)`` pairs into ``matrix[gold][predicted]``."""
    matrix = {gold: dict.fromkeys(VERDICTS, 0) for gold in VERDICTS}
    for gold, predicted in pairs:
        if gold not in matrix:
            raise EvalError(f"unknown gold verdict {gold!r}")
        if predicted not in matrix[gold]:
            raise EvalError(f"unknown predicted verdict {predicted!r}")
        matrix[gold][predicted] += 1
    return matrix


@dataclass(frozen=True, slots=True)
class VerdictScore:
    """Per-verdict counts and metrics. ``None`` means undefined, never zero."""

    verdict: str
    gold: int
    predicted: int
    true_positive: int
    precision: float | None
    recall: float | None
    f1: float | None


def verdict_score(matrix: Mapping[str, Mapping[str, int]], verdict: str) -> VerdictScore:
    """Score one verdict out of a confusion matrix."""
    true_positive = matrix[verdict][verdict]
    gold = sum(matrix[verdict].values())
    predicted = sum(row[verdict] for row in matrix.values())
    precision = None if predicted == 0 else true_positive / predicted
    recall = None if gold == 0 else true_positive / gold
    f1: float | None = None
    if precision is not None and recall is not None and (precision + recall) > 0:
        f1 = 2 * precision * recall / (precision + recall)
    return VerdictScore(
        verdict=verdict,
        gold=gold,
        predicted=predicted,
        true_positive=true_positive,
        precision=precision,
        recall=recall,
        f1=f1,
    )


def abstention_rate(predicted: Sequence[str]) -> float:
    """Share of scored claims answered ``unverifiable``. 0.0 on an empty run."""
    if not predicted:
        return 0.0
    return sum(1 for verdict in predicted if verdict == UNVERIFIABLE) / len(predicted)


@dataclass(frozen=True, slots=True)
class Gate:
    """The one metric allowed to fail the build.

    ``value`` is None when nothing was predicted for :attr:`metric`'s verdict,
    and that **does not pass**: precision is undefined on zero predictions, and a
    gate an empty answer can clear is not a gate.
    """

    verdict: str
    threshold: float
    value: float | None
    predictions: int

    @property
    def passed(self) -> bool:
        return self.value is not None and self.value >= self.threshold


@dataclass(frozen=True, slots=True)
class Report:
    """Everything one run found, ready to print or serialise."""

    mode: str
    golden_path: Path
    total: int
    scored: tuple[ClaimOutcome, ...]
    errored: tuple[ClaimOutcome, ...]
    matrix: Mapping[str, Mapping[str, int]]
    scores: tuple[VerdictScore, ...]
    abstention: float
    source_hits: int
    source_checked: int
    gate: Gate
    cost: Mapping[str, int] | None = None
    """What the run spent at the LLM. ``None`` offline, where a replay's zeros
    would read as a real bill of nothing rather than as no bill at all."""

    @property
    def ok(self) -> bool:
        """True when the gate passed and every claim was actually checked."""
        return self.gate.passed and not self.errored

    def to_json(self) -> dict[str, Any]:
        """The machine-readable summary."""
        return {
            "mode": self.mode,
            "golden": str(self.golden_path),
            "claims": {
                "total": self.total,
                "scored": len(self.scored),
                "errored": len(self.errored),
            },
            "gate": {
                "metric": f"precision on {self.gate.verdict}",
                "threshold": self.gate.threshold,
                "value": self.gate.value,
                "predictions": self.gate.predictions,
                "passed": self.gate.passed,
            },
            "cost": None if self.cost is None else dict(self.cost),
            "abstention_rate": self.abstention,
            "source_hit_rate": (
                None if self.source_checked == 0 else self.source_hits / self.source_checked
            ),
            "source_checked": self.source_checked,
            "per_verdict": {
                score.verdict: {
                    "gold": score.gold,
                    "predicted": score.predicted,
                    "true_positive": score.true_positive,
                    "precision": score.precision,
                    "recall": score.recall,
                    "f1": score.f1,
                }
                for score in self.scores
            },
            "confusion": {gold: dict(row) for gold, row in self.matrix.items()},
            "claims_detail": [
                {
                    "id": outcome.golden.id,
                    "article_id": outcome.golden.article_id,
                    "gold": outcome.golden.gold_verdict,
                    "predicted": outcome.predicted,
                    "correct": outcome.predicted == outcome.golden.gold_verdict,
                    "sources": sorted(outcome.source_domains),
                    "gold_sources": sorted(
                        domain_of(url) for url in outcome.golden.gold_sources
                    ),
                }
                for outcome in self.scored
            ],
            "errors": [
                {"id": outcome.golden.id, "error": outcome.error} for outcome in self.errored
            ],
        }


def build_report(
    outcomes: Sequence[ClaimOutcome],
    *,
    mode: str,
    golden_path: Path,
    cost: Mapping[str, int] | None = None,
) -> Report:
    """Turn a run's outcomes into a :class:`Report`."""
    scored = tuple(item for item in outcomes if item.claim is not None)
    errored = tuple(item for item in outcomes if item.claim is None)

    matrix = confusion_matrix(
        (item.golden.gold_verdict, str(item.predicted)) for item in scored
    )
    scores = tuple(verdict_score(matrix, verdict) for verdict in VERDICTS)
    gate_score = next(score for score in scores if score.verdict == GATE_VERDICT)

    # Source credit is only meaningful where both sides name sources, so an
    # `unverifiable` on either side (which carries none, by rule 2) is excluded
    # rather than counted as a miss.
    source_checked = 0
    source_hits = 0
    for item in scored:
        if item.golden.gold_verdict == UNVERIFIABLE or item.predicted == UNVERIFIABLE:
            continue
        source_checked += 1
        wanted = {domain_of(url) for url in item.golden.gold_sources}
        if wanted & item.source_domains:
            source_hits += 1

    return Report(
        mode=mode,
        golden_path=golden_path,
        total=len(outcomes),
        scored=scored,
        errored=errored,
        matrix=matrix,
        scores=scores,
        abstention=abstention_rate([str(item.predicted) for item in scored]),
        source_hits=source_hits,
        source_checked=source_checked,
        cost=cost,
        gate=Gate(
            verdict=GATE_VERDICT,
            threshold=GATE_MIN_PRECISION,
            value=gate_score.precision,
            predictions=gate_score.predicted,
        ),
    )


# ---------------------------------------------------------------- the report


def _rate(value: float | None) -> str:
    """Format a metric, or an em dash when it is undefined."""
    return "  —  " if value is None else f"{value:.3f}"


def format_report(report: Report) -> str:
    """The human-readable report: per-verdict table, confusion matrix, gate."""
    lines: list[str] = []
    lines.append(f"Re-Vera eval — {report.mode} — {report.golden_path.name}")
    lines.append("")
    lines.append(
        f"{'verdict':<17}{'gold':>5}{'pred':>6}{'hit':>5}"
        f"{'precision':>11}{'recall':>9}{'f1':>8}"
    )
    lines.append("-" * 61)
    for score in report.scores:
        lines.append(
            f"{score.verdict:<17}{score.gold:>5}{score.predicted:>6}"
            f"{score.true_positive:>5}{_rate(score.precision):>11}"
            f"{_rate(score.recall):>9}{_rate(score.f1):>8}"
        )
    lines.append("")

    header = "".join(f"{_SHORT_LABEL[verdict]:>7}" for verdict in VERDICTS)
    lines.append("confusion (rows = gold, columns = predicted)")
    lines.append(f"{'':<17}{header}")
    for gold in VERDICTS:
        row = "".join(f"{report.matrix[gold][pred]:>7}" for pred in VERDICTS)
        lines.append(f"{gold:<17}{row}")
    lines.append("")

    lines.append(f"claims scored     {len(report.scored)} of {report.total}")
    unverifiable = sum(1 for item in report.scored if item.predicted == UNVERIFIABLE)
    lines.append(
        f"abstention rate   {report.abstention:.3f} "
        f"({unverifiable} of {len(report.scored)} answered unverifiable)"
    )
    if report.source_checked:
        rate = report.source_hits / report.source_checked
        lines.append(
            f"source hit rate   {rate:.3f} "
            f"({report.source_hits} of {report.source_checked} decided claims cite "
            f"a gold source's domain)"
        )
    if report.cost is not None:
        lines.append(
            f"llm calls         {report.cost['calls']} "
            f"({report.cost['prompt_tokens']} prompt + "
            f"{report.cost['completion_tokens']} completion tokens)"
        )
    if report.errored:
        lines.append("")
        lines.append(f"ERRORS ({len(report.errored)}) — these claims were not scored:")
        for item in report.errored:
            lines.append(f"  {item.golden.id:<16}{item.error}")

    lines.append("")
    verdict_word = "PASS" if report.gate.passed else "FAIL"
    detail = (
        "nothing was predicted contradicted, so precision is undefined"
        if report.gate.value is None
        else f"{report.gate.value:.3f} over {report.gate.predictions} prediction(s)"
    )
    lines.append(
        f"GATE  precision on `{report.gate.verdict}` >= "
        f"{report.gate.threshold:.2f}   {verdict_word}  ({detail})"
    )
    lines.append(
        "      With a golden set this size the gate swings on single errors. "
        "It is directional,"
    )
    lines.append(
        "      not a statistical guarantee — read the confusion matrix above it."
    )
    return "\n".join(lines)


# ---------------------------------------------------------------- the CLI


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="run_eval.py",
        description=(
            "Run Re-Vera's claim-checking pipeline over the golden set and report "
            "per-verdict precision/recall, the abstention rate and the "
            "contradicted-precision gate."
        ),
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--offline",
        action="store_true",
        help="replay eval/fixtures; no key, no network, no spend (the default)",
    )
    mode.add_argument(
        "--live",
        action="store_true",
        help="call the real OpenAI and Google APIs. Costs money. Needs OPENAI_API_KEY.",
    )
    parser.add_argument(
        "--record",
        action="store_true",
        help="implies --live: write what came back into --fixtures for later replay",
    )
    parser.add_argument(
        "--golden", type=Path, default=DEFAULT_GOLDEN, help="golden JSONL (default: %(default)s)"
    )
    parser.add_argument(
        "--fixtures",
        type=Path,
        default=DEFAULT_FIXTURES,
        help="recording directory (default: %(default)s)",
    )
    parser.add_argument(
        "--json-out", type=Path, default=None, help="also write the JSON summary to this path"
    )
    parser.add_argument(
        "--no-json", action="store_true", help="do not print the JSON summary to stdout"
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the harness and return the process exit code.

    ``0`` — every claim was checked and the gate passed.
    ``1`` — the run happened and something is wrong with it: the gate failed, or
    a claim errored. A run that could not check six of its thirty claims has not
    shown that the gate holds; it has shown that six claims are missing from the
    number printed beside it, so that is a failure too.
    ``2`` — the harness could not run at all (a malformed golden set, a missing
    recording, ``--live`` with no key). Nothing was measured, and that is a
    different thing from measuring something bad.
    """
    args = _parse_args(argv)
    live = args.live or args.record
    mode = "live" if live else "offline"

    undo_guard: Callable[[], None] | None = None
    try:
        golden = load_golden(args.golden)
        fixtures = {} if live else load_fixtures(args.fixtures)
        settings = eval_settings(live=live)
        if not live:
            undo_guard = install_network_guard()
        meter = LLMMeter() if live else None
        outcomes = asyncio.run(
            run_golden_set(
                golden,
                live=live,
                fixtures=fixtures,
                settings=settings,
                record=args.record,
                meter=meter,
            )
        )
    except (EvalError, MissingSettingError) as exc:
        # A harness that cannot run has not measured anything, so it says so in
        # one line and exits 2 — distinct from 1, which means the run happened
        # and the gate failed. A traceback here would bury a message written for
        # the person setting the project up.
        print(f"eval could not run: {exc}", file=sys.stderr)
        return CANNOT_RUN
    finally:
        if undo_guard is not None:
            undo_guard()

    cost = (
        None
        if meter is None
        else {
            "calls": meter.calls,
            "prompt_tokens": meter.prompt_tokens,
            "completion_tokens": meter.completion_tokens,
        }
    )
    report = build_report(outcomes, mode=mode, golden_path=args.golden, cost=cost)
    print(format_report(report))

    summary = report.to_json()
    if not args.no_json:
        print()
        print("--- json summary ---")
        print(json.dumps(summary, indent=2))
    if args.json_out is not None:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        with args.json_out.open("w", encoding="utf-8") as handle:
            json.dump(summary, handle, indent=2)
            handle.write("\n")

    if args.record:
        written = write_recordings(outcomes, args.fixtures)
        print()
        print(f"recorded {len(written)} article file(s) into {args.fixtures}")

    return 0 if report.ok else 1


def eval_settings(*, live: bool) -> Settings:
    """Settings for a run.

    An offline run is built with ``_env_file=None`` so a developer's local
    ``backend/.env`` cannot change a CI number; a live run reads it, because that
    is where the keys are. ``_env_file`` is a pydantic-settings runtime keyword
    its stubs do not declare, hence the one narrow ignore.

    ``_env_file=None`` closes only half of that promise: pydantic-settings still
    reads the **process environment**, and every field in :data:`OFFLINE_PINNED`
    changes what an offline run scores. ``MAX_PASSAGES_PER_CLAIM=1`` in a shell
    moves this golden set from an abstention rate of 0.219 to 0.531 and drops
    ``missing_context`` recall from 0.833 to 0.167 — a number nobody would think
    to distrust, since nothing in the report would mention the variable. Offline
    mode therefore pins those fields back to the defaults declared in
    ``app/config.py``, so an offline run measures the pipeline and the fixtures
    and nothing else. ``--live`` pins nothing: a live run is deliberate, and
    should use the configuration it is deliberately pointed at.
    """
    if live:
        return Settings()
    pinned = {name: Settings.model_fields[name].default for name in OFFLINE_PINNED}
    return Settings(_env_file=None, **pinned)  # type: ignore[call-arg]


if __name__ == "__main__":  # pragma: no cover - entry point
    raise SystemExit(main())
