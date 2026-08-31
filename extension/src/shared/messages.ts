/**
 * The typed contract between the popup, the background service worker and the
 * injected content script.
 *
 * Only the background talks to the backend. The popup is a pure view over
 * `JobState`: it asks for the current state on open (`GET_STATE`) and the
 * background pushes a `STATE` message on every change, so closing and
 * reopening the popup mid-check shows correct partial progress.
 */

import type { Claim, Counts } from '../types/schema'

export type JobStatus = 'idle' | 'extracting' | 'checking' | 'done' | 'error'

export interface JobState {
  status: JobStatus
  url: string | null
  title: string | null
  /** How many claims the backend said it would check; null until `claims_found`. */
  claimCount: number | null
  /** Resolved claims so far, in arrival order. */
  claims: Claim[]
  /** Per-verdict tally; null until the `done` event. */
  counts: Counts | null
  /** True when the backend replayed a cached result instead of checking afresh. */
  cached: boolean
  error: { code: string; message: string } | null
}

export const INITIAL_JOB_STATE: JobState = {
  status: 'idle',
  url: null,
  title: null,
  claimCount: null,
  claims: [],
  counts: null,
  cached: false,
  error: null,
}

/* -------------------------------------------------------------------------- */
/* popup -> background                                                         */
/* -------------------------------------------------------------------------- */

export type StartCheckMessage = { type: 'START_CHECK' }
export type GetStateMessage = { type: 'GET_STATE' }

export type PopupMessage = StartCheckMessage | GetStateMessage

/* -------------------------------------------------------------------------- */
/* background -> popup                                                         */
/* -------------------------------------------------------------------------- */

export type StateMessage = { type: 'STATE'; state: JobState }

/* -------------------------------------------------------------------------- */
/* background -> content script                                                */
/* -------------------------------------------------------------------------- */

export type ExtractMessage = { type: 'EXTRACT' }

export interface ExtractedArticle {
  url: string
  title: string
  text: string
}
