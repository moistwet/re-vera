/**
 * The done state's counts line:
 *
 *   6 claims · 2 supported · 2 contradicted · 1 missing context · 1 unverifiable
 *
 * with each count in its verdict's text colour, per docs/design-handoff.md § 1
 * state D. All four verdicts are always listed, in the canonical order and with
 * the canonical words — no "flagged", no lumping, no dropping a zero
 * (CLAUDE.md rule 1 and decision 3: the same four names on every surface).
 * Colour is only ever a second channel here; the name carries the meaning.
 */

import { Fragment, type ReactElement } from 'react'

import type { Counts } from '../types/schema'

interface Segment {
  key: keyof Counts
  /** Lower case: these words sit mid-sentence, after the count. */
  noun: string
}

const SEGMENTS: readonly Segment[] = [
  { key: 'supported', noun: 'supported' },
  { key: 'contradicted', noun: 'contradicted' },
  { key: 'missing_context', noun: 'missing context' },
  { key: 'unverifiable', noun: 'unverifiable' },
]

export interface SummaryProps {
  counts: Counts
}

export default function Summary({ counts }: SummaryProps): ReactElement {
  const total =
    counts.supported + counts.contradicted + counts.missing_context + counts.unverifiable

  return (
    <p className="rv-summary">
      <span>
        {total} {total === 1 ? 'claim' : 'claims'}
      </span>
      {SEGMENTS.map((segment) => (
        <Fragment key={segment.key}>
          <span aria-hidden="true"> · </span>
          <span className={`rv-count rv-v-${segment.key}`}>
            {counts[segment.key]} {segment.noun}
          </span>
        </Fragment>
      ))}
    </p>
  )
}
