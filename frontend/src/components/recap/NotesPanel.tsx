/**
 * The notes RGS wrote for a finished session, as they appear in the edition:
 * summary, decisions, action items with owners, key points, quotes.
 */
import { useQuery } from '@tanstack/react-query'
import type { ReactNode } from 'react'

import { liveApi, type LiveSessionInfo, type RecapNotes } from '@/api/live'
import { Avatar, CopyButton, Panel, Skeleton } from '@/components/kit'
import { isMockMode } from '@/lib/mockMode'

import { MOCK_NOTES } from './mockNotes'

export function NotesPanel({ session }: { session: LiveSessionInfo }) {
  const ready = !!session.recap?.hasNotes
  const { data, isLoading } = useQuery({
    queryKey: ['live', 'notes', session.id, session.recap?.updatedAt ?? null, isMockMode()],
    enabled: ready,
    queryFn: async () => (isMockMode() ? { notes: MOCK_NOTES, markdown: '# Notes', recap: null } : liveApi.notes(session.id)),
    staleTime: 60_000,
  })
  if (!ready) return null
  const notes = data?.notes
  return (
    <Panel title="Notes" aside={data?.markdown ? <CopyButton text={data.markdown} label="Copy Markdown" variant="ghost" /> : null}>
      {isLoading || !notes ? (
        <div className="flex flex-col gap-2.5 px-4 py-4">
          <Skeleton className="h-4 w-11/12" />
          <Skeleton className="h-4 w-4/5" />
          <Skeleton className="h-4 w-2/3" />
        </div>
      ) : (
        <NotesBody notes={notes} />
      )}
    </Panel>
  )
}

function Section({ title, count, children }: { title: string; count?: number; children: ReactNode }) {
  return (
    <section className="border-t border-border px-4 py-3 first:border-t-0">
      <h3 className="mb-2 flex items-center gap-2 text-[11px] font-semibold tracking-[0.1em] text-muted uppercase">
        {title}
        {count != null && <span className="tabular font-medium tracking-normal text-subtle">{count}</span>}
      </h3>
      {children}
    </section>
  )
}

function Bullets({ items }: { items: string[] }) {
  return (
    <ul className="flex flex-col gap-1.5 text-[13px] leading-5 text-fg-2">
      {items.map((t, i) => (
        <li key={i} className="flex gap-2">
          <span aria-hidden className="mt-[8px] h-1 w-1 shrink-0 rounded-full bg-brand" />
          <span className="min-w-0">{t}</span>
        </li>
      ))}
    </ul>
  )
}

export function NotesBody({ notes }: { notes: RecapNotes }) {
  return (
    <div>
      {notes.summary && (
        <Section title="Summary">
          <p className="text-[13px] leading-[1.55] text-pretty text-fg-2">{notes.summary}</p>
        </Section>
      )}
      {notes.decisions.length > 0 && (
        <Section title="Decisions" count={notes.decisions.length}>
          <Bullets items={notes.decisions} />
        </Section>
      )}
      {notes.actionItems.length > 0 && (
        <Section title="Action items" count={notes.actionItems.length}>
          <ul className="flex flex-col gap-2">
            {notes.actionItems.map((a, i) => (
              <li key={i} className="flex items-start gap-2.5 text-[13px] leading-5">
                <Avatar name={a.owner === 'Unassigned' ? '?' : a.owner} size={20} />
                <span className="min-w-0">
                  <span className="font-medium text-fg">{a.owner}</span>
                  <span className="text-fg-2"> · {a.task}</span>
                  {a.due && <span className="text-muted"> · {a.due}</span>}
                </span>
              </li>
            ))}
          </ul>
        </Section>
      )}
      {notes.keyPoints.length > 0 && (
        <Section title="Key points" count={notes.keyPoints.length}>
          <Bullets items={notes.keyPoints} />
        </Section>
      )}
      {notes.openQuestions.length > 0 && (
        <Section title="Open questions">
          <Bullets items={notes.openQuestions} />
        </Section>
      )}
      {notes.quotes.length > 0 && (
        <Section title="Quotes">
          <div className="flex flex-col gap-2.5">
            {notes.quotes.map((q, i) => (
              <figure key={i} className="border-l-2 border-brand/60 pl-3">
                <blockquote className="text-[13px] leading-5 text-fg italic">“{q.quote}”</blockquote>
                {q.speaker && <figcaption className="mt-0.5 text-[12px] text-muted">{q.speaker}</figcaption>}
              </figure>
            ))}
          </div>
        </Section>
      )}
    </div>
  )
}
