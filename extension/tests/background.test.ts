/**
 * Unit tests for the three background modules that are not the SSE parser:
 * `api.ts`, `installId.ts` and `jobStore.ts`.
 *
 * Two rules shape the whole file:
 *
 *  - **Never the network.** `fetch` is replaced with a stub for every test that
 *    touches `api.ts`; a test that reached the real backend would be a test
 *    that fails on a laptop with no backend running.
 *  - **Never a real `chrome`.** MV3 APIs do not exist in Node, so a minimal
 *    in-memory `chrome.storage` stub stands in. It is deliberately tiny — just
 *    enough surface for the code under test to be exercised honestly.
 *
 * Each test re-imports its module through `vi.resetModules()` + `await import`,
 * because both `installId.ts` and `jobStore.ts` hold module-level caches on
 * purpose (a shared in-flight promise, and a memo of the last state). Sharing
 * one module instance across tests would let one test's cache decide the next
 * test's result.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { JobState } from '../src/shared/messages'

/* -------------------------------------------------------------------------- */
/* Stubs                                                                       */
/* -------------------------------------------------------------------------- */

type Area = Record<string, unknown>

interface StorageStub {
  local: Area
  session: Area
}

/** Install a minimal `chrome.storage` over `globalThis`, and hand back its data. */
function stubChromeStorage(): StorageStub {
  const data: StorageStub = { local: {}, session: {} }

  const area = (name: keyof StorageStub) => ({
    get: async (key: string) =>
      key in data[name] ? { [key]: data[name][key] } : ({} as Record<string, unknown>),
    set: async (items: Record<string, unknown>) => {
      Object.assign(data[name], items)
    },
  })

  // The code under test only ever reaches for chrome.storage.
  vi.stubGlobal('chrome', { storage: { local: area('local'), session: area('session') } })
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
  vi.resetModules()
})

/* -------------------------------------------------------------------------- */
/* api.ts                                                                      */
/* -------------------------------------------------------------------------- */

describe('apiBase', () => {
  it('is an absolute http origin with no trailing slash', async () => {
    const { apiBase } = await import('../src/background/api')
    const base = apiBase()

    expect(base.length).toBeGreaterThan(0)
    expect(base.endsWith('/')).toBe(false)
    // Must parse: every request URL is built by concatenating onto it, and the
    // manifest's single host_permissions entry is derived from the same value.
    expect(() => new URL(base)).not.toThrow()
    expect(new URL(base).protocol).toMatch(/^https?:$/)
  })
})

describe('postCheck', () => {
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
    // This is the real body the backend sends today (routes/check.py raises an
    // HTTPException, and FastAPI wraps the detail).
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

describe('openStream', () => {
  /** The exact wire format the backend emits, keep-alive comment included. */
  const WIRE = [
    'id: 1\nevent: claims_found\ndata: {"type":"claims_found","count":2}\n\n',
    ': keep-alive\n\n',
    'id: 2\nevent: claim\ndata: {"id":"c3","verdict":"supported"}\n\n',
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

  it('passes the abort signal through to fetch', async () => {
    const calls = stubFetch(() => streamResponse(WIRE))
    const { openStream } = await import('../src/background/api')
    const controller = new AbortController()

    await openStream('job-1', () => undefined, controller.signal)

    expect(calls[0].init.signal).toBe(controller.signal)
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
  })

  it('degrades a non-object stored value to the initial state', async () => {
    storage.session.job_state = 'garbage'
    const { getState } = await import('../src/background/jobStore')
    const { INITIAL_JOB_STATE } = await import('../src/shared/messages')

    await expect(getState()).resolves.toEqual(INITIAL_JOB_STATE)
  })
})
