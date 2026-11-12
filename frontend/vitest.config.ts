import path from 'node:path'
import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'

/** Vitest config — runs jsdom for DOM tests, picks up `*.test.{ts,tsx}`
 *  under `src/`. Aliases mirror the app's Vite config so `@/...` imports
 *  resolve. The setup file wires `@testing-library/jest-dom` matchers.
 *
 *  The template ships exactly one test today (the `RequireAuth` reauth-loop
 *  regression — see `src/router.test.tsx`). Keep it green and add more as
 *  the app grows. */
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  test: {
    globals: true,
    environment: 'jsdom',
    setupFiles: ['./src/test-setup.ts'],
    include: ['src/**/*.test.{ts,tsx}'],
    css: false,
  },
})
