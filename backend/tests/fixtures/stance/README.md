# Stage 3 fixtures — stance scoring

Two kinds of file, both read by `tests/test_stance.py`, and neither of them real.
Nothing here touches the network: there is no `OPENAI_API_KEY` in this repository
and stage 3 is driven entirely through `app.llm.ReplayTransport`.

**Passage fixtures** — `{_note, claim, passages}`. They are the *input*: one claim
(as `app.pipeline.types.ExtractedClaim` fields) and the passages retrieval would
have handed it (as `app.pipeline.types.Passage` fields).

| File | What it is |
| --- | --- |
| `passages.json` | One claim from `../article.json` and three passages: one that contradicts it, one that supports part of it, one that is merely about the topic. Passage 1 uses a curly apostrophe where the recordings quote a straight one. |
| `injected_passages.json` | The same three, plus a fourth that opens with orders to the model and then carries one sentence of unrelated content — so a test can tell obedience from analysis. |

**Recorded responses** — the format `app.llm.load_recorded_response` reads,
documented in `tests/fixtures/llm/README.md`. They are the *output*: what a model
would plausibly answer, hand-written, never captured from a live API.

| File | Against | What it records |
| --- | --- | --- |
| `hawker_scores.json` | `passages.json` | The happy path: refutes, supports, neutral, in order. |
| `shuffled_scores.json` | `passages.json` | The same three scores listed 3, 1, 2. |
| `fabricated_quote.json` | `passages.json` | `supports` for passage 1, backed by a sentence it does not contain. |
| `foreign_quote.json` | `passages.json` | Passage 1 scored on a sentence that exists in passage 2. |
| `omits_a_passage.json` | `passages.json` | Only passages 2 and 3 are scored; passage 1 is missing. |
| `out_of_range_index.json` | `passages.json` | Scores for a seventh and a zeroth passage in a batch of three. |
| `duplicate_index.json` | `passages.json` | Passage 1 scored twice, with opposite stances. |
| `injected_scores.json` | `injected_passages.json` | The prompt obeyed: the fourth passage is scored on its content, not on its orders. |
| `injected_obeyed.json` | `injected_passages.json` | The prompt disobeyed: `supports` for everything. Shows exactly how far verification reaches, and where it stops. |
| `malformed.json` | any | Prose instead of JSON, in the `content` form. |

Every article, outlet, figure and quotation in this directory is invented. Like
everything under `tests/fixtures/`, none of it may be presented as real
reporting.
