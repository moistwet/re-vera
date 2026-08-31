/**
 * Where the current check lives.
 *
 * The popup is a pure view: it owns no state, asks for `JobState` when it
 * opens and re-renders whenever the background pushes a new one. That only
 * holds if the state outlives both of them, so it is persisted in
 * `chrome.storage.session` — cleared when the browser closes (it holds article
 * URLs and titles, which have no business surviving a session) but preserved
 * across the MV3 service-worker restarts that happen constantly.
 *
 * Writes are serialised through a promise chain. Claim events arrive faster
 * than a storage round-trip, and two overlapping read-modify-writes would drop
 * a claim; queueing them makes every write see the previous one.
 */

import { INITIAL_JOB_STATE, type JobState } from '../shared/messages'

/** Key in `chrome.storage.session`. */
const STORAGE_KEY = 'job_state'

/**
 * Last known state, so `getState` does not pay a storage round-trip on the hot
 * path. Null only until the first read of a fresh worker.
 */
let memo: JobState | null = null

/** Tail of the serialised write chain. */
let queue: Promise<unknown> = Promise.resolve()

/** Run `task` after every previously queued task, whether those failed or not. */
function enqueue<T>(task: () => Promise<T>): Promise<T> {
  const run = queue.then(task, task)
  queue = run.catch(() => undefined)
  return run
}

/** The current job state, from memory or, on a cold worker, from storage. */
export async function getState(): Promise<JobState> {
  return enqueue(read)
}

/** Replace the whole state. */
export async function setState(next: JobState): Promise<void> {
  await enqueue(async () => {
    await write(next)
  })
}

/** Merge `patch` into the current state and return the result. */
export async function patchState(patch: Partial<JobState>): Promise<JobState> {
  return enqueue(async () => {
    const current = await read()
    return write({ ...current, ...patch })
  })
}

/** Read without queueing — only ever called from inside the queue. */
async function read(): Promise<JobState> {
  if (memo) return memo
  const stored = await chrome.storage.session.get(STORAGE_KEY)
  memo = reviveState(stored[STORAGE_KEY])
  return memo
}

async function write(next: JobState): Promise<JobState> {
  memo = next
  await chrome.storage.session.set({ [STORAGE_KEY]: next })
  return next
}

/**
 * Turn whatever storage handed back into a usable `JobState`.
 *
 * Storage outlives code: a state written by a previous version of the
 * extension, or a half-written one, must degrade to the initial state rather
 * than crash the worker on the popup's first `GET_STATE`.
 */
function reviveState(value: unknown): JobState {
  if (typeof value !== 'object' || value === null) return INITIAL_JOB_STATE
  const candidate = value as Partial<JobState>
  return {
    ...INITIAL_JOB_STATE,
    ...candidate,
    claims: Array.isArray(candidate.claims) ? candidate.claims : [],
    // Same reasoning as `claims`, plus one more: a state written before
    // decision 15 has no `claimIds` at all, and the popup keys its rows off
    // this list — a non-array here would make it allocate rows from garbage.
    claimIds: Array.isArray(candidate.claimIds) ? candidate.claimIds : null,
  }
}
