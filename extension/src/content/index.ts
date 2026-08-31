/**
 * Alternative content-script entry point. NOT the one that ships.
 *
 * The shipped convention — agreed by manifest.config.ts, README.md and
 * src/background/index.ts — is that the service worker imports
 * `'../content/extract?script&iife'`, so CRXJS bundles **extract.ts** and
 * `chrome.scripting.executeScript` injects `src/content/extract.js`. Nothing
 * imports this file, and no build entry points at it; it exists only so that
 * the `content/index?script&iife` spelling also works if the convention is ever
 * switched. Wiring both at once would emit two ~95 kB copies of the same
 * bundle, so switch, do not add.
 *
 * Either way there is no `content_scripts` entry in the manifest, so nothing
 * runs on navigation or at install — only on the reader's click. In milestone 1
 * the extractor renders nothing on the host page: no Shadow DOM host, no
 * styles, no DOM mutation of any kind. Highlights and the claim card arrive in
 * milestone 3.
 *
 * `installExtractBridge()` is idempotent — it is guarded by a flag on the
 * isolated world's global — so re-injecting on every click (which is what
 * `executeScript` does) leaves exactly one listener behind, and calling it here
 * on top of extract.ts's own self-install registers nothing extra.
 */

import { installExtractBridge } from './extract'

installExtractBridge()
