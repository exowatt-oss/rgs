/** Entirely fictional replay. No production meetings, people, sites or transcripts. */
export type Label = 'S1' | 'S2' | 'S3' | 'S4'
export interface MockFinding {
  title: string
  body: string
  links: { title: string; url: string }[]
}
export type Beat =
  | { t: 'say'; s: Label; text: string; pauseMs?: number }
  | { t: 'intro'; s: Label; name: string; evidence: string; confidence: number }
  | { t: 'suggest'; s: Label; name: string; email: string; score: number }
  | { t: 'ask'; action: string; query: string; back: number; answerMs: number; finding: MockFinding }
export interface MockMeeting {
  id: string
  title: string
  meetCode: string
  attendees: { name: string; email: string }[]
  beats: Beat[]
}
export const ATTENDEES = [
  { name: 'Alice Example', email: 'alice@example.org' },
  { name: 'Bob Sample', email: 'bob@example.org' },
  { name: 'Carol Demo', email: 'carol@example.org' },
  { name: 'Dana Test', email: 'dana@example.org' },
]
const INTRO: Beat[] = [
  { t: 'say', s: 'S1', text: "Hi, I'm Alice. Let's review our imaginary library app." },
  { t: 'intro', s: 'S1', name: 'Alice Example', evidence: "I'm Alice", confidence: 0.92 },
  { t: 'say', s: 'S2', text: "I'm Bob. The search demo is ready." },
  { t: 'intro', s: 'S2', name: 'Bob Sample', evidence: "I'm Bob", confidence: 0.90 },
  { t: 'say', s: 'S3', text: 'Who owns the sample project documentation?' },
  { t: 'suggest', s: 'S3', name: 'Carol Demo', email: 'carol@example.org', score: 0.8 },
  { t: 'ask', action: 'find_owner', query: 'sample project documentation', back: 0, answerMs: 1200,
    finding: { title: 'Carol owns the example documentation',
      body: 'This is a fabricated response from the offline demo, not an answer from a real knowledge source.',
      links: [{ title: 'Example documentation', url: 'https://example.org/docs' }] } },
  { t: 'say', s: 'S4', text: 'What did we decide about the fictional launch date?' },
  { t: 'ask', action: 'lookup_past', query: 'fictional launch date', back: 0, answerMs: 1000,
    finding: { title: 'The demo launch is next Tuesday', body: 'Illustrative data only. Check the actual project records.', links: [] } },
]
export const TEAM_CHECKIN: MockMeeting = {
  id: 'mock-sample-team', title: 'Sample team check-in', meetCode: 'abc-defg-hij',
  attendees: ATTENDEES, beats: INTRO,
}
export const STANDUP: MockMeeting = {
  id: 'mock-demo-standup', title: 'Fictional standup', meetCode: 'bcd-efgh-ijk',
  attendees: ATTENDEES, beats: [
    { t: 'say', s: 'S1', text: "I'm Carol. Today's example task is documentation." },
    { t: 'intro', s: 'S1', name: 'Carol Demo', evidence: "I'm Carol", confidence: 0.9 },
    { t: 'say', s: 'S2', text: 'I created the sample search screen.' },
    { t: 'say', s: 'S3', text: 'Should the fake release include a tutorial?' },
    { t: 'ask', action: 'look_up', query: 'sample tutorial', back: 0, answerMs: 900,
      finding: { title: 'Sample tutorial', body: 'Demonstration output; not a factual source.', links: [] } },
  ],
}
export const SITE_WALK: MockMeeting = {
  id: 'mock-solo-notes', title: 'Demo voice memo', meetCode: '', attendees: ATTENDEES,
  beats: [
    { t: 'say', s: 'S1', text: "I'm Dana. This is a fictional voice memo about a library prototype." },
    { t: 'intro', s: 'S1', name: 'Dana Test', evidence: "I'm Dana", confidence: 0.9 },
    { t: 'say', s: 'S1', text: 'We should add a sample index page and a simple help menu.' },
    { t: 'say', s: 'S1', text: 'Who owns the demo help menu?' },
    { t: 'ask', action: 'find_owner', query: 'demo help menu', back: 0, answerMs: 900,
      finding: { title: 'Alice owns the demo help menu', body: 'Fictional test data.', links: [] } },
  ],
}
export const ONE_ON_ONE: MockMeeting = {
  id: 'mock-alice-bob', title: 'Alice / Bob 1:1 (fictional)', meetCode: 'cde-fghi-jkl',
  attendees: ATTENDEES.slice(0, 2), beats: [
    { t: 'say', s: 'S1', text: "I'm Alice. How is the sample app?" },
    { t: 'intro', s: 'S1', name: 'Alice Example', evidence: "I'm Alice", confidence: 0.9 },
    { t: 'say', s: 'S2', text: "I'm Bob. The fake search screen is done." },
    { t: 'intro', s: 'S2', name: 'Bob Sample', evidence: "I'm Bob", confidence: 0.9 },
    { t: 'say', s: 'S1', text: 'What was the name of the imaginary release?' },
    { t: 'ask', action: 'lookup_past', query: 'imaginary release name', back: 0, answerMs: 900,
      finding: { title: 'Example Release', body: 'This is synthetic demo content.', links: [] } },
  ],
}
export const CASTS: Record<1 | 2 | 3 | 4, MockMeeting> = {
  1: SITE_WALK, 2: ONE_ON_ONE, 3: STANDUP, 4: TEAM_CHECKIN,
}
export type CastSize = keyof typeof CASTS
export function castFromUrl(): CastSize | null {
  if (typeof window === 'undefined') return null
  const n = Number(new URLSearchParams(window.location.search).get('cast'))
  return n === 1 || n === 2 || n === 3 || n === 4 ? n : null
}
export const WORD_MS = 150
export const LINE_PAUSE_MS = 1000
