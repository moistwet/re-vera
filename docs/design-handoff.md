> **Re-Vera note (2026-08-31):** This is the design handoff for the prototype, preserved verbatim below. The prototype was named "Sieve"; the product is **Re-Vera** on every surface. Where this file conflicts with `CLAUDE.md` or `docs/decisions.md`, those win. In particular: the reliability-score banner is deferred (do not build it yet), the page bar never says "flagged", confidence is hidden (null) for unverifiable claims, and the reader counter / thumbs feedback / "Report a mistake" / game streak are cut.

---

# Handoff: Sieve — fact-checking browser extension (live demo)

## Overview
Sieve is a Chrome extension that, only when the reader asks, checks the factual claims in the news article they're reading, highlights them in place, and shows the evidence. This handoff covers the interactive demo built so far: the toolbar popup (ready / checking / done states), in-page highlights, the claim card popover, the full-report side panel, game mode ("Guess first"), a page summary bar, and an injected article reliability score ring.

## About the Design Files
`Sieve Live Demo.dc.html` is a **design reference created in HTML** — a working prototype showing intended look and behavior, not production code to copy directly. The task is to **recreate this design as a real Chrome extension (Manifest V3)**: popup page, content script rendering the overlay in a Shadow DOM, and `chrome.sidePanel` for the full report. The demo simulates all three surfaces inside one fake browser frame; in production each surface is its own context. If patterns/libraries already exist in the target codebase, use those.

## Fidelity
**High-fidelity.** Colors, type, spacing, radii, copy, and interaction timing are final intent. Recreate pixel-perfectly, with one caveat: the demo's fake browser chrome (tab bar, URL field, Yahoo-style article) is scaffolding — do not build it.

## Core product rules (bind implementation)
- The tool never says TRUE/FALSE. Only four verdicts: Supported, Contradicted, Missing context, Unverifiable. Names identical across all surfaces; sentence case, never all-caps.
- Never encode a verdict by colour alone — icon + label every time.
- Confidence is a three-dot meter (low/medium/high), never a percentage.
- Highlights must never reflow the host article's text (visual only). Exception the user chose deliberately: the reliability-score banner IS injected above the headline and does shift content down.
- Overlay renders in a Shadow DOM, uses the **system UI font stack only** (never load webfonts into host pages). Popup/side panel may use webfonts (Cabin + IBM Plex Sans).
- Honour `prefers-reduced-motion` (demo disables all animation via media query).

## Design tokens

### Base (light theme)
- surface `#F7F9F8` · card `#FFFFFF` · ink `#1C2523` · muted `#5B6663` · border `#E2E8E5` · soft bg `#F0F4F2`
- primary button: bg ink `#1C2523`, fg `#FFFFFF`

### Dark theme (Sieve UI only; host page stays as-is)
- surface `#232B29` · card `#2A3330` · ink `#EDF2F0` · muted `#9BA8A3` · border `#3A4441` · soft `#1E2624`
- primary button: bg `#EDF2F0`, fg `#1C2523`
- Implemented as CSS variables (`--s-surface`, `--s-card`, `--s-ink`, `--s-mut`, `--s-brd`, `--s-soft`, `--s-btn-bg`, `--s-btn-fg`) swapped at a wrapper.

### Verdict roles
| Verdict | Base | Pill/tint bg (light) | Text (light, AA on white) | Pill bg (dark) | Text (dark) | Icon |
|---|---|---|---|---|---|---|
| Supported | `#12766B` | `#E3F1EE` | `#0D5A51` | `#16443D` | `#8AD4C7` | check in circle |
| Contradicted | `#C24A32` | `#FAE9E4` | `#A03A26` | `#4A241C` | `#F2A490` | x in octagon |
| Missing context | `#A16207` | `#FAF1DC` | `#7C4D08` | `#423414` | `#E7BE66` | half-filled info circle |
| Unverifiable | `#64748B` | `#ECEFF3` | `#475569` | `#333B47` | `#AEBBCB` | question in circle |

Icons are 16×16 stroke SVGs (stroke-width 1.5, round caps/joins), `currentColor`. Exact paths are in the demo file.

### Highlight marks (on host page, light-tuned)
`background: <tint>; box-shadow: inset 0 -<w> 0 <underline>; border-radius: 2px; cursor: pointer`
- Supported: tint `rgba(18,118,107,.10)`, underline `#12766B`, 1.5px
- Contradicted: tint `rgba(194,74,50,.13)`, underline `#C24A32`, **2px (heavier)**
- Missing context: tint `rgba(161,98,7,.12)`, underline `#A16207`, 1.5px
- Unverifiable: tint `rgba(100,116,139,.10)`, underline `#64748B`, 1.5px
- Settle animation on arrival: from `background rgba(28,37,35,.16)` to final, 0.5s ease-out, one claim at a time (~850 ms apart)
- Game-mode outline: `outline: 2px dotted #9BA8A3; outline-offset: 2px`; marked: dotted `#1C2523` + `rgba(28,37,35,.08)` fill
- Reveal flip: `rotateX(70deg)→0`, 0.4s ease-out

### Type
- Headings: Cabin 600/700 (popup title 16, panel title 15, scoreboard 19)
- Body: IBM Plex Sans (popup body 12.5–16); overlay: system-ui stack, 12.5–14
- Verdict labels: 600, 13px, never all-caps

### Spacing & radii
- Spacing scale 4/8/12/16/24/32; radii: 6 controls, 10 cards/buttons, 12 popup/card/panel corners, 999 pills/chips

## Screens / Views

### 1. Toolbar popup (400px wide; design box 400×560)
Header: 26×26 ink-rounded logo tile (funnel/sieve glyph) + "Sieve" (Cabin 700 16) + close.
- **State B — Ready:** article title card (soft bg, border, radius 10, title 600 14.5, meta 12.5 muted) → primary **Check this article** (radius 10, padding 14, 600 16) → secondary **Guess first** (1.5px border, transparent) → privacy line 12.5 muted: "Article text is sent to Sieve to check it. Nothing is stored with your identity."
- **State C — Checking (streaming):** 3-step stepper: "Reading article" → "Found 6 claims" → "Checking N of 6". Done step: 16px filled circle w/ ✓; active: 2px ink ring pulsing (1.2s); pending: 1.5px border circle, muted label. Below, 6 claim rows (min-height 34, bottom hairline): pending = pulsing 15px ring + skeleton bar (9px, soft bg, max-width 220); resolved = verdict icon + one-line quoted snippet, ellipsized. Rows resolve in order 3,1,6,4,2,5 (~850 ms apart). Must always look partial.
- **State D — Done:** summary line "6 claims · 2 supported · 2 contradicted · 1 missing context · 1 unverifiable" (counts coloured w/ verdict text colours); buttons **Hide/Show on page** (secondary toggle) + **Open full report** (primary); footer "Checked 2 min ago · 340 readers have checked this article".
- Toolbar icon gets a coral badge with claim count when done.
- States A (not an article), E (error), F (daily limit) are specced in the brief but not in this demo.

### 2. In-page overlay
- **Highlights:** wrap the exact claim substrings (six claims, sample content below). Click opens the claim card anchored under the clicked span: left-aligned to the span, clamped ≥12px from edges (flip logic: `left = min(spanLeft, containerWidth − 372)`), 10px below span bottom.
- **Claim card** (360px wide, radius 12, padding 14×16, shadow `0 12px 40px rgba(20,30,28,.22)`, entrance fade+6px rise 0.2s): verdict pill (tint bg, 999 radius, icon+label) + confidence dots (7px, filled = verdict text colour, empty = 1.5px border) + "High/Medium/Low confidence" + close ✕ → quoted claim (italic 14, 2px left border) → "WHAT THE EVIDENCE SAYS" (600 11 caps muted) + 1–2 plain sentences naming sources → **provenance trail** → source chips → footer: "Helpful?" 👍👎 · "Report a mistake" (underlined muted) · **Full report →** (teal 600, opens side panel expanded to this claim).
- **Provenance trail (signature element):** soft-bg rounded box; 2–3 nodes, each = 7px ink dot + node title (600 13) + muted note; nodes joined by 1.5px×12px vertical border-colour line. E.g. claim 1: "This article — wire copy, republished on Yahoo" → "Independent reports — CNA · Reuters" → "Original source — gov.sg press release, 12 Mar".
- **Source chips:** pill border chip: 16px circle favicon placeholder (initial letter, soft bg) + outlet + "· date" muted + optional "wire" mini-tag (10px, bordered) + **Read** (teal 600).
- **Page summary bar:** 42px, ink `#1C2523`, white text 13; sieve glyph + state label ("Checking…"/"Checked"/"Guess mode") 600 nowrap + counts (ellipsized, done state uses compact "6 claims · 2 supported · 2 contradicted · 2 flagged") + **Full report** ghost button (nowrap) + ✕. Collapses at scrollTop > 80 into a centered pill (`999 radius, ink bg`, glyph + "6 claims"); click re-expands and pins until scrolled back up. In game mode the bar shows **Reveal** (white bg button) instead.
- **Reliability score banner (injected above headline):** soft-bg bordered card (radius 12, padding 14×18, fade-in 0.45s): 68×68 ring — track `#E7ECEA` 6px, progress arc stroke-dasharray 163.4 (r=26) rotated −90°, round cap — with the score number HTML-overlaid centered (700 17). Score animates 0→50 over 1.4s cubic ease-out (rAF), arc and number in sync. Colour by tier: <40 `#C24A32`, 40–69 `#A16207`, ≥70 `#12766B`. Right column: sieve glyph + "SIEVE RELIABILITY SCORE" (600 11 caps, nowrap) → tier line 600 16 ("Largely contradicted" / "Mixed — read with care" / "Mostly supported") → explainer 12.5: "Based on 5 checkable claims: 2 supported, 2 contradicted, 1 missing context. 1 claim couldn't be verified and isn't counted." Scoring: supported = 1, missing context = 0.5, contradicted = 0; unverifiable excluded; score = points/checkable × 100 (sample: 2.5/5 = 50).

### 3. Side panel (360px, full height, slides in from right 0.25s)
Header (border-bottom): "Full report" (Cabin 700 15) + ✕ → article title 600 13.5 → "Yahoo News · checked 2 min ago" 12 muted → counts line 12.5 muted.
Filter chips row: All / Supported / Contradicted / Missing context / Unverifiable — 999-radius bordered chips, active = ink bg + white text, nowrap.
Claim rows (hairline separated, clickable): verdict icon + quoted claim (500 13) + top source line (11.5 muted, or "No independent source found") + ▼/▲ chevron. Expanded: evidence sentence → compact provenance trail → compact source chips (indented 42px left).
"ABOUT THIS SOURCE" block: "Yahoo News · This article is wire copy republished on Yahoo. The original wire service is not named on the page."
Footer: "How Sieve checks claims" (teal 600) · "Report a mistake" (muted underline).

### 4. Game mode — "Guess first"
Popup B → Guess first closes popup, every claim gets the dotted outline, sticky bottom instruction card (ink bg, radius 12): "Tap the sentences you think won't hold up. Then we'll show what we found." + "N marked" counter chip. Tap toggles marked. **Reveal** (page bar) → all highlights flip in with verdicts + modal scrim `rgba(20,28,26,.35)` with scoreboard card (340px, flip-in 0.35s): "You spotted X of 3 shaky claims" (shaky = 2 contradicted + 1 missing context) → "Streak: 4 articles" → tip box: "Big percentages without a named source are worth a second look." → **See what we found** (primary) → normal done state + score ring animates. No leaderboards, timers, or points.

## Interactions & Behavior summary
- Check flow: popup B → C (stepper: step 1 done at 700 ms, step 2 at 1400 ms; claims resolve from 2100 ms, 850 ms apart) → D at ~6.9 s; highlights land on page as each resolves.
- Toasts (ink pill, bottom-center, 2.2 s): "Thanks for the feedback" (thumbs), "Thanks — we'll take a look" (report a mistake).
- Scroll closes any open claim card.
- Keyboard path (specced, not in demo): Tab through claims, Enter opens card, Esc closes; SR label "Claim 3 of 6, contradicted".

## State Management
`theme (light|dark) · popupOpen · panelOpen · mode (idle|guess|checking|reveal|done) · step (0–2) · resolved[] · marked[] · hlVisible · openId + card anchor coords · barClosed/collapsed/pinned · filter · expandedId · ringPct · toast · scoreOpen`. In production: check results cached per-URL (brief: "Checked earlier today by 340 readers"); no accounts, anonymous install ID; daily limit 20 checks.

## Sample content
Fictional, layout only — replace before any public demo. Article "Hawker stall rents to rise 40% next year, vendors say" (wire copy on Yahoo) with 6 claims; full claim/verdict/evidence/source table is in the design brief (§9) and encoded in the demo's `DATA` array.

## Assets
No binary assets. All icons are inline SVGs in the demo file (sieve/funnel logo glyph, four verdict icons, padlock). Fonts from Google Fonts: Cabin (500–700), IBM Plex Sans (400–600) — popup/panel only.

## Files
- `Sieve Live Demo.dc.html` — the full prototype (committed here as `sieve-live-demo.dc.html`; markup template + logic class with all timings, styles, and data)
