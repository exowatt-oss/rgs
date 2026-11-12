import { useEffect, useSyncExternalStore, type RefObject } from 'react'

import { useLiveController } from '@/lib/liveRecording'

/** 0..1 mic level (~20 Hz). Re-renders the caller; keep it to small leaves. */
export function useLiveLevel(): number {
  const controller = useLiveController()
  return useSyncExternalStore(controller.subscribeLevel, controller.getLevel)
}

/** Writes the mic level into `--level` on an element without re-rendering
 *  React (the presence orb breathes at 20 Hz for free). */
export function useLevelVar(ref: RefObject<HTMLElement | SVGElement | null>, enabled = true): void {
  const controller = useLiveController()
  useEffect(() => {
    const el = ref.current
    if (!el) return
    if (!enabled) {
      el.style.setProperty('--level', '0')
      return
    }
    const apply = () => el.style.setProperty('--level', controller.getLevel().toFixed(3))
    apply()
    return controller.subscribeLevel(apply)
  }, [controller, ref, enabled])
}
