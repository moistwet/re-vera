# Re-Vera evaluation harness

Runs the claim-checking pipeline over a labelled set of claims and reports
whether it is any good — per-verdict precision and recall, the abstention rate,
a confusion matrix, and the one number allowed to fail a build.

```sh
cd backend                                  # everything runs through the backend venv
uv run python ../eval/run_eval.py           # offline by default: no key, no network, no spend
uv run pytest ../eval/tests                 # the harness has its own tests
```

## The gate

**`precision on contradicted >= 0.90`.** `run_eval.py` exits non-zero when it
fails, and CI fails with it.

Precision on `contradicted` is the gate because telling a reader that a true
statement has been contradicted is the worst thing this product can do. It is
worse than missing a false claim, and much worse than abstaining: a reader who
sees "Contradicted" on an accurate sentence has been actively misinformed by the
tool that promised to protect them.

**A run that predicts nothing `contradicted` does not pass.** Precision is
undefined on zero predictions, and a gate that an empty answer clears would make
"never answer" the cheapest way through it.

### The gate is directional, not a statistical guarantee

Read this before you quote the number anywhere.

With ~32 golden claims and roughly a dozen `contradicted` labels, the gate moves
in steps of about 0.08. One false positive out of eight predictions is 0.875 — a
failure. Nine out of ten is 0.900 — a pass, by exactly nothing. The confidence
interval around any of these numbers is far wider than the distance between pass
and fail.

So: the gate is a **tripwire against a change that makes things obviously
worse**, not evidence that the pipeline is accurate. The confusion matrix printed
above it is the part a human should actually read, and a failing run should be
diagnosed claim by claim (`claims_detail` in the JSON summary) rather than
treated as a score. Growing the golden set with hand-labelled real Singapore
articles is what would turn this into a measurement; until then it is a smoke
alarm.

### Exit codes

| code | meaning |
| --- | --- |
| `0` | every claim was checked and the gate passed |
| `1` | the run happened and something is wrong: the gate failed, or a claim errored |
| `2` | the harness could not run at all — a malformed golden set, a missing recording, `--live` with no key |

`2` is separate from `1` on purpose. A red build that measured nothing and a red
build that measured something bad need different responses, and a CI log is read
in a hurry.

## What is actually being measured

Each line of `golden/fictional.jsonl` is one **claim**, not an article, so the
harness runs **stages 2–5** — `retrieve` → `stance` → `judge` → `aggregate` —
through `app.pipeline.run.check_claim`, the same function the live pipeline
calls for every claim. Nothing is reimplemented here.

**Stage 1 (claim extraction) is not scored.** What extraction gets right or
wrong is *which* sentences it picks out of an article and whether it copies them
character for character — neither of which a per-claim golden set can express.
That needs an article-level golden set with gold claim spans, and it does not
exist yet. It is the most important missing measurement in this directory:
extraction silently drops any claim whose quote it paraphrased, so a reader can
lose half an article's claims with nothing in the report to show it. The stage
logs `N candidates -> M located -> K claims`, which is where that ratio will
first be visible.

Also not scored: confidence levels, evidence wording, and the provenance trail.
The harness checks that every claim it produces obeys the two product invariants
(`sources` empty iff `unverifiable`; `confidence` null iff `unverifiable`), but
it does not grade the prose a reader sees.

## The two modes

### `--offline` (default — and what CI runs)

Every provider answer and every model answer is replayed from `eval/fixtures/`.
No API key, no network, no spend, byte-identical output across runs. A
process-wide socket guard is armed before any work starts, so an accidental
outbound call raises `NetworkAccessDenied` and fails the run instead of quietly
costing money.

**What an offline number means, and what it does not.** The recordings are
hand-written — this repository has no keys and no route to OpenAI or Google, so
nothing in `eval/fixtures/` was ever captured from a live API. An offline run
therefore measures **our code**: retrieval's ClaimReview short-circuit, its
wire-copy de-duplication, ranking and cap; stance verification; the judge's
citation check; and the whole of aggregation's rules — against evidence a person
chose. It is a regression test with a gate on it.

It says **nothing** about whether a real model would return those answers, how
often a real model paraphrases a quote instead of copying it, or how much
evidence real web search would find. Only `--live` can speak to any of that, and
nobody has run it yet.

Because the evidence and the recorded answers were written together, most claims
come out right by construction. That is why the set deliberately includes two
claims the pipeline gets **wrong** (see below): a report that always prints 1.000
is not a signal, it is a mirror.

**Nothing outside the fixtures can move an offline number.** Settings are built
with `_env_file=None`, so a developer's `backend/.env` cannot change a CI result
— and, because pydantic-settings still reads the process environment,
`run_eval.OFFLINE_PINNED` additionally pins the one field that would change a
score (`max_passages_per_claim`, which caps what retrieval keeps) back to the
default declared in `app/config.py`. Exporting `MAX_PASSAGES_PER_CLAIM=1` used to
take this set from an abstention rate of 0.219 to 0.531 with nothing in the
report saying why. `--live` pins nothing: a live run should use the configuration
it was deliberately pointed at.

### `--live`

Calls the real OpenAI and Google APIs. Costs money. Needs `OPENAI_API_KEY` in
`backend/.env`; `GOOGLE_FACTCHECK_API_KEY` is optional and its absence makes
every claim fall through to web search, which is more expensive. Run it
deliberately, by hand, and expect the numbers to differ from the offline ones —
if they do not, something is wrong with the fixtures.

```sh
cd backend
uv run python ../eval/run_eval.py --live
uv run python ../eval/run_eval.py --record   # implies --live; rewrites eval/fixtures/
```

`--record` captures each provider's passages (before de-duplication, so a replay
re-runs those steps on the same input the live run had) and each model's answer,
and writes them back into `eval/fixtures/<article_id>.json`. It rewrites the
whole set, so run it on the whole set. **`--record` has never been executed** —
there is no key in this environment — so treat its first run as something to
review rather than to trust.

## The golden set

`golden/fictional.jsonl`, one JSON object per line. The first line is a metadata
record carrying `_fictional`; the loader refuses to read any claim before it.

```jsonc
{ "id": "hawker-01", "article_id": "hawker-rents",
  "article_url": "https://example.com/...", "quote": "rise by 40% from 1 January",
  "kind": "numeric", "gold_verdict": "contradicted",
  "gold_sources": ["https://data.gov.example/..."], "notes": "why this claim is here" }
```

**Everything in it is invented.** Every article, outlet, official body,
fact-checker, figure and quotation is fictional, and every URL points at
`example.com` or a `.example` host — domains reserved by RFC 2606/6761 precisely
so invented data cannot resolve to, or be mistaken for, somebody's real
reporting. A test enforces the URL rule. Never present any line of this file, or
anything derived from it, as real reporting.

32 claims across 6 fictional Singapore-flavoured articles, covering all four
verdicts and the hard cases the brief asks for:

| case | claims |
| --- | --- |
| true-but-misleading statistic (`missing_context`) | `hawker-04`, `mrt-04`, `hdb-03`, `dengue-03`, `screens-03` |
| genuinely no evidence (`unverifiable`) | `hawker-02`, `mrt-05`, `hdb-05`, `dengue-04`, `screens-05` |
| misquotation (`attribution`) | `hawker-05`, `mrt-03`, `hdb-04`, `otter-03`, `screens-04` |
| wire-copy-only, collapsing to one source | `otter-01` |
| numeric claims against official data | `hawker-01`, `mrt-01`, `hdb-01`, `dengue-01`, `screens-01` |
| ClaimReview hit short-circuiting web search | `otter-02`, `screens-03` |
| rules overruling the judge | `hawker-04`, `otter-01` |

### Two claims the pipeline is expected to get wrong

Kept on purpose, with `KNOWN MISS` in their `notes`. Both fail in the safe
direction — they cost recall, never `contradicted` precision.

* **`hawker-07`** — gold `contradicted`, predicted `unverifiable`. The judge
  cites a real but ten-character span (`since 2024`), below
  `judge.MIN_CITED_SPAN_CHARS` (12), so the citation is discarded and the claim
  is downgraded. This is the documented cost of that threshold.
* **`mrt-06`** — gold `missing_context`, predicted `supported`. The statement is
  true and two independent outlets report it, but it is cherry-picked against a
  depressed baseline that no passage states as a number. Cherry-picking is the
  one true-but-misleading signal `aggregate.py` cannot detect; it reaches a
  reader only if the judge spots it in the prose, and here it did not.

If either is ever fixed, the fix will show up as a metric improving, which is
what a golden set is for.

## Reading the report

```
verdict           gold  pred  hit  precision   recall      f1
supported            8     9    8      0.889    1.000   0.941
...
```

`gold` is how many claims carry that label, `pred` how many the pipeline
produced, `hit` the true positives. A metric prints as `—` when it is
**undefined**, never as `0.000` or `1.000`: a verdict nobody predicted has
undefined precision, and a verdict absent from the golden set has undefined
recall. Both of those are exactly the situations a reader of the report needs to
notice.

`abstention rate` is the share answered `unverifiable`. It has no target. A high
rate can mean the pipeline is honest or that retrieval is broken, and only the
confusion matrix distinguishes them.

`source hit rate` is the share of decided claims (gold and predicted both
non-`unverifiable`) that cite at least one domain from `gold_sources`. It catches
a pipeline that reaches the right verdict from the wrong evidence.

`llm calls` appears only on a `--live` run and reports the whole run's calls and
tokens (`app.pipeline.run.LLMMeter`). An offline run prints no cost line at all
rather than a bill of zero: a replay costs nothing, and "0 tokens" beside a
passing gate would read as a live run that was somehow free.

A claim that **errored** is excluded from every metric and listed separately, and
any error fails the run. Scoring an unchecked claim as an abstention would let an
outage read as caution.

The JSON summary (stdout, or `--json-out PATH`) carries the same numbers plus
`claims_detail`, which is the per-claim gold/predicted pair to diff when
something regresses.

## Layout

```
eval/
  README.md              this file
  run_eval.py            the harness
  golden/fictional.jsonl the labelled claims (all fictional)
  fixtures/*.json        one recording per article — see fixtures/README.md
  tests/test_run_eval.py the harness's own tests
```

## CI

`.github/workflows/eval.yml` runs on changes to `backend/app/**` (which covers
the `prompts/` and `pipeline/` paths `CLAUDE.md` names, plus `app/llm.py`,
`app/config.py`, `app/invariants.py` and `app/schema_models.py`, which the
harness imports), `backend/tests/**`, the dependency pins, `eval/**` and the
workflow itself: backend suite, harness suite, `ruff`, `mypy`, then
`run_eval.py --offline`. It never needs a key and never calls OpenAI or Google —
both key variables are pinned empty in the job's `env` so that an accidental live
path fails there rather than picking up a secret that happened to be in scope.

Lint and types are invoked from `backend/` so that `eval/` inherits
`backend/pyproject.toml`'s configuration — `ruff` falls back to the working
directory's config for files that have none of their own, and `eval/` has none by
design. Running `ruff check ../eval` from anywhere else applies ruff's defaults
instead and will disagree.
