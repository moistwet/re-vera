# Re-Vera backend

FastAPI service behind the Re-Vera Chrome extension. `POST /check` and
`GET /check/{job_id}/stream` are real, and since milestone 2 they are backed by
the **real five-stage pipeline** — claim extraction, evidence retrieval, stance
scoring, judging and rule-based aggregation — running over the article the
client actually posted. Redis caching and the per-install daily cap have been
real from day one.

The milestone-1 mock is still here and still works: set `USE_MOCK_PIPELINE=true`
and a check streams the six fictional fixture claims with the prototype's pacing,
no API key and no spend. That is how the extension is developed and how the demo
runs offline. It is a **switch, not a fallback** — a real check that cannot run
publishes an `error` event rather than quietly streaming fixture verdicts for
somebody's actual article, which a reader would have no way to tell apart from a
real answer.

**No live API call was ever made from this repository.** There is no
`OPENAI_API_KEY`, no `GOOGLE_FACTCHECK_API_KEY` and no route to either service in
the environment milestone 2 was built in. Every stage was written and tested
against fixtures through injected seams, and every assumption about a provider's
request or response shape is written down in the one module docstring that makes
it (`app/llm.py`, and one per provider under `app/pipeline/providers/`). Nothing
about the live path has been verified; expect the shape assumptions, not the
logic, to be what needs adjusting on the first run with a real key.

**Offsets now mean what the schema says they mean.** The mock ignores
`CheckRequest.text` and streams the fixture's own offsets, so a client must not
resolve them against the article it sent while `USE_MOCK_PIPELINE=true`. The real
pipeline extracts claims from the submitted text and guarantees
`request.text[start:end] == quote` for every claim it publishes — the contract
milestone 3's anchoring is built on (`docs/decisions.md` §12).

Requires Python 3.12 (see `.python-version`) and a Redis on `localhost:6379`.

## Quickstart

```sh
cd backend

# 1. environment — uv, pinned to Python 3.12 (the system python3 is 3.11)
uv venv --python 3.12
uv pip install -e ".[dev]"

# 2. config — copy the example and edit backend/.env (gitignored, never committed)
cp .env.example .env

# 3. Redis
docker compose up redis            # add -d to background it
#   …or use the redis-server already installed in this environment:
#   redis-server --daemonize yes
#   redis-cli ping                 # -> PONG

# 4. run the API
uv run uvicorn app.main:app --reload
#   http://127.0.0.1:8000/docs
#   …with no API key, and no spend, on the six fictional fixture claims:
#   USE_MOCK_PIPELINE=true uv run uvicorn app.main:app --reload

# 5. tests, lint, types
uv run pytest
uv run ruff check .
uv run mypy app
```

`uv run` uses `.venv` automatically, so activating the venv is optional. If you
prefer to activate it: `source .venv/bin/activate`.

The dev tools live in the `dev` **extra**, so a bare `uv sync` will not install
them — use `uv sync --extra dev` (equivalent to step 1's `uv pip install`).
`uv.lock` is written by `uv` on the first `uv run`; delete it to re-resolve.

Tests use [fakeredis] and never touch the network, a live Redis or a model — step
3 is only needed to run the server by hand, and no test has ever needed a key.
Model answers and provider responses are replayed from hand-written fixtures
through the same seams production uses. The pin is `fakeredis[lua]`, and the extra
is **not** optional: `app/events.py` publishes through a Lua script, and
fakeredis can only answer `EVAL`/`EVALSHA` when the `lupa` that extra pulls is
installed. A plain `fakeredis` pin turns the whole event suite red with
`ResponseError: unknown command 'evalsha'`. Two tests in `tests/test_events.py`
guard it so the next person to tidy the dependency list gets a clear failure
instead of that one.

[fakeredis]: https://pypi.org/project/fakeredis/

### The live-Redis smoke run

`scripts/live_redis_smoke.py` is the one thing the suite deliberately cannot do:
it runs the real pipeline against a **real `redis-server`**, because
`app/events.py` publishes through a Lua script and depends on `RPUSH` returning
the new list length inside it, on `EXPIRE` semantics and on pub/sub ordering —
three things a fake can only promise. It checks the event sequence, that the
cache is written after the last claim and before `done`, that a second check of
the same URL is served from that cache, that one failing claim still yields a
complete run, that zero extracted claims ends cleanly, that the daily cap and
the unknown-job 404 still hold, and that every per-claim cost rule holds on a
whole run.

It is **not** a pytest, so `uv run pytest` stays hermetic. It needs no API key:
the LLM and the four providers are the same fakes `tests/test_pipeline_run.py`
uses, imported rather than copied so the script cannot drift from the suite. The
only real I/O is the loopback socket to Redis.

```bash
redis-server --port 6399 --save '' --appendonly no --daemonize yes
uv run python scripts/live_redis_smoke.py --redis-url redis://localhost:6399/0
redis-cli -p 6399 shutdown nosave
```

It prints one `[PASS]`/`[FAIL]` line per check and exits non-zero if any failed.
Use a spare port: it calls `FLUSHDB` between scenarios and on the way out.

## Redis in this environment

Both routes work:

* **Docker** — `docker compose up redis` starts `redis:7-alpine` and publishes
  6379. The container runs with persistence off; the cache is disposable.
* **Native** — `redis-server` and `redis-cli` are already installed at
  `/usr/bin`. `redis-server --daemonize yes` is the fastest way to a working
  Redis when Docker is not running.

Either way `REDIS_URL=redis://localhost:6379/0` is correct.

## The shared contract

`app/schema_models.py` is **generated** from `shared/schema.json` by
`shared/generate.sh` (the same script also generates the extension's
`src/types/schema.ts`). Never hand-edit it: change `shared/schema.json`, re-run
the script from the repo root, and commit both outputs with the schema change.

```sh
./shared/generate.sh
```

## API

| Method | Path                       | Purpose                                          |
| ------ | -------------------------- | ------------------------------------------------ |
| `POST` | `/check`                   | `CheckRequest` → `CheckJob`; starts a job         |
| `GET`  | `/check/{job_id}/stream`   | Server-Sent Events for that job                   |

### Error bodies

Both error responses are raised as FastAPI `HTTPException`s, so the
`{"code", "message"}` pair arrives **nested under `detail`** — that is the shape
on the wire today, and what the extension's service worker parses:

```json
{ "detail": { "code": "daily_limit", "message": "You have used all 20 of today's checks. …" } }
```

| Status | `code`        | When                                                                 |
| ------ | ------------- | -------------------------------------------------------------------- |
| `429`  | `daily_limit` | An install ID passed `DAILY_CAP` checks today (Asia/Singapore)        |
| `404`  | `unknown_job` | A stream was opened on a job id we never handed out, or one over an hour old |

The daily cap is charged on a **cache miss only**. It exists to bound LLM spend
(`docs/decisions.md` §10) and a replay from the 7-day URL cache costs nothing, so
re-checking an article anyone has already checked is free — a class reading the
same story does not burn thirty allowances on work the backend never does. A
miss is charged before any job is spawned, so the expensive path is never free.

A cache hit is only taken once the stored claims have been re-checked against
the two product invariants (`app/invariants.py`: confidence null iff
`unverifiable`, sources empty iff `unverifiable`). An entry that breaks one —
written by an older build, or corrupted in place — is **deleted** and the request
falls through to the miss branch, so the article is re-checked and the cap is
charged as usual. Without that, one bad entry would replay as an `error` for
every reader of that URL for the full seven-day TTL with nothing able to clear
it. Only the offending claim's id and the rule it broke are logged: never the
article text, and never a URL alongside an install ID.

### The stream

The stream uses the SSE `id:` field as a monotonic per-job sequence number so a
reconnecting client can drop duplicates, `event:` for the type
(`claims_found`, `claim`, `done`, `error`) and one line of JSON in `data:`:

```
id: 1
event: claims_found
data: {"type":"claims_found","count":6,"claim_ids":["c1","c2","c3","c4","c5","c6"]}

id: 2
event: claim
data: {"id":"c3", …}

id: 8
event: done
data: {"type":"done","counts":{…},"checked_at":"2026-08-31T…"}
```

`claims_found.claim_ids` lists every claim the job will send, in **article
order** (ascending by `start`), and `count` always equals its length. The claim
events themselves arrive out of that order on purpose, so a client allocates one
row per id up front and writes each claim into the row matching its own `id`.
The cached replay announces the same ids in the same order as a live run.

A bare `: keep-alive` comment goes out every 20 s of silence so the MV3 service
worker's `fetch` is never idle long enough to be killed. Response headers are
`Cache-Control: no-cache` and `X-Accel-Buffering: no`; `Connection` is hop-by-hop
and belongs to the ASGI server, so the application never sets it.

Sequence numbers are handed out and delivered in **one** indivisible server-side
step (a Lua script in `app/events.py`), not as an append followed by a separate
publish. Two workers publishing for the same job therefore cannot let seq 4 reach
the channel ahead of seq 3 — which would not make seq 3 late but lose it, since
the relay drops anything numbered at or below what it has already sent.

#### The job window slides

A job owns two keys — `job:{id}:events` and the `job:{id}:started` marker — and
**every** published event re-issues the one-hour `EXPIRE` on *both* of them, in
that same atomic step. The hour is therefore measured from a job's most recent
event, not from `POST /check`, so a check that runs longer than an hour is never
404'd by the stream endpoint while it is still publishing. Refreshing them
together is the point: a marker on a shorter clock than its events would make the
stream refuse a live job, and events outliving their marker would be unreachable
anyway. A job that never publishes expires an hour after `POST /check` wrote the
marker.

Refreshing is not creating. `EXPIRE` is a no-op on a key that does not exist, so
publishing for an id `POST /check` never marked leaves it just as non-existent —
the 404 path below is unaffected.

The stream closes after `done` or `error`, and two things guarantee it closes:

* **Unknown job ids are refused.** `POST /check` writes a `job:{job_id}:started`
  marker before spawning the worker, and the stream 404s when it is absent. The
  stream is not covered by the daily cap, so subscribing to any id anyone asked
  for would be an unauthenticated way to pin one Redis pub/sub connection per
  request.
* **Every stream has a deadline**, derived from the configured job shape: for the
  real pipeline, the arithmetic worst case of its own timeouts (one extraction
  call, then `ceil(MAX_CLAIMS / PIPELINE_CONCURRENCY)` batches of claims, each
  claim capped at three provider calls plus a stance and a judge call, each model
  call retried `LLM_MAX_RETRIES` times); for the mock, `MAX_CLAIMS ×
  MOCK_STEP_DELAY` with a generous multiplier. Both have a two-minute floor. A
  real check takes a small fraction of its budget — the number exists to be
  larger than the slowest honest run, because cutting off a check that is still
  working would be far worse than holding one connection open. A worker can
  vanish — the process restarts and the task, being process-local, is simply
  gone — leaving a
  job that will never publish `done`. Past the deadline the stream emits
  `event: error` with `{"type":"error","code":"timeout","message":…}` and closes.
  That one message carries **no `id:` line**: it is the relay's own event, never
  written to the job's replay list, so it borrows no sequence number and leaves
  the client's last event id where it was.

## The pipeline

`app/pipeline/run.py` is the orchestrator. `POST /check` spawns it, it publishes
everything the reader sees, and it never raises — the route spawns it and never
awaits it, so a raise would be unheard:

```
extract_claims(article)                  one LLM call, article truncated first
  → publish claims_found (ids, article order)
  → per claim, PIPELINE_CONCURRENCY at a time:
        retrieve_passages   fact-check → web search → official / cited source
        score_passages      one LLM call, all of that claim's passages at once
        judge_claim         one LLM call; no passages means no call at all
        aggregate           rules only, no model
      → publish claim as soon as it resolves
  → set_check(...)  then  publish done
```

Claims are worked concurrently and published the moment each resolves, so they
arrive out of article order on purpose — that progressive fill is the product's
signature interaction, and it is why `claims_found` carries every id up front
(`docs/decisions.md` §15).

What can go wrong, and what happens:

| Situation | What the reader gets |
| --- | --- |
| One claim's provider or model call fails | That claim only, as `unverifiable`, with an evidence sentence saying the check did not finish. Every other claim gets a real verdict, and the run is **not** cached — we cache results, not outages. |
| No check-worthy claims in the article | `claims_found` with count 0 and an immediate `done` with a zeroed tally. Cached, so re-reading an opinion column is free. |
| Extraction fails, or `OPENAI_API_KEY` is unset | One `error` event and the stream closes. Never `claims_found: 0`, which would tell a reader nothing here is worth checking when nothing was checked. |

Everything a stage sends outward goes through `PipelineDeps` — the LLM client and
the four retrieval providers — so the whole pipeline runs offline with fakes:

```python
deps = PipelineDeps(llm=LLMClient(..., transport=my_fake), providers=my_providers)
await run_pipeline(redis, job_id, request, settings=settings, deps=deps)
```

`tests/test_pipeline_run.py` does exactly that: a transport that answers by
schema name and providers that return fictional passages, with no socket opened
anywhere. `PipelineDeps.build(settings)` builds the production pair instead, and
owns an HTTP client it closes when the job ends. Per-stage seams are the same
idea one level down — `app.llm.ReplayTransport` for model answers,
`app.pipeline.providers.RecordedHttpClient` for provider answers.

Each run logs one line: claims, the per-verdict tally, LLM calls, tokens and
wall-clock milliseconds. No article text, no quote, no passage, no URL and no
install id (privacy rule 6); each stage logs its own claim ids and verdicts.

## Configuration

All settings come from the environment (and from `backend/.env` when present);
see `.env.example` for the documented set and `app/config.py` for the defaults.

| Variable                   | Default                    | Meaning                                     |
| -------------------------- | -------------------------- | ------------------------------------------- |
| `REDIS_URL`                | `redis://localhost:6379/0` | Cache, daily cap, per-job event stream      |
| `ALLOWED_EXTENSION_ORIGIN` | `chrome-extension://*`     | The single CORS origin allowed to call us   |
| `DAILY_CAP`                | `20`                       | Checks per install ID per day (Asia/Singapore) |
| `MAX_CLAIMS`               | `8`                        | Maximum claims verified per article         |
| `MAX_PASSAGES_PER_CLAIM`   | `6`                        | Passages kept per claim after de-duplication |
| `MAX_ARTICLE_CHARS`        | `12000`                    | Article truncated to this before extraction |
| `PIPELINE_CONCURRENCY`     | `4`                        | Claims verified at once                     |
| `OPENAI_API_KEY`           | unset                      | Extraction, stance, judge and web search    |
| `GOOGLE_FACTCHECK_API_KEY` | unset                      | ClaimReview lookups; optional, see below    |
| `OPENAI_MODEL_EXTRACT`     | `gpt-5-mini`               | Model for stage 1                           |
| `OPENAI_MODEL_STANCE`      | `gpt-5-mini`               | Model for stage 3                           |
| `OPENAI_MODEL_JUDGE`       | `gpt-5-mini`               | Model for stage 4                           |
| `LLM_TIMEOUT_SECONDS`      | `30`                       | Hard ceiling on one model call              |
| `LLM_MAX_RETRIES`          | `2`                        | Retries *after* the first attempt; 5xx only |
| `USE_MOCK_PIPELINE`        | `false`                    | Run the milestone-1 mock instead            |
| `MOCK_STEP_DELAY`          | `0.85`                     | Seconds between mock claim events           |

Both keys are read at the point of use, never at startup: the service boots,
serves every route and runs its whole test suite with neither set. Without
`OPENAI_API_KEY` a real check ends in an `error` event whose log line names the
variable; without `GOOGLE_FACTCHECK_API_KEY` retrieval degrades to web search for
every claim, which still works and costs more — a ClaimReview hit is what lets a
claim skip the most expensive step it has.

The default model id is **account-dependent and unverified from this
repository**. It is a mini-tier id the pinned OpenAI SDK enumerates, nothing
more; an account that cannot call it gets a 4xx, which surfaces loudly and is
never retried. Set all three `OPENAI_MODEL_*` variables if that happens.

Secrets live only in `backend/.env`, which is gitignored — never in this repo,
and never in the extension.

The cost controls (`DAILY_CAP`, `MAX_CLAIMS`, `MAX_PASSAGES_PER_CLAIM`,
`MAX_ARTICLE_CHARS`, the 7-day cache) are not tuning knobs. They are what stands
between a demo and a surprising bill, and they are never raised "for testing"
outside local dev.

## Layout

```
backend/
  app/
    config.py          Settings (pydantic-settings) + get_settings()
    schema_models.py   GENERATED from shared/schema.json — do not hand-edit
    main.py            app factory, CORS, Redis lifespan, router mount
    events.py          per-job Redis event list (replay) + pub/sub (live) + started marker
    cache.py           7-day URL cache, key check:{sha256(url)} — get/set/delete
    invariants.py      the two cross-field product rules the schema cannot express
    limits.py          per-install daily cap
    llm.py             the ONLY module that imports the OpenAI SDK
    prompts/*.md       every prompt, with a version header — never inline in code
    routes/check.py    POST /check, GET /check/{job_id}/stream
    pipeline/
      run.py           the orchestrator: five stages onto the event contract
      types.py         the inter-stage vocabulary + the span verifiers
      extract.py       1 · claims with exact quotes and offsets
      retrieve.py      2 · evidence per claim, + providers/ (fact-check, search,
                           official data, cited source) behind one HTTP seam
      stance.py        3 · supports / refutes / neutral per passage
      judge.py         4 · verdict, confidence, evidence — every span verified
      aggregate.py     5 · rules, not a model; builds the provenance trail
      mock.py          milestone-1 fake pipeline (six fixture claims)
  scripts/
    live_redis_smoke.py   the real pipeline against a real redis-server (not a test)
  tests/
    fixtures/article.json   the fictional hawker article and its six claims
    fixtures/<stage>/       recorded answers per stage; each has a README
    test_pipeline_run.py    the orchestrator's event contract, stage by stage
    test_pipeline_integration.py
                            the real pipeline behind the real route, and the
                            per-article cost caps measured on a whole run
```

The sample article and its claims are **fictional**. They exist for fixtures and
demos and must never be presented as real reporting.
