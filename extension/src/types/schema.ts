/* eslint-disable */
/**
 * GENERATED — do not hand-edit, run shared/generate.sh
 * Source of truth: shared/schema.json
 */

/**
 * The only four verdicts Re-Vera may produce. Display names are sentence case: Supported, Contradicted, Missing context, Unverifiable. Never TRUE/FALSE, never all-caps, never alternative vocabulary such as "flagged".
 */
export type Verdict = "supported" | "contradicted" | "missing_context" | "unverifiable";
/**
 * Three-level confidence, never a percentage. Rendered as a three-dot meter. Null (see Claim.confidence) if and only if the verdict is "unverifiable", in which case the UI hides the meter entirely.
 */
export type Confidence = "low" | "medium" | "high";
/**
 * How a retrieved source relates to the claim it was retrieved for.
 */
export type Stance = "supports" | "refutes" | "neutral";

/**
 * Re-Vera shared contract — the single source of truth for the backend and every client. Generated into backend/app/schema_models.py (Pydantic v2) and extension/src/types/schema.ts (TypeScript) by shared/generate.sh. This root object exists only so that every type in $defs is reachable from the root; nothing sends or receives it.
 */
export interface ReVeraSchema {
  check_request?: CheckRequest;
  check_job?: CheckJob;
  claim?: Claim;
  counts?: Counts;
  claims_found_event?: ClaimsFoundEvent;
  done_event?: DoneEvent;
  error_event?: ErrorEvent;
}
/**
 * Body of POST /check. The extracted article text plus the anonymous install ID. Article text is cached by URL hash, never by user; never log the text alongside install_id.
 */
export interface CheckRequest {
  /**
   * Canonical URL of the article being checked. The cache key is sha256 of this string.
   */
  url: string;
  /**
   * Article headline as extracted from the page. Bounded well above any real headline; a longer string is rejected with 422 rather than stored or hashed.
   */
  title: string;
  /**
   * Full extracted article body as plain text. Claim.start and Claim.end are character offsets into this string. Bounded to comfortably clear even a long feature or liveblog (60,000 characters is ~5x settings.max_article_chars, which truncates to 12,000 before extraction) while keeping an unauthenticated POST body — and the Redis cache entry built from it — bounded in size.
   */
  text: string;
  /**
   * Anonymous per-install UUID from chrome.storage.local (crypto.randomUUID(), 36 characters). Used only for the daily cap and folded into a Redis key, so it is bounded well above a UUID's length rather than to it exactly, in case the format ever changes shape.
   */
  install_id: string;
}
/**
 * Response to POST /check. The client then opens GET /check/{job_id}/stream.
 */
export interface CheckJob {
  /**
   * Identifier for the streaming job.
   */
  job_id: string;
  /**
   * True when the result came from the 7-day URL cache and the stream will replay everything immediately.
   */
  cached: boolean;
  /**
   * Number of claims, known up front only on a cache hit; null on a cache miss.
   */
  claim_count: number | null;
}
/**
 * One checked claim. Payload of the SSE "claim" event and the unit stored in the cache.
 */
export interface Claim {
  /**
   * Stable id within the job, e.g. "c1".
   */
  id: string;
  /**
   * Exact substring of CheckRequest.text — text[start:end] must equal this.
   */
  quote: string;
  /**
   * Character offset of the quote's first character in CheckRequest.text.
   */
  start: number;
  /**
   * Character offset one past the quote's last character in CheckRequest.text.
   */
  end: number;
  verdict: Verdict;
  /**
   * Null if and only if verdict is "unverifiable".
   */
  confidence: Confidence | null;
  /**
   * One plain-language sentence naming the sources. For an "unverifiable" verdict it explains what was searched and not found.
   */
  evidence: string;
  /**
   * Empty if and only if verdict is "unverifiable".
   */
  sources: Source[];
  /**
   * Provenance trail, outermost step first.
   */
  trail: TrailNode[];
}
/**
 * One piece of retrieved evidence behind a claim.
 */
export interface Source {
  /**
   * Link the reader can open.
   */
  url: string;
  /**
   * Human-readable publisher name, e.g. "CNA".
   */
  outlet: string;
  /**
   * Publication date as an ISO 8601 calendar date, e.g. "2026-03-12".
   */
  date: string;
  /**
   * True when this is wire copy; near-identical wire text across domains counts as one source.
   */
  wire: boolean;
  stance: Stance;
}
/**
 * One step of the provenance trail, e.g. label "Original source", note "gov.sg press release, 12 Mar".
 */
export interface TrailNode {
  /**
   * Node title, e.g. "This article", "Independent reports", "Original source".
   */
  label: string;
  /**
   * Muted detail line for the node.
   */
  note: string;
}
/**
 * Per-verdict tally for the finished article.
 */
export interface Counts {
  supported: number;
  contradicted: number;
  missing_context: number;
  unverifiable: number;
}
/**
 * First SSE event of a job: how many claims will be checked, and which ids they are, in article order. A client allocates one row per id up front and fills each row when its "claim" arrives, so the live stream (which resolves out of article order) and a cache replay (which does not) render identically, and every row is written exactly once.
 */
export interface ClaimsFoundEvent {
  type: "claims_found";
  /**
   * Number of "claim" events that will follow. Always equal to claim_ids.length.
   */
  count: number;
  /**
   * Every Claim.id this job will send, in article order (ascending by the claim's start offset). Row n belongs to claim_ids[n], whatever order the claim events arrive in.
   */
  claim_ids: string[];
}
/**
 * Final SSE event of a successful job. The stream closes after this.
 */
export interface DoneEvent {
  type: "done";
  counts: Counts;
  /**
   * ISO 8601 timestamp of when the check finished.
   */
  checked_at: string;
}
/**
 * Terminal SSE event when a job fails. The stream closes after this.
 */
export interface ErrorEvent {
  type: "error";
  /**
   * Machine-readable code, e.g. "daily_limit".
   */
  code: string;
  /**
   * Reader-facing sentence.
   */
  message: string;
}
