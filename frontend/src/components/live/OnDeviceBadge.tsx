import { Cpu } from 'lucide-react'
import type { OnDeviceInfo } from '@/lib/asr/copperPhonon'
import { cn } from '@/lib/utils'

/** "On-device words (Copper)": the transcript's words come from Copper's
 *  optional Phonon-2 only in the Copper browser, speakers from Nemotron. Shown while this tab feeds
 *  Copper, or when the session already has on-device words (a follower, an
 *  archive). Inside Copper without them, says why in muted text. */
export function OnDeviceBadge({ info, phrases }: { info?: OnDeviceInfo; phrases: number }) {
  const on = info?.status === 'on' || phrases > 0
  if (on) {
    return (
      <span
        className="inline-flex items-center gap-1 rounded-full border border-border bg-surface-2 px-2 py-0.5 text-[11.5px] font-medium text-fg-2"
        title="Optional words heard on this device in the Copper browser by Phonon-2; speakers from Nemotron."
      >
        <Cpu aria-hidden className="size-3" />
        On-device words (Copper)
      </span>
    )
  }
  if (info?.status === 'starting') return <span className="text-[11.5px] text-muted">Copper on-device words…</span>
  if (info?.status === 'unavailable')
    return (
      <span className={cn('max-w-[16rem] truncate text-[11.5px] text-muted')} title={info.reason ?? undefined}>
        On-device words off{info.reason ? ` — ${info.reason}` : ''}
      </span>
    )
  return null
}
