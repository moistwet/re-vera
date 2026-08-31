"""The two cross-field product rules, enforced in code.

``CLAUDE.md`` rules 2 and 3 (and decisions 4 and 5) say that a claim's
``confidence`` is null **if and only if** its verdict is ``unverifiable``, and
that its ``sources`` are empty **if and only if** its verdict is
``unverifiable``. Neither is expressible in the JSON Schema the models are
generated from, so both lived only in ``description`` strings until
:mod:`app.invariants` existed — meaning ``verdict="unverifiable"`` alongside
``confidence="high"`` and three sources validated cleanly and would have
streamed to a reader.

Four legal shapes and four illegal ones are asserted here, plus the equivalence
of the two input forms (a :class:`~app.schema_models.Claim` and the plain dict
that travels on the SSE stream), because the pipeline validates both.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.invariants import (
    ALLOWED_CONFIDENCES,
    ALLOWED_VERDICTS,
    ClaimInvariantError,
    validate_claim,
    validate_claims,
)
from app.schema_models import Claim, Confidence, Verdict

SOURCE: dict[str, Any] = {
    "url": "https://example.com/re-vera-fixture/fictional-outlet/story",
    "outlet": "Example Outlet",
    "date": "2026-03-12",
    "wire": False,
    "stance": "supports",
}
"""A synthetic source; nothing here resolves anywhere real."""

EVIDENCED_VERDICTS = ["supported", "contradicted", "missing_context"]
"""The three verdicts that must ship evidence (rule 2)."""


def claim(**overrides: Any) -> dict[str, Any]:
    """A legal ``supported`` claim in dict form, with fields overridden."""
    payload: dict[str, Any] = {
        "id": "c1",
        "quote": "q",
        "start": 0,
        "end": 1,
        "verdict": "supported",
        "confidence": "high",
        "evidence": "e",
        "sources": [SOURCE],
        "trail": [],
    }
    payload.update(overrides)
    return payload


# ------------------------------------------------------------- the legal shapes


@pytest.mark.parametrize("verdict", EVIDENCED_VERDICTS)
@pytest.mark.parametrize("confidence", sorted(ALLOWED_CONFIDENCES))
def test_an_evidenced_verdict_with_confidence_and_a_source_is_legal(
    verdict: str, confidence: str
) -> None:
    """supported / contradicted / missing_context: a confidence and ≥1 source."""
    validate_claim(claim(verdict=verdict, confidence=confidence))


def test_an_unverifiable_claim_with_no_confidence_and_no_sources_is_legal() -> None:
    """The fourth shape: no confidence, no sources, just the explanation."""
    validate_claim(claim(verdict="unverifiable", confidence=None, sources=[]))


def test_the_four_verdicts_are_the_only_ones_the_module_knows() -> None:
    """No fifth verdict sneaks in, and the set matches the generated enum."""
    assert set(ALLOWED_VERDICTS) == {
        "supported",
        "contradicted",
        "missing_context",
        "unverifiable",
    }
    assert set(ALLOWED_VERDICTS) == {verdict.value for verdict in Verdict}
    assert set(ALLOWED_CONFIDENCES) == {confidence.value for confidence in Confidence}


# ----------------------------------------------------------- the four breaches


def test_an_unverifiable_claim_may_not_carry_a_confidence() -> None:
    """Breach 1 — ``unverifiable`` with a confidence. The UI hides the meter for
    these claims, so a confidence here is a number nobody would ever see."""
    with pytest.raises(ClaimInvariantError) as excinfo:
        validate_claim(claim(verdict="unverifiable", confidence="high", sources=[]))
    assert "confidence" in str(excinfo.value)


def test_an_unverifiable_claim_may_not_carry_sources() -> None:
    """Breach 2 — ``unverifiable`` with sources (decision 5: it carries none)."""
    with pytest.raises(ClaimInvariantError) as excinfo:
        validate_claim(claim(verdict="unverifiable", confidence=None, sources=[SOURCE]))
    assert "source" in str(excinfo.value)


@pytest.mark.parametrize("verdict", EVIDENCED_VERDICTS)
def test_an_evidenced_verdict_may_not_have_a_null_confidence(verdict: str) -> None:
    """Breach 3 — a null confidence on anything but ``unverifiable``."""
    with pytest.raises(ClaimInvariantError) as excinfo:
        validate_claim(claim(verdict=verdict, confidence=None))
    assert "confidence" in str(excinfo.value)


@pytest.mark.parametrize("verdict", EVIDENCED_VERDICTS)
def test_an_evidenced_verdict_may_not_ship_without_sources(verdict: str) -> None:
    """Breach 4 — rule 2: no evidence means the verdict should have been
    ``unverifiable``, not a confident claim with nothing behind it."""
    with pytest.raises(ClaimInvariantError) as excinfo:
        validate_claim(claim(verdict=verdict, sources=[]))
    assert "source" in str(excinfo.value)


# -------------------------------------------------------------- the verdict set


@pytest.mark.parametrize("verdict", ["TRUE", "FALSE", "flagged", "fake", "Supported", ""])
def test_a_verdict_outside_the_four_is_refused(verdict: str) -> None:
    """Never TRUE/FALSE, never "flagged", never a display name on the wire."""
    with pytest.raises(ClaimInvariantError) as excinfo:
        validate_claim(claim(verdict=verdict))
    assert "verdict" in str(excinfo.value)


def test_a_confidence_outside_the_three_levels_is_refused() -> None:
    """Confidence is low/medium/high — never a percentage (rule 3)."""
    with pytest.raises(ClaimInvariantError) as excinfo:
        validate_claim(claim(confidence="0.92"))
    assert "confidence" in str(excinfo.value)


# ------------------------------------------------------- both accepted shapes


def test_a_model_object_is_validated_the_same_as_a_dict() -> None:
    """The pipeline holds claims both ways; the rules must not depend on which."""
    legal = claim(verdict="unverifiable", confidence=None, sources=[])
    validate_claim(Claim.model_validate(legal))

    illegal = Claim.model_validate(claim(verdict="unverifiable", confidence="low", sources=[]))
    with pytest.raises(ClaimInvariantError):
        validate_claim(illegal)


def test_a_missing_field_is_a_breach_not_a_default() -> None:
    """Absent is not null: omitting ``confidence`` must not slip past the rule."""
    for field in ("verdict", "confidence", "sources"):
        payload = claim(verdict="unverifiable", confidence=None, sources=[])
        del payload[field]
        with pytest.raises(ClaimInvariantError) as excinfo:
            validate_claim(payload)
        assert field in str(excinfo.value), field


@pytest.mark.parametrize("value", [None, "c1", 7, ["c1"]])
def test_something_that_is_not_a_claim_is_refused(value: object) -> None:
    """A stray value never validates by accident."""
    with pytest.raises(ClaimInvariantError):
        validate_claim(value)  # type: ignore[arg-type]


def test_a_claim_invariant_error_is_a_value_error_and_names_the_claim() -> None:
    """Callers guarding with ``except ValueError`` catch this, and the id (which
    carries no article text) is available for logging."""
    with pytest.raises(ValueError) as excinfo:
        validate_claim(claim(id="c4", sources=[]))
    error = excinfo.value
    assert isinstance(error, ClaimInvariantError)
    assert error.claim_id == "c4"
    assert "c4" in str(error)


# -------------------------------------------------------------------- the list


def test_validate_claims_accepts_a_legal_list() -> None:
    """A mixed but legal list passes in one call."""
    validate_claims(
        [
            claim(id="c1"),
            claim(id="c2", verdict="contradicted", confidence="medium"),
            claim(id="c3", verdict="unverifiable", confidence=None, sources=[]),
        ]
    )


def test_validate_claims_fails_on_the_first_breach() -> None:
    """One bad claim fails the batch, and says which one."""
    with pytest.raises(ClaimInvariantError) as excinfo:
        validate_claims([claim(id="c1"), claim(id="c2", sources=[]), claim(id="c3")])
    assert excinfo.value.claim_id == "c2"


def test_validate_claims_accepts_an_empty_list() -> None:
    """Nothing to check is not a failure."""
    validate_claims([])
