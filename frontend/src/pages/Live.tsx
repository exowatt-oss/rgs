import { ArrowLeft, CircleCheck, ExternalLink, Eye, FlaskConical, History, LoaderCircle, Plus, Radio, RotateCcw, Square, Trash2, Unplug, VolumeX } from 'lucide-react'
import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react'
import { Link, Navigate, useNavigate, useParams } from 'react-router-dom'

import { DEFAULT_GATE_ACTIONS, type LiveGateAction } from '@/api/live'
import { Button, ConfirmDialog, PageHeader, Panel, Segmented, buttonClass } from '@/components/kit'
import { RecapCard } from '@/components/recap/RecapCard'
import { PeoplePanel } from '@/components/recap/Sharing'
import { AnswersPanel } from '@/components/live/AnswersRail'
import { ConnectionLights, LiveBanners } from '@/components/live/Banners'
import { LevelMeter } from '@/components/live/LevelMeter'
import { OnDeviceBadge } from '@/components/live/OnDeviceBadge'
import { ListeningFor } from '@/components/live/ListeningFor'
import { describePresence } from '@/components/live/presence'
import { AgentPanel } from '@/components/live/AgentPanel'
import { SpeakerAvatar, SpeakerRenameProvider, type SpeakerRenaming } from '@/components/live/SpeakerName'
import { Transcript } from '@/components/live/Transcript'
import { useAnswerLinks } from '@/components/live/useAnswerLinks'
import { useSpeakerRenaming } from '@/components/live/useSpeakerRenaming'
import { StateBadge, type StateSpec } from '@/components/status'
import type { OnDeviceInfo } from '@/lib/asr/copperPhonon'
import { useNow } from '@/lib/hooks'
import { isTentativeName, speakingNow } from '@/lib/live/conversation'
import { speakerName, transcriptRows, visibleSpeakers, type LiveState } from '@/lib/live/liveReducer'
import { formatClock } from '@/lib/live/presentation'
import { useLiveRecord } from '@/lib/liveRecording'
import { cn } from '@/lib/utils'

type Snapshot = ReturnType<typeof useLiveRecord>

const PHASE: Record<string, StateSpec> = {
  starting: { tone: 'run', label: 'Starting', icon: LoaderCircle, spin: true },
  live: { tone: 'live', label: 'Live', icon: Radio, pulse: true },
  stopping: { tone: 'run', label: 'Saving', icon: LoaderCircle, spin: true },
  viewing: { tone: 'ok', label: 'Following', icon: Eye },
  ended: { tone: 'idle', label: 'Ended', icon: CircleCheck },
}

export function LivePage() {
  const { sessionId } = useParams()
  const snapshot = useLiveRecord()
  const { controller, phase, session, live: state } = snapshot
  const navigate = useNavigate()
  const recording = phase === 'live' || phase === 'starting' || phase === 'stopping'
  const links = useAnswerLinks(state)

  // A shared link (or a reload) follows that session.
  useEffect(() => {
    if (sessionId && session?.id !== sessionId && !recording) void controller.attach(sessionId)
  }, [sessionId, session?.id, recording, controller])

  // Keep the URL on the session being shown.
  useEffect(() => {
    if (!sessionId && session?.id && (phase === 'live' || phase === 'viewing')) navigate(`/live/${session.id}`, { replace: true })
  }, [sessionId, session?.id, phase, navigate])

  const canRename = !!session?.isOwner && !snapshot.discarded
  const rename = useCallback<NonNullable<SpeakerRenaming['rename']>>(
    (id, name, email) => void controller.renameSpeaker(id, name, email),
    [controller],
  )
  const renaming = useSpeakerRenaming(state, session, canRename ? rename : null)

  if (!sessionId && phase === 'idle') return <Navigate to="/" replace />

  const ended = phase === 'ended'
  const following = phase === 'viewing'
  const actions = snapshot.config?.gate.actions?.length ? snapshot.config.gate.actions : DEFAULT_GATE_ACTIONS
  const otherSession = sessionId && session && session.id !== sessionId && recording

  return (
    <div className="flex min-h-full flex-col lg:h-full lg:min-h-0">
      <CallHeader snapshot={snapshot} />
      <div className="flex min-h-0 flex-1 flex-col px-3 py-4 sm:px-5 lg:px-6 lg:py-5">
        {otherSession && (
          <div className="mb-4 rounded-[10px] border border-border bg-surface-2 px-4 py-2.5 text-[13px] text-muted">
            You’re recording another call, so this link can’t open here right now.{' '}
            <Link to={`/live/${session.id}`} className="font-medium text-fg underline underline-offset-2">
              Back to your call
            </Link>
          </div>
        )}
        <LiveBanners snapshot={snapshot} controller={controller} />
        {ended && !snapshot.discarded && session?.id && <RecapCard sessionId={session.id} className="mb-4" />}

        <div className="grid min-h-0 flex-1 grid-cols-1 gap-4 lg:grid-cols-[minmax(0,1fr)_360px] xl:grid-cols-[minmax(0,1fr)_400px]">
          <SpeakerRenameProvider value={renaming}>
            <TranscriptPanel state={state} live={phase === 'live' || following} title="Transcript" ondevice={snapshot.ondevice}>
              <Transcript
                state={state}
                live={phase === 'live' || following}
                answers={links.answers}
                moments={links.moments}
                focus={links.focusLine}
                onOpenAnswer={links.jumpToCard}
                onJumpToLine={links.jumpToLine}
                empty={<TranscriptEmpty phase={phase} />}
                footer={ended ? <EndedFooter sessionId={session?.id ?? null} discarded={snapshot.discarded} /> : null}
              />
            </TranscriptPanel>
          </SpeakerRenameProvider>

          <aside aria-label="Agent" className="scroll-quiet flex min-h-0 flex-col gap-4 lg:overflow-y-auto lg:pr-0.5">
            <AgentHead snapshot={snapshot} />
            <AnswersPanel
              state={state}
              moments={links.moments}
              focusCard={links.focusCard}
              onJumpToLine={links.jumpToLine}
              emptyHint={<TrySaying actions={actions} />}
            />
            {session?.id && !snapshot.discarded && !otherSession && <PeoplePanel sessionId={session.id} state={state} />}
            <ListeningPanel actions={actions} state={state} />
            {controller.mock && (recording || following) && <DemoPanel snapshot={snapshot} />}
          </aside>
        </div>
      </div>
    </div>
  )
}

/** Transcript card: fills the column on desktop, scrolls inside. The header
 *  says who is talking right now while a call is live. */
export function TranscriptPanel({
  state,
  title,
  live = false,
  ondevice,
  children,
}: {
  state: LiveState
  title: ReactNode
  live?: boolean
  /** This tab's Copper on-device words, when recording here. */
  ondevice?: OnDeviceInfo
  children: ReactNode
}) {
  const speakers = visibleSpeakers(state)
  const speakingId = useMemo(() => (live ? speakingNow(transcriptRows(state)) : null), [live, state])
  const speaking = speakingId ? state.speakers[speakingId] : undefined
  return (
    <section
      aria-label="Transcript"
      className="flex h-[78dvh] min-h-0 min-w-0 flex-col overflow-hidden rounded-[10px] border border-border bg-surface shadow-card lg:h-auto"
    >
      <header className="flex min-h-11 shrink-0 flex-wrap items-center justify-between gap-x-4 gap-y-1 border-b border-border px-4 py-2">
        <h2 className="flex items-center gap-2 text-[13px] font-semibold text-fg">
          {title}
          {live && !speaking && <span className="text-[12px] font-normal text-muted">· following live</span>}
          <OnDeviceBadge info={ondevice} phrases={state.ondevice?.length ?? 0} />
        </h2>
        <div className="flex items-center gap-3 text-[12px] text-muted">
          {speaking && (
            <span className="inline-flex items-center gap-2 text-fg-2" aria-live="off">
              <SpeakerAvatar speaker={speaking} size={18} speaking />
              <span className="max-w-[12rem] truncate font-medium text-fg">
                {speakerName(speaking)}
                {isTentativeName(speaking) && <span className="font-normal text-muted">?</span>}
              </span>
              <span className="text-muted">talking</span>
            </span>
          )}
          <span className="tabular hidden sm:inline">
            {speakers.length} speaker{speakers.length === 1 ? '' : 's'} · {state.finals.length} line{state.finals.length === 1 ? '' : 's'}
          </span>
        </div>
      </header>
      {children}
    </section>
  )
}

function AgentHead({ snapshot }: { snapshot: Snapshot }) {
  const { phase, session, live: state, controller } = snapshot
  const now = useNow(1000)
  const ended = phase === 'ended'
  const offline =
    (phase === 'live' && (snapshot.audio.state === 'reconnecting' || snapshot.events.state === 'reconnecting')) ||
    (phase === 'viewing' && snapshot.events.state === 'reconnecting')
  const presence = describePresence({ ended, offline, cards: state.cards, now })
  return (
    <AgentPanel
      presence={presence}
      gate={state.gate}
      gateConfig={snapshot.config?.gate ?? null}
      live={phase === 'live' || phase === 'viewing'}
      canSwitch={!!session?.isOwner && phase === 'live'}
      switching={snapshot.gateSwitching}
      onSwitchProvider={(p) => void controller.switchGateProvider(p)}
      now={now}
    />
  )
}

function ListeningPanel({ actions, state }: { actions: readonly LiveGateAction[]; state: LiveState }) {
  const now = useNow(1000)
  return (
    <Panel title="Listening for" aside={<span className="tabular">{actions.length} kinds</span>}>
      <ListeningFor actions={actions} gate={state.gate} now={now} />
    </Panel>
  )
}

function DemoPanel({ snapshot }: { snapshot: Snapshot }) {
  const mock = snapshot.controller.mock
  if (!mock) return null
  return (
    <Panel
      title={
        <span className="inline-flex items-center gap-1.5">
          <FlaskConical className="h-3.5 w-3.5 text-warn" aria-hidden />
          Demo controls
        </span>
      }
      aside={<span>scripted replay</span>}
    >
      <div className="flex flex-wrap gap-2 px-4 py-3">
        <Button onClick={() => mock.simulateSilence()}>
          <VolumeX className="h-3.5 w-3.5" aria-hidden />
          Mic goes silent
        </Button>
        <Button onClick={() => mock.dropConnection()}>
          <Unplug className="h-3.5 w-3.5" aria-hidden />
          Drop connection
        </Button>
        <Button onClick={() => mock.replay()}>
          <RotateCcw className="h-3.5 w-3.5" aria-hidden />
          Replay
        </Button>
      </div>
      <div className="flex items-center justify-between gap-3 border-t border-border px-4 py-2.5 text-[12px] text-muted">
        <span>People in the call</span>
        <Segmented
          value={mock.cast()}
          options={([1, 2, 3, 4] as const).map((n) => ({ value: n, label: String(n) }))}
          onChange={(n) => mock.replay(n)}
          label="Number of speakers in the scripted call"
        />
      </div>
    </Panel>
  )
}

function TrySaying({ actions }: { actions: readonly LiveGateAction[] }) {
  const examples = actions
    .filter((a) => a.id !== 'speaker_intro')
    .slice(0, 3)
    .map((a) => a.examples[0])
    .filter(Boolean)
  return (
    <div className="flex flex-col gap-1.5">
      <span className="text-[12px] font-medium text-fg-2">Try saying</span>
      <ul className="flex flex-col gap-1">
        {examples.map((ex) => (
          <li key={ex} className="text-[12.5px] text-fg">
            “{ex}”
          </li>
        ))}
      </ul>
    </div>
  )
}

function CallHeader({ snapshot }: { snapshot: Snapshot }) {
  const { phase, session, controller, startedAt, endedAt } = snapshot
  const navigate = useNavigate()
  const recording = phase === 'live'
  const following = phase === 'viewing'
  const ended = phase === 'ended'
  const now = useNow(1000, !ended)
  const elapsed = startedAt ? (ended ? (endedAt ?? now) : now) - startedAt : 0
  const owner = session?.isOwner ?? true
  const [confirm, setConfirm] = useState(false)

  return (
    <>
      <PageHeader
        crumbs={[{ label: 'Sessions', to: '/sessions' }, { label: session?.title ?? (phase === 'starting' ? 'Starting…' : (session?.meetCode ?? 'Conversation')) }]}
        title={null}
        actions={
          <>
            <span className="hidden items-center gap-3 md:inline-flex">
              {session?.meetCode && (
                <a
                  href={session.meetUrl ?? `https://meet.google.com/${session.meetCode}`}
                  target="_blank"
                  rel="noreferrer"
                  className="mono inline-flex items-center gap-1 text-[12px] text-muted hover:text-fg"
                >
                  {session.meetCode}
                  <ExternalLink className="h-3 w-3" aria-hidden />
                </a>
              )}
              {!ended && <ConnectionLights snapshot={snapshot} />}
            </span>
            {recording && (
              <span className="hidden h-7 items-center rounded-md border border-border bg-surface px-2 sm:inline-flex" title={snapshot.micLabel ?? 'Microphone level'}>
                <LevelMeter active={snapshot.micActive} bars={10} className="h-3.5" />
              </span>
            )}
            <span className="tabular mono min-w-[52px] text-right text-[15px] font-medium text-fg" aria-label={`Elapsed ${formatClock(elapsed)}`}>
              {formatClock(elapsed)}
            </span>
            <StateBadge spec={PHASE[phase] ?? PHASE.ended} />
            {recording && owner && (
              <>
                <Button onClick={() => void controller.stop(false)}>
                  <Square className="h-3 w-3 fill-current" aria-hidden />
                  Stop
                </Button>
                <Button variant="danger-quiet" onClick={() => setConfirm(true)} aria-label="Stop and discard">
                  <Trash2 className="h-3.5 w-3.5" aria-hidden />
                  Discard
                </Button>
              </>
            )}
            {following && (
              <>
                {owner && (
                  <Button variant="primary" onClick={() => void controller.resumeCapture()}>
                    Resume capture
                  </Button>
                )}
                <Button
                  onClick={() => {
                    controller.reset()
                    navigate('/')
                  }}
                >
                  <ArrowLeft className="h-3.5 w-3.5" aria-hidden />
                  Leave
                </Button>
              </>
            )}
            {ended && (
              <>
                {session?.id && !snapshot.discarded && (
                  <Link to={`/sessions/${session.id}`} className={buttonClass('default')}>
                    <History className="h-3.5 w-3.5" aria-hidden />
                    Open in Sessions
                  </Link>
                )}
                <Button
                  variant="primary"
                  onClick={() => {
                    controller.reset()
                    navigate('/')
                  }}
                >
                  <Plus className="h-3.5 w-3.5" aria-hidden />
                  New call
                </Button>
              </>
            )}
          </>
        }
      />
      {confirm && (
        <ConfirmDialog
          title="Stop and discard this call?"
          body="The recording, its transcript and every answer are deleted. This can’t be undone."
          confirmLabel="Discard call"
          danger
          onConfirm={() => {
            setConfirm(false)
            void controller.stop(true)
          }}
          onClose={() => setConfirm(false)}
        />
      )}
    </>
  )
}

function TranscriptEmpty({ phase }: { phase: string }) {
  return (
    <div className="flex flex-col items-center gap-3 py-20 text-center">
      <span className="flex h-5 items-center">
        <LevelMeter active={phase === 'live'} bars={18} />
      </span>
      <p className="text-[13px] text-muted">
        {phase === 'starting'
          ? 'Opening the microphone and the session…'
          : phase === 'ended'
            ? 'Nothing was transcribed in this call.'
            : 'Listening for the first words…'}
      </p>
      {phase === 'live' && <p className="max-w-[46ch] text-[12.5px] text-subtle">Tip: start with introductions (“Hi, I’m Maya”) and RGS puts names on the speakers.</p>}
    </div>
  )
}

function EndedFooter({ sessionId, discarded }: { sessionId: string | null; discarded: boolean }) {
  return (
    <div className={cn('mt-8 flex flex-col items-center gap-3 border-t border-border pt-6 text-center')}>
      <p className="text-[13px] text-muted">{discarded ? 'This recording was discarded.' : 'Ended. The transcript and answers are saved.'}</p>
      {!discarded && sessionId && (
        <Link to={`/sessions/${sessionId}`} className={buttonClass('default')}>
          <History className="h-3.5 w-3.5" aria-hidden />
          Open in Sessions
        </Link>
      )}
    </div>
  )
}
