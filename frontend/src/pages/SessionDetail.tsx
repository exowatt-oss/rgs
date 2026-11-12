import { useQueryClient } from '@tanstack/react-query'
import { ExternalLink } from 'lucide-react'
import { useCallback, useState } from 'react'
import { Navigate, useParams } from 'react-router-dom'

import { ApiError } from '@/api/client'
import { gateProviderLabel, liveApi, liveErrorMessage, parseLiveEvent } from '@/api/live'
import { Avatar, Button, Meta, MetaRow, PageHeader, Panel, Skeleton, Tag } from '@/components/kit'
import { AnswersPanel } from '@/components/live/AnswersRail'
import { SpeakerRenameProvider } from '@/components/live/SpeakerName'
import { Transcript } from '@/components/live/Transcript'
import { useAnswerLinks } from '@/components/live/useAnswerLinks'
import { useSpeakerRenaming } from '@/components/live/useSpeakerRenaming'
import { sessionDurationS } from '@/components/sessions/SessionTable'
import { SessionBadge } from '@/components/status'
import { TranscriptPanel } from '@/pages/Live'
import { formatAbsolute } from '@/lib/format'
import { initialLiveState, liveReducer } from '@/lib/live/liveReducer'
import { formatDuration } from '@/lib/live/presentation'
import { isMockMode } from '@/lib/mockMode'
import { useSessionArchive, type SessionArchive } from '@/lib/sessions'
import { mockStore } from '@/mock/store'
import { NotesPanel } from '@/components/recap/NotesPanel'
import { RecapCard } from '@/components/recap/RecapCard'
import { PeoplePanel } from '@/components/recap/Sharing'
import { useSessionRecap } from '@/components/recap/recap'

/** A finished call: its transcript and every answer, linked both ways. */
export function SessionDetailPage() {
  const { sessionId } = useParams()
  const { data, isLoading, isError, refetch } = useSessionArchive(sessionId)
  const state = data?.state ?? initialLiveState
  const links = useAnswerLinks(state)
  // Follow the recap (notes → edition) so the notes appear as soon as they're written.
  const finalized = data?.session.status === 'finalized' ? data.session : undefined
  const recapSession = useSessionRecap(finalized?.id).data ?? finalized
  const rename = useArchiveRename(sessionId)
  const renaming = useSpeakerRenaming(state, data?.session, data?.session.isOwner && rename.available ? rename.rename : null)

  if (data && (data.session.status === 'live' || data.session.status === 'stopping')) return <Navigate to={`/live/${data.session.id}`} replace />

  const s = data?.session

  return (
    <div className="flex min-h-full flex-col lg:h-full lg:min-h-0">
      <PageHeader
        title={null}
        crumbs={[{ label: 'Sessions', to: '/sessions' }, { label: isLoading ? '…' : (s?.title ?? s?.meetCode ?? 'Untitled') }]}
        actions={
          s ? (
            <>
              {s.meetCode && (
                <a
                  href={s.meetUrl ?? `https://meet.google.com/${s.meetCode}`}
                  target="_blank"
                  rel="noreferrer"
                  className="mono inline-flex items-center gap-1 text-[12px] text-muted hover:text-fg"
                >
                  {s.meetCode}
                  <ExternalLink className="h-3 w-3" aria-hidden />
                </a>
              )}
              {s.sharedWithYou && <Tag title={`${s.startedBy?.name ?? 'The organiser'} shared this with you (read only)`}>Shared with you</Tag>}
              <SessionBadge status={s.status} />
            </>
          ) : null
        }
      />
      <div className="flex min-h-0 flex-1 flex-col gap-4 px-3 py-4 sm:px-5 lg:px-6 lg:py-5">
        {isError ? (
          <Panel>
            <div className="flex items-center justify-between gap-4 px-4 py-4 text-[13px]">
              <span className="text-fail-ink">Couldn’t load this session. It may have been discarded, or you may not have access.</span>
              <Button onClick={() => void refetch()}>Retry</Button>
            </div>
          </Panel>
        ) : (
          <>
          {s && s.status === 'finalized' && <RecapCard sessionId={s.id} />}
          <div className="grid min-h-0 flex-1 grid-cols-1 gap-4 lg:grid-cols-[minmax(0,1fr)_360px] xl:grid-cols-[minmax(0,1fr)_400px]">
            <SpeakerRenameProvider value={renaming}>
            <TranscriptPanel state={state} title="Transcript">
              {rename.error && (
                <p role="alert" className="border-b border-border bg-warn-soft px-4 py-1.5 text-[12px] text-warn-ink">
                  {rename.error}
                </p>
              )}
              <Transcript
                state={state}
                live={false}
                answers={links.answers}
                moments={links.moments}
                focus={links.focusLine}
                onOpenAnswer={links.jumpToCard}
                onJumpToLine={links.jumpToLine}
                empty={
                  isLoading ? (
                    <div className="flex flex-col gap-3 pt-4" aria-busy>
                      {[0, 1, 2, 3, 4].map((i) => (
                        <Skeleton key={i} className="h-10" />
                      ))}
                    </div>
                  ) : (
                    <p className="py-20 text-center text-[13px] text-muted">Nothing was transcribed in this call.</p>
                  )
                }
              />
            </TranscriptPanel>
            </SpeakerRenameProvider>

            <aside aria-label="Details" className="scroll-quiet flex min-h-0 flex-col gap-4 lg:overflow-y-auto">
              <Panel title={s?.kind === 'adhoc' ? 'Conversation' : 'Call'}>
                <div className="px-4 py-3.5">
                  {s ? (
                    <Meta>
                      <MetaRow label="Kind">{s.kind === 'adhoc' ? 'In person' : 'Google Meet'}</MetaRow>
                      <MetaRow label="Started">{formatAbsolute(s.startedAt) || '—'}</MetaRow>
                      <MetaRow label="Length">
                        <span className="mono tabular">{formatDuration(sessionDurationS(s))}</span>
                      </MetaRow>
                      <MetaRow label="Started by">
                        <span className="inline-flex items-center gap-2">
                          <Avatar name={s.startedBy?.name ?? s.startedBy?.email} size={20} />
                          {s.startedBy?.name ?? s.startedBy?.email ?? 'Unknown'}
                        </span>
                      </MetaRow>
                      <MetaRow label="Decision gate">{s.gateProvider ? <Tag>{gateProviderLabel(s.gateProvider)}</Tag> : '—'}</MetaRow>
                      <MetaRow label="Answers">
                        <span className="tabular">{state.cards.filter((c) => c.finding).length}</span>
                      </MetaRow>
                    </Meta>
                  ) : (
                    <Skeleton className="h-24" />
                  )}
                </div>
              </Panel>
              {s && <PeoplePanel sessionId={s.id} state={state} />}
              {recapSession && <NotesPanel session={recapSession} />}
              <AnswersPanel state={state} moments={links.moments} focusCard={links.focusCard} onJumpToLine={links.jumpToLine} />
            </aside>
          </div>
          </>
        )}
      </div>
    </div>
  )
}

/**
 * Renaming a speaker after the call: PATCH the speaker, then fold the answer
 * into the cached archive. A server without the endpoint (404/405) turns the
 * feature off for this view instead of failing every click.
 */
function useArchiveRename(sessionId: string | undefined) {
  const qc = useQueryClient()
  const [available, setAvailable] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const rename = useCallback(
    (speakerId: string, displayName: string, email: string | null) => {
      if (!sessionId) return
      const key = ['live', 'archive', sessionId, isMockMode()]
      const before = qc.getQueryData<SessionArchive>(key)
      const apply = (fn: (st: SessionArchive['state']) => SessionArchive['state']) =>
        qc.setQueryData<SessionArchive>(key, (old) => (old ? { ...old, state: fn(old.state) } : old))
      apply((st) => liveReducer(st, { type: 'speaker.rename', speakerId, displayName }))
      setError(null)
      const run = async () => {
        if (isMockMode()) {
          const wire = mockStore.renameSpeaker(sessionId, speakerId, displayName)
          const ev = wire ? parseLiveEvent({ type: 'speaker.update', seq: null, data: wire }) : null
          return ev?.type === 'speaker.update' ? ev.speaker : null
        }
        return liveApi.renameSpeaker(sessionId, speakerId, displayName, email)
      }
      run()
        .then((speaker) => {
          if (speaker) apply((st) => liveReducer(st, { type: 'speaker.upsert', speaker }))
        })
        .catch((err: unknown) => {
          if (before) qc.setQueryData(key, before)
          const status = err instanceof ApiError ? err.status : null
          if (status === 404 || status === 405 || status === 501) {
            setAvailable(false)
            setError('Renaming speakers isn’t available for past sessions yet.')
          } else {
            setError(liveErrorMessage(err, 'Could not rename the speaker'))
          }
        })
    },
    [qc, sessionId],
  )
  return { rename, available, error }
}
