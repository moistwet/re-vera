# Recorded answers for the offline eval

One file per fictional article, named `<article_id>.json`, holding a recording
for every golden claim in that article. `run_eval.py --offline` replays these and
makes no network call at all.

## Everything here is invented, and none of it was captured

This repository has no `OPENAI_API_KEY`, no `GOOGLE_FACTCHECK_API_KEY` and no
route to OpenAI, Google, data.gov.sg or any web page. **Nothing in this directory
was recorded from a live API.** Each entry is a hand-written plausible answer:
what a provider or a model *would* return for one claim of the fictional golden
set.

That has a consequence worth stating plainly. These fixtures are evidence about
**our code** — whether retrieval de-duplicates wire copy, whether the judge's
citations are verified, whether aggregation's rules reach the right verdict on a
given shape of evidence. They are **not** evidence about the world, and they are
not evidence that any real model returns answers of this shape or quality. Only
`run_eval.py --live` can speak to that, and nobody has run it.

Every outlet, official body, fact-checker, figure, quotation and URL is
fictional, and every URL points at `example.com` or a `.example` host (reserved
by RFC 2606/6761). A test in `eval/tests/test_run_eval.py` enforces the URL rule.
Never present any of this as real reporting.

## Format

```jsonc
{
  "_fictional": "the note above, in machine-readable form",
  "claims": {
    "hawker-01": {
      // Four optional buckets, one per provider. The bucket a passage sits in
      // IS its origin, so it is never repeated: factcheck | web | official |
      // cited (-> "cited_source"). Which buckets retrieval actually consults
      // depends on the claim's `kind`, and a non-empty `factcheck` bucket
      // short-circuits `web` — that cost rule is under test here, so a claim may
      // record a `web` passage that must never be reached.
      "official": [
        {
          "outlet": "Hawker Centres Board",
          "url": "https://data.gov.example/datasets/hawker-rental-review-2026",
          "date": "2026-03-12",          // optional; omit when the source states none
          "wire": true,                   // optional, default false
          "rating": "Partly true",        // optional; only meaningful on a factcheck passage
          "stance": "refutes",            // optional, default "neutral"
          "quote": "the median stall rent adjustment at 4 per cent",
          "text": "the passage itself — the only field a model is shown"
        }
      ],
      // The recorded JudgeResponse. Omit it for a claim retrieval finds nothing
      // for: the judge short-circuits without a model call there, and a
      // recording for a call that never happens is a fixture nobody can be
      // wrong about.
      "judge": {
        "verdict": "contradicted",
        "confidence": "high",
        "evidence": "One plain sentence naming the sources.",
        "cited_spans": ["a span that really occurs in one of the passages above"]
      }
    }
  }
}
```

### `stance` and `quote` are per passage, not per index

The stance model answers by passage *index*, and the index a passage gets is
decided by `retrieve_passages` at run time — after de-duplication, ranking and
the cap. A fixture that hard-coded an index would silently mis-align against the
wrong passage the day ranking changed.

So a recording states each passage's stance beside the passage, and
`run_eval.stance_recording` reads the indices back out of the message stage 3
actually built. A passage with no recorded stance is answered `neutral` with no
quote — the same thing `score_passages` does with a passage the model skipped, so
an incomplete fixture degrades to an abstention rather than to an invented
verdict.

### Two things the code will check, and reject

* **`quote` must be a real substring of that passage's `text`.** Stage 3 verifies
  it with `span_occurs_in` (forgiving about typography and case, strict about
  words) and forces the passage to `neutral` if it is not there.
* **Every `cited_spans` entry must occur in a passage the judge was shown, and be
  at least 12 characters.** Stage 4 verifies it, and stage 5 verifies it again;
  a span that is not found downgrades the claim to `unverifiable`. That is the
  milestone's most important correctness property, so a fixture cannot opt out
  of it — `hawker-07` exists to demonstrate the threshold biting a legitimate
  citation.

## Regenerating

`run_eval.py --record` (which implies `--live`, and therefore a key and real
spend) rewrites every file here from what the real APIs returned. It rewrites the
**whole** set, so run it over the whole set. It has never been executed; treat
its first output as something to review line by line rather than to trust.
