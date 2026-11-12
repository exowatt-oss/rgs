import { useEffect, useState } from 'react'

/** Wall clock that re-renders the caller every `intervalMs` while `enabled`. */
export function useNow(intervalMs = 1000, enabled = true): number {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (!enabled) return
    const t = setInterval(() => setNow(Date.now()), intervalMs)
    return () => clearInterval(t)
  }, [intervalMs, enabled])
  return now
}
