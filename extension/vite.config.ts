import { crx } from '@crxjs/vite-plugin'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vitest/config'

import manifest from './manifest.config'

// Vitest runs with `mode === 'test'`; the CRX plugin would otherwise try to
// build the whole extension (and its manifest inputs) just to run unit tests.
export default defineConfig(({ mode }) => {
  const isTest = mode === 'test'

  return {
    plugins: isTest ? [] : [react(), crx({ manifest })],

    build: {
      outDir: 'dist',
      emptyOutDir: true,
      // MV3 only ever runs in a current Chrome.
      target: 'esnext',
      sourcemap: true,
    },

    // CRXJS serves the popup and HMR from this port during `pnpm dev`.
    server: {
      port: 5173,
      strictPort: true,
      hmr: { port: 5173 },
    },

    test: {
      environment: 'node',
      include: ['tests/**/*.test.ts'],
    },
  }
})
