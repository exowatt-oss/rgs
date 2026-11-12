import { AnimatePresence, motion, useReducedMotion } from 'motion/react'
import { ArrowUpRight, CalendarClock, CheckSquare, CornerDownRight, FolderKanban, Gavel, Link2, Sparkles } from 'lucide-react'
import type { FindingLink, LiveSegment } from '@/api/live'
import { MiniMarkdown } from '@/components/live/MiniMarkdown'
import { speakerName, type RailCard, type SpeakerEntry } from '@/lib/live/liveReducer'
import { formatClock, gateActionMeta, speakerClasses } from '@/lib/live/presentation'
import { cn } from '@/lib/utils'

const EASE = [0.22, 1, 0.36, 1] as const

const LINK_ICONS: Record<FindingLink['kind'], typeof Link2> = {
  meeting: CalendarClock,
  decision: Gavel,
  action_item: CheckSquare,
  project: FolderKanban,
  url: Link2,
}

/**
 * One thing the agent did: a pending lookup (the gate fired, an agent is
 * working) that resolves in place into a grounded answer, pinned to the
 * transcript line that asked for it.
 */
export function AnswerCard({
  card,
  moment,
  momentSpeaker,
  flashN,
  onJump,
}: {
  card: RailCard
  moment: LiveSegment | null
  momentSpeaker: SpeakerEntry | undefined
  flashN: number | null
  onJump: (seq: number) => void
}) {
  const reduce = useReducedMotion()
  const { trigger, finding } = card
  const actionId = trigger?.actionId ?? null
  const meta = finding && !trigger ? { label: 'Note', verb: 'Noting', icon: Sparkles } : gateActionMeta(actionId)
  const Icon = meta.icon
  const expired = !finding && trigger?.expiredAt != null
  const pending = !finding && !expired
  const momentSeq = moment?.seq ?? null

  return (
    <motion.article
      id={`card-${card.key}`}
      layout={reduce ? false : 'position'}
      initial={reduce ? { opacity: 0 } : { opacity: 0, y: -10, scale: 0.98 }}
      animate={{ opacity: expired ? 0.45 : 1, y: 0, scale: 1 }}
      exit={{ opacity: 0, scale: 0.98, transition: { duration: 0.3 } }}
      transition={{ duration: 0.45, ease: EASE }}
      className="relative scroll-mt-14 px-4 py-3.5"
      aria-busy={pending || undefined}
    >
      {/* The agent's edge: a gradient bar while it works. */}
      {pending && <span aria-hidden className="ex-bg-gradient absolute inset-y-0 left-0 w-[2px]" />}
      {flashN !== null && (
        <motion.span
          key={flashN}
          aria-hidden
          initial={{ opacity: 1 }}
          animate={{ opacity: 0 }}
          transition={{ duration: 1.6, ease: 'easeOut', delay: 0.3 }}
          className="pointer-events-none absolute inset-0 bg-brand-soft ring-1 ring-inset ring-brand/50"
        />
      )}

      <header className="flex items-center gap-2">
        <span
          aria-hidden
          className={cn(
            'relative inline-grid size-6 place-items-center rounded-md',
            pending ? 'bg-brand-soft text-brand' : finding ? 'bg-brand-soft text-brand-text' : 'bg-selected text-muted',
          )}
        >
          <Icon className="size-3.5" />
          {pending && !reduce && (
            <motion.span
              className="absolute inset-0 rounded-md ring-1 ring-brand/60"
              animate={{ opacity: [0.9, 0.15, 0.9] }}
              transition={{ duration: 1.4, repeat: Infinity, ease: 'easeInOut' }}
            />
          )}
        </span>
        <span className="text-[12px] font-medium text-fg-2">{meta.label}</span>
        <span className="mono ml-auto text-[11.5px] text-subtle">
          {pending ? (trigger?.claimedAt ? 'agent on it' : 'deciding') : expired ? 'no answer' : moment ? `at ${formatClock(moment.startMs)}` : ''}
        </span>
      </header>

      <AnimatePresence mode="wait" initial={false}>
        {finding ? (
          <motion.div
            key="answer"
            initial={reduce ? { opacity: 0 } : { opacity: 0, y: 6 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.4, ease: EASE }}
            className="mt-2.5 flex flex-col gap-2"
          >
            <h3 className="text-[14px] leading-snug font-semibold text-pretty text-fg">{finding.title}</h3>
            {finding.bodyMarkdown && <MiniMarkdown source={finding.bodyMarkdown} className="text-[13px] leading-relaxed text-muted" />}
            {finding.links.length > 0 && (
              <ul className="flex flex-wrap gap-1.5 pt-0.5">
                {finding.links.map((link) => {
                  const LinkIcon = LINK_ICONS[link.kind]
                  return (
                    <li key={link.url}>
                      <a
                        href={link.url}
                        target="_blank"
                        rel="noreferrer"
                        className="inline-flex h-6 max-w-full items-center gap-1.5 rounded-md border border-border bg-surface px-2 text-[12px] text-fg-2 shadow-card transition-colors hover:border-border-strong hover:text-fg"
                      >
                        <LinkIcon className="size-3 shrink-0" aria-hidden />
                        <span className="truncate">{link.title}</span>
                        <ArrowUpRight className="size-3 shrink-0 text-subtle" aria-hidden />
                      </a>
                    </li>
                  )
                })}
              </ul>
            )}
          </motion.div>
        ) : (
          <motion.div key="pending" exit={{ opacity: 0 }} transition={{ duration: 0.2 }} className="mt-2.5 flex flex-col gap-2">
            <p className="text-[13.5px] leading-snug text-fg">
              <span className="text-muted">{expired ? 'Couldn’t find' : `${meta.verb}`}</span>{' '}
              <span className="font-medium">{trigger?.query || trigger?.topic || 'this'}</span>
              {pending && <span className="text-muted">…</span>}
            </p>
            {pending && (
              <div className="flex flex-col gap-1.5" aria-hidden>
                <span className="agent-shimmer h-2 w-[86%] rounded-full bg-selected" />
                <span className="agent-shimmer h-2 w-[62%] rounded-full bg-selected" />
              </div>
            )}
          </motion.div>
        )}
      </AnimatePresence>

      {moment && momentSeq !== null && (
        <button
          type="button"
          onClick={() => onJump(momentSeq)}
          className="group mt-3 flex w-full items-start gap-2 rounded-md border border-border bg-surface-2 px-2.5 py-2 text-left transition-colors hover:border-border-strong"
          title="Jump to this moment in the transcript"
        >
          <CornerDownRight className="mt-0.5 size-3.5 shrink-0 text-subtle transition-colors group-hover:text-brand" aria-hidden />
          <span className="min-w-0 flex-1 text-[12px] leading-relaxed text-muted">
            <span className="inline-flex items-center gap-1.5 font-medium text-fg-2">
              <span aria-hidden className={cn('size-2 rounded-full', speakerClasses(momentSpeaker?.colorIndex).dot)} />
              {speakerName(momentSpeaker)}
            </span>{' '}
            <span className="line-clamp-2">“{moment.text}”</span>
          </span>
        </button>
      )}
    </motion.article>
  )
}
