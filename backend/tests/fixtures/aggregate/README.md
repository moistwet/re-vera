# Stage 5 fixtures — aggregation

One file, `passages.json`, read by `tests/test_aggregate.py`. Nothing here is
real and nothing here is fetched: stage 5 makes no HTTP call and no model call
at all, so a fixture for it is simply the values `retrieve` → `stance` →
`judge` would have handed it.

```jsonc
{
  "claim":       { …app.pipeline.types.ExtractedClaim fields… },
  "article_url": "the page the reader is on",
  "sets":        { "<name>": [ …Passage fields + stance + rationale_quote… ] },
  "judgements":  { "<name>": { verdict, confidence, evidence, cited_spans } }
}
```

Each entry under `sets` carries the seven `Passage` fields plus the two that
belong to the `ScoredPassage` wrapper. Each judgement is keyed by its set's name
and is **unverified model output** by construction — the tests override its
fields freely to see what the rules do with a judge that lies, abstains, or
answers with a word that is not a verdict.

| Set | The rule it exercises |
| --- | --- |
| `contradicted` | A high-confidence refutation: an official release, an independent report, and one wire reprint. |
| `supported_independent` | Two independent supporters on two domains — the "two or more" half of rule 2. |
| `supported_primary` | One official dataset, alone — the "one primary source" half. |
| `missing_context_small_sample` | Reported figure, 42 self-selected respondents behind it. |
| `missing_context_outdated` | Support from 2024 against a 2026 revision, a gap measured between passages and never against the clock. |
| `missing_context_rating` | A ClaimReview whose own rating is "Partly true". |
| `wire_on_five_domains` | One agency story under five mastheads: five domains, one source, so "two independent" is not met. |
| `conflict` | An official refutation against two independent supporters — credible sources that disagree. |
| `undated_and_unnamed` | A passage with no date and a blank outlet: an empty date and a domain fallback, never a guess. |
| `aggregator_and_self` | An aggregator's reprint and the article citing itself, neither of which is evidence. |

Every cited span in `judgements` is an exact substring of one of its set's
passages, so a test that wants a fabricated citation has to introduce one
deliberately (`run(..., cited_spans=[...])`) rather than inheriting it.

Every article, outlet, URL, figure, date, quotation and rating in this directory
is invented. Like everything under `tests/fixtures/`, none of it may be
presented as real reporting: the outlet names are fictional, the domains are
`.test`/`.example` reserved names that cannot resolve, and the "Hawker Centres
Board" is not any real body.
