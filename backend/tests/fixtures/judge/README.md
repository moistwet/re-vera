# Stage 4 fixtures — the judge

Two kinds of file, both read by `tests/test_judge.py`, and neither of them real.
Nothing here touches the network: there is no `OPENAI_API_KEY` in this repository
and stage 4 is driven entirely through `app.llm.ReplayTransport`.

**Scored-passage fixtures** — `{_note, claim, scored}`. They are the *input*: one
claim (as `app.pipeline.types.ExtractedClaim` fields) and the passages stage 3
would have handed it, each wrapped as an `app.pipeline.types.ScoredPassage`
(`{stance, rationale_quote, passage}`).

| File | What it is |
| --- | --- |
| `passages.json` | One claim from `../article.json` and three passages: one that contradicts it, one that supports part of it, one that is merely about the topic. Passage 1 uses a curly apostrophe where the recordings quote a straight one. |
| `injected_passages.json` | The same three, plus a fourth that opens with orders demanding `supported` — the opposite of what the real passages show, so an obeyed injection is visible in the verdict — and then carries one sentence of unrelated content. |

**Recorded responses** — the format `app.llm.load_recorded_response` reads,
documented in `tests/fixtures/llm/README.md`. They are the *output*: what a model
would plausibly answer, hand-written, never captured from a live API.

Every one of them is replayed against `passages.json` unless the table says
otherwise, and every one is also run through
`test_every_path_returns_a_publishable_judgement`, which holds whatever comes out
to the two rules a claim cannot reach the wire without.

| File | What it records | What the stage does with it |
| --- | --- | --- |
| `contradicted.json` | The happy path: two real spans, one retyped with a straight apostrophe and a line break in the middle. | Accepted as `contradicted` / `high`. |
| `capitalised_verdict.json` | `" Contradicted "` and `"High"`. | Normalised, not rejected. |
| `fabricated_span.json` | A confident `supported` cited on a sentence in none of the passages. | Downgraded — the milestone's headline test. |
| `mixed_spans.json` | One real span and one invented one. | Downgraded: verification is all-or-nothing. |
| `stitched_span.json` | A span running from the end of passage 1 into the start of passage 2. | Downgraded: spans are checked per passage, never against the concatenation. |
| `trivial_span.json` | `4 per cent` — genuinely in passage 1, and ten characters. | Downgraded: under `MIN_CITED_SPAN_CHARS`. |
| `whitespace_padded_span.json` | `40    per    cent` — seventeen raw characters (over the floor unnormalised) that collapse to eleven (`40 per cent`, under it) once whitespace runs fold to one space. | Downgraded: the floor is measured on the normalised string, so padding cannot buy a short fragment past it. |
| `unknown_verdict.json` | A fifth verdict, `false`. | Downgraded. |
| `bad_confidence.json` | Confidence `87%`. | Downgraded (rule 3: never a percentage). |
| `null_confidence.json` | A decided verdict with a null confidence. | Downgraded (the iff-rule, other way round). |
| `no_spans.json` | A decided verdict citing nothing at all. | Downgraded. |
| `empty_evidence.json` | A decided verdict with no evidence sentence. | Downgraded (rule 2). |
| `long_evidence.json` | An "evidence sentence" of several hundred characters. | Downgraded: past `MAX_EVIDENCE_CHARS`. |
| `confident_unverifiable.json` | `unverifiable` carrying `high`. | Confidence dropped; the model's explanation and verified spans kept. |
| `bare_unverifiable.json` | `unverifiable` with no spans and no sentence. | An explanation naming what was searched is written for it. |
| `malformed.json` | Prose instead of JSON, in the `content` form. | `LLMInvalidOutput` → unverifiable, never retried. |
| `injected_resisted.json` | Against `injected_passages.json`: the orders read as data. | Verdict comes from the passages that address the claim. |
| `injected_fabricated.json` | Against `injected_passages.json`: the demanded verdict, backed by a composed sentence. | Downgraded. |
| `injected_obeyed.json` | Against `injected_passages.json`: the demanded verdict, quoting the injected page verbatim. | **Passes.** Records exactly where this stage's guarantee stops — it proves a citation is real, never that the page is honest. |

Every article, outlet, figure and quotation in this directory is invented. Like
everything under `tests/fixtures/`, none of it may be presented as real
reporting.
