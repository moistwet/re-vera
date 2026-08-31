// @vitest-environment jsdom
/**
 * Popup tests — the two things the popup got wrong, pinned so they stay fixed.
 *
 * 1. **Row order.** Claims resolve out of article order on a live run (the mock
 *    pipeline's RESOLVE_ORDER streams rows 3, 1, 6, 4, 2, 5 — the demo's
 *    scattered fill, docs/design-handoff.md § 1 state C) and in article order on
 *    a cache replay. Filling rows in arrival order therefore rendered one
 *    article two different ways depending on the cache. `claims_found` now
 *    carries every claim id in article order (docs/decisions.md § 15), so both
 *    paths must render exactly the same rows, and no row may ever be rewritten.
 *
 * 2. **The done state was a dead end.** `JobState` lives in
 *    `chrome.storage.session`, so it outlives both the popup and the service
 *    worker; with no control on the done screen the reader was stuck looking at
 *    a finished result forever. It must offer a way to start another check, and
 *    say which article the result on screen belongs to.
 *
 * Plus the smaller guards: a stale reply must never rewind the view, a pending
 * row is a skeleton, and a resolved row carries icon *and* text label *and*
 * accessible name (CLAUDE.md rule 4 — never colour alone).
 *
 * The popup is driven the way the service worker drives it in production: a
 * `STATE` push per change, through a stubbed `chrome.runtime`. No network, no
 * real `chrome`, no timers.
 */

import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, describe, expect, it, vi } from 'vitest'

import App from '../src/popup/App'
import { INITIAL_JOB_STATE, type JobState } from '../src/shared/messages'
import type { Claim, Confidence, Counts, Verdict } from '../src/types/schema'

/* -------------------------------------------------------------------------- */
/* Fixture — the six fictional hawker-article claims, in article order          */
/* -------------------------------------------------------------------------- */

const ARTICLE_TITLE = 'Hawker stall rents to rise 40% next year, vendors say'
const ARTICLE_URL = 'https://news.example.com/hawker-stall-rents-rise'

function claim(
  id: string,
  start: number,
  quote: string,
  verdict: Verdict,
  confidence: Confidence | null,
): Claim {
  return {
    id,
    quote,
    start,
    end: start + quote.length,
    verdict,
    confidence,
    evidence:
      verdict === 'unverifiable'
        ? 'No independent report of this figure was found.'
        : 'CNA and a gov.sg release put the adjustment elsewhere.',
    // sources is [] if and only if the verdict is "unverifiable" (CLAUDE.md rule 2).
    sources:
      verdict === 'unverifiable'
        ? []
        : [{ url: 'https://cna.example/1', outlet: 'CNA', date: '2026-03-12', wire: false, stance: 'refutes' }],
    trail: [{ label: 'This article', note: 'wire copy, republished on Yahoo' }],
  }
}

/** Article order: ascending by `start`, exactly as `claim_ids` arrives. */
const CLAIMS: readonly Claim[] = [
  claim('c1', 60, 'rise by 40% from 1 January', 'contradicted', 'high'),
  claim('c2', 144, 'More than 200 stalls have already closed this year', 'unverifiable', null),
  claim('c3', 445, 'The last islandwide rent adjustment took effect in 2024', 'supported', 'high'),
  claim('c4', 664, 'eight in ten hawkers are considering leaving', 'missing_context', 'medium'),
  claim('c5', 885, 'will not be capped under any circumstances', 'contradicted', 'medium'),
  claim('c6', 931, 'The review takes effect on 1 January', 'supported', 'high'),
]

const ARTICLE_ORDER = CLAIMS.map((c) => c.id)

/** What the mock pipeline streams on a live run: rows 3, 1, 6, 4, 2, 5. */
const RESOLVE_ORDER = ['c3', 'c1', 'c6', 'c4', 'c2', 'c5']

const COUNTS: Counts = { supported: 2, contradicted: 2, missing_context: 1, unverifiable: 1 }

const VERDICT_LABELS: Record<Verdict, string> = {
  supported: 'Supported',
  contradicted: 'Contradicted',
  missing_context: 'Missing context',
  unverifiable: 'Unverifiable',
}

function byId(id: string): Claim {
  const found = CLAIMS.find((c) => c.id === id)
  if (found === undefined) throw new Error(`no fixture claim ${id}`)
  return found
}

/** How `rowSequence()` renders a resolved claim. */
function label(id: string): string {
  const c = byId(id)
  return `“${c.quote}” · ${VERDICT_LABELS[c.verdict]}`
}

/* -------------------------------------------------------------------------- */
/* Harness                                                                     */
/* -------------------------------------------------------------------------- */

/**
 * A partial state to push. `claimIds` is the field the service worker fills
 * from the `claims_found` event (docs/decisions.md § 15) and the popup keys its
 * rows by; omitting it is how these tests stand in an older worker.
 */
type StatePatch = Partial<JobState>

function jobState(patch: StatePatch): JobState {
  return { ...INITIAL_JOB_STATE, ...patch }
}

type Listener = (message: unknown) => void

interface ChromeOptions {
  /** How the "service worker" answers a popup message. Default: never answers. */
  reply?: (message: unknown) => Promise<unknown>
  /** What the active tab is showing, for `chrome.tabs.query`. */
  tab?: { title: string; url: string }
}

let listeners: Listener[] = []
let sent: unknown[] = []
let container: HTMLElement | null = null
let root: Root | null = null

// React needs to be told it is under test before the first render.
;(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

function stubChrome(options: ChromeOptions = {}): void {
  listeners = []
  sent = []
  vi.stubGlobal('chrome', {
    runtime: {
      onMessage: {
        addListener: (fn: Listener) => {
          listeners.push(fn)
        },
        removeListener: (fn: Listener) => {
          listeners = listeners.filter((l) => l !== fn)
        },
      },
      sendMessage: (message: unknown): Promise<unknown> => {
        sent.push(message)
        // A promise that never settles stands in for a busy worker: the popup
        // must render from pushes alone, which is what production does too.
        return options.reply ? options.reply(message) : new Promise<unknown>(() => undefined)
      },
    },
    tabs: {
      query: async () => (options.tab === undefined ? [] : [options.tab]),
    },
  })
}

function deferred<T>(): { promise: Promise<T>; resolve: (value: T) => void } {
  let resolve: (value: T) => void = () => undefined
  const promise = new Promise<T>((r) => {
    resolve = r
  })
  return { promise, resolve }
}

async function unmount(): Promise<void> {
  const current = root
  if (current !== null) await act(async () => current.unmount())
  container?.remove()
  root = null
  container = null
}

async function mount(): Promise<void> {
  await unmount()
  const host = document.createElement('div')
  document.body.appendChild(host)
  container = host
  const created = createRoot(host)
  root = created
  await act(async () => created.render(<App />))
  await flush()
}

/** Let queued microtasks (chrome.tabs.query, message replies) reach React. */
async function flush(): Promise<void> {
  await act(async () => undefined)
}

/** Deliver a `STATE` push exactly as the service worker broadcasts it. */
async function push(state: JobState): Promise<void> {
  await act(async () => {
    for (const listener of [...listeners]) listener({ type: 'STATE', state })
  })
  await flush()
}

function view(): HTMLElement {
  if (container === null) throw new Error('nothing mounted')
  return container
}

function rows(): HTMLElement[] {
  return [...view().querySelectorAll<HTMLElement>('li.rv-row')]
}

/** One entry per row: the resolved quote and verdict, or `'pending'`. */
function rowSequence(): string[] {
  return rows().map((row) => {
    const quote = row.querySelector('.rv-row-quote')?.textContent ?? null
    if (quote === null) return 'pending'
    return `${quote} · ${row.querySelector('.rv-row-verdict')?.textContent ?? ''}`
  })
}

function findButton(text: string): HTMLButtonElement | null {
  const buttons = [...view().querySelectorAll<HTMLButtonElement>('button')]
  return buttons.find((b) => (b.textContent ?? '').trim() === text) ?? null
}

function requireButton(text: string): HTMLButtonElement {
  const button = findButton(text)
  if (button === null) {
    throw new Error(`no button labelled "${text}"; saw ${JSON.stringify(buttonLabels())}`)
  }
  return button
}

function buttonLabels(): string[] {
  return [...view().querySelectorAll<HTMLButtonElement>('button')].map((b) =>
    (b.textContent ?? '').trim(),
  )
}

async function click(button: HTMLButtonElement): Promise<void> {
  await act(async () => {
    button.dispatchEvent(new MouseEvent('click', { bubbles: true }))
  })
  await flush()
}

/* -------------------------------------------------------------------------- */
/* Playback                                                                    */
/* -------------------------------------------------------------------------- */

const BASE: StatePatch = {
  url: ARTICLE_URL,
  title: ARTICLE_TITLE,
  claimCount: CLAIMS.length,
  claimIds: [...ARTICLE_ORDER],
}

/**
 * Run one whole check through the popup and return the row sequence after every
 * event — `claims_found`, each `claim`, then `done`.
 */
async function playback(order: readonly string[]): Promise<string[][]> {
  stubChrome()
  await mount()

  const frames: string[][] = []
  await push(jobState({ ...BASE, status: 'checking' }))
  frames.push(rowSequence())

  const arrived: Claim[] = []
  for (const id of order) {
    arrived.push(byId(id))
    await push(jobState({ ...BASE, status: 'checking', claims: [...arrived] }))
    frames.push(rowSequence())
  }

  await push(jobState({ ...BASE, status: 'done', claims: [...arrived], counts: COUNTS }))
  frames.push(rowSequence())
  return frames
}

afterEach(async () => {
  await unmount()
  vi.unstubAllGlobals()
})

/* -------------------------------------------------------------------------- */
/* 1. Article order                                                            */
/* -------------------------------------------------------------------------- */

describe('claim rows', () => {
  it('renders the same rows whether claims arrive scattered (live) or in order (cached)', async () => {
    const live = await playback(RESOLVE_ORDER)
    const cached = await playback(ARTICLE_ORDER)

    const expected = ARTICLE_ORDER.map(label)
    expect(live[live.length - 1]).toEqual(expected)
    expect(cached[cached.length - 1]).toEqual(expected)
    // The whole point of decision 15: one article, one rendering.
    expect(live[live.length - 1]).toEqual(cached[cached.length - 1])
  })

  it('allocates every row up front, so the fill is scattered and always partial', async () => {
    const live = await playback(RESOLVE_ORDER)

    // Six rows from the moment claims_found lands, before any claim arrives.
    for (const frame of live) expect(frame).toHaveLength(CLAIMS.length)
    expect(live[0]).toEqual(Array<string>(CLAIMS.length).fill('pending'))

    // c3 resolves first and lands in row 3, not row 1.
    expect(live[1]).toEqual([
      'pending',
      'pending',
      label('c3'),
      'pending',
      'pending',
      'pending',
    ])
    // c1 next, into row 1 — above a row that is already filled.
    expect(live[2][0]).toBe(label('c1'))
    expect(live[2][2]).toBe(label('c3'))
  })

  it('never rewrites a row once it has resolved', async () => {
    const live = await playback(RESOLVE_ORDER)

    for (let frame = 1; frame < live.length; frame += 1) {
      for (let row = 0; row < live[frame].length; row += 1) {
        if (live[frame - 1][row] !== 'pending') {
          expect(live[frame][row]).toBe(live[frame - 1][row])
        }
      }
    }
  })

  it('numbers a row by its position in the article, not by when it resolved', async () => {
    stubChrome()
    await mount()
    await push(jobState({ ...BASE, status: 'checking', claims: [byId('c3')] }))

    const [first, , third] = rows()
    expect(first.textContent).toContain('Claim 1 of 6')
    // The first claim to arrive is the third claim in the article.
    expect(third.textContent).toContain('Claim 3 of 6')
    expect(third.querySelector('.rv-row-quote')?.textContent).toContain(byId('c3').quote)
  })

  it('ignores a claim whose id was never announced rather than growing a stray row', async () => {
    stubChrome()
    await mount()
    const stray = claim('c99', 2000, 'a claim from another job', 'supported', 'low')
    await push(jobState({ ...BASE, status: 'checking', claims: [byId('c3'), stray] }))

    expect(rows()).toHaveLength(CLAIMS.length)
    expect(rowSequence().filter((row) => row !== 'pending')).toEqual([label('c3')])
    expect(view().textContent).not.toContain('a claim from another job')
  })

  it('falls back to article (start) order when the worker announced no ids', async () => {
    stubChrome()
    await mount()
    // An older backend or an older persisted state: count but no claim_ids.
    await push(
      jobState({
        url: ARTICLE_URL,
        title: ARTICLE_TITLE,
        claimCount: CLAIMS.length,
        status: 'done',
        claims: RESOLVE_ORDER.map(byId),
        counts: COUNTS,
      }),
    )

    expect(rowSequence()).toEqual(ARTICLE_ORDER.map(label))
  })

  it('falls back too when the announced ids are shorter than the count', async () => {
    stubChrome()
    await mount()
    await push(
      jobState({
        url: ARTICLE_URL,
        title: ARTICLE_TITLE,
        claimCount: CLAIMS.length,
        claimIds: ['c1', 'c2'],
        status: 'checking',
        claims: [byId('c3')],
      }),
    )

    // Still the full six rows the backend promised, never a truncated list.
    expect(rows()).toHaveLength(CLAIMS.length)
    expect(rowSequence().filter((row) => row !== 'pending')).toEqual([label('c3')])
  })
})

/* -------------------------------------------------------------------------- */
/* 2. Row appearance                                                           */
/* -------------------------------------------------------------------------- */

describe('row appearance', () => {
  it('draws a skeleton while a claim is pending', async () => {
    stubChrome()
    await mount()
    await push(jobState({ ...BASE, status: 'checking' }))

    const [first] = rows()
    expect(first.getAttribute('aria-busy')).toBe('true')
    expect(first.querySelector('.rv-row-skeleton')).not.toBeNull()
    expect(first.querySelector('.rv-row-ring')).not.toBeNull()
    expect(first.querySelector('svg')).toBeNull()
    expect(first.textContent).toContain('Claim 1 of 6, still checking.')
  })

  it('pairs the verdict icon with its text label and accessible name — never colour alone', async () => {
    stubChrome()
    await mount()
    await push(
      jobState({ ...BASE, status: 'done', claims: [...CLAIMS], counts: COUNTS }),
    )

    for (const [index, expected] of CLAIMS.entries()) {
      const row = rows()[index]
      const icon = row.querySelector('svg[role="img"]')
      const name = VERDICT_LABELS[expected.verdict]

      expect(icon).not.toBeNull()
      expect(icon?.getAttribute('aria-label')).toBe(name)
      expect(row.querySelector('.rv-row-verdict')?.textContent).toBe(name)
      expect(row.className).toContain(`rv-v-${expected.verdict}`)
      expect(row.getAttribute('aria-busy')).toBeNull()
    }
  })

  it('uses only the four canonical verdict names', async () => {
    stubChrome()
    await mount()
    await push(jobState({ ...BASE, status: 'done', claims: [...CLAIMS], counts: COUNTS }))

    const text = view().textContent ?? ''
    for (const name of Object.values(VERDICT_LABELS)) expect(text).toContain(name)
    expect(text).not.toMatch(/\b(TRUE|FALSE)\b/)
    expect(text).not.toMatch(/flagged/i)
    expect(text).not.toMatch(/\bSieve\b/)
  })
})

/* -------------------------------------------------------------------------- */
/* 3. The done state is not a dead end                                         */
/* -------------------------------------------------------------------------- */

describe('done state', () => {
  const done = (): JobState =>
    jobState({ ...BASE, status: 'done', claims: [...CLAIMS], counts: COUNTS })

  it('offers a control that starts another check', async () => {
    stubChrome({ tab: { title: 'A different article', url: 'https://news.example.com/other' } })
    await mount()
    await push(done())

    // Different article in the tab, so the button describes what it will do.
    const button = requireButton('Check this article')
    expect(button.disabled).toBe(false)

    await click(button)

    expect(sent).toContainEqual({ type: 'START_CHECK' })
    // …and the popup actually leaves the done state rather than sitting there.
    expect(view().querySelector('.rv-steps')).not.toBeNull()
    expect(view().querySelector('.rv-summary')).toBeNull()
  })

  it('says "Check again" when the tab still shows the article that was checked', async () => {
    stubChrome({ tab: { title: ARTICLE_TITLE, url: `${ARTICLE_URL}#comments` } })
    await mount()
    await push(done())

    expect(findButton('Check again')).not.toBeNull()
    expect(findButton('Check this article')).toBeNull()
  })

  it('names the article the result belongs to', async () => {
    stubChrome({ tab: { title: 'A different article', url: 'https://news.example.com/other' } })
    await mount()
    await push(done())

    expect(view().querySelector('.rv-article-title')?.textContent).toBe(ARTICLE_TITLE)
    expect(view().querySelector('.rv-article-meta')?.textContent).toBe('news.example.com')
    expect(view().querySelector('.rv-article-caption')?.textContent).toBe('Checked article')
  })

  it('draws no stepper — a finished check has no step in progress', async () => {
    stubChrome()
    await mount()
    await push(done())

    expect(view().querySelector('.rv-steps')).toBeNull()
    expect(view().querySelector('.rv-summary')).not.toBeNull()
  })

  it('names the article being checked while the check is running', async () => {
    stubChrome()
    await mount()
    await push(jobState({ ...BASE, status: 'checking' }))

    expect(view().querySelector('.rv-article-title')?.textContent).toBe(ARTICLE_TITLE)
    expect(view().querySelector('.rv-steps')).not.toBeNull()
  })
})

/* -------------------------------------------------------------------------- */
/* 4. Stale replies                                                            */
/* -------------------------------------------------------------------------- */

describe('stale replies', () => {
  it('ignores a START_CHECK reply that a newer pushed state has overtaken', async () => {
    const start = deferred<unknown>()
    stubChrome({
      reply: (message) => {
        const isStart = typeof message === 'object' && message !== null && 'type' in message && message.type === 'START_CHECK'
        return isStart ? start.promise : new Promise<unknown>(() => undefined)
      },
    })
    await mount()
    await push(jobState({ status: 'idle', url: ARTICLE_URL, title: ARTICLE_TITLE }))

    await click(requireButton('Check this article'))

    // The worker gets on with it and three claims land while the reply to the
    // click — which describes the moment of the click — is still in flight.
    await push(jobState({ ...BASE, status: 'checking', claims: ['c3', 'c1', 'c6'].map(byId) }))
    expect(rowSequence().filter((row) => row !== 'pending')).toHaveLength(3)

    await act(async () => {
      start.resolve({ type: 'STATE', state: jobState({ status: 'extracting' }) })
    })
    await flush()

    // Applying it would have rewound three resolved rows back to skeletons.
    expect(rowSequence().filter((row) => row !== 'pending')).toHaveLength(3)
  })

  it('ignores a GET_STATE reply that a pushed state has already overtaken', async () => {
    const seed = deferred<unknown>()
    stubChrome({ reply: () => seed.promise })
    await mount()

    await push(jobState({ ...BASE, status: 'done', claims: [...CLAIMS], counts: COUNTS }))
    expect(view().querySelector('.rv-summary')).not.toBeNull()

    // The seed reply was queued before the popup had any state; it describes an
    // idle worker and must not drag the finished result back to the ready screen.
    await act(async () => {
      seed.resolve({ type: 'STATE', state: INITIAL_JOB_STATE })
    })
    await flush()

    expect(view().querySelector('.rv-summary')).not.toBeNull()
    expect(view().querySelector('.rv-privacy')).toBeNull()
    expect(rowSequence()).toEqual(ARTICLE_ORDER.map(label))
  })

  it('still applies a reply that nothing has overtaken', async () => {
    stubChrome({
      reply: async () =>
        ({ type: 'STATE', state: jobState({ ...BASE, status: 'done', claims: [...CLAIMS], counts: COUNTS }) }),
    })
    await mount()

    // Nothing was pushed, so the seeded GET_STATE reply is the freshest thing
    // the popup has — the guard must not swallow it.
    expect(view().querySelector('.rv-summary')).not.toBeNull()
    expect(rowSequence()).toEqual(ARTICLE_ORDER.map(label))
  })
})
