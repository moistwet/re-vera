import { defineManifest, defineDynamicResource } from '@crxjs/vite-plugin'
import { loadEnv } from 'vite'

/**
 * Re-Vera MV3 manifest.
 *
 * Two rules from CLAUDE.md are load-bearing here:
 *
 *  - `host_permissions` names the backend origin ONLY. Never `<all_urls>`.
 *  - There is no `content_scripts` entry, so nothing runs at install or on
 *    navigation. The content script is injected on click by the service worker
 *    via `chrome.scripting.executeScript` (manual trigger only). CRXJS still
 *    builds it, because the background imports it as
 *    `'../content/extract?script&iife'` — see README.md.
 *
 * On `VITE_API_BASE`: this file keeps a localhost fallback where
 * `src/background/api.ts` deliberately throws instead. A manifest must always
 * be emittable — `pnpm build` cannot be allowed to fail on a checkout with no
 * `.env`, and an empty `host_permissions` is not a thing Chrome will load — so
 * the fallback names the local backend the README tells you to run. The
 * asymmetry is safe in the one direction that matters: an unused
 * `host_permissions` entry for an origin nothing ever calls grants nothing,
 * whereas a *runtime* fallback would quietly point real checks at a backend the
 * reader never started. The error the reader would see then blames the network;
 * the one `apiBase()` throws names the variable.
 */

const DEFAULT_API_BASE = 'http://localhost:8000'

/** `http://localhost:8000` -> `http://localhost:8000/*` (origin only, no path). */
function hostPermission(apiBase: string): string {
  try {
    return `${new URL(apiBase).origin}/*`
  } catch {
    return `${new URL(DEFAULT_API_BASE).origin}/*`
  }
}

export default defineManifest(({ mode }) => {
  // Relative envDir keeps this file free of node globals; Vite resolves it
  // against the project root (extension/), where .env / .env.local live.
  const env = loadEnv(mode, '.', 'VITE_')
  const apiBase = env.VITE_API_BASE || DEFAULT_API_BASE

  return {
    manifest_version: 3,
    name: 'Re-Vera',
    version: '0.1.0',
    description:
      'Checks the factual claims in the news article you are reading, only when you ask, and shows the evidence.',

    permissions: ['storage', 'activeTab', 'scripting', 'sidePanel'],
    host_permissions: [hostPermission(apiBase)],

    action: {
      default_title: 'Re-Vera',
      default_popup: 'src/popup/index.html',
    },

    background: {
      service_worker: 'src/background/index.ts',
      type: 'module',
    },

    // The content script is a CRXJS "dynamic script": it has no manifest
    // `content_scripts` entry, and this placeholder tells CRXJS which origins
    // may load the bundle it emits for `?script&iife` imports.
    //
    // `use_dynamic_url: true` is a privacy control, not a build detail
    // (CLAUDE.md rule 6). Without it Chrome serves the bundle from
    // `chrome-extension://<the extension's permanent id>/src/content/extract.js`
    // — a stable, guessable URL that *any* page can fetch to learn that this
    // reader has Re-Vera installed, without the reader ever invoking it. With
    // it, Chrome swaps in a per-session token that rotates, so there is no
    // stable string left to probe for.
    //
    // The `matches` list cannot be narrowed further: the reader may ask for a
    // check on any news site, and CRXJS defaults to exactly these two patterns
    // when the list is empty anyway. It costs nothing, though — injection goes
    // through `chrome.scripting.executeScript({ files })`, which reads the file
    // from the extension package and never through a web-accessible URL, so a
    // page that guessed the URL could only read a bundle whose source is
    // public. What it must not learn is that the URL resolves at all.
    web_accessible_resources: [
      defineDynamicResource({ matches: ['http://*/*', 'https://*/*'], use_dynamic_url: true }),
    ],

    // NOTE: `side_panel.default_path` lands in milestone 4 together with
    // src/sidepanel/index.html. Declaring a path to a file that does not exist
    // yet makes Chrome refuse to load the unpacked extension, so only the
    // `sidePanel` permission is declared for now.
  }
})
