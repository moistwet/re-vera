# Re-Vera — project brief for Claude Code

Re-Vera is a Chrome extension that, only when the reader asks, checks the factual claims in the news article they are reading, highlights them in place, and shows the evidence. Target users: Singaporean teenagers on school laptops. Hackathon MVP: Ellipsis Tech Series 2026, Digital Literacy track.

Read this file fully before writing any code. When something here conflicts with your instinct, this file wins. When something is missing, ask before assuming.

Naming: the product is **Re-Vera** on every user-facing surface. The design files in `docs/` come from a prototype named "Sieve" — keep their layout, tokens, and copy, but replace the name everywhere. Keep the funnel glyph as the MVP logo mark. Where this file and the design files conflict, this file and `docs/decisions.md` win.

## Non-negotiable product rules

1. Four verdicts only: `supported`, `contradicted`, `missing_context`, `unverifiable`. Never TRUE/FALSE/"fake". Display names: Supported, Contradicted, Missing context, Unverifiable — sentence case, never all-caps, identical on every surface. Never introduce alternative vocabulary (e.g. "flagged") on any surface, including the page summary bar: the bar shows the four canonical counts and ellipsizes when narrow.
2. Every `supported`, `contradicted`, or `missing_context` verdict ships with evidence: at least one source (URL, outlet, date) and a one-sentence plain-language evidence summary that names the sources. An `unverifiable` verdict ships an explanation of what was searched and not found, plus the provenance trail; it carries no sources. No evidence → `unverifiable`. The LLM judge may only use retrieved passages, never its own knowledge.
3. Confidence is `low | medium | high`, never a percentage. It is `null` when the verdict is `unverifiable`, and the UI hides the confidence meter for those claims.
4. Never encode a verdict by colour alone — icon + label every time.
5. Manual trigger only. Nothing runs until the reader clicks Check or Guess first. No background scanning, ever. Clicking **Guess first** counts as a manual trigger: it starts the backend check immediately so Reveal is near-instant. If the reader hits Reveal before all claims resolve, reveal the resolved ones and let the rest flip in as they arrive.
6. Privacy: no accounts. An anonymous random install ID lives in `chrome.storage.local`. Article text is cached by URL hash, never by user. Never log article text with an identifier.
7. The extension contains no secrets. All API keys live in the backend `.env`, which is gitignored.
8. Highlights never mutate or reflow the host page's text. Use the CSS Custom Highlight API on Ranges, not wrapper `<span>`s. All in-page Re-Vera UI renders inside a Shadow DOM using the system font stack; never load webfonts into host pages. The popup and side panel are extension pages and may use the webfonts from the design tokens (Cabin + IBM Plex Sans).
9. Honour `prefers-reduced-motion` — via CSS media query for CSS animations **and** a `matchMedia` check for any JS-driven animation (rAF counters, etc.). The demo misses the JS half; we don't.

## Out of scope for the MVP (deliberate cuts)

The design files show these; do not build them. They were cut on 2026-08-31 (see `docs/decisions.md`):

* **Reliability score banner** (the injected 0–100 ring above the headline). Deferred, may return later — the `done` event already carries per-verdict counts, so it can be added without a schema change.
* "N readers have checked this article" counter.
* 👍/👎 feedback buttons and "Report a mistake" (and their toasts).
* Game-mode streak line. The scoreboard keeps "You spotted X of 3" and the tip box.

## Repository layout (monorepo)

```
re-vera/
  CLAUDE.md
  docs/                 design references + decision log + plans (read them)
    sieve-live-demo.dc.html   interactive design prototype (tokens, timings, copy)
    design-handoff.md         handoff spec for the prototype
    decisions.md              decisions that supersede the design files
    plans/                    per-milestone plans
  extension/            Chrome MV3 extension — TypeScript, React, Vite
  backend/              Python 3.12, FastAPI, Pydantic v2, Redis
  eval/                 evaluation harness + golden set (JSONL)
  shared/               verdict JSON schema (single source of truth, generated into both TS and Pydantic)
```

## The contract (shared/schema)

The backend and every client speak this schema. Change it only with a reason, and regenerate both sides.

```jsonc
// CheckRequest
{ "url": "https://...", "title": "...", "text": "full extracted article text", "install_id": "uuid" }

// CheckJob (response to POST /check)
{ "job_id": "uuid", "cached": false, "claim_count": null }

// Claim event (one per SSE message on GET /check/{job_id}/stream)
{
  "id": "c1",
  "quote": "exact substring of the article text",
  "start": 123, "end": 151,               // character offsets into request.text
  "verdict": "contradicted",
  "confidence": "high",                    // "low" | "medium" | "high" | null (null iff verdict is "unverifiable")
  "evidence": "An official release and CNA put the median adjustment at 4%, not 40%.",
  "sources": [ { "url": "...", "outlet": "CNA", "date": "2026-03-12", "wire": false, "stance": "refutes" } ],
  // sources is [] iff verdict is "unverifiable"; evidence then explains what was searched and not found
  "trail": [ { "label": "This article", "note": "wire copy, republished on Yahoo" },
             { "label": "Independent reports", "note": "CNA · Reuters" },
             { "label": "Original source", "note": "gov.sg press release, 12 Mar" } ]
}

// Final SSE event
{ "type": "done", "counts": { "supported": 2, "contradicted": 2, "missing_context": 1, "unverifiable": 1 }, "checked_at": "..." }
```

SSE event types: `claims_found` (with count, sent first), `claim` (one per claim, as each resolves, in any order), `done`, `error`.

## Backend pipeline (backend/app/pipeline/)

Five stages, each its own module with a typed input/output and its own unit tests. Every stage must run against a fixture without network access.

1. `extract.py` — Claim extraction. OpenAI GPT API with a JSON-schema structured output. Produces atomic, check-worthy claims with exact quotes and character offsets. Filters opinion and prediction. Tags quotation claims (`kind: "attribution"`) and numeric claims (`kind: "numeric"`). Verify every returned `quote` is an exact substring of the text; drop any that is not. Ranks claims by check-worthiness and passes on at most `MAX_CLAIMS` (default 8) per article — long articles stay cheap and fast.
2. `retrieve.py` — Evidence retrieval per claim, in order: Google Fact Check Tools API (ClaimReview) → web search → official data (data.gov.sg) for numeric claims → fetch the cited source for attribution claims. A ClaimReview hit short-circuits web search (search is the most expensive step per claim). Cap retained passages per claim (default 6). For the MVP, web search may be the OpenAI built-in web search tool; wrap it behind a `SearchProvider` interface so it can be swapped. De-duplicate syndicated wire copy: identical or near-identical text on multiple domains counts as one source.
3. `stance.py` — GPT API scores each retrieved passage as `supports | refutes | neutral` with a structured output. Batch passages per claim in one call.
4. `judge.py` — GPT API produces the verdict, confidence and evidence sentence from the scored passages only. The prompt must require quoting the passages it relies on; the code must verify the quoted spans exist in the passages, else downgrade to `unverifiable`.
5. `aggregate.py` — Rules, not a model: a high-confidence refutation from a credible source → `contradicted`; two or more independent supporting sources or one primary source → `supported`; true-but-misleading signals (tiny sample, outdated, cherry-picked) → `missing_context`; otherwise `unverifiable`. Builds the provenance trail.

Cross-cutting:

* `llm.py` — one thin client wrapping the OpenAI API (structured outputs, retries, timeouts, token accounting). Nothing else imports the OpenAI SDK directly. Model is configurable **per stage** via env (`OPENAI_MODEL_EXTRACT`, `OPENAI_MODEL_STANCE`, `OPENAI_MODEL_JUDGE`), defaulting to the cheapest mini-tier model — swap a single stage to a stronger model during test runs without code changes.
* `cache.py` — Redis, key `check:{sha256(url)}`, TTL 7 days, stores the full claim list and counts.
* `limits.py` — daily cap of `DAILY_CAP` (20) checks per install ID, enforced from milestone 1 (it bounds cost, not just abuse); return 429 with a clear message. A per-IP backstop lands in milestone 5 and must be loose: our users sit behind shared school NAT, so per-IP limits punish whole schools.
* Streaming: the API returns a `job_id` immediately; a worker task runs the pipeline and publishes claim events to Redis pub/sub; `GET /check/{job_id}/stream` relays them as SSE. Cache hits stream all claims at once. The stream sends an SSE comment as keep-alive every ~20 s so the MV3 service worker's fetch is never idle long enough to be killed.
* Every LLM call logs prompt version, model, tokens and latency. Prompts live in `backend/app/prompts/*.md` with a version header, never inline in code.

Cost rules (apply everywhere):

* One extraction call per article; truncate article text to a sane token budget before sending.
* At most `MAX_CLAIMS` (8) claims verified per article.
* Fact Check API hit → skip web search for that claim. Passages capped per claim.
* Minimal structured-output schemas; short prompts; no retries on 4xx.
* The 7-day URL cache and the daily install cap are cost controls — never bypass them "for testing" outside local dev.

## Extension (extension/src/)

* `popup/` — React. States: not-an-article, ready, checking (streaming stepper + claim rows), done, error, daily-limit. The popup can close and reopen mid-check: checking state lives in the service worker and the popup re-syncs on open.
* `content/` — content script. `extract.ts` (JSON-LD NewsArticle first, then `@mozilla/readability`), `anchor.ts` (map claims back to DOM Ranges across text nodes; must survive inline `<a>`/`<em>` splits and ads — anchor primarily by searching for the exact `quote` with surrounding context, using `start`/`end` offsets as a hint, since extracted text never matches DOM text byte-for-byte), `highlight.ts` (CSS Custom Highlight API, four verdict styles + game-mode outline), `card.tsx` (claim card), `bar.tsx` (page summary bar), `game.tsx` (Guess first). All rendered in one Shadow DOM host.
* `sidepanel/` — React. Full report, filter chips, About this source.
* `background/` — service worker. Only component that calls the backend. Opens the SSE stream with `fetch` + `ReadableStream`, relays events to popup and content script via `chrome.runtime` messaging. Holds the install ID. Must survive MV3 worker restarts: persist job state so a restarted worker can resume or re-attach the stream.
* `theme/` — design tokens copied from `docs/design-handoff.md` (see the token table there). Verdict colours: supported `#12766B`, contradicted `#C24A32`, missing context `#A16207`, unverifiable `#64748B`; ink `#1C2523`.
* Manifest V3, `host_permissions` for the backend origin only, `activeTab` for the article, `sidePanel`, `storage`. No `<all_urls>` content script injection at install; inject on click via `chrome.scripting`.

## Evaluation harness (eval/)

* `golden/*.jsonl` — claims with gold verdicts and gold sources. Start with 30 fictional claims from the sample article set in `docs/`, then add real Singapore articles hand-labelled by the team.
* `run_eval.py` — runs the pipeline on the golden set, reports per-verdict precision/recall, abstention rate, and the gate metric: precision on `contradicted` ≥ 0.90. Exit non-zero if the gate fails. With ~30 golden claims the gate swings on single errors — treat it as directional, and prefer running retrieval from recorded fixtures where possible so CI doesn't burn API spend on every prompt tweak.
* Runs in CI on every change to `backend/app/prompts/` or `backend/app/pipeline/`.

## Working agreements

* Plan before code: for any task touching more than two files, write the plan (files, functions, tests) and wait for approval.
* Tests first for pipeline stages; every stage has fixtures under `backend/tests/fixtures/` and never calls the network in tests.
* Ask before adding a dependency. Prefer the standard library and what is already installed.
* Small commits with conventional messages (`feat:`, `fix:`, `test:`, `docs:`). Never commit `.env`, keys, or `node_modules`.
* Type-check and lint clean before declaring a task done: `pnpm typecheck && pnpm lint` in `extension/`, `ruff check && mypy` in `backend/`.
* When a design detail is unclear, read `docs/` first; it has the full spec and handoff. If still unclear, ask.
* The sample article and its six claims in `docs/` are fictional. Use them for fixtures and demos; never present them as real.
* Animations should end up fluid and the interactions cool — but polish is a later pass. Wire reduced-motion correctly from the start; don't gold-plate motion before milestone 5.

## Milestones (do them in order; stop at the end of each for review)

1. Skeleton — monorepo scaffolding, shared schema generated to TS + Pydantic, FastAPI with `POST /check` and the SSE endpoint backed by a mocked pipeline that streams the six fixture claims with realistic delays, Redis cache, install-ID daily cap. Extension loads unpacked, popup ready state, content script extracts article text and sends it via the service worker, popup shows streamed claim rows. No highlights yet.
2. Real pipeline — implement stages 1–5 against the OpenAI API with fixtures and unit tests; `run_eval.py` on the fictional golden set.
3. On-page overlay — anchoring, highlights, claim card with provenance trail and source chips, page bar. Test on Yahoo News, CNA, The Straits Times and Mothership.
4. Side panel and Guess first — full report, filters, About this source; game mode with reveal and scoreboard (spotted count + tip, no streak).
5. Hardening — error and daily-limit states, per-IP rate-limit backstop, reduced motion, keyboard path (Tab through claims, Enter opens card, Esc closes), screen-reader labels, privacy policy page, Web Store packaging, motion/animation polish pass.

## Environment

* `backend/.env`: `OPENAI_API_KEY`, `GOOGLE_FACTCHECK_API_KEY`, `REDIS_URL`, `ALLOWED_EXTENSION_ORIGIN`, `DAILY_CAP=20`, `MAX_CLAIMS=8`, `OPENAI_MODEL_EXTRACT`, `OPENAI_MODEL_STANCE`, `OPENAI_MODEL_JUDGE`.
* `extension/.env`: `VITE_API_BASE` only.
* Local run: `docker compose up redis`, `uvicorn app.main:app --reload`, `pnpm dev` in `extension/`, load `extension/dist` unpacked in Chrome.
