# Milestone 2 — open issues

Status as committed: the five-stage pipeline is built and every gate is green
(backend suite, `ruff`, `mypy` over 26 modules, and `eval/run_eval.py --offline`
with the contradicted-precision gate passing at 1.000 over 11 predictions,
abstention 0.344).

**Green gates do not mean this milestone is finished.** Three review rounds ran.
Round 1 built it; round 2 fixed 23 of 25 redteam findings but overcorrected and
introduced new ones; round 3 was written but never executed (the session's agent
budget ran out), so everything below is STILL OPEN in the committed code.

Round 3's plan of record — including the four design decisions taken to resolve
these — is the workflow script referenced in the session notes; the decisions are
restated here so the work can be resumed without it.

## Blockers

1. **SSRF on the provenance fetch path** — `app/pipeline/providers/websearch.py`.
   Round 2 built a complete SSRF perimeter in `providers/cited.py` (scheme
   allowlist, private/loopback/link-local/metadata refusal, redirect re-checking,
   body cap, timeout). Fixing blocker B1 added a *second* outbound fetch —
   `_verify_one` calls `self.http.get(passage.url, ...)` on a URL taken from the
   model's own JSON answer — with no guard at all. Reachable targets include
   `169.254.169.254` (cloud metadata) and `localhost:6379` (this service's Redis).
   Fix: reuse the existing perimeter (move it to `providers/base.py` so it is
   shared rather than duplicated). A refused URL means UNKNOWN provenance — keep
   the passage unverified; never drop, never crash.

2. **B1 is not actually closed on the independence path** — `app/pipeline/aggregate.py`.
   `side_strength` gates only the single-deciding-passage paths (primary alone,
   fact-check alone) on `provenance_verified`. The ">= 2 independent source
   groups" path is ungated, so two model-summarised web passages whose text may
   never have appeared on any page still reach a confident `supported`. That is
   B1's original failure scenario. Two reviewers disagreed here; the one that
   demonstrated it by running the code is correct.
   Fix (decision D1): a decided verdict requires at least one
   provenance-verified passage among those relied upon. Unverified passages may
   corroborate, never decide alone.

3. **Provenance verification destroys genuine evidence** — `websearch.py`.
   Measured over 12 realistic news-HTML shapes, 4 fail, and they are among the
   commonest constructions in news prose: `<a href>the board</a>.` reads as
   "the board ." against the quote "the board."; `<em>clause</em>,` gains a space
   before the comma; `<span>4</span>%` becomes "4 %"; a `<sup>` footnote injects
   real characters. `_extract_text` replaces tags with spaces and `span_occurs_in`
   does not fold a space a tag injected before punctuation. Three of the four are
   recovered by one extra fold (collapse whitespace before `.,;:!?%)]`).
   Fix (decision D1), separating three cases round 2 conflated:
   found in a fetched page -> verified; absent from a page we *did* fetch -> drop
   (affirmative evidence of fabrication); fetch failed/refused -> keep, unverified.

4. **`missing_context` is effectively dead** — `aggregate.py`, `_apply_rules`.
   Requiring `signals and supporting and support_strength == 2` means the verdict
   fires on 1 of 32 golden claims and **0 on the demo article** the whole design
   is built around. The canonical case it misses is fixture claim c4 ("eight in
   ten hawkers are considering leaving"): one supporting outlet plus a
   provenance-verified survey PDF stating 42 self-selected respondents — thin
   support plus a signal contradicting the framing is exactly what a
   true-but-misleading claim looks like.
   Fix (decision D2): trigger on a detected signal + credible supporting evidence
   without demanding `support_strength == 2`. Keep round 2's genuine wins — a 2-2
   refute/support tie still abstains, and refuting/signal-carrying passages stay
   in `relied` so the reader sees them.

## Majors

5. **A short cited span is treated as a fabricated one** — `judge.py`.
   `MIN_CITED_SPAN_CHARS = 12` discards golden claim hawker-07 (gold
   `contradicted`) because the judge's only span was "since 2024" — 10 characters,
   genuine and present. Hits numeric claims hardest, a core use case.
   Fix (decision D3): below the floor -> filter it out (proves nothing, condemns
   nothing); absent from every passage -> fabrication, downgrade. Require at least
   one substantive verified span, and update `prompts/judge.md` to demand a full
   clause (bump the prompt version).

6. **The eval was blinded to its own regression** — `eval/golden/fictional.jsonl`.
   Round 2 relabelled four gold entries from `missing_context` to `unverifiable`
   to match the broken rule, so the harness reported green while a whole verdict
   disappeared. Gold `missing_context` is down to 2 entries with recall 0.5.
   Fix (decision D4): gold labels record the right answer, never what the code
   currently does. Restore the four, and add a known-miss mechanism that reports
   a currently-failing claim prominently with its reason instead of hiding it.

7. **A published fact-check carries no confidence weight** — `aggregate.py`
   `_confidence`. The strongest evidence the pipeline can retrieve renders as
   "Contradicted - low".

8. **The opposing source is invisible on a decided card** — `aggregate.py`.
   Round 2 fixed this only on the `missing_context` path; a credible refuting
   source is still absent from a `supported` card, and vice versa.

9. **Provenance fetches share the search call's timeout budget** — `websearch.py`.
   A slow-but-successful search is discarded entirely and misreported as a
   provider outage. They also add roughly 8s per claim.

10. **Web search is still invisible to cost control** — `run.py` `_log_totals`.
    `WebSearchStats.calls` exists and is logged per call, but the one per-run cost
    line still reports only `llm_calls`/`tokens`. Web search is the dominant
    per-claim cost, and a reviewer measured up to **48 uncounted page fetches per
    article**, each able to download an unbounded body.

## Minors

11. A genuine primary source whose page cannot be fetched can no longer decide a
    claim, so real government evidence abstains (`aggregate.py`).
12. The `unverifiable` explanation says nothing was found even when
    `detect_signals` identified a concrete problem — not the honest account the
    rules require (`aggregate.py`).
13. The OpenAI-client leak fix reaches through the private
    `OpenAIChatTransport._client`, unpinned by any test, so a rename silently
    restores the leak (`run.py`). Prefer a public `aclose()`.
14. No ASGI-level body cap: an oversized POST is fully read and JSON-parsed
    before the schema's `maxLength` rejects it (`main.py`).
15. Golden fixtures flatter the pipeline — they grant `verified: true` to a
    web-origin passage and put primaries on `*.gov.example`, measuring a
    `supported` path the shipped providers may rarely reach (`eval/fixtures/`).

## Standing caveats (not bugs, but do not forget them)

* **No live API call has ever been made.** There is no `OPENAI_API_KEY` or
  `GOOGLE_FACTCHECK_API_KEY` in the build environment and no route to OpenAI,
  Google or data.gov.sg. Every vendor request/response shape is a written-down
  assumption; every fixture is hand-written. The first run against real keys
  should be treated as genuinely untested integration.
* **The eval gate is self-graded.** The harness author wrote both the evidence
  and the recorded model answers, so a green gate mostly proves aggregation
  behaves as its author expected. With ~32 claims it also swings on single
  errors. It is directional, not a quality guarantee.
* **429 is treated as non-retryable** (`llm.py`), so one rate-limited call loses
  a whole article. Defensible on cost grounds; still an unmade human decision.
* `run.py` imports `tally`, `done_payload`, `error_payload` and `FAILURE_MESSAGE`
  from `pipeline/mock.py`, so the real pipeline depends on the milestone-1 fake.
  Move them to a shared module before the mock is ever deleted.
