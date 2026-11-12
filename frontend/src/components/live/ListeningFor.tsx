import { motion } from 'motion/react'
import { Check, Copy } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'

import type { LiveGateAction } from '@/api/live'
import type { GateState } from '@/lib/live/liveReducer'
import { gateActionMeta } from '@/lib/live/presentation'
import { cn } from '@/lib/utils'

const FLASH_MS = 4000

/** "What I'm listening for": every gate action with phrases that trip it.
 *  A row lights up when its action just fired. Tap a phrase to copy it. */
export function ListeningFor({ actions, gate, now }: { actions: readonly LiveGateAction[]; gate: GateState; now: number }) {
  const fire = gate.lastFire
  const freshFire = fire && now - fire.at < FLASH_MS ? fire : null
  return (
    <div className="flex flex-col">
      <ul className="divide-y divide-border" aria-label="Things you can say">
        {actions.map((action) => (
          <ActionRow
            key={action.id}
            action={action}
            count={gate.fireCounts[action.id] ?? 0}
            fireN={freshFire?.actionId === action.id ? freshFire.n : null}
          />
        ))}
      </ul>
      <p className="border-t border-border px-4 py-2.5 text-[12px] text-subtle">
        Your own words work too. The gate listens for intent, not exact phrases.
      </p>
    </div>
  )
}

function ActionRow({ action, count, fireN }: { action: LiveGateAction; count: number; fireN: number | null }) {
  const meta = gateActionMeta(action.id)
  const Icon = meta.icon
  return (
    <li className="relative px-4 py-3" data-action-id={action.id} data-fired={fireN !== null || undefined}>
      {fireN !== null && (
        <motion.span
          key={fireN}
          aria-hidden
          initial={{ opacity: 1 }}
          animate={{ opacity: 0 }}
          transition={{ duration: 2.4, ease: 'easeOut', delay: 0.6 }}
          className="pointer-events-none absolute inset-0 bg-brand-soft"
        />
      )}
      <div className="relative flex items-start gap-3">
        <span
          aria-hidden
          className={cn(
            'inline-grid size-7 shrink-0 place-items-center rounded-md transition-colors',
            fireN !== null ? 'bg-brand-soft text-brand' : 'bg-selected text-fg-2',
          )}
        >
          <Icon className="size-3.5" />
        </span>
        <div className="flex min-w-0 flex-1 flex-col gap-1">
          <div className="flex items-baseline gap-2">
            <h3 className="min-w-0 truncate text-[13px] font-medium text-fg">{action.label}</h3>
            {count > 0 && (
              <span className="tabular ml-auto inline-flex h-[18px] shrink-0 items-center rounded-[5px] bg-brand-soft px-1.5 text-[11px] font-medium text-brand-text" aria-label={`Fired ${count} time${count === 1 ? '' : 's'} this call`}>
                ×{count}
              </span>
            )}
          </div>
          {action.description && <p className="text-[12.5px] leading-snug text-muted">{action.description}</p>}
          {action.examples.length > 0 && (
            <div className="flex flex-wrap gap-1.5 pt-0.5">
              {action.examples.map((ex) => (
                <PhraseChip key={ex} phrase={ex} />
              ))}
            </div>
          )}
        </div>
      </div>
    </li>
  )
}

function PhraseChip({ phrase }: { phrase: string }) {
  const [copied, setCopied] = useState(false)
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)
  useEffect(() => () => {
    if (timer.current) clearTimeout(timer.current)
  }, [])
  return (
    <button
      type="button"
      onClick={() => {
        void navigator.clipboard?.writeText(phrase).then(() => {
          setCopied(true)
          if (timer.current) clearTimeout(timer.current)
          timer.current = setTimeout(() => setCopied(false), 1400)
        })
      }}
      className="group inline-flex h-6 max-w-full items-center gap-1.5 rounded-md border border-border bg-surface px-2 text-left text-[12px] text-fg-2 shadow-card transition-colors hover:border-border-strong hover:text-fg"
      aria-label={copied ? `Copied: ${phrase}` : `Copy phrase: ${phrase}`}
    >
      <span className="truncate">“{phrase}”</span>
      {copied ? (
        <Check className="size-3 shrink-0 text-ok" aria-hidden />
      ) : (
        <Copy className="size-3 shrink-0 opacity-0 transition-opacity group-hover:opacity-60" aria-hidden />
      )}
    </button>
  )
}
