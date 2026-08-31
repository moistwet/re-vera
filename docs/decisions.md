# Decision log

Decisions that reconcile the project brief (`CLAUDE.md`) with the design files
(`sieve-live-demo.dc.html`, `design-handoff.md`). Where these conflict with the
design files, this log and `CLAUDE.md` win.

## 2026-08-31 — brief/design reconciliation (with Isaac)

1. **Name: Re-Vera.** The prototype's "Sieve" name is replaced on every surface.
   The funnel glyph stays as the MVP logo mark.
2. **Reliability score banner: deferred.** The injected 0–100 ring above the
   headline is cut from the MVP; it may return later. The `done` event already
   carries per-verdict counts, so re-adding it needs no schema change. Until
   then, nothing Re-Vera renders reflows the host page.
3. **No "flagged" vocabulary.** The page bar's compact done state uses the four
   canonical verdict names and ellipsizes when narrow; the demo's
   "2 flagged" copy is dropped (rule 1: identical names on every surface).
4. **Confidence is nullable.** Schema: `confidence: "low" | "medium" | "high" | null`,
   where `null` iff `verdict == "unverifiable"`. The UI hides the confidence
   meter for those claims (matches the demo's behaviour, which the old schema
   couldn't express).
5. **Unverifiable claims carry no sources.** Rule 2 reworded: `unverifiable`
   ships an explanation of what was searched and not found, plus the trail,
   with `sources: []` (matches the demo).
6. **Cut features** (not part of the core user journey): reader-count footer
   ("340 readers…"), 👍/👎 feedback, "Report a mistake" + its toasts, game-mode
   streak line. The game scoreboard keeps "You spotted X of 3" and the tip box.

## 2026-08-31 — cost & pipeline decisions

7. **Cheapest model tier per stage, swappable via env.**
   `OPENAI_MODEL_EXTRACT` / `OPENAI_MODEL_STANCE` / `OPENAI_MODEL_JUDGE` default
   to the cheapest mini-tier model; any stage can be pointed at a stronger
   model during test runs without code changes. Escalate a stage only if it
   fails the golden-set eval.
8. **Claims capped at 8 per article** (`MAX_CLAIMS=8`). Extraction ranks by
   check-worthiness and the pipeline verifies the top 8.
9. **Retrieval short-circuit.** A Google Fact Check (ClaimReview) hit skips web
   search for that claim; passages capped per claim (6). Web search is the
   dominant per-claim cost.
10. **Install-ID daily cap (20) lands in milestone 1** — it bounds cost, not
    just abuse. The per-IP backstop moves to milestone 5 and stays loose
    (school NAT shares one IP across many users).
11. **"Guess first" starts the backend check on click** (still a manual
    trigger), so Reveal is near-instant. Reveal before all claims resolve:
    show resolved claims, let the rest flip in as they arrive.
12. **Anchoring strategy:** search for the exact quote with surrounding
    context as the primary anchor; `start`/`end` offsets are a hint only
    (extracted text never matches DOM text byte-for-byte).
13. **SSE keep-alive comment every ~20 s** so the MV3 service worker's stream
    fetch isn't idle-killed mid-check.
14. **Motion:** fluid/cool animations are wanted but polish is deferred to
    milestone 5. Reduced-motion is wired correctly from the start, including a
    JS `matchMedia` check for rAF-driven animation (the demo only covers CSS).

## 2026-08-31 — post-review fixes

15. **`claims_found` carries `claim_ids`, in article order.** The event grows
    from `{type, count}` to `{type, count, claim_ids}`, where `claim_ids` lists
    every `Claim.id` the job will send, ascending by the claim's `start`
    offset; `count` stays and always equals `claim_ids.length`.
    Why: claims resolve out of article order on purpose — the mock pipeline's
    `RESOLVE_ORDER` streams rows 3, 1, 6, 4, 2, 5, which is the demo's
    signature scattered fill (`design-handoff.md` §1 state C) — but a cache
    replay hands back the same claims in article order. A popup that filled
    rows by arrival therefore rendered one article two different ways
    depending on the cache, and it could not do better on its own: when the
    first claim lands it has no way to know which row that claim belongs in.
    Sending the ids up front lets a client allocate all six rows before any
    claim arrives and fill each row when its own claim lands, so the live path
    and the cached path render identically and each row is written exactly
    once. No other type changed; both bindings were regenerated from
    `shared/schema.json`.
16. **`CheckRequest` fields are length-bounded, and there is one canonical
    "same page" identity.**
    `CheckRequest.text` gets `maxLength: 60000` (~5× `settings.max_article_chars`,
    which truncates to 12,000 before extraction — generous enough to clear
    even a long feature or liveblog untouched, bounded enough that the POST
    body and the Redis cache entry built from it are no longer
    attacker-sized), `title` gets `maxLength: 500` (well past any real
    headline), and `install_id` gets `maxLength: 64` (a `crypto.randomUUID()`
    is 36 characters; the cap is looser than that on purpose so a future
    format change doesn't need a schema edit, but it stops an unbounded string
    from being folded into a Redis key). An oversized body is now a clean 422
    before any work happens. Both bindings were regenerated from
    `shared/schema.json`; regenerating them also fixed two latent bugs in
    `shared/generate.sh` unrelated to this change but blocking a clean
    regeneration — the Pydantic side now passes `--field-constraints` so a
    bounded field stays typed `str` with its limit on `Field(...)` instead of
    an inline `constr(...)` call, which is invalid in a static type position
    under `from __future__ import annotations` and failed `mypy`; the
    TypeScript side now passes `json2ts` an absolute path, since `pnpm --dir
    extension exec` runs with `extension/` as its working directory and the
    script's paths are relative to the repo root.
    Separately: `aggregate._usable`'s "an article cannot cite itself" guard
    compared raw URL strings, so retrieval handing the article back with a
    tracking parameter, a `www.` prefix, a different scheme or a fragment —
    exactly how a search engine returns a result — made the article corroborate
    itself. `providers/base.py` now exports one canonical `url_key` /
    `same_page` / `registrable_domain`, replacing the two private, disagreeing
    notions of "the same page" that had grown up separately in `aggregate.py`
    and `providers/websearch.py`. `registrable_domain` also collapses
    subdomains of one publisher (`news.example.com` and `www.example.com` are
    one site, not two independent sources), with Singapore's multi-label
    suffixes (`gov.sg`, `com.sg`, …) handled explicitly rather than left to a
    Public Suffix List dependency this project doesn't carry. Consuming these
    in `aggregate.py` and `providers/websearch.py` is tracked as follow-up
    work in the other stages, not done here.
