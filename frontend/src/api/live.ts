/**
 * Live Record API client — the ONE module that knows the wire contract
 * (see the authenticated OpenAPI schema at `/api/openapi.json`).
 *
 * Everything the Live Record page sends or receives goes through here:
 * REST calls, WebSocket URLs + close codes, the uplink audio format, the
 * HTTP fallbacks, and the mapping from wire shapes to the normalized types
 * the reducer and UI consume. When the contract changes (additively, per
 * its own rules), this is the only file that should need to.
 *
 * Normalizers are tolerant by design — the contract says clients MUST
 * ignore unknown fields and unknown event types: anything unknown or
 * malformed maps to `null` (dropped) instead of throwing in a socket
 * handler.
 */
import { ApiError, apiBase, fetchApi } from '@/api/client'
import { parseOnDevicePhrase, type OnDevicePhrase } from '@/lib/asr/onDevice'

// ---------------------------------------------------------------------------
// Audio uplink format (§2). `GET /live/config` may restate these; the
// defaults below are the contract's fixed values.
// ---------------------------------------------------------------------------

export interface LiveAudioFormat {
  encoding: 'pcm_s16le'
  sampleRate: number
  channels: 1
  frameMs: number
  maxFrameBytes: number
  /** Keep at least this much un-acked audio for resume. */
  clientBufferS: number
}

export const DEFAULT_AUDIO_FORMAT: LiveAudioFormat = {
  encoding: 'pcm_s16le',
  sampleRate: 16_000,
  channels: 1,
  frameMs: 100,
  maxFrameBytes: 65_536,
  clientBufferS: 60,
}

export const BYTES_PER_SAMPLE = 2
/** HTTP fallback body cap (§2: ≤ 1 MiB). */
export const HTTP_AUDIO_MAX_BYTES = 1_048_576

export const frameSamples = (f: LiveAudioFormat) => Math.round((f.sampleRate * f.frameMs) / 1000)

// ---------------------------------------------------------------------------
// Normalized domain types
// ---------------------------------------------------------------------------

export type LiveSessionStatus = 'live' | 'stopping' | 'finalized' | 'failed' | 'cancelled'
export type AsrState = 'ok' | 'degraded' | 'unavailable'

export const isTerminalStatus = (s: LiveSessionStatus | null | undefined): boolean =>
  s === 'finalized' || s === 'failed' || s === 'cancelled'

export interface LivePerson {
  id: string | null
  name: string | null
  email: string | null
}

/** Which engine decides when the agent should act (`LIVE_GATE_PROVIDER`).
 *  Known ids are `jev` and `openai`; future ids pass through as strings. */
export type GateProviderId = string

export interface LiveGateProvider {
  id: GateProviderId
  label: string
  /** Usable on this deployment (its key is configured). */
  available: boolean
}

/** Something the live decision gate listens for, with phrases that trip it. */
export interface LiveGateAction {
  id: string
  label: string
  description: string
  examples: string[]
}

export interface LiveGateConfig {
  enabled: boolean
  defaultProvider: GateProviderId | null
  /** Empty when the server predates provider switching (no switch shown). */
  providers: LiveGateProvider[]
  intervalS: number | null
  actions: LiveGateAction[]
  /** `false` when `config.gate` was absent and these are client defaults. */
  fromServer: boolean
}

export interface LiveConfig {
  enabled: boolean
  asrConfigured: boolean
  storageConfigured: boolean
  websockets: boolean
  audio: LiveAudioFormat
  maxSessionHours: number | null
  findingsPerMinute: number | null
  gate: LiveGateConfig
  /** What the recap can do on this server (`config.recap`); null = server predates recaps. */
  recap: LiveRecapConfig | null
}

// ---------------------------------------------------------------------------
// Recap contract for optional publisher overlays; disabled in the core.
// ---------------------------------------------------------------------------

export type SessionKind = 'call' | 'adhoc'
export type DeliveryChannel = 'slack' | 'email'
export const DELIVERY_CHANNELS: readonly DeliveryChannel[] = ['slack', 'email']
export type RecapStatus = 'pending' | 'writing' | 'printing' | 'delivering' | 'sharing' | 'done' | 'failed' | 'skipped'
export const RECAP_INFLIGHT: readonly RecapStatus[] = ['pending', 'writing', 'printing', 'delivering', 'sharing']

export interface LiveRecapConfig {
  enabled: boolean
  newspapers: boolean
  llm: boolean
  channels: { id: DeliveryChannel; available: boolean }[]
  defaultChannels: DeliveryChannel[]
}

export interface RecapEdition {
  id: string | null
  number: number | null
  status: string | null
  paperName: string | null
  /** Optional link to a configured hosted recap. */
  shareUrl: string | null
  headline: string | null
  dek: string | null
  publishedAt: string | null
  error: string | null
}

export interface RecapDelivery {
  status: 'sent' | 'failed' | 'skipped' | string
  error: string | null
  at: string | null
}

export interface LiveRecap {
  enabled: boolean
  /** null = no recap ran (recap off, or a session from before recaps). */
  status: RecapStatus | null
  deliverVia: DeliveryChannel[]
  headline: string | null
  summary: string | null
  hasNotes: boolean
  edition: RecapEdition | null
  delivery: Partial<Record<DeliveryChannel, RecapDelivery>>
  /** Opt-in: the recap also goes to the people RGS recognised (off by default). */
  shareWithParticipants: boolean
  /** Who it was shared with, and how that went (one row per person). */
  shared: RecapShare[]
  error: string | null
  updatedAt: string | null
}

/** One participant the recap was shared with. `partial` = one channel of two. */
export interface RecapShare {
  email: string
  name: string
  status: 'sent' | 'partial' | 'failed' | 'skipped' | string
  channels: Partial<Record<DeliveryChannel, string | null>>
  error: string | null
  at: string | null
}

/** A speaker's optional local-directory identity. */
export interface LiveSpeakerPerson {
  email: string
  name: string | null
  slackId: string | null
  /** Optional external chat display name, when an adapter supplies one. */
  slackName: string | null
}

/** Someone a share would go to: recognised, spoke, a company address, not the owner. */
export interface SessionParticipant extends LiveSpeakerPerson {
  name: string
  /** The speaker label RGS heard them as. */
  label: string | null
  /** How sharing with them went, once it ran. */
  shareStatus: string | null
}

/** How a speaker got its current name. `self_intro` = they introduced
 *  themselves ("Hi, I'm Alice") and the server renamed the label live. */
export type SpeakerNameSource = 'self_intro' | 'manual' | 'voiceprint' | string

export interface LiveSpeaker {
  /** The diarization label ("S2") — the speaker's id everywhere. */
  id: string
  /** "Speaker 2", or a manual / confident name. */
  displayName: string | null
  /** 0-based, stable palette slot from the server. */
  colorIndex: number | null
  speechSeconds: number | null
  /** Bound person (manual rename to a known user). */
  user: LivePerson | null
  /** Locally known person behind the name when verified. */
  person: LiveSpeakerPerson | null
  /** Voiceprint guess, if any. */
  suggestion: { userId: string | null; name: string; email: string | null; score: number | null } | null
  /** Label this cluster was merged into; its lines render as that speaker. */
  mergedInto: string | null
  /** Where the current name came from (`speaker.update.source`), if said. */
  nameSource: SpeakerNameSource | null
  /** Confidence (0–1) of an automatic name. */
  nameConfidence: number | null
  /** The words that named them, e.g. "Hi, I'm Alice". */
  nameEvidence: string | null
}

export interface LiveSessionInfo {
  id: string
  meetingId: string | null
  meetCode: string | null
  meetUrl: string | null
  title: string | null
  status: LiveSessionStatus
  /** Caller may stream audio, stop and rename speakers. */
  isOwner: boolean
  /** The owner shared the recap with this viewer: read only. */
  sharedWithYou: boolean
  linkedCalendarEvent: boolean
  startedBy: LivePerson | null
  startedAt: string | null
  stoppedAt: string | null
  audioSeconds: number | null
  lastSeq: number
  asrState: AsrState | null
  transcriptId: string | null
  error: string | null
  speakers: LiveSpeaker[]
  /** Active decision-gate engine (`gate_provider`), when the server says. */
  gateProvider: GateProviderId | null
  /** `adhoc` = an in-person conversation without a call link. */
  kind: SessionKind
  recap: LiveRecap | null
  /** Invited people (calendar roster), offered as names when renaming. */
  attendees: { name: string | null; email: string | null }[]
  /** Who a share would go to (detail responses only; [] otherwise). */
  participants: SessionParticipant[]
}

/** A transcript line. Finals use their event `seq` as id (`String(seq)`);
 *  the interim tail renders as unstable pseudo-segments (`interim:N`). */
export interface LiveSegment {
  id: string
  seq: number | null
  speakerId: string | null
  text: string
  startMs: number
  endMs: number | null
  final: boolean
}

/** The unstable tail, replaced wholesale on every update. */
export interface LiveInterim {
  afterSeq: number | null
  speakerId: string | null
  text: string
  startMs: number
  endMs: number | null
  parts: { speakerId: string | null; text: string }[]
}

export type FindingLinkKind = 'meeting' | 'decision' | 'action_item' | 'project' | 'url'

export interface FindingLink {
  title: string
  url: string
  kind: FindingLinkKind
  /** Router path when the URL points at this app (opens in-app). */
  internalPath: string | null
}

export interface LiveFinding {
  id: string
  seq: number | null
  title: string
  bodyMarkdown: string
  links: FindingLink[]
  /** Final segment (seq) that prompted it. */
  relatedSeq: number | null
  /** Gate trigger it answers — resolves that pending card in place. */
  triggerId: string | null
  sourceAgent: string
  createdBy: LivePerson | null
  createdAt: string | null
}

export type GateAction = 'lookup_past' | 'fact_check' | 'find_owner' | 'recall_action_items' | 'define_term'

export const GATE_ACTIONS: readonly GateAction[] = [
  'lookup_past',
  'fact_check',
  'find_owner',
  'recall_action_items',
  'define_term',
]

export type TriggerStatus = 'pending' | 'claimed' | 'answered' | 'expired'

export interface GateTrigger {
  id: string
  seq: number | null
  action: GateAction
  /** The raw action id from the wire (may be newer than `GateAction`). */
  actionId: string
  /** Decision engine that fired it (`jev` / `openai`), when sent. */
  provider: GateProviderId | null
  query: string
  /** Inclusive range of final-segment seqs that prompted the trigger. */
  seqFrom: number | null
  seqTo: number | null
  probability: number | null
  jevLatencyMs: number | null
  /** Optional context attached by the Jev gate (§3/§5). */
  anchorSeq: number | null
  topic: string | null
  contextText: string | null
  decidedAt: string | null
  status: TriggerStatus
  claimedBy: string | null
  findingId: string | null
  createdAt: string | null
}

/** `gate.tick`: one decision-gate check. Ephemeral heartbeat that proves
 *  the agent is listening and shows its last call. */
export interface GateTick {
  provider: GateProviderId | null
  latencyMs: number | null
  consideredLines: number | null
  /** `"none"` or the action id it decided on. */
  decision: string | null
  probability: number | null
}

interface EventBase {
  seq: number | null
  /** Server emit time (epoch ms) from the envelope `ts`. */
  ts: number | null
}

export type LiveEvent = EventBase &
  (
    | { type: 'snapshot'; session: LiveSessionInfo; lastSeq: number; interim: LiveInterim | null }
    | { type: 'segment.final'; segment: LiveSegment }
    | { type: 'segment.interim'; interim: LiveInterim }
    | { type: 'ondevice.segment'; phrase: OnDevicePhrase }
    | { type: 'speaker.update'; speaker: LiveSpeaker }
    | { type: 'finding.new'; finding: LiveFinding }
    | { type: 'gate.trigger'; trigger: GateTrigger }
    | { type: 'gate.claimed'; triggerId: string; claimedBy: string | null }
    | { type: 'gate.tick'; tick: GateTick }
    | {
        type: 'session.status'
        status: LiveSessionStatus
        asrState: AsrState | null
        audioSeconds: number | null
        meetingId: string | null
        transcriptId: string | null
        error: string | null
        gateProvider: GateProviderId | null
      }
    | { type: 'session.heartbeat'; audioSeconds: number | null; asrState: AsrState | null }
    | { type: 'resync'; lastSeq: number | null }
    | { type: 'pong' }
    | { type: 'error'; code: string | null; closeCode: number | null; message: string | null }
  )

// ---------------------------------------------------------------------------
// Wire → normalized mapping
// ---------------------------------------------------------------------------

type Obj = Record<string, unknown>
const isObj = (v: unknown): v is Obj => typeof v === 'object' && v !== null && !Array.isArray(v)
const str = (v: unknown): string | null => (typeof v === 'string' && v.length > 0 ? v : null)
const num = (v: unknown): number | null => {
  if (typeof v === 'number' && Number.isFinite(v)) return v
  if (typeof v === 'string' && v.trim() !== '' && Number.isFinite(Number(v))) return Number(v)
  return null
}
const int = (v: unknown): number | null => {
  const n = num(v)
  return n === null ? null : Math.trunc(n)
}
const idOf = (v: unknown): string | null => (typeof v === 'number' ? String(v) : str(v))
/** Contract offsets are float seconds from session audio start. */
const secondsToMs = (v: unknown): number | null => {
  const s = num(v)
  return s === null ? null : Math.round(s * 1000)
}
const timeMs = (v: unknown): number | null => {
  const s = str(v)
  if (!s) return null
  const t = Date.parse(s)
  return Number.isFinite(t) ? t : null
}

const STATUSES: readonly LiveSessionStatus[] = ['live', 'stopping', 'finalized', 'failed', 'cancelled']
function normalizeStatus(v: unknown): LiveSessionStatus | null {
  const s = str(v)
  return s && (STATUSES as readonly string[]).includes(s) ? (s as LiveSessionStatus) : null
}

const ASR_STATES: readonly AsrState[] = ['ok', 'degraded', 'unavailable']
function normalizeAsr(v: unknown): AsrState | null {
  const s = str(v)
  return s && (ASR_STATES as readonly string[]).includes(s) ? (s as AsrState) : null
}

function normalizePerson(v: unknown): LivePerson | null {
  if (!isObj(v)) return null
  const p = { id: idOf(v.id) ?? idOf(v.user_id), name: str(v.name), email: str(v.email) }
  return p.id || p.name || p.email ? p : null
}

export function normalizeConfig(raw: unknown): LiveConfig {
  const o = isObj(raw) ? raw : {}
  const a = isObj(o.audio) ? o.audio : {}
  const limits = isObj(o.limits) ? o.limits : {}
  const sampleRate = int(a.sample_rate)
  return {
    enabled: o.enabled === true,
    asrConfigured: o.asr_configured !== false,
    storageConfigured: o.storage_configured !== false,
    websockets: o.websockets !== false,
    audio: {
      ...DEFAULT_AUDIO_FORMAT,
      // The capture worklet only produces pcm_s16le mono; a different
      // encoding would be a contract break, not something to adapt to.
      sampleRate: sampleRate && sampleRate >= 8000 && sampleRate <= 48_000 ? sampleRate : DEFAULT_AUDIO_FORMAT.sampleRate,
      frameMs: clampInt(a.recommended_frame_ms, 20, 1000, DEFAULT_AUDIO_FORMAT.frameMs),
      maxFrameBytes: clampInt(a.max_frame_bytes, 1024, 1 << 20, DEFAULT_AUDIO_FORMAT.maxFrameBytes),
      clientBufferS: clampInt(a.client_buffer_s, 10, 3600, DEFAULT_AUDIO_FORMAT.clientBufferS),
    },
    maxSessionHours: num(limits.max_session_hours),
    findingsPerMinute: num(limits.findings_per_minute),
    gate: normalizeGateConfig(o.gate),
    recap: normalizeRecapConfig(o.recap),
  }
}

/** Client-side copy of the contract's gate actions. Used when the server
 *  sends no `config.gate` (older backend) and to fill any action the server
 *  lists without a label, description or examples. */
export const DEFAULT_GATE_ACTIONS: readonly LiveGateAction[] = [
  {
    id: 'find_owner',
    label: 'Find the owner',
    description: 'Ask who owns, leads or is the contact for something.',
    examples: ['Who owns the sample project?', 'Who writes the demo docs?'],
  },
  {
    id: 'lookup_past',
    label: 'Look up past meetings',
    description: 'Ask whether something was already discussed or decided.',
    examples: ['Didn’t we decide on the demo schedule?', 'What did we say last week about the tutorial?'],
  },
  {
    id: 'fact_check',
    label: 'Fact-check',
    description: 'Question a date, number or claim against what was said before.',
    examples: ['Wasn’t the ship date the 15th?', 'Is that the number we agreed on?'],
  },
  {
    id: 'recall_action_items',
    label: 'Recall action items',
    description: 'Ask what is still open, or who was supposed to do what.',
    examples: ['What were the action items from last time?', 'What’s still open on the site survey?'],
  },
  {
    id: 'define_term',
    label: 'Define a term',
    description: 'Ask what an acronym or internal name means.',
    examples: ['What does API stand for?', 'What do we mean by a test fixture?'],
  },
  {
    id: 'speaker_intro',
    label: 'Name yourself',
    description: 'Say who you are and your label becomes your name.',
    examples: ['Hi, I’m Alice.', 'This is Bob from the example team.'],
  },
]

/** Fallback labels for known providers (the server's label wins). */
export const GATE_PROVIDER_LABELS: Record<string, string> = {
  jev: 'TypeSafe Jev',
  openai: 'OpenAI Decisions',
}

export const gateProviderLabel = (id: GateProviderId | null | undefined, providers: readonly LiveGateProvider[] = []): string | null =>
  !id ? null : providers.find((p) => p.id === id)?.label ?? GATE_PROVIDER_LABELS[id] ?? id

function normalizeGateAction(raw: unknown): LiveGateAction | null {
  if (!isObj(raw)) return null
  const id = str(raw.id)
  if (!id) return null
  const fallback = DEFAULT_GATE_ACTIONS.find((a) => a.id === id)
  const examples = Array.isArray(raw.examples)
    ? raw.examples.filter((e): e is string => typeof e === 'string' && e.trim().length > 0).map((e) => e.trim())
    : []
  return {
    id,
    label: str(raw.label) ?? fallback?.label ?? humanizeId(id),
    description: str(raw.description) ?? fallback?.description ?? '',
    examples: examples.length > 0 ? examples : [...(fallback?.examples ?? [])],
  }
}

const humanizeId = (id: string) => {
  const words = id.replace(/[_-]+/g, ' ').trim()
  return words.charAt(0).toUpperCase() + words.slice(1)
}

export function normalizeGateConfig(raw: unknown): LiveGateConfig {
  if (!isObj(raw)) {
    return {
      enabled: true,
      defaultProvider: null,
      providers: [],
      intervalS: null,
      actions: DEFAULT_GATE_ACTIONS.map((a) => ({ ...a, examples: [...a.examples] })),
      fromServer: false,
    }
  }
  const providers = Array.isArray(raw.providers)
    ? raw.providers.flatMap((p): LiveGateProvider[] => {
        if (!isObj(p)) return []
        const id = str(p.id)
        if (!id) return []
        return [{ id, label: str(p.label) ?? GATE_PROVIDER_LABELS[id] ?? id, available: p.available !== false }]
      })
    : []
  const actions = Array.isArray(raw.actions)
    ? raw.actions.map(normalizeGateAction).filter((a): a is LiveGateAction => a !== null)
    : []
  const interval = num(raw.interval_s)
  return {
    enabled: raw.enabled !== false,
    defaultProvider: str(raw.default_provider) ?? providers.find((p) => p.available)?.id ?? null,
    providers,
    intervalS: interval !== null && interval > 0 ? interval : null,
    actions: actions.length > 0 ? actions : DEFAULT_GATE_ACTIONS.map((a) => ({ ...a, examples: [...a.examples] })),
    fromServer: true,
  }
}

function clampInt(v: unknown, min: number, max: number, fallback: number): number {
  const n = int(v)
  return n === null ? fallback : Math.min(max, Math.max(min, n))
}

export function normalizeSpeaker(raw: unknown): LiveSpeaker | null {
  if (!isObj(raw)) return null
  const id = idOf(raw.label)
  if (!id) return null
  const sug = isObj(raw.suggestion) ? raw.suggestion : null
  const sugName = sug ? str(sug.name) : null
  return {
    id,
    displayName: str(raw.display_name),
    colorIndex: int(raw.color_index),
    speechSeconds: num(raw.speech_seconds),
    user: normalizePerson(raw.user),
    person: normalizeSpeakerPerson(raw.person),
    suggestion:
      sug && sugName
        ? { userId: idOf(sug.user_id), name: sugName, email: str(sug.email), score: num(sug.score) }
        : null,
    mergedInto: idOf(raw.merged_into),
    nameSource: str(raw.source) ?? str(raw.name_source),
    nameConfidence: num(raw.confidence) ?? num(raw.name_confidence),
    nameEvidence: str(raw.evidence) ?? str(raw.name_evidence),
  }
}

function normalizeSpeakerPerson(v: unknown): LiveSpeakerPerson | null {
  if (!isObj(v)) return null
  const email = str(v.email)
  return email ? { email, name: str(v.name), slackId: str(v.slack_id), slackName: str(v.slack_name) } : null
}

function normalizeParticipant(v: unknown): SessionParticipant | null {
  const person = normalizeSpeakerPerson(v)
  if (!person || !isObj(v)) return null
  return { ...person, name: person.name ?? person.email, label: str(v.label), shareStatus: str(v.share_status) }
}

function normalizeShare(v: unknown): RecapShare | null {
  if (!isObj(v)) return null
  const email = str(v.email)
  if (!email) return null
  const channels: Partial<Record<DeliveryChannel, string | null>> = {}
  if (isObj(v.channels)) for (const ch of DELIVERY_CHANNELS) if (ch in v.channels) channels[ch] = str(v.channels[ch])
  return { email, name: str(v.name) ?? email, status: str(v.status) ?? 'failed', channels, error: str(v.error), at: str(v.at) }
}

export function normalizeSession(raw: unknown): LiveSessionInfo {
  if (!isObj(raw)) throw new Error('Malformed live session response')
  const id = idOf(raw.id)
  if (!id) throw new Error('Live session response has no id')
  return {
    id,
    meetingId: idOf(raw.meeting_id),
    meetCode: str(raw.meet_code),
    meetUrl: str(raw.meet_url),
    title: str(raw.title),
    status: normalizeStatus(raw.status) ?? 'live',
    isOwner: raw.is_owner === true,
    sharedWithYou: raw.shared_with_you === true,
    linkedCalendarEvent: raw.linked_calendar_event === true,
    startedBy: normalizePerson(raw.started_by),
    startedAt: str(raw.started_at),
    stoppedAt: str(raw.stopped_at),
    audioSeconds: num(raw.audio_seconds),
    lastSeq: int(raw.last_seq) ?? 0,
    asrState: normalizeAsr(raw.asr_state),
    transcriptId: idOf(raw.transcript_id),
    error: str(raw.error),
    speakers: Array.isArray(raw.speakers)
      ? raw.speakers.map(normalizeSpeaker).filter((s): s is LiveSpeaker => s !== null)
      : [],
    gateProvider: str(raw.gate_provider),
    kind: raw.kind === 'adhoc' || (raw.kind == null && !str(raw.meet_url) && !str(raw.meet_code)) ? 'adhoc' : 'call',
    recap: normalizeRecap(raw.recap),
    attendees: Array.isArray(raw.attendees)
      ? raw.attendees.flatMap((a) => {
          const name = isObj(a) ? str(a.name) : typeof a === 'string' ? a.trim() || null : null
          const email = isObj(a) ? str(a.email) : null
          return name || email ? [{ name, email }] : []
        })
      : [],
    participants: Array.isArray(raw.participants)
      ? raw.participants.map(normalizeParticipant).filter((p): p is SessionParticipant => p !== null)
      : [],
  }
}

const isChannel = (v: unknown): v is DeliveryChannel => v === 'slack' || v === 'email'
const RECAP_STATUSES: readonly string[] = [...RECAP_INFLIGHT, 'done', 'failed', 'skipped']

export function normalizeRecap(raw: unknown): LiveRecap | null {
  if (!isObj(raw)) return null
  const ed = isObj(raw.edition) ? raw.edition : null
  const delivery: Partial<Record<DeliveryChannel, RecapDelivery>> = {}
  if (isObj(raw.delivery)) {
    for (const ch of DELIVERY_CHANNELS) {
      const d = raw.delivery[ch]
      if (isObj(d)) delivery[ch] = { status: str(d.status) ?? 'failed', error: str(d.error), at: str(d.at) }
    }
  }
  const status = str(raw.status)
  return {
    enabled: raw.enabled !== false,
    status: status && RECAP_STATUSES.includes(status) ? (status as RecapStatus) : null,
    deliverVia: Array.isArray(raw.deliver_via) ? raw.deliver_via.filter(isChannel) : [],
    headline: str(raw.headline),
    summary: str(raw.summary),
    hasNotes: raw.has_notes === true,
    edition: ed
      ? {
          id: idOf(ed.id),
          number: int(ed.number),
          status: str(ed.status),
          paperName: str(ed.paper_name),
          shareUrl: str(ed.share_url),
          headline: str(ed.headline),
          dek: str(ed.dek),
          publishedAt: str(ed.published_at),
          error: str(ed.error),
        }
      : null,
    delivery,
    shareWithParticipants: raw.share_with_participants === true,
    shared: Array.isArray(raw.shared) ? raw.shared.map(normalizeShare).filter((r): r is RecapShare => r !== null) : [],
    error: str(raw.error),
    updatedAt: str(raw.updated_at),
  }
}

export function normalizeRecapConfig(raw: unknown): LiveRecapConfig | null {
  if (!isObj(raw)) return null
  const channels = Array.isArray(raw.channels)
    ? raw.channels.flatMap((c) => (isObj(c) && isChannel(c.id) ? [{ id: c.id, available: c.available !== false }] : []))
    : []
  const defaults = Array.isArray(raw.default_channels) ? raw.default_channels.filter(isChannel) : []
  return {
    enabled: raw.enabled !== false,
    newspapers: raw.newspapers === true,
    llm: raw.llm === true,
    channels,
    defaultChannels: defaults.length ? defaults : ['slack'],
  }
}

export interface RecapNotes {
  headline: string | null
  summary: string | null
  keyPoints: string[]
  decisions: string[]
  actionItems: { owner: string; task: string; due: string | null }[]
  quotes: { speaker: string | null; quote: string }[]
  speakers: { name: string; contribution: string | null }[]
  openQuestions: string[]
  topics: string[]
}

const strList = (v: unknown): string[] =>
  Array.isArray(v) ? v.filter((x): x is string => typeof x === 'string' && x.trim().length > 0) : []

export function normalizeNotes(raw: unknown): RecapNotes | null {
  if (!isObj(raw)) return null
  const rows = (v: unknown) => (Array.isArray(v) ? v.filter(isObj) : [])
  return {
    headline: str(raw.headline),
    summary: str(raw.summary),
    keyPoints: strList(raw.key_points),
    decisions: strList(raw.decisions),
    actionItems: rows(raw.action_items).flatMap((a) => {
      const task = str(a.task)
      return task ? [{ owner: str(a.owner) ?? 'Unassigned', task, due: str(a.due) }] : []
    }),
    quotes: rows(raw.quotes).flatMap((q) => {
      const quote = str(q.quote)
      return quote ? [{ speaker: str(q.speaker), quote }] : []
    }),
    speakers: rows(raw.speakers).flatMap((sp) => {
      const name = str(sp.name)
      return name ? [{ name, contribution: str(sp.contribution) }] : []
    }),
    openQuestions: strList(raw.open_questions),
    topics: strList(raw.topics),
  }
}

/** The edition is still being written / printed / sent. */
export const recapInFlight = (r: LiveRecap | null | undefined): boolean => !!r?.status && RECAP_INFLIGHT.includes(r.status)

/** `Segment` → final line. `seq` is the segment's id everywhere. */
export function normalizeSegment(raw: unknown, envelopeSeq: number | null = null): LiveSegment | null {
  if (!isObj(raw)) return null
  const seq = int(raw.seq) ?? envelopeSeq
  const text = typeof raw.text === 'string' ? raw.text : null
  if (seq === null || text === null) return null
  return {
    id: String(seq),
    seq,
    speakerId: idOf(raw.speaker),
    text,
    startMs: secondsToMs(raw.start_s) ?? 0,
    endMs: secondsToMs(raw.end_s),
    final: true,
  }
}

export function normalizeInterim(raw: unknown): LiveInterim | null {
  if (!isObj(raw)) return null
  const text = typeof raw.text === 'string' ? raw.text : ''
  const speakerId = idOf(raw.speaker)
  const parts = Array.isArray(raw.parts)
    ? raw.parts
        .filter(isObj)
        .map((p) => ({ speakerId: idOf(p.speaker), text: typeof p.text === 'string' ? p.text : '' }))
        .filter((p) => p.text.trim().length > 0)
    : []
  return {
    afterSeq: int(raw.after_seq),
    speakerId,
    text,
    startMs: secondsToMs(raw.start_s) ?? 0,
    endMs: secondsToMs(raw.end_s),
    parts: parts.length > 0 ? parts : text.trim() ? [{ speakerId, text }] : [],
  }
}

/** Infer what a finding URL points at. Knowledge links (external archives,
 *  decisions, action items, projects) get a kind for their icon and open in
 *  their own app; only links back into RGS itself (`/sessions/…`, `/live/…`)
 *  get an in-app route. */
export function classifyLink(
  url: string,
  knowledgeOrigins: readonly string[] = KNOWLEDGE_ORIGINS,
  appOrigin: string | null = typeof window !== 'undefined' ? window.location.origin : null,
): { kind: FindingLinkKind; internalPath: string | null } {
  let parsed: URL
  try {
    parsed = new URL(url)
  } catch {
    return { kind: 'url', internalPath: null }
  }
  const path = parsed.pathname
  if (appOrigin && parsed.origin === appOrigin && /^\/(sessions|live)(\/|$)/.test(path)) {
    return { kind: 'url', internalPath: `${path}${parsed.search}${parsed.hash}` }
  }
  if (!knowledgeOrigins.includes(parsed.origin)) return { kind: 'url', internalPath: null }
  if (/^\/meetings\/[^/]+/.test(path)) return { kind: 'meeting', internalPath: null }
  if (/^\/decisions\/[^/]+/.test(path)) return { kind: 'decision', internalPath: null }
  if (/^\/action-items/.test(path)) return { kind: 'action_item', internalPath: null }
  if (/^\/projects\/[^/]+/.test(path)) return { kind: 'project', internalPath: null }
  return { kind: 'url', internalPath: null }
}

/** Where grounded answers come from (meeting memory). */
export const KNOWLEDGE_ORIGINS: readonly string[] = []

function normalizeLink(raw: unknown): FindingLink | null {
  if (!isObj(raw)) return null
  const url = str(raw.url)
  if (!url || !/^https?:\/\//i.test(url)) return null
  return { title: str(raw.title) ?? url, url, ...classifyLink(url) }
}

export function normalizeFinding(raw: unknown, envelopeSeq: number | null = null): LiveFinding | null {
  if (!isObj(raw)) return null
  const id = idOf(raw.id)
  const title = str(raw.title)
  if (!id || !title) return null
  return {
    id,
    seq: int(raw.seq) ?? envelopeSeq,
    title,
    bodyMarkdown: typeof raw.body_md === 'string' ? raw.body_md : '',
    links: Array.isArray(raw.links) ? raw.links.map(normalizeLink).filter((l): l is FindingLink => l !== null) : [],
    relatedSeq: int(raw.related_seq),
    triggerId: idOf(raw.trigger_id),
    sourceAgent: str(raw.source_agent) ?? 'agent',
    createdBy: normalizePerson(raw.created_by),
    createdAt: str(raw.created_at),
  }
}

const TRIGGER_STATUSES: readonly TriggerStatus[] = ['pending', 'claimed', 'answered', 'expired']
export function normalizeTrigger(raw: unknown, envelopeSeq: number | null = null): GateTrigger | null {
  if (!isObj(raw)) return null
  const id = idOf(raw.trigger_id)
  const action = str(raw.action)
  if (!id || !action) return null
  const status = str(raw.status)
  return {
    id,
    seq: int(raw.seq) ?? envelopeSeq,
    // Unknown future actions still render (generic lookup icon).
    action: (GATE_ACTIONS as readonly string[]).includes(action) ? (action as GateAction) : 'lookup_past',
    actionId: action,
    provider: str(raw.provider) ?? str(raw.gate_provider),
    query: str(raw.query) ?? '',
    seqFrom: int(raw.seq_from),
    seqTo: int(raw.seq_to),
    probability: num(raw.probability),
    jevLatencyMs: num(raw.jev_latency_ms),
    anchorSeq: int(raw.anchor_seq),
    topic: str(raw.topic),
    contextText: str(raw.context_text),
    decidedAt: str(raw.decided_at),
    status: status && (TRIGGER_STATUSES as readonly string[]).includes(status) ? (status as TriggerStatus) : 'pending',
    claimedBy: str(raw.claimed_by),
    findingId: idOf(raw.finding_id),
    createdAt: str(raw.created_at),
  }
}

/** Map one envelope `{seq, type, ts, data}` to a normalized event, or
 *  `null` for unknown types / malformed payloads. */
export function parseLiveEvent(raw: unknown): LiveEvent | null {
  if (!isObj(raw)) return null
  const type = str(raw.type)
  if (!type) return null
  const seq = int(raw.seq)
  const ts = timeMs(raw.ts)
  const data: Obj = isObj(raw.data) ? raw.data : {}
  const base = { seq, ts }
  switch (type) {
    case 'snapshot': {
      if (!isObj(data.session)) return null
      let session: LiveSessionInfo
      try {
        session = normalizeSession(data.session)
      } catch {
        return null
      }
      return {
        ...base,
        type,
        session,
        lastSeq: int(data.last_seq) ?? session.lastSeq,
        interim: normalizeInterim(data.interim),
      }
    }
    case 'segment.final': {
      const segment = normalizeSegment(data, seq)
      return segment ? { ...base, type, segment } : null
    }
    case 'ondevice.segment': {
      const phrase = parseOnDevicePhrase(data)
      return phrase ? { ...base, type, phrase } : null
    }
    case 'segment.interim': {
      const interim = normalizeInterim(data)
      return interim ? { ...base, type, interim } : null
    }
    case 'speaker.update': {
      const speaker = normalizeSpeaker(data)
      return speaker ? { ...base, type, speaker } : null
    }
    case 'finding.new': {
      const finding = normalizeFinding(data, seq)
      return finding ? { ...base, type, finding } : null
    }
    case 'gate.trigger': {
      const trigger = normalizeTrigger(data, seq)
      return trigger ? { ...base, type, trigger } : null
    }
    case 'gate.claimed': {
      const triggerId = idOf(data.trigger_id)
      return triggerId ? { ...base, type, triggerId, claimedBy: str(data.claimed_by) } : null
    }
    case 'gate.tick':
      return {
        ...base,
        type,
        tick: {
          provider: str(data.provider),
          latencyMs: num(data.latency_ms),
          consideredLines: int(data.considered_lines),
          decision: str(data.decision),
          probability: num(data.p) ?? num(data.probability),
        },
      }
    case 'session.status': {
      const status = normalizeStatus(data.status)
      if (!status) return null
      return {
        ...base,
        type,
        status,
        asrState: normalizeAsr(data.asr_state),
        audioSeconds: num(data.audio_seconds),
        meetingId: idOf(data.meeting_id),
        transcriptId: idOf(data.transcript_id),
        error: str(data.error),
        gateProvider: str(data.gate_provider),
      }
    }
    case 'session.heartbeat':
      return { ...base, type, audioSeconds: num(data.audio_seconds), asrState: normalizeAsr(data.asr_state) }
    case 'resync':
      return { ...base, type, lastSeq: int(data.last_seq) }
    case 'pong':
      return { ...base, type }
    case 'error':
      // Error frames use the §2 close fields at the top level, while
      // events still carry the §3 envelope's seq (null) and ts.
      return {
        ...base,
        type,
        code: str(raw.code) ?? str(data.code),
        closeCode: int(raw.close_code) ?? int(data.close_code),
        message: str(raw.message) ?? str(data.message),
      }
    default:
      return null
  }
}

/** Parse one events-socket text frame. */
export function parseLiveEventMessage(data: string): LiveEvent[] {
  let parsed: unknown
  try {
    parsed = JSON.parse(data)
  } catch {
    return []
  }
  const items = Array.isArray(parsed) ? parsed : [parsed]
  return items.map(parseLiveEvent).filter((e): e is LiveEvent => e !== null)
}

/** Text frames the server sends on the AUDIO socket (§2). */
export type AudioServerMessage =
  | { type: 'ready'; receivedSamples: number }
  | { type: 'ack'; receivedSamples: number }
  | { type: 'pong' }
  | { type: 'error'; code: string | null; closeCode: number | null; message: string; ts: number | null }

export function parseAudioServerMessage(data: string): AudioServerMessage | null {
  let o: unknown
  try {
    o = JSON.parse(data)
  } catch {
    return null
  }
  if (!isObj(o)) return null
  switch (o.type) {
    case 'ready':
    case 'ack': {
      const n = int(o.received_samples)
      return n === null || n < 0 ? null : { type: o.type, receivedSamples: n }
    }
    case 'pong':
      return { type: 'pong' }
    case 'error':
      return {
        type: 'error',
        code: str(o.code),
        closeCode: int(o.close_code),
        message: str(o.message) ?? 'Audio stream error',
        ts: timeMs(o.ts),
      }
    default:
      return null
  }
}

export const AUDIO_PING = JSON.stringify({ type: 'ping' })
export const EVENTS_PING = AUDIO_PING

// ---------------------------------------------------------------------------
// Meet link validation (mirrors the server: full URL, host/code, bare code)
// ---------------------------------------------------------------------------

const MEET_CODE = /^[a-z]{3}-[a-z]{4}-[a-z]{3}$/

/** Normalize a pasted Meet link to `https://meet.google.com/abc-defg-hij`,
 *  or `null` when it isn't one. */
export function normalizeMeetUrl(input: string): string | null {
  const trimmed = input.trim()
  if (!trimmed) return null
  if (MEET_CODE.test(trimmed.toLowerCase())) return `https://meet.google.com/${trimmed.toLowerCase()}`
  let url: URL
  try {
    url = new URL(/^https?:\/\//i.test(trimmed) ? trimmed : `https://${trimmed}`)
  } catch {
    return null
  }
  if (url.hostname.toLowerCase() !== 'meet.google.com') return null
  const code = url.pathname.replace(/^\/+|\/+$/g, '').toLowerCase()
  return MEET_CODE.test(code) ? `https://meet.google.com/${code}` : null
}

// ---------------------------------------------------------------------------
// REST
// ---------------------------------------------------------------------------

const REST_TIMEOUT_MS = 20_000

/** Another live session already exists for this Meet code (409 on start);
 *  the caller should follow it as a viewer. */
export class LiveSessionConflictError extends Error {
  readonly sessionId: string
  constructor(sessionId: string, message: string) {
    super(message)
    this.name = 'LiveSessionConflictError'
    this.sessionId = sessionId
  }
}

/** Human message from a FastAPI error, including `{detail: {message}}`. */
export function liveErrorMessage(error: unknown, fallback: string): string {
  if (error instanceof ApiError) {
    const body = error.body
    if (isObj(body) && isObj(body.detail) && str(body.detail.message)) return str(body.detail.message)!
    if (error.status === 401 || (error.status < 300 && typeof body === 'string'))
      return 'Your sign-in expired. Reload the page to sign in again.'
    if (error.status === 503) return 'Live record is not available on this deployment right now.'
    if (error.message && error.message !== 'API error') return error.message
    return `${fallback} (HTTP ${error.status})`
  }
  if (error instanceof DOMException && (error.name === 'TimeoutError' || error.name === 'AbortError'))
    return `${fallback}: the server did not respond in time.`
  if (error instanceof TypeError) return `${fallback}: network unreachable.`
  return error instanceof Error && error.message ? error.message : fallback
}

/** Was this a lapsed SSO session (401, or the proxy's HTML login page)? */
export function isAuthLapse(error: unknown): boolean {
  return error instanceof ApiError && (error.status === 401 || (error.status < 300 && typeof error.body === 'string'))
}

function request<T>(path: string, init: { method?: string; body?: unknown; signal?: AbortSignal; headers?: Record<string, string> } = {}): Promise<T> {
  return fetchApi<T>(`/live${path}`, {
    method: init.method ?? 'GET',
    body: init.body,
    headers: init.headers,
    signal: init.signal ?? AbortSignal.timeout(REST_TIMEOUT_MS),
    // Never hard-navigate away mid-capture on SSO lapse; the page surfaces it.
    skipAuthRedirect: true,
  })
}

const sid = (id: string) => `/sessions/${encodeURIComponent(id)}`

export interface LiveTranscriptResult {
  segments: LiveSegment[]
  speakers: LiveSpeaker[]
  lastSeq: number
  interim: LiveInterim | null
  status: LiveSessionStatus | null
}

export interface LongPollResult {
  events: LiveEvent[]
  /** Highest durable seq in the raw batch, including event types this
   *  client doesn't know — poll from here so an unknown trailing event
   *  can't make the server answer instantly forever. */
  cursor: number | null
  lastSeq: number | null
  interim: LiveInterim | null
  status: LiveSessionStatus | null
  pendingTriggers: GateTrigger[]
}

export interface LiveCreateOptions {
  title?: string
  gateProvider?: GateProviderId | null
  kind?: SessionKind
  /** Where the edition link goes; [] = keep it in RGS. Omitted = server default (Slack). */
  deliverVia?: DeliveryChannel[]
  recap?: boolean
  /** Also send the edition + RGS link to the people RGS recognises (default off). */
  shareWithParticipants?: boolean
}

export const liveApi = {
  async config(): Promise<LiveConfig> {
    return normalizeConfig(await request<unknown>('/config'))
  },

  /** `POST /sessions` → 201. Throws `LiveSessionConflictError` on 409.
   *  `meetUrl: null` starts an ad-hoc (in-person) conversation. */
  async create(meetUrl: string | null, options: LiveCreateOptions = {}): Promise<LiveSessionInfo> {
    const body: Obj = {}
    if (meetUrl) body.meet_url = meetUrl
    if (options.title) body.title = options.title
    if (options.gateProvider) body.gate_provider = options.gateProvider
    if (options.kind) body.kind = options.kind
    if (options.deliverVia) body.deliver_via = options.deliverVia
    if (options.recap === false) body.recap = false
    if (options.shareWithParticipants) body.share_with_participants = true
    try {
      return normalizeSession(await request<unknown>('/sessions', { method: 'POST', body }))
    } catch (error) {
      if (error instanceof ApiError && error.status === 409 && isObj(error.body) && isObj(error.body.detail)) {
        const existing = idOf(error.body.detail.session_id)
        if (existing)
          throw new LiveSessionConflictError(existing, str(error.body.detail.message) ?? 'This Meet is already being recorded.')
      }
      throw error
    }
  },

  async get(id: string): Promise<LiveSessionInfo> {
    return normalizeSession(await request<unknown>(sid(id)))
  },

  async list(active = true, limit = 20): Promise<LiveSessionInfo[]> {
    const raw = await request<unknown>(`/sessions?active=${active}&limit=${limit}`)
    if (!Array.isArray(raw)) return []
    return raw.flatMap((r) => {
      try {
        return [normalizeSession(r)]
      } catch {
        return []
      }
    })
  },

  /** Full transcript of a session (finals, speakers, interim tail). */
  async transcript(id: string, sinceSeq = 0, limit = 5000): Promise<LiveTranscriptResult> {
    const raw = await request<unknown>(`${sid(id)}/transcript?since_seq=${Math.max(0, sinceSeq)}&limit=${limit}`)
    const o = isObj(raw) ? raw : {}
    return {
      segments: Array.isArray(o.segments)
        ? o.segments.map((s) => normalizeSegment(s)).filter((s): s is LiveSegment => s !== null)
        : [],
      speakers: Array.isArray(o.speakers)
        ? o.speakers.map(normalizeSpeaker).filter((s): s is LiveSpeaker => s !== null)
        : [],
      lastSeq: int(o.last_seq) ?? 0,
      interim: normalizeInterim(o.interim),
      status: normalizeStatus(o.status),
    }
  },

  /** Everything agents posted to a session, oldest first. */
  async findings(id: string): Promise<LiveFinding[]> {
    const raw = await request<unknown>(`${sid(id)}/findings`)
    return Array.isArray(raw) ? raw.map((f) => normalizeFinding(f)).filter((f): f is LiveFinding => f !== null) : []
  },

  /** 202 with `status: "stopping"`; finalize continues server-side. */
  async stop(id: string, discard = false): Promise<LiveSessionInfo> {
    return normalizeSession(await request<unknown>(`${sid(id)}/stop`, { method: 'POST', body: { discard } }))
  },

  /** `POST /sessions/{id}/recap` (owner): print the edition again if it failed
   *  (or `reprint` a fresh one) and send the link again. 202 with the session. */
  /** `POST /sessions/{id}/recap` (owner). `{shareWithParticipants: true}` alone on a
   *  finished recap only sends the participant messages ("Share now"). */
  async rerunRecap(
    id: string,
    options: { deliverVia?: DeliveryChannel[]; reprint?: boolean; shareWithParticipants?: boolean; reshare?: boolean } = {},
  ): Promise<LiveSessionInfo> {
    const body: Obj = {}
    if (options.deliverVia) body.deliver_via = options.deliverVia
    if (options.reprint) body.reprint = true
    if (options.shareWithParticipants !== undefined) body.share_with_participants = options.shareWithParticipants
    if (options.reshare) body.reshare = true
    return normalizeSession(await request<unknown>(`${sid(id)}/recap`, { method: 'POST', body }))
  },

  /** `PATCH /sessions/{id}` (owner): opt in/out of sharing the recap with the participants. */
  async setShareWithParticipants(id: string, share: boolean): Promise<LiveSessionInfo> {
    return normalizeSession(await request<unknown>(sid(id), { method: 'PATCH', body: { share_with_participants: share } }))
  },

  /** The notes the recap wrote (null until written) and their Markdown. */
  async notes(id: string): Promise<{ notes: RecapNotes | null; markdown: string | null; recap: LiveRecap | null }> {
    const raw = await request<unknown>(`${sid(id)}/notes`)
    const o = isObj(raw) ? raw : {}
    return { notes: normalizeNotes(o.notes), markdown: str(o.markdown), recap: normalizeRecap(o.recap) }
  },

  /** `PATCH /sessions/{id}` (owner only): switch the decision gate live.
   *  The server also emits `session.status` with the new `gate_provider`. */
  async setGateProvider(id: string, gateProvider: GateProviderId): Promise<LiveSessionInfo> {
    return normalizeSession(await request<unknown>(sid(id), { method: 'PATCH', body: { gate_provider: gateProvider } }))
  },

  async renameSpeaker(id: string, label: string, displayName: string, userEmail?: string | null): Promise<LiveSpeaker | null> {
    const body: Obj = { display_name: displayName }
    if (userEmail) body.user_email = userEmail
    return normalizeSpeaker(
      await request<unknown>(`${sid(id)}/speakers/${encodeURIComponent(label)}`, { method: 'PATCH', body }),
    )
  },

  /** HTTP audio fallback. A 200 also covers trimmed overlaps/idempotent
   *  retries; only a gap returns 409 with `detail.received_samples` for
   *  resuming. A 409 without that count means not live and is thrown. */
  async sendAudio(id: string, offsetSamples: number, pcm: ArrayBuffer): Promise<{ receivedSamples: number; mismatch: boolean }> {
    try {
      const raw = await request<unknown>(`${sid(id)}/audio?offset_samples=${offsetSamples}`, {
        method: 'POST',
        body: new Blob([pcm], { type: 'application/octet-stream' }),
        headers: { 'Content-Type': 'application/octet-stream' },
      })
      const n = isObj(raw) ? int(raw.received_samples) : null
      if (n === null) throw new Error('Malformed audio ack')
      return { receivedSamples: n, mismatch: false }
    } catch (error) {
      if (error instanceof ApiError && error.status === 409 && isObj(error.body) && isObj(error.body.detail)) {
        const n = int(error.body.detail.received_samples)
        if (n !== null) return { receivedSamples: n, mismatch: true }
      }
      throw error
    }
  },

  /** HTTP long-poll fallback for the events stream. */
  async pollEvents(id: string, sinceSeq: number, signal: AbortSignal, waitS = 20, limit = 200): Promise<LongPollResult> {
    const raw = await request<unknown>(
      `${sid(id)}/events?since_seq=${Math.max(0, sinceSeq)}&wait_s=${waitS}&limit=${limit}`,
      { signal: AbortSignal.any ? AbortSignal.any([signal, AbortSignal.timeout((waitS + 15) * 1000)]) : signal },
    )
    const o = isObj(raw) ? raw : {}
    const rawEvents = Array.isArray(o.events) ? o.events : []
    let cursor: number | null = null
    for (const e of rawEvents) {
      const seq = isObj(e) ? int(e.seq) : null
      if (seq !== null && (cursor === null || seq > cursor)) cursor = seq
    }
    return {
      events: rawEvents.map(parseLiveEvent).filter((e): e is LiveEvent => e !== null),
      cursor,
      lastSeq: int(o.last_seq),
      interim: normalizeInterim(o.interim),
      status: normalizeStatus(o.status),
      pendingTriggers: Array.isArray(o.pending_triggers)
        ? o.pending_triggers.map((t) => normalizeTrigger(t)).filter((t): t is GateTrigger => t !== null)
        : [],
    }
  },
}

/** Speaker renames are owner-only (`LiveSession.is_owner`). */
export const LIVE_SUPPORTS_SPEAKER_RENAME = true

// ---------------------------------------------------------------------------
// WebSockets
// ---------------------------------------------------------------------------

/** Resolve `apiBase` (absolute or same-origin relative) to a ws(s) URL. */
export function wsUrl(path: string, loc: Pick<Location, 'protocol' | 'host'> = window.location): string {
  if (/^https?:\/\//i.test(apiBase)) return `${apiBase.replace(/^http/i, 'ws')}${path}`
  const scheme = loc.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${scheme}//${loc.host}${apiBase}${path}`
}

export function audioSocketUrl(sessionId: string): string {
  return wsUrl(`/live${sid(sessionId)}/audio`)
}

/** Always pass `since_seq` (0 = full durable replay, per §3). */
export function eventsSocketUrl(sessionId: string, sinceSeq: number): string {
  return wsUrl(`/live${sid(sessionId)}/events?since_seq=${Math.max(0, Math.floor(sinceSeq))}`)
}

/** Audio-socket close codes (§2). */
export const AUDIO_CLOSE = {
  stopped: 1000,
  badFrame: 4400,
  unauthenticated: 4401,
  notOwner: 4403,
  notFound: 4404,
  notLive: 4409,
  superseded: 4410,
  unavailable: 4503,
} as const

/** Never reconnect the audio socket after these. 4401 (re-auth) and
 *  1011 (server error) are recoverable; keep buffering and resume. */
export const AUDIO_TERMINAL_CLOSE_CODES: ReadonlySet<number> = new Set([1000, 4400, 4403, 4404, 4409, 4410, 4503])

/** Events readers see only 4404 for a missing/inaccessible session. 4401
 *  requires re-auth; 4408 requests a replay from the applied seq. */
export const EVENTS_TERMINAL_CLOSE_CODES: ReadonlySet<number> = new Set([4404])

/** Repo path of the agent skill that connects an agent to a live session. */
export const LIVE_LISTEN_SKILL_PATH = 'docs/agents/live-listen.md'
export const LIVE_LISTEN_SKILL_URL = `https://github.com/exowatt-oss/rgs/blob/main/${LIVE_LISTEN_SKILL_PATH}`
