# Milestone 1 plan — Skeleton

Status: **proposed, awaiting approval** (per the plan-before-code agreement).

## Goal

End-to-end streaming walking skeleton with zero LLM calls: the extension loads
unpacked, the popup's ready state shows the current article, clicking **Check
this article** extracts the text, POSTs it to the backend, and the popup renders
the six fictional fixture claims streaming in over ~7 s (matching the demo's
pacing) with the stepper, claim rows, and done-state counts. Redis caching and
the install-ID daily cap work. No highlights, no side panel, no game mode.

## Non-goals (later milestones)

Real pipeline stages (M2) · on-page overlay/anchoring (M3) · side panel + Guess
first (M4) · not-an-article/error/daily-limit popup states beyond a minimal
error row, per-IP limits, a11y, motion polish (M5).

## Proposed dependencies (approval of this plan approves these)

**Backend runtime:** `fastapi`, `uvicorn[standard]`, `pydantic` (v2),
`pydantic-settings`, `redis` (redis-py, asyncio).
**Backend dev:** `pytest`, `pytest-asyncio`, `httpx` (ASGI test client),
`fakeredis` (tests never need a real Redis), `ruff`, `mypy`,
`datamodel-code-generator` (JSON Schema → Pydantic).
**Extension runtime:** `react`, `react-dom`. (`@mozilla/readability` is already
mandated by the brief.)
**Extension dev:** `typescript`, `vite`, `@vitejs/plugin-react`,
`@crxjs/vite-plugin` (MV3 manifest/HMR glue), `eslint` + `typescript-eslint`,
`json-schema-to-typescript` (schema codegen), `vitest` (unit test for the SSE
parser only).

Deliberately not used: no task queue (worker = `asyncio.create_task` in-process,
fine for the hackathon), no `sse-starlette` (SSE framing is ~10 lines by hand),
no state library in React.

Tooling assumption: Python via `uv` (fallback `pip` + `requirements.txt` if `uv`
is unavailable), Node via `pnpm`.

## File tree

```
shared/
  schema.json                 # single source of truth (JSON Schema 2020-12)
  generate.sh                 # regenerates both outputs; CI/devs run after edits
backend/
  pyproject.toml              # deps + ruff + mypy config
  docker-compose.yml          # redis:7-alpine
  .env.example                # REDIS_URL, ALLOWED_EXTENSION_ORIGIN, DAILY_CAP=20 (+ M2 keys commented)
  app/
    __init__.py
    main.py                   # FastAPI app factory, CORS, router mount, redis lifespan
    config.py                 # Settings (pydantic-settings): redis_url, allowed_extension_origin, daily_cap, max_claims
    schema_models.py          # GENERATED from shared/schema.json — do not hand-edit
    events.py                 # publish_event(job_id, event) / stream_events(job_id): Redis list (replay) + pub/sub (live), seq-deduped
    cache.py                  # get_check(url) / set_check(url, result): key check:{sha256(url)}, TTL 7d
    limits.py                 # check_daily_cap(install_id): INCR cap:{install_id}:{YYYYMMDD} (Asia/Singapore date), 48h expiry, raise 429 past DAILY_CAP
    routes/
      __init__.py
      check.py                # POST /check, GET /check/{job_id}/stream (SSE, keep-alive comment every 20s)
    pipeline/
      __init__.py
      mock.py                 # run_mock_pipeline(job_id, request): claims_found → 6 claims (850ms apart, demo order) → done; writes cache
  tests/
    conftest.py               # fakeredis fixture, app fixture, fixture loader
    fixtures/
      article.json            # fictional hawker article: url, title, text, 6 claims w/ verified offsets
    test_schema.py            # every fixture claim validates; text[start:end] == quote for all 6
    test_limits.py            # 20 pass, 21st → 429 with clear message
    test_cache.py             # set/get roundtrip, key is sha256, TTL set
    test_check_flow.py        # POST → stream yields claims_found(6), 6 claim events, done with correct counts; second POST → cached=true, replay is immediate
extension/
  package.json  tsconfig.json  vite.config.ts  .eslintrc.cjs  .env.example  # VITE_API_BASE
  manifest.config.ts          # MV3: storage, activeTab, scripting, sidePanel; host_permissions [VITE_API_BASE]
  src/
    types/schema.ts           # GENERATED from shared/schema.json — do not hand-edit
    shared/messages.ts        # typed runtime messages: START_CHECK, CHECK_EVENT, GET_STATE, STATE
    background/
      index.ts                # message router; onMessage(START_CHECK) → runCheck(tabId)
      installId.ts            # getInstallId(): crypto.randomUUID persisted in storage.local
      api.ts                  # postCheck(req) → CheckJob; openStream(jobId, onEvent) via fetch + ReadableStream
      sse.ts                  # parseSSE(stream): AsyncGenerator<{event, data}> — handles chunk splits, comments/keep-alives
      jobStore.ts             # in-memory + storage.session job state so the popup can re-sync after reopen/SW restart
    content/
      extract.ts              # extractArticle(): JSON-LD NewsArticle first, else Readability → {url, title, text}; injected on demand via chrome.scripting
    popup/
      index.html  main.tsx
      App.tsx                 # state machine: ready | checking | done | error, driven by GET_STATE + CHECK_EVENT messages
      Stepper.tsx             # 3-step stepper per handoff (done ✓ / active pulse / pending)
      ClaimRow.tsx            # pending skeleton ↔ resolved icon + ellipsized quote
      Summary.tsx             # done-state counts line, verdict text colours
      verdictIcons.tsx        # four 16×16 stroke SVGs lifted from the demo
      theme.css               # tokens from docs/design-handoff.md (light theme only in M1)
  tests/sse.test.ts           # parseSSE: split-chunk events, multiple events per chunk, comment lines ignored
.gitignore  README.md         # quickstart: docker compose up redis · uvicorn · pnpm dev · load unpacked
```

## Key behaviours

- **`shared/schema.json`** defines `CheckRequest`, `CheckJob`, `Source`,
  `TrailNode`, `Claim` (with `confidence: low|medium|high|null`), and the four
  SSE event payloads. `generate.sh` runs `datamodel-code-generator` →
  `backend/app/schema_models.py` and `json-schema-to-typescript` →
  `extension/src/types/schema.ts`; both outputs are committed.
- **POST /check**: validate → `limits.check_daily_cap` → cache lookup. Hit:
  create job whose stream replays everything instantly, return
  `{cached: true, claim_count: N}`. Miss: `asyncio.create_task(run_mock_pipeline)`,
  return `{cached: false, claim_count: null}`.
- **GET /check/{job_id}/stream**: replay the job's Redis event list, then relay
  pub/sub live, deduped by sequence number; `: keep-alive` comment every 20 s;
  closes after `done`/`error`.
- **Mock pipeline**: emits the six fixture claims in the demo's order with
  ~850 ms gaps and stores the finished result in the cache — so the cache path
  and the streaming path are both real even though the pipeline is fake.
- **Fixture offsets are verified by test**, not by hand: `text[start:end] == quote`
  for all six claims (this contract is what M3 anchoring depends on).
- **Popup re-sync**: on open, popup sends `GET_STATE`; background replies with
  the current job snapshot, then keeps pushing `CHECK_EVENT`s. Closing and
  reopening the popup mid-check shows the correct partial state.

## Definition of done

- `ruff check` + `mypy` clean; `pytest` green (backend). `pnpm typecheck` +
  `pnpm lint` clean; `vitest` green (extension).
- Manual pass: `docker compose up redis` → `uvicorn app.main:app --reload` →
  `pnpm dev` → load `extension/dist` unpacked → open any article page → popup
  ready state → Check → stepper + 6 rows stream in ~7 s → done counts →
  re-check same URL streams instantly with `cached: true` → 21st check from the
  same install ID shows a clear 429 message in the popup's error state.
- Small conventional commits (roughly: `chore: scaffolding`, `feat: shared
  schema + codegen`, `feat: backend check/stream endpoints + mock pipeline`,
  `test: backend skeleton`, `feat: extension skeleton`, `docs: quickstart`).
