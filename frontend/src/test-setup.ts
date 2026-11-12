import '@testing-library/jest-dom/vitest'
import { afterEach } from 'vitest'
import { cleanup } from '@testing-library/react'

// Clean up the DOM between tests so leftover nodes don't bleed across cases.
afterEach(() => {
  cleanup()
})

// jsdom doesn't implement ResizeObserver, which some shadcn/Radix primitives
// (dialog, dropdown-menu, tabs) touch in effects. A no-op stub is enough for
// tests — we don't assert on observer callbacks. Apps that add observer-driven
// UI inherit the stub for free.
if (typeof globalThis.ResizeObserver === 'undefined') {
  globalThis.ResizeObserver = class ResizeObserver {
    observe() {}
    unobserve() {}
    disconnect() {}
  }
}

// jsdom doesn't implement matchMedia. Stub it as "no match" so any
// responsive hooks render the desktop layout deterministically.
if (typeof window !== 'undefined' && typeof window.matchMedia !== 'function') {
  window.matchMedia = (query: string) =>
    ({
      matches: false,
      media: query,
      onchange: null,
      addEventListener: () => {},
      removeEventListener: () => {},
      addListener: () => {},
      removeListener: () => {},
      dispatchEvent: () => false,
    }) as unknown as MediaQueryList
}

// jsdom has no layout, so `scrollIntoView` is missing; the transcript and
// answer cards call it when jumping between a finding and its line.
if (typeof Element.prototype.scrollIntoView !== 'function') {
  Element.prototype.scrollIntoView = () => {}
}
