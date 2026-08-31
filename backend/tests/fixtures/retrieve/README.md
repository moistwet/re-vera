# Recorded retrieval answers

Hand-written stand-ins for what stage 2's four providers would receive from
Google Fact Check Tools, the OpenAI Responses API (web search), data.gov.sg and
an ordinary web page.

**Everything here is fictional.** The article, the claims, the outlets, the
ratings, the datasets and every URL are invented for Re-Vera's tests, and every
URL points at `example.com` or an `example` TLD so that nothing here can
resolve to, or be mistaken for, real reporting. The outlet names that read like
real ones exist only so the fixtures read realistically; none of these
organisations published any of this.

**They are not captures.** This repository has no `OPENAI_API_KEY`, no
`GOOGLE_FACTCHECK_API_KEY` and no route to either service, so no live call was
ever made. Each file is what a *plausible* answer looks like, given the wire
shape assumed in the provider's module docstring — which is the only place that
assumption is written down, and the first thing to check when the real API
disagrees. A fixture passing proves the provider parses this shape, not that the
service returns it.

## Format

JSON files are loaded by `app.pipeline.providers.base.load_recorded_http`:

```jsonc
{
  "_note":       "which test replays this, and what it stands for",
  "status_code": 200,                    // optional, defaults to 200
  "url":         "https://example.com/", // optional final URL after redirects
  "json":        { "…": "…" },           // the body, written readably
  "text":        "<html>…</html>"        // …or the exact bytes; `json` wins
}
```

`.html` files are read directly by the tests that need a page rather than an
API payload; they are wrapped in an `HttpResponse` there, headers and all.

| File | Stands for |
| --- | --- |
| `factcheck_hit.json` | A ClaimReview search that found a review — the case that short-circuits web search. |
| `factcheck_empty.json` | A ClaimReview search that found nothing. The normal case. |
| `factcheck_not_json.json` | A 200 carrying an HTML error page, as proxies and captive portals send. |
| `websearch_results.json` | A Responses answer with the `web_search` tool run and three cited results. |
| `official_datasets.json` | A data.gov.sg catalogue search with two datasets, one of which names no URL. |
| `cited_article.html` | A news page whose links include navigation, a share button and one real citation. |
| `cited_source.html` | The press release that page cites, with `og:site_name` and a `datePublished`. |
