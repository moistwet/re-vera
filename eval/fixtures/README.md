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
          "verified": true,               // optional; see "provenance_verified" below
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

### `verified` — `provenance_verified`, defaulted by bucket

Aggregation's ``side_strength`` will only let a *single* passage decide a
verdict alone (the primary-source path, or a fact-check's refutation) when
that passage's `app.pipeline.types.Passage.provenance_verified` bit is set —
its text is known to really appear where it says, not merely a model's
summary of it. Two-or-more-independent-source corroboration is not gated this
way; the corroboration is its own safeguard.

The real providers set this bit themselves: `factcheck.py`, `official.py` and
`cited.py` build passages from structured API fields or from bytes fetched
directly, so they are verified *by construction*; `websearch.py` hands the
judge a model's free-form summary of a page, so it is not, unless a future
fetch-and-check step confirms it. A fixture standing in for one of those
providers should say what it would actually have said, so `run_eval.py`
defaults `verified` to `true` for the `factcheck`, `official` and `cited`
buckets, and to `false` for `web` — an *unmarked* entry gets its bucket's
honest default. Write `"verified": false` on a factcheck/official/cited entry
(or `true` on a web entry) only to deliberately exercise the other path.

Get this wrong and the harness *undercounts*: a fixture that leaves a genuine
single-source refutation or primary support unmarked was, before this field
existed, silently treated by aggregation's strength rule as too weak to
decide anything alone, and most contradicted/supported claims in this golden
set that rely on exactly one factcheck/official/cited passage would abstain
to `unverifiable` instead — never the other way around (the gate cannot be
gamed by *over*-marking, since `unverifiable` claims carry no evidence bucket
this decides at all).

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
