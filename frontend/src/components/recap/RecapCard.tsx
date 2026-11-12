/**
 * "Your edition": what happens after stop. RGS writes the notes, prints them as
 * a external publisher edition on its own canvas and sends the link by Slack / email.
 * Follows the session (polls) until the edition is out, then becomes the
 * edition's link card.
 */
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Check, ExternalLink, Mail, MessageSquare, Newspaper, PenLine, RotateCw, Send, TriangleAlert, UsersRound } from 'lucide-react'
import type { ReactNode } from 'react'

import { liveApi, recapInFlight, type DeliveryChannel, type LiveRecap, type LiveSessionInfo, type RecapStatus } from '@/api/live'
import { Button, CopyButton, ErrorNote, Skeleton, buttonClass } from '@/components/kit'
import { isMockMode } from '@/lib/mockMode'
import { cn } from '@/lib/utils'

import { deliveryPhrase, savedDeliveryChannels, useSessionRecap } from './recap'
import { ShareRow } from './Sharing'

export function RecapCard({ sessionId, className }: { sessionId: string; className?: string }) {
  const { data: s, isError } = useSessionRecap(sessionId)
  if (!s && isError) return null
  if (!s) {
    return (
      <Shell className={className}>
        <div className="flex gap-4 p-4">
          <Skeleton className="h-[84px] w-16 shrink-0" />
          <div className="flex flex-1 flex-col gap-2 pt-1">
            <Skeleton className="h-3 w-24" />
            <Skeleton className="h-5 w-3/4" />
            <Skeleton className="h-3 w-1/2" />
          </div>
        </div>
      </Shell>
    )
  }
  const recap = s.recap
  if (s.status === 'cancelled' || s.status === 'failed' || s.status === 'live') return null
  if (s.status === 'stopping' || (recap?.enabled && recap.status === null && s.status === 'finalized' && recentlyStopped(s))) {
    return <Printing className={className} session={s} recap={recap} stage="pending" />
  }
  if (!recap || recap.status === null) return s.isOwner && s.status === 'finalized' ? <PrintCta className={className} session={s} /> : null
  // "Share now" after the recap: the edition stays on the card while the messages go out.
  const sharingAfter = recap.status === 'sharing' && !!recap.edition
  if (recapInFlight(recap) && !sharingAfter) return <Printing className={className} session={s} recap={recap} stage={recap.status} />
  if (recap.status === 'skipped') {
    return (
      <Shell className={className} quiet>
        <p className="flex items-center gap-2.5 px-4 py-3 text-[13px] text-muted">
          <Newspaper className="h-4 w-4 shrink-0 text-subtle" aria-hidden />
          {recap.error ?? 'Nothing to print for this conversation.'}
        </p>
      </Shell>
    )
  }
  if ((recap.status === 'failed' || sharingAfter) && !recap.edition?.shareUrl) return <Failed className={className} session={s} recap={recap} />
  return <Edition className={className} session={s} recap={recap} />
}

const recentlyStopped = (s: LiveSessionInfo) => {
  const t = s.stoppedAt ? Date.parse(s.stoppedAt) : NaN
  return Number.isFinite(t) && Date.now() - t < 120_000
}

function Shell({ children, className, quiet }: { children: ReactNode; className?: string; quiet?: boolean }) {
  return (
    <section
      aria-label="Your edition"
      className={cn('pop-in relative min-w-0 overflow-hidden rounded-[10px] border border-border bg-surface shadow-card', className)}
    >
      {!quiet && <div aria-hidden className="ex-bg-gradient h-[3px]" />}
      {children}
    </section>
  )
}

function Eyebrow({ children }: { children: ReactNode }) {
  return <div className="truncate text-[11px] font-semibold tracking-[0.12em] text-muted uppercase">{children}</div>
}

/** A tiny front page; its columns shimmer while the edition is being laid out. */
function PaperThumb({ printing, className }: { printing?: boolean; className?: string }) {
  const line = cn('h-[3px] rounded-full bg-track-strong', printing && 'agent-shimmer')
  return (
    <div
      aria-hidden
      className={cn(
        'flex h-[84px] w-16 shrink-0 flex-col gap-[5px] rounded-[4px] border border-border bg-surface-2 p-[7px] shadow-[0_1px_2px_rgb(0_0_0/0.08)]',
        printing && 'animate-[rgs-press_2.4s_ease-in-out_infinite]',
        className,
      )}
    >
      <div className="ex-bg-gradient h-[3px] rounded-full" />
      <div className="h-[6px] w-4/5 rounded-[1px] bg-fg/80" />
      <div className="mt-[1px] h-[3px] w-full rounded-full bg-fg/45" />
      <div className="mt-[2px] grid flex-1 grid-cols-2 gap-x-[5px] gap-y-[4px] content-start">
        {[0, 1, 2, 3, 4, 5].map((i) => (
          <div key={i} className={cn(line, i % 3 === 2 && 'w-2/3')} style={printing ? { animationDelay: `${i * 0.18}s` } : undefined} />
        ))}
      </div>
    </div>
  )
}

const STEPS: { key: string; label: string; icon: typeof PenLine }[] = [
  { key: 'notes', label: 'Writing the notes', icon: PenLine },
  { key: 'print', label: 'Printing the edition', icon: Newspaper },
  { key: 'send', label: 'Sending the link', icon: Send },
  { key: 'share', label: 'Sharing with the participants', icon: UsersRound },
]

const stepIndex = (stage: RecapStatus | 'pending'): number =>
  stage === 'printing' ? 1 : stage === 'delivering' ? 2 : stage === 'sharing' ? 3 : stage === 'done' ? 4 : 0

function Steps({ stage, channels, share }: { stage: RecapStatus | 'pending'; channels: readonly DeliveryChannel[]; share: boolean }) {
  const at = stepIndex(stage)
  const steps = channels.length === 0 ? STEPS.slice(0, 2) : share ? STEPS : STEPS.slice(0, 3)
  return (
    <ol className="flex flex-wrap items-center gap-x-2 gap-y-1.5" aria-label="Progress">
      {steps.map((st, i) => {
        const Icon = st.icon
        const done = i < at
        const active = i === at
        return (
          <li key={st.key} className="flex items-center gap-2">
            {i > 0 && <span aria-hidden className={cn('h-px w-4 sm:w-6', done || active ? 'bg-brand/50' : 'bg-border')} />}
            <span
              className={cn(
                'inline-flex h-6 items-center gap-1.5 rounded-full pr-2.5 pl-1.5 text-[12px] font-medium',
                done ? 'bg-ok-soft text-ok-ink' : active ? 'bg-brand-soft text-brand-text' : 'text-subtle',
              )}
              aria-current={active ? 'step' : undefined}
            >
              {done ? (
                <Check className="h-3.5 w-3.5" aria-hidden />
              ) : (
                <Icon className={cn('h-3.5 w-3.5', active && 'blink')} aria-hidden />
              )}
              {st.label}
            </span>
          </li>
        )
      })}
    </ol>
  )
}

function Printing({
  session,
  recap,
  stage,
  className,
}: {
  session: LiveSessionInfo
  recap: LiveRecap | null
  stage: RecapStatus | 'pending'
  className?: string
}) {
  const channels = recap?.deliverVia ?? []
  return (
    <Shell className={className}>
      <div className="flex gap-4 p-4" role="status" aria-live="polite">
        <PaperThumb printing />
        <div className="flex min-w-0 flex-1 flex-col gap-2.5">
          <div>
            <Eyebrow>Your edition</Eyebrow>
            <h3 className="mt-1 text-[17px] leading-6 font-semibold tracking-[-0.01em] text-fg">
              Your edition is printing<span className="caret">…</span>
            </h3>
            <p className="mt-0.5 text-[13px] text-muted">
              {recap?.headline ? (
                <>
                  Lead story: <span className="font-medium text-fg-2">{recap.headline}</span>.{' '}
                </>
              ) : (
                `RGS is writing up ${session.kind === 'adhoc' ? 'the conversation' : 'the call'}. `
              )}
              {channels.length > 0 ? `The link comes to you ${deliveryPhrase(channels)} in about a minute.` : 'The link lands here in about a minute.'}
            </p>
          </div>
          <Steps stage={stage} channels={channels} share={!!recap?.shareWithParticipants && session.participants.length > 0} />
        </div>
      </div>
    </Shell>
  )
}

function DeliveryChips({ recap }: { recap: LiveRecap }) {
  const chips = recap.deliverVia.map((ch) => {
    const d = recap.delivery[ch]
    const Icon = ch === 'slack' ? MessageSquare : Mail
    const sent = d?.status === 'sent'
    const failed = d && d.status !== 'sent'
    const label = ch === 'slack' ? (sent ? 'Sent on Slack' : 'Slack') : sent ? 'Emailed to you' : 'Email'
    return (
      <span
        key={ch}
        title={failed ? (d?.error ?? 'Not sent') : undefined}
        className={cn(
          'inline-flex h-6 items-center gap-1.5 rounded-full px-2.5 text-[12px] font-medium',
          sent ? 'bg-ok-soft text-ok-ink' : failed ? 'bg-warn-soft text-warn-ink' : 'bg-selected text-muted',
        )}
      >
        <Icon className="h-3.5 w-3.5" aria-hidden />
        {failed ? `${label} not sent` : label}
        {sent && <Check className="h-3 w-3" aria-hidden />}
      </span>
    )
  })
  return chips.length ? <div className="flex flex-wrap items-center gap-1.5">{chips}</div> : null
}

function useRerun(session: LiveSessionInfo) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (opts: { deliverVia?: DeliveryChannel[]; reprint?: boolean }) =>
      isMockMode() ? Promise.resolve(session) : liveApi.rerunRecap(session.id, opts),
    onSuccess: (s) => {
      qc.setQueryData(['live', 'recap', session.id, isMockMode()], s)
      void qc.invalidateQueries({ queryKey: ['live', 'recap', session.id] })
    },
  })
}

function Edition({ session, recap, className }: { session: LiveSessionInfo; recap: LiveRecap; className?: string }) {
  const ed = recap.edition
  const url = ed?.shareUrl ?? null
  const rerun = useRerun(session)
  const headline = ed?.headline ?? recap.headline ?? session.title ?? 'Your edition'
  return (
    <Shell className={className}>
      <div className="flex gap-4 p-4">
        {url ? (
          <a href={url} target="_blank" rel="noreferrer" className="shrink-0 transition-transform hover:-translate-y-0.5" aria-label="Open the edition">
            <PaperThumb />
          </a>
        ) : (
          <PaperThumb />
        )}
        <div className="flex min-w-0 flex-1 flex-col gap-3">
          <div className="min-w-0">
            <Eyebrow>
              {ed?.paperName ?? 'Your edition'}
              {ed?.number ? ` · Edition ${ed.number}` : ''}
            </Eyebrow>
            <h3 className="mt-1 text-[19px] leading-[1.25] font-semibold tracking-[-0.015em] text-pretty text-fg">
              {url ? (
                <a href={url} target="_blank" rel="noreferrer" className="hover:underline hover:decoration-brand/60 hover:underline-offset-4">
                  {headline}
                </a>
              ) : (
                headline
              )}
            </h3>
            {(ed?.dek ?? recap.summary) && <p className="mt-1 line-clamp-3 text-[13px] leading-5 text-muted">{ed?.dek ?? recap.summary}</p>}
          </div>
          <div className="flex flex-wrap items-center gap-2">
            {url && (
              <a href={url} target="_blank" rel="noreferrer" className={buttonClass('primary', { size: 'md' })}>
                <Newspaper className="h-3.5 w-3.5" aria-hidden />
                Open the edition
                <ExternalLink className="h-3 w-3 opacity-80" aria-hidden />
              </a>
            )}
            {url && <CopyButton text={url} label="Copy link" size="md" />}
            <span className="ml-auto flex flex-wrap items-center gap-1.5">
              {session.isOwner && <DeliveryChips recap={recap} />}
              {session.isOwner && recap.deliverVia.length > 0 && (
                <Button
                  variant="ghost"
                  onClick={() => rerun.mutate({})}
                  disabled={rerun.isPending}
                  title={`Send the link again ${deliveryPhrase(recap.deliverVia)}`}
                >
                  <RotateCw className={cn('h-3.5 w-3.5', rerun.isPending && 'spin')} aria-hidden />
                  Send again
                </Button>
              )}
            </span>
          </div>
          {rerun.error && <ErrorNote error={rerun.error} />}
        </div>
      </div>
      <ShareRow session={session} />
    </Shell>
  )
}

function Failed({ session, recap, className }: { session: LiveSessionInfo; recap: LiveRecap; className?: string }) {
  const rerun = useRerun(session)
  return (
    <Shell className={className}>
      <div className="flex gap-4 p-4">
        <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg bg-warn-soft text-warn-ink">
          <TriangleAlert className="h-5 w-5" aria-hidden />
        </span>
        <div className="flex min-w-0 flex-1 flex-col gap-2.5">
          <div>
            <Eyebrow>{session.isOwner ? 'Your edition' : 'The edition'}</Eyebrow>
            <h3 className="mt-1 text-[15px] font-semibold text-fg">The edition didn’t print this time</h3>
            <p className="mt-0.5 text-[13px] text-muted">
              {recap.hasNotes ? 'The notes are written and saved with the transcript. ' : ''}
              {recap.error ? <span className="text-fg-2">{recap.error}</span> : null}
            </p>
            {recap.headline && <p className="mt-2 text-[14px] font-medium text-fg">{recap.headline}</p>}
          </div>
          <div className="flex flex-wrap items-center gap-2">
            {session.isOwner && (
              <Button variant="primary" size="md" onClick={() => rerun.mutate({})} disabled={rerun.isPending}>
                <RotateCw className={cn('h-3.5 w-3.5', rerun.isPending && 'spin')} aria-hidden />
                Print it again
              </Button>
            )}
            {session.isOwner && <DeliveryChips recap={recap} />}
          </div>
          {rerun.error && <ErrorNote error={rerun.error} />}
        </div>
      </div>
      <ShareRow session={session} />
    </Shell>
  )
}

/** Sessions without a recap (recap was off, or from before recaps): print one now. */
function PrintCta({ session, className }: { session: LiveSessionInfo; className?: string }) {
  const rerun = useRerun(session)
  const channels = savedDeliveryChannels()
  return (
    <Shell className={className} quiet>
      <div className="flex flex-wrap items-center gap-3 px-4 py-3">
        <Newspaper className="h-4 w-4 shrink-0 text-subtle" aria-hidden />
        <p className="min-w-0 flex-1 text-[13px] text-muted">Turn this {session.kind === 'adhoc' ? 'conversation' : 'call'} into a newspaper edition, sent to you {deliveryPhrase(channels)}.</p>
        <Button onClick={() => rerun.mutate({ deliverVia: channels })} disabled={rerun.isPending}>
          <Newspaper className="h-3.5 w-3.5" aria-hidden />
          Print an edition
        </Button>
        {rerun.error && <ErrorNote error={rerun.error} className="basis-full" />}
      </div>
    </Shell>
  )
}
