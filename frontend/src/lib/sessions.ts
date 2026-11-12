/**
 * Read-only session data for the Sessions pages: the list, and one past
 * session folded through the same reducer the live view uses (so speakers,
 * self-introduction names and finding ↔ line links render identically).
 * Never touches the live controller, so browsing works mid-call.
 */
import { useQuery } from '@tanstack/react-query'

import { liveApi, type LiveEvent, type LiveSessionInfo } from '@/api/live'
import { fetchOnDevicePhrases } from '@/lib/asr/onDevice'
import { initialLiveState, liveReducer, type LiveState } from '@/lib/live/liveReducer'
import { isMockMode } from '@/lib/mockMode'
import { mockStore } from '@/mock/store'

export function useSessionList() {
  return useQuery({
    queryKey: ['live', 'sessions', isMockMode()],
    queryFn: async (): Promise<LiveSessionInfo[]> => {
      if (isMockMode()) return mockStore.list(false)
      return liveApi.list(false, 50)
    },
    refetchInterval: 15_000,
  })
}

export function useActiveSessions() {
  return useQuery({
    queryKey: ['live', 'sessions', 'active', isMockMode()],
    queryFn: async (): Promise<LiveSessionInfo[]> => {
      if (isMockMode()) return mockStore.list(true)
      return liveApi.list(true, 20)
    },
    refetchInterval: 10_000,
  })
}

export interface SessionArchive {
  session: LiveSessionInfo
  state: LiveState
}

export function useSessionArchive(id: string | undefined) {
  return useQuery({
    queryKey: ['live', 'archive', id, isMockMode()],
    enabled: !!id,
    queryFn: async (): Promise<SessionArchive> => {
      if (isMockMode()) {
        const session = mockStore.get(id!)
        if (!session) throw new Error('not found')
        return { session, state: fold(session, mockStore.events(id!)) }
      }
      const [session, transcript, findings, phrases] = await Promise.all([
        liveApi.get(id!),
        liveApi.transcript(id!),
        liveApi.findings(id!),
        fetchOnDevicePhrases(id!),
      ])
      const events: LiveEvent[] = [
        ...transcript.speakers.map((speaker): LiveEvent => ({ type: 'speaker.update', seq: null, ts: null, speaker })),
        ...transcript.segments.map((segment): LiveEvent => ({ type: 'segment.final', seq: segment.seq, ts: null, segment })),
        ...phrases.map((phrase): LiveEvent => ({ type: 'ondevice.segment', seq: null, ts: null, phrase })),
        ...findings.map((finding): LiveEvent => ({ type: 'finding.new', seq: null, ts: null, finding })),
      ]
      return { session, state: fold(session, events) }
    },
  })
}

function fold(session: LiveSessionInfo, events: LiveEvent[]): LiveState {
  let state = liveReducer(initialLiveState, { type: 'session', session })
  // Old, so nothing counts as a fresh fire.
  state = liveReducer(state, { type: 'events', events, now: Date.now() + 3_600_000 })
  return state
}
