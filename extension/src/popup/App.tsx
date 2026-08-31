/**
 * The Re-Vera toolbar popup.
 *
 * The popup holds no state of its own beyond what it is looking at. On open it
 * asks the service worker for the current `JobState` (`GET_STATE`) and then
 * re-renders from the `STATE` messages the worker pushes on every change, so
 * closing the popup mid-check and reopening it shows the real partial progress
 * rather than restarting anything. The one message it sends on its own is
 * `START_CHECK`, from the reader's click — nothing here runs before that
 * (CLAUDE.md rule 5).
 *
 * Layout and copy follow docs/design-handoff.md § 1 with "Sieve" replaced by
 * "Re-Vera". Milestone 1 renders four states — ready, checking, done, error —
 * and stubs the two surfaces that arrive later: **Guess first** (game mode,
 * milestone 4) and **Open full report** (side panel, milestone 4) are rendered
 * disabled and say so. The cut features — reader counter, thumbs feedback,
 * "Report a mistake", the score banner — are not here at all.
 *
 * Two things the first cut got wrong, and how they are handled now:
 *
 *  - **A finished check is not a dead end.** `JobState` outlives the popup and
 *    the worker (it lives in `chrome.storage.session`), so a `done` state with
 *    no way out left the reader staring at the previous article's counts with
 *    no button. The done state now carries its own primary control, and both
 *    the checking and the done state name the article the result belongs to.
 *  - **Rows are article-ordered, not arrival-ordered.** Claims resolve out of
 *    order on a live run and in order on a cache replay; `claims_found` now
 *    lists every claim id in article order up front, so every row exists (as a
 *    skeleton) before any claim lands and each row is written exactly once.
 */

import { useCallback, useEffect, useRef, useState, type ReactElement } from 'react'

import {
  INITIAL_JOB_STATE,
  type GetStateMessage,
  type JobState,
  type StartCheckMessage,
  type StateMessage,
} from '../shared/messages'
// One rule, shared with the service worker. The popup only labels a button with
// the answer; the worker discards a finished result with it. A private copy here
// is how the two drifted last time — see src/shared/url.ts.
import { sameArticle } from '../shared/url'
import type { Claim } from '../types/schema'
import ClaimRow from './ClaimRow'
import Stepper from './Stepper'
import Summary from './Summary'
import { FunnelMark } from './verdictIcons'

const COPY = {
  check: 'Check this article',
  checkAgain: 'Check again',
  guess: 'Guess first',
  guessSoon: 'Guess first arrives in a later Re-Vera update.',
  report: 'Open full report',
  reportSoon: 'The full report opens in the side panel, which arrives in a later Re-Vera update.',
  privacy:
    'Article text is sent to Re-Vera to check it. Nothing is stored with your identity.',
  tryAgain: 'Try again',
  untitled: 'This page',
  checkedArticle: 'Checked article',
  cached: 'Re-Vera checked this article recently and reused that result.',
  limitTitle: 'Daily limit reached',
  errorTitle: 'The check did not finish',
  unreachable:
    'Re-Vera is not responding. Reload the extension from chrome://extensions and try again.',
  genericError: 'Something went wrong while checking this article. Please try again.',
} as const

/** Whatever the tab is showing, for the ready state's article card. */
interface PageInfo {
  title: string
  url: string
}

export default function App(): ReactElement {
  const view = useJobState()

  return (
    <div className="rv-app rv-theme-light">
      <header className="rv-header">
        <span className="rv-logo">
          <FunnelMark />
        </span>
        <span className="rv-wordmark">Re-Vera</span>
      </header>
      <main>{renderState(view)}</main>
    </div>
  )
}

function renderState(view: JobStateView): ReactElement {
  switch (view.state.status) {
    case 'extracting':
    case 'checking':
      return <CheckingState state={view.state} />
    case 'done':
      // The done state gets the same handler as the error state: a finished
      // check must never be a state the reader cannot leave.
      return <DoneState state={view.state} onCheck={view.startCheck} />
    case 'error':
      return <ErrorState state={view.state} onRetry={view.startCheck} />
    default:
      return <ReadyState onCheck={view.startCheck} />
  }
}

/* -------------------------------------------------------------------------- */
/* Wiring                                                                      */
/* -------------------------------------------------------------------------- */

interface JobStateView {
  state: JobState
  startCheck: () => void
}

/**
 * Subscribe to the service worker's job state.
 *
 * `GET_STATE` seeds the view; every later change arrives as a pushed `STATE`.
 *
 * Both replies — to `GET_STATE` and to `START_CHECK` — describe the job as it
 * was when the message was sent, and a `STATE` push can easily overtake them
 * (the worker broadcasts on every SSE event while a reply is still crossing the
 * message channel). Applying a reply that has been overtaken rewinds the popup:
 * rows that had already resolved go back to skeletons. So every state the popup
 * renders bumps `version`, each request remembers the version it was sent at,
 * and a reply is dropped unless that version is still the one on screen.
 */
function useJobState(): JobStateView {
  const [state, setState] = useState<JobState>(INITIAL_JOB_STATE)
  /** How many states have been rendered. Monotonic; only ever compared, never shown. */
  const version = useRef(0)

  useEffect(() => {
    let live = true

    const onMessage = (message: unknown): void => {
      if (!live || !isStateMessage(message)) return
      version.current += 1
      setState(message.state)
    }
    chrome.runtime.onMessage.addListener(onMessage)

    const at = version.current
    const request: GetStateMessage = { type: 'GET_STATE' }
    void chrome.runtime
      .sendMessage(request)
      .then((response: unknown) => {
        if (!live || version.current !== at || !isStateMessage(response)) return
        version.current += 1
        setState(response.state)
      })
      .catch(() => {
        if (!live || version.current !== at) return
        version.current += 1
        setState(unreachable)
      })

    return () => {
      live = false
      chrome.runtime.onMessage.removeListener(onMessage)
    }
  }, [])

  const startCheck = useCallback(() => {
    // Show the first step immediately; the worker's reply carries the same
    // state a moment later and every step after it arrives as a push.
    version.current += 1
    setState((previous) => ({
      ...INITIAL_JOB_STATE,
      url: previous.url,
      title: previous.title,
      status: 'extracting',
    }))

    const at = version.current
    const request: StartCheckMessage = { type: 'START_CHECK' }
    void chrome.runtime
      .sendMessage(request)
      .then((response: unknown) => {
        if (version.current !== at || !isStateMessage(response)) return
        version.current += 1
        setState(response.state)
      })
      .catch(() => {
        if (version.current !== at) return
        version.current += 1
        setState(unreachable)
      })
  }, [])

  return { state, startCheck }
}

/** The tab the reader is looking at, for the ready state's title card. */
function usePageInfo(): PageInfo | null {
  const [page, setPage] = useState<PageInfo | null>(null)

  useEffect(() => {
    let live = true
    void (async () => {
      try {
        // Opening the popup grants `activeTab` for this tab, which is what
        // makes the title and URL readable here without the `tabs` permission.
        const [tab] = await chrome.tabs.query({ active: true, currentWindow: true })
        if (live && tab) setPage({ title: tab.title ?? '', url: tab.url ?? '' })
      } catch {
        // No access to the tab: the card falls back to generic copy.
      }
    })()
    return () => {
      live = false
    }
  }, [])

  return page
}

const unreachable: JobState = {
  ...INITIAL_JOB_STATE,
  status: 'error',
  error: { code: 'no_worker', message: COPY.unreachable },
}

function isStateMessage(value: unknown): value is StateMessage {
  if (typeof value !== 'object' || value === null) return false
  const message = value as Partial<StateMessage>
  return message.type === 'STATE' && typeof message.state === 'object' && message.state !== null
}

/**
 * The claim ids of the current job, in article order, or null if this state has
 * none.
 *
 * The service worker records them from the `claims_found` event (decision 15)
 * onto `JobState.claimIds`. The value is still validated rather than trusted:
 * it arrives over `chrome.runtime` messaging from a worker that may be an
 * older build, and via `chrome.storage.session`, which outlives code. A state
 * that carries no usable list — an older worker, an older backend still
 * sending `{type, count}` — falls back to `claim.start` order instead of
 * breaking.
 */
function claimIdsOf(state: JobState): string[] | null {
  const raw: unknown = state.claimIds
  if (!Array.isArray(raw) || raw.length === 0) return null
  return raw.every((id): id is string => typeof id === 'string') ? raw : null
}

/* -------------------------------------------------------------------------- */
/* States                                                                      */
/* -------------------------------------------------------------------------- */

function ReadyState({ onCheck }: { onCheck: () => void }): ReactElement {
  const page = usePageInfo()

  return (
    <div className="rv-stack">
      <ArticleCard title={page?.title ?? null} url={page?.url ?? null} />

      <button type="button" className="rv-btn rv-btn-primary" onClick={onCheck}>
        {COPY.check}
      </button>

      {/* Game mode is milestone 4; the button keeps its place in the layout and
          says why it does nothing yet. The reason lives on the wrapper as well
          as the button, because a disabled control swallows the pointer events
          its own tooltip would need. */}
      <span className="rv-btn-slot" title={COPY.guessSoon}>
        <button
          type="button"
          className="rv-btn rv-btn-secondary"
          disabled
          title={COPY.guessSoon}
          aria-label={`${COPY.guess}. ${COPY.guessSoon}`}
        >
          {COPY.guess}
        </button>
      </span>

      <p className="rv-privacy">{COPY.privacy}</p>
    </div>
  )
}

function CheckingState({ state }: { state: JobState }): ReactElement {
  return (
    <div className="rv-stack rv-stack-check">
      {/* Nothing is known about the article until the extractor answers, so
          during `extracting` there is simply no card. */}
      {(state.title !== null || state.url !== null) && (
        <ArticleCard title={state.title} url={state.url} />
      )}
      <Stepper
        // `extracting` and `checking` are the only statuses that reach here.
        status={state.status === 'checking' ? 'checking' : 'extracting'}
        claimCount={state.claimCount}
        resolvedCount={state.claims.length}
      />
      <ClaimList
        claims={state.claims}
        claimIds={claimIdsOf(state)}
        claimCount={state.claimCount}
      />
    </div>
  )
}

/**
 * The finished check — and the way out of it.
 *
 * `JobState` is persisted in `chrome.storage.session`, so this state survives
 * the popup closing, the service worker being torn down, and the reader
 * navigating to a different article. Without a control of its own it was a trap:
 * the only status that offered a "Check this article" button was `idle`, and
 * nothing here ever returned to it. The button below is the same `startCheck`
 * the ready and error states use, and the card above it says which article the
 * numbers describe — which is the other half of the confusion, since the result
 * on screen may belong to an article the reader has already left.
 */
function DoneState({ state, onCheck }: { state: JobState; onCheck: () => void }): ReactElement {
  const page = usePageInfo()
  // Only claim "again" when we positively know the tab still holds the article
  // that was checked; when the tab is unknown, the honest label is the one that
  // describes what the button actually does.
  const again = page !== null && state.url !== null && sameArticle(page.url, state.url)

  return (
    <div className="rv-stack">
      {(state.title !== null || state.url !== null) && (
        <ArticleCard title={state.title} url={state.url} caption={COPY.checkedArticle} />
      )}

      {state.counts !== null && <Summary counts={state.counts} />}

      <ClaimList
        claims={state.claims}
        claimIds={claimIdsOf(state)}
        claimCount={state.claimCount}
      />

      <button type="button" className="rv-btn rv-btn-primary" onClick={onCheck}>
        {again ? COPY.checkAgain : COPY.check}
      </button>

      {/* The side panel is milestone 4. */}
      <span className="rv-btn-slot" title={COPY.reportSoon}>
        <button
          type="button"
          className="rv-btn rv-btn-secondary rv-btn-compact"
          disabled
          title={COPY.reportSoon}
          aria-label={`${COPY.report}. ${COPY.reportSoon}`}
        >
          {COPY.report}
        </button>
      </span>

      {state.cached && <p className="rv-footnote">{COPY.cached}</p>}
    </div>
  )
}

function ErrorState({
  state,
  onRetry,
}: {
  state: JobState
  onRetry: () => void
}): ReactElement {
  const code = state.error?.code ?? 'unexpected'
  const message = state.error?.message ?? COPY.genericError
  const limited = code === 'daily_limit'

  return (
    <div className="rv-stack">
      <div className="rv-error" role="alert">
        <span className="rv-error-title">{limited ? COPY.limitTitle : COPY.errorTitle}</span>
        <span className="rv-error-message">{message}</span>
      </div>

      {/* Retrying a daily limit just spends another 429, so that one state
          offers no button — the message already says when it resets. */}
      {!limited && (
        <button type="button" className="rv-btn rv-btn-primary rv-btn-compact" onClick={onRetry}>
          {COPY.tryAgain}
        </button>
      )}
    </div>
  )
}

/* -------------------------------------------------------------------------- */
/* Article card                                                                */
/* -------------------------------------------------------------------------- */

/** The soft-bg title card: what article this screen is talking about. */
function ArticleCard({
  title,
  url,
  caption,
}: {
  title: string | null
  url: string | null
  caption?: string
}): ReactElement {
  const heading = title !== null && title.trim() !== '' ? title.trim() : COPY.untitled
  const site = url === null ? null : siteLabel(url)

  return (
    <div className="rv-article">
      {caption !== undefined && <div className="rv-article-caption">{caption}</div>}
      <div className="rv-article-title">{heading}</div>
      {site !== null && <div className="rv-article-meta">{site}</div>}
    </div>
  )
}

/* -------------------------------------------------------------------------- */
/* Claim list                                                                  */
/* -------------------------------------------------------------------------- */

interface Row {
  /**
   * React key — the row's *position*, never the claim's id.
   *
   * Row n is `claim_ids[n]` by construction, so the index IS the row's
   * identity, and it is the only identity available on both sides of the two
   * transitions a row goes through. Keying by id looked more precise and was
   * strictly worse: on a cache hit `POST /check` answers with
   * `claim_count: 6` while `claim_ids` is still unknown, so the list rendered
   * six `pending-0…5` rows and then, the instant `claims_found` landed, six
   * `c1…c6` rows. Six new keys means React unmounts and remounts every row —
   * a full flash on the one path (a cache hit) that is meant to feel instant.
   * With the index, the same six `<li>` nodes simply gain their content.
   */
  key: string
  /** 1-based position in the article, for "Claim 3 of 6". */
  index: number
  claim: Claim | null
}

interface ClaimListProps {
  /** Claims received so far, in arrival order — which is not article order. */
  claims: Claim[]
  /** Every claim id this job will send, in article order; null on an older worker. */
  claimIds: string[] | null
  /** How many claims the backend said it would check; null until `claims_found`. */
  claimCount: number | null
}

function ClaimList({ claims, claimIds, claimCount }: ClaimListProps): ReactElement | null {
  const rows = buildRows(claims, claimIds, claimCount)
  if (rows.length === 0) return null

  return (
    <ol className="rv-rows">
      {rows.map((row) => (
        <ClaimRow key={row.key} index={row.index} total={rows.length} claim={row.claim} />
      ))}
    </ol>
  )
}

/**
 * One row per claim, in article order.
 *
 * `claims_found` hands over every claim id ascending by the claim's `start`
 * offset, so all the rows can be allocated before a single claim arrives and
 * each one is filled when its own claim lands. That is what makes a live run
 * (which resolves 3, 1, 6, 4, 2, 5 — the demo's scattered fill,
 * docs/design-handoff.md § 1 state C) and a cache replay (which resolves 1..6)
 * render the same article the same way, with no row ever rewritten. A claim
 * whose id is not in the list is ignored rather than appended: an unexpected id
 * is a bug on the wire, not a seventh row.
 *
 * Without usable ids — an older backend, an older persisted state, or the brief
 * moment on a cache hit when `claim_count` is known and `claims_found` has not
 * arrived — the rows fall back to `claim.start` order, which lands in the right
 * place once everything has arrived even though earlier rows may be rewritten on
 * the way.
 *
 * Both branches key by position, which is what lets a row survive the crossing
 * between them: see the note on `Row.key`.
 */
function buildRows(claims: Claim[], claimIds: string[] | null, claimCount: number | null): Row[] {
  const expected = Math.max(claimCount ?? 0, 0)

  if (claimIds !== null && claimIds.length >= expected) {
    const byId = new Map(claims.map((claim) => [claim.id, claim]))
    return claimIds.map((id, i) => ({ key: rowKey(i), index: i + 1, claim: byId.get(id) ?? null }))
  }

  const ordered = [...claims].sort((a, b) => a.start - b.start)
  const total = Math.max(expected, ordered.length)
  return Array.from({ length: total }, (_, i) => ({
    key: rowKey(i),
    index: i + 1,
    claim: ordered[i] ?? null,
  }))
}

/** The row's identity: where it sits in the article. */
function rowKey(index: number): string {
  return `row-${index}`
}

/* -------------------------------------------------------------------------- */
/* Helpers                                                                     */
/* -------------------------------------------------------------------------- */

/** "https://news.yahoo.com/…" -> "news.yahoo.com". Null for anything unparseable. */
function siteLabel(url: string): string | null {
  try {
    return new URL(url).hostname.replace(/^www\./, '')
  } catch {
    return null
  }
}
