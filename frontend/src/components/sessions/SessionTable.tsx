import { ChevronRight, Newspaper } from 'lucide-react'
import { Link } from 'react-router-dom'

import { gateProviderLabel, recapInFlight, type LiveSessionInfo } from '@/api/live'
import { Avatar, Empty, Skeleton, Tag, Time } from '@/components/kit'
import { SessionBadge } from '@/components/status'
import { formatClock, formatDuration } from '@/lib/live/presentation'
import { cn } from '@/lib/utils'

const GRID = 'md:grid md:grid-cols-[minmax(0,1fr)_150px_88px_72px_130px_92px_16px] md:items-center md:gap-x-4'

export function sessionDurationS(s: LiveSessionInfo): number | null {
  if (s.audioSeconds != null) return s.audioSeconds
  if (s.startedAt && s.stoppedAt) return (Date.parse(s.stoppedAt) - Date.parse(s.startedAt)) / 1000
  return null
}

export const isLiveSession = (s: LiveSessionInfo) => s.status === 'live' || s.status === 'stopping'

/** Dense session table (devboxes list style): one row per call. */
export function SessionTable({
  sessions,
  loading,
  now,
  empty,
  rows = 5,
}: {
  sessions: LiveSessionInfo[] | undefined
  loading?: boolean
  now: number
  empty?: React.ReactNode
  rows?: number
}) {
  return (
    <div>
      <div aria-hidden className={cn('hidden border-b border-border px-4 py-2 text-[12px] font-medium text-muted', GRID)}>
        <span>Call</span>
        <span>Started by</span>
        <span>Started</span>
        <span>Length</span>
        <span>Decision gate</span>
        <span>Status</span>
        <span />
      </div>
      {loading ? (
        <ul className="divide-y divide-border" aria-busy>
          {Array.from({ length: rows }, (_, i) => (
            <li key={i} className="flex items-center gap-4 px-4 py-3">
              <Skeleton className="h-4 w-56" />
              <Skeleton className="ml-auto h-4 w-24" />
            </li>
          ))}
        </ul>
      ) : !sessions?.length ? (
        (empty ?? <Empty>No calls yet.</Empty>)
      ) : (
        <ul aria-label="Sessions" className="divide-y divide-border">
          {sessions.map((s) => (
            <SessionRow key={s.id} s={s} now={now} />
          ))}
        </ul>
      )}
    </div>
  )
}

/** The newspaper edition printed from a session (sits above the row link). */
function EditionLink({ s }: { s: LiveSessionInfo }) {
  const r = s.recap
  const url = r?.edition?.shareUrl
  if (url) {
    return (
      <a
        href={url}
        target="_blank"
        rel="noreferrer"
        title={r?.edition?.headline ?? 'Open the newspaper edition'}
        className="relative z-[1] inline-flex h-[18px] shrink-0 items-center gap-1 rounded-[5px] border border-brand-line bg-brand-soft px-1.5 text-[11px] font-medium whitespace-nowrap text-brand-text transition-colors hover:border-brand/60 hover:bg-brand/15"
      >
        <Newspaper className="h-3 w-3" aria-hidden />
        Edition{r?.edition?.number ? ` ${r.edition.number}` : ''}
      </a>
    )
  }
  if (recapInFlight(r)) {
    return (
      <span className="inline-flex h-[18px] shrink-0 items-center gap-1 rounded-[5px] border border-border px-1.5 text-[11px] font-medium whitespace-nowrap text-muted">
        <Newspaper className="blink h-3 w-3" aria-hidden />
        Printing…
      </span>
    )
  }
  return null
}

function SessionRow({ s, now }: { s: LiveSessionInfo; now: number }) {
  const live = isLiveSession(s)
  const dur = sessionDurationS(s)
  const started = s.startedAt ? Date.parse(s.startedAt) : null
  const edition = !!s.recap?.edition?.shareUrl || recapInFlight(s.recap)
  const subline = s.meetCode ?? (s.kind === 'adhoc' ? 'In person' : '—')
  return (
    <li className="relative">
      {/* Outside the row link (no nested anchors), laid over the title cell
          right after its subline: an invisible copy of the subline holds the space. */}
      {edition && (
        <span className="pointer-events-none absolute top-[26px] left-4 flex max-w-[calc(100%-2rem)] items-center gap-2">
          <span aria-hidden className={cn('invisible truncate text-[11.5px]', s.meetCode && 'mono')}>
            {subline}
          </span>
          <span className="pointer-events-auto">
            <EditionLink s={s} />
          </span>
        </span>
      )}
      <Link
        to={live ? `/live/${s.id}` : `/sessions/${s.id}`}
        className={cn('group flex flex-col gap-1 px-4 py-2.5 transition-colors hover:bg-hover', GRID)}
      >
        <span className="flex min-w-0 items-start justify-between gap-3 md:block">
          <span className="flex min-w-0 flex-col leading-tight">
            <span className="truncate text-[13px] font-medium text-fg">{s.title ?? s.meetCode ?? 'Untitled call'}</span>
            <span className={cn('mt-1 truncate text-[11.5px] text-subtle', s.meetCode && 'mono')}>{subline}</span>
          </span>
          <span className="shrink-0 md:hidden">
            <SessionBadge status={s.status} />
          </span>
        </span>
        {/* Phone: one meta line under the title. */}
        <span className="flex min-w-0 items-center gap-1.5 text-[12px] text-muted md:hidden">
          <Avatar name={s.startedBy?.name ?? s.startedBy?.email} size={16} />
          <span className="truncate">{s.startedBy?.name ?? s.startedBy?.email ?? 'Unknown'}</span>
          <span aria-hidden>·</span>
          <Time iso={s.startedAt} now={now} />
          <span aria-hidden>·</span>
          <span className="tabular mono">{live && started ? formatClock(now - started) : formatDuration(dur)}</span>
        </span>
        <span className="hidden min-w-0 items-center gap-2 md:flex">
          <Avatar name={s.startedBy?.name ?? s.startedBy?.email} size={20} />
          <span className="truncate text-[12.5px] text-fg-2">{s.startedBy?.name ?? s.startedBy?.email ?? 'Unknown'}</span>
        </span>
        <span className="hidden text-[12.5px] text-muted md:block">
          <Time iso={s.startedAt} now={now} />
        </span>
        <span className="tabular mono hidden text-[12px] text-fg-2 md:block">
          {live && started ? formatClock(now - started) : formatDuration(dur)}
        </span>
        <span className="hidden min-w-0 md:block">{s.gateProvider ? <Tag>{gateProviderLabel(s.gateProvider)}</Tag> : <span className="text-subtle">—</span>}</span>
        <span className="hidden md:block">
          <SessionBadge status={s.status} />
        </span>
        <ChevronRight className="ml-auto hidden h-4 w-4 text-subtle transition-transform group-hover:translate-x-0.5 group-hover:text-fg md:block" aria-hidden />
      </Link>
    </li>
  )
}
