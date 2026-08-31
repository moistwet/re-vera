"""The fixture article honours the shared contract.

Everything downstream leans on these invariants. Milestone 3's on-page anchoring
starts from ``start``/``end`` offsets into ``CheckRequest.text``, so an offset
that has silently drifted would only surface as a highlight landing on the wrong
sentence weeks later. The confidence and sources rules are the two places where
the product rules in ``CLAUDE.md`` are expressible in data, so they are asserted
in both directions rather than spot-checked.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.schema_models import CheckRequest, Claim, Verdict

ALLOWED_VERDICTS = {"supported", "contradicted", "missing_context", "unverifiable"}
"""The only four verdicts that exist. Never TRUE/FALSE, never "flagged"."""

EXPECTED_CLAIM_IDS = ["c1", "c2", "c3", "c4", "c5", "c6"]
"""The fixture's claims, in article order."""


def test_fixture_document_has_the_expected_shape(fixture_article: dict[str, Any]) -> None:
    """The fixture is exactly ``{url, title, text, claims}`` — no stored counts.

    ``counts`` is deliberately absent: ``mock.tally()`` derives it, so there is
    only one place the numbers can come from.
    """
    assert set(fixture_article) == {"url", "title", "text", "claims"}
    assert fixture_article["text"]
    assert fixture_article["title"]


def test_fixture_article_is_a_valid_check_request(fixture_article: dict[str, Any]) -> None:
    """The fixture doubles as a ``CheckRequest`` payload, and survives AnyUrl.

    ``CheckRequest.url`` parses as ``pydantic.AnyUrl``, which normalises some
    URLs (it appends a trailing slash to an origin-only one). The fixture URL has
    a path, so it round-trips byte-identical — which is what keeps the sha256
    cache key stable whether it is computed from the raw string or from a parsed
    request.
    """
    request = CheckRequest.model_validate(
        {
            "url": fixture_article["url"],
            "title": fixture_article["title"],
            "text": fixture_article["text"],
            "install_id": "00000000-0000-4000-8000-000000000000",
        }
    )
    assert str(request.url) == fixture_article["url"]


def test_fixture_has_six_claims_in_article_order(fixture_claims: list[dict[str, Any]]) -> None:
    """Six claims, ids c1 … c6, ordered by where they appear in the text."""
    assert [claim["id"] for claim in fixture_claims] == EXPECTED_CLAIM_IDS
    starts = [claim["start"] for claim in fixture_claims]
    assert starts == sorted(starts)


def test_every_claim_validates_against_the_generated_model(
    fixture_claims: list[dict[str, Any]],
) -> None:
    """Each claim round-trips through the generated Pydantic ``Claim``.

    The generated models set ``extra='forbid'``, so this also proves the fixture
    carries the nine schema fields and nothing else — an extra key here would be
    published straight onto the wire as a ``claim`` event.
    """
    for claim in fixture_claims:
        model = Claim.model_validate(claim)
        assert model.model_dump(mode="json") == claim


def test_quotes_sit_at_their_offsets(
    fixture_article: dict[str, Any], fixture_claims: list[dict[str, Any]]
) -> None:
    """``text[start:end] == quote`` for all six claims.

    This is the contract milestone 3's anchoring is built on.
    """
    text: str = fixture_article["text"]
    for claim in fixture_claims:
        assert text[claim["start"] : claim["end"]] == claim["quote"], claim["id"]


def test_each_quote_occurs_exactly_once(
    fixture_article: dict[str, Any], fixture_claims: list[dict[str, Any]]
) -> None:
    """No quote appears twice in the article.

    Uniqueness is what lets the anchoring in milestone 3 fall back to searching
    for the quote text when DOM text does not match the extracted text
    byte-for-byte; a duplicate quote would make that fallback ambiguous.
    """
    text: str = fixture_article["text"]
    for claim in fixture_claims:
        assert text.count(claim["quote"]) == 1, claim["id"]


def test_verdicts_are_one_of_the_four(fixture_claims: list[dict[str, Any]]) -> None:
    """Only the four canonical verdicts, and every one of them is represented."""
    verdicts = [claim["verdict"] for claim in fixture_claims]
    assert set(verdicts) <= ALLOWED_VERDICTS
    assert set(verdicts) == ALLOWED_VERDICTS, "the fixture should exercise all four verdicts"
    assert {verdict.value for verdict in Verdict} == ALLOWED_VERDICTS


def test_confidence_is_null_iff_unverifiable(fixture_claims: list[dict[str, Any]]) -> None:
    """Rule 3: no confidence for an unverifiable claim, one for every other."""
    for claim in fixture_claims:
        unverifiable = claim["verdict"] == "unverifiable"
        assert (claim["confidence"] is None) is unverifiable, claim["id"]
        if not unverifiable:
            assert claim["confidence"] in {"low", "medium", "high"}, claim["id"]


def test_sources_are_empty_iff_unverifiable(fixture_claims: list[dict[str, Any]]) -> None:
    """Rule 2 / decision 5: an unverifiable claim carries no sources, and every
    other verdict ships at least one."""
    for claim in fixture_claims:
        unverifiable = claim["verdict"] == "unverifiable"
        assert (claim["sources"] == []) is unverifiable, claim["id"]


def test_every_claim_has_evidence_and_a_trail(fixture_claims: list[dict[str, Any]]) -> None:
    """Rule 2: evidence is never blank — for an unverifiable claim it explains
    what was searched and not found — and the provenance trail is always there."""
    for claim in fixture_claims:
        assert claim["evidence"].strip(), claim["id"]
        assert claim["trail"], claim["id"]


@pytest.mark.parametrize(
    ("field", "value"),
    [("confidence", "high"), ("sources", [])],
)
def test_model_accepts_the_nullable_fields_explicitly(field: str, value: object) -> None:
    """``confidence`` is required-but-nullable, so it must be passed explicitly.

    Guards against a caller relying on a default that the generated model does
    not give it: ``Claim(...)`` without ``confidence`` raises.
    """
    base: dict[str, Any] = {
        "id": "c1",
        "quote": "q",
        "start": 0,
        "end": 1,
        "verdict": "supported",
        "confidence": "high",
        "evidence": "e",
        "sources": [],
        "trail": [],
    }
    base[field] = value
    assert Claim.model_validate(base)

    incomplete = dict(base)
    del incomplete["confidence"]
    with pytest.raises(ValueError):
        Claim.model_validate(incomplete)


def test_claim_rejects_unknown_fields() -> None:
    """``extra='forbid'`` — a stray key is an error, not a silently dropped one."""
    with pytest.raises(ValueError):
        Claim.model_validate(
            {
                "id": "c1",
                "quote": "q",
                "start": 0,
                "end": 1,
                "verdict": "supported",
                "confidence": "high",
                "evidence": "e",
                "sources": [],
                "trail": [],
                "kind": "numeric",
            }
        )
