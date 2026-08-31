/**
 * The Re-Vera service worker: the only part of the extension that talks to the
 * backend, and the owner of the current check.
 *
 * The whole flow hangs off one user gesture — the reader clicking **Check this
 * article** in the popup. Nothing scans, nothing polls, nothing runs at install
 * (CLAUDE.md rule 5). On `START_CHECK` the worker:
 *
 *   1. injects the extractor into the active tab and asks it for the article;
 *   2. `POST /check` with the article and the anonymous install ID;
 *   3. streams `GET /check/{job_id}/stream`, folding each SSE event into
 *      `JobState`;
 *   4. broadcasts every new `JobState` to the popup and badges the toolbar
 *      icon with the claim count when the job finishes.
 *
 * The popup may close and reopen at any point. It holds no state of its own: it
 * asks for the current `JobState` on open (`GET_STATE`) and re-renders from the
 * `STATE` messages pushed here, which is what makes reopening mid-check show
 * correct partial progress.
 */

// CRXJS builds the extractor as a self-contained IIFE bundle and hands back its
// emitted path — "src/content/extract.js" in a production build,
// "src/content/extract.ts.iife.js" under `pnpm dev`. Never hardcode either:
// `executeScript({ files })` runs the file as a classic script, so this import
// is also what guarantees the bundle has no `import` statements in it.
import extractScript from '../content/extract?script&iife'
import {
  INITIAL_JOB_STATE,
  type ExtractMessage,
  type ExtractedArticle,
  type JobState,
  type StateMessage,
} from '../shared/messages'
// The same-article rule is shared with the popup on purpose. This side decides
// whether to DISCARD a finished result; the popup side only labels a button.
// When they were separate copies they drifted (the popup normalised trailing
// slashes, this did not), and the stricter copy was the one that threw work
// away — see src/shared/url.ts.
import { sameArticle } from '../shared/url'
import type { Claim, ClaimsFoundEvent, DoneEvent, ErrorEvent } from '../types/schema'
import { ApiError, openStream, postCheck } from './api'
import { getInstallId } from './installId'
import { getState, setState } from './jobStore'
import type { SSEMessage } from './sse'

/** Coral, the contradicted verdict base colour (docs/design-handoff.md). */
const BADGE_BACKGROUND = '#C24A32'
const BADGE_TEXT_COLOUR = '#FFFFFF'

/** Schemes Chrome refuses to inject into — and which are never articles anyway. */
const BLOCKED_SCHEMES = [
  'chrome:',
  'chrome-extension:',
  'edge:',
  'about:',
  'devtools:',
  'view-source:',
]

const MESSAGES = {
  noTab: 'Re-Vera could not find the page to check. Open the article and try again.',
  notAnArticle: 'Re-Vera can only check pages on the web. Open a news article and try again.',
  injectFailed:
    'Re-Vera could not read this page. Reload the article and try again — some pages block extensions.',
  noText: 'Re-Vera could not find any article text on this page.',
  interrupted: 'The check stopped before it finished. Please try again.',
  streamEnded: 'The connection to Re-Vera ended before the check finished. Please try again.',
  unexpected: 'Something went wrong while checking this article. Please try again.',
} as const

/** A step of the check flow failing for a reason the reader can act on. */
class FlowError extends Error {
  readonly code: string

  constructor(code: string, message: string) {
    super(message)
    this.name = 'FlowError'
    this.code = code
  }
}

/* -------------------------------------------------------------------------- */
/* Live state                                                                  */
/* -------------------------------------------------------------------------- */

/**
 * The authoritative state while this worker is alive.
 *
 * SSE events arrive faster than a storage round-trip, so they are folded into
 * this synchronously and persisted afterwards — reading storage per event would
 * race and drop claims.
 */
let jobState: JobState = INITIAL_JOB_STATE

/** The in-flight check, if any. Non-null is what "a check is running" means. */
let activeRun: Promise<void> | null = null

/** Aborts the SSE fetch when a run ends or is superseded. */
let streamAbort: AbortController | null = null

/** Highest SSE sequence number applied to `jobState`; resets per job. */
let lastSeq = 0

/** Tab currently wearing the badge, so a new check can clear the right one. */
let badgeTabId: number | null = null

/** Tab the running check is reading, so its badge lands on the right one. */
let checkedTabId: number | null = null

/* -------------------------------------------------------------------------- */
/* Message routing                                                             */
/* -------------------------------------------------------------------------- */

// Registered at the top level: MV3 wakes the worker for a message only if the
// listener is attached during the first turn of script evaluation.
chrome.runtime.onMessage.addListener((message: unknown, _sender, sendResponse) => {
  if (!isRecord(message)) return

  if (message.type === 'GET_STATE') {
    // `ready` is the one place the persisted state is adopted; everything else
    // reads the live variable, so answering here can never roll the popup back
    // to a snapshot that a claim event has already moved past.
    // `dropStaleResult` runs before the reply so the popup never paints one
    // article's result over another article — see its own comment.
    void ready
      .then(dropStaleResult)
      .then(() => sendResponse(stateMessage(jobState)))
    return true // keep the channel open for the async reply
  }

  if (message.type === 'START_CHECK') {
    // Answer with the state the click produced right away; the check itself
    // keeps running and reports its progress through STATE broadcasts, so the
    // message channel is not held open for the seven seconds it takes.
    sendResponse(stateMessage(startCheck()))
    return
  }

  return
})

function stateMessage(state: JobState): StateMessage {
  return { type: 'STATE', state }
}

/**
 * Push the new state to whoever is listening.
 *
 * With the popup closed there is no receiver and `sendMessage` rejects with
 * "Could not establish connection. Receiving end does not exist." That is the
 * normal case — the check runs happily without a popup — so it is swallowed
 * rather than allowed to reject into the streaming loop.
 */
function broadcast(state: JobState): void {
  chrome.runtime.sendMessage(stateMessage(state)).catch(() => undefined)
}

/* -------------------------------------------------------------------------- */
/* State updates                                                               */
/* -------------------------------------------------------------------------- */

/** Merge `patch` into the live state, broadcast it, and persist it. */
function update(patch: Partial<JobState>): JobState {
  jobState = { ...jobState, ...patch }
  const snapshot = jobState
  broadcast(snapshot)
  // Persisting is queued inside jobStore, so writes land in the order the
  // updates happened even though this does not await them.
  void setState(snapshot)
  return snapshot
}

/* -------------------------------------------------------------------------- */
/* The check flow                                                              */
/* -------------------------------------------------------------------------- */

/**
 * Run a check, unless one is already running.
 *
 * The guard is "is there a run in *this* worker", not "does the stored status
 * say checking". A worker torn down mid-check leaves `checking` behind with no
 * stream attached; keying off the stored status would then lock the reader out
 * of ever starting another check. `reconcile()` below turns that stale status
 * into a visible error instead.
 */
function startCheck(): JobState {
  if (activeRun) return jobState

  // Everything up to the first await happens in one synchronous block, so the
  // guard above cannot be raced and the caller gets the fresh state back
  // immediately.
  streamAbort?.abort()
  streamAbort = null
  lastSeq = 0
  checkedTabId = null
  void clearBadge()

  jobState = { ...INITIAL_JOB_STATE, status: 'extracting' }
  broadcast(jobState)
  void setState(jobState)

  const run = runCheck()
  activeRun = run
  // `runCheck` resolves rather than rejects, but a bug in its own error path
  // must not become an unhandled rejection in a service worker.
  void run.catch(() => undefined).finally(() => {
    // Only the run that is still current may clear the slot. Whatever went
    // wrong — a thrown error, an aborted fetch, a watchdog firing — the slot
    // MUST end up null, because a non-null `activeRun` makes every later
    // START_CHECK a no-op and leaves the reader looking at a popup with no
    // button in it.
    if (activeRun === run) {
      activeRun = null
      streamAbort = null
    }
  })
  return jobState
}

async function runCheck(): Promise<void> {
  try {
    const tabId = await activeTabId()
    checkedTabId = tabId
    const article = await extractArticle(tabId)
    update({ url: article.url, title: article.title })

    const installId = await getInstallId()
    const job = await postCheck({
      url: article.url,
      title: article.title,
      text: article.text,
      install_id: installId,
    })

    update({ status: 'checking', cached: job.cached, claimCount: job.claim_count })

    streamAbort = new AbortController()
    // `openStream` is guaranteed to settle: it carries its own idle and total
    // watchdogs, so a backend that keep-alives forever without ever sending
    // `done` ends up here as an error the reader can retry, not as a promise
    // that never resolves.
    await openStream(job.job_id, applyEvent, streamAbort.signal)

    // The backend closes the stream after `done` or `error`. Reaching here in
    // any other status means the connection dropped mid-job.
    if (jobState.status !== 'done' && jobState.status !== 'error') {
      fail('stream_ended', MESSAGES.streamEnded)
    }
  } catch (error) {
    const { code, message } = describeError(error)
    fail(code, message)
  } finally {
    // Belt and braces with startCheck's own cleanup: nothing after this point
    // may leave a dead run holding the lock.
    streamAbort?.abort()
  }
}

/** Fold one SSE message into the job state. */
function applyEvent(message: SSEMessage): void {
  // The stream replays everything already published before going live, so a
  // reconnect re-delivers events this worker has applied. The id is the job's
  // monotonic sequence number, which makes dropping them exact.
  if (message.id !== undefined) {
    const seq = Number(message.id)
    if (Number.isFinite(seq)) {
      if (seq <= lastSeq) return
      lastSeq = seq
    }
  }

  switch (message.event) {
    case 'claims_found': {
      const payload = parseJson<ClaimsFoundEvent>(message.data)
      if (payload === null) return
      // `count` and `claim_ids.length` are the same number by contract
      // (docs/decisions.md §15). When both are present the list wins: it is the
      // thing the rows are actually keyed by, so a disagreement must not leave
      // the popup allocating a different number of rows than it can ever fill.
      const ids = Array.isArray(payload.claim_ids)
        ? payload.claim_ids.filter((id): id is string => typeof id === 'string')
        : null
      const count = ids !== null ? ids.length : payload.count
      if (typeof count !== 'number' || !Number.isFinite(count)) return
      // The list is carried on, not just counted: it is the article order the
      // popup allocates and fills its rows by. Dropping it here is what left
      // the live path and the cache replay rendering one article two ways.
      update({ status: 'checking', claimCount: count, claimIds: ids })
      return
    }

    case 'claim': {
      const claim = parseJson<Claim>(message.data)
      if (typeof claim?.id !== 'string') return
      // Belt and braces next to the sequence check: a claim already shown must
      // never appear twice in the popup's list.
      if (jobState.claims.some((existing) => existing.id === claim.id)) return
      update({ status: 'checking', claims: [...jobState.claims, claim] })
      return
    }

    case 'done': {
      const payload = parseJson<DoneEvent>(message.data)
      const state = update({ status: 'done', counts: payload?.counts ?? null })
      void showBadge(state.claims.length || state.claimCount || 0)
      return
    }

    case 'error': {
      const payload = parseJson<ErrorEvent>(message.data)
      fail(payload?.code ?? 'stream_error', payload?.message ?? MESSAGES.unexpected)
      return
    }

    default:
      // Unknown event names are ignored so the backend can add one without
      // breaking an older extension build.
      return
  }
}

function fail(code: string, message: string): void {
  update({ status: 'error', error: { code, message } })
}

/** Map anything thrown during the flow onto a code and a reader-facing sentence. */
function describeError(error: unknown): { code: string; message: string } {
  if (error instanceof ApiError || error instanceof FlowError) {
    return { code: error.code, message: error.message }
  }
  if (error instanceof DOMException && error.name === 'AbortError') {
    return { code: 'aborted', message: MESSAGES.interrupted }
  }
  return { code: 'unexpected', message: MESSAGES.unexpected }
}

/* -------------------------------------------------------------------------- */
/* Tab + extraction                                                            */
/* -------------------------------------------------------------------------- */

/**
 * Forget a finished result that belongs to a different article.
 *
 * `JobState` outlives the popup (it is persisted in `chrome.storage.session`),
 * which is what makes closing and reopening the popup mid-check work — but the
 * same persistence meant that opening the popup on a *second* article showed
 * the *first* article's verdicts, with no hint that they were about something
 * else. Nothing else ever returned the state to `idle`.
 *
 * So: when the popup asks for state, if the tab it is opening over is not the
 * article the stored result describes, throw the result away and give it the
 * ready state for what the reader is actually looking at.
 *
 * Three guards keep this from eating something it should not:
 *
 *  - a run owned by this worker is never touched, so a check the reader started
 *    survives them switching tabs while it runs;
 *  - `idle` has nothing to drop;
 *  - a tab URL we cannot read (no `activeTab` grant, a closed window) leaves
 *    the state alone. Guessing would be worse than being stale.
 *
 * This hangs off `GET_STATE` — a message the popup only sends because the
 * reader opened it — rather than off `chrome.tabs.onActivated`/`onUpdated`.
 * Both would work, but tab listeners wake the service worker on every
 * navigation in every tab, and CLAUDE.md rule 5 is that nothing in Re-Vera runs
 * except on the reader's own gesture. Opening the popup is that gesture, and it
 * is also the only moment the answer is ever read.
 */
async function dropStaleResult(): Promise<void> {
  if (activeRun) return
  if (jobState.status === 'idle') return
  // Nothing to compare against: the run failed before it ever identified an
  // article (a blocked scheme, a page that refused injection). Leaving the
  // error up is the honest option — the popup's Try again re-runs against
  // whatever tab is in front of the reader now.
  if (jobState.url === null) return

  const current = await activeTabUrl()
  if (current === null) return
  if (sameArticle(jobState.url, current)) return

  lastSeq = 0
  checkedTabId = null
  void clearBadge()
  jobState = INITIAL_JOB_STATE
  broadcast(jobState)
  void setState(jobState)
}

/** URL of the tab the popup is sitting over, or null when it cannot be read. */
async function activeTabUrl(): Promise<string | null> {
  try {
    const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true })
    return tab?.url ?? null
  } catch {
    return null
  }
}

async function activeTabId(): Promise<number> {
  // `lastFocusedWindow` rather than `currentWindow`: a service worker has no
  // window of its own for `currentWindow` to resolve against.
  const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true })
  if (!tab?.id) throw new FlowError('no_tab', MESSAGES.noTab)
  if (tab.url && BLOCKED_SCHEMES.some((scheme) => tab.url?.startsWith(scheme))) {
    throw new FlowError('not_an_article', MESSAGES.notAnArticle)
  }
  return tab.id
}

/**
 * Inject the extractor and ask it for the article.
 *
 * Injection happens here and nowhere else: the manifest has no
 * `content_scripts` entry, so this click is the first moment any Re-Vera code
 * touches the page.
 */
async function extractArticle(tabId: number): Promise<ExtractedArticle> {
  try {
    await chrome.scripting.executeScript({ target: { tabId }, files: [extractScript] })
  } catch {
    throw new FlowError('inject_failed', MESSAGES.injectFailed)
  }

  let response: unknown
  try {
    const request: ExtractMessage = { type: 'EXTRACT' }
    response = await chrome.tabs.sendMessage(tabId, request)
  } catch {
    throw new FlowError('extract_failed', MESSAGES.injectFailed)
  }

  if (!isExtractedArticle(response)) throw new FlowError('no_text', MESSAGES.noText)
  return response
}

function isExtractedArticle(value: unknown): value is ExtractedArticle {
  if (!isRecord(value)) return false
  return (
    typeof value.url === 'string' &&
    typeof value.title === 'string' &&
    typeof value.text === 'string' &&
    value.text.trim().length > 0
  )
}

/* -------------------------------------------------------------------------- */
/* Toolbar badge                                                               */
/* -------------------------------------------------------------------------- */

/**
 * Coral badge with the claim count, scoped to the tab that was checked.
 *
 * Scoping matters: the count describes one article, so it must not follow the
 * reader onto every other tab.
 */
async function showBadge(count: number): Promise<void> {
  if (count <= 0) return
  const target = checkedTabId === null ? {} : { tabId: checkedTabId }
  badgeTabId = checkedTabId
  try {
    await chrome.action.setBadgeText({ ...target, text: String(count) })
    await chrome.action.setBadgeBackgroundColor({ ...target, color: BADGE_BACKGROUND })
    // Chrome 110+; older builds keep the automatic contrasting colour.
    await chrome.action.setBadgeTextColor({ ...target, color: BADGE_TEXT_COLOUR })
  } catch {
    // A closed tab makes these reject. The badge is decoration — never let it
    // take the check down with it.
  }
}

/** Wipe the badge — both the global text and whichever tab last carried one. */
async function clearBadge(): Promise<void> {
  const previous = badgeTabId
  badgeTabId = null
  try {
    await chrome.action.setBadgeText({ text: '' })
    if (previous !== null) await chrome.action.setBadgeText({ tabId: previous, text: '' })
  } catch {
    // Same reasoning as showBadge.
  }
}

/* -------------------------------------------------------------------------- */
/* Worker startup                                                              */
/* -------------------------------------------------------------------------- */

/**
 * Adopt the persisted state on a cold worker.
 *
 * If it says a check was in flight, that check died with the previous worker —
 * its stream is gone and no event will ever arrive — so it becomes a visible
 * error rather than a stepper that spins forever. Claims already received are
 * kept; the reader sees how far it got.
 */
async function reconcile(): Promise<void> {
  let stored: JobState
  try {
    stored = await getState()
  } catch {
    return // storage unavailable: the initial state is a fine place to start
  }

  // A START_CHECK that arrived while this was awaiting owns the state now.
  if (activeRun) return
  jobState = stored

  if (stored.status === 'extracting' || stored.status === 'checking') {
    update({ status: 'error', error: { code: 'interrupted', message: MESSAGES.interrupted } })
  }
}

/** Resolves once the persisted state has been adopted. Never rejects. */
const ready: Promise<void> = reconcile()

/* -------------------------------------------------------------------------- */
/* Helpers                                                                     */
/* -------------------------------------------------------------------------- */

function parseJson<T>(raw: string): T | null {
  try {
    return JSON.parse(raw) as T
  } catch {
    return null
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null
}
