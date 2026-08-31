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
 */

import { useCallback, useEffect, useRef, useState, type ReactElement } from 'react'

import {
  INITIAL_JOB_STATE,
  type GetStateMessage,
  type JobState,
  type StartCheckMessage,
  type StateMessage,
} from '../shared/messages'
import type { Claim } from '../types/schema'
import ClaimRow from './ClaimRow'
import Stepper from './Stepper'
import Summary from './Summary'
import { FunnelMark } from './verdictIcons'

const COPY = {
  check: 'Check this article',
  guess: 'Guess first',
  guessSoon: 'Guess first arrives in a later Re-Vera update.',
  report: 'Open full report',
  reportSoon: 'The full report opens in the side panel, which arrives in a later Re-Vera update.',
  privacy:
    'Article text is sent to Re-Vera to check it. Nothing is stored with your identity.',
  tryAgain: 'Try again',
  untitled: 'This page',
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
  const state = useJobState()

  return (
    <div className="rv-app rv-theme-light">
      <header className="rv-header">
        <span className="rv-logo">
          <FunnelMark />
        </span>
        <span className="rv-wordmark">Re-Vera</span>
      </header>
      <main>{renderState(state)}</main>
    </div>
  )
}

function renderState(view: JobStateView): ReactElement {
  switch (view.state.status) {
    case 'extracting':
    case 'checking':
      return <CheckingState state={view.state} />
    case 'done':
      return <DoneState state={view.state} />
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
 * A push that lands before the seeded reply wins — the reply was queued
 * earlier, so applying it afterwards would rewind the popup by one event.
 */
function useJobState(): JobStateView {
  const [state, setState] = useState<JobState>(INITIAL_JOB_STATE)
  const pushed = useRef(false)

  useEffect(() => {
    let live = true

    const onMessage = (message: unknown): void => {
      if (!live || !isStateMessage(message)) return
      pushed.current = true
      setState(message.state)
    }
    chrome.runtime.onMessage.addListener(onMessage)

    const request: GetStateMessage = { type: 'GET_STATE' }
    void chrome.runtime
      .sendMessage(request)
      .then((response: unknown) => {
        if (!live || pushed.current || !isStateMessage(response)) return
        setState(response.state)
      })
      .catch(() => {
        if (live) setState(unreachable)
      })

    return () => {
      live = false
      chrome.runtime.onMessage.removeListener(onMessage)
    }
  }, [])

  const startCheck = useCallback(() => {
    // Show the first step immediately; the worker's reply carries the same
    // state a moment later and every step after it arrives as a push.
    setState((previous) => ({
      ...INITIAL_JOB_STATE,
      url: previous.url,
      title: previous.title,
      status: 'extracting',
    }))
    const request: StartCheckMessage = { type: 'START_CHECK' }
    void chrome.runtime
      .sendMessage(request)
      .then((response: unknown) => {
        if (isStateMessage(response)) setState(response.state)
      })
      .catch(() => setState(unreachable))
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

/* -------------------------------------------------------------------------- */
/* States                                                                      */
/* -------------------------------------------------------------------------- */

function ReadyState({ onCheck }: { onCheck: () => void }): ReactElement {
  const page = usePageInfo()
  const title = page?.title.trim() ? page.title.trim() : COPY.untitled
  const site = page ? siteLabel(page.url) : null

  return (
    <div className="rv-stack">
      <div className="rv-article">
        <div className="rv-article-title">{title}</div>
        {site !== null && <div className="rv-article-meta">{site}</div>}
      </div>

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
  const expected = Math.max(state.claimCount ?? 0, state.claims.length)

  return (
    <div className="rv-stack rv-stack-check">
      <Stepper
        status={state.status}
        claimCount={state.claimCount}
        resolvedCount={state.claims.length}
      />
      {expected > 0 && <ClaimList claims={state.claims} expected={expected} />}
    </div>
  )
}

function DoneState({ state }: { state: JobState }): ReactElement {
  return (
    <div className="rv-stack">
      {state.counts !== null && <Summary counts={state.counts} />}
      {state.claims.length > 0 && (
        <ClaimList claims={state.claims} expected={state.claims.length} />
      )}

      {/* The side panel is milestone 4. */}
      <span className="rv-btn-slot" title={COPY.reportSoon}>
        <button
          type="button"
          className="rv-btn rv-btn-primary rv-btn-compact"
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
/* Claim list                                                                  */
/* -------------------------------------------------------------------------- */

/**
 * `expected` rows, filled from the top in arrival order and padded with pending
 * placeholders. Claims resolve out of order, so a row is written once and never
 * rewritten — nothing already on screen shuffles when the next claim lands.
 */
function ClaimList({ claims, expected }: { claims: Claim[]; expected: number }): ReactElement {
  const slots: (Claim | null)[] = Array.from({ length: expected }, (_, i) => claims[i] ?? null)

  return (
    <ol className="rv-rows">
      {slots.map((claim, i) => (
        <ClaimRow
          key={claim?.id ?? `pending-${i}`}
          index={i + 1}
          total={expected}
          claim={claim}
        />
      ))}
    </ol>
  )
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
