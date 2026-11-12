import type { LiveSpeakerPerson } from '@/api/live'
import { identityText, slackHandle } from '@/lib/live/identity'
import { cn } from '@/lib/utils'

/**
 * Who a recognised speaker is outside the call: their Slack name (when RGS
 * could look it up) and their email, from the people directory. Compact,
 * secondary text; `name` prefixes it when the row has no name of its own.
 */
export function PersonIdentity({
  person,
  name,
  className,
}: {
  person: LiveSpeakerPerson | null | undefined
  name?: string | null
  className?: string
}) {
  if (!person) return null
  const handle = slackHandle(person.slackName)
  return (
    <span className={cn('inline-flex min-w-0 items-baseline gap-1 text-[11.5px] text-muted', className)} title={identityText(person, name)}>
      {name && <span className="shrink-0 font-medium text-fg-2">{name}</span>}
      {name && <Dot />}
      {handle && (
        <>
          <span className="shrink-0 text-fg-2" aria-label={`Slack ${handle}`}>
            {handle}
          </span>
          <Dot />
        </>
      )}
      <span className="min-w-0 truncate">{person.email}</span>
    </span>
  )
}

const Dot = () => (
  <span aria-hidden className="text-subtle">
    ·
  </span>
)
