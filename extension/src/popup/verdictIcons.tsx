/**
 * The four verdict icons, lifted path-for-path from docs/sieve-live-demo.dc.html.
 *
 * 16×16 stroke SVGs on a 0 0 16 16 viewBox, stroke-width 1.5 with round caps and
 * joins, drawn in `currentColor` so the caller sets the colour with a class
 * (`.rv-v-contradicted` and friends in theme.css) rather than a literal.
 *
 * Two rules bind everything in this file:
 *
 *  - Four verdicts, and only these display names: Supported, Contradicted,
 *    Missing context, Unverifiable. Sentence case, never all-caps, never
 *    TRUE/FALSE, never "flagged" (CLAUDE.md rule 1).
 *  - A verdict is never encoded by colour alone (rule 4). Each icon has a
 *    distinct silhouette — check in a circle, x in an octagon, half-filled info
 *    circle, question in a circle — and carries `role="img"` with the verdict
 *    name as its `aria-label`, so the shape carries it for sighted readers and
 *    the label carries it for everyone else. Callers pair it with the visible
 *    text label too.
 */

import type { ReactElement } from 'react'

import type { Verdict } from '../types/schema'

/** The only display names any Re-Vera surface may use for a verdict. */
export const VERDICT_LABELS: Record<Verdict, string> = {
  supported: 'Supported',
  contradicted: 'Contradicted',
  missing_context: 'Missing context',
  unverifiable: 'Unverifiable',
}

/** Maps a verdict onto the theme.css class that supplies its two colours. */
export function verdictClass(verdict: Verdict): string {
  return `rv-v-${verdict}`
}

const PATHS: Record<Verdict, ReactElement> = {
  supported: (
    <>
      <circle cx="8" cy="8" r="6.5" stroke="currentColor" strokeWidth="1.5" />
      <path
        d="M5.2 8.2l2 2 3.6-4"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </>
  ),
  contradicted: (
    <>
      <path
        d="M5.4 1.9h5.2l3.5 3.5v5.2l-3.5 3.5H5.4L1.9 10.6V5.4z"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinejoin="round"
      />
      <path d="M6 6l4 4M10 6l-4 4" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
    </>
  ),
  missing_context: (
    <>
      <circle cx="8" cy="8" r="6.5" stroke="currentColor" strokeWidth="1.5" />
      <path d="M8 1.5a6.5 6.5 0 000 13z" fill="currentColor" opacity=".25" />
      <path d="M8 7v3.4M8 5.2v.2" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
    </>
  ),
  unverifiable: (
    <>
      <circle cx="8" cy="8" r="6.5" stroke="currentColor" strokeWidth="1.5" />
      <path
        d="M6.4 6.2c0-1 .7-1.7 1.6-1.7s1.6.6 1.6 1.5c0 1.2-1.6 1.3-1.6 2.5"
        stroke="currentColor"
        strokeWidth="1.4"
        strokeLinecap="round"
      />
      <path d="M8 11v.2" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
    </>
  ),
}

export interface VerdictIconProps {
  verdict: Verdict
  /** Rendered size in px. 16 in the design; the popup's claim rows use 15. */
  size?: number
  className?: string
}

export function VerdictIcon({ verdict, size = 16, className }: VerdictIconProps): ReactElement {
  return (
    <svg
      role="img"
      aria-label={VERDICT_LABELS[verdict]}
      className={className}
      width={size}
      height={size}
      viewBox="0 0 16 16"
      fill="none"
    >
      {PATHS[verdict]}
    </svg>
  )
}

/**
 * The funnel mark — Re-Vera's logo glyph, kept from the prototype (the "Sieve"
 * name is dead, the glyph is not). Drawn on a 20×20 viewBox in `currentColor`.
 */
export function FunnelMark({ size = 14 }: { size?: number }): ReactElement {
  return (
    <svg width={size} height={size} viewBox="0 0 20 20" fill="none" aria-hidden="true">
      <path
        d="M3 4h14L12.5 10v5.2L7.5 17v-7z"
        stroke="currentColor"
        strokeWidth="1.7"
        strokeLinejoin="round"
      />
    </svg>
  )
}
