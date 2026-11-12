/** Presentation helpers shared by the live and archive views. */
import type { ComponentType } from 'react'
import { BookA, History, IdCard, ListChecks, ShieldCheck, Sparkles, UserSearch } from 'lucide-react'

/** Literal class strings per speaker slot (Tailwind only emits classes it
 *  can see in source). Colours come from `--color-speaker-N` in index.css.
 *  Text never wears the speaker colour: names use text tokens and the
 *  coloured avatar / mark beside them carries identity. */
export const SPEAKER_CLASSES = [
  { dot: 'bg-speaker-1', avatar: 'bg-speaker-1 text-speaker-1-ink', tint: 'bg-speaker-1/8', lane: 'bg-speaker-1/25', ring: 'ring-speaker-1', fill: 'fill-speaker-1' },
  { dot: 'bg-speaker-2', avatar: 'bg-speaker-2 text-speaker-2-ink', tint: 'bg-speaker-2/8', lane: 'bg-speaker-2/25', ring: 'ring-speaker-2', fill: 'fill-speaker-2' },
  { dot: 'bg-speaker-3', avatar: 'bg-speaker-3 text-speaker-3-ink', tint: 'bg-speaker-3/8', lane: 'bg-speaker-3/25', ring: 'ring-speaker-3', fill: 'fill-speaker-3' },
  { dot: 'bg-speaker-4', avatar: 'bg-speaker-4 text-speaker-4-ink', tint: 'bg-speaker-4/8', lane: 'bg-speaker-4/25', ring: 'ring-speaker-4', fill: 'fill-speaker-4' },
  { dot: 'bg-speaker-5', avatar: 'bg-speaker-5 text-speaker-5-ink', tint: 'bg-speaker-5/8', lane: 'bg-speaker-5/25', ring: 'ring-speaker-5', fill: 'fill-speaker-5' },
  { dot: 'bg-speaker-6', avatar: 'bg-speaker-6 text-speaker-6-ink', tint: 'bg-speaker-6/8', lane: 'bg-speaker-6/25', ring: 'ring-speaker-6', fill: 'fill-speaker-6' },
  { dot: 'bg-speaker-7', avatar: 'bg-speaker-7 text-speaker-7-ink', tint: 'bg-speaker-7/8', lane: 'bg-speaker-7/25', ring: 'ring-speaker-7', fill: 'fill-speaker-7' },
  { dot: 'bg-speaker-8', avatar: 'bg-speaker-8 text-speaker-8-ink', tint: 'bg-speaker-8/8', lane: 'bg-speaker-8/25', ring: 'ring-speaker-8', fill: 'fill-speaker-8' },
] as const

export type SpeakerClasses = (typeof SPEAKER_CLASSES)[number] | typeof UNKNOWN_SPEAKER_CLASSES

export const UNKNOWN_SPEAKER_CLASSES = {
  dot: 'bg-idle',
  avatar: 'bg-selected text-muted',
  tint: 'bg-hover',
  lane: 'bg-border',
  ring: 'ring-border-strong',
  fill: 'fill-idle',
} as const

export function speakerClasses(colorIndex: number | undefined): SpeakerClasses {
  return colorIndex === undefined ? UNKNOWN_SPEAKER_CLASSES : SPEAKER_CLASSES[colorIndex % SPEAKER_CLASSES.length]
}

/** `ms` from session start → `M:SS` / `H:MM:SS`. */
export function formatClock(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000))
  const h = Math.floor(total / 3600)
  const m = Math.floor((total % 3600) / 60)
  const s = total % 60
  const ss = String(s).padStart(2, '0')
  return h > 0 ? `${h}:${String(m).padStart(2, '0')}:${ss}` : `${m}:${ss}`
}

export type LiveIcon = ComponentType<{ className?: string; strokeWidth?: number }>

/** Per gate action: what the agent is doing ("Finding who owns …"), a
 *  short label, an icon. Unknown ids use `GATE_ACTION_FALLBACK`. */
export const GATE_ACTION_META: Record<string, { verb: string; label: string; icon: LiveIcon }> = {
  find_owner: { verb: 'Finding who owns', label: 'Owner', icon: UserSearch },
  lookup_past: { verb: 'Looking up', label: 'Past meetings', icon: History },
  fact_check: { verb: 'Fact-checking', label: 'Fact check', icon: ShieldCheck },
  recall_action_items: { verb: 'Recalling action items on', label: 'Action items', icon: ListChecks },
  define_term: { verb: 'Defining', label: 'Definition', icon: BookA },
  speaker_intro: { verb: 'Naming', label: 'Introductions', icon: IdCard },
}

export const GATE_ACTION_FALLBACK = { verb: 'Looking into', label: 'Lookup', icon: Sparkles as LiveIcon }

export const gateActionMeta = (id: string | null | undefined) => (id && GATE_ACTION_META[id]) || GATE_ACTION_FALLBACK

/** "4s ago" / "2m ago" for short live intervals. */
export function formatAgo(ms: number): string {
  const s = Math.max(0, Math.round(ms / 1000))
  if (s < 2) return 'just now'
  if (s < 60) return `${s}s ago`
  const m = Math.floor(s / 60)
  return m < 60 ? `${m}m ago` : `${Math.floor(m / 60)}h ago`
}

/** "Today 14:05" / "Tue 09:30" / "Oct 21". */
export function formatWhen(iso: string | null): string {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return '—'
  const now = new Date()
  const time = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false })
  const days = Math.floor((startOfDay(now) - startOfDay(d)) / 86_400_000)
  if (days === 0) return `Today ${time}`
  if (days === 1) return `Yesterday ${time}`
  if (days < 7) return `${d.toLocaleDateString([], { weekday: 'short' })} ${time}`
  return d.toLocaleDateString([], { month: 'short', day: 'numeric' })
}

const startOfDay = (d: Date) => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime()

/** "31 min" / "1 h 04" for durations in seconds. */
export function formatDuration(seconds: number | null): string {
  if (seconds == null || !Number.isFinite(seconds)) return '—'
  const m = Math.round(seconds / 60)
  if (m < 1) return '<1 min'
  if (m < 60) return `${m} min`
  return `${Math.floor(m / 60)} h ${String(m % 60).padStart(2, '0')}`
}
