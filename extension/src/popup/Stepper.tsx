/**
 * The three-step progress stepper of the checking state.
 *
 * "Reading article" → "Found N claims" → "Checking N of M", exactly as in
 * docs/design-handoff.md § 1: the finished step is a filled ink circle with a
 * check, the current step is a 2px ink ring pulsing at 1.2s, and steps still to
 * come are a 1.5px bordered circle with a muted label.
 *
 * The step is derived from `JobState` alone, never from a timer, so a popup
 * reopened halfway through a check draws the same stepper the one that was
 * closed would have drawn.
 */

import type { ReactElement } from 'react'

import type { JobStatus } from '../shared/messages'

export interface StepperProps {
  status: JobStatus
  /** How many claims the backend said it would check; null until `claims_found`. */
  claimCount: number | null
  /** How many claim events have arrived so far. */
  resolvedCount: number
}

type StepState = 'done' | 'active' | 'pending'

/**
 * Index of the step in progress; 3 once every step is behind us.
 *
 * `extracting` is the article being read. `checking` with no claim count yet
 * means the request is in but the backend has not said how many claims it
 * found. A claim count is the signal that the last step has started — on a
 * cache hit it arrives with the POST response, so the stepper jumps straight
 * there and the reader never sees a stall.
 */
function currentStep(status: JobStatus, claimCount: number | null): number {
  if (status === 'done') return 3
  if (status === 'checking') return claimCount === null ? 1 : 2
  return 0
}

function stepLabels(claimCount: number | null, resolvedCount: number): [string, string, string] {
  if (claimCount === null) {
    // Nothing is invented before the backend says it: no "Found 6 claims"
    // until there is a 6.
    return ['Reading article', 'Finding claims', 'Checking claims']
  }
  const noun = claimCount === 1 ? 'claim' : 'claims'
  const at = Math.min(resolvedCount + 1, claimCount)
  return ['Reading article', `Found ${claimCount} ${noun}`, `Checking ${at} of ${claimCount}`]
}

export default function Stepper({
  status,
  claimCount,
  resolvedCount,
}: StepperProps): ReactElement {
  const current = currentStep(status, claimCount)
  const labels = stepLabels(claimCount, resolvedCount)

  return (
    <ol className="rv-steps" aria-live="polite">
      {labels.map((label, index) => {
        const state: StepState = current > index ? 'done' : current === index ? 'active' : 'pending'
        return (
          <li
            key={index}
            className={`rv-step rv-step-${state}`}
            aria-current={state === 'active' ? 'step' : undefined}
          >
            <span className="rv-step-dot" aria-hidden="true">
              {state === 'done' ? '✓' : ''}
            </span>
            <span className="rv-step-label">{label}</span>
            {state === 'done' && <span className="rv-sr">— done</span>}
          </li>
        )
      })}
    </ol>
  )
}
