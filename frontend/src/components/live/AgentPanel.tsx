import { useEffect, useRef, useState } from 'react'

import { gateProviderLabel, type GateProviderId, type LiveGateConfig } from '@/api/live'
import { AgentMark } from '@/components/AgentMark'
import { Panel, Segmented } from '@/components/kit'
import type { PresenceInfo } from '@/components/live/presence'
import { StatusDot } from '@/components/status'
import type { GateState } from '@/lib/live/liveReducer'
import { gateActionMeta } from '@/lib/live/presentation'
import { cn } from '@/lib/utils'

const HISTORY = 32

/**
 * The agent: presence, what it is doing in words, and the decision gate's
 * heartbeat (one bar per check: latency, orange when it fired).
 */
export function AgentPanel({
  presence,
  gate,
  gateConfig,
  live,
  canSwitch,
  switching,
  onSwitchProvider,
  now,
}: {
  presence: PresenceInfo
  gate: GateState
  gateConfig: LiveGateConfig | null
  live: boolean
  canSwitch: boolean
  switching: boolean
  onSwitchProvider: (provider: GateProviderId) => void
  now: number
}) {
  const tick = gate.lastTick
  const history = useTickHistory(gate)
  const providers = gateConfig?.providers ?? []
  const active = gate.provider ?? gateConfig?.defaultProvider ?? null
  const sinceS = tick ? Math.max(0, (now - tick.at) / 1000) : null
  const stale = live && sinceS !== null && gateConfig?.intervalS ? sinceS > gateConfig.intervalS * 4 : false
  const gateOn = gateConfig?.enabled !== false
  const fired = Object.values(gate.fireCounts).reduce((a, b) => a + b, 0)
  const lastDecision = tick ? (tick.decision && tick.decision !== 'none' ? gateActionMeta(tick.decision).label : 'Nothing to do') : '—'

  return (
    <Panel
      title="Agent"
      aside={
        <span className={cn('inline-flex items-center gap-1.5', stale && 'text-warn-ink')}>
          <StatusDot tone={!live ? 'idle' : stale ? 'warn' : 'ok'} />
          {!live ? 'Idle' : stale ? 'No recent checks' : sinceS === null ? 'Warming up' : `Checked ${sinceS < 1 ? 'just now' : `${sinceS.toFixed(0)}s ago`}`}
        </span>
      }
    >
      <div className="flex items-center gap-3.5 px-4 pt-4 pb-3.5">
        <AgentMark state={presence.state} pulseKey={gate.tickCount} size={48} live={live} />
        <div className="min-w-0 flex-1">
          <div
            className={cn(
              'truncate text-[15px] font-semibold tracking-[-0.01em]',
              presence.state === 'thinking' || presence.state === 'found' ? 'text-brand-text' : 'text-fg',
            )}
          >
            {presence.title}
          </div>
          {presence.detail && <p className="mt-0.5 line-clamp-2 text-[12.5px] leading-snug text-muted">{presence.detail}</p>}
        </div>
      </div>
      <p className="sr-only" aria-live="polite">
        {presence.state === 'thinking' || presence.state === 'found' ? `${presence.title} ${presence.detail ?? ''}` : ''}
      </p>

      {gateOn && (
        <div className="border-t border-border px-4 py-3.5">
          <div className="flex items-center justify-between gap-3">
            <span className="text-[12px] text-muted">Decision gate</span>
            {canSwitch && providers.length > 1 ? (
              <Segmented
                label="Decision gate engine"
                value={active ?? providers[0].id}
                onChange={onSwitchProvider}
                disabled={switching}
                options={providers.map((p) => ({ value: p.id, label: p.label, title: p.available ? undefined : 'Not configured on this server' }))}
              />
            ) : (
              <span className="text-[12.5px] font-medium text-fg">{gateProviderLabel(active, providers) ?? 'Server default'}</span>
            )}
          </div>

          <Heartbeat history={history} />

          <dl className="mt-3 grid grid-cols-3 gap-3">
            <Fig label="Latency" value={tick?.latencyMs != null ? `${tick.latencyMs} ms` : '—'} mono />
            <Fig label="Checks" value={String(gate.tickCount)} mono />
            <Fig label="Acted" value={String(fired)} mono hot={fired > 0} />
          </dl>
          <div className="mt-3 flex items-center justify-between gap-3 text-[12px]">
            <span className="text-muted">Last decision</span>
            <span className={cn('truncate font-medium', tick?.decision && tick.decision !== 'none' ? 'text-brand-text' : 'text-fg-2')}>{lastDecision}</span>
          </div>
        </div>
      )}
    </Panel>
  )
}

function Fig({ label, value, mono, hot }: { label: string; value: string; mono?: boolean; hot?: boolean }) {
  return (
    <div className="min-w-0">
      <dt className="text-[11.5px] text-subtle">{label}</dt>
      <dd className={cn('tabular mt-0.5 truncate text-[15px] font-semibold', mono && 'mono font-medium', hot ? 'text-brand-text' : 'text-fg')}>{value}</dd>
    </div>
  )
}

interface TickPoint {
  n: number
  latency: number
  fired: boolean
}

function useTickHistory(gate: GateState): TickPoint[] {
  const [points, setPoints] = useState<TickPoint[]>([])
  const seen = useRef(0)
  const tick = gate.lastTick
  useEffect(() => {
    if (!tick || gate.tickCount === seen.current) return
    seen.current = gate.tickCount
    const point = { n: gate.tickCount, latency: tick.latencyMs ?? 0, fired: !!tick.decision && tick.decision !== 'none' }
    setPoints((prev) => [...prev.slice(-(HISTORY - 1)), point])
  }, [gate.tickCount, tick])
  return points
}

/** One bar per gate check: height = latency, orange = the gate acted. */
function Heartbeat({ history }: { history: TickPoint[] }) {
  const max = Math.max(400, ...history.map((p) => p.latency))
  const slots = Array.from({ length: HISTORY }, (_, i) => history[history.length - HISTORY + i] ?? null)
  return (
    <div className="mt-3 flex h-8 items-end gap-[2px]" role="img" aria-label={`Decision gate latency, last ${history.length} checks`}>
      {slots.map((p, i) => (
        <span
          key={p ? p.n : `empty-${i}`}
          className={cn('pop-in flex-1 rounded-[1.5px]', p ? (p.fired ? 'bg-brand' : 'bg-bar') : 'bg-track')}
          style={{ height: p ? `${Math.max(10, (p.latency / max) * 100)}%` : '10%' }}
          title={p ? `${p.latency} ms${p.fired ? ' · acted' : ''}` : undefined}
        />
      ))}
    </div>
  )
}
