import { useCallback, useMemo, useRef, useState } from 'react'

import type { LiveSegment } from '@/api/live'
import type { LineAnswer } from '@/components/live/Transcript'
import { findMomentSegment, type LiveState } from '@/lib/live/liveReducer'

/**
 * Two-way links between answer cards and transcript lines: which line each
 * card points at, which lines carry answers, and the "jump + flash" state in
 * both directions.
 */
export function useAnswerLinks(state: LiveState) {
  const moments = useMemo(() => {
    const out = new Map<string, LiveSegment | null>()
    for (const card of state.cards) {
      out.set(
        card.key,
        findMomentSegment(state, {
          seq: card.finding?.relatedSeq ?? null,
          seqFrom: card.trigger?.seqFrom ?? null,
          seqTo: card.trigger?.seqTo ?? null,
        }),
      )
    }
    return out
    // eslint-disable-next-line react-hooks/exhaustive-deps -- finals + cards are the inputs
  }, [state.cards, state.finals])

  const answers = useMemo(() => {
    const out = new Map<number, LineAnswer[]>()
    for (const card of state.cards) {
      if (!card.finding) continue
      const seg = moments.get(card.key)
      if (seg?.seq == null) continue
      const list = out.get(seg.seq) ?? []
      list.push({ cardKey: card.key, actionId: card.trigger?.actionId ?? null, title: card.finding.title })
      out.set(seg.seq, list)
    }
    return out
  }, [state.cards, moments])

  const [focusLine, setFocusLine] = useState<{ seq: number; n: number } | null>(null)
  const [focusCard, setFocusCard] = useState<{ key: string; n: number } | null>(null)
  const counter = useRef(0)

  const jumpToLine = useCallback((seq: number) => setFocusLine({ seq, n: ++counter.current }), [])
  const jumpToCard = useCallback((key: string) => {
    setFocusCard({ key, n: ++counter.current })
    // After the rail has rendered the card (it may have just switched tabs).
    requestAnimationFrame(() =>
      requestAnimationFrame(() => document.getElementById(`card-${key}`)?.scrollIntoView({ block: 'nearest', behavior: 'smooth' })),
    )
  }, [])
  return { moments, answers, focusLine, focusCard, jumpToLine, jumpToCard }
}
