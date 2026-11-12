/**
 * Browser audio capture for Live Record: laptop mic, optionally mixed with
 * the Meet tab's audio, encoded to PCM16 frames by an AudioWorklet.
 *
 * Graph:  mic ─ micGain ─┐
 *                         ├─ mix ─ lowpass ─ lowpass ─ worklet ─ (gain 0) ─ destination
 *         tab ─ tabGain ─┘
 *
 * The AudioContext runs at the device's native rate (Safari and Firefox
 * refuse to connect a MediaStream source to a context at a different rate),
 * two cascaded Butterworth low-passes act as the anti-alias filter, and the
 * worklet decimates to the contract's 16 kHz. The worklet's output feeds a
 * muted gain into the destination so every engine keeps pulling it.
 *
 * Mic processing choice (room capture):
 *  - mic only: echoCancellation OFF. Meet usually plays the remote side out
 *    of this laptop's speakers; AEC would subtract exactly those voices.
 *  - mic + Meet tab: remote voices arrive clean from the tab, so AEC ON
 *    removes their speaker bleed from the mic and avoids a doubled echo.
 *  - noiseSuppression OFF (ASR prefers the raw signal), AGC ON (people sit
 *    at very different distances from a laptop).
 */
import { DEFAULT_AUDIO_FORMAT, frameSamples, type LiveAudioFormat } from '@/api/live'

export type AudioSourceMode = 'mic' | 'mic+tab'
export type MicPermission = 'unknown' | 'prompt' | 'granted' | 'denied' | 'unsupported'

export interface MicDevice {
  deviceId: string
  label: string
}

// The worklet source is bundled into the app and loaded from a Blob URL, so
// addModule never goes over the network: a separate fetch can hit the SSO
// proxy's cross-origin redirect (expired cookie) and fail with "Unable to
// load a worklet's module". A data: URL is the fallback for browsers or
// policies that refuse blob: modules.
import WORKLET_SOURCE from './live-pcm16-capture.worklet.js?raw'

export const WORKLET_SOURCE_TEXT: string = WORKLET_SOURCE

async function addWorkletModule(context: AudioContext): Promise<void> {
  const blobUrl = URL.createObjectURL(new Blob([WORKLET_SOURCE], { type: 'text/javascript' }))
  try {
    await context.audioWorklet.addModule(blobUrl)
    return
  } catch (blobError) {
    try {
      await context.audioWorklet.addModule(
        `data:text/javascript;charset=utf-8,${encodeURIComponent(WORKLET_SOURCE)}`,
      )
      return
    } catch {
      throw blobError
    }
  } finally {
    URL.revokeObjectURL(blobUrl)
  }
}
const WORKLET_NAME = 'live-pcm16-capture'
const ANTI_ALIAS_HZ = 7_200

export interface AudioCaptureEvents {
  onFrame: (buffer: ArrayBuffer) => void
  onLevel: (rms: number, peak: number) => void
  onMicEnded: () => void
  /** The browser muted / unmuted the mic track (another page took it,
   *  OS-level mute, hidden WebKit page). */
  onMicMuted?: (muted: boolean) => void
  /** AudioContext left (or returned to) 'running'. */
  onContextState?: (state: string) => void
  onTabEnded: () => void
}

export function micConstraints(mode: AudioSourceMode, deviceId: string | null): MediaTrackConstraints {
  return {
    ...(deviceId ? { deviceId: { exact: deviceId } } : {}),
    channelCount: 1,
    echoCancellation: mode === 'mic+tab',
    noiseSuppression: false,
    autoGainControl: true,
  }
}

export function captureSupported(): boolean {
  return (
    typeof window !== 'undefined' &&
    typeof window.AudioContext === 'function' &&
    typeof window.AudioWorkletNode === 'function' &&
    !!navigator.mediaDevices?.getUserMedia
  )
}

export function tabCaptureSupported(): boolean {
  return typeof navigator !== 'undefined' && !!navigator.mediaDevices?.getDisplayMedia
}

export async function queryMicPermission(): Promise<MicPermission> {
  if (!captureSupported()) return 'unsupported'
  try {
    const status = await navigator.permissions.query({ name: 'microphone' as PermissionName })
    return status.state === 'granted' || status.state === 'denied' || status.state === 'prompt' ? status.state : 'unknown'
  } catch {
    return 'unknown'
  }
}

export async function listMicrophones(): Promise<MicDevice[]> {
  if (!navigator.mediaDevices?.enumerateDevices) return []
  const devices = await navigator.mediaDevices.enumerateDevices()
  return devices
    .filter((d) => d.kind === 'audioinput' && d.deviceId !== 'default' && d.deviceId !== 'communications')
    .map((d, i) => ({ deviceId: d.deviceId, label: d.label || `Microphone ${i + 1}` }))
}

/** Human-readable reason for a getUserMedia / getDisplayMedia failure. */
export function describeMediaError(error: unknown, what: 'mic' | 'tab'): string {
  const name = error instanceof DOMException || error instanceof Error ? error.name : ''
  if (what === 'tab') {
    if (name === 'NotAllowedError') return 'Tab sharing was cancelled. Recording will use the microphone only.'
    return 'Could not capture the Meet tab audio. Recording will use the microphone only.'
  }
  if (name === 'NotAllowedError' || name === 'SecurityError')
    return 'Microphone access is blocked. Allow it from the site settings (the icon left of the address bar), then try again.'
  if (name === 'NotFoundError' || name === 'OverconstrainedError')
    return 'No microphone found. Plug one in or pick another input.'
  if (name === 'NotReadableError') return 'The microphone is in use by another app or failed to start.'
  return error instanceof Error && error.message ? error.message : 'Could not start the microphone.'
}

export class AudioCapture {
  readonly context: AudioContext
  private events: AudioCaptureEvents
  private node: AudioWorkletNode | null = null
  private mix: GainNode
  private filters: BiquadFilterNode[] = []
  private sink: GainNode
  private mic: { stream: MediaStream; source: MediaStreamAudioSourceNode } | null = null
  private tab: { stream: MediaStream; source: MediaStreamAudioSourceNode; gain: GainNode } | null = null
  private micGain: GainNode
  private flushWaiter: (() => void) | null = null
  private closed = false

  /** Construct synchronously inside the user gesture (Safari only lets a
   *  context start from one), then `await init()`. */
  constructor(events: AudioCaptureEvents) {
    this.events = events
    this.context = new AudioContext({ latencyHint: 'interactive' })
    this.mix = this.context.createGain()
    this.micGain = this.context.createGain()
    this.micGain.connect(this.mix)
    this.sink = this.context.createGain()
    this.sink.gain.value = 0
    this.context.onstatechange = () => {
      if (!this.closed) this.events.onContextState?.(this.context.state)
    }
    void this.context.resume().catch(() => {})
  }

  /** Restart a suspended / interrupted context (best from a user gesture). */
  async resume(): Promise<void> {
    if (this.closed || this.context.state === 'running') return
    await this.context.resume().catch(() => {})
  }

  /** 'muted' / 'ended' when the browser is feeding the graph nothing. */
  get micTrackState(): 'live' | 'muted' | 'ended' | null {
    const track = this.mic?.stream.getAudioTracks()[0]
    if (!track) return null
    if (track.readyState === 'ended') return 'ended'
    return track.muted ? 'muted' : 'live'
  }

  async init(format: LiveAudioFormat = DEFAULT_AUDIO_FORMAT): Promise<void> {
    await addWorkletModule(this.context)
    if (this.closed) return
    const node = new AudioWorkletNode(this.context, WORKLET_NAME, {
      numberOfInputs: 1,
      numberOfOutputs: 1,
      outputChannelCount: [1],
      channelCount: 1,
      channelCountMode: 'explicit',
      channelInterpretation: 'speakers',
      processorOptions: {
        targetSampleRate: format.sampleRate,
        frameSamples: frameSamples(format),
      },
    })
    node.port.onmessage = (event: MessageEvent) => {
      const data = event.data as { type?: string; buffer?: ArrayBuffer; rms?: number; peak?: number }
      if (data.type === 'frame' && data.buffer) this.events.onFrame(data.buffer)
      else if (data.type === 'level') this.events.onLevel(data.rms ?? 0, data.peak ?? 0)
      else if (data.type === 'flushed') {
        this.flushWaiter?.()
        this.flushWaiter = null
      }
    }
    this.node = node
    let upstream: AudioNode = this.mix
    if (this.context.sampleRate > format.sampleRate) {
      for (let i = 0; i < 2; i += 1) {
        const f = this.context.createBiquadFilter()
        f.type = 'lowpass'
        f.frequency.value = Math.min(ANTI_ALIAS_HZ, format.sampleRate * 0.45)
        f.Q.value = Math.SQRT1_2
        upstream.connect(f)
        upstream = f
        this.filters.push(f)
      }
    }
    upstream.connect(node)
    node.connect(this.sink)
    this.sink.connect(this.context.destination)
    await this.context.resume().catch(() => {})
  }

  get running(): boolean {
    return !this.closed
  }

  get micLabel(): string | null {
    return this.mic?.stream.getAudioTracks()[0]?.label || null
  }

  get micDeviceId(): string | null {
    return this.mic?.stream.getAudioTracks()[0]?.getSettings().deviceId ?? null
  }

  get tabLabel(): string | null {
    if (!this.tab) return null
    return this.tab.stream.getAudioTracks()[0]?.label || 'Shared tab'
  }

  /** (Re)acquire the microphone; replaces any current mic input. */
  async setMic(mode: AudioSourceMode, deviceId: string | null): Promise<void> {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: micConstraints(mode, deviceId), video: false })
    if (this.closed) {
      stream.getTracks().forEach((t) => t.stop())
      return
    }
    this.releaseMic()
    const source = this.context.createMediaStreamSource(stream)
    source.connect(this.micGain)
    this.mic = { stream, source }
    const track = stream.getAudioTracks()[0]
    track?.addEventListener('ended', () => {
      if (this.mic?.stream === stream) this.events.onMicEnded()
    })
    const muted = (value: boolean) => () => {
      if (this.mic?.stream === stream) this.events.onMicMuted?.(value)
    }
    track?.addEventListener('mute', muted(true))
    track?.addEventListener('unmute', muted(false))
    await this.resume()
  }

  /** Prompt for a tab (pick the Meet tab, tick "share tab audio"). Must run
   *  from a user gesture. Throws when no audio track was shared. */
  async addTab(): Promise<void> {
    const options = {
      video: true, // Chrome requires video to offer tab audio; the track is stopped at once.
      audio: { echoCancellation: false, noiseSuppression: false, autoGainControl: false },
      preferCurrentTab: false,
      selfBrowserSurface: 'exclude',
      surfaceSwitching: 'include',
      systemAudio: 'include',
      monitorTypeSurfaces: 'exclude',
    } as DisplayMediaStreamOptions
    const stream = await navigator.mediaDevices.getDisplayMedia(options)
    stream.getVideoTracks().forEach((t) => t.stop())
    const audio = stream.getAudioTracks()
    if (audio.length === 0) {
      stream.getTracks().forEach((t) => t.stop())
      throw new Error('No tab audio was shared. Pick the Meet tab and turn on "Also share tab audio".')
    }
    if (this.closed) {
      stream.getTracks().forEach((t) => t.stop())
      return
    }
    this.removeTab()
    const source = this.context.createMediaStreamSource(new MediaStream(audio))
    const gain = this.context.createGain()
    source.connect(gain)
    gain.connect(this.mix)
    this.tab = { stream, source, gain }
    audio[0].addEventListener('ended', () => {
      if (this.tab?.stream === stream) {
        this.removeTab()
        this.events.onTabEnded()
      }
    })
  }

  removeTab(): void {
    if (!this.tab) return
    this.tab.source.disconnect()
    this.tab.gain.disconnect()
    this.tab.stream.getTracks().forEach((t) => t.stop())
    this.tab = null
  }

  get hasTab(): boolean {
    return this.tab !== null
  }

  /** Ask the worklet to emit its partial last frame; resolves once it has
   *  (or after a short timeout if the context is already suspended). */
  flush(timeoutMs = 600): Promise<void> {
    const node = this.node
    if (!node || this.closed) return Promise.resolve()
    return new Promise<void>((resolve) => {
      const timer = setTimeout(() => {
        this.flushWaiter = null
        resolve()
      }, timeoutMs)
      this.flushWaiter = () => {
        clearTimeout(timer)
        resolve()
      }
      node.port.postMessage({ type: 'flush' })
    })
  }

  async close(): Promise<void> {
    if (this.closed) return
    this.closed = true
    this.context.onstatechange = null
    this.releaseMic()
    this.removeTab()
    if (this.node) {
      this.node.port.onmessage = null
      this.node.disconnect()
    }
    this.filters.forEach((f) => f.disconnect())
    this.mix.disconnect()
    this.micGain.disconnect()
    this.sink.disconnect()
    await this.context.close().catch(() => {})
  }

  private releaseMic(): void {
    if (!this.mic) return
    this.mic.source.disconnect()
    this.mic.stream.getTracks().forEach((t) => t.stop())
    this.mic = null
  }
}

/** Map RMS (linear) to a 0..1 meter value on a -60..0 dBFS scale. */
export function rmsToLevel(rms: number): number {
  if (rms <= 0) return 0
  const db = 20 * Math.log10(rms)
  return Math.min(1, Math.max(0, (db + 60) / 60))
}
