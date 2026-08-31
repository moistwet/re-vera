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

/** Used when `VITE_API_BASE` is unset, and matched by the manifest's host_permissions. */
const DEFAULT_API_BASE = 'http://localhost:8000'

const NETWORK_MESSAGE =
  'Could not reach Re-Vera. Check that the backend is running, then try again.'
const SERVER_MESSAGE = 'Re-Vera had a problem checking this article. Please try again.'

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
 */
export function apiBase(): string {
  const configured = import.meta.env.VITE_API_BASE
  const base = typeof configured === 'string' && configured.length > 0 ? configured : DEFAULT_API_BASE
  return base.replace(/\/+$/, '')
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

/**
 * Open the job's SSE stream and hand every framed message to `onMessage`.
 *
 * Resolves when the backend closes the stream, which it does after `done` or
 * `error`. Aborting `signal` rejects with an `AbortError` — the caller decides
 * whether that was its own doing.
 */
export async function openStream(
  jobId: string,
  onMessage: (m: SSEMessage) => void,
  signal?: AbortSignal,
): Promise<void> {
  const url = `${apiBase()}/check/${encodeURIComponent(jobId)}/stream`
  const response = await request(url, {
    method: 'GET',
    headers: { accept: 'text/event-stream' },
    // The stream is a live job's transcript; a cached copy is never right.
    cache: 'no-store',
    signal,
  })

  if (!response.ok) throw await responseError(response)
  if (!response.body) {
    throw new ApiError('no_stream', SERVER_MESSAGE, response.status)
  }

  for await (const message of parseSSE(response.body)) {
    onMessage(message)
  }
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
