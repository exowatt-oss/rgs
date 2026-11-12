import { CircleCheck, CircleMinus, CircleX, LoaderCircle, Radio, type LucideIcon } from 'lucide-react'

import type { LiveSessionStatus } from '@/api/live'
import { cn } from '@/lib/utils'

export type Tone = 'ok' | 'fail' | 'run' | 'warn' | 'idle' | 'live'

interface ToneSpec {
  mark: string // icon colour
  ink: string // text colour
  chip: string // subtle background
  dot: string // solid fill
}

/** devboxes / cicd tone recipes; `live` is the agent's own (brand) tone. */
export const TONES: Record<Tone, ToneSpec> = {
  ok: { mark: 'text-ok', ink: 'text-ok-ink', chip: 'bg-ok-soft', dot: 'bg-ok' },
  fail: { mark: 'text-fail', ink: 'text-fail-ink', chip: 'bg-fail-soft', dot: 'bg-fail' },
  run: { mark: 'text-run', ink: 'text-run-ink', chip: 'bg-run-soft', dot: 'bg-run' },
  warn: { mark: 'text-warn', ink: 'text-warn-ink', chip: 'bg-warn-soft', dot: 'bg-warn' },
  idle: { mark: 'text-idle', ink: 'text-muted', chip: 'bg-selected', dot: 'bg-idle' },
  live: { mark: 'text-brand', ink: 'text-brand-text', chip: 'bg-brand-soft', dot: 'bg-brand' },
}

export interface StateSpec {
  tone: Tone
  label: string
  icon: LucideIcon
  spin?: boolean
  pulse?: boolean
}

export const SESSION_STATES: Record<LiveSessionStatus, StateSpec> = {
  live: { tone: 'live', label: 'Live', icon: Radio, pulse: true },
  stopping: { tone: 'run', label: 'Saving', icon: LoaderCircle, spin: true },
  finalized: { tone: 'ok', label: 'Saved', icon: CircleCheck },
  failed: { tone: 'fail', label: 'Failed', icon: CircleX },
  cancelled: { tone: 'idle', label: 'Discarded', icon: CircleMinus },
}

export function describeSession(status: string | null | undefined): StateSpec {
  return (status && SESSION_STATES[status as LiveSessionStatus]) || { tone: 'idle', label: status ?? 'Unknown', icon: CircleMinus }
}

export function StateIcon({ spec, className }: { spec: StateSpec; className?: string }) {
  const Icon = spec.icon
  return (
    <Icon
      aria-hidden
      className={cn('h-4 w-4 shrink-0', TONES[spec.tone].mark, spec.spin && 'spin', spec.pulse && 'blink', className)}
      strokeWidth={2}
    />
  )
}

/** Icon + label — state never relies on colour alone. */
export function StateBadge({
  spec,
  className,
  subtle = false,
  size = 'sm',
}: {
  spec: StateSpec
  className?: string
  /** No chip background — for dense rows. */
  subtle?: boolean
  size?: 'sm' | 'lg'
}) {
  const t = TONES[spec.tone]
  return (
    <span
      className={cn(
        'inline-flex items-center font-medium whitespace-nowrap',
        size === 'lg' ? 'gap-2 text-[15px]' : 'gap-1.5 text-[12px]',
        !subtle && (size === 'lg' ? 'h-8 rounded-full pr-3.5 pl-2.5' : 'h-[22px] rounded-full pr-2.5 pl-1.5'),
        !subtle && t.chip,
        t.ink,
        className,
      )}
    >
      <StateIcon spec={spec} className={size === 'lg' ? 'h-[18px] w-[18px]' : 'h-3.5 w-3.5'} />
      {spec.label}
    </span>
  )
}

export function SessionBadge({ status, ...rest }: { status: string | null | undefined; className?: string; subtle?: boolean; size?: 'sm' | 'lg' }) {
  return <StateBadge spec={describeSession(status)} {...rest} />
}

export function StatusDot({ tone, className, pulse }: { tone: Tone; className?: string; pulse?: boolean }) {
  return <span className={cn('inline-flex h-2 w-2 shrink-0 rounded-full', TONES[tone].dot, pulse && 'blink', className)} aria-hidden />
}
