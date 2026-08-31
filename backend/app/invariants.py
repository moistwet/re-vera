"""The product's data rules, as code rather than prose.

``shared/schema.json`` can say what a claim's *fields* look like, but two of the
non-negotiable rules in ``CLAUDE.md`` are relationships *between* fields, and
JSON Schema (as we generate it) cannot express them:

* **Rule 3 / decision 4** — ``confidence`` is ``null`` **if and only if**
  ``verdict`` is ``"unverifiable"``.
* **Rule 2 / decision 5** — ``sources`` is ``[]`` **if and only if** ``verdict``
  is ``"unverifiable"``.

Until this module existed those two rules lived only in ``description`` strings,
so :class:`app.schema_models.Claim` cheerfully accepted an ``unverifiable``
claim carrying a confidence and three sources — and the moment the milestone-2
judge emitted one it would have streamed to a reader with nothing failing.

This file is **hand-written on purpose**. ``app/schema_models.py`` is generated
by ``shared/generate.sh`` and any edit to it is erased on the next run, so the
checks that the generator cannot produce live here instead and are called from
the pipeline: once when the fixture is loaded, and once more immediately before
each claim is published, so a violating claim can never reach the wire.

The functions take either a :class:`~app.schema_models.Claim` or the plain dict
form that travels on the SSE stream, because both shapes exist in the pipeline
(claims are validated as models, then dumped to JSON dicts for publishing).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from app.schema_models import Claim, Confidence, Verdict

__all__ = [
    "ALLOWED_CONFIDENCES",
    "ALLOWED_VERDICTS",
    "UNVERIFIABLE",
    "ClaimInvariantError",
    "validate_claim",
    "validate_claims",
]

ALLOWED_VERDICTS = frozenset(verdict.value for verdict in Verdict)
"""The only four verdicts that exist: supported, contradicted, missing_context,
unverifiable. Never TRUE/FALSE, never "flagged"."""

ALLOWED_CONFIDENCES = frozenset(confidence.value for confidence in Confidence)
"""``low``/``medium``/``high`` — never a percentage (rule 3)."""

UNVERIFIABLE = Verdict.unverifiable.value
"""The one verdict that carries no confidence and no sources."""

_UNKNOWN_ID = "<no id>"
"""Stand-in used in error messages when the claim has no usable ``id``."""


class ClaimInvariantError(ValueError):
    """A claim breaks one of the product's data rules.

    A :class:`ValueError`, so a caller that already guards a claim with
    ``except ValueError`` (the shape ``pydantic.ValidationError`` also takes)
    catches this too. ``claim_id`` is kept as an attribute for logging: it is an
    opaque per-job id like ``"c3"`` and carries no article text, so it is safe to
    log where the claim itself is not.
    """

    def __init__(self, claim_id: str, problem: str) -> None:
        self.claim_id = claim_id
        """The offending claim's id, or ``"<no id>"`` if it had none."""
        self.problem = problem
        """What is wrong, as a sentence, without quoting the article."""
        super().__init__(f"claim {claim_id}: {problem}")


def validate_claim(claim: Claim | Mapping[str, Any]) -> None:
    """Raise :class:`ClaimInvariantError` unless ``claim`` obeys the rules.

    Checks, in order: the verdict is one of the four; ``confidence`` is null iff
    the verdict is ``unverifiable`` (and is one of the three levels otherwise);
    ``sources`` is empty iff the verdict is ``unverifiable``.

    Nothing else about the claim is re-checked — offsets, quotes and the trail
    are the generated model's and :mod:`tests.test_schema`'s job. This function
    is deliberately cheap enough to run on every claim on its way to the wire.
    """
    claim_id, verdict, confidence, sources = _read(claim)

    if verdict not in ALLOWED_VERDICTS:
        raise ClaimInvariantError(
            claim_id,
            f"verdict {verdict!r} is not one of {sorted(ALLOWED_VERDICTS)}",
        )

    unverifiable = verdict == UNVERIFIABLE

    if unverifiable and confidence is not None:
        raise ClaimInvariantError(
            claim_id,
            f"an unverifiable verdict carries no confidence, but confidence is {confidence!r}",
        )
    if not unverifiable and confidence is None:
        raise ClaimInvariantError(
            claim_id,
            f"confidence is null, which only an unverifiable verdict may be — not {verdict!r}",
        )
    if confidence is not None and confidence not in ALLOWED_CONFIDENCES:
        raise ClaimInvariantError(
            claim_id,
            f"confidence {confidence!r} is not one of {sorted(ALLOWED_CONFIDENCES)}",
        )

    if unverifiable and sources:
        raise ClaimInvariantError(
            claim_id,
            f"an unverifiable verdict carries no sources, but {len(sources)} were given",
        )
    if not unverifiable and not sources:
        raise ClaimInvariantError(
            claim_id,
            f"a {verdict!r} verdict needs at least one source; none were given",
        )


def validate_claims(claims: Iterable[Claim | Mapping[str, Any]]) -> None:
    """Run :func:`validate_claim` over a whole list, failing on the first breach."""
    for claim in claims:
        validate_claim(claim)


def _read(claim: Claim | Mapping[str, Any]) -> tuple[str, str, str | None, Sequence[Any]]:
    """Pull ``(id, verdict, confidence, sources)`` out of either supported shape.

    Values come back as plain strings (``Verdict`` and ``Confidence`` are
    ``StrEnum``s, and the dicts on the wire hold their string form), so the rest
    of the module compares one type rather than two.
    """
    if isinstance(claim, Claim):
        return (
            claim.id,
            claim.verdict.value,
            None if claim.confidence is None else claim.confidence.value,
            claim.sources,
        )

    if not isinstance(claim, Mapping):
        raise ClaimInvariantError(_UNKNOWN_ID, f"expected a Claim or a mapping, got {type(claim)}")

    raw_id = claim.get("id")
    claim_id = raw_id if isinstance(raw_id, str) and raw_id else _UNKNOWN_ID

    if "verdict" not in claim:
        raise ClaimInvariantError(claim_id, "has no verdict")
    verdict = claim["verdict"]
    if not isinstance(verdict, str):
        raise ClaimInvariantError(claim_id, f"verdict must be a string, got {type(verdict)}")

    # A missing key is a breach in its own right: both fields are required by the
    # schema, and treating "absent" as "null" would let a claim slip past the
    # confidence rule by simply omitting the field.
    if "confidence" not in claim:
        raise ClaimInvariantError(claim_id, "has no confidence field (it is required, if nullable)")
    confidence = claim["confidence"]
    if confidence is not None and not isinstance(confidence, str):
        raise ClaimInvariantError(
            claim_id, f"confidence must be a string or null, got {type(confidence)}"
        )

    if "sources" not in claim:
        raise ClaimInvariantError(claim_id, "has no sources field")
    sources = claim["sources"]
    if not isinstance(sources, Sequence) or isinstance(sources, str | bytes):
        raise ClaimInvariantError(claim_id, f"sources must be a list, got {type(sources)}")

    return claim_id, str(verdict), confidence, sources
