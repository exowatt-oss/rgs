import { ArrowRight, CircleCheck, Mic, MonitorUp, Square, Users, Video } from 'lucide-react'
import { useEffect, useState, type ReactNode } from 'react'
import { Link, useNavigate } from 'react-router-dom'

import { DEFAULT_GATE_ACTIONS, gateProviderLabel, normalizeMeetUrl, type LiveSessionInfo, type SessionKind } from '@/api/live'
import { AgentMark } from '@/components/AgentMark'
import { PRODUCT_LINE } from '@/components/brand'
import { Button, ErrorNote, PageBody, PageHeader, Panel, Segmented, SelectField, Stat, inputClass } from '@/components/kit'
import { LevelMeter } from '@/components/live/LevelMeter'
import { DeliveryPicker } from '@/components/recap/DeliveryPicker'
import { channelsOf, deliveryPhrase, useDeliveryChoice } from '@/components/recap/recap'
import { shareBlockedReason } from '@/components/recap/share'
import { ShareToggle } from '@/components/recap/ShareToggle'
import { SessionTable } from '@/components/sessions/SessionTable'
import { StatusDot } from '@/components/status'
import { useNow } from '@/lib/hooks'
import { formatClock, gateActionMeta } from '@/lib/live/presentation'
import { useLiveRecord } from '@/lib/liveRecording'
import { isMockMode } from '@/lib/mockMode'
import { useActiveSessions, useSessionList } from '@/lib/sessions'
import { cn } from '@/lib/utils'

const DEFAULT_DEVICE = '__default__'

export function HomePage() {
  const now = useNow(30_000)
  const list = useSessionList()
  const recent = (list.data ?? []).filter((s) => s.status !== 'cancelled').slice(0, 6)
  const [kind, setKind] = useState<SessionKind>(() => (new URLSearchParams(window.location.search).get('mode') === 'adhoc' ? 'adhoc' : 'call'))
  return (
    <>
      <PageHeader title={kind === 'adhoc' ? 'New conversation' : 'New call'} subtitle={PRODUCT_LINE} />
      <PageBody className="flex flex-col gap-5">
        <div className="grid grid-cols-1 items-start gap-5 lg:grid-cols-[minmax(0,1fr)_320px] xl:grid-cols-[minmax(0,1fr)_380px]">
          <StartPanel kind={kind} onKind={setKind} />
          <div className="flex min-w-0 flex-col gap-5">
            <ListensForPanel />
            <LiveNowPanel />
          </div>
        </div>
        <Panel
          title="Recent sessions"
          aside={
            <Link to="/sessions" className="inline-flex items-center gap-1 text-muted transition-colors hover:text-fg">
              All sessions
              <ArrowRight className="h-3.5 w-3.5" aria-hidden />
            </Link>
          }
        >
          <SessionTable sessions={recent} loading={list.isLoading} now={now} rows={4} />
        </Panel>
      </PageBody>
    </>
  )
}

/** The setup card: what you're recording, status line + one row per setting (devboxes rows). */
function StartPanel({ kind, onKind }: { kind: SessionKind; onKind: (k: SessionKind) => void }) {
  const live = useLiveRecord()
  const { controller, config, phase } = live
  const navigate = useNavigate()
  const [meet, setMeet] = useState(() => new URLSearchParams(window.location.search).get('meet') ?? (isMockMode() ? 'meet.google.com/abc-defg-hij' : ''))
  const [title, setTitle] = useState('')
  const [delivery, setDelivery] = useDeliveryChoice()
  // Off by default, every time: sharing is a per-conversation decision.
  const [share, setShare] = useState(false)
  const [touched, setTouched] = useState(false)
  const adhoc = kind === 'adhoc'
  const meetUrl = normalizeMeetUrl(meet)
  const recapCfg = config?.recap ?? null
  const recapOn = !recapCfg || (recapCfg.enabled && recapCfg.newspapers)
  const shareBlocked = shareBlockedReason(channelsOf(delivery), recapCfg, recapOn)
  const busy = phase === 'starting' || phase === 'live' || phase === 'stopping' || phase === 'viewing'

  // Leaving setup releases a preview-only mic (never a recording one).
  useEffect(() => () => void controller.stopPreview(), [controller])

  const providers = config?.gate.providers?.length
    ? config.gate.providers
    : [
        { id: 'jev', label: 'TypeSafe Jev', available: true },
        { id: 'openai', label: 'OpenAI Decisions', available: true },
      ]
  const gateEnabled = config?.gate.enabled ?? true
  const providerValue = live.gateProviderChoice ?? config?.gate.defaultProvider ?? providers[0]?.id ?? 'jev'
  const disabledReason = !config
    ? null
    : !config.enabled
      ? 'Live capture is turned off on this server.'
      : !config.asrConfigured
        ? 'Transcription isn’t configured on this server yet.'
        : null
  const invalid = !adhoc && touched && !!meet && !meetUrl
  const canStart = (adhoc || !!meetUrl) && !busy && !disabledReason

  const chooseKind = (k: SessionKind) => {
    onKind(k)
    // A room has no tab to share: ad-hoc listens through the mic only.
    if (k === 'adhoc' && live.source !== 'mic') controller.setSource('mic')
  }

  const onStart = () => {
    setTouched(true)
    if (!canStart || (!adhoc && !meetUrl)) return
    if (adhoc && live.source !== 'mic') controller.setSource('mic')
    void controller.start(adhoc ? null : meetUrl, {
      title: adhoc ? title : undefined,
      deliverVia: recapOn ? channelsOf(delivery) : undefined,
      shareWithParticipants: share && !shareBlocked,
    })
    navigate('/live')
  }

  if (busy) return <ActiveCallPanel phase={phase} session={live.session} startedAt={live.startedAt} />

  return (
    <Panel>
      <form
        onSubmit={(e) => {
          e.preventDefault()
          onStart()
        }}
        className="flex flex-col gap-5 p-5"
      >
        <KindPicker value={kind} onChange={chooseKind} />

        <div className="flex flex-wrap items-start justify-between gap-4">
          <div className="flex min-w-0 items-center gap-3.5">
            <AgentMark state={live.micActive ? 'listening' : 'idle'} size={44} live={live.micActive} />
            <div className="min-w-0">
              <div className="flex items-center gap-2">
                <span
                  className={cn(
                    'inline-flex h-8 items-center gap-2 rounded-full pr-3.5 pl-2.5 text-[15px] font-medium',
                    canStart ? 'bg-ok-soft text-ok-ink' : 'bg-selected text-fg-2',
                  )}
                >
                  {canStart ? <CircleCheck className="h-[18px] w-[18px] text-ok" aria-hidden /> : <StatusDot tone="idle" className="mx-1" />}
                  {canStart ? 'Ready to listen' : disabledReason ? 'Unavailable' : 'Waiting for a Meet link'}
                </span>
              </div>
              <p className="mt-1.5 text-[13px] text-muted">
                {adhoc
                  ? 'RGS listens through this device’s microphone. Set it where everyone can be heard.'
                  : 'RGS joins as you, from this browser. Answers land next to the line that asked.'}
              </p>
            </div>
          </div>
          <Button type="submit" variant="primary" size="md" disabled={!canStart} className="min-w-[132px]">
            <span className="relative flex h-2 w-2">
              <span className="absolute inset-0 rounded-full bg-white" />
            </span>
            Start listening
          </Button>
        </div>

        {adhoc ? (
          <Row label="Title" htmlFor="adhoc-title" hint="Optional. It becomes the edition’s title; RGS names it from the conversation otherwise.">
            <input
              id="adhoc-title"
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              placeholder="Fictional library demo"
              autoComplete="off"
              maxLength={300}
              className={cn(inputClass, 'w-full max-w-[420px]')}
            />
          </Row>
        ) : (
        <>
        <Row
          label="Meet link"
          htmlFor="meet-link"
          hint={
            invalid ? (
              <span className="text-fail-ink">That isn’t a Google Meet link. It looks like meet.google.com/abc-defg-hij.</span>
            ) : (
              'Paste the link or just the code.'
            )
          }
        >
          <div className="relative w-full max-w-[420px]">
            <input
              id="meet-link"
              value={meet}
              onChange={(e) => setMeet(e.target.value)}
              onBlur={() => setTouched(true)}
              placeholder="meet.google.com/abc-defg-hij"
              autoComplete="off"
              spellCheck={false}
              inputMode="url"
              aria-invalid={invalid}
              className={cn(inputClass, 'mono pr-8', invalid && 'border-fail/60')}
            />
            {meetUrl && <CircleCheck className="absolute top-2 right-2.5 h-4 w-4 text-ok" aria-label="Valid Meet link" />}
          </div>
        </Row>

        <Row
          label="Audio"
          hint={
            live.source === 'mic+tab'
              ? 'You’ll pick the Meet tab and tick “Share tab audio”, so RGS hears everyone, even on headphones.'
              : 'Best with speakers in a shared room. On headphones, add tab audio.'
          }
        >
          <Segmented
            label="Audio source"
            value={live.source}
            onChange={(v) => controller.setSource(v)}
            options={[
              { value: 'mic', label: <span className="inline-flex items-center gap-1.5"><Mic className="h-3.5 w-3.5" aria-hidden />Microphone</span> },
              { value: 'mic+tab', label: <span className="inline-flex items-center gap-1.5"><MonitorUp className="h-3.5 w-3.5" aria-hidden />Mic + tab audio</span> },
            ]}
          />
        </Row>
        </>
        )}

        <Row
          label="Microphone"
          htmlFor="mic-select"
          hint={
            live.micPermission === 'denied'
              ? <span className="text-warn-ink">Microphone access is blocked for this site. Allow it in the address bar, then check again.</span>
              : live.micActive
                ? 'Speak normally: the bars should move.'
                : 'Check it before the call starts.'
          }
        >
          <div className="flex w-full max-w-[420px] items-center gap-2">
            <SelectField
              id="mic-select"
              value={live.deviceId ?? DEFAULT_DEVICE}
              onChange={(e) => controller.setDevice(e.target.value === DEFAULT_DEVICE ? null : e.target.value)}
              className="flex-1"
            >
              <option value={DEFAULT_DEVICE}>System default microphone</option>
              {live.devices.map((d) => (
                <option key={d.deviceId} value={d.deviceId}>
                  {d.label || 'Microphone'}
                </option>
              ))}
            </SelectField>
            {live.micActive ? (
              <span className="inline-flex h-8 shrink-0 items-center gap-2 rounded-md border border-border px-2.5" title={live.micLabel ?? undefined}>
                <LevelMeter active bars={12} />
                <button type="button" onClick={() => void controller.stopPreview()} className="text-subtle hover:text-fg" aria-label="Stop mic check">
                  <Square className="h-3 w-3" aria-hidden />
                </button>
              </span>
            ) : (
              <Button size="md" onClick={() => void controller.startPreview()}>
                Check mic
              </Button>
            )}
          </div>
        </Row>

        <Row
          label="Decision gate"
          hint={
            gateEnabled
              ? `Decides when the room is asking for something${config?.gate.intervalS ? `, about every ${config.gate.intervalS.toFixed(1)} s` : ''}. You can switch mid-call.`
              : 'The decision gate is off on this server; RGS will transcribe only.'
          }
        >
          <Segmented
            label="Decision gate engine"
            value={providerValue}
            onChange={(v) => controller.setGateProviderChoice(v)}
            disabled={!gateEnabled}
            options={providers.map((p) => ({
              value: p.id,
              label: gateProviderLabel(p.id, providers) ?? p.label,
              title: p.available ? undefined : 'Not configured on this server',
            }))}
          />
        </Row>

        <Row
          label="Send me the edition"
          hint={
            recapOn
              ? `When you stop, RGS writes the notes (summary, decisions, action items with owners, quotes), prints them as a newspaper edition and sends you the link ${deliveryPhrase(channelsOf(delivery))}.`
              : 'Newspaper recaps aren’t set up on this server yet; the transcript and answers are still saved.'
          }
        >
          <DeliveryPicker value={delivery} onChange={setDelivery} config={recapCfg} disabled={!recapOn} />
        </Row>
        <ShareToggle checked={share} onChange={setShare} disabledReason={shareBlocked} className="-mt-2" />

        {(live.error || disabledReason || live.configError) && (
          <div className="flex items-start justify-between gap-3 border-t border-border pt-4">
            <ErrorNote error={new Error(live.error ?? disabledReason ?? live.configError ?? '')} />
            {live.error && (
              <Button variant="ghost" onClick={() => controller.dismissError()}>
                Dismiss
              </Button>
            )}
          </div>
        )}
      </form>
    </Panel>
  )
}

/** The two ways to start: a Meet call, or an ad-hoc (in-person) conversation. */
function KindPicker({ value, onChange }: { value: SessionKind; onChange: (k: SessionKind) => void }) {
  const options: { value: SessionKind; icon: typeof Video; title: string; body: string }[] = [
    { value: 'call', icon: Video, title: 'Google Meet call', body: 'RGS joins as you and answers next to the line that asked.' },
    { value: 'adhoc', icon: Users, title: 'Ad-hoc conversation', body: 'In person, no link. The notes come back as a newspaper edition.' },
  ]
  return (
    <div role="radiogroup" aria-label="What are you recording?" className="grid grid-cols-1 gap-2.5 sm:grid-cols-2">
      {options.map((o) => {
        const on = o.value === value
        const Icon = o.icon
        return (
          <button
            key={o.value}
            type="button"
            role="radio"
            aria-checked={on}
            onClick={() => !on && onChange(o.value)}
            className={cn(
              'flex items-start gap-3 rounded-lg border px-3.5 py-3 text-left transition-[background-color,border-color,box-shadow] duration-100',
              on ? 'border-brand/50 bg-brand-soft shadow-[0_0_0_1px_var(--brand-line)]' : 'border-border bg-surface hover:border-border-strong hover:bg-hover',
            )}
          >
            <span className={cn('flex h-8 w-8 shrink-0 items-center justify-center rounded-md', on ? 'ex-bg-gradient' : 'bg-selected text-fg-2')}>
              <Icon className="h-4 w-4" strokeWidth={1.9} aria-hidden />
            </span>
            <span className="min-w-0">
              <span className="block text-[13.5px] font-semibold text-fg">{o.title}</span>
              <span className="mt-0.5 block text-[12.5px] leading-[1.4] text-muted">{o.body}</span>
            </span>
          </button>
        )
      })}
    </div>
  )
}

/** devboxes setting row: label left, control right, hint under, hairline above. */
function Row({ label, htmlFor, hint, children }: { label: string; htmlFor?: string; hint?: ReactNode; children: ReactNode }) {
  const Label = htmlFor ? 'label' : 'span'
  return (
    <div className="flex flex-col gap-2 border-t border-border pt-4">
      <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2">
        <Label htmlFor={htmlFor} className="text-[13px] font-medium text-fg">
          {label}
        </Label>
        {children}
      </div>
      {hint && <p className="text-[12.5px] text-muted">{hint}</p>}
    </div>
  )
}

function ActiveCallPanel({ phase, session, startedAt }: { phase: string; session: LiveSessionInfo | null; startedAt: number | null }) {
  const now = useNow(1000)
  return (
    <Panel>
      <div className="flex flex-col gap-5 p-5">
        <div className="flex flex-wrap items-center justify-between gap-4">
          <div className="flex min-w-0 items-center gap-3.5">
            <AgentMark state="listening" size={44} live />
            <div className="min-w-0">
              <span className="inline-flex h-8 items-center gap-2 rounded-full bg-brand-soft pr-3.5 pl-2.5 text-[15px] font-medium text-brand-text">
                <StatusDot tone="live" pulse className="mx-1" />
                {phase === 'viewing' ? 'Following a call' : phase === 'starting' ? 'Starting' : 'In a call'}
              </span>
              <p className="mt-1.5 truncate text-[13px] text-muted">{session?.title ?? session?.meetCode ?? 'Opening the session…'}</p>
            </div>
          </div>
          <Link
            to={session ? `/live/${session.id}` : '/live'}
            className="inline-flex h-8 items-center gap-1.5 rounded-md bg-brand px-3 text-[13px] font-medium text-white hover:bg-brand/90"
          >
            Open call
            <ArrowRight className="h-3.5 w-3.5" aria-hidden />
          </Link>
        </div>
        <div className="grid grid-cols-2 gap-4 border-t border-border pt-4 sm:grid-cols-3">
          <Stat label="Elapsed" value={<span className="mono">{startedAt ? formatClock(now - startedAt) : '—'}</span>} />
          {session && !session.meetCode ? (
            <Stat label="Where" value={<span className="text-[17px]">In person</span>} />
          ) : (
            <Stat label="Meet code" value={<span className="mono text-[17px]">{session?.meetCode ?? '—'}</span>} />
          )}
          <Stat label="Decision gate" value={<span className="text-[17px]">{gateProviderLabel(session?.gateProvider ?? null) ?? '—'}</span>} />
        </div>
      </div>
    </Panel>
  )
}

function ListensForPanel() {
  const config = useLiveRecord().config
  const actions = (config?.gate.actions?.length ? config.gate.actions : DEFAULT_GATE_ACTIONS).filter((a) => a.id !== 'speaker_intro')
  return (
    <Panel title="What RGS listens for" aside={config?.gate.intervalS ? <span className="tabular">every {config.gate.intervalS.toFixed(1)} s</span> : null}>
      <ul className="divide-y divide-border">
        {actions.map((a) => {
          const meta = gateActionMeta(a.id)
          const Icon = meta.icon
          return (
            <li key={a.id} className="flex gap-3 px-4 py-3">
              <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md bg-selected text-fg-2">
                <Icon className="h-3.5 w-3.5" strokeWidth={1.75} />
              </span>
              <div className="min-w-0">
                <div className="text-[13px] font-medium text-fg">{a.label}</div>
                {a.examples[0] && <div className="mt-0.5 truncate text-[12.5px] text-muted">“{a.examples[0]}”</div>}
              </div>
            </li>
          )
        })}
        <li className="flex gap-3 px-4 py-3">
          <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md bg-selected text-fg-2">
            {(() => {
              const Icon = gateActionMeta('speaker_intro').icon
              return <Icon className="h-3.5 w-3.5" strokeWidth={1.75} />
            })()}
          </span>
          <div className="min-w-0">
            <div className="text-[13px] font-medium text-fg">Introductions</div>
            <div className="mt-0.5 text-[12.5px] text-muted">“Hi, I’m Maya” names that speaker.</div>
          </div>
        </li>
      </ul>
    </Panel>
  )
}

function LiveNowPanel() {
  const { data } = useActiveSessions()
  const mine = useLiveRecord().session?.id ?? null
  const now = useNow(1000)
  const sessions = (data ?? []).filter((s) => s.id !== mine && s.status === 'live')
  return (
    <Panel title="Live now" aside={<span className="tabular">{sessions.length}</span>}>
      {sessions.length === 0 ? (
        <p className="px-4 py-4 text-[12.5px] text-muted">No other calls are being listened to right now.</p>
      ) : (
        <ul className="divide-y divide-border">
          {sessions.map((s) => {
            const started = s.startedAt ? Date.parse(s.startedAt) : null
            return (
              <li key={s.id}>
                <Link to={`/live/${s.id}`} className="group flex items-center gap-3 px-4 py-2.5 transition-colors hover:bg-hover">
                  <StatusDot tone="live" pulse />
                  <span className="min-w-0 flex-1 leading-tight">
                    <span className="block truncate text-[13px] font-medium text-fg">{s.title ?? s.meetCode ?? 'Untitled call'}</span>
                    <span className="mt-0.5 block truncate text-[12px] text-subtle">
                      {s.startedBy?.name ?? s.startedBy?.email ?? 'Someone'} · <span className="mono tabular">{started ? formatClock(now - started) : '—'}</span>
                    </span>
                  </span>
                  <span className="text-[12px] font-medium text-muted group-hover:text-fg">Follow</span>
                </Link>
              </li>
            )
          })}
        </ul>
      )}
    </Panel>
  )
}
