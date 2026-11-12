/**
 * On-device words (Copper's Phonon-2) in the live transcript — the authenticated OpenAPI schema.
 *
 * The server sends each phrase a Copper browser heard on-device as an
 * `ondevice.segment` event: its words with their times on the session
 * timeline. Nemotron's `segment.final` lines stay the units of the transcript
 * (who spoke, seq, highlights); this only swaps the words inside them:
 *
 *  - every on-device word goes into the final line it overlaps most in time
 *    (or the nearest one within 1.5 s) — so it takes that line's speaker;
 *  - a line keeps its own Nemotron words only where no on-device phrase
 *    covers their time (spread evenly over the line; lines carry no word times).
 *
 * The server's final transcript applies the same rule (`app.live.ondevice`).
 */

import { fetchApi } from '@/api/client'
import type { LiveSegment } from '@/api/live'

export interface OnDeviceWord {
  text: string
  startMs: number
  endMs: number
}

export interface OnDevicePhrase {
  /** The browser's phrase key (start in ms on the session timeline). */
  clientSeq: number
  engine: string
  startMs: number
  endMs: number
  text: string
  words: OnDeviceWord[]
}

type Obj = Record<string, unknown>
const isObj = (v: unknown): v is Obj => typeof v === 'object' && v !== null && !Array.isArray(v)
const num = (v: unknown): number | null => (typeof v === 'number' && Number.isFinite(v) ? v : null)
const ms = (v: unknown): number | null => {
  const s = num(v)
  return s === null ? null : Math.round(s * 1000)
}

/** `ondevice.segment` data → a phrase (words from every part, in time order). */
export function parseOnDevicePhrase(data: unknown): OnDevicePhrase | null {
  if (!isObj(data)) return null
  const startMs = ms(data.start_s)
  const endMs = ms(data.end_s)
  const clientSeq = num(data.client_seq)
  if (startMs === null || endMs === null || clientSeq === null) return null
  const words: OnDeviceWord[] = []
  // Live events carry words per speaker part; `GET …/ondevice/segments` flat.
  const parts = Array.isArray(data.parts) ? data.parts : [data]
  for (const part of parts) {
    if (!isObj(part) || !Array.isArray(part.words)) continue
    for (const w of part.words) {
      if (!isObj(w) || typeof w.text !== 'string' || !w.text.trim()) continue
      const s = ms(w.start_s)
      const e = ms(w.end_s)
      if (s === null || e === null) continue
      words.push({ text: w.text.trim(), startMs: s, endMs: Math.max(s, e) })
    }
  }
  words.sort((a, b) => a.startMs - b.startMs)
  return {
    clientSeq,
    engine: typeof data.engine === 'string' ? data.engine : 'phonon-2',
    startMs,
    endMs,
    text: typeof data.text === 'string' ? data.text : words.map((w) => w.text).join(' '),
    words,
  }
}

/** Phrases by `clientSeq` (a replayed event replaces its twin), kept in start order. */
export function addPhrase(phrases: OnDevicePhrase[], phrase: OnDevicePhrase): OnDevicePhrase[] {
  const rest = phrases.filter((p) => p.clientSeq !== phrase.clientSeq)
  let i = rest.length
  while (i > 0 && rest[i - 1].startMs > phrase.startMs) i -= 1
  return [...rest.slice(0, i), phrase, ...rest.slice(i)]
}

const NEAR_MS = 1500

function lowerBound(xs: number[], x: number): number {
  let lo = 0
  let hi = xs.length
  while (lo < hi) {
    const mid = (lo + hi) >> 1
    if (xs[mid] < x) lo = mid + 1
    else hi = mid
  }
  return lo
}

function union(spans: [number, number][]): [number, number][] {
  const out: [number, number][] = []
  for (const [a, b] of [...spans].sort((x, y) => x[0] - y[0])) {
    const last = out[out.length - 1]
    if (last && a <= last[1]) last[1] = Math.max(last[1], b)
    else out.push([a, b])
  }
  return out
}

/** The final lines with on-device words where Copper heard them. */
export function withOnDeviceWords(finals: LiveSegment[], phrases: OnDevicePhrase[]): LiveSegment[] {
  if (!phrases.length || !finals.length) return finals
  const lines = finals.map((seg, i) => ({
    seg,
    i,
    start: seg.startMs,
    end: seg.endMs ?? seg.startMs,
  }))
  const byStart = [...lines].sort((a, b) => a.start - b.start)
  const starts = byStart.map((l) => l.start)
  const longest = byStart.reduce((m, l) => Math.max(m, l.end - l.start), 0)
  const got: { text: string; at: number }[][] = finals.map(() => [])
  for (const phrase of phrases) {
    for (const w of phrase.words) {
      let best = -1
      let bestOverlap = 0
      let near = -1
      let nearGap = NEAR_MS
      const lo = lowerBound(starts, w.startMs - longest - NEAR_MS)
      for (let k = lo; k < byStart.length && byStart[k].start <= w.endMs + NEAR_MS; k++) {
        const l = byStart[k]
        const overlap = Math.min(w.endMs, l.end) - Math.max(w.startMs, l.start)
        if (overlap > bestOverlap) {
          best = l.i
          bestOverlap = overlap
        }
        const gap = Math.max(l.start - w.endMs, w.startMs - l.end, 0)
        if (gap <= nearGap) {
          near = l.i
          nearGap = gap
        }
      }
      const into = best !== -1 ? best : near
      if (into !== -1) got[into].push({ text: w.text, at: w.startMs })
    }
  }
  const spans = union(phrases.map((p) => [p.startMs, p.endMs]))
  const spanStarts = spans.map((s) => s[0])
  const covered = (t: number) => {
    const k = lowerBound(spanStarts, t + 1) - 1
    return k >= 0 && spans[k][0] <= t && t <= spans[k][1]
  }
  return finals.map((seg, i) => {
    const words = got[i]
    const own = seg.text.split(/\s+/).filter(Boolean)
    const end = seg.endMs ?? seg.startMs
    const step = own.length ? Math.max(end - seg.startMs, 10) / own.length : 0
    const kept = own
      .map((text, k) => ({
        text,
        at: seg.startMs + k * step,
        mid: seg.startMs + (k + 0.5) * step,
      }))
      .filter((w) => !covered(w.mid))
    if (!words.length && kept.length === own.length) return seg
    // Covered, yet Copper heard nothing here: show Nemotron's words rather than an empty line.
    if (!words.length && !kept.length) return seg
    const all = [...words, ...kept].sort((a, b) => a.at - b.at)
    return { ...seg, text: all.map((w) => w.text).join(' ') }
  })
}

/** A finished session's phrases (`GET /api/live/sessions/{id}/ondevice/segments`). */
export async function fetchOnDevicePhrases(sessionId: string): Promise<OnDevicePhrase[]> {
  try {
    const raw = await fetchApi<unknown>(`/live/sessions/${encodeURIComponent(sessionId)}/ondevice/segments`, {
      signal: AbortSignal.timeout(15_000),
      skipAuthRedirect: true,
    })
    const list = isObj(raw) && Array.isArray(raw.segments) ? raw.segments : []
    return list.map(parseOnDevicePhrase).filter((p): p is OnDevicePhrase => p !== null)
  } catch {
    return []
  }
}
