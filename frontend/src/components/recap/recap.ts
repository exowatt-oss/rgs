/**
 * Recap plumbing: the start-screen delivery preference and the live query that
 * follows a finished session's recap (notes → newspaper edition → Slack/email).
 */
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useCallback, useEffect, useRef, useState } from 'react'

import { liveApi, recapInFlight, type DeliveryChannel, type LiveSessionInfo } from '@/api/live'
import { isMockMode } from '@/lib/mockMode'
import { mockStore } from '@/mock/store'

export type DeliveryChoice = 'slack' | 'email' | 'both'

const PREF_KEY = 'rgs.recap.deliverVia'

export const channelsOf = (choice: DeliveryChoice): DeliveryChannel[] =>
  choice === 'both' ? ['slack', 'email'] : [choice]

export const choiceOf = (channels: readonly DeliveryChannel[]): DeliveryChoice | null =>
  channels.includes('slack') && channels.includes('email')
    ? 'both'
    : channels.includes('email')
      ? 'email'
      : channels.includes('slack')
        ? 'slack'
        : null

function readChoice(): DeliveryChoice {
  try {
    const v = window.localStorage.getItem(PREF_KEY)
    if (v === 'slack' || v === 'email' || v === 'both') return v
  } catch {
    /* private mode */
  }
  return 'slack'
}

/** Slack / Email / Both, remembered on this browser (default Slack). */
export function useDeliveryChoice(): [DeliveryChoice, (v: DeliveryChoice) => void] {
  const [choice, setChoice] = useState<DeliveryChoice>(readChoice)
  const set = useCallback((v: DeliveryChoice) => {
    setChoice(v)
    try {
      window.localStorage.setItem(PREF_KEY, v)
    } catch {
      /* private mode */
    }
  }, [])
  return [choice, set]
}

export const savedDeliveryChannels = (): DeliveryChannel[] => channelsOf(readChoice())

/** "on Slack", "by email", "on Slack and by email". */
export function deliveryPhrase(channels: readonly DeliveryChannel[]): string {
  const parts = channels.map((c) => (c === 'slack' ? 'on Slack' : 'by email'))
  return parts.length === 0 ? 'here in RGS' : parts.join(' and ')
}

/** A session stopped in the last couple of minutes may not have flagged its recap yet. */
const justStopped = (s: LiveSessionInfo) => {
  const t = s.stoppedAt ? Date.parse(s.stoppedAt) : NaN
  return Number.isFinite(t) && Date.now() - t < 120_000
}

export function recapPollMs(s: LiveSessionInfo | undefined): number | false {
  if (!s) return 3_000
  if (s.status === 'live') return 10_000
  if (s.status === 'stopping') return 1_500
  if (recapInFlight(s.recap)) return 2_000
  if (s.status === 'finalized' && s.recap?.enabled && s.recap.status === null && justStopped(s)) return 2_000
  return false
}

/** Follows one session until its recap settles; refreshes the lists when it does. */
export function useSessionRecap(id: string | null | undefined) {
  const qc = useQueryClient()
  const mock = isMockMode()
  const query = useQuery({
    queryKey: ['live', 'recap', id, mock],
    enabled: !!id,
    queryFn: async (): Promise<LiveSessionInfo> => {
      if (mock) {
        const s = mockStore.get(id!)
        if (!s) throw new Error('not found')
        return s
      }
      return liveApi.get(id!)
    },
    refetchInterval: (q) => recapPollMs(q.state.data),
  })
  const status = query.data?.recap?.status ?? null
  const prev = useRef(status)
  useEffect(() => {
    if (prev.current !== status && (status === 'done' || status === 'failed')) {
      void qc.invalidateQueries({ queryKey: ['live', 'sessions'] })
    }
    prev.current = status
  }, [status, qc])
  return query
}
