# Re-Vera — Chrome extension

Manifest V3 extension (TypeScript · React · Vite · CRXJS). It checks the factual
claims in the article you are reading — **only when you click**, never in the
background.

## Quickstart

```sh
# Node 22 + pnpm 10 (never npm install)
pnpm install
cp .env.example .env        # VITE_API_BASE — .env is gitignored, never commit it
pnpm dev                    # writes dist/ and serves HMR on :5173
```

Then in Chrome:

1. Open `chrome://extensions`.
2. Turn on **Developer mode** (top right).
3. **Load unpacked** → pick `extension/dist`.
4. Start the backend (`uvicorn app.main:app --reload` in `backend/`), open a news
   article and click the Re-Vera toolbar icon.

`pnpm dev` keeps `dist/` up to date and reloads the extension on change. Leave it
running; re-run **Load unpacked** only if you delete `dist/`.

For a production bundle: `pnpm build` (writes `dist/`, no dev server needed).

## Scripts

| Script           | What it does                                                     |
| ---------------- | ---------------------------------------------------------------- |
| `pnpm dev`       | Vite dev server + CRXJS, writes `dist/` with HMR                  |
| `pnpm build`     | Production build into `dist/`                                     |
| `pnpm typecheck` | `tsc --noEmit` over `src/`, `tests/` and the config files         |
| `pnpm lint`      | `eslint .`                                                        |
| `pnpm test`      | `vitest run` over `tests/**/*.test.{ts,tsx}` (node; popup opts into jsdom) |
| `pnpm gen`       | Regenerates `src/types/schema.ts` from `../shared/schema.json`    |

Type-check and lint clean is part of "done" for every change.

## Environment

`VITE_API_BASE` is the only variable, and it is **required**. Copy
`.env.example` to `.env` before your first build. It is read twice, and the two
readers deliberately disagree about what to do when it is missing:

- at runtime, as `import.meta.env.VITE_API_BASE` (`src/background/api.ts`).
  There is **no fallback here**: `apiBase()` throws a `missing_config` error
  naming the variable and this file. A localhost guess would quietly point real
  checks at a backend you never started, and the reader would see a message
  blaming their network.
- at build time by `manifest.config.ts`, which turns it into the single
  `host_permissions` entry — the backend origin and nothing else, never
  `<all_urls>`. This one **does** fall back to `http://localhost:8000`, because
  a manifest must always be emittable: `pnpm build` cannot fail on a fresh
  checkout with no `.env`, and Chrome will not load an extension whose
  `host_permissions` is empty. An unused host permission for an origin nothing
  ever calls grants nothing, so the asymmetry is safe in the one direction that
  matters.

Change it and you must rebuild and reload the unpacked extension.

## How the pieces fit

```
manifest.config.ts        MV3 manifest (name, permissions, popup, service worker)
src/types/schema.ts       GENERATED from shared/schema.json — do not hand-edit
src/shared/messages.ts    typed popup ↔ background ↔ content-script messages
src/background/           service worker: the only thing that calls the backend
src/content/extract.ts    injected on click, never at install
src/popup/                React popup (ready → checking → done → error)
```

### The content script is injected, never declared

The manifest has **no `content_scripts` entry** — nothing runs until the reader
clicks. The service worker injects the extractor with `chrome.scripting`, and
CRXJS builds that file because the background imports it as a dynamic script:

```ts
// src/background/index.ts
import extractScript from '../content/extract?script&iife'

await chrome.scripting.executeScript({ target: { tabId }, files: [extractScript] })
```

`extractScript` is the built file's path — `src/content/extract.js` in a
production build, `src/content/extract.ts.iife.js` in dev — so **never hardcode
it**. The `?script&iife` query makes CRXJS emit one self-contained IIFE bundle
(Readability inlined, no `import` statements), which is what
`executeScript({ files })` requires, and adds it to `web_accessible_resources`.

`src/vite-env.d.ts` carries the type declarations for those query imports
(`@crxjs/vite-plugin/client`), for `chrome.*`, and for `import.meta.env`.

### Reviewing privacy: review `pnpm build`, never a `pnpm dev` load

The production manifest declares the extractor bundle with
`use_dynamic_url: true`, so Chrome serves it from a per-session token rather
than a stable `chrome-extension://<permanent id>/…` URL that any page could
fetch to learn the reader has Re-Vera installed (CLAUDE.md rule 6).

**That control only exists on the production build.** Under `pnpm dev`, CRXJS's
serve-mode plugin pushes its own web-accessible-resources entry
(`{ matches: ['<all_urls>'], resources: ['**/*', '*'], use_dynamic_url: false }`)
that `manifest.config.ts` cannot override. A dev-loaded extension therefore
exposes every file to every origin at a stable URL. It is CRXJS behaviour, it
does not reach the shipped bundle, and the fix is simply to review the right
artefact: run `pnpm build` and inspect `dist/manifest.json`.

### Regenerating the shared schema

`src/types/schema.ts` is generated from the single source of truth,
`shared/schema.json`. After editing the schema run `./shared/generate.sh` from
the repo root (it regenerates the Pydantic models too) or `pnpm gen` for the
TypeScript half only, and commit the generated file with the schema change.

## Milestone 1 scope

Popup only: ready → checking (stepper + claim rows) → done → error. No
highlights, no side panel, no game mode. The `sidePanel` permission is declared
but `side_panel.default_path` is not — that page arrives in milestone 4, and
pointing the manifest at a file that does not exist stops Chrome from loading
the extension.

There are no toolbar icons in the manifest yet either, for the same reason: the
icon assets land with the design pass.
