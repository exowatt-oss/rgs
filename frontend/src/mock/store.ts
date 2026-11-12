/** In-memory session registry for `?mock=1` (lists, past transcripts). */
import { normalizeSession, parseLiveEvent, type DeliveryChannel, type LiveEvent, type LiveSessionInfo } from '@/api/live'
import { ONE_ON_ONE, TEAM_CHECKIN, SITE_WALK, STANDUP, type MockMeeting } from '@/mock/script'
import { MOCK_OTHER, MOCK_USER, buildStaticEvents, envelope, mockPerson, sessionWire, speakerWire, type Wire } from '@/mock/wire'

export interface MockSessionRecord {
  meeting: MockMeeting
  status: 'live' | 'finalized'
  startedAt: number
  stoppedAt: number | null
  provider: string
  owner: 'me' | 'other'
  /** Mock recap: finished sessions of mine print an edition a few seconds after stop. */
  deliverVia?: DeliveryChannel[]
  /** Manual speaker names given after the call (archive rename). */
  renames?: Wire[]
  /** Opt-in sharing with the participants, and when it was sent. */
  share?: boolean
  sharedAt?: number
}

const HOUR = 3_600_000
const now = Date.now()

const records: MockSessionRecord[] = [
  {
    meeting: { ...STANDUP, id: 'mock-standup-live', title: 'Fictional standup' },
    status: 'live',
    startedAt: now - 6 * 60_000,
    stoppedAt: null,
    provider: 'jev',
    owner: 'other',
  },
  { meeting: TEAM_CHECKIN, status: 'finalized', startedAt: now - 26 * HOUR, stoppedAt: now - 26 * HOUR + 31 * 60_000, provider: 'openai', owner: 'me' },
  { meeting: ONE_ON_ONE, status: 'finalized', startedAt: now - 30 * HOUR, stoppedAt: now - 30 * HOUR + 22 * 60_000, provider: 'jev', owner: 'me' },
  { meeting: SITE_WALK, status: 'finalized', startedAt: now - 2 * 24 * HOUR, stoppedAt: now - 2 * 24 * HOUR + 9 * 60_000, provider: 'openai', owner: 'me' },
  {
    meeting: { ...STANDUP, id: 'mock-standup-tue', title: 'Fictional standup' },
    status: 'finalized',
    startedAt: now - 3 * 24 * HOUR,
    stoppedAt: now - 3 * 24 * HOUR + 12 * 60_000,
    provider: 'jev',
    owner: 'me',
  },
  {
    meeting: { ...TEAM_CHECKIN, id: 'mock-demo-review', title: 'Sample library review', meetCode: 'def-ghij-klm' },
    status: 'finalized',
    startedAt: now - 9 * 24 * HOUR,
    stoppedAt: now - 9 * 24 * HOUR + 44 * 60_000,
    provider: 'openai',
    owner: 'other',
  },
]

function toSession(r: MockSessionRecord): LiveSessionInfo {
  return normalizeSession(
    sessionWire(r.meeting, {
      status: r.status,
      startedAt: r.startedAt,
      stoppedAt: r.stoppedAt,
      audioSeconds: r.stoppedAt ? (r.stoppedAt - r.startedAt) / 1000 : (Date.now() - r.startedAt) / 1000,
      isOwner: r.owner === 'me',
      startedBy: r.owner === 'me' ? undefined : MOCK_OTHER,
      gateProvider: r.provider,
      recap: mockRecap(r),
      participants: mockParticipants(r),
    }),
  )
}

/** When the mock shared the recap: on "Share now", or as the recap finishes when opted in. */
function sharedAt(r: MockSessionRecord): number | null {
  if (r.sharedAt) return r.sharedAt
  return r.share && r.stoppedAt && Date.now() - r.stoppedAt >= 18_000 ? r.stoppedAt + 18_000 : null
}

/** The people a share would go to: everyone named from an intro, except me. */
function mockParticipants(r: MockSessionRecord): { email: string; name: string; label: string; slack_id: string | null; slack_name: string | null; share_status: string | null }[] {
  const seen = new Set<string>()
  const out = []
  for (const b of r.meeting.beats) {
    if (b.t !== 'intro') continue
    const person = mockPerson(r.meeting, b.name)
    if (!person || person.email === MOCK_USER.email || seen.has(person.email)) continue
    seen.add(person.email)
    out.push({ ...person, name: b.name, label: b.s, share_status: sharedAt(r) ? 'sent' : null })
  }
  return out
}

/** Walks pending → writing → printing → delivering → done over ~18 s after stop. */
function mockRecap(r: MockSessionRecord): Record<string, unknown> | null {
  if (r.status !== 'finalized' || !r.stoppedAt || r.owner !== 'me') return null
  const via = r.deliverVia ?? ['slack']
  const t = (Date.now() - r.stoppedAt) / 1000
  const status = t < 2 ? 'pending' : t < 8 ? 'writing' : t < 16 ? 'printing' : t < 18 ? 'delivering' : 'done'
  const done = status === 'done'
  const headline = r.meeting.meetCode ? 'Fictional sample team agrees on a tutorial' : 'Demo memo: add a help menu'
  return {
    enabled: true,
    status,
    deliver_via: via,
    headline: t >= 8 ? headline : null,
    summary: t >= 8 ? 'The room agreed on next steps and who owns them.' : null,
    has_notes: t >= 8,
    edition: done
      ? {
          id: `ed-${r.meeting.id}`,
          number: 1 + (records.length - records.indexOf(r)),
          status: 'published',
          paper_name: 'Fictional RGS Notes',
          share_url: 'https://example.org/demo-not-a-real-share',
          headline,
          dek: 'Three decisions, four action items, one open question.',
          published_at: new Date(r.stoppedAt + 16_000).toISOString(),
          error: null,
        }
      : null,
    delivery: done ? Object.fromEntries(via.map((c) => [c, { status: 'sent', at: new Date().toISOString() }])) : {},
    share_with_participants: !!r.share,
    shared: sharedAt(r)
      ? mockParticipants(r).map((p) => ({
          email: p.email,
          name: p.name,
          status: 'sent',
          channels: Object.fromEntries(via.map((c) => [c, 'sent'])),
          at: new Date(sharedAt(r)!).toISOString(),
        }))
      : [],
    error: null,
    updated_at: new Date().toISOString(),
  }
}

export const mockStore = {
  find(id: string): MockSessionRecord | null {
    return records.find((r) => r.meeting.id === id) ?? null
  },

  rememberFinished(meeting: MockMeeting, startedAt: number, stoppedAt: number, provider: string, deliverVia?: DeliveryChannel[], share = false): void {
    if (records.some((r) => r.meeting.id === meeting.id)) return
    records.splice(1, 0, { meeting, status: 'finalized', startedAt, stoppedAt, provider, owner: 'me', deliverVia, share })
  },

  list(active: boolean): LiveSessionInfo[] {
    return records.filter((r) => (active ? r.status === 'live' : true)).map(toSession)
  },

  get(id: string): LiveSessionInfo | null {
    const r = mockStore.find(id)
    return r ? toSession(r) : null
  },

  /** Durable events for a finished session (transcript view). */
  events(id: string): LiveEvent[] {
    const r = mockStore.find(id)
    if (!r) return []
    const { events, lastSeq } = buildStaticEvents(r.meeting, r.startedAt, r.provider)
    const renames = (r.renames ?? []).map((w, i) => envelope('speaker.update', lastSeq + 1 + i, w))
    return [...events, ...renames].map(parseLiveEvent).filter((e): e is LiveEvent => e !== null)
  },

  /** Opt in/out of sharing the recap with the participants. */
  setShare(id: string, share: boolean): LiveSessionInfo | null {
    const r = mockStore.find(id)
    if (!r || r.owner !== 'me') return null
    r.share = share
    return toSession(r)
  },

  /** "Share now": the participants get the link (instantly, in the mock). */
  shareNow(id: string): LiveSessionInfo | null {
    const r = mockStore.find(id)
    if (!r || r.owner !== 'me') return null
    r.share = true
    r.sharedAt = Date.now()
    return toSession(r)
  },

  /** Owner rename after the call; returns the speaker wire the API would. */
  renameSpeaker(id: string, label: string, displayName: string): Wire | null {
    const r = mockStore.find(id)
    if (!r || r.owner !== 'me') return null
    const wire = speakerWire(label, { display_name: displayName, source: 'manual' })
    r.renames = [...(r.renames ?? []), wire]
    return wire
  },
}
