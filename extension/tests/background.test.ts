/**
 * Unit tests for the service worker and the three background modules that are
 * not the SSE parser: `index.ts`, `api.ts`, `installId.ts` and `jobStore.ts`.
 *
 * Two rules shape the whole file:
 *
 *  - **Never the network.** `fetch` is replaced with a stub for every test that
 *    touches `api.ts`; a test that reached the real backend would be a test
 *    that fails on a laptop with no backend running.
 *  - **Never a real `chrome`.** MV3 APIs do not exist in Node, so in-memory
 *    stubs stand in. They are deliberately small — just enough surface for the
 *    code under test to be exercised honestly.
 *
 * Each test re-imports its module through `vi.resetModules()` + `await import`,
 * because `installId.ts`, `jobStore.ts` and `index.ts` all hold module-level
 * state on purpose (a shared in-flight promise, a memo of the last state, the
 * live `JobState` and the `activeRun` lock). Sharing one module instance across
 * tests would let one test's state decide the next test's result.
 *
 * `VITE_API_BASE` has no value under `vitest` — there is no `extension/.env` in
 * the repo, and there must not be one — so every test that reaches `apiBase()`
 * stubs it. That absence is itself a behaviour worth pinning down, and
 * `describe('apiBase')` does.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { JobState } from '../src/shared/messages'

// The service worker imports the extractor bundle for its emitted path. Only
// CRXJS can resolve that specifier, and `pnpm test` runs without the plugin, so
// stand in the path a production build produces.
vi.mock('../src/content/extract?script&iife', () => ({ default: 'src/content/extract.js' }))

/* -------------------------------------------------------------------------- */
/* Stubs                                                                       */
/* -------------------------------------------------------------------------- */

/** Whatever `VITE_API_BASE` is set to in `extension/.env` for a real build. */
const TEST_API_BASE = 'http://localhost:8000'

type Area = Record<string, unknown>

interface StorageStub {
  local: Area
  session: Area
}

function storageAreas(data: StorageStub) {
  const area = (name: keyof StorageStub) => ({
    get: async (key: string) =>
      key in data[name] ? { [key]: data[name][key] } : ({} as Record<string, unknown>),
    set: async (items: Record<string, unknown>) => {
      Object.assign(data[name], items)
    },
  })
  return { local: area('local'), session: area('session') }
}

/** Install a minimal `chrome.storage` over `globalThis`, and hand back its data. */
function stubChromeStorage(): StorageStub {
  const data: StorageStub = { local: {}, session: {} }
  // The code under test only ever reaches for chrome.storage.
  vi.stubGlobal('chrome', { storage: storageAreas(data) })
  return data
}

/** A `Response`-alike good enough for `api.ts`, which only reads these members. */
function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  })
}

/** A response whose body streams `chunks`, so `openStream` sees real SSE bytes. */
function streamResponse(chunks: string[]): Response {
  const encoder = new TextEncoder()
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(encoder.encode(chunk))
      controller.close()
    },
  })
  return new Response(body, { status: 200, headers: { 'content-type': 'text/event-stream' } })
}

/**
 * A stream that never ends, optionally repeating `chunk` every `everyMs`.
 *
 * This is the shape of the bug the watchdogs exist for: a backend that keeps
 * the connection healthy with `: keep-alive` comments but never sends `done`.
 * Like a real `fetch`, it honours the abort signal — a stub that ignored it
 * would let a broken watchdog still look like it worked.
 */
function endlessResponse(init: RequestInit, chunk?: string, everyMs = 5): Response {
  const encoder = new TextEncoder()
  let timer: ReturnType<typeof setInterval> | undefined

  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      const stop = () => {
        if (timer !== undefined) clearInterval(timer)
        timer = undefined
      }
      if (chunk !== undefined) {
        timer = setInterval(() => {
          try {
            controller.enqueue(encoder.encode(chunk))
          } catch {
            stop() // the consumer went away first
          }
        }, everyMs)
      }
      init.signal?.addEventListener('abort', () => {
        stop()
        try {
          controller.error(new DOMException('The check was aborted.', 'AbortError'))
        } catch {
          // Already closed or errored — nothing to do.
        }
      })
    },
    cancel() {
      if (timer !== undefined) clearInterval(timer)
      timer = undefined
    },
  })

  return new Response(body, { status: 200, headers: { 'content-type': 'text/event-stream' } })
}

/** Record every `fetch` call and reply from `handler`. */
function stubFetch(handler: (url: string, init: RequestInit) => Response | Promise<Response>) {
  const calls: { url: string; init: RequestInit }[] = []
  vi.stubGlobal('fetch', async (input: RequestInfo | URL, init: RequestInit = {}) => {
    const url = typeof input === 'string' ? input : String(input)
    calls.push({ url, init })
    return handler(url, init)
  })
  return calls
}

const ARTICLE = {
  url: 'https://news.example.com/hawker-stall-rents-rise',
  title: 'Hawker stall rents rise',
  text: 'SINGAPORE — some article text.',
  install_id: '11111111-2222-3333-4444-555555555555',
}

afterEach(() => {
  vi.unstubAllGlobals()
  vi.unstubAllEnvs()
  // `vi.doMock` registrations outlive `vi.resetModules()` — the module registry
  // is cleared, the mock registry is not. Without this, the first test that
  // stubs `api.ts` silently hands its stub to every later test that imports the
  // service worker, and a test asserting real streaming behaviour quietly
  // asserts nothing. Unregister it explicitly.
  vi.doUnmock('../src/background/api')
  vi.resetModules()
  vi.restoreAllMocks()
})

/* -------------------------------------------------------------------------- */
/* api.ts — configuration                                                      */
/* -------------------------------------------------------------------------- */

describe('apiBase', () => {
  it('names the missing variable instead of inventing an origin', async () => {
    // No extension/.env: `import.meta.env.VITE_API_BASE` is genuinely
    // undefined, which is why vite-env.d.ts types it optional. Falling back to
    // a localhost guess would report a build mistake as "could not reach
    // Re-Vera", which sends the reader looking at their network.
    const { apiBase, ApiError } = await import('../src/background/api')

    const error = (() => {
      try {
        apiBase()
        return null
      } catch (e: unknown) {
        return e
      }
    })()

    expect(error).toBeInstanceOf(ApiError)
    expect((error as InstanceType<typeof ApiError>).code).toBe('missing_config')
    expect((error as Error).message).toContain('VITE_API_BASE')
    expect((error as Error).message).toContain('.env.example')
  })

  it('rejects a value that is not an absolute http(s) URL', async () => {
    // "localhost:8000" parses as a URL with the scheme "localhost:", which
    // would produce requests the manifest's host_permissions cannot cover.
    vi.stubEnv('VITE_API_BASE', 'localhost:8000')
    const { apiBase, ApiError } = await import('../src/background/api')

    expect(() => apiBase()).toThrow(ApiError)
    expect(() => apiBase()).toThrow(/VITE_API_BASE/)
  })

  it('is an absolute http origin with no trailing slash', async () => {
    vi.stubEnv('VITE_API_BASE', `${TEST_API_BASE}/`)
    const { apiBase } = await import('../src/background/api')
    const base = apiBase()

    expect(base).toBe(TEST_API_BASE)
    expect(base.endsWith('/')).toBe(false)
    // Must parse: every request URL is built by concatenating onto it, and the
    // manifest's single host_permissions entry is derived from the same value.
    expect(() => new URL(base)).not.toThrow()
    expect(new URL(base).protocol).toMatch(/^https?:$/)
  })
})

/* -------------------------------------------------------------------------- */
/* api.ts — requests                                                           */
/* -------------------------------------------------------------------------- */

describe('postCheck', () => {
  beforeEach(() => {
    vi.stubEnv('VITE_API_BASE', TEST_API_BASE)
  })

  it('POSTs the CheckRequest as JSON and returns the CheckJob', async () => {
    const calls = stubFetch(() => jsonResponse(200, { job_id: 'job-1', cached: false, claim_count: null }))
    const { postCheck, apiBase } = await import('../src/background/api')

    const job = await postCheck(ARTICLE)

    expect(job).toEqual({ job_id: 'job-1', cached: false, claim_count: null })
    expect(calls).toHaveLength(1)
    expect(calls[0].url).toBe(`${apiBase()}/check`)
    expect(calls[0].init.method).toBe('POST')
    expect(JSON.parse(String(calls[0].init.body))).toEqual(ARTICLE)
  })

  it('carries a cache hit through unchanged', async () => {
    stubFetch(() => jsonResponse(200, { job_id: 'job-2', cached: true, claim_count: 6 }))
    const { postCheck } = await import('../src/background/api')

    await expect(postCheck(ARTICLE)).resolves.toEqual({
      job_id: 'job-2',
      cached: true,
      claim_count: 6,
    })
  })

  it("unwraps FastAPI's nested detail so the 429 keeps the backend's own words", async () => {
    // This is the real body the backend sends today: routes/check.py raises an
    // HTTPException with detail={"code": "daily_limit", "message": ...} and
    // FastAPI wraps it under "detail". The sentence is
    // routes/check.py::daily_limit_message(20), verbatim.
    const message =
      "You have used all 20 of today's checks. The count resets at midnight Singapore time — please try again then."
    stubFetch(() => jsonResponse(429, { detail: { code: 'daily_limit', message } }))
    const { postCheck, ApiError } = await import('../src/background/api')

    const error = await postCheck(ARTICLE).catch((e: unknown) => e)

    expect(error).toBeInstanceOf(ApiError)
    expect((error as InstanceType<typeof ApiError>).code).toBe('daily_limit')
    expect((error as InstanceType<typeof ApiError>).status).toBe(429)
    // Rendered verbatim by the popup, so it must survive the trip untouched.
    expect((error as Error).message).toBe(message)
  })

  it('accepts a flat {code, message} body too, so the backend may drop the wrapper', async () => {
    stubFetch(() => jsonResponse(429, { code: 'daily_limit', message: 'No checks left today.' }))
    const { postCheck, ApiError } = await import('../src/background/api')

    const error = await postCheck(ARTICLE).catch((e: unknown) => e)

    expect(error).toBeInstanceOf(ApiError)
    expect((error as InstanceType<typeof ApiError>).code).toBe('daily_limit')
    expect((error as Error).message).toBe('No checks left today.')
  })

  it('still says "daily limit" when a 429 body carries no code at all', async () => {
    // A proxy or a future backend shape must not turn the daily cap into a
    // generic error: the popup keys its "Daily limit reached" panel — and its
    // decision not to offer a retry — off this code.
    stubFetch(() => new Response('Too Many Requests', { status: 429 }))
    const { postCheck, ApiError } = await import('../src/background/api')

    const error = await postCheck(ARTICLE).catch((e: unknown) => e)

    expect(error).toBeInstanceOf(ApiError)
    expect((error as InstanceType<typeof ApiError>).code).toBe('daily_limit')
    expect((error as Error).message).toMatch(/midnight/i)
  })

  it('falls back to a status-derived code when the error body is not JSON', async () => {
    stubFetch(() => new Response('<html>502</html>', { status: 502 }))
    const { postCheck, ApiError } = await import('../src/background/api')

    const error = await postCheck(ARTICLE).catch((e: unknown) => e)

    expect(error).toBeInstanceOf(ApiError)
    expect((error as InstanceType<typeof ApiError>).code).toBe('server_error')
    expect((error as Error).message.length).toBeGreaterThan(0)
  })

  it('turns a connection failure into a reader-facing network error', async () => {
    vi.stubGlobal('fetch', async () => {
      throw new TypeError('fetch failed')
    })
    const { postCheck, ApiError } = await import('../src/background/api')

    const error = await postCheck(ARTICLE).catch((e: unknown) => e)

    expect(error).toBeInstanceOf(ApiError)
    expect((error as InstanceType<typeof ApiError>).code).toBe('network')
  })

  it('rejects a 200 whose body is not a CheckJob', async () => {
    stubFetch(() => jsonResponse(200, { nope: true }))
    const { postCheck, ApiError } = await import('../src/background/api')

    const error = await postCheck(ARTICLE).catch((e: unknown) => e)

    expect(error).toBeInstanceOf(ApiError)
    expect((error as InstanceType<typeof ApiError>).code).toBe('bad_response')
  })
})

/* -------------------------------------------------------------------------- */
/* api.ts — the stream                                                         */
/* -------------------------------------------------------------------------- */

describe('openStream', () => {
  beforeEach(() => {
    vi.stubEnv('VITE_API_BASE', TEST_API_BASE)
  })

  /** The exact wire format the backend emits, keep-alive comment included. */
  const WIRE = [
    'id: 1\nevent: claims_found\ndata: {"type":"claims_found","count":2,"claim_ids":["c1","c2"]}\n\n',
    ': keep-alive\n\n',
    'id: 2\nevent: claim\ndata: {"id":"c1","verdict":"supported"}\n\n',
    'id: 3\nevent: done\ndata: {"type":"done","counts":{"supported":1,"contradicted":0,"missing_context":0,"unverifiable":1}}\n\n',
  ]

  it('requests the job stream and yields every message with its sequence id', async () => {
    const calls = stubFetch(() => streamResponse(WIRE))
    const { openStream, apiBase } = await import('../src/background/api')

    const seen: { id?: string; event: string }[] = []
    await openStream('job-1', (m) => seen.push({ id: m.id, event: m.event }))

    expect(calls[0].url).toBe(`${apiBase()}/check/job-1/stream`)
    expect(calls[0].init.method).toBe('GET')
    // A cached transcript of a live job is never right.
    expect(calls[0].init.cache).toBe('no-store')
    // The `: keep-alive` comment must not surface as a message.
    expect(seen).toEqual([
      { id: '1', event: 'claims_found' },
      { id: '2', event: 'claim' },
      { id: '3', event: 'done' },
    ])
  })

  it('percent-encodes the job id rather than splicing it into the path raw', async () => {
    const calls = stubFetch(() => streamResponse([]))
    const { openStream } = await import('../src/background/api')

    await openStream('a/b?c', () => undefined)

    expect(calls[0].url).toContain('a%2Fb%3Fc')
  })

  it('raises the backend error rather than opening a stream on a non-2xx', async () => {
    stubFetch(() => jsonResponse(404, { detail: { code: 'no_job', message: 'Unknown job.' } }))
    const { openStream, ApiError } = await import('../src/background/api')

    const error = await openStream('job-1', () => undefined).catch((e: unknown) => e)

    expect(error).toBeInstanceOf(ApiError)
    expect((error as InstanceType<typeof ApiError>).code).toBe('no_job')
  })

  it("aborts the fetch on the caller's signal, and reports it as an AbortError", async () => {
    // The caller tells its own abort apart from a watchdog's: one is the worker
    // superseding a run, the other is a failure the reader must be told about.
    const calls = stubFetch((_url, init) => endlessResponse(init))
    const { openStream } = await import('../src/background/api')
    const controller = new AbortController()

    const pending = openStream('job-1', () => undefined, controller.signal, {
      idleTimeoutMs: 5_000,
      totalTimeoutMs: 5_000,
    })
    controller.abort()
    const error = await pending.catch((e: unknown) => e)

    expect(error).toBeInstanceOf(DOMException)
    expect((error as DOMException).name).toBe('AbortError')
    // The fetch gets a signal of openStream's own, chained off the caller's.
    expect(calls[0].init.signal).toBeInstanceOf(AbortSignal)
    expect(calls[0].init.signal?.aborted).toBe(true)
  })

  it('gives up on a silent stream instead of hanging on it forever', async () => {
    stubFetch((_url, init) => endlessResponse(init))
    const { openStream, ApiError } = await import('../src/background/api')

    const error = await openStream('job-1', () => undefined, undefined, {
      idleTimeoutMs: 20,
      totalTimeoutMs: 5_000,
    }).catch((e: unknown) => e)

    expect(error).toBeInstanceOf(ApiError)
    expect((error as InstanceType<typeof ApiError>).code).toBe('stream_stalled')
    expect((error as Error).message).toMatch(/try again/i)
  })

  it('gives up on a stream that keep-alives forever and never says done', async () => {
    // The exact reported failure: the backend's `: keep-alive` comment every
    // 20 s keeps the connection alive, `parseSSE` correctly swallows it, and
    // nothing ever terminates the job. Before the total watchdog this promise
    // never settled, `activeRun` never cleared, and every later START_CHECK was
    // dropped while the popup sat in CheckingState — which renders no button.
    stubFetch((_url, init) => endlessResponse(init, ': keep-alive\n\n', 5))
    const { openStream, ApiError } = await import('../src/background/api')

    const seen: string[] = []
    const error = await openStream('job-1', (m) => seen.push(m.event), undefined, {
      // Idle is generously longer than the comment interval, so only the total
      // budget can end this: the comments really are keeping it alive.
      idleTimeoutMs: 200,
      totalTimeoutMs: 60,
    }).catch((e: unknown) => e)

    expect(error).toBeInstanceOf(ApiError)
    expect((error as InstanceType<typeof ApiError>).code).toBe('stream_timeout')
    // Comments are not events; the job produced nothing the reader could use.
    expect(seen).toEqual([])
  })

  it('counts a keep-alive comment as a sign of life for the idle watchdog', async () => {
    // The complement of the test above, and the reason the idle watchdog counts
    // bytes rather than framed messages: a healthy but slow check sends only
    // comments between claims, and must not be killed for it.
    stubFetch((_url, init) => endlessResponse(init, ': keep-alive\n\n', 5))
    const { openStream, ApiError } = await import('../src/background/api')

    const error = await openStream('job-1', () => undefined, undefined, {
      idleTimeoutMs: 40, // < the 60 ms total, so an unfed idle timer wins the race
      totalTimeoutMs: 60,
    }).catch((e: unknown) => e)

    expect(error).toBeInstanceOf(ApiError)
    expect((error as InstanceType<typeof ApiError>).code).toBe('stream_timeout')
  })

  it('lets a stream that finishes in time through untouched', async () => {
    stubFetch(() => streamResponse(WIRE))
    const { openStream } = await import('../src/background/api')

    const seen: string[] = []
    await expect(
      openStream('job-1', (m) => seen.push(m.event), undefined, {
        idleTimeoutMs: 5_000,
        totalTimeoutMs: 5_000,
      }),
    ).resolves.toBeUndefined()
    expect(seen).toEqual(['claims_found', 'claim', 'done'])
  })

  it('ships default budgets that outlast the backend, not the other way round', async () => {
    const { STREAM_IDLE_TIMEOUT_MS, STREAM_TOTAL_TIMEOUT_MS } = await import(
      '../src/background/api'
    )

    // The backend keep-alives every 20 s (docs/decisions.md §13): the idle
    // budget must survive at least two missed ones, or a slow check dies.
    expect(STREAM_IDLE_TIMEOUT_MS).toBeGreaterThan(2 * 20_000)
    expect(STREAM_IDLE_TIMEOUT_MS).toBeLessThan(STREAM_TOTAL_TIMEOUT_MS)
    // Looser than the backend's own stream deadline (a 120 s floor, ~136 s for
    // the configured job shape), so a job that dies server-side is reported by
    // its `error: timeout` event — which has the better sentence — and this
    // fires only when even that never arrives.
    expect(STREAM_TOTAL_TIMEOUT_MS).toBeGreaterThan(140_000)
  })
})

/* -------------------------------------------------------------------------- */
/* index.ts — the service worker                                               */
/* -------------------------------------------------------------------------- */

type MessageListener = (
  message: unknown,
  sender: unknown,
  sendResponse: (response: unknown) => void,
) => boolean | undefined

interface WorkerHarness {
  storage: StorageStub
  /** URL reported by `chrome.tabs.query` — reassign it to "switch tab". */
  tabUrl: string
  /** Every state the worker broadcast, in order. */
  broadcasts: JobState[]
  send: (message: unknown) => Promise<JobState>
}

/**
 * Stand up the whole `chrome` surface `index.ts` touches and hand back a way to
 * drive its message listener.
 */
function stubWorkerChrome(seed?: Partial<JobState>): WorkerHarness {
  const data: StorageStub = { local: {}, session: {} }
  if (seed) data.session.job_state = seed

  const harness: WorkerHarness = {
    storage: data,
    tabUrl: ARTICLE.url,
    broadcasts: [],
    send: async () => {
      throw new Error('worker not imported yet')
    },
  }

  let listener: MessageListener | null = null

  vi.stubGlobal('chrome', {
    storage: storageAreas(data),
    runtime: {
      onMessage: {
        addListener: (fn: MessageListener) => {
          listener = fn
        },
        removeListener: () => undefined,
      },
      sendMessage: async (message: unknown) => {
        const state = (message as { state?: JobState }).state
        if (state) harness.broadcasts.push(state)
        // No popup open is the normal case, and Chrome rejects for it.
        throw new Error('Receiving end does not exist.')
      },
    },
    tabs: {
      query: async () => [{ id: 7, url: harness.tabUrl, title: ARTICLE.title }],
      sendMessage: async () => ({
        url: ARTICLE.url,
        title: ARTICLE.title,
        text: ARTICLE.text,
      }),
    },
    scripting: { executeScript: async () => [] },
    action: {
      setBadgeText: async () => undefined,
      setBadgeBackgroundColor: async () => undefined,
      setBadgeTextColor: async () => undefined,
    },
  })

  harness.send = (message: unknown) =>
    new Promise<JobState>((resolve, reject) => {
      if (!listener) {
        reject(new Error('index.ts registered no chrome.runtime.onMessage listener'))
        return
      }
      listener(message, {}, (response: unknown) => {
        resolve((response as { state: JobState }).state)
      })
    })

  return harness
}

/** Let every already-queued microtask and timer callback run. */
async function settle(rounds = 6): Promise<void> {
  for (let i = 0; i < rounds; i += 1) await new Promise((resolve) => setTimeout(resolve, 0))
}

const DONE_STATE: Partial<JobState> = {
  status: 'done',
  url: ARTICLE.url,
  title: ARTICLE.title,
  claimCount: 1,
  claims: [],
  counts: { supported: 1, contradicted: 0, missing_context: 0, unverifiable: 0 },
  cached: false,
  error: null,
}

describe('service worker: the activeRun lock', () => {
  beforeEach(() => {
    vi.stubEnv('VITE_API_BASE', TEST_API_BASE)
  })

  it('releases the lock when the stream fails, so the reader can retry', async () => {
    // The lock is what makes a hung stream a lockout rather than one bad check:
    // `startCheck` drops every START_CHECK while `activeRun` is set, and the
    // popup renders no button in its checking state. Whatever `openStream`
    // does — and it now always settles — the lock must come back.
    const harness = stubWorkerChrome()
    const actual = await vi.importActual<typeof import('../src/background/api')>(
      '../src/background/api',
    )
    const postCheck = vi.fn(async () => ({ job_id: 'job-1', cached: false, claim_count: null }))
    const openStream = vi.fn(async () => {
      await settle(1)
      throw new actual.ApiError('stream_timeout', 'This check took too long to finish.')
    })
    vi.doMock('../src/background/api', () => ({ ...actual, postCheck, openStream }))

    await import('../src/background/index')
    await settle()

    const first = await harness.send({ type: 'START_CHECK' })
    expect(first.status).toBe('extracting')
    await settle()

    const failed = harness.broadcasts.at(-1)
    expect(failed?.status).toBe('error')
    expect(failed?.error?.code).toBe('stream_timeout')

    // The retry the popup's error state offers must actually start a check.
    const second = await harness.send({ type: 'START_CHECK' })
    expect(second.status).toBe('extracting')
    await settle()
    expect(postCheck).toHaveBeenCalledTimes(2)
    expect(openStream).toHaveBeenCalledTimes(2)
  })
})

describe('service worker: GET_STATE and the article on screen', () => {
  beforeEach(() => {
    vi.stubEnv('VITE_API_BASE', TEST_API_BASE)
  })

  it('returns to idle when the popup opens over a different article', async () => {
    // `JobState` lives in chrome.storage.session so the popup can close and
    // reopen mid-check. Nothing else ever put it back to idle, so opening the
    // popup on the next article showed the previous article's verdicts as if
    // they were about this one.
    const harness = stubWorkerChrome(DONE_STATE)
    harness.tabUrl = 'https://news.example.com/a-completely-different-story'

    await import('../src/background/index')
    const state = await harness.send({ type: 'GET_STATE' })

    expect(state.status).toBe('idle')
    expect(state.url).toBeNull()
    expect(state.claims).toEqual([])
    expect(state.counts).toBeNull()
    // Persisted too: the next cold worker must not resurrect it.
    expect((harness.storage.session.job_state as JobState).status).toBe('idle')
  })

  it('keeps the result when the popup reopens over the same article', async () => {
    const harness = stubWorkerChrome(DONE_STATE)

    await import('../src/background/index')
    const state = await harness.send({ type: 'GET_STATE' })

    expect(state.status).toBe('done')
    expect(state.counts).toEqual(DONE_STATE.counts)
  })

  it('treats a fragment change as the same article', async () => {
    // The stored URL comes from the content script's document.URL and the
    // compared one from chrome.tabs; jumping to #comments is not a new page.
    const harness = stubWorkerChrome(DONE_STATE)
    harness.tabUrl = `${ARTICLE.url}#comments`

    await import('../src/background/index')

    await expect(harness.send({ type: 'GET_STATE' })).resolves.toMatchObject({ status: 'done' })
  })

  it('does not discard a check that is still running', async () => {
    const harness = stubWorkerChrome()
    const actual = await vi.importActual<typeof import('../src/background/api')>(
      '../src/background/api',
    )
    vi.doMock('../src/background/api', () => ({
      ...actual,
      postCheck: async () => ({ job_id: 'job-1', cached: false, claim_count: null }),
      // A live check: this settles only when the caller aborts it.
      openStream: (_id: string, _on: unknown, signal?: AbortSignal) =>
        new Promise<void>((_resolve, reject) => {
          signal?.addEventListener('abort', () => reject(new Error('aborted')), { once: true })
        }),
    }))

    await import('../src/background/index')
    await settle()
    await harness.send({ type: 'START_CHECK' })
    await settle()

    // The reader flicks to another tab mid-check and opens the popup there.
    harness.tabUrl = 'https://news.example.com/some-other-story'
    const state = await harness.send({ type: 'GET_STATE' })

    expect(state.status).toBe('checking')
    expect(state.url).toBe(ARTICLE.url)
  })

  it('leaves the state alone when the active tab cannot be read', async () => {
    // No activeTab grant, or a closed window. Guessing would be worse than
    // being stale: it would silently bin a result the reader asked for.
    const harness = stubWorkerChrome(DONE_STATE)
    await import('../src/background/index')
    const chromeStub = globalThis.chrome as unknown as { tabs: { query: () => Promise<unknown> } }
    chromeStub.tabs.query = async () => {
      throw new Error('no access')
    }

    await expect(harness.send({ type: 'GET_STATE' })).resolves.toMatchObject({ status: 'done' })
  })
})

describe('service worker: claims_found', () => {
  beforeEach(() => {
    vi.stubEnv('VITE_API_BASE', TEST_API_BASE)
  })

  it('takes the claim count from claim_ids, which is what the rows are keyed by', async () => {
    // count and claim_ids.length are the same number by contract
    // (docs/decisions.md §15). If a producer ever lets them disagree, the popup
    // must allocate as many rows as there are ids — not one it can never fill.
    const harness = stubWorkerChrome()
    const actual = await vi.importActual<typeof import('../src/background/api')>(
      '../src/background/api',
    )
    vi.doMock('../src/background/api', () => ({
      ...actual,
      postCheck: async () => ({ job_id: 'job-1', cached: false, claim_count: null }),
      openStream: async (_id: string, onMessage: (m: { event: string; data: string }) => void) => {
        onMessage({
          event: 'claims_found',
          data: JSON.stringify({ type: 'claims_found', count: 9, claim_ids: ['c1', 'c2', 'c3'] }),
        })
        onMessage({
          event: 'done',
          data: JSON.stringify({
            type: 'done',
            counts: { supported: 3, contradicted: 0, missing_context: 0, unverifiable: 0 },
          }),
        })
      },
    }))

    await import('../src/background/index')
    await settle()
    await harness.send({ type: 'START_CHECK' })
    await settle()

    const last = harness.broadcasts.at(-1)
    expect(last?.status).toBe('done')
    expect(last?.claimCount).toBe(3)
  })

  it('carries claim_ids onto JobState in the article order the backend sent', async () => {
    // Regression: the worker parsed `claim_ids`, used it only to derive
    // `claimCount`, and dropped the list — so the popup never saw it and fell
    // back to ordering rows by `claim.start`. That made a live run (claims
    // resolving 3, 1, 6, 4, 2, 5) and a cache replay (1..6) render the same
    // article two different ways, which is the whole point of decision 15.
    const harness = stubWorkerChrome()
    const actual = await vi.importActual<typeof import('../src/background/api')>(
      '../src/background/api',
    )
    const ids = ['c1', 'c2', 'c3', 'c4', 'c5', 'c6']
    vi.doMock('../src/background/api', () => ({
      ...actual,
      postCheck: async () => ({ job_id: 'job-1', cached: false, claim_count: null }),
      openStream: async (_id: string, onMessage: (m: { event: string; data: string }) => void) => {
        onMessage({
          event: 'claims_found',
          data: JSON.stringify({ type: 'claims_found', count: ids.length, claim_ids: ids }),
        })
      },
    }))

    await import('../src/background/index')
    await settle()
    await harness.send({ type: 'START_CHECK' })
    await settle()

    const announced = harness.broadcasts.find((state) => state.claimIds !== null)
    expect(announced?.claimIds).toEqual(ids)
    expect(announced?.claimCount).toBe(ids.length)
    // Persisted with the rest of the state, so a popup opening after a worker
    // restart still gets its row order.
    expect((harness.storage.session.job_state as JobState).claimIds).toEqual(ids)
  })

  it('leaves claimIds null for a backend that still sends only {type, count}', async () => {
    // The popup's documented fallback: order rows by `claim.start`. It must
    // stay reachable, so an older backend degrades rather than breaking.
    const harness = stubWorkerChrome()
    const actual = await vi.importActual<typeof import('../src/background/api')>(
      '../src/background/api',
    )
    vi.doMock('../src/background/api', () => ({
      ...actual,
      postCheck: async () => ({ job_id: 'job-1', cached: false, claim_count: null }),
      openStream: async (_id: string, onMessage: (m: { event: string; data: string }) => void) => {
        onMessage({
          event: 'claims_found',
          data: JSON.stringify({ type: 'claims_found', count: 6 }),
        })
      },
    }))

    await import('../src/background/index')
    await settle()
    await harness.send({ type: 'START_CHECK' })
    await settle()

    const last = harness.broadcasts.at(-1)
    expect(last?.claimCount).toBe(6)
    expect(last?.claimIds).toBeNull()
  })

  it('applies an error event that carries no sequence id', async () => {
    // The backend's stream-deadline `error: timeout` is generated by the relay
    // rather than replayed from the job's event list, so it deliberately ships
    // with no `id:` line. `applyEvent` drops already-applied events by that id;
    // it must not drop — or mis-sequence — a message that never had one.
    const harness = stubWorkerChrome()
    const actual = await vi.importActual<typeof import('../src/background/api')>(
      '../src/background/api',
    )
    const timeout =
      'This check stopped responding before it finished. Please try checking the article again.'
    vi.doMock('../src/background/api', () => ({
      ...actual,
      postCheck: async () => ({ job_id: 'job-1', cached: false, claim_count: null }),
      openStream: async (
        _id: string,
        onMessage: (m: { id?: string; event: string; data: string }) => void,
      ) => {
        onMessage({
          id: '1',
          event: 'claims_found',
          data: JSON.stringify({ type: 'claims_found', count: 1, claim_ids: ['c1'] }),
        })
        onMessage({
          event: 'error',
          data: JSON.stringify({ type: 'error', code: 'timeout', message: timeout }),
        })
      },
    }))

    await import('../src/background/index')
    await settle()
    await harness.send({ type: 'START_CHECK' })
    await settle()

    const last = harness.broadcasts.at(-1)
    expect(last?.status).toBe('error')
    expect(last?.error?.code).toBe('timeout')
    expect(last?.error?.message).toBe(timeout)
  })

  it('ends a live run and a cached replay on the same claimIds in the same order', async () => {
    // The end-to-end half of decision 15, driven through the real `openStream`
    // and the real SSE parser on the exact bytes the backend writes. The live
    // path resolves claims in RESOLVE_ORDER (c3, c1, c6, c4, c2, c5) and the
    // cache path in article order; the row order the popup keys off must be
    // identical either way, or the same article renders two different ways.
    const ARTICLE_ORDER = ['c1', 'c2', 'c3', 'c4', 'c5', 'c6']
    const RESOLVE_ORDER = ['c3', 'c1', 'c6', 'c4', 'c2', 'c5']
    const counts = { supported: 2, contradicted: 2, missing_context: 1, unverifiable: 1 }

    /** The backend's wire bytes for one whole job. */
    const transcript = (resolveOrder: string[]): string[] => {
      const found = JSON.stringify({
        type: 'claims_found',
        count: ARTICLE_ORDER.length,
        claim_ids: ARTICLE_ORDER,
      })
      const chunks = [`id: 1\nevent: claims_found\ndata: ${found}\n\n`]
      resolveOrder.forEach((id, i) => {
        const claim = JSON.stringify({ id, start: ARTICLE_ORDER.indexOf(id) * 100 })
        chunks.push(`id: ${i + 2}\nevent: claim\ndata: ${claim}\n\n`)
      })
      chunks.push(
        `id: ${resolveOrder.length + 2}\nevent: done\ndata: ${JSON.stringify({ type: 'done', counts })}\n\n`,
      )
      return chunks
    }

    /** Run one whole check through the real worker and hand back its last state. */
    const run = async (resolveOrder: string[], cached: boolean): Promise<JobState> => {
      vi.resetModules()
      const harness = stubWorkerChrome()
      stubFetch((url) =>
        url.endsWith('/stream')
          ? streamResponse(transcript(resolveOrder))
          : jsonResponse(200, { job_id: 'job-1', cached, claim_count: null }),
      )

      await import('../src/background/index')
      await settle()
      await harness.send({ type: 'START_CHECK' })
      await settle()

      const last = harness.broadcasts.at(-1)
      if (last === undefined) throw new Error('the worker broadcast nothing')
      return last
    }

    const live = await run(RESOLVE_ORDER, false)
    const replay = await run(ARTICLE_ORDER, true)

    expect(live.status).toBe('done')
    expect(replay.status).toBe('done')
    expect(live.claimIds).toEqual(ARTICLE_ORDER)
    // The identity that makes both paths render one article one way.
    expect(replay.claimIds).toEqual(live.claimIds)
    expect(live.claimCount).toBe(ARTICLE_ORDER.length)

    // Claims themselves still arrive in resolve order — the worker must not
    // sort them, because the popup keys rows by claimIds, not by arrival.
    expect(live.claims.map((c) => c.id)).toEqual(RESOLVE_ORDER)
    expect(replay.claims.map((c) => c.id)).toEqual(ARTICLE_ORDER)
  })
})

/* -------------------------------------------------------------------------- */
/* installId.ts                                                                */
/* -------------------------------------------------------------------------- */

describe('getInstallId', () => {
  let storage: StorageStub

  beforeEach(() => {
    storage = stubChromeStorage()
  })

  it('mints a UUID on first use and persists it in chrome.storage.local', async () => {
    const { getInstallId } = await import('../src/background/installId')

    const id = await getInstallId()

    expect(id).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i)
    expect(storage.local.install_id).toBe(id)
  })

  it('reuses the stored id — changing it would reset everyone’s daily cap', async () => {
    storage.local.install_id = 'already-here'
    const { getInstallId } = await import('../src/background/installId')

    await expect(getInstallId()).resolves.toBe('already-here')
    await expect(getInstallId()).resolves.toBe('already-here')
  })

  it('gives concurrent callers one id, not one each', async () => {
    // Read-then-write is not atomic across awaits: without the shared in-flight
    // promise the second caller would overwrite the first, and a check already
    // counted against one id would continue under another.
    const { getInstallId } = await import('../src/background/installId')

    const ids = await Promise.all([getInstallId(), getInstallId(), getInstallId()])

    expect(new Set(ids).size).toBe(1)
    expect(storage.local.install_id).toBe(ids[0])
  })
})

/* -------------------------------------------------------------------------- */
/* jobStore.ts                                                                 */
/* -------------------------------------------------------------------------- */

describe('jobStore', () => {
  let storage: StorageStub

  beforeEach(() => {
    storage = stubChromeStorage()
  })

  it('starts from INITIAL_JOB_STATE when storage is empty', async () => {
    const { getState } = await import('../src/background/jobStore')
    const { INITIAL_JOB_STATE } = await import('../src/shared/messages')

    await expect(getState()).resolves.toEqual(INITIAL_JOB_STATE)
  })

  it('round-trips a state through chrome.storage.session', async () => {
    const { getState, setState } = await import('../src/background/jobStore')
    const { INITIAL_JOB_STATE } = await import('../src/shared/messages')

    const next: JobState = { ...INITIAL_JOB_STATE, status: 'checking', claimCount: 6 }
    await setState(next)

    expect(storage.session.job_state).toEqual(next)
    await expect(getState()).resolves.toEqual(next)
  })

  it('merges a patch without dropping the rest of the state', async () => {
    const { getState, patchState, setState } = await import('../src/background/jobStore')
    const { INITIAL_JOB_STATE } = await import('../src/shared/messages')

    await setState({ ...INITIAL_JOB_STATE, status: 'checking', title: 'Rents rise' })
    const merged = await patchState({ status: 'done' })

    expect(merged.status).toBe('done')
    expect(merged.title).toBe('Rents rise')
    await expect(getState()).resolves.toEqual(merged)
  })

  it('serialises overlapping patches so none is lost', async () => {
    // Claim events arrive faster than a storage round-trip. Two unqueued
    // read-modify-writes would each read the same state and the later write
    // would silently drop the earlier claim.
    const { getState, patchState } = await import('../src/background/jobStore')

    await Promise.all([
      patchState({ claimCount: 6 }),
      patchState({ status: 'checking' }),
      patchState({ cached: true }),
    ])

    const state = await getState()
    expect(state.claimCount).toBe(6)
    expect(state.status).toBe('checking')
    expect(state.cached).toBe(true)
  })

  it('revives a half-written or older stored state instead of crashing', async () => {
    // Storage outlives code: a state written by a previous build must degrade,
    // not take the worker down on the popup's first GET_STATE.
    storage.session.job_state = { status: 'done', claims: 'not-an-array' }
    const { getState } = await import('../src/background/jobStore')

    const state = await getState()

    expect(state.status).toBe('done')
    expect(state.claims).toEqual([])
    expect(state.counts).toBeNull()
    expect(state.error).toBeNull()
    // Written before decision 15, so it has no claimIds at all.
    expect(state.claimIds).toBeNull()
  })

  it('revives claimIds, and refuses a stored value that is not a list', async () => {
    // The popup allocates one row per entry, so a non-array here would have it
    // building rows out of garbage rather than falling back to `claim.start`.
    const { getState, setState } = await import('../src/background/jobStore')
    const { INITIAL_JOB_STATE } = await import('../src/shared/messages')

    await setState({ ...INITIAL_JOB_STATE, status: 'checking', claimIds: ['c1', 'c2'] })
    await expect(getState()).resolves.toMatchObject({ claimIds: ['c1', 'c2'] })

    storage.session.job_state = { status: 'checking', claimIds: 'c1,c2' }
    vi.resetModules()
    const fresh = await import('../src/background/jobStore')

    await expect(fresh.getState()).resolves.toMatchObject({ claimIds: null })
  })

  it('degrades a non-object stored value to the initial state', async () => {
    storage.session.job_state = 'garbage'
    const { getState } = await import('../src/background/jobStore')
    const { INITIAL_JOB_STATE } = await import('../src/shared/messages')

    await expect(getState()).resolves.toEqual(INITIAL_JOB_STATE)
  })
})
