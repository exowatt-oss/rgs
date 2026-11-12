import type { LiveRecordController } from '@/lib/live/liveRecordController'

/** Everything the UI may call on a live controller. The real
 *  `LiveRecordController` and the `?mock=1` replay both satisfy it. */
export type LiveController = Pick<
  LiveRecordController,
  | 'getSnapshot'
  | 'subscribe'
  | 'getLevel'
  | 'subscribeLevel'
  | 'probe'
  | 'loadConfig'
  | 'setSource'
  | 'setGateProviderChoice'
  | 'startGateProvider'
  | 'switchGateProvider'
  | 'setDevice'
  | 'startPreview'
  | 'stopPreview'
  | 'shareTab'
  | 'start'
  | 'resumeCapture'
  | 'stop'
  | 'attach'
  | 'reset'
  | 'renameSpeaker'
  | 'retryConnections'
  | 'dismissWarning'
  | 'dismissError'
  | 'onVisible'
  | 'retryMic'
  | 'activate'
  | 'dispose'
> & {
  /** Review-only levers, present in `?mock=1` mode. */
  readonly mock?: MockControls
}

export interface MockControls {
  simulateSilence(): void
  dropConnection(): void
  /** Number of speakers in the scripted call (1–4). */
  cast(): 1 | 2 | 3 | 4
  /** Replay from the top, optionally with another cast. */
  replay(cast?: 1 | 2 | 3 | 4): void
}
