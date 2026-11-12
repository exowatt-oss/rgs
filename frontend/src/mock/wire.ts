/** Wire-format builders for the mock replay (snake_case, per the contract). */
import { LINE_PAUSE_MS, WORD_MS, type Beat, type Label, type MockMeeting } from '@/mock/script'

export type Wire = Record<string, unknown>

export const MOCK_USER = { id: 'u-alice', name: 'Alice Example', email: 'alice@example.org' }
export const MOCK_OTHER = { id: 'u-bob', name: 'Bob Sample', email: 'bob@example.org' }

export const LABELS: Label[] = ['S1', 'S2', 'S3', 'S4']
export const colorIndexOf = (label: string) => Math.max(0, LABELS.indexOf(label as Label))

export function envelope(type: string, seq: number | null, data: Wire, ts = Date.now()): Wire {
  return { type, seq, ts: new Date(ts).toISOString(), data }
}

export function speakerWire(label: string, extra: Partial<Wire> = {}): Wire {
  return {
    label,
    display_name: `Speaker ${colorIndexOf(label) + 1}`,
    color_index: colorIndexOf(label),
    speech_seconds: 0,
    user: null,
    suggestion: null,
    merged_into: null,
    ...extra,
  }
}

/** A directory person by name (the meeting's invitees), as the API's speaker `person`. */
export function mockPerson(meeting: Pick<MockMeeting, 'attendees'> | null, name: string): { email: string; name: string; slack_id: string | null; slack_name: string | null } | null {
  const hit = meeting?.attendees.find((a) => a.name === name)
  if (!hit) return null
  const handle = hit.email.split('@')[0]
  // One person without Slack, to show the email-only case.
  const slack = handle === 'dana' ? null : handle
  return { email: hit.email, name: hit.name, slack_id: slack ? `U-${handle}` : null, slack_name: slack }
}

/** The `speaker.update` wire for an `intro` / `suggest` beat. */
export function beatSpeakerWire(beat: Extract<Beat, { t: 'intro' | 'suggest' }>, meeting: Pick<MockMeeting, 'attendees'> | null = null): Wire {
  if (beat.t === 'intro') {
    return speakerWire(beat.s, {
      display_name: beat.name,
      source: 'self_intro',
      confidence: beat.confidence,
      evidence: beat.evidence,
      person: mockPerson(meeting, beat.name),
    })
  }
  const userId = `u-${beat.email.split('@')[0]}`
  return speakerWire(beat.s, { suggestion: { user_id: userId, name: beat.name, email: beat.email, score: beat.score } })
}

export interface SessionWireOptions {
  status: string
  startedAt: number
  stoppedAt?: number | null
  audioSeconds?: number
  lastSeq?: number
  isOwner?: boolean
  gateProvider?: string
  startedBy?: Wire
  speakers?: Wire[]
  recap?: Wire | null
  participants?: Wire[]
}

export function sessionWire(meeting: MockMeeting, o: SessionWireOptions): Wire {
  return {
    id: meeting.id,
    meeting_id: `m-${meeting.id}`,
    meet_code: meeting.meetCode || null,
    meet_url: meeting.meetCode ? `https://meet.google.com/${meeting.meetCode}` : null,
    kind: meeting.meetCode ? 'call' : 'adhoc',
    recap: o.recap ?? null,
    title: meeting.title,
    status: o.status,
    linked_calendar_event: true,
    started_by: o.startedBy ?? MOCK_USER,
    is_owner: o.isOwner ?? true,
    started_at: new Date(o.startedAt).toISOString(),
    stopped_at: o.stoppedAt ? new Date(o.stoppedAt).toISOString() : null,
    audio_seconds: o.audioSeconds ?? 0,
    last_seq: o.lastSeq ?? 0,
    asr_state: 'ok',
    transcript_id: o.status === 'finalized' ? `t-${meeting.id}` : null,
    error: null,
    speakers: o.speakers ?? [],
    attendees: meeting.attendees,
    participants: o.participants ?? [],
    shared_with_you: false,
    gate_provider: o.gateProvider ?? 'openai',
  }
}

export function tickWire(provider: string, decision = 'none', p?: number): Wire {
  const slow = provider === 'openai'
  const latency = Math.round((slow ? 320 : 90) + Math.random() * (slow ? 380 : 170))
  return {
    provider,
    latency_ms: latency,
    considered_lines: 2 + Math.floor(Math.random() * 4),
    decision,
    p: p ?? Number((0.02 + Math.random() * 0.16).toFixed(2)),
  }
}

export function triggerWire(o: {
  id: string
  action: string
  query: string
  seqFrom: number
  seqTo: number
  provider: string
  topic: string
  context: string
}): Wire {
  return {
    trigger_id: o.id,
    action: o.action,
    query: o.query,
    seq_from: o.seqFrom,
    seq_to: o.seqTo,
    probability: Number((0.86 + Math.random() * 0.12).toFixed(2)),
    jev_latency_ms: Math.round(120 + Math.random() * 300),
    anchor_seq: o.seqTo,
    topic: o.topic,
    context_text: o.context,
    decided_at: new Date().toISOString(),
    provider: o.provider,
    status: 'pending',
  }
}

export function findingWire(o: {
  id: string
  seq: number
  title: string
  body: string
  links: { title: string; url: string }[]
  relatedSeq: number
  triggerId: string
  at?: number
}): Wire {
  return {
    id: o.id,
    seq: o.seq,
    title: o.title,
    body_md: o.body,
    links: o.links,
    related_seq: o.relatedSeq,
    trigger_id: o.triggerId,
    source_agent: 'rgs',
    created_by: MOCK_USER,
    created_at: new Date(o.at ?? Date.now()).toISOString(),
  }
}

/** The whole meeting as durable events, instantly (past sessions and
 *  sessions joined as a viewer). Times are laid out as if spoken. */
export function buildStaticEvents(meeting: MockMeeting, startedAt: number, provider = 'openai'): { events: Wire[]; durationS: number; lastSeq: number } {
  const events: Wire[] = []
  let seq = 0
  let clockMs = 1200
  const finals: { seq: number; s: string; text: string }[] = []
  const names: Record<string, string> = {}
  let n = 0
  for (const beat of meeting.beats) {
    if (beat.t === 'say') {
      clockMs += beat.pauseMs ?? LINE_PAUSE_MS
      const words = beat.text.split(/\s+/).length
      const start = clockMs
      clockMs += words * WORD_MS * 2.2
      seq += 1
      finals.push({ seq, s: beat.s, text: beat.text })
      events.push(
        envelope('segment.final', seq, { seq, speaker: beat.s, start_s: start / 1000, end_s: clockMs / 1000, text: beat.text }, startedAt + clockMs),
      )
    } else if (beat.t === 'intro' || beat.t === 'suggest') {
      seq += 1
      if (beat.t === 'intro') names[beat.s] = beat.name
      events.push(envelope('speaker.update', seq, beatSpeakerWire(beat, meeting), startedAt + clockMs))
    } else {
      const last = finals[finals.length - 1]
      const from = finals[Math.max(0, finals.length - 1 - beat.back)]
      const triggerId = `${meeting.id}-trig-${(n += 1)}`
      seq += 1
      events.push(
        envelope(
          'gate.trigger',
          seq,
          triggerWire({
            id: triggerId,
            action: beat.action,
            query: beat.query,
            seqFrom: from.seq,
            seqTo: last.seq,
            provider,
            topic: last.text,
            context: finals.slice(-4).map((f) => `${names[f.s] ?? f.s}: ${f.text}`).join('\n'),
          }),
          startedAt + clockMs,
        ),
      )
      seq += 1
      events.push(
        envelope(
          'finding.new',
          seq,
          findingWire({ id: `${triggerId}-f`, seq, ...beat.finding, relatedSeq: last.seq, triggerId, at: startedAt + clockMs + beat.answerMs }),
          startedAt + clockMs + beat.answerMs,
        ),
      )
    }
  }
  return { events, durationS: Math.round(clockMs / 1000 + 4), lastSeq: seq }
}
