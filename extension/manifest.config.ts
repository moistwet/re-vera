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
    web_accessible_resources: [
      defineDynamicResource({ matches: ['http://*/*', 'https://*/*'] }),
    ],

    // NOTE: `side_panel.default_path` lands in milestone 4 together with
    // src/sidepanel/index.html. Declaring a path to a file that does not exist
    // yet makes Chrome refuse to load the unpacked extension, so only the
    // `sidePanel` permission is declared for now.
  }
})
