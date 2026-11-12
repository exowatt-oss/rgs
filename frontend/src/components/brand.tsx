import { cn } from '@/lib/utils'

export const PRODUCT_LINE = 'Your agent in every call'

/** Generic waveform mark; no organization trademark artwork. */
export function Wordmark({ className }: { className?: string; org?: boolean }) {
  return (
    <span className={cn('flex min-w-0 items-center gap-2.5', className)}>
      <span className="inline-grid h-[22px] w-[22px] place-items-center rounded-[6px] bg-brand text-white" aria-hidden>≋</span>
      <span className="text-[14px] font-semibold tracking-[-0.01em] text-fg">RGS</span>
    </span>
  )
}
