/**
 * Article extraction — the whole of Re-Vera's in-page footprint in milestone 1.
 *
 * Nothing here renders UI, injects styles, or mutates the live page. The
 * JSON-LD path only reads `<script>` text; Readability runs on a **clone** of
 * the document, because `Readability.parse()` rewrites the DOM it is given.
 *
 * Strategy, in order:
 *
 *  1. **JSON-LD.** Publishers that ship `schema.org` metadata give us the
 *     article body verbatim, without navigation, related-links rails or
 *     "read more" teasers mixed in. Preferred when `articleBody` is present
 *     and substantial.
 *  2. **Readability.** Everything else. We keep paragraph boundaries by
 *     walking the parsed content element block by block (see `blockText`)
 *     rather than taking `parse().textContent`, which is a raw
 *     `Node.textContent` and therefore glues the last word of one paragraph
 *     to the first word of the next.
 *
 * Whatever survives is whitespace-normalised: runs of spaces and tabs collapse
 * to one space, any run of newlines becomes a single `"\n\n"` paragraph break,
 * and the result is trimmed. Character offsets into this string are the
 * contract the backend's claim `start`/`end` refer to.
 */

import { Readability, isProbablyReaderable } from '@mozilla/readability'

import type { ExtractMessage, ExtractedArticle } from '../shared/messages'

/**
 * Anything shorter than this is a stub, a paywall teaser, a section front or a
 * cookie wall — i.e. the "not an article" signal, surfaced as `null`.
 */
const MIN_ARTICLE_CHARS = 500

/** `@type` values we accept as "this is the article on this page". */
const ARTICLE_LD_TYPES = new Set(['NewsArticle', 'Article', 'ReportageNewsArticle'])

/** How deep to chase nested arrays / `@graph` before giving up on a blob. */
const MAX_LD_DEPTH = 6

const NODE_TYPE_ELEMENT = 1
const NODE_TYPE_TEXT = 3

/** Subtrees whose text is never article prose. */
const SKIP_TAGS = new Set([
  'SCRIPT',
  'STYLE',
  'NOSCRIPT',
  'TEMPLATE',
  'IFRAME',
  'OBJECT',
  'SVG',
  'CANVAS',
  'BUTTON',
  'SELECT',
  'TEXTAREA',
])

/** Tags that end the current paragraph. Everything else is inline. */
const BLOCK_TAGS = new Set([
  'ADDRESS',
  'ARTICLE',
  'ASIDE',
  'BLOCKQUOTE',
  'DD',
  'DIV',
  'DL',
  'DT',
  'FIELDSET',
  'FIGCAPTION',
  'FIGURE',
  'FOOTER',
  'FORM',
  'H1',
  'H2',
  'H3',
  'H4',
  'H5',
  'H6',
  'HEADER',
  'HR',
  'LI',
  'MAIN',
  'NAV',
  'OL',
  'P',
  'PRE',
  'SECTION',
  'TABLE',
  'TD',
  'TH',
  'TR',
  'UL',
])

/* -------------------------------------------------------------------------- */
/* whitespace                                                                  */
/* -------------------------------------------------------------------------- */

/** Invisible characters CMSs sprinkle through copy; they only break quote matching. */
const ZERO_WIDTH = /[\u200B-\u200F\u2060\uFEFF]/g

/** Non-breaking and typographic spaces, folded to a plain space. */
const UNICODE_SPACES = /[\u00A0\u1680\u2000-\u200A\u202F\u205F\u3000]/g

/** Collapse a headline to a single line. */
function normaliseTitle(raw: string): string {
  return raw.replace(ZERO_WIDTH, '').replace(UNICODE_SPACES, ' ').replace(/\s+/g, ' ').trim()
}

/**
 * Collapse runs of spaces/tabs to one space and any run of newlines to a
 * single `"\n\n"` paragraph break. Curly quotes, em dashes and other
 * non-ASCII punctuation are left alone — claim quotes must stay exact
 * substrings of this text.
 */
function normaliseText(raw: string): string {
  return raw
    .replace(/\r\n?/g, '\n')
    .replace(ZERO_WIDTH, '')
    .replace(UNICODE_SPACES, ' ')
    .replace(/[^\S\n]+/g, ' ')
    .replace(/ *\n[ \n]*/g, '\n\n')
    .trim()
}

/* -------------------------------------------------------------------------- */
/* JSON-LD                                                                     */
/* -------------------------------------------------------------------------- */

/**
 * Parse one `application/ld+json` block. These are frequently malformed, or
 * wrapped in CDATA / HTML comments by a CMS, so every failure is just "no
 * metadata here" rather than an exception.
 */
function parseLdJson(source: string): unknown {
  const cleaned = source
    .trim()
    .replace(/^<!--/, '')
    .replace(/-->$/, '')
    .replace(/^(?:\/\/)?\s*<!\[CDATA\[/, '')
    .replace(/(?:\/\/)?\s*\]\]>$/, '')
    .trim()

  if (!cleaned) return null
  try {
    return JSON.parse(cleaned) as unknown
  } catch {
    return null
  }
}

/** Flatten arrays and `@graph` wrappers into a flat list of candidate objects. */
function collectLdObjects(value: unknown, out: Record<string, unknown>[], depth = 0): void {
  if (depth > MAX_LD_DEPTH || value === null || typeof value !== 'object') return

  if (Array.isArray(value)) {
    for (const item of value) collectLdObjects(item, out, depth + 1)
    return
  }

  const node = value as Record<string, unknown>
  out.push(node)
  if ('@graph' in node) collectLdObjects(node['@graph'], out, depth + 1)
}

/** `"http://schema.org/NewsArticle"` and `["NewsArticle", …]` both mean NewsArticle. */
function ldTypeNames(value: unknown): string[] {
  const raw = Array.isArray(value) ? value : [value]
  const names: string[] = []
  for (const entry of raw) {
    if (typeof entry !== 'string') continue
    names.push(entry.replace(/^https?:\/\/(?:www\.)?schema\.org\//i, '').trim())
  }
  return names
}

/** JSON-LD fields are routinely `"x"`, `["x"]` or `["x", "y"]`. Take the first string. */
function firstString(value: unknown): string | null {
  if (typeof value === 'string') return value
  if (Array.isArray(value)) {
    for (const item of value) {
      const found = firstString(item)
      if (found !== null) return found
    }
  }
  return null
}

function articleFromJsonLd(doc: Document): { title: string; text: string } | null {
  const nodes: Record<string, unknown>[] = []
  for (const script of Array.from(doc.querySelectorAll('script[type="application/ld+json"]'))) {
    collectLdObjects(parseLdJson(script.textContent ?? ''), nodes)
  }

  for (const node of nodes) {
    if (!ldTypeNames(node['@type']).some((name) => ARTICLE_LD_TYPES.has(name))) continue

    const body = firstString(node['articleBody'])
    if (body === null) continue

    const text = normaliseText(body)
    if (text.length < MIN_ARTICLE_CHARS) continue

    const headline = firstString(node['headline']) ?? firstString(node['name']) ?? ''
    return { title: normaliseTitle(headline), text }
  }

  return null
}

/* -------------------------------------------------------------------------- */
/* Readability                                                                 */
/* -------------------------------------------------------------------------- */

/**
 * Text of an element with paragraph boundaries preserved: inline elements
 * (`<a>`, `<em>`, …) contribute their text with no separator, block elements
 * end the current paragraph.
 */
function blockText(root: Node): string {
  const paragraphs: string[] = []
  let buffer = ''

  const flush = (): void => {
    const paragraph = buffer.replace(/\s+/g, ' ').trim()
    if (paragraph) paragraphs.push(paragraph)
    buffer = ''
  }

  const visit = (node: Node): void => {
    if (node.nodeType === NODE_TYPE_TEXT) {
      buffer += node.nodeValue ?? ''
      return
    }
    if (node.nodeType !== NODE_TYPE_ELEMENT) return

    const tag = (node as Element).tagName.toUpperCase()
    if (SKIP_TAGS.has(tag)) return
    if (tag === 'BR') {
      buffer += ' '
      return
    }

    const isBlock = BLOCK_TAGS.has(tag)
    if (isBlock) flush()
    for (const child of Array.from(node.childNodes)) visit(child)
    if (isBlock) flush()
  }

  visit(root)
  flush()
  return paragraphs.join('\n\n')
}

function articleFromReadability(doc: Document): { title: string; text: string } | null {
  let parsed: ReturnType<Readability<Element>['parse']>
  try {
    // The clone is the whole point: Readability rewrites the document it is
    // handed, and the reader's page must come out of this untouched.
    const clone = doc.cloneNode(true) as Document
    parsed = new Readability<Element>(clone, {
      // Identity serializer: we want the content element, not an HTML string,
      // so `blockText` can keep the paragraph boundaries.
      serializer: (node) => node as Element,
    }).parse()
  } catch (error) {
    console.error('[Re-Vera] Readability failed', error)
    return null
  }

  if (!parsed) return null

  const content = parsed.content
  const raw = content ? blockText(content) : (parsed.textContent ?? '')
  const text = normaliseText(raw)
  if (!text) return null

  return { title: normaliseTitle(parsed.title ?? ''), text }
}

/* -------------------------------------------------------------------------- */
/* public API                                                                  */
/* -------------------------------------------------------------------------- */

/** Best available title for the page, used when the extractor found none. */
function fallbackTitle(doc: Document): string {
  const ogTitle = doc.querySelector('meta[property="og:title"]')?.getAttribute('content')
  return normaliseTitle(doc.title || ogTitle || '')
}

/**
 * Extract the article on `doc`, or `null` when this is not an article — the
 * signal the popup turns into its not-an-article state.
 *
 * The live document is never modified.
 */
export function extractArticle(doc: Document = document): ExtractedArticle | null {
  const candidate = articleFromJsonLd(doc) ?? articleFromReadability(doc)
  if (!candidate || candidate.text.length < MIN_ARTICLE_CHARS) return null

  return {
    url: doc.URL,
    title: candidate.title || fallbackTitle(doc),
    text: candidate.text,
  }
}

/**
 * Cheap "is there an article here at all?" check — it does not parse the whole
 * document, so it is safe to call before the reader has asked for anything.
 */
export function isProbablyArticle(doc: Document = document): boolean {
  if (articleFromJsonLd(doc) !== null) return true
  try {
    return isProbablyReaderable(doc)
  } catch (error) {
    console.error('[Re-Vera] readerable check failed', error)
    return false
  }
}

/* -------------------------------------------------------------------------- */
/* message bridge                                                              */
/* -------------------------------------------------------------------------- */

/**
 * Guard flag on the isolated world's global. `chrome.scripting.executeScript`
 * re-runs the bundle every time the reader clicks, and the isolated world
 * survives between injections, so without this the page would accumulate one
 * listener per click and answer the same message several times.
 */
const BRIDGE_FLAG = '__reVeraExtractBridge__'

function isExtractMessage(message: unknown): message is ExtractMessage {
  return (
    typeof message === 'object' &&
    message !== null &&
    (message as { type?: unknown }).type === 'EXTRACT'
  )
}

function inContentScript(): boolean {
  return (
    typeof chrome !== 'undefined' &&
    typeof chrome.runtime?.onMessage?.addListener === 'function' &&
    typeof chrome.runtime.id === 'string'
  )
}

/**
 * Register the `{ type: 'EXTRACT' }` responder. Idempotent: returns `true` only
 * for the injection that actually installed it, `false` for every repeat and
 * outside a content-script context (unit tests, the popup bundle).
 */
export function installExtractBridge(): boolean {
  if (!inContentScript()) return false

  const scope = globalThis as unknown as Record<string, unknown>
  if (scope[BRIDGE_FLAG] === true) return false
  scope[BRIDGE_FLAG] = true

  chrome.runtime.onMessage.addListener((message: unknown, _sender, sendResponse) => {
    if (!isExtractMessage(message)) return false

    try {
      sendResponse(extractArticle())
    } catch (error) {
      console.error('[Re-Vera] article extraction failed', error)
      sendResponse(null)
    }
    // Keeps the message channel open; harmless now that we have replied, and
    // correct if extraction ever becomes asynchronous.
    return true
  })

  return true
}

// Self-install when this module is the injected bundle. `index.ts` is the
// nominal entry point, but the manifest/README convention injects
// `../content/extract?script&iife`, so either file works as the entry and the
// flag above keeps exactly one listener registered whichever is used. Inert in
// Node (unit tests) and anywhere `chrome.runtime` is absent.
installExtractBridge()
