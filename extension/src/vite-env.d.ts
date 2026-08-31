/// <reference types="vite/client" />
/// <reference types="chrome" />
/// <reference types="@crxjs/vite-plugin/client" />

interface ImportMetaEnv {
  /**
   * Base URL of the Re-Vera backend, e.g. "http://localhost:8000".
   * Set it in extension/.env (gitignored — copy .env.example). The manifest's
   * host_permissions is derived from this origin, so changing it means
   * rebuilding and reloading the unpacked extension.
   */
  readonly VITE_API_BASE: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
