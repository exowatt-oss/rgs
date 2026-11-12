import { useId, type ReactNode } from 'react'

import { cn } from '@/lib/utils'

import { SHARE_HINT } from './share'

/**
 * "Share with meeting participants": opt-in (off by default) sharing of the
 * edition and the RGS link with the people RGS recognised. A plain checkbox
 * with one line of help under it; disabled with the reason when it can't apply.
 */
export function ShareToggle({
  checked,
  onChange,
  disabledReason,
  busy,
  hint,
  className,
}: {
  checked: boolean
  onChange: (v: boolean) => void
  /** Why it can't be used right now (shown instead of the hint). */
  disabledReason?: string | null
  busy?: boolean
  /** Replaces the default help line. */
  hint?: ReactNode
  className?: string
}) {
  const id = useId()
  const disabled = !!disabledReason || busy
  return (
    <div className={cn('flex min-w-0 flex-col gap-0.5', className)}>
      <label
        htmlFor={id}
        className={cn(
          'inline-flex w-fit items-center gap-2 text-[13px] font-medium text-fg select-none',
          disabledReason ? 'cursor-not-allowed text-fg-2 opacity-70' : 'cursor-pointer',
        )}
      >
        <input
          id={id}
          type="checkbox"
          checked={checked && !disabledReason}
          disabled={disabled}
          onChange={(e) => onChange(e.target.checked)}
          aria-describedby={`${id}-hint`}
          className="size-3.5 shrink-0 cursor-[inherit] accent-brand"
        />
        Share with meeting participants
      </label>
      <p id={`${id}-hint`} className="pl-[22px] text-[12px] leading-[1.45] text-muted">
        {disabledReason ?? hint ?? SHARE_HINT}
      </p>
    </div>
  )
}
