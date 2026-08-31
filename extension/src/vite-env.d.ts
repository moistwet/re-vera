/// <reference types="vite/client" />
/// <reference types="chrome" />
/// <reference types="@crxjs/vite-plugin/client" />

interface ImportMetaEnv {
  /**
   * Base URL of the Re-Vera backend, e.g. "http://localhost:8000".
   *
   * Set it in extension/.env (gitignored — copy .env.example). The manifest's
   * host_permissions is derived from this origin, so changing it means
   * rebuilding and reloading the unpacked extension.
   *
   * **Optional, and it means it.** Vite only inlines the variables it finds, so
   * a build with no `extension/.env` leaves this `undefined` at runtime.
   * Declaring it `string` made every reader of it look total when it is not,
   * which is how a missing variable turns into a request to `undefined/check`
   * instead of a sentence naming the variable. `apiBase()` in
   * `src/background/api.ts` is the one place that reads it, and it throws a
   * `missing_config` ApiError when it is absent.
   */
  readonly VITE_API_BASE?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
