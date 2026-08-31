/**
 * The Re-Vera backend client. Nothing else in the extension calls the network.
 *
 * Two calls, in order:
 *
 *   POST /check                  -> CheckJob   (cheap; enforces the daily cap)
 *   GET  /check/{job_id}/stream  -> SSE        (claims_found, claim…, done|error)
 *
 * Failures arrive as `ApiError`, which carries the backend's own `code` and
 * reader-facing `message` so the popup can show the daily-limit sentence
 * verbatim instead of a generic "something went wrong".
 */

import type { CheckJob, CheckRequest } from '../types/schema'
import { parseSSE, type SSEMessage } from './sse'

const NETWORK_MESSAGE =
  'Could not reach Re-Vera. Check that the backend is running, then try again.'
const SERVER_MESSAGE = 'Re-Vera had a problem checking this article. Please try again.'

/**
 * Shown when the build had no backend origin baked into it.
 *
 * Names the variable and the file to copy, because the only person who can ever
 * see this sentence is whoever built the extension.
 */
const CONFIG_MESSAGE =
  'Re-Vera was built without a backend address. Copy extension/.env.example to ' +
  'extension/.env, set VITE_API_BASE (e.g. http://localhost:8000), then rebuild and reload the extension.'

/** Silence for this long on an open stream and the watchdog gives up on it. */
export const STREAM_IDLE_TIMEOUT_MS = 45_000

/** Hard ceiling on one stream, however chatty it is. */
export const STREAM_TOTAL_TIMEOUT_MS = 180_000

const STALLED_MESSAGE =
  'Re-Vera stopped hearing back from the server part-way through this check. Please try again.'
const TIMEOUT_MESSAGE = 'This check took too long to finish. Please try again.'

/** A failed backend call, carrying the backend's machine code and its sentence. */
export class ApiError extends Error {
  /** Machine-readable code, e.g. `"daily_limit"`, `"network"`, `"http_error"`. */
  readonly code: string
  /** HTTP status, when the failure came back as a response. */
  readonly status?: number

  constructor(code: string, message: string, status?: number) {
    super(message)
    this.name = 'ApiError'
    this.code = code
    this.status = status
  }
}

/**
 * Backend origin, without a trailing slash.
 *
 * Vite inlines `import.meta.env.VITE_API_BASE` at build time; the same variable
 * drives the manifest's single `host_permissions` entry, so the two can never
 * drift apart. Change it and you must rebuild and reload the extension.
 *
 * `VITE_API_BASE` is genuinely optional — a build with no `extension/.env` has
 * none — so this throws rather than quietly inventing an origin. A silent
 * fallback would send every check at a backend the reader never started and
 * report it as "could not reach Re-Vera", which points at the wrong problem;
 * an absolute URL that `new URL()` refuses would produce a request to a path
 * like `undefined/check` that `host_permissions` cannot cover. Both are build
 * mistakes, and both say so here, by name.
 */
export function apiBase(): string {
  const configured = import.meta.env.VITE_API_BASE
  if (typeof configured !== 'string' || configured.trim().length === 0) {
    throw new ApiError('missing_config', CONFIG_MESSAGE)
  }

  const base = configured.trim().replace(/\/+$/, '')
  let parsed: URL
  try {
    parsed = new URL(base)
  } catch {
    throw new ApiError('bad_config', `${CONFIG_MESSAGE} (VITE_API_BASE="${configured}" is not a URL.)`)
  }
  if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') {
    throw new ApiError(
      'bad_config',
      `${CONFIG_MESSAGE} (VITE_API_BASE="${configured}" is not an http(s) URL.)`,
    )
  }
  return base
}

/** Start a check. Returns the job to stream from; throws `ApiError` otherwise. */
export async function postCheck(req: CheckRequest): Promise<CheckJob> {
  const response = await request(`${apiBase()}/check`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(req),
  })

  if (!response.ok) throw await responseError(response)

  const job = (await response.json()) as CheckJob
  if (typeof job?.job_id !== 'string') {
    throw new ApiError('bad_response', SERVER_MESSAGE, response.status)
  }
  return job
}

/** Watchdog budgets for one `openStream` call. Both are overridable for tests. */
export interface StreamOptions {
  /** Give up after this much silence. Any byte from the server resets it. */
  idleTimeoutMs?: number
  /** Give up after this long overall, however chatty the server is. */
  totalTimeoutMs?: number
}

/**
 * Open the job's SSE stream and hand every framed message to `onMessage`.
 *
 * Resolves when the backend closes the stream, which it does after `done` or
 * `error`. Aborting `signal` rejects with an `AbortError` — the caller decides
 * whether that was its own doing.
 *
 * **This call is guaranteed to settle.** That guarantee is the whole point of
 * the two watchdogs, because the caller keeps `activeRun` set for as long as
 * this is pending and drops every later START_CHECK on the floor: a stream that
 * hung here would lock the reader out of the extension until Chrome recycled
 * the service worker.
 *
 *  - **Idle** (`idleTimeoutMs`): reset by *every byte* the server sends, not
 *    only by framed events. The backend's `: keep-alive` comment never surfaces
 *    as a message (`parseSSE` swallows comments, correctly), so counting only
 *    events would fire this watchdog on a perfectly healthy but slow check. The
 *    budget is comfortably more than two missed 20 s keep-alives.
 *  - **Total** (`totalTimeoutMs`): the one that catches the actual failure —
 *    a server that keep-alives forever and never sends `done` or `error`. It is
 *    deliberately looser than the backend's own stream deadline, so a job that
 *    dies server-side is reported by the backend's `error: timeout` event (with
 *    its better sentence) and this only fires when even that fails to arrive.
 *
 * Either firing aborts the fetch and throws an `ApiError` — never an
 * `AbortError`, which the caller reserves for aborts it asked for itself.
 */
export async function openStream(
  jobId: string,
  onMessage: (m: SSEMessage) => void,
  signal?: AbortSignal,
  options: StreamOptions = {},
): Promise<void> {
  const idleMs = options.idleTimeoutMs ?? STREAM_IDLE_TIMEOUT_MS
  const totalMs = options.totalTimeoutMs ?? STREAM_TOTAL_TIMEOUT_MS

  const url = `${apiBase()}/check/${encodeURIComponent(jobId)}/stream`

  // Our own controller, so the watchdogs can abort the fetch without the caller
  // losing the ability to abort it too.
  const controller = new AbortController()
  let expired: 'idle' | 'total' | null = null
  let idleTimer: ReturnType<typeof setTimeout> | undefined
  let totalTimer: ReturnType<typeof setTimeout> | undefined

  function expire(reason: 'idle' | 'total'): void {
    if (expired !== null || controller.signal.aborted) return
    expired = reason
    controller.abort()
  }

  /** Restart the idle countdown. Called on every chunk of body bytes. */
  function poke(): void {
    if (expired !== null || controller.signal.aborted) return
    clearTimeout(idleTimer)
    if (idleMs > 0 && Number.isFinite(idleMs)) {
      idleTimer = setTimeout(() => expire('idle'), idleMs)
    }
  }

  const onCallerAbort = (): void => controller.abort()
  if (signal?.aborted) controller.abort()
  else signal?.addEventListener('abort', onCallerAbort, { once: true })

  // Rejects the moment the fetch is aborted, so this function returns even if
  // the body stream declines to unwind. A watchdog that could itself hang would
  // be no watchdog at all.
  const aborted = new Promise<never>((_, reject) => {
    if (controller.signal.aborted) {
      reject(abortReason(expired))
      return
    }
    controller.signal.addEventListener('abort', () => reject(abortReason(expired)), { once: true })
  })
  aborted.catch(() => undefined) // the read loop usually wins; never leave this unhandled

  const consume = (async () => {
    const response = await request(url, {
      method: 'GET',
      headers: { accept: 'text/event-stream' },
      // The stream is a live job's transcript; a cached copy is never right.
      cache: 'no-store',
      signal: controller.signal,
    })

    if (!response.ok) throw await responseError(response)
    if (!response.body) {
      throw new ApiError('no_stream', SERVER_MESSAGE, response.status)
    }

    for await (const message of parseSSE(watched(response.body, poke))) {
      poke()
      onMessage(message)
    }
  })()
  consume.catch(() => undefined) // ditto, for when the watchdog wins the race

  try {
    if (totalMs > 0 && Number.isFinite(totalMs)) {
      totalTimer = setTimeout(() => expire('total'), totalMs)
    }
    poke()
    await Promise.race([consume, aborted])
  } catch (error) {
    if (expired !== null) throw watchdogError(expired)
    throw error
  } finally {
    clearTimeout(idleTimer)
    clearTimeout(totalTimer)
    signal?.removeEventListener('abort', onCallerAbort)
    // A watchdog that fired mid-read leaves the body open; cancelling releases
    // the connection instead of leaving it to the worker's eventual death.
    controller.abort()
  }
}

/**
 * The same bytes, with `onChunk` called as each one passes.
 *
 * Wrapping the body rather than counting framed messages is what lets a
 * keep-alive comment count as a sign of life without `sse.ts` having to
 * surface comments it is right to hide.
 */
function watched(
  body: ReadableStream<Uint8Array>,
  onChunk: () => void,
): ReadableStream<Uint8Array> {
  return body.pipeThrough(
    new TransformStream<Uint8Array, Uint8Array>({
      transform(chunk, controller) {
        onChunk()
        controller.enqueue(chunk)
      },
    }),
  )
}

/** What the abort race rejects with: our own error, or a plain `AbortError`. */
function abortReason(expired: 'idle' | 'total' | null): unknown {
  return expired === null
    ? new DOMException('The check was aborted.', 'AbortError')
    : watchdogError(expired)
}

function watchdogError(expired: 'idle' | 'total'): ApiError {
  return expired === 'idle'
    ? new ApiError('stream_stalled', STALLED_MESSAGE)
    : new ApiError('stream_timeout', TIMEOUT_MESSAGE)
}

/** `fetch`, with a connection failure turned into an `ApiError`. */
async function request(url: string, init: RequestInit): Promise<Response> {
  try {
    return await fetch(url, init)
  } catch (error) {
    // An aborted fetch is the caller's own signal firing, not a network fault;
    // let it through untouched so the caller can tell the two apart.
    if (error instanceof DOMException && error.name === 'AbortError') throw error
    throw new ApiError('network', NETWORK_MESSAGE)
  }
}

/**
 * Turn a non-2xx response into an `ApiError`.
 *
 * FastAPI wraps a raised `HTTPException` detail, so the daily-limit 429 arrives
 * as `{"detail": {"code": "daily_limit", "message": "…"}}`. A bare
 * `{"code", "message"}` body is accepted too, so the backend can drop the
 * wrapper later without breaking the extension.
 */
async function responseError(response: Response): Promise<ApiError> {
  const body = await readJson(response)
  const detail = isRecord(body) && isRecord(body.detail) ? body.detail : body
  const code = isRecord(detail) && typeof detail.code === 'string' ? detail.code : null
  const message = isRecord(detail) && typeof detail.message === 'string' ? detail.message : null

  return new ApiError(
    code ?? defaultCode(response.status),
    message ?? defaultMessage(response.status),
    response.status,
  )
}

async function readJson(response: Response): Promise<unknown> {
  try {
    return await response.json()
  } catch {
    // An HTML error page from a proxy, or an empty body: fall back to status.
    return null
  }
}

function defaultCode(status: number): string {
  if (status === 429) return 'daily_limit'
  if (status >= 500) return 'server_error'
  return 'http_error'
}

function defaultMessage(status: number): string {
  if (status === 429) {
    return 'You have used all of today’s checks. The count resets at midnight Singapore time.'
  }
  return SERVER_MESSAGE
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null
}
