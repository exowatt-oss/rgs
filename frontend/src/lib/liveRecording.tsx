import { createContext, useContext, useEffect, useState, useSyncExternalStore, type ReactNode } from 'react'

import type { LiveController } from '@/lib/live/controller'
import { LiveRecordController, type LiveSnapshot } from '@/lib/live/liveRecordController'
import { isMockMode } from '@/lib/mockMode'
import { MockLiveController } from '@/mock/MockLiveController'

const Context = createContext<LiveController | null>(null)

/** One live-session owner per app shell, so moving between pages never
 *  stops the capture. `?mock=1` swaps in the scripted replay. */
export function LiveRecordProvider({ children, controller: injected }: { children: ReactNode; controller?: LiveController }) {
  const [controller] = useState<LiveController>(
    () => injected ?? (isMockMode() ? new MockLiveController() : new LiveRecordController()),
  )
  useEffect(() => {
    controller.activate()
    void controller.probe()
    const onVisible = () => {
      if (document.visibilityState === 'visible') controller.onVisible()
    }
    document.addEventListener('visibilitychange', onVisible)
    return () => {
      document.removeEventListener('visibilitychange', onVisible)
      controller.dispose()
    }
  }, [controller])
  const capturing = useSyncExternalStore(controller.subscribe, () => {
    const phase = controller.getSnapshot().phase
    return phase === 'starting' || phase === 'live' || phase === 'stopping'
  })
  useEffect(() => {
    // The ?mock=1 replay has nothing to lose; only a real capture warns.
    if (!capturing || controller.mock) return
    const warn = (event: BeforeUnloadEvent) => {
      event.preventDefault()
      event.returnValue = ''
    }
    window.addEventListener('beforeunload', warn)
    return () => window.removeEventListener('beforeunload', warn)
  }, [capturing, controller])
  return <Context.Provider value={controller}>{children}</Context.Provider>
}

// eslint-disable-next-line react-refresh/only-export-components -- context hook shares its provider
export function useLiveController(): LiveController {
  const controller = useContext(Context)
  if (!controller) throw new Error('LiveRecordProvider is missing')
  return controller
}

// eslint-disable-next-line react-refresh/only-export-components -- context hook shares its provider
export function useLiveRecord(): LiveSnapshot & { controller: LiveController } {
  const controller = useLiveController()
  const snapshot = useSyncExternalStore(controller.subscribe, controller.getSnapshot)
  return { controller, ...snapshot }
}

/** Subscribe to one derived primitive (shell chrome re-renders only when
 *  it changes, not on every transcript event). */
// eslint-disable-next-line react-refresh/only-export-components -- context hook shares its provider
export function useLiveRecordSelector<T extends string | number | boolean | null>(select: (s: LiveSnapshot) => T): T {
  const controller = useLiveController()
  return useSyncExternalStore(controller.subscribe, () => select(controller.getSnapshot()))
}
