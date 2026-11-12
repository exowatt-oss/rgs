/** Sharing a recap with the meeting participants: the non-component helpers. */
import type { DeliveryChannel, LiveRecapConfig, LiveSpeakerPerson, SessionParticipant } from '@/api/live'
import type { SpeakerEntry } from '@/lib/live/liveReducer'

export const SHARE_HINT = 'Sends the newspaper and the RGS notes to the people RGS recognised in the call, by the same channel.'

/** Why sharing can't be turned on with these channels (null = it can). */
export function shareBlockedReason(
  channels: readonly DeliveryChannel[],
  config: LiveRecapConfig | null | undefined,
  recapOn = true,
): string | null {
  if (!recapOn) return 'Needs the newspaper recap, which isn’t set up on this server yet.'
  if (channels.length === 0) return 'Pick Slack or email first: participants get the link the way you do.'
  const available = (id: DeliveryChannel) => config?.channels.find((c) => c.id === id)?.available ?? true
  if (!channels.some(available)) return `${channels.includes('slack') ? 'Slack' : 'Email'} isn’t connected on this server yet.`
  return null
}

/** "Bob Sample, Carol Demo and 2 others". */
export function namesPhrase(names: string[], max = 3): string {
  if (names.length <= max) return names.length < 2 ? (names[0] ?? '') : `${names.slice(0, -1).join(', ')} and ${names[names.length - 1]}`
  const rest = names.length - max
  return `${names.slice(0, max).join(', ')} and ${rest} other${rest === 1 ? '' : 's'}`
}

/** The speaker's directory identity, with the Slack name from the session when the event lacked it. */
export function personOf(speaker: SpeakerEntry | undefined, participants: readonly SessionParticipant[] = []): LiveSpeakerPerson | null {
  const p = speaker?.person
  if (!p) return null
  if (p.slackName) return p
  const known = participants.find((x) => x.email.toLowerCase() === p.email.toLowerCase())
  return known?.slackName ? { ...p, slackId: known.slackId, slackName: known.slackName } : p
}
