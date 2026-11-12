import { useMemo } from 'react'

import type { LiveSessionInfo } from '@/api/live'
import type { SpeakerRenaming } from '@/components/live/SpeakerName'
import { nameSuggestions } from '@/lib/live/conversation'
import { visibleSpeakers, type LiveState } from '@/lib/live/liveReducer'

/** Renaming for a transcript view: the action (null = read-only) and the
 *  names to offer — voice match, the current guess, attendees, the owner. */
export function useSpeakerRenaming(
  state: LiveState,
  session: LiveSessionInfo | null | undefined,
  rename: SpeakerRenaming['rename'],
): SpeakerRenaming {
  const { speakers, speakerOrder } = state
  return useMemo(() => {
    const cast = visibleSpeakers({ speakers, speakerOrder } as LiveState)
    return {
      rename,
      suggestionsFor: (speaker) => nameSuggestions(speaker, cast, { attendees: session?.attendees, owner: session?.startedBy }),
    }
  }, [speakers, speakerOrder, session?.attendees, session?.startedBy, rename])
}
