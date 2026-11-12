/** How a recognised speaker reads outside the call: Slack name and email. */
import type { LiveSpeakerPerson } from '@/api/live'

/** "@alice" — a Slack name as people @-mention it. */
export const slackHandle = (name: string | null | undefined): string | null => {
  const n = name?.trim()
  return n ? (n.startsWith('@') ? n : `@${n}`) : null
}

/** "Alice Example · @alice · alice@example.org" as plain text (titles, aria labels). */
export function identityText(person: LiveSpeakerPerson | null | undefined, name?: string | null): string {
  if (!person) return ''
  return [name, slackHandle(person.slackName), person.email].filter(Boolean).join(' · ')
}
