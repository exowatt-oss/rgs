import { AnimatePresence } from 'motion/react'
import type { ReactNode } from 'react'

import type { LiveSegment } from '@/api/live'
import { Panel } from '@/components/kit'
import { AnswerCard } from '@/components/live/AnswerCard'
import { resolveSpeakerId, type LiveState } from '@/lib/live/liveReducer'

/** The agent's answers (newest first), each linked to the line that asked. */
export function AnswersPanel({
  state,
  moments,
  focusCard,
  onJumpToLine,
  emptyHint,
  className,
}: {
  state: LiveState
  moments: Map<string, LiveSegment | null>
  focusCard: { key: string; n: number } | null
  onJumpToLine: (seq: number) => void
  emptyHint?: ReactNode
  className?: string
}) {
  const answered = state.cards.filter((c) => c.finding).length
  const working = state.cards.length - answered
  return (
    <Panel
      title="Answers"
      className={className}
      aside={
        <span className="tabular">
          {answered}
          {working > 0 && <span className="text-brand-text"> · {working} working</span>}
        </span>
      }
    >
      {state.cards.length === 0 ? (
        <div className="flex flex-col gap-3 px-4 py-4">
          <p className="text-[12.5px] text-muted">
            When someone asks for something RGS can look up, the answer appears here, pinned to the line that asked.
          </p>
          {emptyHint}
        </div>
      ) : (
        <div className="divide-y divide-border">
          <AnimatePresence initial={false}>
            {state.cards.map((card) => {
              const moment = moments.get(card.key) ?? null
              const speakerId = moment ? resolveSpeakerId(state, moment.speakerId) : null
              return (
                <AnswerCard
                  key={card.key}
                  card={card}
                  moment={moment}
                  momentSpeaker={speakerId ? state.speakers[speakerId] : undefined}
                  flashN={focusCard?.key === card.key ? focusCard.n : null}
                  onJump={onJumpToLine}
                />
              )
            })}
          </AnimatePresence>
        </div>
      )}
    </Panel>
  )
}
