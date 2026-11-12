import { useRef } from 'react'

import { useLevelVar } from '@/lib/useLevel'
import { cn } from '@/lib/utils'

export type AgentState = 'idle' | 'listening' | 'thinking' | 'found' | 'offline' | 'ended'

/**
 * Generic audio activity glyph. One ring pings out per decision-gate check
 * (`pulseKey`), the core
 * breathes with the mic (`live`), a dashed ring turns while the agent works.
 * Pure CSS — cheap enough to sit in a sidebar row.
 */
export function AgentMark({
  state,
  size = 40,
  pulseKey,
  live = false,
  className,
}: {
  state: AgentState
  size?: number
  pulseKey?: number
  live?: boolean
  className?: string
}) {
  const coreRef = useRef<HTMLSpanElement>(null)
  useLevelVar(coreRef, live && (state === 'listening' || state === 'thinking' || state === 'found'))
  const dim = state === 'offline' || state === 'ended' || state === 'idle'
  const core = Math.round(size * 0.42)
  return (
    <span
      className={cn('relative inline-grid shrink-0 place-items-center', className)}
      style={{ width: size, height: size }}
      data-state={state}
      aria-hidden
    >
      {/* Activity rings. */}
      <span className={cn('absolute inset-0 rounded-full border', dim ? 'border-border-strong' : 'border-brand-line')} />
      <span className={cn('absolute inset-[16%] rounded-full border', dim ? 'border-border' : 'border-brand/50')} />
      {state === 'thinking' && (
        <svg viewBox="0 0 40 40" className="spin absolute inset-0 text-brand" style={{ animationDuration: '2.4s' }}>
          <circle cx="20" cy="20" r="19.4" fill="none" stroke="currentColor" strokeWidth="1.2" strokeDasharray="10 16" strokeLinecap="round" />
        </svg>
      )}
      {/* Heartbeat: a fresh ring per gate tick. */}
      {pulseKey != null && pulseKey > 0 && !dim && (
        <span key={pulseKey} className="ping pointer-events-none absolute rounded-full border border-brand" style={{ width: core, height: core }} />
      )}
      {state === 'found' && <span className="ping absolute inset-[10%] rounded-full bg-brand-soft" />}
      <span
        ref={coreRef}
        className={cn('level-scale rounded-full', dim ? 'bg-idle/60' : 'bg-brand')}
        style={{ width: core, height: core }}
      />
    </span>
  )
}
