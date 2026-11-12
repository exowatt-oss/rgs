/**
 * "Is the microphone actually hearing anything?" for Live Record.
 *
 * A granted, open mic can still deliver nothing: WebKit (Safari, Copper)
 * zeroes or mutes capture for a page that is hidden or isn't the active
 * capturer (e.g. Google Meet open in another tab of the same browser), and
 * Chrome can hand back a silent track for a mic another app holds
 * exclusively. The worklet then posts frames of exact digital zeros, or the
 * AudioContext stops pulling and nothing arrives at all.
 *
 * Fed the worklet's ~20 Hz level reports plus track/context events; pure so
 * it can be unit-tested with an injected clock.
 */
export type MicProblem = 'silent' | 'muted' | 'stalled' | 'ended'

/** About -80 dBFS: below any real (AGC'd) microphone's noise floor, so only
 *  digital silence or a muted track reads lower. */
export const SILENCE_RMS = 1e-4
/** Digital silence this long right after the mic opens → it isn't working. */
export const SILENT_AFTER_MS = 3_000
/** Once real sound was heard, allow longer (noise-gated mics, pauses). */
export const SILENT_AFTER_SOUND_MS = 15_000
/** No level report this long → the audio graph stopped running. */
export const STALL_MS = 2_500

export class SignalHealth {
  private lastLevelAt = 0
  private silentSince: number | null = 0
  private heardSound = false
  muted = false
  ended = false

  constructor(now: number) {
    this.reset(now)
  }

  /** The mic was (re)opened: judge it afresh. */
  reset(now: number): void {
    this.lastLevelAt = now
    this.silentSince = now
    this.heardSound = false
    this.muted = false
    this.ended = false
  }

  level(rms: number, now: number): void {
    this.lastLevelAt = now
    if (rms >= SILENCE_RMS) {
      this.silentSince = null
      this.heardSound = true
    } else if (this.silentSince === null) {
      this.silentSince = now
    }
  }

  problem(now: number): MicProblem | null {
    if (this.ended) return 'ended'
    if (this.muted) return 'muted'
    if (now - this.lastLevelAt > STALL_MS) return 'stalled'
    const limit = this.heardSound ? SILENT_AFTER_SOUND_MS : SILENT_AFTER_MS
    if (this.silentSince !== null && now - this.silentSince >= limit) return 'silent'
    return null
  }
}
