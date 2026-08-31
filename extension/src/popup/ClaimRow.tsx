/**
 * One row of the popup's claim list.
 *
 * A row is either still being checked — a pulsing 15px ring plus a 9px skeleton
 * bar — or resolved, in which case it shows the verdict icon, the quoted
 * sentence ellipsized to one line, and the verdict's display name. Claims
 * resolve in whatever order the backend finishes them, so any mix of the two is
 * a state the list must be able to draw.
 *
 * The visible verdict label is not decoration: CLAUDE.md rule 4 forbids
 * encoding a verdict by colour alone, so icon *and* name ship together. The
 * icon already carries the name as its `aria-label`, so the visible copy is
 * `aria-hidden` and a screen reader hears it once.
 */

import type { ReactElement } from 'react'

import type { Claim } from '../types/schema'
import { VERDICT_LABELS, VerdictIcon, verdictClass } from './verdictIcons'

export interface ClaimRowProps {
  /** 1-based position in the list, for the screen-reader "Claim 3 of 6". */
  index: number
  /** How many claims this check covers. */
  total: number
  /** The resolved claim, or null while this row is still waiting. */
  claim: Claim | null
}

export default function ClaimRow({ index, total, claim }: ClaimRowProps): ReactElement {
  if (claim === null) {
    return (
      <li className="rv-row" aria-busy="true">
        <span className="rv-row-ring" aria-hidden="true" />
        <span className="rv-row-skeleton" aria-hidden="true" />
        <span className="rv-sr">
          Claim {index} of {total}, still checking.
        </span>
      </li>
    )
  }

  return (
    <li className={`rv-row ${verdictClass(claim.verdict)}`}>
      <span className="rv-sr">
        Claim {index} of {total}.
      </span>
      <VerdictIcon verdict={claim.verdict} size={15} className="rv-row-icon" />
      <span className="rv-row-quote">{`“${claim.quote}”`}</span>
      <span className="rv-row-verdict" aria-hidden="true">
        {VERDICT_LABELS[claim.verdict]}
      </span>
    </li>
  )
}
