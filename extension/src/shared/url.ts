/**
 * The one rule for "are these two URLs the same article?".
 *
 * Two places ask that question, and they must never answer it differently:
 *
 *  - the service worker's `dropStaleResult`, which THROWS AWAY a finished
 *    result when the popup opens over a different article;
 *  - the popup's done state, which only decides whether the button reads
 *    "Check again" or "Check this article".
 *
 * They used to carry a copy each, and the copies had drifted: the worker
 * compared `pathname` verbatim while the popup stripped trailing slashes first.
 * So a stored `https://site.com/story/` against a tab showing
 * `https://site.com/story` made the worker reset a completed check to idle
 * while the popup would have called it the same page — the reader lost a
 * finished result to a trailing slash. One module, imported by both, is what
 * makes that class of divergence impossible rather than merely fixed.
 *
 * ## The canonical form, and why each part of it
 *
 * `protocol//host` + `pathname` (trailing slashes stripped) + `search`.
 *
 *  - **Fragment dropped.** `#comments` is a position on a page, not a
 *    different page. It also genuinely differs between the two sources: the
 *    stored URL comes from the content script's `document.URL`, the compared
 *    one from `chrome.tabs`, and they need not agree about the hash.
 *  - **Trailing slash stripped.** `/story` and `/story/` are one article on
 *    every news site that serves both; canonical tags, share links and
 *    redirects hand out the two forms interchangeably. `/a//b` is left alone —
 *    only the trailing run is collapsed, because interior slashes can be
 *    meaningful path segments.
 *  - **Query string KEPT, verbatim.** This is the deliberate one. Dropping it
 *    would be wrong: news sites routinely put the article's identity in the
 *    query (`?id=12345`, `?page=2`, and CMSes that serve every story from one
 *    path). Treating `?id=1` and `?id=2` as the same article would let the
 *    worker show one story's verdicts over another's — the exact failure this
 *    check exists to prevent. Kept verbatim also means key order matters
 *    (`?a=1&b=2` != `?b=2&a=1`) and tracking parameters (`utm_*`) count: that
 *    is the safe direction to err, since the cost is re-running a check the
 *    reader asked for, not showing them the wrong article's result.
 *  - **`protocol//host` rather than `.origin`.** Identical for the http(s)
 *    URLs this ever sees, but `origin` collapses opaque-origin schemes to the
 *    literal string `"null"`, which would make two unrelated URLs compare
 *    equal. Nothing should reach here with such a scheme; not depending on it
 *    is free.
 *
 * Case is left alone. `URL` already lowercases the host for us, and path case
 * is significant on plenty of servers.
 */

/**
 * The comparable form of `raw`, or `null` when it is not a URL at all.
 *
 * Callers that just want to know whether two URLs match should use
 * `sameArticle`; this is exported for the tests that pin the canonical form and
 * for anything that needs a stable key.
 */
export function canonicalUrl(raw: string): string | null {
  let url: URL
  try {
    url = new URL(raw)
  } catch {
    return null
  }
  // Only the trailing run of slashes goes; interior ones are path segments.
  const path = url.pathname.replace(/\/+$/, '')
  return `${url.protocol}//${url.host}${path}${url.search}`
}

/**
 * Whether two URLs name the same article.
 *
 * Anything unparseable falls back to an exact string comparison, so two
 * identical non-URL strings still count as the same page rather than as two
 * different unknowns.
 */
export function sameArticle(a: string, b: string): boolean {
  const left = canonicalUrl(a)
  const right = canonicalUrl(b)
  if (left === null || right === null) return a === b
  return left === right
}
