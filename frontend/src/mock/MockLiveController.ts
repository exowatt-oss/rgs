/**
 * `?mock=1`: a stand-in for `LiveRecordController` that replays a scripted
 * meeting through the real wire normalizers and reducer — interim text
 * streaming word by word, gate ticks, a self-introduction rename, gate
 * triggers that get claimed and answered — with no microphone or backend.
 */
import { normalizeConfig, normalizeSession, parseLiveEvent, type DeliveryChannel, type GateProviderId, type LiveEvent } from '@/api/live'
import type { StartOptions } from '@/lib/live/liveRecordController'
import type { LiveController, MockControls } from '@/lib/live/controller'
import type { LiveSnapshot } from '@/lib/live/liveRecordController'
import { hasPendingTriggers, initialLiveState, liveReducer, type LiveAction } from '@/lib/live/liveReducer'
import { mockStore } from '@/mock/store'
import { CASTS, LINE_PAUSE_MS, TEAM_CHECKIN, WORD_MS, castFromUrl, type CastSize, type MockMeeting } from '@/mock/script'
import {
  MOCK_OTHER,
  beatSpeakerWire,
  buildStaticEvents,
  envelope,
  findingWire,
  sessionWire,
  speakerWire,
  tickWire,
  triggerWire,
  type Wire,
} from '@/mock/wire'

class Cancelled extends Error {}

export const MOCK_CONFIG_WIRE: Wire = {
  enabled: true,
  asr_configured: true,
  storage_configured: true,
  websockets: true,
  audio: { encoding: 'pcm_s16le', sample_rate: 16000, channels: 1, recommended_frame_ms: 100, max_frame_bytes: 65536, client_buffer_s: 60 },
  limits: { max_session_hours: 4, findings_per_minute: 6 },
  gate: {
    enabled: true,
    default_provider: 'openai',
    providers: [
      { id: 'jev', label: 'TypeSafe Jev', available: true },
      { id: 'openai', label: 'OpenAI Decisions', available: true },
    ],
    interval_s: 2.4,
  },
}

const DEVICES = [
  { deviceId: 'mbp', label: 'MacBook Pro Microphone' },
  { deviceId: 'studio', label: 'Studio Display Microphone' },
  { deviceId: 'airpods', label: 'AirPods Pro' },
]

const IDLE_AUDIO: LiveSnapshot['audio'] = {
  state: 'idle',
  attempt: 0,
  bufferedMs: 0,
  unackedMs: 0,
  droppedMs: 0,
  serverError: null,
  authLapsed: false,
  ended: null,
}

const TICK_MS = 2400

export class MockLiveController implements LiveController {
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
    micPermission: 'granted',
    devices: DEVICES,
    deviceId: null,
    micLabel: null,
    micProblem: null,
    tabLabel: null,
    audio: IDLE_AUDIO,
    events: { state: 'idle', attempt: 0 },
    authLapsed: false,
    gateProviderChoice: null,
    gateSwitching: false,
    live: initialLiveState,
  }
  private listeners = new Set<() => void>()
  private levelListeners = new Set<() => void>()
  private level = 0
  private run = 0
  private seq = 0
  private lastFinalSeq = 0
  private speaking = false
  private silent = false
  private levelTimer: ReturnType<typeof setInterval> | null = null
  private tickTimer: ReturnType<typeof setInterval> | null = null
  private heartbeatTimer: ReturnType<typeof setInterval> | null = null
  private expiryTimer: ReturnType<typeof setInterval> | null = null
  private timeouts = new Set<ReturnType<typeof setTimeout>>()
  private meeting: MockMeeting = TEAM_CHECKIN
  private cast: CastSize = castFromUrl() ?? 4
  private deliverVia: DeliveryChannel[] = ['slack']
  private share = false

  readonly mock: MockControls = {
    simulateSilence: () => {
      this.silent = true
      this.later(1400, () => {
        if (this.silent) this.update({ micProblem: 'silent' })
      })
    },
    dropConnection: () => {
      if (this.snapshot.phase !== 'live') return
      this.update({
        events: { state: 'reconnecting', attempt: 1 },
        audio: { ...this.snapshot.audio, state: 'reconnecting', attempt: 1, bufferedMs: 1800 },
      })
      this.later(2000, () => this.update({ events: { state: 'reconnecting', attempt: 2 }, audio: { ...this.snapshot.audio, attempt: 2, bufferedMs: 3800 } }))
      this.later(4200, () => this.retryConnections())
    },
    cast: () => this.cast,
    replay: (cast?: CastSize) => {
      if (this.snapshot.phase === 'live' || this.snapshot.phase === 'viewing') {
        if (cast && cast !== this.cast && this.snapshot.phase === 'live') {
          this.cast = cast
          const script = CASTS[cast]
          this.meeting = { ...script, id: this.meeting.id, meetCode: script.meetCode ? this.meeting.meetCode || script.meetCode : this.meeting.meetCode }
        }
        this.cancelRun()
        this.dispatch({ type: 'reset' })
        this.seq = 0
        this.lastFinalSeq = 0
        this.update({ startedAt: Date.now() })
        this.openSession(this.snapshot.session?.isOwner ?? true)
      }
    },
  }

  // --- store plumbing ---------------------------------------------------------

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
    const pending = hasPendingTriggers(this.snapshot.live)
    if (pending && this.expiryTimer === null) this.expiryTimer = setInterval(() => this.dispatch({ type: 'tick', now: Date.now() }), 500)
    else if (!pending && this.expiryTimer !== null) {
      clearInterval(this.expiryTimer)
      this.expiryTimer = null
    }
  }

  private emit(...raw: Wire[]): void {
    const events = raw.map(parseLiveEvent).filter((e): e is LiveEvent => e !== null)
    if (events.length) this.dispatch({ type: 'events', events, now: Date.now() })
  }

  private later(ms: number, fn: () => void): void {
    const t = setTimeout(() => {
      this.timeouts.delete(t)
      fn()
    }, ms)
    this.timeouts.add(t)
  }

  private sleep(ms: number, run: number): Promise<void> {
    return new Promise((resolve, reject) => {
      this.later(ms, () => (run === this.run ? resolve() : reject(new Cancelled())))
    })
  }

  private get provider(): GateProviderId {
    return this.snapshot.live.gate.provider ?? this.snapshot.gateProviderChoice ?? 'openai'
  }

  // --- environment -----------------------------------------------------------------

  async probe(): Promise<void> {
    await this.loadConfig()
  }

  loadConfig(): Promise<void> {
    if (!this.snapshot.config) this.update({ config: normalizeConfig(MOCK_CONFIG_WIRE) })
    return Promise.resolve()
  }

  setSource(source: LiveSnapshot['source']): void {
    this.update({ source, tabLabel: source === 'mic' ? null : this.snapshot.tabLabel })
  }

  setGateProviderChoice(provider: GateProviderId | null): void {
    this.update({ gateProviderChoice: provider })
  }

  startGateProvider(): GateProviderId | null {
    return this.snapshot.gateProviderChoice
  }

  async switchGateProvider(provider: GateProviderId): Promise<void> {
    const session = this.snapshot.session
    if (!session?.isOwner || this.snapshot.gateSwitching) return
    this.dispatch({ type: 'gate.provider', provider })
    this.update({ gateSwitching: true })
    await new Promise((r) => setTimeout(r, 450))
    this.emit(
      envelope('session.status', ++this.seq, {
        status: 'live',
        asr_state: 'ok',
        audio_seconds: this.audioSeconds(),
        meeting_id: session.meetingId,
        transcript_id: null,
        error: null,
        gate_provider: provider,
      }),
    )
    this.update({ gateSwitching: false, gateProviderChoice: provider, session: { ...session, gateProvider: provider } })
  }

  setDevice(deviceId: string | null): void {
    const label = DEVICES.find((d) => d.deviceId === deviceId)?.label ?? DEVICES[0].label
    this.update({ deviceId, micLabel: label })
  }

  async startPreview(): Promise<void> {
    this.update({ micActive: true, micLabel: this.snapshot.micLabel ?? DEVICES[0].label })
    this.startLevel()
  }

  async stopPreview(): Promise<void> {
    if (this.snapshot.phase === 'live') return
    this.stopLevel()
    this.update({ micActive: false })
  }

  async shareTab(): Promise<boolean> {
    this.update({ tabLabel: `Meet · ${this.meeting.meetCode}` })
    return true
  }

  // --- lifecycle ---------------------------------------------------------------

  async start(meetUrl: string | null, options: StartOptions = {}): Promise<void> {
    if (this.snapshot.phase !== 'idle' && this.snapshot.phase !== 'ended') return
    this.cancelRun()
    this.dispatch({ type: 'reset' })
    const id = `mock-live-${Date.now().toString(36)}`
    const script = CASTS[this.cast]
    if (meetUrl) {
      const code = /([a-z]{3}-[a-z]{4}-[a-z]{3})/.exec(meetUrl)?.[1] ?? script.meetCode
      this.meeting = { ...script, id, meetCode: code, title: options.title?.trim() || script.title }
    } else {
      // Ad-hoc: no call link, titled by the user or by the time.
      const d = new Date()
      const fallback = `Ad-hoc conversation — ${d.toLocaleString('en-US', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false })}`
      this.meeting = { ...script, id, meetCode: '', title: options.title?.trim() || fallback }
    }
    this.deliverVia = options.deliverVia ?? ['slack']
    this.share = !!options.shareWithParticipants
    this.update({ phase: 'starting', error: null, warning: null, notice: null, discarded: false, endedAt: null })
    if (this.snapshot.source === 'mic+tab' && !this.snapshot.tabLabel) await this.shareTab()
    await this.startPreview()
    await new Promise((r) => setTimeout(r, 650))
    this.seq = 0
    this.lastFinalSeq = 0
    this.update({ phase: 'live', startedAt: Date.now() })
    this.openSession(true)
  }

  async resumeCapture(): Promise<void> {
    if (this.snapshot.phase !== 'viewing' || !this.snapshot.session?.isOwner) return
    this.update({ phase: 'live' })
  }

  async stop(discard = false): Promise<void> {
    const session = this.snapshot.session
    if (!session || !['live', 'viewing'].includes(this.snapshot.phase)) return
    this.update({ phase: 'stopping' })
    this.cancelRun()
    this.stopLevel()
    this.speaking = false
    this.emit(envelope('segment.interim', null, { after_seq: this.lastFinalSeq, speaker: null, text: '' }))
    await new Promise((r) => setTimeout(r, 700))
    const status = discard ? 'cancelled' : 'finalized'
    this.emit(
      envelope('session.status', ++this.seq, {
        status,
        asr_state: 'ok',
        audio_seconds: this.audioSeconds(),
        meeting_id: session.meetingId,
        transcript_id: discard ? null : `t-${session.id}`,
        error: null,
      }),
    )
    const stoppedAt = Date.now()
    if (!discard) mockStore.rememberFinished(this.meeting, this.snapshot.startedAt ?? stoppedAt, stoppedAt, this.provider, this.deliverVia, this.share)
    this.update({
      phase: 'ended',
      endedAt: stoppedAt,
      discarded: discard,
      micActive: false,
      micProblem: null,
      tabLabel: null,
      session: { ...session, status, stoppedAt: new Date(stoppedAt).toISOString() },
      audio: { ...IDLE_AUDIO, state: 'closed' },
    })
  }

  async attach(sessionId: string): Promise<void> {
    if (this.snapshot.session?.id === sessionId) return
    await this.loadConfig()
    const found = mockStore.find(sessionId)
    if (!found) {
      this.update({ error: 'That live session doesn’t exist or you can’t see it.' })
      return
    }
    this.cancelRun()
    this.dispatch({ type: 'reset' })
    this.meeting = found.meeting
    if (found.status === 'live') {
      // Someone else's live call: follow it as a viewer.
      this.seq = 0
      this.lastFinalSeq = 0
      this.update({ phase: 'viewing', startedAt: Date.now() - 4000, endedAt: null })
      this.openSession(false)
      return
    }
    const { events, lastSeq, durationS } = buildStaticEvents(found.meeting, found.startedAt, found.provider)
    const session = normalizeSession(
      sessionWire(found.meeting, { status: 'finalized', startedAt: found.startedAt, stoppedAt: found.stoppedAt, audioSeconds: durationS, lastSeq, gateProvider: found.provider }),
    )
    this.update({ session, phase: 'ended', startedAt: found.startedAt, endedAt: found.stoppedAt })
    this.dispatch({ type: 'session', session })
    this.emit(...events)
  }

  reset(): void {
    if (this.snapshot.phase === 'live' || this.snapshot.phase === 'starting') return
    this.cancelRun()
    this.dispatch({ type: 'reset' })
    this.update({ phase: 'idle', session: null, error: null, warning: null, notice: null, discarded: false, startedAt: null, endedAt: null })
  }

  async renameSpeaker(speakerId: string, displayName: string): Promise<void> {
    if (!this.snapshot.session?.isOwner) return
    this.dispatch({ type: 'speaker.rename', speakerId, displayName })
    await new Promise((r) => setTimeout(r, 250))
    this.emit(envelope('speaker.update', ++this.seq, speakerWire(speakerId, { display_name: displayName, source: 'manual' })))
  }

  retryConnections(): void {
    if (this.snapshot.phase !== 'live' && this.snapshot.phase !== 'viewing') return
    this.update({
      events: { state: 'open', attempt: 0 },
      audio: this.snapshot.phase === 'live' ? { ...IDLE_AUDIO, state: 'open' } : this.snapshot.audio,
    })
  }

  dismissWarning(): void {
    this.update({ warning: null, notice: null })
  }

  dismissError(): void {
    this.update({ error: null })
  }

  onVisible(): void {}

  async retryMic(): Promise<void> {
    this.silent = false
    this.update({ micProblem: null, micActive: true })
    this.startLevel()
  }

  activate(): void {}

  dispose(): void {
    this.cancelRun()
    this.stopLevel()
  }

  // --- the replay ----------------------------------------------------------------

  private audioSeconds(): number {
    return this.snapshot.startedAt ? Math.max(0, (Date.now() - this.snapshot.startedAt) / 1000) : 0
  }

  private openSession(isOwner: boolean): void {
    const run = ++this.run
    const startedAt = this.snapshot.startedAt ?? Date.now()
    const wire = sessionWire(this.meeting, {
      status: 'live',
      startedAt,
      isOwner,
      startedBy: isOwner ? undefined : MOCK_OTHER,
      gateProvider: isOwner ? (this.snapshot.gateProviderChoice ?? 'openai') : 'jev',
      speakers: [],
    })
    const session = normalizeSession(wire)
    this.update({
      session,
      events: { state: 'open', attempt: 0 },
      audio: isOwner ? { ...IDLE_AUDIO, state: 'open' } : IDLE_AUDIO,
    })
    this.emit(envelope('snapshot', null, { session: wire, last_seq: 0, interim: null }))
    if (isOwner) this.startLevel()
    this.tickTimer = setInterval(() => {
      this.emit(envelope('gate.tick', null, tickWire(this.provider)))
    }, TICK_MS)
    this.heartbeatTimer = setInterval(() => {
      this.emit(envelope('session.heartbeat', null, { audio_seconds: this.audioSeconds(), asr_state: 'ok' }))
    }, 5000)
    void this.play(run).catch((e) => {
      if (!(e instanceof Cancelled)) throw e
    })
  }

  private async play(run: number): Promise<void> {
    const finals: { seq: number; s: string; text: string }[] = []
    const names: Record<string, string> = {}
    let n = 0
    await this.sleep(900, run)
    for (const beat of this.meeting.beats) {
      if (beat.t === 'say') {
        await this.sleep(beat.pauseMs ?? LINE_PAUSE_MS, run)
        const words = beat.text.split(/\s+/)
        const start = this.audioSeconds()
        this.speaking = true
        for (let i = 1; i <= words.length; i += 1) {
          const text = words.slice(0, i).join(' ')
          this.emit(
            envelope('segment.interim', null, {
              after_seq: this.lastFinalSeq,
              speaker: beat.s,
              start_s: start,
              end_s: this.audioSeconds(),
              text,
              parts: [{ speaker: beat.s, text }],
            }),
          )
          await this.sleep(WORD_MS + Math.random() * 70, run)
        }
        this.speaking = false
        const seq = ++this.seq
        this.lastFinalSeq = seq
        finals.push({ seq, s: beat.s, text: beat.text })
        this.emit(
          envelope('segment.final', seq, { seq, speaker: beat.s, start_s: start, end_s: this.audioSeconds(), text: beat.text }),
          envelope('segment.interim', null, { after_seq: seq, speaker: null, text: '' }),
        )
      } else if (beat.t === 'intro' || beat.t === 'suggest') {
        await this.sleep(700, run)
        if (beat.t === 'intro') names[beat.s] = beat.name
        this.emit(envelope('speaker.update', ++this.seq, beatSpeakerWire(beat, this.meeting)))
      } else {
        const last = finals[finals.length - 1]
        const from = finals[Math.max(0, finals.length - 1 - beat.back)]
        if (!last) continue
        const triggerId = `${this.meeting.id}-trig-${(n += 1)}`
        const action = beat.action
        const finding = beat.finding
        const answerMs = beat.answerMs
        // The gate decides on its next tick, then an agent claims and answers.
        void (async () => {
          await this.sleep(500, run)
          this.emit(envelope('gate.tick', null, tickWire(this.provider, action, 0.91)))
          this.emit(
            envelope(
              'gate.trigger',
              ++this.seq,
              triggerWire({
                id: triggerId,
                action,
                query: beat.query,
                seqFrom: from.seq,
                seqTo: last.seq,
                provider: this.provider,
                topic: last.text,
                context: finals
                  .slice(-4)
                  .map((f) => `${names[f.s] ?? f.s}: ${f.text}`)
                  .join('\n'),
              }),
            ),
          )
          await this.sleep(650, run)
          this.emit(envelope('gate.claimed', ++this.seq, { trigger_id: triggerId, claimed_by: 'rgs-agent', claimed_at: new Date().toISOString() }))
          await this.sleep(answerMs, run)
          const seq = ++this.seq
          this.emit(envelope('finding.new', seq, findingWire({ id: `${triggerId}-f`, seq, ...finding, relatedSeq: last.seq, triggerId })))
        })().catch(() => {})
      }
    }
  }

  private startLevel(): void {
    if (this.levelTimer !== null) return
    const t0 = performance.now()
    this.levelTimer = setInterval(() => {
      const t = (performance.now() - t0) / 1000
      const target = this.silent
        ? 0
        : this.speaking
          ? 0.32 + 0.42 * Math.abs(Math.sin(t * 7.3)) * (0.6 + Math.random() * 0.4)
          : 0.03 + Math.random() * 0.05
      this.level = target > this.level ? target : this.level * 0.82 + target * 0.18
      this.levelListeners.forEach((l) => l())
    }, 50)
  }

  private stopLevel(): void {
    if (this.levelTimer !== null) clearInterval(this.levelTimer)
    this.levelTimer = null
    this.level = 0
    this.levelListeners.forEach((l) => l())
  }

  private cancelRun(): void {
    this.run += 1
    this.timeouts.forEach((t) => clearTimeout(t))
    this.timeouts.clear()
    if (this.tickTimer !== null) clearInterval(this.tickTimer)
    if (this.heartbeatTimer !== null) clearInterval(this.heartbeatTimer)
    if (this.expiryTimer !== null) clearInterval(this.expiryTimer)
    this.tickTimer = null
    this.heartbeatTimer = null
    this.expiryTimer = null
    this.speaking = false
  }
}
