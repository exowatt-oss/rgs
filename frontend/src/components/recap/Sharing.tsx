/**
 * Sharing a recap with the people in the call (opt-in, off by default), and
 * who those people are: the speakers RGS matched to the people directory
 * (local directory), with their Slack name and email.
 */
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Check, LoaderCircle, RotateCw, Send, TriangleAlert, UsersRound } from 'lucide-react'
import { useMemo } from 'react'

import { liveApi, type LiveSessionInfo, type RecapShare, type SessionParticipant } from '@/api/live'
import { Button, ErrorNote, Panel } from '@/components/kit'
import { PersonIdentity } from '@/components/live/PersonIdentity'
import { SpeakerAvatar } from '@/components/live/SpeakerName'
import { castOf } from '@/lib/live/conversation'
import { speakerName, transcriptRows, type LiveState } from '@/lib/live/liveReducer'
import { isMockMode } from '@/lib/mockMode'
import { cn } from '@/lib/utils'
import { mockStore } from '@/mock/store'

import { deliveryPhrase, useSessionRecap } from './recap'
import { namesPhrase, personOf, shareBlockedReason } from './share'
import { ShareToggle } from './ShareToggle'

function useShareActions(session: LiveSessionInfo) {
  const qc = useQueryClient()
  const mock = isMockMode()
  const onSuccess = (s: LiveSessionInfo) => {
    qc.setQueryData(['live', 'recap', session.id, mock], s)
    void qc.invalidateQueries({ queryKey: ['live', 'recap', session.id] })
  }
  const setFlag = useMutation({
    mutationFn: async (on: boolean) => (mock ? (mockStore.setShare(session.id, on) ?? session) : liveApi.setShareWithParticipants(session.id, on)),
    onSuccess,
  })
  const shareNow = useMutation({
    mutationFn: async (opts: { reshare?: boolean } = {}) =>
      mock ? (mockStore.shareNow(session.id) ?? session) : liveApi.rerunRecap(session.id, { shareWithParticipants: true, reshare: opts.reshare }),
    onSuccess,
  })
  return { setFlag, shareNow }
}

/** One line per outcome: who got it, who didn't (and why). */
function ShareOutcome({ shared }: { shared: RecapShare[] }) {
  const sent = shared.filter((r) => r.status === 'sent' || r.status === 'partial')
  const failed = shared.filter((r) => r.status === 'failed' || r.status === 'skipped')
  const partial = shared.filter((r) => r.status === 'partial')
  return (
    <div className="flex min-w-0 flex-col gap-1 text-[12.5px]">
      {sent.length > 0 && (
        <p className="flex min-w-0 items-start gap-1.5 text-fg-2">
          <Check className="mt-[3px] size-3.5 shrink-0 text-ok" aria-hidden />
          <span className="min-w-0">
            Shared with <span className="font-medium text-fg">{namesPhrase(sent.map((r) => r.name), 6)}</span>
            {partial.length > 0 && (
              <span className="text-muted" title={partial.map((r) => `${r.name}: ${r.error ?? 'one channel failed'}`).join('\n')}>
                {' '}
                (one channel failed for {namesPhrase(partial.map((r) => r.name))})
              </span>
            )}
          </span>
        </p>
      )}
      {failed.length > 0 && (
        <p className="flex min-w-0 items-start gap-1.5 text-warn-ink">
          <TriangleAlert className="mt-[3px] size-3.5 shrink-0" aria-hidden />
          <span className="min-w-0">
            Not sent to {namesPhrase(failed.map((r) => r.name), 6)}
            {failed[0]?.error && <span className="text-muted"> · {failed[0].error}</span>}
          </span>
        </p>
      )}
    </div>
  )
}

/** Who a share goes to, with how RGS knows them. */
function Recipients({ people }: { people: SessionParticipant[] }) {
  return (
    <ul className="flex min-w-0 flex-col gap-0.5" aria-label="People it goes to">
      {people.map((p) => (
        <li key={p.email} className="flex min-w-0">
          <PersonIdentity person={p} name={p.name} />
        </li>
      ))}
    </ul>
  )
}

/**
 * The recap card's sharing row. The owner opts in (or out); after the recap
 * the opt-in reveals who it goes to and a "Share now" button. A viewer the
 * recap was shared with sees who shared it.
 */
export function ShareRow({ session }: { session: LiveSessionInfo }) {
  const recap = session.recap
  const { setFlag, shareNow } = useShareActions(session)
  if (!recap) return null
  if (!session.isOwner) {
    if (!session.sharedWithYou) return null
    return (
      <p className="flex items-center gap-2 border-t border-border px-4 py-2.5 text-[12.5px] text-muted">
        <UsersRound className="size-3.5 shrink-0 text-subtle" aria-hidden />
        <span>
          <span className="font-medium text-fg-2">{session.startedBy?.name ?? session.startedBy?.email ?? 'The organiser'}</span> shared this with you.
        </span>
      </p>
    )
  }
  const sharing = recap.status === 'sharing' || shareNow.isPending
  const checked = setFlag.isPending ? !!setFlag.variables : recap.shareWithParticipants
  const blocked = shareBlockedReason(recap.deliverVia, null)
  const people = session.participants
  const done = new Set(recap.shared.filter((r) => r.status === 'sent').map((r) => r.email))
  const pending = people.filter((p) => !done.has(p.email))
  const retryable = recap.shared.some((r) => r.status !== 'sent')
  const error = setFlag.error ?? shareNow.error
  return (
    <div className="flex flex-col gap-2.5 border-t border-border px-4 py-3">
      <div className="flex flex-wrap items-start justify-between gap-x-4 gap-y-2">
        <ShareToggle
          checked={checked}
          onChange={(v) => setFlag.mutate(v)}
          busy={setFlag.isPending || sharing}
          disabledReason={blocked}
          hint={
            checked && people.length === 0
              ? 'Nobody RGS recognised from the people directory spoke in this call, so there’s no one to share with.'
              : undefined
          }
        />
        {checked && !blocked && (sharing ? (
          <span className="inline-flex h-7 items-center gap-1.5 text-[12px] text-muted" role="status">
            <LoaderCircle className="size-3.5 spin" aria-hidden />
            Sharing {deliveryPhrase(recap.deliverVia)}…
          </span>
        ) : pending.length > 0 ? (
          <Button variant="primary" onClick={() => shareNow.mutate({})} title={`Send ${namesPhrase(pending.map((p) => p.name))} the link ${deliveryPhrase(recap.deliverVia)}`}>
            <Send className="size-3.5" aria-hidden />
            {recap.shared.length ? 'Share with the rest' : 'Share now'}
          </Button>
        ) : retryable ? (
          <Button onClick={() => shareNow.mutate({})}>
            <RotateCw className="size-3.5" aria-hidden />
            Try again
          </Button>
        ) : null)}
      </div>
      {checked && !blocked && pending.length > 0 && !sharing && (
        <div className="flex min-w-0 flex-col gap-1 pl-[22px]">
          <span className="text-[11px] font-semibold tracking-[0.08em] text-subtle uppercase">Goes to</span>
          <Recipients people={pending} />
        </div>
      )}
      {recap.shared.length > 0 && (
        <div className="pl-[22px]">
          <ShareOutcome shared={recap.shared} />
        </div>
      )}
      {error && <ErrorNote error={error} />}
    </div>
  )
}

/**
 * The people in this conversation RGS matched to the directory, with their
 * Slack name and email. While the call is live, the owner opts in to sharing
 * the recap with them here (after the call it lives on the recap card).
 */
export function PeoplePanel({ sessionId, state }: { sessionId: string; state: LiveState }) {
  const { data: session } = useSessionRecap(sessionId)
  const rows = useMemo(() => transcriptRows(state), [state])
  const cast = useMemo(() => castOf(state, rows), [state, rows])
  const participants = session?.participants ?? []
  const known = cast.filter((s) => s.person)
  const others = cast.length - known.length
  const live = session?.status === 'live'
  if (!session || (!known.length && !(live && session.isOwner))) return null
  return (
    <Panel title="People" aside={cast.length ? <span className="tabular">{known.length} of {cast.length} recognised</span> : null}>
      {known.length > 0 ? (
        <ul className="flex flex-col gap-2.5 px-4 py-3">
          {known.map((s) => (
            <li key={s.id} className="flex min-w-0 items-center gap-2.5">
              <SpeakerAvatar speaker={s} size={24} />
              <div className="flex min-w-0 flex-col">
                <span className="truncate text-[13px] font-medium text-fg">{speakerName(s)}</span>
                <PersonIdentity person={personOf(s, participants)} />
              </div>
            </li>
          ))}
        </ul>
      ) : (
        <p className="px-4 py-3 text-[12.5px] text-muted">Nobody matched to the people directory yet. Introductions help: “Hi, I’m Maya.”</p>
      )}
      {others > 0 && known.length > 0 && (
        <p className="-mt-1 px-4 pb-3 text-[12px] text-subtle">
          {others} more {others === 1 ? 'voice' : 'voices'} not matched to the people directory.
        </p>
      )}
      {live && session.isOwner && <LiveShare session={session} />}
    </Panel>
  )
}

function LiveShare({ session }: { session: LiveSessionInfo }) {
  const { setFlag } = useShareActions(session)
  const recap = session.recap
  const checked = setFlag.isPending ? !!setFlag.variables : !!recap?.shareWithParticipants
  const blocked = !recap?.enabled ? 'The recap is off for this call.' : shareBlockedReason(recap.deliverVia, null)
  return (
    <div className={cn('flex flex-col gap-2 border-t border-border px-4 py-3')}>
      <ShareToggle checked={checked} onChange={(v) => setFlag.mutate(v)} busy={setFlag.isPending} disabledReason={blocked} />
      {setFlag.error && <ErrorNote error={setFlag.error} />}
    </div>
  )
}
