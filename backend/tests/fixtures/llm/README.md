# Recorded LLM responses

Hand-written stand-ins for what a model would return, replayed offline by
`app.llm.ReplayTransport` so that every pipeline stage can be tested without an
API key, without a network, and without spending anything.

**These are not captures from a live API.** This repository has no
`OPENAI_API_KEY` and no route to the OpenAI endpoint, so nobody has ever seen a
real response to compare them against. They record what the pipeline *should do
with a plausible answer* — which is exactly what a stage test needs — and they
are no evidence at all that a real model returns this shape. Anything they imply
about the provider's wire format is an assumption, documented in
`app.llm.OpenAIChatTransport`.

Any article, outlet, claim or source that appears in one of these files is
**fictional**, like everything in `tests/fixtures/article.json`. Never present it
as real reporting.

## Format

One JSON object per file, loaded by `app.llm.load_recorded_response(path)` into
an `LLMResponse`:

```json
{
  "_note": "What this recording is and which stage replays it.",
  "json": { "…": "the answer, written readably" },
  "prompt_tokens": 812,
  "completion_tokens": 143
}
```

| Key | Meaning |
| --- | --- |
| `_note` | Free text for the next reader. Ignored by the loader. |
| `json` | The answer as an object. Serialised for you — use this form. |
| `content` | The answer as exact bytes. Use **only** to record a deliberately malformed answer, which `json` cannot express. |
| `prompt_tokens` | Optional, defaults to 0. Plausible numbers keep the token-accounting assertions honest. |
| `completion_tokens` | Optional, defaults to 0. |

Give either `json` or `content`. If both are present, `json` wins.

## Using one

```python
from app.llm import LLMClient, ReplayTransport, load_recorded_response

recorded = load_recorded_response(FIXTURES / "llm" / "example_response.json")
transport = ReplayTransport([recorded])
client = LLMClient(api_key="test-key-unused", timeout=5.0, max_retries=0,
                   transport=transport)
```

`ReplayTransport` takes a list of outcomes and consumes it in order. An entry
that is an `Exception` is raised instead of returned, which is how a test
scripts "503, then success":

```python
ReplayTransport([LLMUnavailable("503"), recorded])
```

Every call is appended to `transport.calls`, so a test can assert how many calls
a stage made (the no-retry-on-4xx rule) and what it sent. Running past the end
of the list raises `AssertionError` rather than quietly making an extra call.

## Adding one

Name it `<stage>_<what it is>.json`, keep it as small as the assertion needs,
and say in `_note` which test replays it. Prefer several small recordings over
one that several tests share and nobody dares change.

## Files

| File | What it is |
| --- | --- |
| `example_response.json` | A minimal well-formed answer, matching the throwaway model in `tests/test_llm.py`. The format's worked example. |
| `malformed_response.json` | Not valid JSON at all — the `content` form, for the test that a bad answer surfaces as `LLMInvalidOutput` rather than being retried or passed through. |
