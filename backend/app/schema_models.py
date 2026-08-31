# GENERATED — do not hand-edit, run shared/generate.sh
# Source of truth: shared/schema.json

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import AnyUrl, BaseModel, ConfigDict, Field


class Verdict(StrEnum):
    supported = 'supported'
    contradicted = 'contradicted'
    missing_context = 'missing_context'
    unverifiable = 'unverifiable'


class Confidence(StrEnum):
    low = 'low'
    medium = 'medium'
    high = 'high'


class Stance(StrEnum):
    supports = 'supports'
    refutes = 'refutes'
    neutral = 'neutral'


class CheckRequest(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    url: AnyUrl = Field(
        ...,
        description='Canonical URL of the article being checked. The cache key is sha256 of this string.',
    )
    title: str = Field(
        ...,
        description='Article headline as extracted from the page. Bounded well above any real headline; a longer string is rejected with 422 rather than stored or hashed.',
        max_length=500,
    )
    text: str = Field(
        ...,
        description='Full extracted article body as plain text. Claim.start and Claim.end are character offsets into this string. Bounded to comfortably clear even a long feature or liveblog (60,000 characters is ~5x settings.max_article_chars, which truncates to 12,000 before extraction) while keeping an unauthenticated POST body — and the Redis cache entry built from it — bounded in size.',
        max_length=60000,
    )
    install_id: str = Field(
        ...,
        description="Anonymous per-install UUID from chrome.storage.local (crypto.randomUUID(), 36 characters). Used only for the daily cap and folded into a Redis key, so it is bounded well above a UUID's length rather than to it exactly, in case the format ever changes shape.",
        max_length=64,
    )


class CheckJob(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    job_id: str = Field(..., description='Identifier for the streaming job.')
    cached: bool = Field(
        ...,
        description='True when the result came from the 7-day URL cache and the stream will replay everything immediately.',
    )
    claim_count: int | None = Field(
        ...,
        description='Number of claims, known up front only on a cache hit; null on a cache miss.',
    )


class Source(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    url: str = Field(..., description='Link the reader can open.')
    outlet: str = Field(..., description='Human-readable publisher name, e.g. "CNA".')
    date: str = Field(
        ...,
        description='Publication date as an ISO 8601 calendar date, e.g. "2026-03-12".',
    )
    wire: bool = Field(
        ...,
        description='True when this is wire copy; near-identical wire text across domains counts as one source.',
    )
    stance: Stance


class TrailNode(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    label: str = Field(
        ...,
        description='Node title, e.g. "This article", "Independent reports", "Original source".',
    )
    note: str = Field(..., description='Muted detail line for the node.')


class Claim(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    id: str = Field(..., description='Stable id within the job, e.g. "c1".')
    quote: str = Field(
        ...,
        description='Exact substring of CheckRequest.text — text[start:end] must equal this.',
    )
    start: int = Field(
        ...,
        description="Character offset of the quote's first character in CheckRequest.text.",
    )
    end: int = Field(
        ...,
        description="Character offset one past the quote's last character in CheckRequest.text.",
    )
    verdict: Verdict
    confidence: Confidence | None = Field(
        ..., description='Null if and only if verdict is "unverifiable".'
    )
    evidence: str = Field(
        ...,
        description='One plain-language sentence naming the sources. For an "unverifiable" verdict it explains what was searched and not found.',
    )
    sources: list[Source] = Field(
        ..., description='Empty if and only if verdict is "unverifiable".'
    )
    trail: list[TrailNode] = Field(
        ..., description='Provenance trail, outermost step first.'
    )


class Counts(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    supported: int
    contradicted: int
    missing_context: int
    unverifiable: int


class ClaimsFoundEvent(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    type: Literal['claims_found']
    count: int = Field(
        ...,
        description='Number of "claim" events that will follow. Always equal to claim_ids.length.',
    )
    claim_ids: list[str] = Field(
        ...,
        description="Every Claim.id this job will send, in article order (ascending by the claim's start offset). Row n belongs to claim_ids[n], whatever order the claim events arrive in.",
    )


class DoneEvent(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    type: Literal['done']
    counts: Counts
    checked_at: str = Field(
        ..., description='ISO 8601 timestamp of when the check finished.'
    )


class ErrorEvent(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    type: Literal['error']
    code: str = Field(..., description='Machine-readable code, e.g. "daily_limit".')
    message: str = Field(..., description='Reader-facing sentence.')


class ReVeraSchema(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    check_request: CheckRequest | None = None
    check_job: CheckJob | None = None
    claim: Claim | None = None
    counts: Counts | None = None
    claims_found_event: ClaimsFoundEvent | None = None
    done_event: DoneEvent | None = None
    error_event: ErrorEvent | None = None
