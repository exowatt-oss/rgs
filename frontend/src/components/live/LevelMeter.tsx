import { useLiveLevel } from '@/lib/useLevel'
import { cn } from '@/lib/utils'

const BARS = 14

/** Segmented input meter (instrument style). Bars past ~70% read warm. */
export function LevelMeter({ active, className, bars = BARS }: { active: boolean; className?: string; bars?: number }) {
  const level = useLiveLevel()
  const lit = active ? Math.round(Math.min(1, level * 1.35) * bars) : 0
  return (
    <span
      className={cn('inline-flex h-4 items-end gap-[2px]', className)}
      role="meter"
      aria-label="Microphone level"
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={Math.round(level * 100)}
    >
      {Array.from({ length: bars }, (_, i) => {
        const on = i < lit
        const hot = i >= Math.floor(bars * 0.72)
        return (
          <span
            key={i}
            className={cn(
              'w-[3px] rounded-[1px] transition-[background-color,opacity] duration-75',
              on ? (hot ? 'bg-brand' : 'bg-bar-strong') : 'bg-track-strong',
            )}
            style={{ height: `${40 + (i / (bars - 1)) * 60}%` }}
          />
        )
      })}
    </span>
  )
}
