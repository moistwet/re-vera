# Stage 1 fixtures — claim extraction

Two kinds of file, both read by `tests/test_extract.py`, and neither of them
real. Nothing here touches the network: there is no `OPENAI_API_KEY` in this
repository and stage 1 is driven entirely through `app.llm.ReplayTransport`.

**Article fixtures** — `{_fictional, url, title, text}`, the same shape as
`tests/fixtures/article.json` minus its claims. They are the *input*: the text
stage 1 truncates, fences and later searches for quotes in.

| File | What it is |
| --- | --- |
| `opinion_article.json` | An opinion column: judgements, forecasts and rhetorical questions, with nothing checkable in it. |
| `repetition_article.json` | States one fact three times — twice word for word, once differing only by a capital letter — for the repeated-quote and de-duplication tests. |
| `injected_article.json` | Contains a prompt-injection paragraph that closes the fence early and demands a fabricated claim. |

**Recorded responses** — the format `app.llm.load_recorded_response` reads,
documented in `tests/fixtures/llm/README.md`. They are the *output*: what a
model would plausibly answer, hand-written, never captured from a live API.

| File | Against | What it records |
| --- | --- | --- |
| `hawker_claims.json` | `../article.json` | The happy path: seven exact quotes with a spread of check-worthiness scores. |
| `invented_quotes.json` | `../article.json` | Two exact quotes and two paraphrases that appear nowhere in the article. |
| `duplicate_fact.json` | `../article.json` | One fact quoted twice as overlapping spans — a clause and the sentence containing it. |
| `case_variant_fact.json` | `repetition_article.json` | One fact quoted from two sentences, identical but for a capital letter. |
| `repeated_quote.json` | `repetition_article.json` | One quote that occurs twice in the article. |
| `out_of_range_score.json` | `../article.json` | Check-worthiness of `95.0` and `-3.0` — a mis-scaled scale. |
| `opinion_none.json` | `opinion_article.json` | No claims at all, which is the right answer there. |
| `injection_attempt.json` | `injected_article.json` | A model that half-obeyed the injection and returned the fabricated claim it asked for. |
| `malformed.json` | any | Not JSON at all, in the `content` form. |

Every article, outlet, figure and quotation in this directory is invented. Like
everything under `tests/fixtures/`, none of it may be presented as real
reporting.
