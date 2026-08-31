# Re-Vera backend

FastAPI service behind the Re-Vera Chrome extension. Milestone 1 is a walking
skeleton: `POST /check` and `GET /check/{job_id}/stream` are real, backed by a
**mocked** pipeline that streams the six fictional fixture claims over roughly
seven seconds. There are no LLM calls and no API keys yet. Redis caching and the
per-install daily cap are real from day one.

**Milestone 1 ignores the submitted article text.** The mock pipeline never
reads `CheckRequest.text`; it replays `tests/fixtures/article.json` verbatim, so
every `start`/`end` it streams indexes *that fixture's own* text, not the text
the client posted — even though `shared/schema.json` defines the pair as
character offsets into `CheckRequest.text` and milestone 3's anchoring is built
on that contract (`docs/decisions.md` §12). The offsets are deliberately left
un-faked rather than recomputed against the submitted article. **A client must
not resolve these offsets against the article it sent until the real pipeline
lands in milestone 2**; the claim's `quote` is the only field that ties it to any
real text before then.

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

Tests use [fakeredis] and never touch the network or a live Redis — step 3 is
only needed to run the server by hand. The pin is `fakeredis[lua]`, and the extra
is **not** optional: `app/events.py` publishes through a Lua script, and
fakeredis can only answer `EVAL`/`EVALSHA` when the `lupa` that extra pulls is
installed. A plain `fakeredis` pin turns the whole event suite red with
`ResponseError: unknown command 'evalsha'`. Two tests in `tests/test_events.py`
guard it so the next person to tidy the dependency list gets a clear failure
instead of that one.

[fakeredis]: https://pypi.org/project/fakeredis/

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
* **Every stream has a deadline**, derived from `MAX_CLAIMS × MOCK_STEP_DELAY`
  with a generous multiplier and a two-minute floor. A worker can vanish — the
  process restarts and the task, being process-local, is simply gone — leaving a
  job that will never publish `done`. Past the deadline the stream emits
  `event: error` with `{"type":"error","code":"timeout","message":…}` and closes.
  That one message carries **no `id:` line**: it is the relay's own event, never
  written to the job's replay list, so it borrows no sequence number and leaves
  the client's last event id where it was.

## Configuration

All settings come from the environment (and from `backend/.env` when present);
see `.env.example` for the documented set and `app/config.py` for the defaults.

| Variable                   | Default                    | Meaning                                     |
| -------------------------- | -------------------------- | ------------------------------------------- |
| `REDIS_URL`                | `redis://localhost:6379/0` | Cache, daily cap, per-job event stream      |
| `ALLOWED_EXTENSION_ORIGIN` | `chrome-extension://*`     | The single CORS origin allowed to call us   |
| `DAILY_CAP`                | `20`                       | Checks per install ID per day (Asia/Singapore) |
| `MAX_CLAIMS`               | `8`                        | Maximum claims verified per article         |
| `MOCK_STEP_DELAY`          | `0.85`                     | Seconds between mock claim events           |

Milestone-2 keys (`OPENAI_API_KEY`, `GOOGLE_FACTCHECK_API_KEY`,
`OPENAI_MODEL_EXTRACT`, `OPENAI_MODEL_STANCE`, `OPENAI_MODEL_JUDGE`) are listed
commented-out in `.env.example` and are not read yet. Secrets live only in
`backend/.env`, which is gitignored — never in this repo, and never in the
extension.

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
    routes/check.py    POST /check, GET /check/{job_id}/stream
    pipeline/mock.py   milestone-1 fake pipeline (six fixture claims)
  tests/
    fixtures/article.json   the fictional hawker article and its six claims
```

The sample article and its claims are **fictional**. They exist for fixtures and
demos and must never be presented as real reporting.
