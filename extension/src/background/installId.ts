/**
 * The anonymous install ID.
 *
 * Re-Vera has no accounts. One random UUID per browser profile exists for
 * exactly one purpose — the backend's daily cap of 20 checks — and it lives in
 * `chrome.storage.local`, so it survives service-worker restarts and browser
 * restarts but disappears with the extension. It is never attached to article
 * text, never sent anywhere except `POST /check`, and never logged next to what
 * was checked (CLAUDE.md, privacy rule 6).
 */

/** Key in `chrome.storage.local`. Fixed: changing it resets everyone's cap. */
const STORAGE_KEY = 'install_id'

/**
 * In-flight generation, shared by concurrent callers.
 *
 * Read-then-write is not atomic across `await`s: two callers that both miss the
 * store would each mint a UUID and the second would overwrite the first, so a
 * check already counted against one ID would continue under another. Caching
 * the promise makes the first caller's ID the only one.
 */
let pending: Promise<string> | null = null

/** The install ID, creating and persisting one on first use. */
export async function getInstallId(): Promise<string> {
  pending ??= resolveInstallId().finally(() => {
    pending = null
  })
  return pending
}

async function resolveInstallId(): Promise<string> {
  const stored = await chrome.storage.local.get(STORAGE_KEY)
  const existing = stored[STORAGE_KEY]
  if (typeof existing === 'string' && existing.length > 0) return existing

  const installId = crypto.randomUUID()
  await chrome.storage.local.set({ [STORAGE_KEY]: installId })
  return installId
}
