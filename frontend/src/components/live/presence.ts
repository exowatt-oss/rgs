import type { AgentState } from '@/components/AgentMark'
import type { RailCard } from '@/lib/live/liveReducer'
import { gateActionMeta } from '@/lib/live/presentation'

export interface PresenceInfo {
  state: AgentState
  title: string
  detail: string | null
}

/** What the agent is doing right now, in words. */
export function describePresence({
  ended,
  offline,
  cards,
  now,
}: {
  ended: boolean
  offline: boolean
  cards: RailCard[]
  now: number
}): PresenceInfo {
  if (ended) return { state: 'ended', title: 'Call ended', detail: 'The transcript and every answer are saved.' }
  if (offline) return { state: 'offline', title: 'Reconnecting', detail: 'Audio is buffered on this device. Nothing is lost.' }
  const working = cards.find((c) => c.trigger && !c.finding && c.trigger.expiredAt === null)
  if (working?.trigger) {
    const meta = gateActionMeta(working.trigger.actionId)
    return { state: 'thinking', title: `${meta.verb}…`, detail: working.trigger.query || working.trigger.topic }
  }
  const latest = cards.find((c) => c.finding)
  const at = latest?.finding?.createdAt ? Date.parse(latest.finding.createdAt) : null
  if (latest?.finding && at !== null && now - at < 6000) return { state: 'found', title: 'Answered', detail: latest.finding.title }
  return { state: 'listening', title: 'Listening', detail: 'Ask out loud: answers land below.' }
}

