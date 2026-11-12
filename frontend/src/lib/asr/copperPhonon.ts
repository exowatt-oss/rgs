/**
 * Copper's on-device speech-to-text (Phonon-2), when RGS runs inside Copper.
 *
 * The optional Copper browser (https://github.com/copper-browser/Copper) gives authorized pages
 * `window.copper.voice.listen()` after the person allows it once per site.
 * The page pushes the same 16 kHz PCM16 frames it uploads to RGS; Copper
 * returns each finished phrase with word times, counted from the first frame
 * pushed. This module feeds Copper, puts those times on the session timeline
 * (the uplink's sample count tells where the first pushed frame landed) and
 * posts the phrases to `POST /api/live/sessions/{id}/ondevice/segments`.
 * Nemotron still decides who spoke; the server merges the two by time.
 *
 * Anywhere else (another browser, Copper without voice, consent refused) it
 * reports why and does nothing: RGS runs on Nemotron alone, as before.
 */

import { fetchApi } from '@/api/client'

export interface CopperWord {
  text: string
  start: number
  end: number
  approx?: boolean
}

export interface CopperFinal {
  type: 'final'
  seq: number
  start: number
  end: number
  text: string
  words: CopperWord[]
}

export interface CopperState {
  ok: boolean
  available: boolean
  enabled: boolean
  ready: boolean
  model: string
  consent: 'granted' | 'denied' | 'ask'
  engine: string
}

interface CopperListen {
  id: number
  push(pcm: ArrayBuffer | Int16Array | Float32Array): void
  stop(): Promise<void>
  cancel(): void
}

interface CopperVoice {
  version: number
  engine: string
  state(): Promise<CopperState>
  listen(opts: {
    sampleRate?: number
    onPartial?: (e: { text: string }) => void
    onFinal?: (e: CopperFinal) => void
    onEnd?: (e: { reason: string }) => void
    onError?: (e: { message?: string }) => void
  }): Promise<CopperListen>
}

/** Copper's page voice, if this page has it. */
export function copperVoice(): CopperVoice | null {
  if (typeof window === 'undefined') return null
  const voice = (window as unknown as { copper?: { voice?: Partial<CopperVoice> } }).copper?.voice
  return voice && typeof voice.listen === 'function' && typeof voice.state === 'function' ? (voice as CopperVoice) : null
}

export type OnDeviceStatus =
  /** Not in Copper (or a Copper without page voice). */
  | 'absent'
  /** Copper, but voice is off, the model isn't ready, or this site was refused. */
  | 'unavailable'
  | 'starting'
  | 'on'
  | 'ended'

export interface OnDeviceInfo {
  status: OnDeviceStatus
  engine: string | null
  /** Why it isn't on, in words, when it isn't. */
  reason: string | null
  /** Phrases the server has from this tab. */
  phrases: number
  /** Last partial (the phrase in flight). */
  partial: string
}

export const IDLE_ONDEVICE: OnDeviceInfo = {
  status: 'absent',
  engine: null,
  reason: null,
  phrases: 0,
  partial: '',
}

const SAMPLE_RATE = 16000
const POST_RETRY_MS = 2000
const STOP_TIMEOUT_MS = 4000

interface WireSegment {
  seq: number
  start_s: number
  end_s: number
  text: string
  words: { text: string; start_s: number; end_s: number }[]
}

const round3 = (x: number) => Math.round(x * 1000) / 1000

/** A Copper final on the session timeline (`baseS` = where Copper's 0 is). */
export function toWire(final: CopperFinal, baseS: number): WireSegment {
  const start = round3(baseS + final.start)
  return {
    // Unique per session across tabs and reloads: the phrase's start in ms.
    seq: Math.round(start * 1000),
    start_s: start,
    end_s: round3(baseS + final.end),
    text: final.text,
    words: (final.words ?? []).map((w) => ({
      text: w.text,
      start_s: round3(baseS + w.start),
      end_s: round3(baseS + w.end),
    })),
  }
}

export interface PhononStreamOptions {
  sessionId: string
  /** Session-timeline samples the uplink has buffered so far (its `buffer.end`). */
  sessionSamples: () => number
  onChange: (info: OnDeviceInfo) => void
  voice?: CopperVoice | null
  post?: (sessionId: string, body: { engine: string; segments: WireSegment[] }) => Promise<unknown>
}

const defaultPost = (sessionId: string, body: { engine: string; segments: WireSegment[] }) =>
  fetchApi(`/live/sessions/${encodeURIComponent(sessionId)}/ondevice/segments`, {
    method: 'POST',
    body,
    signal: AbortSignal.timeout(10_000),
    skipAuthRedirect: true,
  })

/**
 * One Copper listen for one uplink. `push` every frame right after the
 * uplink gets it; `stop` before the session stops.
 */
export class PhononStream {
  private info: OnDeviceInfo = { ...IDLE_ONDEVICE }
  private listen: CopperListen | null = null
  private opening: Promise<void> | null = null
  /** Samples pushed to Copper: its time 0 is `sessionSamples() - fed` at any moment. */
  private fed = 0
  private queue: WireSegment[] = []
  private posting = false
  private retry: ReturnType<typeof setTimeout> | null = null
  private closed = false
  private readonly voice: CopperVoice | null
  private readonly post: NonNullable<PhononStreamOptions['post']>

  private readonly opts: PhononStreamOptions

  constructor(opts: PhononStreamOptions) {
    this.opts = opts
    this.voice = opts.voice === undefined ? copperVoice() : opts.voice
    this.post = opts.post ?? defaultPost
  }

  get status(): OnDeviceInfo {
    return this.info
  }

  private set(patch: Partial<OnDeviceInfo>): void {
    this.info = { ...this.info, ...patch }
    this.opts.onChange(this.info)
  }

  /** Ask Copper (consent the first time); resolves once listening or refused. */
  open(): Promise<void> {
    if (!this.opening) this.opening = this.doOpen()
    return this.opening
  }

  private async doOpen(): Promise<void> {
    const voice = this.voice
    if (!voice) {
      this.set({ status: 'absent' })
      return
    }
    this.set({ status: 'starting', engine: voice.engine ?? 'phonon-2' })
    try {
      const state = await voice.state()
      if (!state.enabled) throw new Error('Turn on Voice in Copper › Settings › Intelligence.')
      if (!state.ready) throw new Error(`Copper’s speech model isn’t ready (${state.model}).`)
      if (state.consent === 'denied') throw new Error('On-device words are blocked for this site in Copper.')
      const listen = await voice.listen({
        sampleRate: SAMPLE_RATE,
        onPartial: (e) => this.set({ partial: e.text ?? '' }),
        onFinal: (e) => this.heard(e),
        onEnd: () => {
          if (!this.closed && this.info.status === 'on') this.set({ status: 'ended', partial: '' })
        },
      })
      if (this.closed) {
        listen.cancel()
        return
      }
      this.listen = listen
      this.set({ status: 'on', reason: null })
    } catch (error) {
      this.set({
        status: 'unavailable',
        reason: error instanceof Error ? error.message : String(error),
      })
    }
  }

  /** One PCM16 frame, the same one the uplink just took. */
  push(frame: ArrayBuffer): void {
    if (!this.listen || this.closed) return
    this.listen.push(frame)
    this.fed += frame.byteLength >> 1
  }

  private heard(final: CopperFinal): void {
    if (!final.text?.trim()) return
    const baseS = (this.opts.sessionSamples() - this.fed) / SAMPLE_RATE
    this.queue.push(toWire(final, Math.max(0, baseS)))
    this.set({ partial: '' })
    void this.flush()
  }

  private async flush(): Promise<void> {
    if (this.posting || !this.queue.length) return
    this.posting = true
    const batch = this.queue.slice(0, 50)
    try {
      await this.post(this.opts.sessionId, {
        engine: this.info.engine ?? 'phonon-2',
        segments: batch,
      })
      this.queue = this.queue.slice(batch.length)
      this.set({ phrases: this.info.phrases + batch.length })
    } catch {
      if (this.retry === null && !this.closed)
        this.retry = setTimeout(() => {
          this.retry = null
          void this.flush()
        }, POST_RETRY_MS)
    } finally {
      this.posting = false
    }
    if (this.queue.length && this.retry === null) void this.flush()
  }

  /** The last phrase out of Copper and to the server (bounded), then done. */
  async stop(): Promise<void> {
    const listen = this.listen
    this.listen = null
    if (listen) {
      await Promise.race([listen.stop().catch(() => {}), new Promise((r) => setTimeout(r, STOP_TIMEOUT_MS))])
    }
    this.closed = true
    if (this.retry !== null) clearTimeout(this.retry)
    this.retry = null
    for (let i = 0; i < 3 && this.queue.length; i++) {
      while (this.posting) await new Promise((r) => setTimeout(r, 50))
      await this.flush()
    }
    if (this.info.status === 'on') this.set({ status: 'ended', partial: '' })
  }

  /** Drop everything (discard, a superseded uplink). */
  cancel(): void {
    this.closed = true
    this.listen?.cancel()
    this.listen = null
    if (this.retry !== null) clearTimeout(this.retry)
    this.retry = null
    this.queue = []
    if (this.info.status === 'on' || this.info.status === 'starting') this.set({ status: 'ended', partial: '' })
  }
}
