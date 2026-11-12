/**
 * Pure state machine for a live session's event stream
 * (`README.md` §3).
 *
 * Owns the merge rules the UI depends on:
 *  - resume dedupe: a durable event whose `seq` is ≤ the highest applied
 *    seq is a replay and is dropped (the events socket reconnects with
 *    `since_seq`, and a server may over-deliver at the boundary);
 *  - interim → final: the interim is one unstable tail, replaced wholesale;
 *    a `segment.final` newer than the tail's `after_seq` supersedes it, and
 *    a stale tail (older than the newest final) is ignored;
 *  - speakers keep the server's stable `color_index`; merged clusters render
 *    as the speaker they were merged into;
 *  - `gate.trigger` creates a pending agent card that the matching
 *    `finding.new` (same `trigger_id`) resolves in place, or that fades out
 *    after `TRIGGER_TTL_MS` unanswered;
 *  - `gate.tick` keeps the decision gate's heartbeat (provider, latency,
 *    last decision), and fresh triggers / self-introductions mark which
 *    "listening for" action just fired.
 *  - a `speaker.update` without a `source` keeps the previous name
 *    provenance while the name itself is unchanged.
 *
 * No timers or I/O — callers pass `now` (and the server clock skew) so
 * expiry is deterministic in tests.
 */
import type {
  AsrState,
  GateProviderId,
  GateTick,
  GateTrigger,
  LiveEvent,
  LiveFinding,
  LiveInterim,
  LiveSegment,
  LiveSessionInfo,
  LiveSessionStatus,
  LiveSpeaker,
} from '@/api/live'
import { addPhrase, withOnDeviceWords, type OnDevicePhrase } from '@/lib/asr/onDevice'

export const SPEAKER_COLOR_COUNT = 8
/** Pending trigger lifetime (from receipt, or from its claim) before it
 *  fades out unresolved. */
export const TRIGGER_TTL_MS = 30_000
/** How long an expired trigger stays mounted so its fade-out can play. */
export const TRIGGER_FADE_MS = 600
/** Replayed fires older than this still count, but don't flash the
 *  "listening for" panel (a reload must not light everything up). */
export const FIRE_FRESH_MS = 15_000
/** The gate action id a self-introduction counts as. */
export const SPEAKER_INTRO_ACTION = 'speaker_intro'

export interface SpeakerEntry extends LiveSpeaker {
  /** Resolved palette slot (server `color_index`, else first-appearance). */
  colorIndex: number
  /** 1-based order of first appearance (fallback "Speaker N" name). */
  ordinal: number
}

export interface TriggerState extends GateTrigger {
  /** Client-clock time the trigger was emitted (server `ts` + skew). */
  receivedAt: number
  claimedAt: number | null
  expiredAt: number | null
}

export interface GateTickState extends GateTick {
  /** Client-clock time the tick was applied. */
  at: number
}

export interface GateFire {
  actionId: string
  at: number
  /** Bumps on every fire, so a repeat of the same action re-flashes. */
  n: number
  source: 'trigger' | 'self_intro'
}

export interface GateState {
  /** Active decision engine for this session (`gate_provider`). */
  provider: GateProviderId | null
  lastTick: GateTickState | null
  tickCount: number
  /** Most recent fresh fire (drives the brief highlight). */
  lastFire: GateFire | null
  /** Fires per action id this session, replays included. */
  fireCounts: Record<string, number>
}

export const initialGateState: GateState = {
  provider: null,
  lastTick: null,
  tickCount: 0,
  lastFire: null,
  fireCounts: {},
}

/** One card in the agent rail. A trigger card keeps its key when its finding
 *  lands so React morphs it in place instead of remounting. */
export interface RailCard {
  key: string
  trigger: TriggerState | null
  finding: LiveFinding | null
}

export interface LiveState {
  lastSeq: number
  /** Ascending by start time. */
  finals: LiveSegment[]
  /** Highest `segment.final` seq applied. */
  lastFinalSeq: number
  interim: LiveInterim | null
  speakers: Record<string, SpeakerEntry>
  speakerOrder: string[]
  /** Every trigger seen (cards may outlive or drop them). */
  triggers: Record<string, TriggerState>
  /** Newest first. */
  cards: RailCard[]
  status: LiveSessionStatus | null
  asrState: AsrState | null
  /** Durable audio the server holds, seconds. */
  audioSeconds: number | null
  meetingId: string | null
  transcriptId: string | null
  error: string | null
  /** Most recently finalized line (drives the aria-live announcement). */
  lastFinal: LiveSegment | null
  gate: GateState
  /** Copper's on-device phrases (`ondevice.segment`), start order; their words replace Nemotron's. */
  ondevice?: OnDevicePhrase[]
}

export const initialLiveState: LiveState = {
  lastSeq: 0,
  finals: [],
  lastFinalSeq: -1,
  interim: null,
  speakers: {},
  speakerOrder: [],
  triggers: {},
  cards: [],
  status: null,
  asrState: null,
  audioSeconds: null,
  meetingId: null,
  transcriptId: null,
  error: null,
  lastFinal: null,
  gate: initialGateState,
}

export type LiveAction =
  /** `skewMs` = client clock − server clock, used to age replayed triggers. */
  | { type: 'events'; events: LiveEvent[]; now: number; skewMs?: number }
  /** Optimistic local rename (reverted with `speaker.upsert`). */
  | { type: 'speaker.rename'; speakerId: string; displayName: string }
  | { type: 'speaker.upsert'; speaker: LiveSpeaker }
  /** Optimistic decision-engine switch (reverted with another one). */
  | { type: 'gate.provider'; provider: GateProviderId | null }
  | { type: 'session'; session: LiveSessionInfo }
  | { type: 'tick'; now: number }
  | { type: 'reset' }

export function liveReducer(state: LiveState, action: LiveAction): LiveState {
  switch (action.type) {
    case 'events': {
      let next = state
      for (const event of action.events) next = applyEvent(next, event, action.now, action.skewMs ?? 0)
      return next
    }
    case 'speaker.rename': {
      const existing = state.speakers[action.speakerId]
      if (!existing) return state
      return {
        ...state,
        speakers: {
          ...state.speakers,
          [action.speakerId]: { ...existing, displayName: action.displayName, nameSource: 'manual', nameConfidence: null, nameEvidence: null },
        },
      }
    }
    case 'speaker.upsert':
      return upsertSpeaker(state, action.speaker)
    case 'gate.provider':
      return state.gate.provider === action.provider ? state : { ...state, gate: { ...state.gate, provider: action.provider } }
    case 'session':
      return applySession(state, action.session)
    case 'tick':
      return expireTriggers(state, action.now)
    case 'reset':
      return initialLiveState
  }
}

export function applyEvent(state: LiveState, event: LiveEvent, now: number, skewMs = 0): LiveState {
  if (event.seq !== null && event.seq <= state.lastSeq) return state
  const base: LiveState = event.seq !== null ? { ...state, lastSeq: event.seq } : state
  switch (event.type) {
    case 'snapshot': {
      const withSession = applySession(base, event.session)
      return event.interim ? applyInterim(withSession, event.interim) : withSession
    }
    case 'segment.final':
      return applyFinal(base, event.segment)
    case 'segment.interim':
      return applyInterim(base, event.interim)
    case 'ondevice.segment':
      return { ...base, ondevice: addPhrase(base.ondevice ?? [], event.phrase) }
    case 'speaker.update': {
      const before = base.speakers[event.speaker.id]
      const next = upsertSpeaker(base, event.speaker)
      const named =
        event.speaker.nameSource === 'self_intro' &&
        !!event.speaker.displayName &&
        event.speaker.displayName !== before?.displayName
      if (!named) return next
      const emitted = event.ts !== null ? Math.min(now, event.ts + skewMs) : now
      return recordFire(next, SPEAKER_INTRO_ACTION, 'self_intro', emitted, now)
    }
    case 'finding.new':
      return applyFinding(base, event.finding)
    case 'gate.trigger': {
      const emitted = event.ts !== null ? Math.min(now, event.ts + skewMs) : now
      const known = !!base.triggers[event.trigger.id]
      const next = applyTrigger(base, event.trigger, emitted, now)
      return known ? next : recordFire(next, event.trigger.actionId, 'trigger', emitted, now)
    }
    case 'gate.tick':
      return {
        ...base,
        gate: {
          ...base.gate,
          // `provider` stays the session's setting (so an in-flight tick from
          // the old engine can't flip the switch back); the heartbeat shows
          // the tick's own provider.
          lastTick: { ...event.tick, at: now },
          tickCount: base.gate.tickCount + 1,
        },
      }
    case 'gate.claimed': {
      const emitted = event.ts !== null ? Math.min(now, event.ts + skewMs) : now
      return applyClaim(base, event.triggerId, event.claimedBy, emitted)
    }
    case 'session.status':
      return {
        ...base,
        status: event.status,
        asrState: event.asrState ?? base.asrState,
        audioSeconds: event.audioSeconds ?? base.audioSeconds,
        meetingId: event.meetingId ?? base.meetingId,
        transcriptId: event.transcriptId ?? base.transcriptId,
        error: event.error,
        gate: event.gateProvider && event.gateProvider !== base.gate.provider ? { ...base.gate, provider: event.gateProvider } : base.gate,
      }
    case 'session.heartbeat':
      if (event.audioSeconds === base.audioSeconds && (event.asrState ?? base.asrState) === base.asrState) return base
      return { ...base, audioSeconds: event.audioSeconds ?? base.audioSeconds, asrState: event.asrState ?? base.asrState }
    case 'resync':
    case 'pong':
    case 'error':
      return base
  }
}

function applySession(state: LiveState, session: LiveSessionInfo): LiveState {
  let next: LiveState = {
    ...state,
    status: session.status,
    asrState: session.asrState ?? state.asrState,
    audioSeconds: session.audioSeconds ?? state.audioSeconds,
    meetingId: session.meetingId ?? state.meetingId,
    transcriptId: session.transcriptId ?? state.transcriptId,
    error: session.error,
    gate:
      session.gateProvider && session.gateProvider !== state.gate.provider
        ? { ...state.gate, provider: session.gateProvider }
        : state.gate,
  }
  for (const speaker of session.speakers) next = upsertSpeaker(next, speaker)
  return next
}

/** Count a gate action firing; flash it only when it is fresh. */
function recordFire(state: LiveState, actionId: string, source: GateFire['source'], emittedAt: number, now: number): LiveState {
  const gate = state.gate
  const fireCounts = { ...gate.fireCounts, [actionId]: (gate.fireCounts[actionId] ?? 0) + 1 }
  const fresh = now - emittedAt < FIRE_FRESH_MS
  return {
    ...state,
    gate: {
      ...gate,
      fireCounts,
      lastFire: fresh ? { actionId, at: emittedAt, n: (gate.lastFire?.n ?? 0) + 1, source } : gate.lastFire,
    },
  }
}

function ensureSpeaker(state: LiveState, speakerId: string | null): LiveState {
  if (!speakerId || state.speakers[speakerId]) return state
  return upsertSpeaker(state, {
    id: speakerId,
    displayName: null,
    colorIndex: null,
    speechSeconds: null,
    user: null,
    person: null,
    suggestion: null,
    mergedInto: null,
    nameSource: null,
    nameConfidence: null,
    nameEvidence: null,
  })
}

function upsertSpeaker(state: LiveState, speaker: LiveSpeaker): LiveState {
  const existing = state.speakers[speaker.id]
  if (existing) {
    // An update that doesn't say where the name came from (a merge, a new
    // voiceprint guess) keeps the provenance of an unchanged name.
    const keepProvenance = speaker.nameSource === null && speaker.displayName === existing.displayName
    return {
      ...state,
      speakers: {
        ...state.speakers,
        [speaker.id]: {
          ...speaker,
          ordinal: existing.ordinal,
          colorIndex: speaker.colorIndex ?? existing.colorIndex,
          ...(keepProvenance
            ? { nameSource: existing.nameSource, nameConfidence: existing.nameConfidence, nameEvidence: existing.nameEvidence }
            : null),
        },
      },
    }
  }
  const ordinal = state.speakerOrder.length + 1
  return {
    ...state,
    speakerOrder: [...state.speakerOrder, speaker.id],
    speakers: {
      ...state.speakers,
      [speaker.id]: { ...speaker, ordinal, colorIndex: speaker.colorIndex ?? ordinal - 1 },
    },
  }
}

function applyInterim(state: LiveState, interim: LiveInterim): LiveState {
  // A tail computed before the newest final is stale.
  if (interim.afterSeq !== null && interim.afterSeq < state.lastFinalSeq) return state
  if (interim.parts.length === 0) return state.interim === null ? state : { ...state, interim: null }
  let next = state
  for (const part of interim.parts) next = ensureSpeaker(next, part.speakerId)
  return { ...next, interim }
}

function applyFinal(state: LiveState, segment: LiveSegment): LiveState {
  const withSpeaker = ensureSpeaker(state, segment.speakerId)
  const seq = segment.seq ?? withSpeaker.lastFinalSeq
  // The final supersedes the tail it was cut from.
  const interim =
    withSpeaker.interim && (withSpeaker.interim.afterSeq === null || withSpeaker.interim.afterSeq < seq)
      ? null
      : withSpeaker.interim
  return {
    ...withSpeaker,
    interim,
    finals: insertFinal(withSpeaker.finals, segment),
    lastFinalSeq: Math.max(withSpeaker.lastFinalSeq, seq),
    lastFinal: segment,
  }
}

/** Insert ordered by startMs (stable for ties); replace in place on id match. */
function insertFinal(finals: LiveSegment[], segment: LiveSegment): LiveSegment[] {
  const existing = finals.findIndex((s) => s.id === segment.id)
  if (existing !== -1) {
    const copy = finals.slice()
    copy[existing] = segment
    return copy
  }
  let i = finals.length
  while (i > 0 && finals[i - 1].startMs > segment.startMs) i -= 1
  if (i === finals.length) return [...finals, segment]
  return [...finals.slice(0, i), segment, ...finals.slice(i)]
}

function applyFinding(state: LiveState, finding: LiveFinding): LiveState {
  if (state.cards.some((c) => c.finding?.id === finding.id)) return state
  let triggers = state.triggers
  const known = finding.triggerId ? state.triggers[finding.triggerId] : undefined
  if (known) {
    triggers = { ...triggers, [known.id]: { ...known, status: 'answered', findingId: finding.id } }
  }
  if (finding.triggerId) {
    const idx = state.cards.findIndex((c) => c.trigger?.id === finding.triggerId && !c.finding)
    if (idx !== -1) {
      const cards = state.cards.slice()
      // Resolve in place, even if it had started fading.
      cards[idx] = { ...cards[idx], trigger: { ...(triggers[finding.triggerId] ?? cards[idx].trigger!), expiredAt: null }, finding }
      return { ...state, triggers, cards }
    }
  }
  const card: RailCard = { key: `finding:${finding.id}`, trigger: known ? triggers[known.id] : null, finding }
  return { ...state, triggers, cards: [card, ...state.cards] }
}

function applyTrigger(state: LiveState, trigger: GateTrigger, emittedAt: number, now: number): LiveState {
  if (state.triggers[trigger.id]) return state
  const triggerState: TriggerState = { ...trigger, receivedAt: emittedAt, claimedAt: null, expiredAt: null }
  const triggers = { ...state.triggers, [trigger.id]: triggerState }
  // Answered before we saw the trigger (replay order)? Attach context.
  const resolvedIdx = state.cards.findIndex((c) => c.finding?.triggerId === trigger.id)
  if (resolvedIdx !== -1) {
    const cards = state.cards.slice()
    cards[resolvedIdx] = { ...cards[resolvedIdx], trigger: { ...triggerState, status: 'answered' } }
    return { ...state, triggers, cards }
  }
  // Already past its lifetime (replayed after a reload): remember it so a
  // late finding keeps its context, but don't flash a stale pending card.
  if (now - emittedAt >= TRIGGER_TTL_MS || trigger.status === 'answered' || trigger.status === 'expired') {
    return { ...state, triggers }
  }
  return { ...state, triggers, cards: [{ key: `trigger:${trigger.id}`, trigger: triggerState, finding: null }, ...state.cards] }
}

function applyClaim(state: LiveState, triggerId: string, claimedBy: string | null, at: number): LiveState {
  const known = state.triggers[triggerId]
  if (!known) return state
  const updated: TriggerState = { ...known, status: known.status === 'answered' ? 'answered' : 'claimed', claimedBy, claimedAt: at }
  return {
    ...state,
    triggers: { ...state.triggers, [triggerId]: updated },
    cards: state.cards.map((c) => (c.trigger?.id === triggerId ? { ...c, trigger: { ...updated, expiredAt: c.trigger.expiredAt } } : c)),
  }
}

function expireTriggers(state: LiveState, now: number): LiveState {
  let changed = false
  const cards: RailCard[] = []
  for (const card of state.cards) {
    const t = card.trigger
    if (!card.finding && t) {
      if (t.expiredAt !== null && now - t.expiredAt >= TRIGGER_FADE_MS) {
        changed = true
        continue // faded out: unmount
      }
      const since = Math.max(t.receivedAt, t.claimedAt ?? 0)
      if (t.expiredAt === null && now - since >= TRIGGER_TTL_MS) {
        changed = true
        cards.push({ ...card, trigger: { ...t, expiredAt: now } })
        continue
      }
    }
    cards.push(card)
  }
  return changed ? { ...state, cards } : state
}

// ---------------------------------------------------------------------------
// Selectors
// ---------------------------------------------------------------------------

/** Follow `merged_into` so lines from a merged cluster render as the
 *  speaker it was merged into. */
export function resolveSpeakerId(state: Pick<LiveState, 'speakers'>, id: string | null): string | null {
  let current = id
  for (let hops = 0; current && hops < 8; hops += 1) {
    const next = state.speakers[current]?.mergedInto
    if (!next || next === current) break
    current = next
  }
  return current
}

export function speakerName(speaker: SpeakerEntry | undefined): string {
  if (!speaker) return 'Unknown speaker'
  return speaker.displayName ?? speaker.user?.name ?? `Speaker ${speaker.ordinal}`
}

const PLACEHOLDER_NAME = /^speaker\s*(\d+)$/i

/** Still "Speaker N" (nobody has named them yet)? */
export function isPlaceholderName(speaker: SpeakerEntry | undefined): boolean {
  if (!speaker) return true
  if (speaker.user?.name && !speaker.displayName) return false
  return !speaker.displayName || PLACEHOLDER_NAME.test(speaker.displayName.trim())
}

/** Avatar text: initials once named, else the speaker number. */
export function speakerInitials(speaker: SpeakerEntry | undefined): string {
  if (!speaker) return '?'
  if (isPlaceholderName(speaker)) {
    const m = speaker.displayName ? PLACEHOLDER_NAME.exec(speaker.displayName.trim()) : null
    return m ? m[1] : String(speaker.ordinal)
  }
  const parts = speakerName(speaker).trim().split(/\s+/).filter(Boolean)
  if (parts.length === 0) return '?'
  if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase()
  return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase()
}

/** Speakers to list as chips: first-appearance order, merged ones hidden. */
export function visibleSpeakers(state: LiveState): SpeakerEntry[] {
  return state.speakerOrder.map((id) => state.speakers[id]).filter((s) => s && !s.mergedInto)
}

/** Final lines, then the interim tail's parts, with merged speakers
 *  resolved. Interim parts always render at the live edge. */
export function transcriptRows(state: LiveState): LiveSegment[] {
  const anyMerged = state.speakerOrder.some((id) => state.speakers[id]?.mergedInto)
  const heard = state.ondevice?.length ? withOnDeviceWords(state.finals, state.ondevice) : state.finals
  const finals = anyMerged
    ? heard.map((s) => {
        const resolved = resolveSpeakerId(state, s.speakerId)
        return resolved === s.speakerId ? s : { ...s, speakerId: resolved }
      })
    : heard
  const interim = state.interim
  if (!interim || interim.parts.length === 0) return finals
  const tail: LiveSegment[] = interim.parts.map((part, i) => ({
    id: `interim:${i}`,
    seq: null,
    speakerId: resolveSpeakerId(state, part.speakerId),
    text: part.text,
    startMs: interim.startMs,
    endMs: interim.endMs,
    final: false,
  }))
  return [...finals, ...tail]
}

/** Seq ranges of triggers still waiting for a finding (transcript highlight). */
export function pendingTriggerRanges(state: LiveState): { from: number; to: number; triggerId: string }[] {
  const out: { from: number; to: number; triggerId: string }[] = []
  for (const card of state.cards) {
    const t = card.trigger
    if (!t || card.finding || t.expiredAt !== null) continue
    if (t.seqFrom === null && t.seqTo === null) continue
    const from = t.seqFrom ?? t.seqTo!
    const to = t.seqTo ?? t.seqFrom!
    out.push({ from: Math.min(from, to), to: Math.max(from, to), triggerId: t.id })
  }
  return out
}

/** The transcript line a card points at: an exact seq (a finding's
 *  `related_seq`), else the newest line inside a trigger's seq range (the
 *  words that tripped the gate). */
export function findMomentSegment(
  state: Pick<LiveState, 'finals'>,
  ref: { seq?: number | null; seqFrom?: number | null; seqTo?: number | null },
): LiveSegment | null {
  if (ref.seq != null) {
    const hit = state.finals.find((s) => s.seq === ref.seq)
    if (hit) return hit
  }
  if (ref.seqFrom != null || ref.seqTo != null) {
    const from = Math.min(ref.seqFrom ?? ref.seqTo!, ref.seqTo ?? ref.seqFrom!)
    const to = Math.max(ref.seqFrom ?? ref.seqTo!, ref.seqTo ?? ref.seqFrom!)
    let best: LiveSegment | null = null
    for (const s of state.finals) {
      if (s.seq !== null && s.seq >= from && s.seq <= to && (!best || s.seq > best.seq!)) best = s
    }
    return best
  }
  return null
}

/** Cards still able to change by the clock alone. */
export function hasPendingTriggers(state: LiveState): boolean {
  return state.cards.some((c) => c.trigger && !c.finding)
}
