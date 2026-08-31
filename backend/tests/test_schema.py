"""The fixture article honours the shared contract.

Everything downstream leans on these invariants. Milestone 3's on-page anchoring
starts from ``start``/``end`` offsets into ``CheckRequest.text``, so an offset
that has silently drifted would only surface as a highlight landing on the wrong
sentence weeks later. The confidence and sources rules are the two places where
the product rules in ``CLAUDE.md`` are expressible in data, so they are asserted
in both directions rather than spot-checked — here against the fixture, and in
:mod:`tests.test_invariants` against :mod:`app.invariants`, which is what
actually enforces them on every claim heading for the wire.

The fixture is fictional (``CLAUDE.md``: "never present them as real"), so the
source urls are pinned here too: outlet *names* are real because the design
specifies them, but no url may point anywhere a reader could mistake for real
reporting.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pytest

import app.pipeline.mock as mock_pipeline
from app.invariants import ClaimInvariantError, validate_claim, validate_claims
from app.pipeline.mock import (
    FAILURE_MESSAGE,
    article_order,
    claims_found_payload,
    done_payload,
    error_payload,
    load_fixture_claims,
)
from app.schema_models import (
    CheckRequest,
    Claim,
    ClaimsFoundEvent,
    DoneEvent,
    ErrorEvent,
    Verdict,
)

ALLOWED_VERDICTS = {"supported", "contradicted", "missing_context", "unverifiable"}
"""The only four verdicts that exist. Never TRUE/FALSE, never "flagged"."""

EXPECTED_CLAIM_IDS = ["c1", "c2", "c3", "c4", "c5", "c6"]
"""The fixture's claims, in article order."""

FIXTURE_URL_HOST = "example.com"
"""RFC 2606 reserves example.com precisely so it can never be a real site. Every
url in the fixture lives there, so nothing invented can be taken for real."""

REAL_NEWSROOM_DOMAINS = {
    "channelnewsasia.com",
    "cna.asia",
    "data.gov.sg",
    "gov.sg",
    "mothership.sg",
    "reuters.com",
    "straitstimes.com",
    "yahoo.com",
    "hawkersvoice.sg",
}
"""Domains the fixture used to attribute invented evidence to. The outlet names
stay (the demo needs them to read realistically); the urls must not."""


def test_fixture_document_has_the_expected_shape(fixture_article: dict[str, Any]) -> None:
    """The fixture is ``{_fictional, url, title, text, claims}`` — no stored counts.

    ``counts`` is deliberately absent: ``mock.tally()`` derives it, so there is
    only one place the numbers can come from. ``_fictional`` is the standing
    warning that travels with the file.
    """
    assert set(fixture_article) == {"_fictional", "url", "title", "text", "claims"}
    assert fixture_article["text"]
    assert fixture_article["title"]


def test_the_fixture_says_in_writing_that_it_is_invented(
    fixture_article: dict[str, Any],
) -> None:
    """A reader of the file cannot miss that none of this is real reporting."""
    note: str = fixture_article["_fictional"]
    lowered = note.lower()
    assert "fictional" in lowered
    assert "never present" in lowered
    for word in ("claims", "source", "invented"):
        assert word in lowered, word


def test_no_fixture_url_points_at_a_real_newsroom(fixture_article: dict[str, Any]) -> None:
    """Every url — the article's own and every source's — is obviously synthetic.

    The evidence in this file is invented. Hanging it off ``reuters.com`` or
    ``gov.sg`` would attribute fabricated reporting to real newsrooms, and a
    resolvable link invites someone to click it and believe it.
    """
    urls = [fixture_article["url"]]
    urls += [
        source["url"] for claim in fixture_article["claims"] for source in claim["sources"]
    ]
    assert len(urls) > 1, "the fixture should carry source urls to check"

    for url in urls:
        host = (urlparse(url).hostname or "").lower()
        assert host == FIXTURE_URL_HOST, url
        registrable = ".".join(host.split(".")[-2:])
        assert registrable not in REAL_NEWSROOM_DOMAINS, url
        assert "re-vera-fixture" in url, url


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


def test_the_fixture_keeps_the_real_outlet_names(fixture_article: dict[str, Any]) -> None:
    """Names stay, urls go. The design names these outlets and the demo needs
    them to read like a real check; only the links had to become synthetic."""
    outlets = {
        source["outlet"] for claim in fixture_article["claims"] for source in claim["sources"]
    }
    assert {"CNA", "Reuters", "Straits Times", "Mothership"} <= outlets


def test_the_whole_fixture_satisfies_the_product_invariants(
    fixture_claims: list[dict[str, Any]],
) -> None:
    """The fixture is exactly what :mod:`app.invariants` lets onto the wire."""
    validate_claims(fixture_claims)


def sample_claim(**overrides: Any) -> dict[str, Any]:
    """A minimal legal ``supported`` claim, with fields overridden as needed."""
    claim: dict[str, Any] = {
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
    claim.update(overrides)
    return claim


SOURCE: dict[str, Any] = {
    "url": "https://example.com/re-vera-fixture/fictional-outlet/story",
    "outlet": "Example Outlet",
    "date": "2026-03-12",
    "wire": False,
    "stance": "supports",
}
"""One synthetic source, enough to satisfy "every non-unverifiable verdict ships
evidence" (rule 2)."""


def test_a_sourceless_supported_claim_is_rejected() -> None:
    """Rule 2: ``sources: []`` is legal **only** for an ``unverifiable`` verdict.

    This test used to assert the opposite — that the generated model happily
    validates a ``supported`` claim with no sources. It does, because the rule
    is a cross-field one that JSON Schema does not express; pinning that gap as
    expected behaviour meant the first judge to emit such a claim would have
    streamed it to a reader with the suite still green. The model's acceptance
    is asserted below for exactly what it is, and :func:`validate_claim` is what
    now refuses the claim.
    """
    sourceless = sample_claim(sources=[])

    assert Claim.model_validate(sourceless), "the generated model alone cannot catch this"

    with pytest.raises(ClaimInvariantError) as excinfo:
        validate_claim(sourceless)
    assert "source" in str(excinfo.value)

    # …and the same claim as a model object, since the pipeline handles both.
    with pytest.raises(ClaimInvariantError):
        validate_claim(Claim.model_validate(sourceless))


@pytest.mark.parametrize(
    ("field", "value"),
    [("confidence", "high"), ("sources", [SOURCE])],
)
def test_model_accepts_the_nullable_fields_explicitly(field: str, value: object) -> None:
    """``confidence`` is required-but-nullable, so it must be passed explicitly.

    Guards against a caller relying on a default that the generated model does
    not give it: ``Claim(...)`` without ``confidence`` raises.
    """
    base = sample_claim(**{field: value})
    assert Claim.model_validate(base)
    validate_claim(base)

    incomplete = dict(base)
    del incomplete["confidence"]
    with pytest.raises(ValueError):
        Claim.model_validate(incomplete)


def test_claim_rejects_unknown_fields() -> None:
    """``extra='forbid'`` — a stray key is an error, not a silently dropped one."""
    with pytest.raises(ValueError):
        Claim.model_validate(sample_claim(kind="numeric"))


# ----------------------------------------------------- the SSE event payloads
#
# The three non-claim events used to be hand-written dicts, which left the
# generated ClaimsFoundEvent/DoneEvent/ErrorEvent models as dead code and the
# wire free to drift from shared/schema.json without anything noticing. Every
# payload is now built by constructing the model and dumping it; these tests pin
# that, and pin the claim_ids contract from decision 15.


def test_claims_found_payload_lists_every_id_in_article_order(
    fixture_claims: list[dict[str, Any]],
) -> None:
    """``claim_ids`` is c1 … c6, ascending by ``start``, and ``count`` matches."""
    payload = claims_found_payload(article_order(fixture_claims))

    event = ClaimsFoundEvent.model_validate(payload)
    assert event.claim_ids == EXPECTED_CLAIM_IDS
    assert event.count == len(event.claim_ids)
    assert payload == {"type": "claims_found", "count": 6, "claim_ids": EXPECTED_CLAIM_IDS}


def test_claims_found_count_is_derived_from_the_id_list() -> None:
    """``count`` is never counted independently, so the two cannot disagree."""
    for size in (0, 1, 6, 8):
        claims = [{"id": f"c{index + 1}", "start": index} for index in range(size)]
        payload = claims_found_payload(claims)
        assert payload["count"] == size == len(payload["claim_ids"])


def test_article_order_sorts_claims_by_their_start_offset(
    fixture_claims: list[dict[str, Any]],
) -> None:
    """The ordering that ``claim_ids`` promises is produced, not assumed.

    Feeding the claims in a scrambled order — which is how they resolve — still
    yields article order, so the live path and a cache replay announce the same
    ids in the same order however the list reached the function.
    """
    scrambled = [fixture_claims[index] for index in (2, 0, 5, 3, 1, 4)]
    assert [claim["id"] for claim in article_order(scrambled)] == EXPECTED_CLAIM_IDS
    assert claims_found_payload(article_order(scrambled)) == claims_found_payload(
        article_order(fixture_claims)
    )


def test_done_payload_is_a_valid_done_event() -> None:
    """The ``done`` payload round-trips through the generated model."""
    counts = {"supported": 2, "contradicted": 2, "missing_context": 1, "unverifiable": 1}
    payload = done_payload(counts, "2026-08-31T04:15:09Z")

    event = DoneEvent.model_validate(payload)
    assert event.counts.model_dump() == counts
    assert payload == {"type": "done", "counts": counts, "checked_at": "2026-08-31T04:15:09Z"}


def test_done_payload_refuses_a_tally_that_is_not_the_four_counts() -> None:
    """A missing or unknown verdict fails here, not on a reader's counts line."""
    with pytest.raises(ValueError):
        done_payload({"supported": 1}, "2026-08-31T04:15:09Z")
    with pytest.raises(ValueError):
        done_payload(
            {
                "supported": 1,
                "contradicted": 0,
                "missing_context": 0,
                "unverifiable": 0,
                "flagged": 1,
            },
            "2026-08-31T04:15:09Z",
        )


def test_error_payload_is_a_valid_error_event() -> None:
    """The ``error`` payload round-trips, and reads as a sentence a reader sees."""
    payload = error_payload("internal", FAILURE_MESSAGE)

    event = ErrorEvent.model_validate(payload)
    assert event.code == "internal"
    assert event.message == FAILURE_MESSAGE
    assert "flagged" not in event.message.lower()
    assert payload == {"type": "error", "code": "internal", "message": FAILURE_MESSAGE}


def test_loading_a_fixture_that_breaks_a_product_rule_fails_loudly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``load_fixture_claims`` is a gate, not just a reader.

    It is one of the two places :func:`app.invariants.validate_claims` is called
    (the other is immediately before each claim is published), so a fixture
    edited into a state the product forbids fails on load rather than on a
    reader's screen. Driven against a throwaway copy of the fixture so the real
    one is never written to.
    """
    assert load_fixture_claims(8), "the real fixture still loads"

    illegal = tmp_path / "article.json"
    illegal.write_text(
        json.dumps({"claims": [sample_claim(sources=[])]}, ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(mock_pipeline, "FIXTURE_PATH", illegal)

    with pytest.raises(ClaimInvariantError):
        load_fixture_claims(8)
