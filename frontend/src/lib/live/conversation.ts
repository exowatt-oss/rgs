/**
 * Conversation structure for the transcript views: turns, the layout a cast
 * size calls for, who holds the floor, talk time, and name suggestions.
 * Pure functions over the reducer's state so the Live and Session views (and
 * the tests) read the conversation the same way.
 */
import type { LivePerson, LiveSegment } from '@/api/live'
import { isPlaceholderName, speakerName, type LiveState, type SpeakerEntry } from '@/lib/live/liveReducer'

/** A run of consecutive lines by one speaker. */
export interface Turn {
  key: string
  speakerId: string | null
  startMs: number
  endMs: number
  lines: LiveSegment[]
}

/** A pause this long starts a new turn even for the same speaker. */
export const TURN_GAP_MS = 45_000

const endOf = (s: LiveSegment) => Math.max(s.startMs, s.endMs ?? s.startMs)

export function groupTurns(rows: LiveSegment[], gapMs = TURN_GAP_MS, maxLines = Infinity): Turn[] {
  const turns: Turn[] = []
  for (const row of rows) {
    const last = turns[turns.length - 1]
    if (last && last.speakerId === row.speakerId && row.startMs - last.endMs < gapMs && last.lines.length < maxLines) {
      last.lines.push(row)
      last.endMs = Math.max(last.endMs, endOf(row))
    } else {
      turns.push({ key: row.id, speakerId: row.speakerId, startMs: row.startMs, endMs: endOf(row), lines: [row] })
    }
  }
  return turns
}

/**
 * How the transcript lays out for a cast: one voice reads as a single
 * focused column, two as a dialogue (one each side), three or more on
 * per-speaker lanes.
 */
export type CastLayout = 'solo' | 'duo' | 'group'

export function castLayout(speakerCount: number): CastLayout {
  if (speakerCount <= 1) return 'solo'
  if (speakerCount === 2) return 'duo'
  return 'group'
}

/** Speakers who have said something, in order of first appearance (merged
 *  clusters resolve to their target). Lane `i` belongs to `cast[i]`. */
export function castOf(state: LiveState, rows: LiveSegment[]): SpeakerEntry[] {
  const heard = new Set<string>()
  for (const r of rows) if (r.speakerId) heard.add(r.speakerId)
  return state.speakerOrder
    .map((id) => state.speakers[id])
    .filter((s): s is SpeakerEntry => !!s && !s.mergedInto && heard.has(s.id))
}

export interface SpeakerStat {
  talkMs: number
  turns: number
  lines: number
  /** Share of everyone's talk time, 0–1. */
  share: number
}

/** Talk time per speaker from the transcript's own timing. */
export function speakerStats(rows: LiveSegment[], turns: Turn[]): Map<string, SpeakerStat> {
  const out = new Map<string, SpeakerStat>()
  const get = (id: string) => {
    let s = out.get(id)
    if (!s) out.set(id, (s = { talkMs: 0, turns: 0, lines: 0, share: 0 }))
    return s
  }
  let total = 0
  for (const r of rows) {
    if (!r.speakerId) continue
    const ms = Math.max(0, endOf(r) - r.startMs)
    const s = get(r.speakerId)
    s.talkMs += ms
    if (r.final) s.lines += 1
    total += ms
  }
  for (const t of turns) if (t.speakerId) get(t.speakerId).turns += 1
  for (const s of out.values()) s.share = total > 0 ? s.talkMs / total : 0
  return out
}

/** One stretch of someone holding the floor (for the speaker tracks). */
export interface FloorSpan {
  speakerId: string
  startMs: number
  endMs: number
  /** First final line in the span (jump target); null for the live tail. */
  seq: number | null
}

/** Merges a speaker's back-to-back lines (gap under `mergeMs`) into spans. */
export function floorSpans(rows: LiveSegment[], mergeMs = 1500): FloorSpan[] {
  const spans: FloorSpan[] = []
  const open = new Map<string, FloorSpan>()
  for (const r of rows) {
    if (!r.speakerId) continue
    const prev = open.get(r.speakerId)
    const end = endOf(r)
    if (prev && r.startMs - prev.endMs <= mergeMs && spans[spans.length - 1] === prev) {
      prev.endMs = Math.max(prev.endMs, end)
      if (prev.seq === null && r.seq !== null) prev.seq = r.seq
      continue
    }
    const span: FloorSpan = { speakerId: r.speakerId, startMs: r.startMs, endMs: end, seq: r.seq }
    spans.push(span)
    open.set(r.speakerId, span)
  }
  return spans
}

/** Who is talking right now: the speaker of the live (interim) tail. */
export function speakingNow(rows: LiveSegment[]): string | null {
  for (let i = rows.length - 1; i >= 0; i -= 1) {
    const r = rows[i]
    if (r.final) return null
    if (r.speakerId && r.text.trim()) return r.speakerId
  }
  return null
}

/** Below this, an automatic name is shown as a guess ("Alice"). */
export const TENTATIVE_BELOW = 0.7

/** An automatic name the system isn't sure of (never a manual one). */
export function isTentativeName(speaker: SpeakerEntry | undefined): boolean {
  if (!speaker || isPlaceholderName(speaker) || speaker.user) return false
  if (speaker.nameSource === 'manual') return false
  return speaker.nameConfidence != null && speaker.nameConfidence < TENTATIVE_BELOW
}

export interface NameSuggestion {
  name: string
  /** Bind the speaker to this RGS user (only for real users). */
  email: string | null
  /** Why it's offered: a voice match, the current guess, or the invite list. */
  reason: 'voice' | 'guess' | 'attendee' | 'owner'
  score: number | null
}

/**
 * Names to offer when renaming a speaker, best first: the voice match, the
 * current low-confidence guess (to confirm it), then the call's attendees and
 * the owner — minus names other speakers already carry.
 */
export function nameSuggestions(
  speaker: SpeakerEntry,
  others: SpeakerEntry[],
  context: { attendees?: { name: string | null; email: string | null }[]; owner?: LivePerson | null } = {},
): NameSuggestion[] {
  const taken = new Set(
    others.filter((o) => o.id !== speaker.id && !o.mergedInto && !isPlaceholderName(o)).map((o) => norm(speakerName(o))),
  )
  const out: NameSuggestion[] = []
  const seen = new Set<string>()
  const add = (s: NameSuggestion) => {
    const key = norm(s.name)
    if (!key || seen.has(key) || taken.has(key)) return
    seen.add(key)
    out.push(s)
  }
  if (speaker.suggestion) add({ name: speaker.suggestion.name, email: speaker.suggestion.email, reason: 'voice', score: speaker.suggestion.score })
  if (isTentativeName(speaker)) {
    const guess = speakerName(speaker)
    // A first name ("Alice") also suggests the attendee it most likely means.
    add({ name: guess, email: null, reason: 'guess', score: speaker.nameConfidence })
  }
  const attendees = context.attendees ?? []
  const guessFirst = isTentativeName(speaker) ? norm(speakerName(speaker)).split(' ')[0] : null
  const ranked = [...attendees].sort((a, b) => Number(firstIs(b.name, guessFirst)) - Number(firstIs(a.name, guessFirst)))
  for (const a of ranked) if (a.name) add({ name: a.name, email: null, reason: 'attendee', score: null })
  if (context.owner?.name) add({ name: context.owner.name, email: context.owner.email ?? null, reason: 'owner', score: null })
  return out
}

const norm = (s: string | null | undefined) => (s ?? '').trim().replace(/\s+/g, ' ').toLowerCase()
const firstIs = (name: string | null, first: string | null) => !!first && norm(name).split(' ')[0] === first
