import { useMemo, useRef, useState, type KeyboardEvent, type PointerEvent } from 'react'

import type { LiveSegment } from '@/api/live'
import { SpeakerAvatar } from '@/components/live/SpeakerName'
import { isTentativeName, type FloorSpan, type SpeakerStat } from '@/lib/live/conversation'
import { identityText } from '@/lib/live/identity'
import { speakerName, type SpeakerEntry } from '@/lib/live/liveReducer'
import { formatClock, speakerClasses } from '@/lib/live/presentation'
import { cn } from '@/lib/utils'

/** An agent answer on the timeline, at the line that asked for it. */
export interface FloorMark {
  cardKey: string
  atMs: number
  label: string
  pending: boolean
}

/**
 * "The floor": one track per speaker showing when each of them talked, the
 * agent's answers on their own track (at the line that asked), and each
 * person's share of the talk time. Identity is the row (and its label), not
 * only the colour. Point or arrow along the tracks to read a moment; click or
 * Enter jumps the transcript there. Clicking a name focuses that speaker.
 */
export function Floor({
  cast,
  spans,
  stats,
  marks,
  lines,
  endMs,
  speakingId,
  focusId,
  onFocus,
  onJump,
  onOpenMark,
}: {
  cast: SpeakerEntry[]
  spans: FloorSpan[]
  stats: Map<string, SpeakerStat>
  marks: FloorMark[]
  /** Final lines by seq (tooltip text). */
  lines: Map<number, LiveSegment>
  endMs: number
  speakingId: string | null
  focusId: string | null
  onFocus: (speakerId: string | null) => void
  onJump: (seq: number) => void
  onOpenMark: (cardKey: string) => void
}) {
  const trackRef = useRef<HTMLDivElement>(null)
  const [hover, setHover] = useState<{ x: number; span: FloorSpan | null } | null>(null)
  const [cursor, setCursor] = useState<number | null>(null)
  const total = Math.max(endMs, 1)
  const pct = (ms: number) => `${Math.min(100, Math.max(0, (ms / total) * 100))}%`
  const lane = useMemo(() => new Map(cast.map((s, i) => [s.id, i])), [cast])
  const jumpable = useMemo(() => spans.filter((s) => s.seq !== null && lane.has(s.speakerId)), [spans, lane])

  const spanAt = (ms: number): FloorSpan | null => {
    let best: FloorSpan | null = null
    let bestD = Infinity
    for (const s of jumpable) {
      const d = ms < s.startMs ? s.startMs - ms : ms > s.endMs ? ms - s.endMs : 0
      if (d < bestD) {
        bestD = d
        best = s
      }
    }
    return best
  }

  const onMove = (e: PointerEvent<HTMLDivElement>) => {
    const el = trackRef.current
    if (!el) return
    const r = el.getBoundingClientRect()
    const x = Math.min(r.width, Math.max(0, e.clientX - r.left))
    setHover({ x: x / r.width, span: spanAt((x / r.width) * total) })
  }

  const onKey = (e: KeyboardEvent<HTMLDivElement>) => {
    if (!jumpable.length) return
    if (e.key === 'ArrowRight' || e.key === 'ArrowLeft') {
      e.preventDefault()
      const next = cursor === null ? (e.key === 'ArrowRight' ? 0 : jumpable.length - 1) : cursor + (e.key === 'ArrowRight' ? 1 : -1)
      setCursor(Math.min(jumpable.length - 1, Math.max(0, next)))
    } else if ((e.key === 'Enter' || e.key === ' ') && cursor !== null) {
      e.preventDefault()
      const s = jumpable[cursor]
      if (s?.seq != null) onJump(s.seq)
    }
  }

  const keySpan = cursor !== null ? jumpable[cursor] : null
  const shown = hover?.span ?? keySpan ?? null
  const shownX = hover ? hover.x : keySpan ? (keySpan.startMs + keySpan.endMs) / 2 / total : null
  const shownLine = shown?.seq != null ? lines.get(shown.seq) : undefined
  const shownSpeaker = shown ? cast.find((c) => c.id === shown.speakerId) : undefined

  const summary = cast
    .map((s) => `${speakerName(s)} ${Math.round((stats.get(s.id)?.share ?? 0) * 100)}%`)
    .join(', ')

  return (
    <div className="shrink-0 border-b border-border px-3 pt-2 pb-1.5 [--row:19px] sm:px-4 sm:pt-2.5 sm:pb-2 sm:[--row:22px]">
      <div className="grid grid-cols-[auto_minmax(0,1fr)_auto] items-center gap-x-3 sm:grid-cols-[minmax(0,10rem)_minmax(0,1fr)_auto]">
        {/* Labels column */}
        <div className="col-start-1 row-start-1 flex flex-col">
          {cast.map((s) => {
            const on = focusId === s.id
            const speaking = speakingId === s.id
            return (
              <button
                key={s.id}
                type="button"
                onClick={() => onFocus(on ? null : s.id)}
                aria-pressed={on}
                title={on ? 'Show everyone' : `Show only ${speakerName(s)}${s.person ? ` (${identityText(s.person)})` : ''}`}
                className={cn(
                  'flex h-(--row) min-w-0 items-center gap-2 rounded-[5px] pr-1.5 pl-0.5 text-left transition-colors hover:bg-hover',
                  focusId && !on && 'opacity-55',
                  on && 'bg-selected',
                )}
              >
                <SpeakerAvatar speaker={s} size={16} speaking={speaking} />
                <span className="hidden min-w-0 truncate text-[12px] font-medium text-fg sm:inline">
                  {speakerName(s)}
                  {isTentativeName(s) && <span className="font-normal text-muted">?</span>}
                </span>
                {speaking && <span className="sr-only sm:not-sr-only sm:shrink-0 sm:text-[11px] sm:text-muted">speaking</span>}
              </button>
            )
          })}
          {marks.length > 0 && (
            <span className="flex h-(--row) items-center gap-2 pl-0.5 text-[12px] text-muted">
              <span aria-hidden className="grid size-4 place-items-center">
                <span className="size-2 rotate-45 rounded-[2px] bg-brand" />
              </span>
              <span className="hidden sm:inline">RGS answers</span>
            </span>
          )}
        </div>

        {/* Tracks */}
        <div
          ref={trackRef}
          role="group"
          tabIndex={0}
          aria-label={`Who talked when. Talk time: ${summary}. Arrow keys step through turns; Enter jumps to one.`}
          onPointerMove={onMove}
          onPointerLeave={() => setHover(null)}
          onBlur={() => setCursor(null)}
          onKeyDown={onKey}
          onClick={() => {
            if (hover?.span?.seq != null) onJump(hover.span.seq)
          }}
          className="relative col-start-2 row-start-1 cursor-pointer rounded-[4px] outline-none focus-visible:ring-2 focus-visible:ring-brand/50"
        >
          {cast.map((s) => (
            <div key={s.id} className="relative h-(--row)">
              <span aria-hidden className="absolute inset-x-0 top-1/2 h-px bg-grid" />
            </div>
          ))}
          {marks.length > 0 && (
            <div className="relative h-(--row)">
              <span aria-hidden className="absolute inset-x-0 top-1/2 h-px bg-grid" />
            </div>
          )}
          {spans.map((sp, i) => {
            const row = lane.get(sp.speakerId)
            if (row === undefined) return null
            const cls = speakerClasses(cast[row].colorIndex)
            const dim = focusId !== null && focusId !== sp.speakerId
            return (
              <span
                key={i}
                aria-hidden
                className={cn('absolute h-2 rounded-[2px] transition-opacity', cls.dot, dim && 'opacity-20', sp.seq === null && 'opacity-60')}
                style={{ left: pct(sp.startMs), width: `max(3px, ${pct(sp.endMs - sp.startMs)})`, top: `calc(var(--row) * ${row} + var(--row) / 2 - 4px)` }}
              />
            )
          })}
          {marks.map((m) => (
            <button
              key={m.cardKey}
              type="button"
              tabIndex={-1}
              onClick={(e) => {
                e.stopPropagation()
                onOpenMark(m.cardKey)
              }}
              title={`RGS: ${m.label}`}
              className="absolute grid size-4 -translate-x-1/2 place-items-center"
              style={{ left: pct(m.atMs), top: `calc(var(--row) * ${cast.length} + var(--row) / 2 - 8px)` }}
            >
              <span className={cn('size-2 rotate-45 rounded-[2px] bg-brand', m.pending && 'blink')} />
              <span className="sr-only">Open answer: {m.label}</span>
            </button>
          ))}
          {shownX !== null && (
            <span aria-hidden className="pointer-events-none absolute inset-y-0 w-px bg-border-strong" style={{ left: `${shownX * 100}%` }} />
          )}
          {shown && shownX !== null && (
            <div
              role="status"
              className="pointer-events-none absolute top-full z-20 mt-1.5 w-max max-w-[min(22rem,80vw)] rounded-lg border border-border bg-surface px-2.5 py-1.5 text-[12px] shadow-pop"
              style={{ left: `${shownX * 100}%`, transform: `translateX(${shownX > 0.66 ? '-100%' : shownX < 0.33 ? '0' : '-50%'})` }}
            >
              <span className="flex items-center gap-1.5">
                <SpeakerAvatar speaker={shownSpeaker} size={14} />
                <span className="font-medium text-fg">{speakerName(shownSpeaker)}</span>
                <span className="mono text-[11px] text-subtle">{formatClock(shown.startMs)}</span>
              </span>
              {shownLine && <span className="mt-0.5 line-clamp-2 block text-muted">“{shownLine.text}”</span>}
            </div>
          )}
        </div>

        {/* Talk time */}
        <div className="col-start-3 row-start-1 flex flex-col">
          {cast.map((s) => {
            const st = stats.get(s.id)
            return (
              <span key={s.id} className="flex h-(--row) items-center justify-end gap-2 text-[11.5px]">
                <span className="mono tabular hidden text-subtle sm:inline">{formatClock(st?.talkMs ?? 0)}</span>
                <span className="mono tabular w-8 text-right text-fg-2">{Math.round((st?.share ?? 0) * 100)}%</span>
              </span>
            )
          })}
          {marks.length > 0 && (
            <span className="flex h-(--row) items-center justify-end text-[11.5px] text-muted">
              <span className="mono tabular">{marks.length}</span>
            </span>
          )}
        </div>
        <span className="col-start-2 row-start-2 flex justify-between pt-0.5 mono text-[10.5px] text-subtle tabular">
          <span>0:00</span>
          <span>{formatClock(total)}</span>
        </span>
      </div>
    </div>
  )
}
