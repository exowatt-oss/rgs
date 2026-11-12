import { describe, expect, it } from 'vitest'
import { CASTS, TEAM_CHECKIN } from './script'
import { buildStaticEvents } from './wire'

describe('fictional offline fixtures', () => {
  it('contains only example.org people and fabricated contexts', () => {
    for (const meeting of Object.values(CASTS)) {
      expect(meeting.attendees.every((a) => a.email.endsWith('@example.org'))).toBe(true)
      expect(meeting.beats.some((b) => b.t === 'say')).toBe(true)
    }
    expect(TEAM_CHECKIN.title).toContain('Sample')
  })
  it('builds the same event format the live UI consumes', () => {
    const result = buildStaticEvents(TEAM_CHECKIN, Date.now(), 'mock')
    expect(result.events.length).toBeGreaterThan(5)
    expect(result.lastSeq).toBeGreaterThan(0)
  })
})
