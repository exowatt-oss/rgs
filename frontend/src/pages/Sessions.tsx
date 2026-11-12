import { Search } from 'lucide-react'
import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'

import { Button, Empty, ErrorNote, PageBody, PageHeader, Panel, Segmented, Stat, inputClass } from '@/components/kit'
import { SessionTable, isLiveSession, sessionDurationS } from '@/components/sessions/SessionTable'
import { useAuth } from '@/lib/auth'
import { useNow } from '@/lib/hooks'
import { useSessionList } from '@/lib/sessions'
import { cn } from '@/lib/utils'

type Scope = 'all' | 'mine' | 'live'

export function SessionsPage() {
  const { data, isLoading, isError, error, refetch } = useSessionList()
  const { user } = useAuth()
  const now = useNow(15_000)
  const [query, setQuery] = useState('')
  const [scope, setScope] = useState<Scope>('all')

  const email = user?.email ?? null
  const all = useMemo(() => (data ?? []).filter((s) => s.status !== 'cancelled'), [data])
  const sessions = useMemo(() => {
    const q = query.trim().toLowerCase()
    return all.filter((s) => {
      if (scope === 'live' && !isLiveSession(s)) return false
      if (scope === 'mine' && !(s.isOwner || (email && s.startedBy?.email === email))) return false
      return !q || `${s.title ?? ''} ${s.meetCode ?? ''} ${s.startedBy?.name ?? ''}`.toLowerCase().includes(q)
    })
  }, [all, query, scope, email])

  const weekAgo = now - 7 * 86_400_000
  const thisWeek = all.filter((s) => s.startedAt && Date.parse(s.startedAt) >= weekAgo)
  const hours = thisWeek.reduce((acc, s) => acc + (sessionDurationS(s) ?? 0), 0) / 3600
  const liveCount = all.filter(isLiveSession).length

  return (
    <>
      <PageHeader title="Sessions" subtitle="Every call RGS has listened to, with its transcript and answers" />
      <PageBody className="flex flex-col gap-5">
        <Panel>
          <div className="grid grid-cols-2 gap-x-4 gap-y-5 p-5 md:grid-cols-4">
            <Stat label="Sessions" value={isLoading ? '—' : all.length} hint="kept, not discarded" />
            <Stat label="Live now" value={isLoading ? '—' : liveCount} tone={liveCount > 0 ? 'text-brand-text' : undefined} hint="open to follow" />
            <Stat label="This week" value={isLoading ? '—' : thisWeek.length} hint="calls in the last 7 days" />
            <Stat label="Listened" value={isLoading ? '—' : `${hours < 10 ? hours.toFixed(1) : Math.round(hours)} h`} hint="of audio this week" />
          </div>
        </Panel>

        <Panel
          title="Calls"
          aside={
            <div className="flex flex-wrap items-center gap-2">
              <Segmented
                label="Show"
                value={scope}
                onChange={setScope}
                options={[
                  { value: 'all', label: 'All' },
                  { value: 'mine', label: 'Mine' },
                  { value: 'live', label: 'Live' },
                ]}
              />
              <label className="relative block w-56">
                <span className="sr-only">Filter sessions</span>
                <Search className="pointer-events-none absolute top-[7px] left-2.5 h-3.5 w-3.5 text-subtle" aria-hidden />
                <input
                  value={query}
                  onChange={(e) => setQuery(e.target.value)}
                  placeholder="Title, code or person"
                  className={cn(inputClass, 'h-7 pl-8 text-[12.5px]')}
                />
              </label>
            </div>
          }
        >
          {isError ? (
            <div className="flex items-center justify-between gap-4 px-4 py-4">
              <ErrorNote error={error ?? new Error('Couldn’t load sessions.')} />
              <Button onClick={() => void refetch()}>Retry</Button>
            </div>
          ) : (
            <SessionTable
              sessions={sessions}
              loading={isLoading}
              now={now}
              rows={6}
              empty={
                <Empty>
                  {query || scope !== 'all' ? (
                    'No calls match.'
                  ) : (
                    <>
                      No calls yet.{' '}
                      <Link to="/" className="font-medium text-brand-text hover:underline">
                        Start one
                      </Link>
                      .
                    </>
                  )}
                </Empty>
              }
            />
          )}
        </Panel>
      </PageBody>
    </>
  )
}
