import { AnimatePresence, motion } from 'motion/react'
import { AudioLines, Info, KeyRound, MicOff, RotateCcw, TriangleAlert, WifiOff, X } from 'lucide-react'
import type { ReactNode } from 'react'

import { LevelMeter } from '@/components/live/LevelMeter'
import { SelectField, buttonClass } from '@/components/kit'
import type { LiveController } from '@/lib/live/controller'
import type { LiveSnapshot } from '@/lib/live/liveRecordController'
import type { SocketState } from '@/lib/live/reconnectingSocket'
import type { MicProblem } from '@/lib/live/signalHealth'
import { cn } from '@/lib/utils'

const DEFAULT_DEVICE = '__default__'

const CAUSE: Record<MicProblem, string> = {
  silent: 'The browser is handing this page pure silence.',
  muted: 'The browser muted it, usually because another tab or app (like Google Meet) took the microphone.',
  stalled: 'Audio capture paused, usually because this tab was hidden or another tab started using the microphone.',
  ended: 'The microphone was disconnected or turned off.',
}

/** Stacked, animated alert strip at the top of the live view. */
export function LiveBanners({ snapshot, controller }: { snapshot: LiveSnapshot; controller: LiveController }) {
  const recording = snapshot.phase === 'live'
  const audioDown = recording && snapshot.audio.state === 'reconnecting'
  const feedDown = (recording || snapshot.phase === 'viewing') && snapshot.events.state === 'reconnecting'
  const items: { key: string; node: ReactNode }[] = []

  if (snapshot.micProblem && recording) items.push({ key: 'mic', node: <MicProblemBanner snapshot={snapshot} controller={controller} /> })
  if (snapshot.authLapsed)
    items.push({
      key: 'auth',
      node: (
        <Banner tone="warn" icon={KeyRound} title="Your sign-in expired">
          Audio keeps buffering here. Open RGS in a new tab and sign in; it resumes on its own.
        </Banner>
      ),
    })
  if (audioDown || feedDown)
    items.push({
      key: 'net',
      node: (
        <Banner
          tone="warn"
          icon={WifiOff}
          title="Connection interrupted"
          action={
            <button type="button" onClick={() => controller.retryConnections()} className={buttonClass('default')}>
              <RotateCcw className="size-3.5" aria-hidden />
              Retry now
            </button>
          }
        >
          {recording
            ? `Audio is buffered on this device${snapshot.audio.bufferedMs >= 1000 ? ` (${(snapshot.audio.bufferedMs / 1000).toFixed(1)} s)` : ''} and sends when it reconnects.`
            : 'The live feed reconnects on its own.'}
        </Banner>
      ),
    })
  if (recording && snapshot.audio.droppedMs > 0)
    items.push({
      key: 'dropped',
      node: (
        <Banner tone="warn" icon={TriangleAlert} title="Some audio was lost">
          The connection was down too long: {(snapshot.audio.droppedMs / 1000).toFixed(0)} s of the oldest buffered audio was discarded.
        </Banner>
      ),
    })
  if (snapshot.error)
    items.push({
      key: 'error',
      node: (
        <Banner tone="danger" icon={TriangleAlert} title={snapshot.error} onDismiss={() => controller.dismissError()} />
      ),
    })
  if (snapshot.warning)
    items.push({
      key: 'warning',
      node: <Banner tone="warn" icon={TriangleAlert} title={snapshot.warning} onDismiss={() => controller.dismissWarning()} />,
    })
  if (snapshot.notice)
    items.push({
      key: 'notice',
      node: (
        <Banner
          tone="info"
          icon={Info}
          title={snapshot.notice}
          onDismiss={() => controller.dismissWarning()}
          action={
            snapshot.phase === 'viewing' && snapshot.session?.isOwner ? (
              <button type="button" onClick={() => void controller.resumeCapture()} className={buttonClass('default')}>
                Resume capture here
              </button>
            ) : undefined
          }
        />
      ),
    })

  return (
    <div className="flex flex-col gap-2 [&:has(>div)]:mb-4">
      <AnimatePresence initial={false}>
        {items.map((item) => (
          <motion.div
            key={item.key}
            initial={{ opacity: 0, height: 0 }}
            animate={{ opacity: 1, height: 'auto' }}
            exit={{ opacity: 0, height: 0 }}
            transition={{ duration: 0.3, ease: [0.22, 1, 0.36, 1] }}
            className="overflow-hidden"
          >
            {item.node}
          </motion.div>
        ))}
      </AnimatePresence>
      {/* Degradations are announced once, politely. */}
      <p className="sr-only" role="status" aria-live="polite">
        {audioDown || feedDown ? 'Connection interrupted. Audio is buffered and will be sent when it reconnects.' : ''}
      </p>
    </div>
  )
}

const TONES = {
  warn: { box: 'border-warn/30 bg-warn-soft', icon: 'text-warn' },
  danger: { box: 'border-fail/35 bg-fail-soft', icon: 'text-fail' },
  info: { box: 'border-border bg-surface-2', icon: 'text-muted' },
} as const

function Banner({
  tone,
  icon: Icon,
  title,
  children,
  action,
  onDismiss,
}: {
  tone: keyof typeof TONES
  icon: typeof Info
  title: string
  children?: ReactNode
  action?: ReactNode
  onDismiss?: () => void
}) {
  const t = TONES[tone]
  return (
    <div role={tone === 'info' ? 'status' : 'alert'} className={cn('flex items-center gap-3 rounded-[10px] border px-4 py-2.5', t.box)}>
      <Icon className={cn('mt-0.5 size-4 shrink-0', t.icon)} aria-hidden />
      <div className="flex min-w-0 flex-1 flex-col gap-0.5">
        <p className="text-[13px] font-medium text-fg">{title}</p>
        {children && <p className="text-[12.5px] leading-relaxed text-muted">{children}</p>}
      </div>
      {action}
      {onDismiss && (
        <button type="button" onClick={onDismiss} aria-label="Dismiss" className="rounded-md p-1 text-subtle hover:text-fg">
          <X className="size-3.5" aria-hidden />
        </button>
      )}
    </div>
  )
}

/** "We hear nothing": the loudest banner, with a mic picker and retry. It
 *  clears itself as soon as real sound arrives. */
function MicProblemBanner({ snapshot, controller }: { snapshot: LiveSnapshot; controller: LiveController }) {
  const problem = snapshot.micProblem!
  const mic = snapshot.micLabel ?? 'the microphone'
  return (
    <div role="alert" className="flex flex-col gap-3 rounded-[10px] border border-fail/40 bg-fail-soft p-4">
      <div className="flex items-start gap-3">
        <span className="grid size-8 shrink-0 place-items-center rounded-[10px] bg-fail-soft">
          <MicOff className="size-4 text-fail" aria-hidden />
        </span>
        <div className="flex min-w-0 flex-1 flex-col gap-1">
          <p className="text-[14px] font-semibold text-fg">
            {problem === 'ended' ? `${mic} disconnected` : `No sound from ${mic}`}
            <span className="font-normal text-muted"> · nothing is being transcribed</span>
          </p>
          <p className="text-[12.5px] leading-relaxed text-muted">
            {CAUSE[problem]} Pick another microphone or unmute it, and keep this tab visible. If it stays silent, use Chrome.
          </p>
        </div>
      </div>
      <div className="flex flex-wrap items-center gap-2 sm:pl-11">
        <SelectField
          aria-label="Microphone"
          icon={<AudioLines className="h-3.5 w-3.5" aria-hidden />}
          className="max-w-xs min-w-[220px] flex-1"
          value={snapshot.deviceId ?? DEFAULT_DEVICE}
          onChange={(e) => controller.setDevice(e.target.value === DEFAULT_DEVICE ? null : e.target.value)}
        >
          <option value={DEFAULT_DEVICE}>System default microphone</option>
          {snapshot.devices.map((d) => (
            <option key={d.deviceId} value={d.deviceId}>
              {d.label || 'Microphone'}
            </option>
          ))}
        </SelectField>
        <button type="button" onClick={() => void controller.retryMic()} className={buttonClass('primary', { size: 'md' })}>
          <RotateCcw className="size-3.5" aria-hidden />
          Retry mic
        </button>
        <span className="flex h-8 items-center rounded-md border border-border bg-surface px-2.5">
          <LevelMeter active={snapshot.micActive} />
        </span>
      </div>
    </div>
  )
}

/** Compact transport lights for the call header. */
export function ConnectionLights({ snapshot }: { snapshot: LiveSnapshot }) {
  const showAudio = snapshot.phase === 'live'
  return (
    <span className="inline-flex items-center gap-3 text-[12px] text-muted">
      {showAudio && <Light label="audio" state={snapshot.audio.state} />}
      <Light label="feed" state={snapshot.events.state} />
    </span>
  )
}

function Light({ label, state }: { label: string; state: SocketState }) {
  const tone =
    state === 'open' ? 'bg-ok' : state === 'connecting' ? 'bg-muted blink' : state === 'reconnecting' ? 'bg-warn blink' : 'bg-idle'
  const text = state === 'open' ? 'ok' : state === 'reconnecting' ? 'retrying' : state
  return (
    <span className="inline-flex items-center gap-1.5" title={`${label}: ${text}`}>
      <span className={cn('size-1.5 rounded-full', tone)} aria-hidden />
      {label}
      <span className="sr-only">{text}</span>
    </span>
  )
}
