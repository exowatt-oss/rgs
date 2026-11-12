/**
 * Owns one Live Record session end-to-end, independent of routed page
 * lifetimes (the provider lives in the app shell, so opening a finding's
 * earlier meeting never cuts the recording):
 *
 *   capture → PCM16 frames → AudioUplink (sample-indexed, resumable)
 *   events channel (WS or long-poll, resumes with since_seq) → liveReducer
 *
 * Exposes a `useSyncExternalStore`-shaped snapshot. The mic level has its
 * own store so the 20 Hz meter never re-renders the transcript.
 */
import {
  DEFAULT_AUDIO_FORMAT,
  AUDIO_CLOSE,
  BYTES_PER_SAMPLE,
  LiveSessionConflictError,
  isAuthLapse,
  isTerminalStatus,
  gateProviderLabel,
  liveApi,
  liveErrorMessage,
  type DeliveryChannel,
  type GateProviderId,
  type LiveConfig,
  type LiveEvent,
  type LiveSessionInfo,
} from '@/api/live'
import { ApiError } from '@/api/client'
import { copperVoice, PhononStream, type OnDeviceInfo } from '@/lib/asr/copperPhonon'
import {
  AudioCapture,
  captureSupported,
  describeMediaError,
  listMicrophones,
  queryMicPermission,
  rmsToLevel,
  type AudioSourceMode,
  type MicDevice,
  type MicPermission,
} from '@/lib/live/audioCapture'
import { AudioUplink, type UplinkCloseContext, type UplinkEndReason, type UplinkStats } from '@/lib/live/audioUplink'
import { openEventsChannel, type EventsChannel } from '@/lib/live/eventsChannel'
import { SignalHealth, type MicProblem } from '@/lib/live/signalHealth'
import {
  hasPendingTriggers,
  initialLiveState,
  liveReducer,
  type LiveAction,
  type LiveState,
} from '@/lib/live/liveReducer'
import type { SocketState } from '@/lib/live/reconnectingSocket'

export interface StartOptions {
  /** Optional title (ad-hoc conversations have no calendar event to name them). */
  title?: string
  /** Where the edition link goes when the session stops. */
  deliverVia?: DeliveryChannel[]
  /** Also share the edition with the people RGS recognises (opt-in). */
  shareWithParticipants?: boolean
}

export type LivePhase = 'idle' | 'starting' | 'live' | 'stopping' | 'ended' | 'viewing'

export interface LiveSnapshot {
  phase: LivePhase
  session: LiveSessionInfo | null
  config: LiveConfig | null
  configError: string | null
  /** Local clock (ms) when capture started; drives the elapsed timer. */
  startedAt: number | null
  endedAt: number | null
  error: string | null
  warning: string | null
  /** Neutral information (e.g. "following an existing session"). */
  notice: string | null
  /** The last stop discarded the recording. */
  discarded: boolean
  source: AudioSourceMode
  micActive: boolean
  micPermission: MicPermission
  devices: MicDevice[]
  deviceId: string | null
  micLabel: string | null
  /** The open mic delivers no usable sound (digital silence, muted, the
   *  audio graph stalled, or the device went away). */
  micProblem: MicProblem | null
  tabLabel: string | null
  audio: UplinkStats
  events: { state: SocketState; attempt: number }
  /** Sign-in lapsed; capture keeps buffering until it's restored. */
  authLapsed: boolean
  /** Decision engine picked in setup for the next session (remembered
   *  per browser). `null` = the server default. */
  gateProviderChoice: GateProviderId | null
  /** A live engine switch is in flight. */
  gateSwitching: boolean
  live: LiveState
  /** Copper's on-device words for this tab's capture (lib/asr/copperPhonon). */
  ondevice?: OnDeviceInfo
}

const IDLE_UPLINK: UplinkStats = {
  state: 'idle',
  attempt: 0,
  bufferedMs: 0,
  unackedMs: 0,
  droppedMs: 0,
  serverError: null,
  authLapsed: false,
  ended: null,
}

/** Ten minutes of 16 kHz PCM16 (~19 MB), far above the contract's 60 s
 *  `client_buffer_s` floor, so a long outage loses nothing. */
const MAX_BUFFER_SECONDS = 600
const DRAIN_TIMEOUT_MS = 8_000
export const ACTIVE_SESSION_KEY = 'rgs:live-session'
export const GATE_PROVIDER_KEY = 'rgs:gate-provider'

async function retry<T>(op: () => Promise<T>, attempts = 3): Promise<T> {
  for (let i = 0; ; i += 1) {
    try {
      return await op()
    } catch (error) {
      if (i >= attempts - 1 || (error instanceof ApiError && error.status < 500) || error instanceof LiveSessionConflictError) throw error
      await new Promise((r) => setTimeout(r, 500 * 2 ** i))
    }
  }
}

const parseTime = (iso: string | null): number | null => {
  if (!iso) return null
  const t = Date.parse(iso)
  return Number.isFinite(t) ? t : null
}

export class LiveRecordController {
  private snapshot: LiveSnapshot = {
    phase: 'idle',
    session: null,
    config: null,
    configError: null,
    startedAt: null,
    endedAt: null,
    error: null,
    warning: null,
    notice: null,
    discarded: false,
    source: 'mic',
    micActive: false,
    micPermission: 'unknown',
    devices: [],
    deviceId: null,
    micLabel: null,
    micProblem: null,
    tabLabel: null,
    audio: IDLE_UPLINK,
    events: { state: 'idle', attempt: 0 },
    authLapsed: false,
    gateProviderChoice: rememberedGateProvider(),
    gateSwitching: false,
    live: initialLiveState,
  }
  private listeners = new Set<() => void>()
  private levelListeners = new Set<() => void>()
  private level = 0
  private capture: AudioCapture | null = null
  private captureReady: Promise<void> | null = null
  private reopening: Promise<void> | null = null
  private health: SignalHealth | null = null
  private healthTimer: ReturnType<typeof setInterval> | null = null
  private uplink: AudioUplink | null = null
  private phonon: PhononStream | null = null
  /** Frames captured before the session exists (first words matter). */
  private preroll: ArrayBuffer[] | null = null
  private channel: EventsChannel | null = null
  private recording = false
  private pendingEvents: LiveEvent[] = []
  private flushTimer: ReturnType<typeof setTimeout> | null = null
  private tickTimer: ReturnType<typeof setInterval> | null = null
  private statsTimer: ReturnType<typeof setInterval> | null = null
  private skewMs = 0
  private wakeLock: { release: () => Promise<void> } | null = null
  private disposed = false
  private permissionStatus: PermissionStatus | null = null
  private configLoad: Promise<void> | null = null

  // --- store plumbing -----------------------------------------------------------

  getSnapshot = (): LiveSnapshot => this.snapshot
  subscribe = (cb: () => void) => {
    this.listeners.add(cb)
    return () => {
      this.listeners.delete(cb)
    }
  }
  getLevel = (): number => this.level
  subscribeLevel = (cb: () => void) => {
    this.levelListeners.add(cb)
    return () => {
      this.levelListeners.delete(cb)
    }
  }

  private update(patch: Partial<LiveSnapshot>): void {
    this.snapshot = { ...this.snapshot, ...patch }
    this.listeners.forEach((l) => l())
  }

  private dispatch(action: LiveAction): void {
    const live = liveReducer(this.snapshot.live, action)
    if (live !== this.snapshot.live) this.update({ live })
    this.syncTicker()
  }

  private get format() {
    return this.snapshot.config?.audio ?? DEFAULT_AUDIO_FORMAT
  }

  private get transport(): 'ws' | 'http' {
    return this.snapshot.config?.websockets === false ? 'http' : 'ws'
  }

  // --- environment ----------------------------------------------------------------

  /** Feature flag + capabilities, mic permission and devices (no prompts). */
  async probe(): Promise<void> {
    void this.loadConfig()
    const micPermission = await queryMicPermission()
    let devices: MicDevice[]
    try {
      devices = micPermission === 'granted' ? await listMicrophones() : []
    } catch {
      devices = []
    }
    if (this.disposed) return
    this.update({ micPermission, devices })
    if (!this.permissionStatus && micPermission !== 'unsupported') {
      try {
        const status = await navigator.permissions.query({ name: 'microphone' as PermissionName })
        status.onchange = () => void this.probe()
        this.permissionStatus = status
      } catch {
        // Permissions API without microphone support (older Safari).
      }
    }
  }

  loadConfig(force = false): Promise<void> {
    if (this.configLoad && !force) return this.configLoad
    this.configLoad = liveApi
      .config()
      .then((config) => this.update({ config, configError: null }))
      .catch((error) => {
        this.configLoad = null
        this.update({ configError: liveErrorMessage(error, 'Could not load Live record settings') })
      })
    return this.configLoad
  }

  setSource(source: AudioSourceMode): void {
    if (source === this.snapshot.source) return
    this.update({ source })
    if (source === 'mic' && this.capture?.hasTab) {
      this.capture.removeTab()
      this.update({ tabLabel: null })
    }
    // Echo cancellation differs per mode: reopen the mic.
    if (this.capture) void this.reopenMic()
  }

  /** Setup: which decision engine the next session starts with. */
  setGateProviderChoice(provider: GateProviderId | null): void {
    if (provider === this.snapshot.gateProviderChoice) return
    this.update({ gateProviderChoice: provider })
    try {
      if (provider) window.localStorage.setItem(GATE_PROVIDER_KEY, provider)
      else window.localStorage.removeItem(GATE_PROVIDER_KEY)
    } catch {
      // Storage disabled: the choice just isn't remembered.
    }
  }

  /** The engine to request on start: the setup choice when this deployment
   *  offers it, else `null` (server default). Older servers that list no
   *  providers never receive `gate_provider`. */
  startGateProvider(): GateProviderId | null {
    const providers = this.snapshot.config?.gate.providers ?? []
    const choice = this.snapshot.gateProviderChoice
    return choice && providers.some((p) => p.id === choice && p.available) ? choice : null
  }

  /** Live (owner only): switch the decision engine for this session. */
  async switchGateProvider(provider: GateProviderId): Promise<void> {
    const session = this.snapshot.session
    if (!session || !session.isOwner || isTerminalStatus(this.snapshot.live.status)) return
    const before = this.snapshot.live.gate.provider ?? session.gateProvider ?? this.snapshot.config?.gate.defaultProvider ?? null
    if (before === provider || this.snapshot.gateSwitching) return
    this.dispatch({ type: 'gate.provider', provider })
    this.update({ gateSwitching: true })
    try {
      const updated = await liveApi.setGateProvider(session.id, provider)
      if (this.snapshot.session?.id !== session.id) return
      const applied = updated.gateProvider ?? provider
      this.dispatch({ type: 'gate.provider', provider: applied })
      this.update({ session: { ...this.snapshot.session, gateProvider: applied } })
      this.setGateProviderChoice(applied)
    } catch (error) {
      if (this.snapshot.session?.id !== session.id) return
      this.dispatch({ type: 'gate.provider', provider: before })
      const label = gateProviderLabel(provider, this.snapshot.config?.gate.providers) ?? provider
      this.update({ warning: liveErrorMessage(error, `Could not switch to ${label}`) })
    } finally {
      this.update({ gateSwitching: false })
    }
  }

  setDevice(deviceId: string | null): void {
    this.update({ deviceId })
    if (this.capture) void this.reopenMic()
  }

  /** Open the mic for the level meter (user gesture). */
  async startPreview(): Promise<void> {
    await this.ensureCapture().catch(() => {})
  }

  async stopPreview(): Promise<void> {
    // Never during start: Home unmounts (navigate to /live) right after
    // start() kicks off capture, and closing the context mid-addModule fails
    // with "Unable to load a worklet's module".
    if (this.recording || !this.capture) return
    if (this.snapshot.phase !== 'idle' && this.snapshot.phase !== 'ended') return
    await this.releaseCapture()
  }

  /** Prompt for the Meet tab's audio (user gesture). */
  async shareTab(keepCaptureOnFailure = false): Promise<boolean> {
    // The context is created synchronously inside the gesture (Safari).
    const capture = this.capture ?? this.createCapture()
    try {
      await capture.addTab()
      this.update({ tabLabel: capture.tabLabel, warning: null })
    } catch (error) {
      const message =
        error instanceof DOMException ? describeMediaError(error, 'tab') : liveErrorMessage(error, describeMediaError(error, 'tab'))
      this.update({ warning: message })
      if (!keepCaptureOnFailure && !this.snapshot.micActive && !this.recording) await this.releaseCapture()
      return false
    }
    await this.ensureCapture().catch(() => {})
    return true
  }

  // --- session lifecycle ----------------------------------------------------------

  /** Start capture + create the session. Call from the record click. */
  /** `meetUrl: null` = an ad-hoc (in-person) conversation. */
  async start(meetUrl: string | null, options: StartOptions = {}): Promise<void> {
    if (this.snapshot.phase !== 'idle' && this.snapshot.phase !== 'ended') return
    if (!captureSupported()) {
      this.update({ error: 'This browser can’t capture audio for live transcription. Use a current Chrome, Edge or Safari.' })
      return
    }
    this.closeChannel()
    this.resetSessionState()
    this.update({ phase: 'starting', session: null, error: null, warning: null, notice: null, discarded: false })
    // Capture first so the first words are kept while the session is created.
    const capturePromise = (async () => {
      if (this.snapshot.source === 'mic+tab' && !this.capture?.hasTab) await this.shareTab(true)
      await this.ensureCapture()
    })()
    const configPromise = this.loadConfig()
    try {
      await capturePromise
    } catch {
      this.update({ phase: 'idle' })
      return
    }
    await configPromise
    if (this.disposed) return
    this.preroll = []
    this.recording = true
    this.update({ startedAt: Date.now() })
    let session: LiveSessionInfo
    try {
      const gateProvider = this.startGateProvider()
      session = await retry(() =>
        liveApi.create(meetUrl, {
          gateProvider,
          title: options.title?.trim() || undefined,
          kind: meetUrl ? 'call' : 'adhoc',
          deliverVia: options.deliverVia,
          shareWithParticipants: options.shareWithParticipants,
        }),
      )
    } catch (error) {
      this.recording = false
      this.preroll = null
      await this.releaseCapture()
      if (error instanceof LiveSessionConflictError) {
        this.update({ phase: 'idle', startedAt: null })
        await this.attach(error.sessionId)
        const mine = this.snapshot.session?.id === error.sessionId && this.snapshot.session.isOwner
        this.update({
          notice: mine
            ? 'You’re already recording this Meet from another tab or device. Resume capture here to move the recording to this laptop.'
            : `${error.message} You’re following that session live instead.`,
        })
        return
      }
      this.update({ phase: 'idle', startedAt: null, error: liveErrorMessage(error, 'Could not start the live session') })
      return
    }
    if (this.disposed) return
    this.update({ session })
    this.dispatch({ type: 'session', session })
    rememberSession(session.id)
    this.beginUplink(session.id)
    this.update({ phase: 'live' })
    this.openChannel(session.id)
    void this.acquireWakeLock()
  }

  /** After a reload (owner only): capture from this laptop again into the
   *  same session. The server's `ready` offset makes this a clean resume. */
  async resumeCapture(): Promise<void> {
    const session = this.snapshot.session
    if (!session || !session.isOwner || this.snapshot.phase !== 'viewing' || this.recording) return
    if (this.snapshot.live.status && this.snapshot.live.status !== 'live') return
    this.update({ error: null, warning: null, notice: null })
    if (this.snapshot.source === 'mic+tab' && !this.capture?.hasTab) await this.shareTab(true)
    try {
      await this.ensureCapture()
    } catch {
      return
    }
    this.recording = true
    this.beginUplink(session.id)
    this.update({ phase: 'live', startedAt: parseTime(session.startedAt) ?? Date.now() })
    rememberSession(session.id)
    void this.acquireWakeLock()
  }

  /** Stop capture, flush buffered audio, and close the session. `discard`
   *  deletes the audio, transcript and findings server-side. */
  async stop(discard = false): Promise<void> {
    const session = this.snapshot.session
    if (!session || !['live', 'stopping', 'viewing'].includes(this.snapshot.phase)) return
    this.update({ phase: 'stopping', error: null })
    if (this.capture && this.recording) await this.capture.flush()
    this.recording = false
    await this.releaseCapture()
    const phonon = this.phonon
    this.phonon = null
    if (discard) phonon?.cancel()
    const phononDone = discard ? Promise.resolve() : (phonon?.stop() ?? Promise.resolve())
    if (this.uplink) {
      if (!discard) {
        const drained = await this.uplink.drain(DRAIN_TIMEOUT_MS)
        if (!drained && !this.uplink.stats.ended)
          this.update({ warning: 'The last few seconds of audio could not be confirmed by the server before stopping.' })
      }
      this.uplink.close()
      this.uplink = null
    }
    this.stopStatsTimer()
    await phononDone
    let stopped: LiveSessionInfo
    try {
      stopped = await retry(() => liveApi.stop(session.id, discard))
    } catch (error) {
      this.update({ error: liveErrorMessage(error, 'Could not stop the session') })
      return
    }
    this.update({ session: stopped, discarded: discard })
    this.dispatch({ type: 'session', session: stopped })
    this.finish()
  }

  /** Follow a session (reload recovery, a 409 on start, or a shared link). */
  async attach(sessionId: string): Promise<void> {
    if (this.snapshot.session?.id === sessionId || this.recording) return
    this.closeChannel()
    this.resetSessionState()
    void this.loadConfig()
    let session: LiveSessionInfo
    try {
      session = await liveApi.get(sessionId)
    } catch (error) {
      forgetSession(sessionId)
      const gone = error instanceof ApiError && error.status === 404
      this.update({ error: gone ? 'That live session doesn’t exist or you can’t see it.' : liveErrorMessage(error, 'Could not load the live session') })
      return
    }
    if (this.disposed) return
    const ended = isTerminalStatus(session.status)
    this.update({
      session,
      phase: ended ? 'ended' : 'viewing',
      startedAt: parseTime(session.startedAt),
      endedAt: ended ? parseTime(session.stoppedAt) ?? Date.now() : null,
      discarded: session.status === 'cancelled',
    })
    this.dispatch({ type: 'session', session })
    if (!ended && session.isOwner) rememberSession(session.id)
    this.openChannel(session.id)
  }

  /** Leave an ended / followed session and return to setup. */
  reset(): void {
    if (this.recording) return
    this.closeChannel()
    if (this.snapshot.session) forgetSession(this.snapshot.session.id)
    this.resetSessionState()
    this.update({ phase: 'idle', session: null, error: null, warning: null, notice: null, discarded: false })
  }

  async renameSpeaker(speakerId: string, displayName: string, userEmail: string | null = null): Promise<void> {
    const session = this.snapshot.session
    const before = this.snapshot.live.speakers[speakerId]
    if (!session || !before || !session.isOwner) return
    this.dispatch({ type: 'speaker.rename', speakerId, displayName })
    try {
      const updated = await liveApi.renameSpeaker(session.id, speakerId, displayName, userEmail)
      if (updated) this.dispatch({ type: 'speaker.upsert', speaker: updated })
    } catch (error) {
      this.dispatch({ type: 'speaker.upsert', speaker: before })
      this.update({ warning: liveErrorMessage(error, 'Could not rename the speaker') })
    }
  }

  retryConnections(): void {
    this.uplink?.retryNow()
    this.channel?.retryNow()
  }

  dismissWarning(): void {
    this.update({ warning: null, notice: null })
  }

  dismissError(): void {
    this.update({ error: null })
  }

  /** Called by the provider when the tab becomes visible again. */
  onVisible(): void {
    this.retryConnections()
    this.recoverAudio()
    if (this.recording && !this.wakeLock) void this.acquireWakeLock()
  }

  /** "Retry mic" (user gesture): restart the audio context and re-acquire
   *  the selected microphone. */
  async retryMic(): Promise<void> {
    const capture = this.capture
    if (!capture) {
      await this.startPreview()
      return
    }
    // Synchronously inside the click, so Safari lets the context restart.
    void capture.resume()
    await this.reopenMic()
    const devices = await listMicrophones().catch(() => null)
    if (devices && !this.disposed) this.update({ devices })
  }

  get isCapturing(): boolean {
    return this.recording
  }

  activate(): void {
    this.disposed = false
  }

  dispose(): void {
    this.disposed = true
    this.recording = false
    this.uplink?.close()
    this.uplink = null
    this.phonon?.cancel()
    this.phonon = null
    this.stopStatsTimer()
    this.stopHealth()
    this.closeChannel()
    void this.releaseCapture()
    void this.releaseWakeLock()
    if (this.permissionStatus) this.permissionStatus.onchange = null
    if (this.tickTimer !== null) clearInterval(this.tickTimer)
    this.tickTimer = null
  }

  // --- capture -----------------------------------------------------------------

  private createCapture(): AudioCapture {
    const capture = new AudioCapture({
      onFrame: (buffer) => {
        if (!this.recording) return
        if (this.uplink) {
          this.uplink.push(buffer)
          this.phonon?.push(buffer)
        } else this.preroll?.push(buffer)
      },
      onLevel: (rms) => {
        this.health?.level(rms, Date.now())
        const next = rmsToLevel(rms)
        // Fast attack, slow release reads like a real meter.
        this.level = next > this.level ? next : this.level * 0.82 + next * 0.18
        this.levelListeners.forEach((l) => l())
      },
      onMicEnded: () => {
        if (this.health) this.health.ended = true
        this.update({ micActive: false })
        this.checkSignal()
      },
      onMicMuted: (muted) => {
        if (this.health) this.health.muted = muted
        if (!muted) void capture.resume()
        this.checkSignal()
      },
      onContextState: (state) => {
        if (state !== 'running') void capture.resume()
        this.checkSignal()
      },
      onTabEnded: () => {
        this.update({
          tabLabel: null,
          warning: this.recording ? 'Meet tab audio stopped. Recording continues from the microphone.' : null,
        })
      },
    })
    this.capture = capture
    this.captureReady = null
    return capture
  }

  private ensureCapture(): Promise<void> {
    if (this.captureReady) return this.captureReady
    const capture = this.capture ?? this.createCapture()
    const ready = (async () => {
      try {
        await capture.init(this.format)
        await capture.setMic(this.snapshot.source, this.snapshot.deviceId)
      } catch (error) {
        await this.releaseCapture()
        const micPermission = await queryMicPermission()
        this.update({ micPermission, error: describeMediaError(error, 'mic') })
        throw error
      }
      const devices = await listMicrophones().catch(() => [] as MicDevice[])
      this.update({
        micActive: true,
        micPermission: 'granted',
        devices,
        micLabel: capture.micLabel,
        tabLabel: capture.tabLabel,
        error: null,
      })
      this.armHealth(capture)
    })()
    this.captureReady = ready
    ready.catch(() => {
      if (this.captureReady === ready) this.captureReady = null
    })
    return ready
  }

  private reopenMic(): Promise<void> {
    const capture = this.capture
    if (!capture) return Promise.resolve()
    const run = (async () => {
      try {
        await this.captureReady
        await capture.setMic(this.snapshot.source, this.snapshot.deviceId)
        if (this.capture !== capture) return
        this.update({ micActive: true, micLabel: capture.micLabel, warning: null })
        this.armHealth(capture)
      } catch (error) {
        this.update({ warning: describeMediaError(error, 'mic') })
      }
    })().finally(() => {
      if (this.reopening === run) this.reopening = null
    })
    this.reopening = run
    return run
  }

  /** Tab visible again: restart a context WebKit suspended and take the
   *  mic back if it was muted or ended while hidden. */
  private recoverAudio(): void {
    const capture = this.capture
    if (!capture) return
    void capture.resume()
    const track = capture.micTrackState
    if ((track === 'muted' || track === 'ended') && !this.reopening) void this.reopenMic()
  }

  /** Judge a freshly opened mic from scratch. */
  private armHealth(capture: AudioCapture): void {
    const health = new SignalHealth(Date.now())
    const track = capture.micTrackState
    health.muted = track === 'muted'
    health.ended = track === 'ended'
    this.health = health
    if (this.healthTimer === null) this.healthTimer = setInterval(() => this.checkSignal(), 500)
    this.checkSignal()
  }

  private checkSignal(): void {
    const capture = this.capture
    const health = this.health
    if (!capture || !health) return
    const problem = health.problem(Date.now())
    // A stalled graph is usually a suspended / interrupted context.
    if (problem === 'stalled' || capture.context.state !== 'running') void capture.resume()
    if (problem !== this.snapshot.micProblem) this.update({ micProblem: problem })
  }

  private stopHealth(): void {
    if (this.healthTimer !== null) clearInterval(this.healthTimer)
    this.healthTimer = null
    this.health = null
  }

  private async releaseCapture(): Promise<void> {
    const capture = this.capture
    this.capture = null
    this.captureReady = null
    this.stopHealth()
    this.level = 0
    this.levelListeners.forEach((l) => l())
    if (this.snapshot.micActive || this.snapshot.tabLabel || this.snapshot.micProblem)
      this.update({ micActive: false, tabLabel: null, micProblem: null })
    if (capture) await capture.close()
  }

  // --- uplink ------------------------------------------------------------------

  private beginUplink(sessionId: string): void {
    const format = this.format
    const uplink = new AudioUplink({
      sessionId,
      format,
      transport: this.transport,
      maxBufferBytes: Math.max(format.clientBufferS, MAX_BUFFER_SECONDS) * format.sampleRate * BYTES_PER_SAMPLE,
      onChange: () => this.onUplinkChange(),
      onEnded: (reason, message, context) => void this.onUplinkEnded(reason, message, context),
    })
    for (const frame of this.preroll ?? []) uplink.push(frame)
    this.preroll = null
    this.uplink = uplink
    uplink.start()
    this.openPhonon(sessionId, uplink)
    if (this.statsTimer === null) this.statsTimer = setInterval(() => this.onUplinkChange(), 500)
  }

  /** Inside Copper: its on-device words for this capture, beside the uplink. */
  private openPhonon(sessionId: string, uplink: AudioUplink): void {
    this.phonon?.cancel()
    if (!copperVoice()) {
      this.phonon = null
      return
    }
    const phonon = new PhononStream({
      sessionId,
      sessionSamples: () => uplink.buffer.end,
      onChange: (ondevice) => {
        if (this.phonon === phonon || ondevice.status === 'ended') this.update({ ondevice })
      },
    })
    this.phonon = phonon
    void phonon.open()
  }

  private onUplinkChange(): void {
    const audio = this.uplink?.stats ?? IDLE_UPLINK
    const prev = this.snapshot.audio
    if (
      audio.state !== prev.state ||
      audio.attempt !== prev.attempt ||
      Math.abs(audio.bufferedMs - prev.bufferedMs) >= 250 ||
      audio.droppedMs !== prev.droppedMs ||
      audio.serverError !== prev.serverError ||
      audio.authLapsed !== prev.authLapsed ||
      audio.ended !== prev.ended
    ) {
      this.update({ audio, authLapsed: audio.authLapsed || (this.snapshot.authLapsed && audio.state !== 'open') })
    }
  }

  private async onUplinkEnded(reason: UplinkEndReason, message: string | null, context?: UplinkCloseContext): Promise<void> {
    if (!this.recording) return
    this.recording = false
    await this.releaseCapture()
    this.uplink?.close()
    this.uplink = null
    this.phonon?.cancel()
    this.phonon = null
    this.stopStatsTimer()
    void this.releaseWakeLock()
    // The server's error.code is authoritative: 4409 is only not_live,
    // 4410 only superseded. Probe status solely when that frame was lost.
    if (
      context?.hasErrorFrame === false &&
      (context.closeCode === AUDIO_CLOSE.notLive || context.closeCode === AUDIO_CLOSE.superseded) &&
      this.snapshot.session
    ) {
      const current = await liveApi.get(this.snapshot.session.id).catch(() => null)
      if (current && current.status !== 'live') {
        this.update({ session: current })
        this.dispatch({ type: 'session', session: current })
        reason = 'stopped'
      }
    }
    switch (reason) {
      case 'superseded':
        this.update({
          phase: 'viewing',
          warning: 'Audio for this session is now coming from another tab or device, so this tab stopped capturing.',
        })
        return
      case 'not_owner':
        this.update({ phase: 'viewing', warning: 'Only the person who started this session can send its audio.' })
        return
      case 'bad_frame':
        this.update({ phase: 'viewing', error: `The server rejected the audio stream${message ? `: ${message}` : ''}. Resume capture to try again.` })
        return
      case 'unavailable':
        this.update({ phase: 'viewing', error: 'Live record is not available on this deployment right now.' })
        return
      case 'not_found':
        this.update({ error: 'This live session no longer exists.' })
        this.finish()
        return
      case 'stopped':
        // Stopped elsewhere (another device, an admin, the session limit).
        this.update({ notice: 'The session was stopped on the server.' })
        this.finish()
    }
  }

  private stopStatsTimer(): void {
    if (this.statsTimer !== null) clearInterval(this.statsTimer)
    this.statsTimer = null
  }

  // --- events ------------------------------------------------------------------

  private openChannel(sessionId: string): void {
    this.closeChannel()
    this.channel = openEventsChannel({
      sessionId,
      transport: this.transport,
      sinceSeq: () => this.snapshot.live.lastSeq,
      onEvents: (events) => this.enqueueEvents(events),
      onStateChange: (state, attempt) => this.update({ events: { state, attempt } }),
      onEnded: (reason) => {
        if (reason === 'not_found') this.update({ error: 'This live session no longer exists or you can’t see it.' })
        else if (reason === 'forbidden') this.update({ error: 'You don’t have access to this live session.' })
      },
      onAuthLapse: () => this.update({ authLapsed: true }),
      onTrouble: () => void this.diagnose(sessionId),
      expectHeartbeat: () => this.snapshot.live.status === 'live',
    })
  }

  private closeChannel(): void {
    this.channel?.close()
    this.channel = null
    if (this.flushTimer !== null) clearTimeout(this.flushTimer)
    this.flushTimer = null
    if (this.pendingEvents.length) this.flushEvents()
  }

  /** Repeated failures: tell an expired sign-in apart from a network blip. */
  private async diagnose(sessionId: string): Promise<void> {
    try {
      const session = await liveApi.get(sessionId)
      this.dispatch({ type: 'session', session })
      if (this.snapshot.authLapsed) this.update({ authLapsed: false })
    } catch (error) {
      if (isAuthLapse(error)) this.update({ authLapsed: true })
      else if (error instanceof ApiError && error.status === 404) this.update({ error: 'This live session no longer exists.' })
    }
  }

  private enqueueEvents(events: LiveEvent[]): void {
    for (const e of events) {
      // Ephemeral events are "now" on the server: estimate clock skew so
      // replayed triggers can be aged correctly.
      if (e.ts !== null && (e.type === 'snapshot' || e.type === 'session.heartbeat' || e.type === 'segment.interim')) {
        this.skewMs = Date.now() - e.ts
      }
    }
    this.pendingEvents.push(...events)
    // Coalesce bursts (resume replay) into one render.
    if (this.flushTimer === null) this.flushTimer = setTimeout(() => this.flushEvents(), 16)
  }

  private flushEvents(): void {
    this.flushTimer = null
    const events = this.pendingEvents
    this.pendingEvents = []
    if (!events.length) return
    this.dispatch({ type: 'events', events, now: Date.now(), skewMs: this.skewMs })
    if (this.snapshot.authLapsed && this.snapshot.audio.state !== 'reconnecting') this.update({ authLapsed: false })
    const { status, meetingId } = this.snapshot.live
    const session = this.snapshot.session
    if (session && (status !== session.status || meetingId !== session.meetingId) && status) {
      this.update({ session: { ...session, status, meetingId: meetingId ?? session.meetingId } })
    }
    // The server ended or is finalizing a session this tab is capturing.
    if (this.snapshot.phase === 'live' && status && status !== 'live') void this.serverEnded()
    if (this.snapshot.phase === 'viewing' && isTerminalStatus(status)) this.finish()
  }

  private async serverEnded(): Promise<void> {
    if (!this.recording) return
    this.recording = false
    await this.releaseCapture()
    this.uplink?.close()
    this.uplink = null
    this.phonon?.cancel()
    this.phonon = null
    this.stopStatsTimer()
    this.finish()
  }

  /** Capture is over; keep the events channel to watch finalization. */
  private finish(): void {
    void this.releaseWakeLock()
    if (this.snapshot.session) forgetSession(this.snapshot.session.id)
    this.update({
      phase: 'ended',
      endedAt: this.snapshot.endedAt ?? parseTime(this.snapshot.session?.stoppedAt ?? null) ?? Date.now(),
    })
  }

  private syncTicker(): void {
    const needed = hasPendingTriggers(this.snapshot.live)
    if (needed && this.tickTimer === null) {
      this.tickTimer = setInterval(() => this.dispatch({ type: 'tick', now: Date.now() }), 500)
    } else if (!needed && this.tickTimer !== null) {
      clearInterval(this.tickTimer)
      this.tickTimer = null
    }
  }

  private resetSessionState(): void {
    if (this.tickTimer !== null) clearInterval(this.tickTimer)
    this.tickTimer = null
    this.pendingEvents = []
    this.skewMs = 0
    this.update({
      live: initialLiveState,
      startedAt: null,
      endedAt: null,
      audio: IDLE_UPLINK,
      events: { state: 'idle', attempt: 0 },
      authLapsed: false,
      ondevice: undefined,
    })
  }

  // --- wake lock ---------------------------------------------------------------

  private async acquireWakeLock(): Promise<void> {
    type Lock = { release: () => Promise<void>; addEventListener: (e: 'release', cb: () => void) => void }
    const nav = navigator as Navigator & { wakeLock?: { request: (t: 'screen') => Promise<Lock> } }
    if (!nav.wakeLock || document.visibilityState !== 'visible') return
    try {
      const lock = await nav.wakeLock.request('screen')
      lock.addEventListener('release', () => {
        if (this.wakeLock === lock) this.wakeLock = null
      })
      this.wakeLock = lock
    } catch {
      // Battery saver / permissions policy — the laptop may sleep; not fatal.
    }
  }

  private async releaseWakeLock(): Promise<void> {
    const lock = this.wakeLock
    this.wakeLock = null
    await lock?.release().catch(() => {})
  }
}

export function rememberSession(id: string): void {
  try {
    window.sessionStorage.setItem(ACTIVE_SESSION_KEY, id)
  } catch {
    // Storage disabled: reload recovery just won't be offered.
  }
}

export function forgetSession(id: string): void {
  try {
    if (window.sessionStorage.getItem(ACTIVE_SESSION_KEY) === id) window.sessionStorage.removeItem(ACTIVE_SESSION_KEY)
  } catch {
    // ignore
  }
}

export function rememberedGateProvider(): GateProviderId | null {
  try {
    return window.localStorage.getItem(GATE_PROVIDER_KEY) || null
  } catch {
    return null
  }
}

export function rememberedSession(): string | null {
  try {
    return window.sessionStorage.getItem(ACTIVE_SESSION_KEY)
  } catch {
    return null
  }
}
