/**
 * `?mock=1` review mode: the whole app runs against a replayed fake meeting
 * (no sign-in, no microphone, no backend). The flag sticks for the tab
 * (sessionStorage) so in-app navigation keeps it; `?mock=0` turns it off.
 */
const KEY = 'rgs:mock'

let cached: boolean | null = null

export function isMockMode(): boolean {
  if (cached !== null) return cached
  cached = readMockFlag()
  return cached
}

function readMockFlag(): boolean {
  if (typeof window === 'undefined') return false
  const param = new URLSearchParams(window.location.search).get('mock')
  try {
    if (param === '1' || param === 'true') window.sessionStorage.setItem(KEY, '1')
    else if (param === '0' || param === 'false') window.sessionStorage.removeItem(KEY)
    return window.sessionStorage.getItem(KEY) === '1'
  } catch {
    return param === '1' || param === 'true'
  }
}

/** Test hook. */
export function setMockModeForTests(value: boolean | null): void {
  cached = value
}
