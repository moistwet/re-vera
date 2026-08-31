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

Each entry under `sets` carries the eight `Passage` fields (including
`provenance_verified`) plus the two that belong to the `ScoredPassage`
wrapper. `provenance_verified` is set per entry to match the contract every
provider is meant to honour: `official`/`factcheck`/`cited_source` entries are
`true` (built from a structured API field or bytes fetched directly — verified
by construction), `web`-origin entries are `false` (a model's free-form
summary, never confirmed against the page). Each judgement is keyed by its
set's name and is **unverified model output** by construction — the tests
override its fields freely to see what the rules do with a judge that lies,
abstains, or answers with a word that is not a verdict.

| Set | The rule it exercises |
| --- | --- |
| `contradicted` | A high-confidence refutation: an official release, an independent report, and one wire reprint. |
| `supported_independent` | Two independent supporters on two domains — the "two or more" half of rule 2. |
| `supported_primary` | A data.gov.sg **catalogue** entry, alone — M8: never enough, however government-flavoured the domain. |
| `supported_primary_press_release` | One genuine government press release, alone — the "one primary source" half of rule 2. |
| `missing_context_small_sample` | Reported figure, corroborated by two outlets, 42 self-selected respondents behind it — M7 needs the corroboration for `missing_context` to apply at all. |
| `missing_context_outdated` | Support from 2024 against a 2026 revision, a gap measured between passages and never against the clock. |
| `missing_context_rating` | A ClaimReview whose own rating is "Partly true". |
| `wire_on_five_domains` | One agency story under five mastheads: five domains, one source, so "two independent" is not met. |
| `conflict` | An official refutation against two independent supporters — credible sources that disagree. |
| `undated_and_unnamed` | A passage with no date and a blank outlet: an empty date and a domain fallback, never a guess. |
| `aggregator_and_self` | An aggregator's reprint and the article citing itself, neither of which is evidence. |
| `self_site_citation` | B3: the article cites a sibling page on its own site — never primary, never independent. |
| `conflict_with_signal` | B2: a strong ClaimReview refutation tied against strong-but-signalled support — the tie is checked before `missing_context`, so the refutation is not silently dropped. |
| `single_outlet_conflict` | M16: the same outlet's own notices disagree — a genuine tie naming only one outlet, for the singular-grammar case. |
| `stance_mismatch_cap` | M5: under a tight per-claim cap, only a refuting chip survives for an otherwise-`missing_context` claim — must never be described as "backing" it. |

Every cited span in `judgements` is an exact substring of one of its set's
passages, so a test that wants a fabricated citation has to introduce one
deliberately (`run(..., cited_spans=[...])`) rather than inheriting it.

Every article, outlet, URL, figure, date, quotation and rating in this directory
is invented. Like everything under `tests/fixtures/`, none of it may be
presented as real reporting: the outlet names are fictional, the domains are
`.test`/`.example` reserved names that cannot resolve, and the "Hawker Centres
Board" is not any real body.
