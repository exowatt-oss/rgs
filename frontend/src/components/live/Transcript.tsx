import { AnimatePresence, motion, useReducedMotion } from 'motion/react'
import { ArrowDown, X } from 'lucide-react'
import { memo, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type CSSProperties, type ReactNode } from 'react'

import type { LiveSegment } from '@/api/live'
import { Floor, type FloorMark } from '@/components/live/Floor'
import { PersonIdentity } from '@/components/live/PersonIdentity'
import { SpeakerAvatar, SpeakerName } from '@/components/live/SpeakerName'
import {
  castLayout,
  castOf,
  floorSpans,
  groupTurns,
  speakerStats,
  speakingNow,
  type CastLayout,
  type SpeakerStat,
  type Turn,
} from '@/lib/live/conversation'
import { pendingTriggerRanges, speakerName, transcriptRows, type LiveState, type SpeakerEntry } from '@/lib/live/liveReducer'
import { formatClock, gateActionMeta, speakerClasses } from '@/lib/live/presentation'
import { cn } from '@/lib/utils'

export interface LineAnswer {
  cardKey: string
  actionId: string | null
  title: string
}

const PIN_SLACK_PX = 72
const SOLO_PARAGRAPH_GAP_MS = 2000
const SOLO_PARAGRAPH_LINES = 4
const EASE = [0.22, 1, 0.36, 1] as const

type Block = { kind: 'turn'; turn: Turn } | { kind: 'others'; key: string; turns: Turn[] }

interface LineContext {
  answers: Map<number, LineAnswer[]>
  pending: { from: number; to: number }[]
  focus: { seq: number; n: number } | null
  onOpenAnswer: (cardKey: string) => void
}

/**
 * The conversation, laid out for its cast: one voice reads as a focused
 * column, two as a dialogue, three or more on per-speaker lanes. Above it,
 * the floor — who talked when and for how long — doubles as the focus
 * filter and a scrubber. While live it follows the newest words; scrolling
 * up parks it behind a "jump to live" pill.
 */
export function Transcript({
  state,
  live,
  answers,
  moments,
  focus,
  onOpenAnswer,
  onJumpToLine,
  empty,
  footer,
}: {
  state: LiveState
  live: boolean
  answers: Map<number, LineAnswer[]>
  /** Card → the line it answers (agent marks on the floor). */
  moments?: Map<string, LiveSegment | null>
  /** Scroll to and flash this line (bump `n` to re-trigger). */
  focus: { seq: number; n: number } | null
  onOpenAnswer: (cardKey: string) => void
  onJumpToLine: (seq: number) => void
  empty: ReactNode
  footer?: ReactNode
}) {
  const rows = useMemo(() => transcriptRows(state), [state])
  const cast = useMemo(() => castOf(state, rows), [state, rows])
  const layout: CastLayout = castLayout(cast.length)
  // A monologue breaks into paragraphs at its pauses; a conversation at the speaker.
  const turns = useMemo(() => (layout === 'solo' ? groupTurns(rows, SOLO_PARAGRAPH_GAP_MS, SOLO_PARAGRAPH_LINES) : groupTurns(rows)), [rows, layout])
  const lane = useMemo(() => new Map(cast.map((s, i) => [s.id, i])), [cast])
  const stats = useMemo(() => speakerStats(rows, turns), [rows, turns])
  const spans = useMemo(() => floorSpans(rows), [rows])
  const pending = useMemo(() => pendingTriggerRanges(state), [state])
  const speaking = live ? speakingNow(rows) : null
  const [focusId, setFocusId] = useState<string | null>(null)
  const activeFocus = focusId && lane.has(focusId) && layout !== 'solo' ? focusId : null

  const endMs = useMemo(() => rows.reduce((m, r) => Math.max(m, r.endMs ?? r.startMs), 0), [rows])
  const finalsBySeq = useMemo(() => {
    const out = new Map<number, LiveSegment>()
    for (const f of state.finals) if (f.seq !== null) out.set(f.seq, f)
    return out
  }, [state.finals])
  const marks = useMemo((): FloorMark[] => {
    if (!moments) return []
    const out: FloorMark[] = []
    for (const card of state.cards) {
      const m = moments.get(card.key)
      if (!m) continue
      if (!card.finding && card.trigger?.expiredAt != null) continue
      const label = card.finding?.title ?? card.trigger?.query ?? card.trigger?.topic ?? 'Working on it'
      out.push({ cardKey: card.key, atMs: m.startMs, label, pending: !card.finding })
    }
    return out
  }, [state.cards, moments])

  const blocks = useMemo((): Block[] => {
    if (!activeFocus) return turns.map((turn) => ({ kind: 'turn', turn }))
    const out: Block[] = []
    for (const turn of turns) {
      if (turn.speakerId === activeFocus) {
        out.push({ kind: 'turn', turn })
        continue
      }
      const last = out[out.length - 1]
      if (last?.kind === 'others') last.turns.push(turn)
      else out.push({ kind: 'others', key: `others:${turn.key}`, turns: [turn] })
    }
    return out
  }, [turns, activeFocus])

  const scrollRef = useRef<HTMLDivElement>(null)
  const pinned = useRef(true)
  const [parked, setParked] = useState(false)
  const [unseen, setUnseen] = useState(0)
  const finalsAtPark = useRef(0)
  const reduce = useReducedMotion()

  const toBottom = useCallback(
    (smooth: boolean) => {
      const el = scrollRef.current
      if (!el) return
      el.scrollTo({ top: el.scrollHeight, behavior: smooth && !reduce ? 'smooth' : 'auto' })
    },
    [reduce],
  )

  const onScroll = () => {
    const el = scrollRef.current
    if (!el) return
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < PIN_SLACK_PX
    if (atBottom !== pinned.current) {
      pinned.current = atBottom
      setParked(!atBottom)
      if (!atBottom) finalsAtPark.current = state.finals.length
      else setUnseen(0)
    }
  }

  // Follow the live edge (finals and the interim tail) while pinned.
  const interimText = state.interim?.text ?? ''
  useLayoutEffect(() => {
    if (!live) return
    if (pinned.current) toBottom(false)
  }, [live, rows.length, interimText, layout, activeFocus, toBottom])

  useEffect(() => {
    if (parked) setUnseen(Math.max(0, state.finals.length - finalsAtPark.current))
  }, [parked, state.finals.length])

  // Jump to a line (from an answer card or the floor). A line folded away by
  // the speaker focus clears the focus first (adjusted while rendering).
  const [seenJump, setSeenJump] = useState<number | null>(null)
  if (focus && focus.n !== seenJump) {
    setSeenJump(focus.n)
    const target = finalsBySeq.get(focus.seq)
    if (focusId && target && target.speakerId !== focusId) setFocusId(null)
  }
  const handled = useRef<number | null>(null)
  useEffect(() => {
    if (!focus || handled.current === focus.n) return
    const el = document.getElementById(`seg-${focus.seq}`)
    if (!el) return
    handled.current = focus.n
    el.scrollIntoView({ block: 'center', behavior: reduce ? 'auto' : 'smooth' })
  }, [focus, activeFocus, reduce])

  const speakerFor = (id: string | null): SpeakerEntry | undefined => (id ? state.speakers[id] : undefined)
  const ctx: LineContext = { answers, pending, focus, onOpenAnswer }
  const speakingEntry = speakerFor(speaking)

  return (
    <div className="relative flex min-h-0 flex-1 flex-col">
      {layout === 'solo' && cast[0] && <SoloHeader speaker={cast[0]} stat={stats.get(cast[0].id)} live={live} speaking={speaking === cast[0].id} />}
      {layout !== 'solo' && (
        <Floor
          cast={cast}
          spans={spans}
          stats={stats}
          marks={marks}
          lines={finalsBySeq}
          endMs={endMs}
          speakingId={speaking}
          focusId={activeFocus}
          onFocus={setFocusId}
          onJump={onJumpToLine}
          onOpenMark={onOpenAnswer}
        />
      )}
      {activeFocus && (
        <div className="flex shrink-0 items-center gap-2 border-b border-border bg-surface-2 px-4 py-1.5 text-[12px] text-muted">
          <SpeakerAvatar speaker={speakerFor(activeFocus)} size={16} />
          <span className="min-w-0 truncate">
            Only <span className="font-medium text-fg">{speakerName(speakerFor(activeFocus))}</span>; everyone else is folded
          </span>
          <button
            type="button"
            onClick={() => setFocusId(null)}
            className="ml-auto inline-flex h-6 items-center gap-1 rounded-md px-2 font-medium text-fg-2 hover:bg-hover hover:text-fg"
          >
            <X className="size-3.5" aria-hidden />
            Show everyone
          </button>
        </div>
      )}
      <div
        ref={scrollRef}
        onScroll={onScroll}
        className="min-h-0 flex-1 overflow-y-auto overscroll-contain"
        role="log"
        aria-label="Transcript"
        aria-live="off"
      >
        {turns.length === 0 ? (
          <div className="px-4 sm:px-5">{empty}</div>
        ) : layout === 'solo' ? (
          <div className="mx-auto w-full max-w-[46rem] px-4 pt-2 pb-14 sm:px-6">
            {turns.map((turn) => (
              <SoloTurn key={turn.key} turn={turn} ctx={ctx} />
            ))}
            {footer}
          </div>
        ) : layout === 'duo' ? (
          <div className="flex flex-col px-3 pt-1 pb-14 sm:px-5">
            {blocks.map((b) =>
              b.kind === 'others' ? (
                <Folded key={b.key} turns={b.turns} speakerFor={speakerFor} onShowAll={() => setFocusId(null)} />
              ) : (
                <DuoTurn
                  key={b.turn.key}
                  turn={b.turn}
                  speaker={speakerFor(b.turn.speakerId)}
                  right={lane.get(b.turn.speakerId ?? '') === 1}
                  speaking={isSpeakingTurn(b.turn, speaking)}
                  ctx={ctx}
                />
              ),
            )}
            {footer}
          </div>
        ) : (
          <RailsView cast={cast} blocks={blocks} lane={lane} speaking={speaking} focusId={activeFocus} speakerFor={speakerFor} ctx={ctx} footer={footer} onShowAll={() => setFocusId(null)} />
        )}
      </div>

      {/* Newest final, announced politely for screen readers. */}
      <p className="sr-only" aria-live="polite" aria-atomic="true">
        {live && state.lastFinal ? `${speakerName(speakerFor(state.lastFinal.speakerId))}: ${state.lastFinal.text}` : ''}
      </p>

      <AnimatePresence>
        {live && parked && (
          <motion.button
            type="button"
            initial={{ opacity: 0, y: 12 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: 12 }}
            transition={{ duration: 0.25, ease: EASE }}
            onClick={() => {
              pinned.current = true
              setParked(false)
              setUnseen(0)
              toBottom(true)
            }}
            className="absolute bottom-4 left-1/2 inline-flex h-8 -translate-x-1/2 items-center gap-2 rounded-full border border-border-strong bg-surface pr-3.5 pl-2 text-[12px] font-medium whitespace-nowrap text-fg shadow-pop hover:bg-surface-2"
          >
            {speakingEntry ? (
              <SpeakerAvatar speaker={speakingEntry} size={20} speaking />
            ) : (
              <span className="relative ml-1 flex size-1.5">
                <span className="absolute inset-0 blink rounded-full bg-fail" />
              </span>
            )}
            Jump to live
            {unseen > 0 && <span className="tabular rounded-full bg-selected px-1.5 mono text-[10.5px] text-muted">+{unseen}</span>}
            <ArrowDown className="size-3.5 text-muted" aria-hidden />
          </motion.button>
        )}
      </AnimatePresence>
    </div>
  )
}

const isSpeakingTurn = (turn: Turn, speaking: string | null) =>
  !!speaking && turn.speakerId === speaking && !turn.lines[turn.lines.length - 1].final

/** One voice: the speaker once at the top, then their words as paragraphs
 *  with the time in the margin. */
function SoloHeader({ speaker, stat, live, speaking }: { speaker: SpeakerEntry; stat: SpeakerStat | undefined; live: boolean; speaking: boolean }) {
  return (
    <div className="flex shrink-0 items-center gap-3 border-b border-border px-4 py-2.5 sm:px-5">
      <SpeakerAvatar speaker={speaker} size={28} speaking={speaking} />
      <div className="flex min-w-0 flex-col">
        <SpeakerName speaker={speaker} showSource />
        <span className="flex min-w-0 items-baseline gap-1.5 text-[12px] text-muted">
          {speaking ? 'Speaking now' : live ? 'The only voice so far' : 'The only voice'}
          {speaker.person && (
            <>
              <span aria-hidden className="text-subtle">
                ·
              </span>
              <PersonIdentity person={speaker.person} />
            </>
          )}
        </span>
      </div>
      <span className="ml-auto flex items-baseline gap-3 text-[12px] text-muted">
        <span>
          <span className="mono tabular text-fg-2">{formatClock(stat?.talkMs ?? 0)}</span> talking
        </span>
        <span className="hidden sm:inline">
          <span className="mono tabular text-fg-2">{stat?.lines ?? 0}</span> lines
        </span>
      </span>
    </div>
  )
}

const SoloTurn = memo(function SoloTurn({ turn, ctx }: { turn: Turn; ctx: LineContext }) {
  return (
    <TurnMotion className="grid grid-cols-[2.75rem_minmax(0,1fr)] gap-x-3 pt-4 sm:grid-cols-[3.25rem_minmax(0,1fr)] sm:gap-x-4">
      <time className="mono tabular pt-[5px] text-right text-[11.5px] text-subtle">{formatClock(turn.startMs)}</time>
      <p className="text-[15px] leading-[1.7] text-pretty">
        {turn.lines.map((line) => (
          <Line key={line.id} line={line} ctx={ctx} inline />
        ))}
      </p>
    </TurnMotion>
  )
})

/** Two voices: a dialogue — the first speaker on the left, the second on the right. */
const DuoTurn = memo(function DuoTurn({
  turn,
  speaker,
  right,
  speaking,
  ctx,
}: {
  turn: Turn
  speaker: SpeakerEntry | undefined
  right: boolean
  speaking: boolean
  ctx: LineContext
}) {
  const cls = speakerClasses(speaker?.colorIndex)
  return (
    <TurnMotion className={cn('flex items-start gap-2.5 pt-3', right && 'flex-row-reverse')}>
      <SpeakerAvatar speaker={speaker} size={26} speaking={speaking} className="mt-[22px]" />
      <div className={cn('flex min-w-0 max-w-[min(86%,40rem)] flex-col', right && 'items-end')}>
        <header className={cn('flex min-w-0 items-center gap-2 px-1 pb-1', right && 'flex-row-reverse')}>
          <SpeakerName speaker={speaker} />
          <time className="mono tabular text-[11.5px] text-subtle">{formatClock(turn.startMs)}</time>
        </header>
        <div className={cn('flex min-w-0 flex-col rounded-2xl px-3.5 py-2', cls.tint, right ? 'rounded-tr-[5px]' : 'rounded-tl-[5px]')}>
          {turn.lines.map((line) => (
            <Line key={line.id} line={line} ctx={ctx} />
          ))}
        </div>
      </div>
    </TurnMotion>
  )
})

/**
 * Three or more voices on lanes: each speaker owns a rail in the gutter and
 * every turn hangs off its speaker's rail, so a person can be followed down
 * the page by position, colour, initials and name together.
 */
function RailsView({
  cast,
  blocks,
  lane,
  speaking,
  focusId,
  speakerFor,
  ctx,
  footer,
  onShowAll,
}: {
  cast: SpeakerEntry[]
  blocks: Block[]
  lane: Map<string, number>
  speaking: string | null
  focusId: string | null
  speakerFor: (id: string | null) => SpeakerEntry | undefined
  ctx: LineContext
  footer?: ReactNode
  onShowAll: () => void
}) {
  const style = { '--n': cast.length } as CSSProperties
  return (
    <div className="px-3 pt-1 pb-14 sm:px-5">
      <div className="relative [--lane:15px] sm:[--lane:22px]" style={style}>
        {cast.map((s, i) => (
          <span
            key={s.id}
            aria-hidden
            className={cn(
              'absolute top-0 bottom-0 w-[2px] -translate-x-1/2 rounded-full transition-opacity',
              speakerClasses(s.colorIndex).lane,
              focusId && focusId !== s.id && 'opacity-30',
            )}
            style={{ left: `calc(var(--lane) * ${i} + var(--lane) / 2)` }}
          />
        ))}
        {blocks.map((b) =>
          b.kind === 'others' ? (
            <div key={b.key} className="relative grid grid-cols-[calc(var(--lane)*var(--n)+10px)_minmax(0,1fr)]">
              <span />
              <Folded turns={b.turns} speakerFor={speakerFor} onShowAll={onShowAll} />
            </div>
          ) : (
            <RailTurn
              key={b.turn.key}
              turn={b.turn}
              speaker={speakerFor(b.turn.speakerId)}
              laneIndex={lane.get(b.turn.speakerId ?? '') ?? 0}
              speaking={isSpeakingTurn(b.turn, speaking)}
              ctx={ctx}
            />
          ),
        )}
        {footer && <div className="relative bg-surface">{footer}</div>}
      </div>
    </div>
  )
}

const RailTurn = memo(function RailTurn({
  turn,
  speaker,
  laneIndex,
  speaking,
  ctx,
}: {
  turn: Turn
  speaker: SpeakerEntry | undefined
  laneIndex: number
  speaking: boolean
  ctx: LineContext
}) {
  const cls = speakerClasses(speaker?.colorIndex)
  const x = `calc(var(--lane) * ${laneIndex} + var(--lane) / 2)`
  return (
    <TurnMotion className="relative grid grid-cols-[calc(var(--lane)*var(--n)+10px)_minmax(0,1fr)] pt-3.5">
      <div aria-hidden className="relative">
        <span className={cn('absolute top-[11px] right-0 h-[2px] rounded-full', cls.lane)} style={{ left: x }} />
        <span className="absolute top-0 -translate-x-1/2" style={{ left: x }}>
          <SpeakerAvatar speaker={speaker} size={22} speaking={speaking} className="rounded-full ring-[3px] ring-surface" />
        </span>
      </div>
      <div className="flex min-w-0 flex-col pl-2">
        <header className="flex min-h-[22px] min-w-0 items-center gap-2">
          <SpeakerName speaker={speaker} />
          <time className="mono tabular text-[11.5px] text-subtle">{formatClock(turn.startMs)}</time>
        </header>
        <div className="flex flex-col">
          {turn.lines.map((line) => (
            <Line key={line.id} line={line} ctx={ctx} />
          ))}
        </div>
      </div>
    </TurnMotion>
  )
})

/** Turns folded away by a speaker focus, as one quiet row. */
function Folded({
  turns,
  speakerFor,
  onShowAll,
}: {
  turns: Turn[]
  speakerFor: (id: string | null) => SpeakerEntry | undefined
  onShowAll: () => void
}) {
  const who = [...new Set(turns.map((t) => t.speakerId))].map(speakerFor)
  const lines = turns.reduce((n, t) => n + t.lines.length, 0)
  return (
    <button
      type="button"
      onClick={onShowAll}
      className="my-1.5 flex min-w-0 items-center gap-2 self-stretch rounded-md border border-dashed border-border px-2.5 py-1 text-left text-[12px] text-subtle transition-colors hover:border-border-strong hover:text-muted"
    >
      <span className="flex -space-x-1">
        {who.slice(0, 4).map((s, i) => (
          <SpeakerAvatar key={s?.id ?? i} speaker={s} size={14} className="rounded-full ring-2 ring-surface" />
        ))}
      </span>
      <span className="min-w-0 truncate">
        {lines} line{lines === 1 ? '' : 's'} from {who.map((s) => speakerName(s)).join(', ')}
      </span>
    </button>
  )
}

function TurnMotion({ className, style, children }: { className?: string; style?: CSSProperties; children: ReactNode }) {
  const reduce = useReducedMotion()
  return (
    <motion.section
      initial={reduce ? false : { opacity: 0, y: 6 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.35, ease: EASE }}
      className={className}
      style={style}
    >
      {children}
    </motion.section>
  )
}

/** One transcript line: shimmers while the agent acts on it, carries its
 *  answers as small tags, flashes when jumped to. `inline` flows it into a
 *  paragraph (the solo layout). */
function Line({ line, ctx, inline = false }: { line: LiveSegment; ctx: LineContext; inline?: boolean }) {
  const seq = line.seq
  const answers = seq != null ? ctx.answers.get(seq) : undefined
  const hot = seq != null && ctx.pending.some((r) => seq >= r.from && seq <= r.to)
  const flashN = ctx.focus && seq === ctx.focus.seq ? ctx.focus.n : null
  const Tag = inline ? 'span' : 'p'
  return (
    <>
      <Tag
        key={flashN ?? 'line'}
        id={seq != null ? `seg-${seq}` : undefined}
        data-seq={seq ?? undefined}
        className={cn(
          'scroll-mt-24 rounded-md transition-colors',
          inline ? 'box-decoration-clone py-[1px]' : '-mx-1.5 px-1.5 py-[2px] text-[14px] leading-[1.6]',
          line.final ? 'text-fg' : 'text-muted',
          hot && 'agent-shimmer',
          answers && 'bg-brand-soft/60',
          flashN !== null && 'flash',
        )}
      >
        {line.text}
        {!line.final && <span aria-hidden className="ml-0.5 inline-block h-[1.05em] w-[2px] translate-y-[3px] caret rounded-full bg-brand" />}
        {answers?.map((a) => {
          const meta = gateActionMeta(a.actionId)
          const Icon = meta.icon
          return (
            <button
              key={a.cardKey}
              type="button"
              onClick={() => ctx.onOpenAnswer(a.cardKey)}
              title={a.title}
              className="relative ml-1.5 inline-flex h-5 translate-y-[-1px] items-center gap-1 rounded-[5px] border border-brand-line bg-brand-soft px-1.5 align-middle text-[11px] font-medium text-brand-text transition-colors hover:border-brand"
            >
              <Icon className="size-3" aria-hidden />
              {meta.label}
              <span className="sr-only">: open answer, {a.title}</span>
            </button>
          )
        })}
      </Tag>
      {inline && ' '}
    </>
  )
}
