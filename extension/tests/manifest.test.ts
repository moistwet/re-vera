/**
 * Manifest tests — the privacy shape of the shipped extension, pinned.
 *
 * `manifest.config.ts` carries three controls that CLAUDE.md names directly and
 * that nothing else in the repo enforces. Each of them is one careless edit away
 * from silently disappearing, and none of them fails a build, a type-check or a
 * lint when it does:
 *
 *  1. **`use_dynamic_url: true` on every web-accessible resource** (rule 6).
 *     Without it Chrome serves the injected extractor from
 *     `chrome-extension://<the extension's permanent id>/src/content/extract.js`
 *     — a stable, guessable URL that ANY page can fetch to learn that this
 *     reader has Re-Vera installed, with no gesture from the reader at all.
 *     `defineDynamicResource(...)` defaults this flag to `false`, so dropping
 *     the one word `use_dynamic_url: true` reintroduces the fingerprint and
 *     leaves the file looking perfectly reasonable.
 *  2. **No `content_scripts` key** (rule 5, manual trigger only). A
 *     `content_scripts` entry runs Re-Vera code on every matching page at
 *     install, forever, whether or not the reader ever clicks. Injection goes
 *     through `chrome.scripting.executeScript` on the click and nowhere else.
 *  3. **No `<all_urls>` in `host_permissions`** (CLAUDE.md § Extension). The
 *     backend origin, and nothing else.
 *
 * Plus the permission set itself: four permissions, exactly, so a fifth has to
 * be argued for rather than added.
 *
 * The manifest is a `defineManifest` callback (`defineManifest` is identity —
 * it exists for the types), so this calls it the way Vite does and asserts on
 * what comes back.
 *
 * Scope note: this is the PRODUCTION manifest. Under `pnpm dev` CRXJS's
 * serve-mode plugin appends its own web-accessible-resources entry with
 * `<all_urls>` and `use_dynamic_url: false`, which `manifest.config.ts` cannot
 * override. That is a dev-only artefact of the CRXJS dev server and never
 * reaches `dist/` from `pnpm build`; see extension/README.md.
 */

import { describe, expect, it } from 'vitest'

import manifestExport from '../manifest.config'

/** The four permissions milestone 1 is allowed to ask for, and no others. */
const EXPECTED_PERMISSIONS = ['storage', 'activeTab', 'scripting', 'sidePanel']

/**
 * Build the manifest exactly as `vite build` does.
 *
 * `defineManifest` types its return as `ManifestV3 | Promise<ManifestV3> |
 * ManifestV3Fn`, so the callback shape is asserted rather than assumed: if
 * someone converts this file to a plain object the test says so instead of
 * quietly skipping every assertion below.
 */
async function buildManifest() {
  expect(typeof manifestExport).toBe('function')
  if (typeof manifestExport !== 'function') {
    throw new Error('manifest.config.ts no longer exports a defineManifest callback')
  }
  return await manifestExport({ mode: 'production', command: 'build' })
}

describe('production manifest', () => {
  it('is manifest v3', async () => {
    const manifest = await buildManifest()
    expect(manifest.manifest_version).toBe(3)
  })

  it('marks every web-accessible resource use_dynamic_url, so no page can probe for us', async () => {
    const manifest = await buildManifest()
    const resources = manifest.web_accessible_resources ?? []

    // An empty list would pass a `.every()` vacuously; the extractor bundle has
    // to be declared, so an empty list is itself a failure.
    expect(resources.length).toBeGreaterThan(0)
    for (const entry of resources) {
      expect(entry.use_dynamic_url).toBe(true)
    }
  })

  it('exposes those resources to no more than the http(s) web', async () => {
    const manifest = await buildManifest()
    for (const entry of manifest.web_accessible_resources ?? []) {
      const matches = 'matches' in entry ? entry.matches : []
      expect(matches).not.toContain('<all_urls>')
    }
  })

  it('declares no content_scripts, so nothing runs until the reader clicks', async () => {
    const manifest = await buildManifest()
    expect(manifest.content_scripts).toBeUndefined()
  })

  it('asks for the backend origin only, never <all_urls>', async () => {
    const manifest = await buildManifest()
    const hosts = manifest.host_permissions ?? []

    expect(hosts.length).toBeGreaterThan(0)
    expect(hosts).not.toContain('<all_urls>')
    expect(hosts).not.toContain('*://*/*')
    for (const host of hosts) {
      // Each entry is one concrete origin: scheme + host (+ port), then `/*`.
      expect(host).toMatch(/^https?:\/\/[^/*]+\/\*$/)
    }
  })

  it('asks for exactly the four milestone-1 permissions', async () => {
    const manifest = await buildManifest()
    expect(manifest.permissions).toEqual(EXPECTED_PERMISSIONS)
    // Anything broad enough to read pages the reader has not opted into would
    // show up here; `activeTab` is the deliberately narrow alternative.
    expect(manifest.permissions).not.toContain('tabs')
    expect(manifest.permissions).not.toContain('webNavigation')
  })
})
